"""--source-system sirsi: every record gets a 002 copied from its 001."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marc_repair as m  # noqa: E402
from marc_repair_lib.patching import Sirsi002Tally, sirsi_002_value  # noqa: E402

LEADER = "00000cam a2200000 a 4500"


def _rec(id_001, extra=()):
    fields = [m.Field_("001", None, None, content=id_001),
              m.Field_("245", "00", [("a", "Title")]), *extra]
    return m.ParsedRecord(leader=LEADER, entries=[], fields=fields)


def _002s(rec):
    return [f.content for f in rec.fields if f.tag == "002"]


def test_value_strips_only_letter_plus_eight_digits():
    assert sirsi_002_value("o12345678") == "12345678"
    assert sirsi_002_value("B12345678") == "12345678"
    for keep in ("a445304", "o1234567", "o123456789", "x12345678", "12345678", "b12345678x"):
        assert sirsi_002_value(keep) == keep


def test_apply_adds_002_after_001_and_replaces_existing():
    tally = Sirsi002Tally()
    plain = _rec("a445304")
    assert tally.apply(plain) == []
    assert _002s(plain) == ["a445304"]
    assert [f.tag for f in plain.fields][:2] == ["001", "002"]

    old_same = _rec("a1", [m.Field_("002", None, None, content="a1")])
    assert tally.apply(old_same) == [] and _002s(old_same) == ["a1"]

    old_diff = _rec("o12345678", [m.Field_("002", None, None, content="zzz")])
    lost = tally.apply(old_diff)
    assert len(lost) == 1 and "'zzz'" in lost[0]
    assert _002s(old_diff) == ["12345678"]
    assert tally.records == 3
    assert tally.changed == ["o12345678"]


def test_summary_lists_ids_only_under_25():
    t = Sirsi002Tally()
    for n in range(24):
        t.apply(_rec(f"o{10000000 + n}"))
    count, body = t.summary()
    assert count == "24 record(s)" and "o10000000, " in body
    t.apply(_rec("o99999999"))
    assert "o10000000" not in t.summary()[1] and "25 or more" in t.summary()[1]


def test_cli_logs_count_ids_and_old_002(tmp_path):
    src = tmp_path / "in.mrc"
    src.write_bytes(
        m.assemble_marc(_rec("o12345678"))
        + m.assemble_marc(_rec("a1", [m.Field_("002", None, None, content="old")]))
        + m.assemble_marc(_rec("plain1"))
    )
    out = tmp_path / "out.mrc"
    rc = m.main([str(src), "-o", str(out), "--source-system", "sirsi",
                 "--log", str(tmp_path / "run.log"), "--no-profile",
                 "--check-non-standard-fields"])
    assert rc == 0
    log = next(tmp_path.glob("run_*.log")).read_text()
    assert "=== INFORMATIONAL: added_002_from_001 ===" in log
    assert "For Data Engineers:" in log and "=== 3 record(s) ===" in log
    assert "o12345678, a1" in log
    assert "FIXED/REQUIRES ATTENTION: removed_existing_002" in log
    data = out.read_bytes()
    assert b"12345678" in data and b"old" not in data


def test_switch_off_and_other_systems_do_nothing(tmp_path):
    src = tmp_path / "in.mrc"
    src.write_bytes(m.assemble_marc(_rec("o12345678")))
    for extra in (["--source-system", "sirsi", "--no-add-002-from-001"],
                  ["--source-system", "other"]):
        out = tmp_path / "o.mrc"
        m.main([str(src), "-o", str(out), "--log", str(tmp_path / "x.log"),
                "--no-profile", *extra])
        assert b"\x1e12345678\x1e" not in out.read_bytes()
        assert "INFORMATIONAL: added_002_from_001" not in "".join(
            p.read_text() for p in tmp_path.glob("x_*.log"))
        for p in tmp_path.glob("x_*.log"):
            p.unlink()
