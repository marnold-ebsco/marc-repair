"""Probe: can record language (008/35-37, 041) plus a lexicon of cleanly
encoded diacritic words resolve overloaded bare MARC-8 bytes (0xA5, 0xAE, ...)?

Two steps, both in this one file:

  build    scan a full corpus once, collect every word from corruption-free
           subfields that carries a diacritic (decoded with pymarc), keyed by
           its flattened ASCII form, with counts overall and per 008 language.
           Cached as a pickle -- only needs doing once per corpus.

  analyze  for each occurrence of one target byte in a (small) sample file,
           flatten the surrounding garbled word, look it up in the lexicon,
           and report which (offset, mark) hypothesis the lexicon supports,
           how strongly, and whether it agrees with marc_repair.py's current
           rule for that byte.

Certainty is a dial, not a switch. Every lookup yields a confidence (share of
lexicon evidence, by count, behind the winning hypothesis) and a support
(how many corpus tokens that is). A row is:
    accept  conf >= --min-confidence and support >= --min-support
    review  conf >= --review-confidence (and support >= 1)
    reject  otherwise / no lexicon match
The summary also prints a ladder of thresholds so you can see what each lesser
percentage would buy -- and, on rows the existing rule already fixes (believed
correct), how often the lexicon agrees at that threshold: an empirical
precision estimate for the lexicon itself.

Nothing here modifies marc_repair.py or any data file.
"""
from __future__ import annotations

import argparse
import csv
import pickle
import re
import os
import sys
import unicodedata
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..'))
import marc_repair as m  # noqa: E402

CORPUS = 'working/GTU_bibs.mrc'
LEXICON_CACHE = 'working/lexicon_GTU.pkl'

#: ANSEL combining byte -> the Unicode combining mark it becomes.
ANSEL_TO_MARK = {
    '\xf2': '̣',  # dot below
    '\xf0': '̧',  # cedilla
    '\xf1': '̨',  # ogonek
    '\xe8': '̈',  # diaeresis
}

WORD_RE = re.compile(r'(?:[^\W\d_]|[̀-ͯ])+')
ANSEL_PAIR_RE = re.compile('[\xe0-\xfe]+[A-Za-z]')

_TRIG = re.escape(m._MARC8_BARE_TRIGGER_BYTES)
JUNK = r'(?:\x1b(?!\()[^\x1b' + _TRIG + r']{1,2})'
TOKEN_RE = re.compile(
    r'(?P<trig>' + JUNK + r'+(?P<byte>[' + _TRIG + r']))'
    r'|(?P<junk>' + JUNK + r'+)'
    r'|(?P<desig>\x1b\(Q?[A-Z0-9])'
    r'|(?P<ansel>[\xe0-\xfe]+[A-Za-z])'
    r'|(?P<letter>[A-Za-z])'
)


def flatten(word: str) -> str | None:
    """NFD, drop combining marks, lowercase; None unless pure a-z remains."""
    nfd = unicodedata.normalize('NFD', word)
    flat = ''.join(c for c in nfd if unicodedata.category(c) != 'Mn').lower()
    return flat if flat.isascii() and flat.isalpha() else None


def marks_by_index(word: str) -> dict[int, str]:
    """NFD word -> {base-letter index: combining marks on it}."""
    out: dict[int, str] = {}
    idx = -1
    for c in unicodedata.normalize('NFD', word).lower():
        if unicodedata.category(c) == 'Mn':
            if idx >= 0:
                out[idx] = out.get(idx, '') + c
        else:
            idx += 1
    return out


def record_langs(parsed) -> tuple[str, str]:
    lang008 = lang041 = ''
    for f in parsed.fields:
        if f.tag == '008' and f.is_control() and f.content:
            lang008 = f.content[35:38]
        elif f.tag == '041' and not f.is_control() and not lang041:
            lang041 = ','.join(d for c, d in f.subfields if c == 'a')
    return lang008, lang041


def decode_clean(data: str) -> str | None:
    try:
        return m.marc8_to_unicode(data.encode('latin-1'), hide_utf8_warnings=True)
    except Exception:
        return None


# ---------------------------------------------------------------- build
def build(corpus: str, cache: str) -> None:
    from pymarc.marc8 import marc8_to_unicode
    m.marc8_to_unicode = marc8_to_unicode
    glob_lex: dict[str, Counter] = defaultdict(Counter)
    lang_lex: dict[tuple[str, str], Counter] = defaultdict(Counter)
    plain: Counter = Counter()
    n = 0
    for parsed, _ in m.iter_repair_stream(corpus):
        n += 1
        if n % 20000 == 0:
            print(f'  {n} records, {len(glob_lex)} diacritic keys', file=sys.stderr)
        if parsed.unresolved or parsed.leader[9:10] == m.UNICODE_ENCODING_BYTE:
            continue
        lang = record_langs(parsed)[0]
        for f in parsed.fields:
            if f.is_control():
                continue
            for _, data in f.subfields:
                if not data or '\x1b' in data:
                    continue  # corrupted (or escape-switched) -- not clean evidence
                if ANSEL_PAIR_RE.search(data):
                    text = decode_clean(data)
                    if text is None:
                        continue
                else:
                    text = data
                if not text.isascii():
                    text = unicodedata.normalize('NFC', text)
                for w in WORD_RE.findall(text):
                    key = flatten(w)
                    if key is None or len(key) < 3:
                        continue
                    if w.isascii():
                        plain[key] += 1
                        continue
                    var = unicodedata.normalize('NFD', w).lower()
                    glob_lex[key][var] += 1
                    lang_lex[(lang, key)][var] += 1
    print(f'built from {n} records: {len(glob_lex)} diacritic keys', file=sys.stderr)
    with open(cache, 'wb') as fh:
        pickle.dump({'glob': dict(glob_lex), 'lang': dict(lang_lex), 'plain': plain}, fh)


LC_LABEL_RE = re.compile(
    r'<http://www\.w3\.org/2004/02/skos/core#(?:prefLabel|altLabel)> "((?:[^"\\]|\\.)*)"')
NT_ESCAPE_RE = re.compile(r'\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})')


def lc_words(nt_gz: str, out: str) -> None:
    """Stream an id.loc.gov SKOS N-Triples dump and write every distinct word
    containing a non-ASCII letter from its prefLabel/altLabel values, one per
    line, for `merge`."""
    import gzip
    seen: set[str] = set()
    with gzip.open(nt_gz, 'rt', encoding='utf-8') as fh:
        for line in fh:
            hit = LC_LABEL_RE.search(line)
            if not hit or hit.group(1).isascii() and '\\u' not in hit.group(1):
                continue
            label = NT_ESCAPE_RE.sub(
                lambda g: chr(int(g.group(1) or g.group(2), 16)), hit.group(1))
            if not label.isascii():
                label = unicodedata.normalize('NFC', label)
                seen.update(w.lower() for w in WORD_RE.findall(label) if not w.isascii())
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(sorted(seen)) + '\n')
    print(f'{len(seen)} distinct diacritic words -> {out}', file=sys.stderr)


def merge_wordlist(base: str, wordlist: str, out: str, weight: int = 1) -> None:
    """Add the diacritic words of a plain UTF-8 word list (one per line) to a
    copy of the corpus lexicon. The list has no frequencies, so each variant
    gets `weight`; ambiguous pairs (moechte/maechte) then split their
    confidence instead of looking certain."""
    with open(base, 'rb') as fh:
        lex = pickle.load(fh)
    added = 0
    with open(wordlist, encoding='utf-8') as fh:
        for line in fh:
            w = line.strip()
            if w.isascii() or not WORD_RE.fullmatch(w):
                continue
            key = flatten(w)
            if key is None or len(key) < 3:
                continue
            lex['glob'].setdefault(key, Counter())[
                unicodedata.normalize('NFD', w).lower()] += weight
            added += 1
    print(f'merged {added} diacritic words; {len(lex["glob"])} keys', file=sys.stderr)
    with open(out, 'wb') as fh:
        pickle.dump(lex, fh)


def trim_for_b2(base: str, out: str) -> None:
    """Write the small JSON lexicon `marc_repair.py --b2-lexicon` loads: only
    variants carrying an a/o/u umlaut (all 0xB2 can stand for), as
    {flattened_key: {nfd_variant: count}}. JSON, not pickle, so it is safe to
    hand around."""
    import json
    with open(base, 'rb') as fh:
        glob = pickle.load(fh)['glob']
    trimmed = {}
    for key, table in glob.items():
        keep = {v: c for v, c in table.items()
                if any(ch in unicodedata.normalize('NFC', v) for ch in 'äöü')}
        if keep:
            trimmed[key] = keep
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(trimmed, fh, ensure_ascii=False, separators=(',', ':'))
    print(f'{len(trimmed)} keys -> {out}', file=sys.stderr)


# ---------------------------------------------------------------- analyze
def tokenize(text: str):
    """Yield words as (letters, markers): letters = flat lowercase chars;
    markers = [(index of the letter before it, trigger byte)]."""
    letters: list[str] = []
    markers: list[tuple[int, str]] = []
    pos = 0
    words = []

    def flush():
        nonlocal letters, markers
        if letters or markers:
            words.append((letters, markers))
        letters, markers = [], []

    for mm in TOKEN_RE.finditer(text):
        if mm.start() != pos:
            flush()  # a gap of non-word characters
        pos = mm.end()
        if mm.group('trig'):
            markers.append((len(letters) - 1, mm.group('byte')))
        elif mm.group('ansel'):
            decoded = decode_clean(mm.group('ansel'))
            flat = flatten(decoded) if decoded else None
            letters.extend(flat if flat else [mm.group('ansel')[-1].lower()])
        elif mm.group('letter'):
            letters.append(mm.group('letter').lower())
        # junk / desig: lost marks we can't place -- dropped from the word
    flush()
    return words


def hypothesis_for(variant: str, idx: int, window: int):
    """Nearest marked letter at or before idx within window -> (offset, mark)."""
    marks = marks_by_index(variant)
    for off in range(window + 1):
        if idx - off in marks:
            return off, marks[idx - off][0]
    return None


def evaluate(key: str, idx: int, glob, lang_lex, lang: str, window: int,
             min_support: int):
    """-> (source, {(offset, mark): count}, {(offset, mark): top variant})."""
    for source, table in (('lang', lang_lex.get((lang, key))), ('global', glob.get(key))):
        if not table:
            continue
        votes: Counter = Counter()
        best: dict = {}
        for variant, count in table.items():
            if len(flatten(variant) or '') != len(key):
                continue
            hyp = hypothesis_for(variant, idx, window)
            if hyp is None:
                continue
            votes[hyp] += count
            if hyp not in best or count > table[best[hyp]]:
                best[hyp] = variant
        if sum(votes.values()) >= min_support or source == 'global':
            return source, votes, best
    return 'none', Counter(), {}


def rule_hypothesis(byte: str, before: str):
    """What marc_repair.py's current rule does for this occurrence."""
    override = m._MARC8_BARE_COMBINING_BEFORE_OVERRIDES.get(byte, {}).get(before.lower())
    mark = override or m._MARC8_BARE_COMBINING_BYTES.get(byte)
    allowed = m._MARC8_BARE_COMBINING_RESTRICTED_BEFORE.get(byte)
    blocked = allowed is not None and before.lower() not in allowed
    if override is None and (mark is None or blocked):
        return None
    return 0, ANSEL_TO_MARK.get(mark, '?')


def analyze(args) -> None:
    from pymarc.marc8 import marc8_to_unicode
    m.marc8_to_unicode = marc8_to_unicode
    with open(args.cache, 'rb') as fh:
        lex = pickle.load(fh)
    glob, lang_lex, plain = lex['glob'], lex['lang'], lex['plain']
    target = chr(int(args.byte, 16))
    rows = []
    for parsed, _ in m.iter_repair_stream(args.sample):
        if parsed.unresolved:
            continue
        lang008, lang041 = record_langs(parsed)
        rec_id = m.record_identifier(parsed)
        has880 = any(f.tag == '880' for f in parsed.fields)
        for f in parsed.fields:
            if f.is_control():
                continue
            for code, data in f.subfields:
                if not data or '\x1b' not in data:
                    continue
                text = m._MARC8_DIACRITIC_ESCAPE_RE.sub(
                    m._marc8_diacritic_replacement, data)
                for letters, markers in tokenize(text):
                    for idx, byte in markers:
                        if byte != target or idx < 0:
                            continue
                        key = ''.join(letters)
                        before = letters[idx]
                        rule = rule_hypothesis(byte, before)
                        source, votes, best = evaluate(
                            key, idx, glob, lang_lex, lang008, args.window,
                            args.min_support)
                        total = sum(votes.values())
                        top = votes.most_common(1)
                        hyp = top[0][0] if top else None
                        conf = top[0][1] / total if top else 0.0
                        if total and conf >= args.min_confidence and total >= args.min_support:
                            tier = 'accept'
                        elif total and conf >= args.review_confidence:
                            tier = 'review'
                        else:
                            tier = 'reject'
                        shown = ''.join(
                            (c + ('[%s]' % target.encode('latin-1').hex()
                                  if i in {mi for mi, mb in markers if mb == target}
                                  else ''))
                            for i, c in enumerate(letters))
                        rows.append({
                            'record_id': rec_id, 'field': f'={f.tag} ${code}',
                            'lang008': lang008, 'lang041': lang041,
                            'has880': int(has880), 'word': shown, 'before': before,
                            'rule_fires': int(rule is not None),
                            'source': source, 'support': total,
                            'plain_count': plain.get(key, 0),
                            'top_offset': hyp[0] if hyp else '',
                            'top_mark': (unicodedata.name(hyp[1], hyp[1])
                                         if hyp else ''),
                            'top_variant': (unicodedata.normalize('NFC', best[hyp])
                                            if hyp else ''),
                            'confidence': round(conf, 3),
                            'n_hypotheses': len(votes),
                            'agrees_with_rule': (int(hyp == rule)
                                                 if rule and hyp else ''),
                            'tier': tier,
                        })
    out = f'working/lexicon_probe_{args.byte}.tsv'
    with open(out, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter='\t')
        w.writeheader()
        w.writerows(rows)
    print(f'{len(rows)} occurrences of 0x{args.byte} -> {out}\n')
    summarize(rows, args)


def summarize(rows, args) -> None:
    ladder = [1.0, 0.95, 0.9, 0.8, 0.7, 0.6, 0.5]
    matched = [r for r in rows if r['support']]
    print(f'lexicon matched {len(matched)}/{len(rows)} occurrences')
    print('by 008 language (all occurrences):',
          dict(Counter(r['lang008'] or '(blank)' for r in rows).most_common(8)))
    fixed = [r for r in rows if r['rule_fires']]
    left = [r for r in rows if not r['rule_fires']]
    print(f'\nrule already fixes {len(fixed)} (calibration); '
          f'{len(left)} leftover (the interesting ones)\n')
    print(f'{"min conf":>8} | {"calib: lexicon agrees":>22} | '
          f'{"leftover resolved":>17} | top leftover hypotheses')
    for t in ladder:
        ok = [r for r in fixed if r['support'] >= args.min_support
              and r['confidence'] >= t]
        agree = sum(1 for r in ok if r['agrees_with_rule'] == 1)
        got = [r for r in left if r['support'] >= args.min_support
               and r['confidence'] >= t]
        hyps = Counter((r['top_offset'], r['top_mark'].replace('COMBINING ', ''))
                       for r in got)
        calib = f'{agree}/{len(ok)}' + (f' ({agree / len(ok):.0%})' if ok else '')
        print(f'{t:>8.2f} | {calib:>22} | {len(got):>17} | '
              f'{dict(hyps.most_common(3))}')
    print(f'\nleftover rows, tier at --min-confidence {args.min_confidence} / '
          f'--review-confidence {args.review_confidence} / '
          f'--min-support {args.min_support}:')
    print(' ', dict(Counter(r['tier'] for r in left)))
    print('  by before-letter x tier:')
    by = defaultdict(Counter)
    for r in left:
        by[r['before']][r['tier']] += 1
    for k, v in sorted(by.items()):
        print(f'    {k}: {dict(v)}')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--corpus', default=CORPUS)
    b.add_argument('--cache', default=LEXICON_CACHE)
    lw = sub.add_parser('lc-words')
    lw.add_argument('--nt-gz', required=True)
    lw.add_argument('--out', required=True)
    g = sub.add_parser('merge')
    g.add_argument('--wordlist', required=True)
    g.add_argument('--base', default=LEXICON_CACHE)
    g.add_argument('--out', required=True)
    g.add_argument('--weight', type=int, default=1)
    t = sub.add_parser('trim')
    t.add_argument('--base', required=True)
    t.add_argument('--out', required=True)
    a = sub.add_parser('analyze')
    a.add_argument('--byte', required=True, help='hex, e.g. a5')
    a.add_argument('--sample', help='default: working/GTU_bibs_0x<BYTE>_sample.mrc')
    a.add_argument('--cache', default=LEXICON_CACHE)
    a.add_argument('--window', type=int, default=2,
                   help='how many letters back a mark may sit from the marker')
    a.add_argument('--min-confidence', type=float, default=0.8,
                   help='accept threshold; 1.0 = require unanimity (default 0.8)')
    a.add_argument('--review-confidence', type=float, default=0.5)
    a.add_argument('--min-support', type=int, default=2,
                   help='min lexicon tokens behind the winning hypothesis')
    args = ap.parse_args()
    if args.cmd == 'build':
        build(args.corpus, args.cache)
    elif args.cmd == 'lc-words':
        lc_words(args.nt_gz, args.out)
    elif args.cmd == 'merge':
        merge_wordlist(args.base, args.wordlist, args.out, args.weight)
    elif args.cmd == 'trim':
        trim_for_b2(args.base, args.out)
    else:
        args.byte = args.byte.lower().replace('0x', '')
        args.sample = args.sample or f'working/GTU_bibs_0x{args.byte.upper()}_sample.mrc'
        analyze(args)


if __name__ == '__main__':
    main()
