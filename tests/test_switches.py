"""The on/off switch registry (marc_repair_lib/switches.py): one toggle per check, plus groups."""

import glob
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import marc_repair as m  # noqa: E402
from marc_repair_lib import switches as sw  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
VERBS = ("fix", "remove", "add", "merge", "move", "recode", "normalize", "split", "check",
         "collapse", "truncate", "trim", "reattach", "transcode", "marc8", "allow", "remap")
HEADER = re.compile(
    r"=== [^\n]*: (\w+)(?: \([^\n]*\))? ===\n(?:=== [^\n]*\n)+?=== (\d+) record\(s\) ===", re.MULTILINE
)


@pytest.fixture(scope="module")
def parser():
    return m.build_parser()


def _actions(parser):
    return {a.dest: a for a in parser._actions if a.option_strings}


def test_registry_is_consistent(parser):
    actions = _actions(parser)
    dests = [s.dest for s in sw.SWITCHES]
    assert len(dests) == len(set(dests))
    strings = [o for s in sw.SWITCHES for o in sw.option_strings(s)]
    assert len(strings) == len(set(strings))
    for s in sw.SWITCHES:
        assert isinstance(actions[s.dest], sw.ToggleAction), s.dest
        assert set(actions[s.dest].option_strings) == set(sw.option_strings(s))
        assert s.scope in ("bib", "holdings", "both")
        assert s.name.split("-")[0] in VERBS, s.name
    for members in sw.GROUPS.values():
        assert all(d in sw.BY_DEST for d in members)


def test_defaults_match_registry(parser):
    args = parser.parse_args([])
    assert sw.switch_values(args) == {s.dest: s.default for s in sw.SWITCHES}


@pytest.mark.parametrize("s", sw.SWITCHES, ids=lambda s: s.dest)
def test_both_spellings_set_the_one_dest(parser, s):
    assert getattr(parser.parse_args([f"--{s.name}"]), s.dest) is True
    assert getattr(parser.parse_args([f"--no-{s.name}"]), s.dest) is False


def test_legacy_spellings_still_work(parser):
    for old, dest in (
        ("--no-strip-empty-fields", "strip_empty_fields"),
        ("--no-strip-missing-required-a", "strip_missing_required_a"),
        ("--no-strip-duplicate-non-repeatable-fields", "strip_duplicate_non_repeatable_fields"),
        ("--no-fix-missing-852c", "fix_missing_852c"),
        ("--no-fix-mojibake", "fix_mojibake"),
        ("--no-trim-oversized-record", "trim_oversized_record"),
    ):
        assert getattr(parser.parse_args([old]), dest) is False


def test_a_cli_switch_can_undo_a_profile(parser):
    parser.set_defaults(fix_mojibake=False)
    try:
        assert parser.parse_args([]).fix_mojibake is False
        assert parser.parse_args(["--fix-mojibake"]).fix_mojibake is True
    finally:
        parser.set_defaults(fix_mojibake=True)


def test_group_sets_every_member(parser):
    off = parser.parse_args(["--no-group-marc8-encoding"])
    assert all(getattr(off, d) is False for d in sw.GROUPS["marc8-encoding"])
    on = parser.parse_args(["--group-detect-only"])
    assert on.check_isbn_issn_checksum is True and on.check_dangling_880_links is True


@pytest.mark.parametrize("argv", [
    ["--no-group-marc8-encoding", "--fix-mojibake"],
    ["--fix-mojibake", "--no-group-marc8-encoding"],
])
def test_individual_switch_beats_group_in_either_order(parser, argv):
    args = parser.parse_args(argv)
    assert args.fix_mojibake is True
    assert args.transcode_marc8 is False and args.fix_marc8_diacritic_escapes is False


def test_group_beats_profile_default(parser):
    parser.set_defaults(transcode_marc8=False)
    try:
        assert parser.parse_args(["--group-marc8-encoding"]).transcode_marc8 is True
    finally:
        parser.set_defaults(transcode_marc8=True)


def test_groups_are_not_profile_options(parser):
    import marc_repair_lib.cli as cli
    assert not set(cli.profile_option_actions(parser)) & sw.GROUP_DESTS


def _counts(log_path):
    logs = glob.glob(os.path.splitext(str(log_path))[0] + "*.log")
    text = open(logs[0], encoding="utf-8").read()
    return {cat: int(n) for cat, n in HEADER.findall(text)}


def _run(tmp_path, fixture, extra, holdings):
    out = tmp_path / "out.mrc"
    log = tmp_path / "out.log"
    argv = [os.path.join(FIXTURES, fixture), "-o", str(out), "--log", str(log), "--log-zero"]
    argv += ["--repair-holdings"] if holdings else []
    m.main(argv + ["--no-b2-lexicon", "--no-profile"] + extra)  # exit 1 just means some record was unfixable
    return _counts(log)


def _flip(s):
    return f"--no-{s.name}" if s.default else f"--{s.name}"


def _sole_categories(s, pipeline):
    """Categories no other in-scope switch also produces."""
    others = {c for o in sw.SWITCHES if o is not s and o.scope in ("both", pipeline)
              for c in o.categories}
    return [c for c in s.categories if c not in others]


@pytest.fixture(scope="module")
def baselines(tmp_path_factory):
    return {
        "bib": _run(tmp_path_factory.mktemp("b"), "kitchen_sink_bib.mrc", [], False),
        "holdings": _run(tmp_path_factory.mktemp("h"), "kitchen_sink_holdings.mrc", [], True),
    }


def test_baseline_exercises_many_checks(baselines):
    assert sum(1 for n in baselines["bib"].values() if n) >= 20
    assert sum(1 for n in baselines["holdings"].values() if n) >= 10


CASES = [
    (s, pipeline) for s in sw.SWITCHES for pipeline in ("bib", "holdings")
    if s.scope in ("both", pipeline)
]


@pytest.mark.parametrize("s,pipeline", CASES, ids=lambda v: v.dest if hasattr(v, "dest") else v)
def test_turning_a_switch_off_stops_its_check(tmp_path, baselines, s, pipeline):
    if not s.default:
        pytest.skip("off by default; covered by the on-direction test below")
    fixture = "kitchen_sink_bib.mrc" if pipeline == "bib" else "kitchen_sink_holdings.mrc"
    counts = _run(tmp_path, fixture, [_flip(s)], pipeline == "holdings")
    for cat in _sole_categories(s, pipeline):
        assert counts.get(cat, 0) == 0, f"{cat} still logged with {_flip(s)}"
        if baselines[pipeline].get(cat, 0):
            assert cat not in counts, f"{cat} header still present with {_flip(s)}"


@pytest.mark.parametrize("s", [s for s in sw.SWITCHES if not s.default], ids=lambda s: s.dest)
def test_turning_an_opt_in_switch_on_runs(tmp_path, s):
    assert _run(tmp_path, "kitchen_sink_bib.mrc", [_flip(s)], False)


def test_group_off_removes_all_member_findings(tmp_path, baselines):
    counts = _run(tmp_path, "kitchen_sink_holdings.mrc", ["--no-group-holdings-852"], True)
    for dest in sw.GROUPS["holdings-852"]:
        for cat in _sole_categories(sw.BY_DEST[dest], "holdings"):
            assert counts.get(cat, 0) == 0


# --- switches the kitchen-sink fixtures don't exercise ---------------------------------
_LEADER = "00000cam a2200000 a 4500"


def _f(tag, ind, *subs):
    return m.Field_(tag, ind, list(subs))


def _synthetic_bib(tmp_path):
    """One record per defect, so a switch that is off changes the record-level count by one."""
    base = [
        m.Field_("001", None, None, content="x{n}"),
        m.Field_("008", None, None, content="200101s2020    xxu           000 0 eng d"),
        _f("245", "10", ("a", "A title")),
    ]
    defects = [
        [_f("040", "  ", ("a", "DLC")), _f("040", "  ", ("d", "XYZ"))],
        [_f("100", "1 ", ("a", "Doe, Jane")), _f("100", "1 ", ("a", "Roe, Rick"))],
        [_f("240", "10", ("a", "Uniform")), _f("240", "10", ("a", "Other uniform"))],
        [_f("245", "10", ("a", "A variant title"))],
        [_f("245", "10", ("a", "Different"), ("c", "by someone else"))],
        [_f("110", "2 ", ("a", "Corp one")), _f("110", "2 ", ("a", "Corp two"))],
        [_f("700", "1 ", ("t", "An analytical title"))],
        [_f("650", " 0", ("b", "Philosophy"), ("v", "Early works"))],
        [_f("880", "  ", ("a", "Orphan vernacular"))],
        [_f("856", "40", ("a", "http://example.org/x"))],
        [_f("020", "  ", ("a", "9780306406158"))],
        [_f("880", "1 ", ("6", "100-01"), ("a", "Name"))],
        [_f("500", "  ", ("a", "cafÃ© society"))],
    ]
    recs = []
    for n, extra in enumerate(defects):
        fields = [
            m.Field_(f.tag, f.indicators, f.subfields, content=f.content.replace("{n}", str(n)))
            if f.is_control() else f
            for f in base
        ]
        recs.append(m.assemble_marc(
            m.ParsedRecord(leader=_LEADER, entries=[], fields=fields + extra)
        ))
    path = tmp_path / "synthetic_bib.mrc"
    path.write_bytes(b"".join(recs))
    return str(path)


SYNTHETIC = {  # switch dest -> category it should drive
    "merge_duplicate_040": "merged_duplicate_040",
    "move_duplicate_100_to_700": "moved_duplicate_to_added_entry",
    "move_duplicate_240_to_730": "moved_duplicate_to_added_entry",
    "move_duplicate_245_to_246": "moved_duplicate_to_added_entry",
    "recode_misfiled_duplicate_245": "moved_duplicate_to_added_entry",
    "strip_duplicate_non_repeatable_fields": "removed_non_repeatable_duplicate",
    "recode_title_only_7xx": "recoded_title_only_7xx_to_730",
    "recode_6xx_b_to_a": "recoded_6xx_b_to_a",
    "remove_880_missing_6": "removed_880_missing_6",
    "fix_856_missing_u": "bib_856_missing_u",
    "check_isbn_issn_checksum": "invalid_isbn_issn_checksum",
    "check_dangling_880_links": "dangling_880_link",
    "fix_mojibake": "fixed_mojibake",
}


def _run_synthetic(tmp_path, extra):
    out = tmp_path / "o.mrc"
    log = tmp_path / "o.log"
    argv = [_synthetic_bib(tmp_path), "-o", str(out), "--log", str(log), "--log-zero",
            "--no-b2-lexicon", "--no-profile", "--check-isbn-issn-checksum",
            "--check-dangling-880-links"]
    m.main(argv + extra)
    return _counts(log)


@pytest.mark.parametrize("dest,cat", SYNTHETIC.items())
def test_synthetic_defect_is_found_then_switched_off(tmp_path_factory, dest, cat):
    on = _run_synthetic(tmp_path_factory.mktemp("on"), [])
    assert on.get(cat, 0) >= 1, f"synthetic record never triggered {cat}"
    off = _run_synthetic(tmp_path_factory.mktemp("off"), [f"--no-{sw.BY_DEST[dest].name}"])
    assert off.get(cat, 0) < on[cat], f"--no-{sw.BY_DEST[dest].name} did not stop {cat}"


# --- the run header records which switches were non-default ---------------------------------
def _header(tmp_path, extra, fixture="kitchen_sink_bib.mrc", holdings=False):
    out = tmp_path / "out.mrc"
    log = tmp_path / "out.log"
    argv = [os.path.join(FIXTURES, fixture), "-o", str(out), "--log", str(log), "--no-b2-lexicon",
            "--no-profile"] + (["--repair-holdings"] if holdings else []) + extra
    m.main(argv)
    return open(glob.glob(str(tmp_path / "out*.log"))[0], encoding="utf-8").read().split("\n\n")[0]


def test_header_says_all_defaults(tmp_path):
    assert "Switches: all defaults" in _header(tmp_path, [])


def test_header_lists_group_and_individual_switches(tmp_path):
    head = _header(tmp_path, ["--no-group-detect-only", "--fix-mojibake", "--no-fix-bad-indicators"])
    assert "Switches: --no-group-detect-only, --no-fix-bad-indicators" in head
    assert "Checks switched off (no section in this log):" in head
    assert "suspect_marc8_escape" in head


def test_holdings_header_lists_switches(tmp_path):
    head = _header(tmp_path, ["--no-group-holdings-852"], "kitchen_sink_holdings.mrc", True)
    assert "Switches: --no-group-holdings-852" in head
