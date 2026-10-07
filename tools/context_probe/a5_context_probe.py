"""0xA5 context experiment: does language context decide how to encode 0xA5?

Extends MARCPolyglotDetector with English / Sanskrit (IAST) / Arabic /
Greek transliteration profiles, scores the whole record's text (plus the
record's 008/041 language codes as a prior), then applies a small
language -> (mark, letters, how far back) table to every 0xA5 occurrence in
working/GTU_bibs_0xA5_sample.mrc. Compares against marc_repair.py's current
rule (dot below on an immediately preceding 'r', nothing else).

Certainty dial: --min-confidence. Below it the proposal falls back to the
current rule, so 1.01 reproduces today's behaviour exactly.

Nothing here modifies marc_repair.py or any data file.
"""
from __future__ import annotations

import argparse
import csv
import re
import os
import sys
import unicodedata
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..'))
sys.path.insert(0, HERE)
import marc_repair as m  # noqa: E402
from polyglot_detector_marc import MARCPolyglotDetector  # noqa: E402
from lang_lexicon_probe import record_langs, tokenize  # noqa: E402

BYTE = '\xa5'
CODE_PRIOR_BONUS = 20.0
WORD_WEIGHT = 4.0
MIN_EVIDENCE = 10.0
MIN_WORD_EVIDENCE = 1.0
DOT_BELOW = '̣'
TEXT_TAGS = ('100', '110', '130', '240', '245', '246', '490', '500', '505',
             '520', '600', '610', '650', '651', '700', '710', '730', '740', '830')

#: 008/041 language codes that act as a prior for each context class.
CODE_PRIOR = {
    'sanskrit': {'san', 'hin', 'pli', 'ben', 'mar', 'pra', 'nep', 'tam', 'tel',
                 'kan', 'mal', 'guj', 'pan', 'tib', 'bod'},
    'arabic': {'ara', 'per', 'urd', 'heb', 'tur', 'ota', 'kur'},
    'greek': {'gre', 'grc'},
    'french': {'fre', 'fra'},
}
#: language -> letters (lowercase) the byte may sit on, and how far back the
#: dot-below can really belong (0 = the letter just before the byte).
#: None = byte is NOT a dot below in this context: do not fix.
CONTEXT_RULES = {
    'sanskrit': {'letters': 'r', 'window': 2},
    'arabic': {'letters': 'hsdtz', 'window': 0},
    'greek': None,
    # French "rôle", "Côte" etc.: the byte is a destroyed accented vowel,
    # not a dot below -- the current rule's "ṛle" is a false fix.
    'french': None,
}


class ContextDetector(MARCPolyglotDetector):
    """MARCPolyglotDetector + the profiles 0xA5's ambiguity needs."""

    def __init__(self):
        super().__init__()
        # the base detector's French profile has 4+ letter "trigrams" that
        # can never match a 3-letter window; replace with a usable one
        self.profiles['french'] = {
            'words': {'le', 'la', 'les', 'des', 'du', 'de', 'et', 'un', 'une',
                      'dans', 'pour', 'par', 'sur', 'au', 'aux', 'est', 'qui',
                      'que', 'son', 'ses', 'avec', 'sont', 'cette', 'ce', 'ou',
                      'plus', 'leur', 'entre', 'nous', 'vous', 'il', 'elle'},
            # no generic trigrams (ent/ion/ant...): they fire on English
            'trigrams': {'eau', 'aux', 'ais', 'oir'},
            'weight': 1.2}
        self.profiles['english'] = {
            'words': {'the', 'of', 'and', 'in', 'to', 'by', 'with', 'for', 'on',
                      'from', 'an', 'is', 'its', 'translated', 'edited', 'volume',
                      'history', 'introduction', 'text', 'commentary', 'press'},
            'trigrams': {'the', 'ing', 'and', 'ion', 'tio', 'ent', 'her', 'for',
                         'tha', 'ted', 'ous', 'ver', 'ere', 'ati'},
            'weight': 1.0}
        self.profiles['sanskrit'] = {
            'words': {'sri', 'sutra', 'veda', 'upanisad', 'gita', 'yoga', 'dharma',
                      'bhasya', 'tika', 'sastra', 'purana', 'samhita', 'tantra',
                      'karika', 'vrtti', 'brahmana', 'mahabharata', 'ramayana',
                      'sarasvati', 'vedanta', 'samskrta', 'smrti', 'krsna',
                      'rgveda', 'siddhanta', 'darsana', 'kavya'},
            'trigrams': {'sam', 'skr', 'krt', 'krs', 'rta', 'dha', 'bhy', 'aya',
                         'tya', 'rsi', 'pra', 'ika', 'sya', 'vad', 'rgv', 'mrt',
                         'sta', 'ana', 'hya', 'bha', 'dya', 'ksa', 'jna',
                         'tra', 'dra', 'ndr', 'ata', 'tan', 'sna', 'rga', 'vrt'},
            'weight': 1.5}
        self.profiles['arabic'] = {
            'words': {'al', 'ibn', 'bin', 'abu', 'kitab', 'fi', 'min', 'wa',
                      'risalah', 'tafsir', 'hadith', 'ihya', 'ulum', 'din',
                      'ahmad', 'muhammad', 'sharh', 'mukhtasar', 'bayan'},
            'trigrams': {'muh', 'ham', 'mad', 'ibn', 'ulu', 'iya', 'ayy', 'abd',
                         'rah', 'man', 'shar', 'qur', 'mas', 'sal', 'hma'},
            'weight': 1.4}
        self.profiles['greek'] = {
            'words': {'kai', 'tou', 'ton', 'tes', 'tis', 'ho', 'he', 'ta', 'ek',
                      'eis', 'ta', 'peri', 'theou', 'christou', 'ekklesia'},
            'trigrams': {'tou', 'ton', 'tes', 'kai', 'ois', 'oun', 'ikn', 'kon',
                         'sis', 'tik', 'ika', 'ios', 'eio', 'oik', 'theo'},
            'weight': 1.5}


CONTEXT_CLASSES = ('sanskrit', 'arabic', 'greek', 'french')
PROFILE_TO_CLASS = {c: c for c in CONTEXT_CLASSES}
JUNK_ANY = re.compile(r'\x1b(?!\()[^\x1b]{1,2}|\x1b\(Q?[A-Z0-9]')


def plain_text(data: str) -> str:
    text = JUNK_ANY.sub('', data)
    return re.sub(r'\s+', ' ', re.sub(r'[^\x20-\x7e]', '', text)).strip()


def record_text(parsed) -> str:
    parts = []
    for f in parsed.fields:
        if f.tag in TEXT_TAGS and not f.is_control():
            parts.extend(plain_text(d) for c, d in f.subfields if c != '6')
    return ' '.join(p for p in parts if p)


def classify(det: ContextDetector, text: str, codes: set[str], word: str = ''):
    """-> (class or None, confidence). Class is one of CONTEXT_CLASSES when the
    transliteration profile wins. Evidence: the record's text, the garbled
    word itself (weighted WORD_WEIGHT -- a Sanskrit word in an English record
    is still a Sanskrit word), and the 008/041 prior."""
    full = {name: 0.0 for name in det.profiles}
    word_ev = {name: 0.0 for name in det.profiles}
    for chunk, factor in ((text, 1.0), (word, WORD_WEIGHT)):
        words = det.clean_marc_field(chunk.lower()).split()
        for name, rules in det.profiles.items():
            wm = sum(1 for w in words if w in rules['words'])
            tm = sum(1 for w in words if len(w) >= 3
                     for i in range(len(w) - 2) if w[i:i + 3] in rules['trigrams'])
            score = (wm * 3.0 + tm * 0.8) * rules['weight']
            full[name] += score * factor
            if chunk is word:
                word_ev[name] = score
    for cls, cset in CODE_PRIOR.items():
        if codes & cset:
            full[cls] = full.get(cls, 0.0) + CODE_PRIOR_BONUS
    # English is deliberately NOT a competitor: its score grows with record
    # length, and "English record containing a Sanskrit word" is the normal
    # case here. Only the transliteration classes compete, and the winner
    # needs MIN_EVIDENCE absolute score so noise in an English record can't win.
    scored = {c: full[c] for c in CONTEXT_CLASSES}
    ranked = sorted(scored.values(), reverse=True)
    top = max(scored, key=scored.get)
    if ranked[0] < MIN_EVIDENCE:
        return None, 0.0, 0.0
    # A French/Greek record that contains a Sanskrit or Arabic word is still
    # about that word: if the garbled word itself carries transliteration
    # evidence, let it win over the record's blocking language.
    if CONTEXT_RULES[top] is None:
        for cls in ('sanskrit', 'arabic'):
            if word_ev[cls] >= MIN_WORD_EVIDENCE and word_ev[cls] > word_ev[top]:
                top = cls
                break
    others = sorted((v for c, v in scored.items() if c != top), reverse=True)[0]
    # confidence = top vs runner-up, not share of everything
    conf = scored[top] / (scored[top] + others) if others > 0 else 1.0
    return top, round(conf, 3), word_ev[top]


def current_rule(letters: list[str], idx: int) -> int | None:
    """Letter index the existing rule marks, or None."""
    return idx if letters[idx].lower() == 'r' else None


def proposed(letters: list[str], idx: int, cls: str | None, conf: float,
             min_conf: float, word_ev: float = 0.0):
    """-> (letter index to mark or None, reason)."""
    if cls is None or conf < min_conf:
        cur = current_rule(letters, idx)
        return cur, 'fallback-current-rule'
    rule = CONTEXT_RULES[cls]
    if rule is None:
        return None, f'blocked-{cls}'
    if word_ev < MIN_WORD_EVIDENCE:
        # a record about Islam says nothing about whether a lone "s" is a ṣ
        # or just a mangled apostrophe in English prose -- the word must
        # itself look like this kind of transliteration to be marked
        return current_rule(letters, idx), 'no-word-evidence'
    for back in range(rule['window'] + 1):
        j = idx - back
        if j >= 0 and letters[j] in rule['letters']:
            return j, f'{cls}-back{back}'
    return None, f'{cls}-no-letter'


def render(letters: list[str], mark_at: int | None, byte_after: int) -> str:
    out = []
    for i, c in enumerate(letters):
        out.append(c + (DOT_BELOW if i == mark_at else ''))
        if i == byte_after:
            out.append('‸')  # visible placeholder for the byte
    return unicodedata.normalize('NFC', ''.join(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--min-confidence', type=float, default=0.6)
    ap.add_argument('--sample', default='working/GTU_bibs_0xA5_sample.mrc')
    ap.add_argument('--ladder', action='store_true',
                    help='print the result at several confidence thresholds')
    args = ap.parse_args()
    from pymarc.marc8 import marc8_to_unicode
    m.marc8_to_unicode = marc8_to_unicode
    import lang_lexicon_probe as llp
    llp.m.marc8_to_unicode = marc8_to_unicode

    det = ContextDetector()
    rows = []
    for parsed, _ in m.iter_repair_stream(args.sample):
        if parsed.unresolved:
            continue
        lang008, lang041 = record_langs(parsed)
        codes = {lang008.strip()} | {c for c in lang041.split(',') if c}
        rec_text = record_text(parsed)
        rec_id = m.record_identifier(parsed)
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
                        if byte != BYTE or idx < 0:
                            continue
                        cls, conf, wev = classify(det, rec_text, codes, ''.join(letters))
                        rows.append((rec_id, f'={f.tag} ${code}', lang008, lang041,
                                     cls, conf, letters, idx, wev))
    results = []
    for rec_id, field, l8, l41, cls, conf, letters, idx, wev in rows:
        results.append((rec_id, field, l8, l41, cls, conf, letters, idx, wev))

    def evaluate(min_conf):
        out = []
        for rec_id, field, l8, l41, cls, conf, letters, idx, wev in results:
            cur = current_rule(letters, idx)
            new, why = proposed(letters, idx, cls, conf, min_conf, wev)
            if cur == new:
                kind = 'same-fixed' if cur is not None else 'same-unresolved'
            elif cur is None:
                kind = 'NEW-FIX'
            elif new is None:
                kind = 'BLOCKED'
            else:
                kind = 'MOVED'
            out.append((kind, why, rec_id, field, l8, l41, cls, conf, letters, idx,
                        cur, new))
        return out

    if args.ladder:
        for t in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 1.01):
            res = evaluate(t)
            print(f'min-confidence {t:>4}: ', dict(Counter(r[0] for r in res)))
        print()
    res = evaluate(args.min_confidence)
    print(f'{len(res)} occurrences, min-confidence {args.min_confidence}')
    print('record classes:', dict(Counter(str(r[6]) for r in res)))
    print('outcomes:      ', dict(Counter(r[0] for r in res)), '\n')
    with open('working/a5_context_probe.tsv', 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh, delimiter='\t')
        w.writerow(['outcome', 'reason', 'record', 'field', 'lang008', 'lang041',
                    'class', 'conf', 'current', 'proposed'])
        for r in res:
            kind, why, rec_id, field, l8, l41, cls, conf, letters, idx, cur, new = r
            w.writerow([kind, why, rec_id, field, l8, l41, cls, conf,
                        render(letters, cur, idx), render(letters, new, idx)])
    for kind in ('NEW-FIX', 'BLOCKED', 'MOVED', 'same-unresolved'):
        sel = [r for r in res if r[0] == kind]
        print(f'--- {kind} ({len(sel)}) ---')
        for r in sel:
            kind_, why, rec_id, field, l8, l41, cls, conf, letters, idx, cur, new = r
            print(f'  {rec_id:11} {l8} {str(cls):9} {conf:<5} {why:22} '
                  f'now={render(letters, cur, idx):28} new={render(letters, new, idx)}')


if __name__ == '__main__':
    main()
