"""PyTest suite for marc_repair.py.

Fixtures under tests/fixtures/ are real (anonymized institutional) MARC
extracts exhibiting the three defect classes this tool targets:

  * six_records_corrupted.txt          -- Mode 2: delimiters fully stripped
    (as happens when a record is pasted through a chat box)
  * bad_length_bib_*.mrc                -- Mode 1: intact delimiters, but a
    mangled leader/directory terminator inflates the declared length
  * bad_missing245_bib_*.mrc            -- structurally sound, but some
    records have no 245 field at all
  * bad_bib_mandatoryfields*.mrc        -- structurally sound, but some
    fields in the "required $a" tag list are missing/empty $a
"""

import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marc_repair as m  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def _resolve_log(log_path):
    """marc_repair.py always inserts a run timestamp before a log path's
    extension, even one given explicitly via --log, so a test can't just
    read back the literal path it passed in. Resolve it to whatever file
    actually got written (each test's tmp_path is unique, so at most one
    match exists); falls back to `log_path` itself when nothing matches,
    so `.exists()` still correctly reports False."""
    base, ext = os.path.splitext(str(log_path))
    matches = glob.glob(f"{base}_*{ext}")
    if not matches:
        return log_path
    return type(log_path)(matches[0])


def _read(name: str) -> str:
    return m._read_text(_fixture(name))


def _make_stripped_record(leader: str, fields: list) -> str:
    """Build a real delimited record from `fields` (via assemble_marc, so
    the leader/directory are guaranteed self-consistent) and then strip its
    delimiters -- exactly what Mode 2 has to reconstruct from. Used for
    small, fast, deterministic test fixtures instead of the large real-world
    records, whose free-text fields make automatic Mode 2 resolution
    genuinely underdetermined (see TestModeStrippedDelimiters docstring)."""
    parsed = m.ParsedRecord(leader=leader, entries=[], fields=fields)
    raw = m.assemble_marc(parsed).decode("utf-8")
    directory_end = raw.index(m.FIELDTERM, 24)
    directory = raw[24:directory_end]
    field_data = raw[directory_end + 1:]
    stripped_field_data = (
        field_data.replace(m.SUBFIELD, "").replace(m.FIELDTERM, "").replace(m.RECTERM, "")
    )
    return raw[:24] + directory + stripped_field_data


_SYNTHETIC_LEADER = "00000nam a2200000ka 4500"
# All leader bytes checked by find_suspicious_fields' leader-validity checks
# (05/06/08/17) are valid here, unlike _SYNTHETIC_LEADER's 'k' at byte 17 --
# for tests that assert an exact/empty find_suspicious_fields() result and
# aren't themselves testing those checks.
_VALID_LEADER = "00000cam a2200000 a 4500"


_HOLDINGS_LEADER = _SYNTHETIC_LEADER[:6] + "x" + _SYNTHETIC_LEADER[7:]


class TestCheckHoldingsRecord:
    def _holdings_text(self, fields) -> str:
        parsed = m.ParsedRecord(leader=_HOLDINGS_LEADER, entries=[], fields=fields)
        return m.assemble_marc(parsed).decode("utf-8")

    def test_clean_record_has_no_issues(self):
        text = self._holdings_text([
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ])
        issues, record_id = m.check_holdings_record(text, "utf-8")
        assert issues == []

    def test_missing_008_is_flagged(self):
        text = self._holdings_text([m.Field_("852", "  ", [("a", "Main Library")])])
        issues, _ = m.check_holdings_record(text, "utf-8")
        assert any(cat == "holdings_missing_008" for cat, _ in issues)

    def test_invalid_subfield_code_is_flagged(self):
        text = self._holdings_text([
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("A", "Main Library")]),
        ])
        issues, _ = m.check_holdings_record(text, "utf-8")
        assert any(cat == "holdings_invalid_subfield_code" for cat, _ in issues)

    def test_null_identifier_is_flagged(self):
        text = self._holdings_text([
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "")]),
        ])
        issues, _ = m.check_holdings_record(text, "utf-8")
        assert any(cat == "holdings_null_identifier" for cat, _ in issues)

    def test_record_id_comes_from_001(self):
        text = self._holdings_text([
            m.Field_("001", None, None, content="on123"),
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ])
        _, record_id = m.check_holdings_record(text, "utf-8")
        assert record_id == "on123"

    def test_nothing_is_modified(self):
        text = self._holdings_text([m.Field_("852", "  ", [("a", "Main Library")])])
        before = text
        m.check_holdings_record(text, "utf-8")
        assert text == before

    def test_missing_004_is_flagged(self):
        text = self._holdings_text([
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ])
        issues, _ = m.check_holdings_record(text, "utf-8")
        assert any(cat == "holdings_missing_004" for cat, _ in issues)

    def test_single_004_is_not_flagged(self):
        text = self._holdings_text([
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ])
        issues, _ = m.check_holdings_record(text, "utf-8")
        cats_004 = ("holdings_missing_004", "holdings_multiple_004")
        assert not any(cat in cats_004 for cat, _ in issues)

    def test_multiple_004_is_flagged(self):
        text = self._holdings_text([
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("004", None, None, content="ocm456"),
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ])
        issues, _ = m.check_holdings_record(text, "utf-8")
        assert any(cat == "holdings_multiple_004" for cat, _ in issues)


class TestRepairHoldingsRecords:
    def _write_holdings_file(self, tmp_path, records: list[bytes], name="holdings.mrc"):
        path = tmp_path / name
        path.write_bytes(b"".join(records))
        return path

    def _holdings_record(self, leader=_HOLDINGS_LEADER, fields=None) -> bytes:
        if fields is None:
            fields = [
                m.Field_("004", None, None, content="ocm123"),
                m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
                m.Field_("852", "  ", [("a", "Main Library")]),
            ]
        return m.assemble_marc(m.ParsedRecord(leader=leader, entries=[], fields=fields))

    def _run(self, tmp_path, records: list[bytes]):
        src = self._write_holdings_file(tmp_path, records)
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        result = m.repair_holdings_records(str(src), str(out), str(log))
        return result, out, log

    def test_clean_record_passes_through_with_no_log(self, tmp_path):
        # byte 17 (encoding level) of _HOLDINGS_LEADER is 'k', which is
        # NOT a valid MARC21 encoding-level code -- fine for most tests
        # here (that fix is exercised elsewhere), but this test wants a
        # record with genuinely nothing to fix, so its leader corrects
        # that one byte to a value that's actually valid for HOLDINGS
        # specifically ('u', Unknown) -- unlike the bib/authority
        # leader, holdings' own spec has no defined blank code at all,
        # see LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS.
        clean_leader = _HOLDINGS_LEADER[:17] + "u" + _HOLDINGS_LEADER[18:]
        result, out, log = self._run(tmp_path, [self._holdings_record(leader=clean_leader)])
        assert result == {"total": 1, "unresolved": 0, "log_lines": 0, "not_fixed": 0}
        assert m.count_records(str(out)) == 1
        assert not _resolve_log(log).exists()

    def test_missing_008_gets_blank_holdings_placeholder(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        # +1 for byte 17 (encoding level) always being defaulted for this
        # fixture's leader -- see the comment in
        # test_clean_record_passes_through_with_no_log
        assert result["log_lines"] == 2
        assert result["not_fixed"] == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "added_default_holdings_008" in content
        raw = out.read_bytes()
        parsed = m.read_intact_record(raw.decode("utf-8"))
        field008 = next(f for f in parsed.fields if f.tag == "008")
        assert field008.content == " " * m.HOLDINGS_008_LENGTH

    def test_wrong_length_008_padded_to_32_not_40(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "fixed_holdings_008_length" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field008 = next(f for f in parsed.fields if f.tag == "008")
        assert len(field008.content) == m.HOLDINGS_008_LENGTH

    def test_null_identifier_flagged_not_fixed(self, tmp_path):
        # $b is the null identifier under test; $a is real, non-empty
        # data so the field survives strip_empty_fields (a field with
        # ONLY an empty subfield is dropped entirely and silently --
        # see strip_empty_fields -- so isolating the null-identifier
        # case needs at least one other non-empty subfield alongside it)
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("b", "")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["not_fixed"] == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_null_identifier" in content
        assert "[NOT FIXED]" in content

    def test_escape_sequence_flagged_not_transcoded(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library\x1b(Bfoo")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["not_fixed"] >= 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_escape_sequence" in content
        # the ESC byte itself must still be present in the output -- not
        # transcoded away, per this pipeline's explicit, temporary scope
        assert b"\x1b" in out.read_bytes()

    def test_invalid_leader_byte_06_defaults_to_unknown_not_bib(self, tmp_path):
        bad_leader = _HOLDINGS_LEADER[:6] + "!" + _HOLDINGS_LEADER[7:]
        result, out, log = self._run(tmp_path, [self._holdings_record(leader=bad_leader)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_leader_byte_defaulted" in content
        assert "[FIXED/REQUIRES ATTENTION]" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert parsed.leader[6] == "u"

    def test_blank_byte_17_is_invalid_for_holdings_and_defaulted_to_u(self, tmp_path):
        # Blank ("full level") is a valid bib/authority encoding-level
        # code but NOT a defined holdings one (see
        # LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS) -- this confirms the
        # holdings pipeline actually enforces holdings' own code set
        # rather than the permissive bib/authority/holdings union.
        blank_byte17_leader = _HOLDINGS_LEADER[:17] + " " + _HOLDINGS_LEADER[18:]
        result, out, log = self._run(
            tmp_path, [self._holdings_record(leader=blank_byte17_leader)]
        )
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_leader_byte_defaulted" in content
        assert "[FIXED/REQUIRES ATTENTION]" in content
        assert "encoding level" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert parsed.leader[17] == "u"

    def test_unresolvable_record_passed_through_unchanged(self, tmp_path):
        # A whole file with literally no MARC leader anywhere is a fatal
        # RepairError for iter_repair_stream (nowhere to even start) --
        # per-record UNRESOLVED handling instead kicks in for a *trailing*
        # chunk after at least one real leader was found, which is what
        # this exercises: one clean record, then trailing garbage with
        # no leader of its own.
        garbage = b"not a marc record at all, no leader here whatsoever" + b"\x1d"
        result, out, log = self._run(tmp_path, [self._holdings_record(), garbage])
        assert result["total"] == 2
        assert result["unresolved"] == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "unresolved_record" in content

    def test_invalid_tag_renamed_to_unused_9xx(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_HOLDINGS_LEADER, entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
                m.Field_("85Z", "  ", [("a", "bad tag")]),
            ],
        )
        raw = m.assemble_marc(parsed)
        result, out, log = self._run(tmp_path, [raw])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "invalid_tag" in content
        parsed_out = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert all(f.tag.isdigit() for f in parsed_out.fields)

    def test_missing_004_flagged_not_fixed(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["not_fixed"] == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_missing_004" in content
        assert "[NOT FIXED]" in content

    def test_single_004_not_flagged(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        if _resolve_log(log).exists():
            content = _resolve_log(log).read_text(encoding="utf-8")
            assert "holdings_missing_004" not in content
            assert "holdings_multiple_004" not in content

    def test_multiple_004_flagged_informational(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("004", None, None, content="ocm456"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["not_fixed"] == 0  # informational, not NOT FIXED
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_multiple_004" in content
        assert "[INFORMATIONAL]" in content

    def test_fix_missing_852c_off_by_default(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["log_lines"] == 1  # only the byte-17 leader default
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert not any(code == "c" for code, _ in field852.subfields)

    def test_fix_missing_852c_when_enabled(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        result = m.repair_holdings_records(str(src), str(out), str(log), fix_missing_852c=True)
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "added_missing_852c" in content
        assert "Migration" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in field852.subfields
        assert result["log_lines"] == 2  # byte-17 leader default + this

    def test_fix_missing_852c_noop_when_c_already_present(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        m.repair_holdings_records(str(src), str(out), str(log), fix_missing_852c=True)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert field852.subfields.count(("c", "Stacks")) == 1
        assert not any(code == "c" and data == "Migration" for code, data in field852.subfields)

    def test_cli_fix_missing_852c_flag(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        src = tmp_path / "mixed.mrc"
        src.write_bytes(self._holdings_record(fields=fields))
        rc = m.main([str(src), "--split-bib-holdings", "--fix-missing-852c"])
        assert rc == 0
        out = tmp_path / "mixed_holdings_repaired.mrc"
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in field852.subfields

    def test_cli_repair_holdings_flag_standalone(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        src = tmp_path / "holdings_only.mrc"
        src.write_bytes(self._holdings_record(fields=fields))
        rc = m.main([str(src), "--repair-holdings"])
        assert rc == 0
        out = tmp_path / "holdings_only_repaired.mrc"
        assert out.exists()
        assert m.count_records(str(out)) == 1
        logs = list(tmp_path.glob("holdings_only_log_*.log"))
        assert len(logs) == 1
        content = logs[0].read_text(encoding="utf-8")
        assert "added_default_holdings_008" in content

    def test_cli_repair_holdings_flag_respects_out_and_fix_852c(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        src = tmp_path / "holdings_only.mrc"
        src.write_bytes(self._holdings_record(fields=fields))
        out_path = tmp_path / "custom_out.mrc"
        rc = m.main(
            [str(src), "--repair-holdings", "--fix-missing-852c", "-o", str(out_path)]
        )
        assert rc == 0
        assert out_path.exists()
        parsed = m.read_intact_record(out_path.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in field852.subfields

    def test_repairs_real_short_bucknell_holdings_file(self):
        # Regression/integration check against real production data
        # (a Bucknell export) rather than only synthetic fixtures --
        # confirms the holdings-specific 008 length assumption (32
        # bytes, not bib's 40) actually matches real records, and that
        # a real file with hundreds of records round-trips through the
        # whole pipeline without crashing or losing records.
        import tempfile

        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "short_bucknell_marc_holdings.mrc",
        )
        if not os.path.exists(base):
            pytest.skip("real short_bucknell_marc_holdings.mrc fixture not present")
        n_input = m.count_records(base)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, "out.mrc")
            log = os.path.join(tmpdir, "out.log")
            result = m.repair_holdings_records(base, out, log)
            assert result["total"] == n_input == 528
            assert result["unresolved"] == 0
            assert m.count_records(out) == n_input
