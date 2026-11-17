"""List records that are probably ebooks: language material with an 856 $u
(ind2 0/1/blank) plus at least one online/electronic indicator."""
import sys
from pymarc import MARCReader

src, out = sys.argv[1], sys.argv[2]
tot = has856 = ebk = 0
with open(src, 'rb') as f, open(out, 'w', encoding='utf-8') as o:
    o.write('001\t907a\tleader06\tevidence\t856u\ttitle\n')
    for r in MARCReader(f, to_unicode=True, force_utf8=True,
                        permissive=True):
        if r is None:
            continue
        tot += 1
        ld = r.leader
        if ld[6] not in 'at' or ld[7] not in 'am':
            continue
        urls = [u for x in r.get_fields('856') if x.indicator2 in '01 '
                for u in x.get_subfields('u')]
        if not urls:
            continue
        has856 += 1
        ev = []
        f8 = r['008'].data if r['008'] else ''
        if len(f8) > 23 and f8[23] in 'os':
            ev.append('008/23=' + f8[23])
        for s in r.get_fields('006'):
            if s.data[:1] == 'a' and len(s.data) > 6 and s.data[6] in 'os':
                ev.append('006/06')
        for s in r.get_fields('007'):
            if s.data[:2] == 'cr':
                ev.append('007cr')
        if any('c' in x.get_subfields('b') for x in r.get_fields('337')):
            ev.append('337c')
        if any('cr' in x.get_subfields('b') for x in r.get_fields('338')):
            ev.append('338cr')
        if any('electronic resource' in h.lower()
               for x in r.get_fields('245') for h in x.get_subfields('h')):
            ev.append('245h')
        if any('online resource' in a.lower()
               for x in r.get_fields('300') for a in x.get_subfields('a')):
            ev.append('300online')
        if not ev:
            continue
        ebk += 1
        t = (r['245'].value() if r['245'] else '').replace('\t', ' ')
        i = r['001'].data if r['001'] else ''
        a = r['907']['a'] if r['907'] and r['907']['a'] else ''
        o.write(f"{i}\t{a}\t{ld[6]}\t{','.join(sorted(set(ev)))}\t"
                f"{urls[0]}\t{t}\n")
print(tot, has856, ebk)
