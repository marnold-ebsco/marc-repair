"""Measure the "German + 0xB2 -> o-umlaut" default against hand-labelled gold.

Gold = the true spelling of each unique single-gap word, labelled by hand
(from working/b2_context_probe.tsv's word list); words the labeller was not
sure of are left out (UNSURE) and reported, not guessed. No external German
word list exists on this machine, so these labels are the ground truth and
the accuracy below is only as good as them -- treat as an estimate.

Three buckets for a word with one gap:
  CLEAN      filling the gap with the vowel gives the gold word exactly
  DISPLACED  the vowel belongs next to the gap, not in it (e.g. "relig_ise" =
             "religiose" with the o-umlaut one position over): needs a
             different fix than "fill the gap"
  NONGERMAN  the byte is something else (Tamil n-macron, Arabic, etc.)

Run from the repo root:  python tools/context_probe/b2_gold_check.py
"""
from __future__ import annotations

import csv
from collections import Counter

# gap-filling vowel is o-umlaut unless listed in CLEAN_OTHER
CLEAN_O = """
Ausl_ser B_hm B_hmen B_rner B_se B_sen Beh_rden Bisch_fe Bisch_fliche
Bisch_flichen Buchver_ffentlichungen Er_rterung Erstver_ffentlichungen
F_rderung Fr_hlich Fr_mmigkeit G_cke G_ran G_ssner G_tter G_ttingen G_ttinger
G_ttlichem G_tzendienstproblematik Gleichf_rmigkeit Gr_zinger H_hepunkt H_hn
H_lscher Hansj_rg Herausl_sungsprozessen J_rg K_hler K_nemann K_nige K_nigeb
K_nigsherrschaft K_nigsrahmen K_nigsrahmens K_nigszeit K_rper K_rperschaften
K_stenberger Kl_ckner L_ffler L_hr L_sung L_sungsan M_glichkeiten M_nch
Pers_nlichkeit Pers_nlichkeitstheorie R_hrig R_mer S_ding Salb_l Sch_ningh
Sch_pfung Schl_gl Schr_ter Sprichw_rter Str_mungen T_pelmann Tr_ndle V_lker
V_lkern Ver_ffentlichte Ver_ffentlichungen Verk_rperte Vers_hnung
Vers_hnungsbegegnungen Volksfr_mmigkeit _ffentliche _ffentlichen _ffentlicher
_kologische _konomischen _kotheologische _kumenisch _kumenische _kumenischen
_kumenisches _sterreichische _stlicher b_hmische b_hmischen bef_rdern
bisch_flichen einl_sen er_ffnen er_ffnet er_rtern er_rtert er_rtertes
ergew_hnlichen ergew_hnlicher erl_senden erm_glichte franz_sische g_ttliche
geh_ren geh_rt h_chsten h_rt k_nne k_nnen k_nnte m_ge m_glich n_rdlichen
pers_nlich pers_nliche pers_nlichen r_misch r_mische r_mischen
sch_pferischen unaufh_rlich unersch_pflichen v_llig verk_rperter
zeitgen_ssische zeitgen_ssischen zeitgen_ssischer zw_lf
""".split()
CLEAN_U = "B_hler H_bsch".split()  # Buhler, Hubsch: u-umlaut
DISPLACED = """
Erlsu_ng G_otingen Gt_tingen Gt_zendiener Interrelig_iser Kn_ige Relig_ise
Relig_isen Religise_n Rme_r Rme_rbrief Zur_ich _oumenischen ermg_licht
g_otliches gt_tliche interrelig_ise interrelig_isen interrelig_iser
interrelig_ises interreligis_e relig_is relig_ise relig_isen relig_iser
relig_ises religis_en religis_er
""".split()
NONGERMAN = """
Hait_ Nantan_ Ot_m aiq_ pad_a Kut_ar Il_avar it_b massek_ JakubowskiT_iessen
JohannesVe_rl Promi_um Untersuchung_ D_ c_ d_ ih_ st_ gr_ Abot_ Aksh_aya
Algaddup_ama Atr_aitadaiva Att_ar Att_art Attan_ Ayyappan_ Az_argushasb
Ch_ing Civan_ D_elopoulos D_imm D_ora Daswen_ Emenyon_u Ent_e Er_p
Il_am Kallar_annatt Kul_in Mar_aimalaiyatikal Mas_navi Masn_avi Melok_
Muk_h Murukan_ Nisg_a R_m Sa_n Semahot_ Semanhot_ Taz_kira Taz_kirah
Taz_kirat Theravd_a Tirrukkur_al Tirukkur_al Tiruvorriy_ Usm_ Val_i
ab_ ah_ ahat_ ak_a al_v aman_ aman_i amacantiran_ an_ an_anta an_d
anighan_tu ar_ ariyamman_ arn_ava arthasan_graha as_ ast_yan at_ at_um
atan_ attan_ avar_a ayahguz_ aymol_i camayattin_ d_imm ehattint_e eb_e
ekkil_ et_ h_al h_ed hit_h hrvt_ ib_ in_ iran_ it_ kol_kaiyam l_ ler_abenu
mah_ebar nan_acittiy nan_ap nt_r ok_o on_ sastr_avum tar_k tirumol_i
tjut_a ud_ uk_ upan_a ut_ vin_ with_
""".split()
UNSURE = """
C_lius Fr_chtling Gr_ Gr_ger H_hm K_rnigen K_rver L_ssl L_we M_llers
Meckenl_r Meckenl_rs R_bel R_hser R_sch R_sel R_ser Referenzgr_ Z_hrer
_Skizzen m_chte Gr_ Kn_ ah_ _ _Al _in _reflected _v
""".split()


def main() -> None:
    rows = list(csv.DictReader(open('working/b2_context_probe.tsv',
                                    encoding='utf-8'), delimiter='\t'))
    one = [r for r in rows if r['gaps'] == '1']
    bucket = {}
    for w in CLEAN_O:
        bucket[w] = 'CLEAN_O'
    for w in CLEAN_U:
        bucket[w] = 'CLEAN_U'
    for w in DISPLACED:
        bucket[w] = 'DISPLACED'
    for w in NONGERMAN:
        bucket.setdefault(w, 'NONGERMAN')
    for w in UNSURE:
        bucket.setdefault(w, 'UNSURE')
    seen = Counter()
    for r in one:
        seen[bucket.get(r['word'], 'UNLABELLED')] += 1
    print(f'{len(one)} single-gap occurrences (of {len(rows)} gapped words)')
    print('by bucket (occurrences):', dict(seen))

    german = seen['CLEAN_O'] + seen['CLEAN_U'] + seen['DISPLACED']
    clean = seen['CLEAN_O'] + seen['CLEAN_U']
    print(f'\nlabelled German: {german}')
    print(f'  fill-the-gap with o-umlaut correct : {seen["CLEAN_O"]}/{clean} '
          f'clean words ({seen["CLEAN_O"] / clean:.1%})')
    print(f'  fill-the-gap with u-umlaut correct : {seen["CLEAN_U"]}/{clean}')
    print(f'  vowel displaced from the gap       : {seen["DISPLACED"]}/{german} '
          f'({seen["DISPLACED"] / german:.1%}) -- a different fix is needed')
    print(f'  overall, "German -> o-umlaut in the gap" is right for '
          f'{seen["CLEAN_O"]}/{german} = {seen["CLEAN_O"] / german:.1%} of German words')

    print('\ngating: which single-gap words would an 008=ger-only gate touch?')
    by_gate = Counter()
    for r in one:
        b = bucket.get(r['word'], 'UNLABELLED')
        if r['lang008'] == 'ger':
            by_gate['008=ger, ' + b] += 1
    for k, v in sorted(by_gate.items()):
        print(f'  {k:32} {v}')
    ger_false = sum(v for k, v in by_gate.items() if k.endswith('NONGERMAN'))
    ger_all = sum(by_gate.values())
    print(f'  -> non-German words inside 008=ger records: {ger_false}/{ger_all} '
          f'({ger_false / ger_all:.1%}) would be wrongly given an o-umlaut')

    print('\nGerman words in NON-ger records (a 008 gate would miss these):')
    miss = Counter()
    for r in one:
        b = bucket.get(r['word'], 'UNLABELLED')
        if r['lang008'] != 'ger' and b in ('CLEAN_O', 'CLEAN_U', 'DISPLACED'):
            miss[r['word']] += 1
    print(f'  {sum(miss.values())} occurrences, e.g. {dict(miss.most_common(8))}')

    unl = Counter(r['word'] for r in one if bucket.get(r['word']) is None)
    print(f'\nunlabelled (not in any list): {sum(unl.values())} occurrences, '
          f'{dict(unl.most_common(12))}')

    acc = [r for r in one if r['tier'] == 'accept']
    wrong = [r for r in acc if bucket.get(r['word']) in ('NONGERMAN', 'DISPLACED')
             or (bucket.get(r['word']) == 'CLEAN_O' and r['pick'] != 'o')
             or (bucket.get(r['word']) == 'CLEAN_U' and r['pick'] != 'u')]
    print(f'\nlexicon-accepted single-gap words: {len(acc)}; contradicted by gold: '
          f'{len(wrong)} {[(r["word"], r["resolved_word"]) for r in wrong]}')


if __name__ == '__main__':
    main()
