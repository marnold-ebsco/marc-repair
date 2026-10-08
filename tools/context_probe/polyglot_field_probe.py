"""Does the polyglot detector's language call on a suspect field predict
which mark an overloaded bare MARC-8 byte should become?

For every occurrence of each target byte (junk escape run + byte, the same
shape marc_repair.py fixes) in the per-byte sample files, run the field's
text (junk/escapes/non-ASCII bytes stripped) through MARCPolyglotDetector and
tabulate: byte x detected language x (rule fixes it / leftover) x before-letter.
Rows where the current rule already fixes the byte are the believed-correct
calibration set -- if language predicts their mark, it's a usable signal.
"""
from __future__ import annotations

import argparse
import csv
import re
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..'))
sys.path.insert(0, HERE)
import marc_repair as m  # noqa: E402
from polyglot_detector_marc import MARCPolyglotDetector  # noqa: E402

BYTES = ['a4', 'a5', 'a6', 'a8', 'ae', 'b2', 'b3']
JUNK_ANY = re.compile(r'\x1b(?!\()[^\x1b]{1,2}|\x1b\(Q?[A-Z0-9]')
MARK = {'\xf2': 'dot-below', '\xf0': 'cedilla', '\xf1': 'ogonek', '\xe8': 'umlaut'}


def plain_text(data: str) -> str:
    text = JUNK_ANY.sub('', data)
    text = re.sub(r'[^\x20-\x7e]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def rule_mark(byte: str, before: str) -> str:
    override = m._MARC8_BARE_COMBINING_BEFORE_OVERRIDES.get(byte, {}).get(before.lower())
    mark = override or m._MARC8_BARE_COMBINING_BYTES.get(byte)
    allowed = m._MARC8_BARE_COMBINING_RESTRICTED_BEFORE.get(byte)
    if override is None and (mark is None or (allowed and before.lower() not in allowed)):
        return ''
    return MARK.get(mark, '?')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--bytes', nargs='*', default=BYTES)
    args = ap.parse_args()
    det = MARCPolyglotDetector()
    rows = []
    for hx in args.bytes:
        byte = chr(int(hx, 16))
        pat = re.compile(r'([A-Za-z])(?:\x1b(?!\()[^\x1b' + re.escape(byte)
                         + r']{1,2})+' + re.escape(byte))
        sample = f'working/GTU_bibs_0x{hx.upper()}_sample.mrc'
        for parsed, _ in m.iter_repair_stream(sample):
            if parsed.unresolved:
                continue
            for f in parsed.fields:
                if f.is_control():
                    continue
                for code, data in f.subfields:
                    if not data or '\x1b' not in data:
                        continue
                    text = m._MARC8_DIACRITIC_ESCAPE_RE.sub(
                        m._marc8_diacritic_replacement, data)
                    for mm in pat.finditer(text):
                        res = det.detect(plain_text(text))
                        rows.append({
                            'byte': hx, 'record': m.record_identifier(parsed),
                            'field': f'={f.tag} ${code}', 'before': mm.group(1),
                            'rule_mark': rule_mark(byte, mm.group(1)),
                            'language': res['language'],
                            'confidence': res['confidence'],
                            'text': plain_text(text)[:80],
                        })
    with open('working/polyglot_field_probe.tsv', 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter='\t')
        w.writeheader()
        w.writerows(rows)
    for hx in args.bytes:
        sub = [r for r in rows if r['byte'] == hx]
        print(f'\n=== 0x{hx.upper()}: {len(sub)} occurrences ===')
        by = defaultdict(Counter)
        for r in sub:
            by[r['language']][r['rule_mark'] or 'LEFTOVER'] += 1
        for lang, c in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
            print(f'  {lang:28} {dict(c)}')


if __name__ == '__main__':
    main()
