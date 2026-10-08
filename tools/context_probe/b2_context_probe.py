"""0xB2 experiment: byte stands for a whole destroyed vowel (mostly German
umlaut vowels), so language alone can't pick the character. Test: for each
occurrence, fill the gap with a/o/u, look the flattened word up in the
clean-corpus lexicon (working/lexicon_GTU.pkl, built by lang_lexicon_probe.py),
and see which umlaut vowel makes a real word that appears with an umlaut
elsewhere in clean text.

Confidence dial: --min-confidence (share of lexicon tokens behind the winning
vowel) and --min-support (tokens). Language (008 + the polyglot detector's
German profile) is reported alongside so you can see whether gating on
"German" would be needed.

Nothing here modifies marc_repair.py.
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
sys.path.insert(0, HERE)
import marc_repair as m  # noqa: E402
from lang_lexicon_probe import record_langs  # noqa: E402
from a5_context_probe import ContextDetector, record_text  # noqa: E402

BYTE = '\xb2'
GAP = '\x00'
UMLAUT = '̈'
VOWELS = 'aou'
MARKER_RE = re.compile(r'(?:\x1b(?!\()[^\x1b\xb2]{1,2})+\xb2')
JUNK_RE = re.compile(r'\x1b(?!\()[^\x1b]{1,2}|\x1b\(Q?[A-Z0-9]')
WORD_RE = re.compile('[A-Za-z\x00]+')


def words_with_gaps(data: str):
    """-> [(word_with_GAP_chars, start_of_word_in_marked_text)]"""
    marked = MARKER_RE.sub(GAP, data)
    marked = JUNK_RE.sub('', marked)
    return [mm.group() for mm in WORD_RE.finditer(marked) if GAP in mm.group()]


def candidates(word: str):
    """All fillings of the gaps with a/o/u -> [(filled lowercase, vowels)]"""
    n = word.count(GAP)
    if n > 2:
        return []
    out = [(word.lower(), [])]
    for _ in range(n):
        nxt = []
        for w, vs in out:
            for v in VOWELS:
                nxt.append((w.replace(GAP, v, 1), vs + [v]))
        out = nxt
    return out


def has_umlaut_at(variant: str, positions: list[int]) -> bool:
    nfd = unicodedata.normalize('NFD', variant).lower()
    base = -1
    marked = set()
    for c in nfd:
        if unicodedata.category(c) == 'Mn':
            if c == UMLAUT:
                marked.add(base)
        else:
            base += 1
    return all(p in marked for p in positions)


def resolve(word: str, glob):
    """-> (votes {vowel-tuple: tokens}, best variant per vowel-tuple)"""
    votes: Counter = Counter()
    best: dict = {}
    gap_positions = [i for i, c in enumerate(word) if c == GAP]
    for filled, vowels in candidates(word):
        table = glob.get(filled)
        if not table:
            continue
        for variant, count in table.items():
            if has_umlaut_at(variant, gap_positions):
                votes[tuple(vowels)] += count
                if tuple(vowels) not in best or count > table[best[tuple(vowels)]]:
                    best[tuple(vowels)] = variant
    return votes, best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--min-confidence', type=float, default=0.8)
    ap.add_argument('--min-support', type=int, default=2)
    ap.add_argument('--min-length', type=int, default=0,
                    help="accept only words at least this long (gap counts)")
    ap.add_argument('--sample', default='working/GTU_bibs_0xB2_sample.mrc')
    ap.add_argument('--cache', default='working/lexicon_GTU.pkl')
    args = ap.parse_args()
    from pymarc.marc8 import marc8_to_unicode
    m.marc8_to_unicode = marc8_to_unicode
    with open(args.cache, 'rb') as fh:
        glob = pickle.load(fh)['glob']
    det = ContextDetector()

    rows = []
    for parsed, _ in m.iter_repair_stream(args.sample):
        if parsed.unresolved:
            continue
        lang008, _ = record_langs(parsed)
        res = det.detect(record_text(parsed))
        rec_id = m.record_identifier(parsed)
        for f in parsed.fields:
            if f.is_control():
                continue
            for code, data in f.subfields:
                if not data or BYTE not in data:
                    continue
                text = m._MARC8_DIACRITIC_ESCAPE_RE.sub(
                    m._marc8_diacritic_replacement, data)
                for word in words_with_gaps(text):
                    votes, best = resolve(word, glob)
                    total = sum(votes.values())
                    top = votes.most_common(1)
                    pick = top[0][0] if top else ()
                    conf = top[0][1] / total if top else 0.0
                    if (total >= args.min_support and conf >= args.min_confidence
                            and len(word) >= args.min_length):
                        tier = 'accept'
                    elif total and conf >= 0.5:
                        tier = 'review'
                    else:
                        tier = 'reject'
                    rows.append({
                        'record': rec_id, 'field': f'={f.tag} ${code}',
                        'lang008': lang008, 'detected': res['language'],
                        'word': word.replace(GAP, '_'),
                        'gaps': word.count(GAP), 'support': total,
                        'confidence': round(conf, 3),
                        'pick': ''.join(pick),
                        'resolved_word': (unicodedata.normalize('NFC', best[pick])
                                          if pick else ''),
                        'n_vowel_options': len(votes), 'tier': tier,
                    })
    out = 'working/b2_context_probe.tsv'
    with open(out, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter='\t')
        w.writeheader()
        w.writerows(rows)

    print(f'{len(rows)} gapped words -> {out}')
    print('tiers overall:', dict(Counter(r['tier'] for r in rows)))
    by = defaultdict(Counter)
    for r in rows:
        by['ger' if r['lang008'] == 'ger' else
           'German-detected' if r['detected'] == 'German' else 'other'][r['tier']] += 1
    for k, v in by.items():
        print(f'  {k:16} {dict(v)}')
    acc = [r for r in rows if r['tier'] == 'accept']
    print('accepted vowel picks:', dict(Counter(r['pick'] for r in acc)))
    print('\nladder (support>=%d):' % args.min_support)
    for t in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
        n = sum(1 for r in rows if r['support'] >= args.min_support
                and r['confidence'] >= t)
        print(f'  conf >= {t}: {n}')
    print('\n--- sample accepted ---')
    for r in acc[:25]:
        print(f"  {r['record']:11} {r['lang008']:3} {r['word']:22} -> "
              f"{r['resolved_word']:22} conf={r['confidence']} n={r['support']}")
    print('\n--- sample ambiguous (review) ---')
    for r in [x for x in rows if x['tier'] == 'review'][:10]:
        print(f"  {r['record']:11} {r['lang008']:3} {r['word']:22} -> "
              f"{r['resolved_word']:22} conf={r['confidence']} n={r['support']}")
    print('\n--- most common UNRESOLVED words (lexicon has no entry) ---')
    miss = Counter(r['word'] for r in rows if r['tier'] == 'reject')
    for w, c in miss.most_common(25):
        print(f'  {c:3} {w}')


if __name__ == '__main__':
    main()
