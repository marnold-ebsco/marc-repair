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
import re
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


def _detail_line_marker(category: str) -> re.Pattern:
    """Pattern that only matches when `category` has an actual
    per-record detail line logged in full -- not just its header+count
    (LogEntry.render() no longer repeats the category name on every
    row, since it's already stated once in the category's own header
    right above; see LogEntry.render's own comment). Matches the
    category's 3-line header followed immediately by a row starting
    with a tab -- the only thing that can immediately follow the
    count line when at least one record was actually listed."""
    return re.compile(
        rf"=== [^\n]*: {re.escape(category)}(?: \([^)\n]*\))? ===\n"
        rf"=== [^\n]* ===\n"
        rf"=== \d+ record\(s\)(?: - [^\n]*)? ===\n"
        rf"\t"
    )


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
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
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
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ])
        issues, _ = m.check_holdings_record(text, "utf-8")
        assert any(cat == "holdings_missing_004" for cat, _ in issues)

    def test_single_004_is_not_flagged(self):
        text = self._holdings_text([
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ])
        issues, _ = m.check_holdings_record(text, "utf-8")
        cats_004 = ("holdings_missing_004", "holdings_multiple_004")
        assert not any(cat in cats_004 for cat, _ in issues)

    def test_multiple_004_is_flagged(self):
        text = self._holdings_text([
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("004", None, None, content="ocm456"),
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
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
                # $c present -- fix_missing_852c is on by default now, so
                # a record meant to be genuinely "nothing to fix" needs
                # one already, unlike tests specifically about that fix
                # (which build their own fields without it)
                m.Field_(
                    "852", "  ", [("a", "Main Library"), ("c", "Stacks"), ("h", "ABC123")],
                ),
            ]
        return m.assemble_marc(m.ParsedRecord(leader=leader, entries=[], fields=fields))

    def _run(self, tmp_path, records: list[bytes], **kwargs):
        src = self._write_holdings_file(tmp_path, records)
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        result = m.repair_holdings_records(str(src), str(out), str(log), **kwargs)
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
        assert result == {
            "total": 1, "written": 1, "unfixable": 0, "log_lines": 0, "not_fixed": 0,
        }
        assert m.count_records(str(out)) == 1
        # the log file always exists now (a header + count for every
        # check that ran, even with nothing found), but has no
        # per-record detail lines at all for a genuinely clean record
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not any(ln.startswith("[") for ln in content.splitlines())

    def test_missing_008_gets_blank_holdings_placeholder(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks"), ("h", "ABC123")]),
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
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "fixed_holdings_008_length" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field008 = next(f for f in parsed.fields if f.tag == "008")
        assert len(field008.content) == m.HOLDINGS_008_LENGTH

    def test_misplaced_subfield_code_recovered_not_discarded(self, tmp_path):
        # Real defect found in production holdings data: a stray space
        # right after the delimiter, immediately followed by the real
        # code -- e.g. raw "\x1f z Microfilm..." parses as code=" ",
        # data="z Microfilm..." when it really means $z "Microfilm...".
        # Must be recovered before strip_invalid_subfield_codes gets a
        # chance to discard it outright, same as the bib pipeline.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("a", "Main Library"), ("h", "ABC123"),
                (" ", "z Microfilm: 1983-1999"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("fixed_misplaced_subfield_code").search(content)
        assert not _detail_line_marker("removed_invalid_subfield").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("z", " Microfilm: 1983-1999") in f852.subfields

    def test_misplaced_subfield_code_stays_summary_only_even_with_log_full(self, tmp_path):
        # fixed_misplaced_subfield_code is INFORMATIONAL -- never listed
        # in full, not even via full_categories.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("a", "Main Library"), ("h", "ABC123"),
                (" ", "z Microfilm: 1983-1999"),
            ]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        m.repair_holdings_records(
            str(src), str(out), str(log), full_categories={"fixed_misplaced_subfield_code"},
        )
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("fixed_misplaced_subfield_code").search(content)
        assert "INFORMATIONAL: fixed_misplaced_subfield_code" in content

    def test_null_identifier_removed_but_not_logged_by_default(self, tmp_path):
        # $b is the null identifier under test, on a field OTHER than
        # 852 -- 852's own empty subfields have their own specific
        # fixes (see strip_empty_852_subfields), so isolating this
        # generic catch-all needs a different tag. $a is real,
        # non-empty data so the field survives strip_empty_fields (a
        # field with ONLY an empty subfield is dropped entirely and
        # silently -- see strip_empty_fields -- so isolating the
        # null-identifier case needs at least one other non-empty
        # subfield alongside it)
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
            m.Field_("500", "  ", [("a", "Main Library"), ("b", "")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("removed_null_identifier").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f500 = next(f for f in parsed.fields if f.tag == "500")
        assert not any(code == "b" for code, _ in f500.subfields)
        assert ("a", "Main Library") in f500.subfields

    def test_null_identifier_stays_summary_only_even_with_log_full(self, tmp_path):
        # removed_null_identifier is INFORMATIONAL -- never listed in
        # full, not even via full_categories.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
            m.Field_("500", "  ", [("a", "Main Library"), ("b", "")]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        m.repair_holdings_records(
            str(src), str(out), str(log), full_categories={"removed_null_identifier"},
        )
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("removed_null_identifier").search(content)
        assert "=== INFORMATIONAL:" in content

    def test_escape_sequence_flagged_not_transcoded(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library\x1b(Bfoo"), ("h", "ABC123")]),
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
        assert "=== FIXED/REQUIRES ATTENTION:" in content
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
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        assert "encoding level" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert parsed.leader[17] == "u"

    def test_unresolvable_record_diverted_to_error_file(self, tmp_path):
        # A whole file with literally no MARC leader anywhere is a fatal
        # RepairError for iter_repair_stream (nowhere to even start) --
        # per-record UNRESOLVED handling instead kicks in for a *trailing*
        # chunk after at least one real leader was found, which is what
        # this exercises: one clean record, then trailing garbage with
        # no leader of its own. Treated the same as the bib pipeline's
        # own unfixable records: diverted, byte-for-byte, to the "_error"
        # file rather than left in the main output.
        garbage = b"not a marc record at all, no leader here whatsoever" + b"\x1d"
        result, out, log = self._run(tmp_path, [self._holdings_record(), garbage])
        assert result["total"] == 2
        assert result["written"] == 1
        assert result["unfixable"] == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== UNFIXABLE: unfixable ===" in content
        error_path = m._error_output_path(str(out))
        assert os.path.exists(error_path)

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

    def test_invalid_tag_stripped_when_every_9xx_slot_is_taken(self, tmp_path):
        # Same fallback as the bib pipeline's own test of this
        # (TestFixInvalidTags::test_falls_back_to_stripping_when_every_
        # 9xx_slot_is_taken): with every 900-999 tag already used
        # elsewhere in the file, there's nowhere to rename an invalid
        # tag to, so the field is stripped out entirely instead --
        # which needs a full second-pass rewrite of the output file
        # (removal changes record length, unlike a rename).
        filler_fields = [
            m.Field_("004", None, None, content="ocm-filler"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
        ]
        for n in range(900, 1000):
            filler_fields.append(m.Field_(str(n), "  ", [("a", "filler")]))
        filler_record = self._holdings_record(fields=filler_fields)
        bad_tag_record = self._holdings_record(fields=[
            m.Field_("004", None, None, content="ocm-badtag"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks")]),
            m.Field_("85Z", "00", [("a", "bad tag")]),
        ])
        result, out, log = self._run(tmp_path, [filler_record, bad_tag_record])
        assert result["total"] == 2
        assert result["written"] == 2
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "unfixed_non_numeric_tag" in content
        assert "content discarded" in content
        results = m.repair_text(m._read_text(str(out)))
        assert not any(f.tag == "85Z" for f in results[1].fields)
        assert sum(1 for f in results[0].fields if f.tag.startswith("9")) == 100

    def test_missing_004_flagged_not_fixed(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["not_fixed"] == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_missing_004" in content
        assert "=== NOT FIXED:" in content

    def test_single_004_not_flagged(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("holdings_missing_004").search(content)
        assert not _detail_line_marker("holdings_multiple_004").search(content)

    def test_multiple_004_flagged_informational(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("004", None, None, content="ocm456"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["not_fixed"] == 0  # informational, not NOT FIXED
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_multiple_004" in content
        assert "=== INFORMATIONAL:" in content

    def test_multiple_852_split_into_separate_records(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
            m.Field_("852", "  ", [("b", "Annex"), ("h", "XYZ789")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["total"] == 1
        assert result["written"] == 2
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert _detail_line_marker("split_holdings_multiple_852").search(content)
        assert "1 record(s) - 1 new holdings record(s) added" in content
        assert m.count_records(str(out)) == 2
        records = [
            m.read_intact_record(text)
            for text in out.read_bytes().decode("utf-8").split(m.RECTERM)[:-1]
        ]
        assert len(records) == 2
        for rec in records:
            assert sum(1 for f in rec.fields if f.tag == "852") == 1
        b_values = {
            data for rec in records for f in rec.fields if f.tag == "852"
            for code, data in f.subfields if code == "b"
        }
        assert b_values == {"Main Library", "Annex"}

    def test_split_count_note_sums_new_records_across_multiple_source_records(self, tmp_path):
        two_852s = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
            m.Field_("852", "  ", [("b", "Annex"), ("h", "XYZ789")]),
        ]
        three_852s = [
            m.Field_("004", None, None, content="local456"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "AAA111")]),
            m.Field_("852", "  ", [("b", "Annex"), ("h", "BBB222")]),
            m.Field_("852", "  ", [("b", "Storage"), ("h", "CCC333")]),
        ]
        result, out, log = self._run(
            tmp_path,
            [self._holdings_record(fields=two_852s), self._holdings_record(fields=three_852s)],
        )
        assert result["total"] == 2
        assert result["written"] == 5
        content = _resolve_log(log).read_text(encoding="utf-8")
        # 1 new record from the first split + 2 new from the second = 3
        assert "2 record(s) - 3 new holdings record(s) added" in content

    def test_multiple_852_split_copies_get_suffixed_001(self, tmp_path):
        fields = [
            m.Field_("001", None, None, content="12345"),
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
            m.Field_("852", "  ", [("b", "Annex"), ("h", "XYZ789")]),
            m.Field_("852", "  ", [("b", "Storage"), ("h", "QRS456")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["written"] == 3
        records = [
            m.read_intact_record(text)
            for text in out.read_bytes().decode("utf-8").split(m.RECTERM)[:-1]
        ]
        by_b = {
            next(data for code, data in f.subfields if code == "b"): next(
                fld.content for fld in rec.fields if fld.tag == "001"
            )
            for rec in records for f in rec.fields if f.tag == "852"
        }
        assert by_b == {
            "Main Library": "12345",
            "Annex": "12345-2",
            "Storage": "12345-3",
        }

    def test_multiple_852_incomplete_one_dropped_not_duplicated(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
            m.Field_("852", "  ", [("a", "INT"), ("k", "Full text")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["total"] == 1
        assert result["written"] == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert _detail_line_marker("incomplete_852").search(content)
        assert "no $b" in content
        assert not _detail_line_marker("split_holdings_multiple_852").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852s = [f for f in parsed.fields if f.tag == "852"]
        assert len(f852s) == 1
        assert ("b", "Main Library") in f852s[0].subfields

    def test_single_852_with_no_b_not_touched(self, tmp_path):
        # A lone 852 (no other 852 to be ambiguous against) is left
        # alone here regardless of which subfield carries its location
        # -- see `fix_missing_852_location` for the generic $a/$b/$c
        # handling that already covers this case.
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "INT"), ("k", "Full text")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        assert result["total"] == 1
        assert result["written"] == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("incomplete_852").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852s = [f for f in parsed.fields if f.tag == "852"]
        assert len(f852s) == 1

    def test_single_852_not_flagged(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("split_holdings_multiple_852").search(content)

    def test_fix_missing_852c_on_by_default(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "added_missing_852c" in content
        assert result["log_lines"] == 2  # byte-17 leader default + this
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in field852.subfields

    def test_fix_missing_852c_disabled_via_flag(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        result = m.repair_holdings_records(str(src), str(out), str(log), fix_missing_852c=False)
        assert result["log_lines"] == 1  # only the byte-17 leader default
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert not any(code == "c" for code, _ in field852.subfields)

    def test_fix_missing_852c_when_enabled(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        result = m.repair_holdings_records(
            str(src), str(out), str(log), fix_missing_852c=True,
        )
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== FIXED/REQUIRES ATTENTION: added_missing_852c ===" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in field852.subfields
        assert result["log_lines"] == 2  # byte-17 leader default + this

    def test_fix_missing_852c_noop_when_c_already_present(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("c", "Stacks"), ("h", "ABC123")]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        m.repair_holdings_records(str(src), str(out), str(log), fix_missing_852c=True)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert field852.subfields.count(("c", "Stacks")) == 1
        assert not any(code == "c" and data == "Migration" for code, data in field852.subfields)

    def test_cli_fix_missing_852c_on_by_default(self, tmp_path):
        fields = [
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        src = tmp_path / "mixed.mrc"
        src.write_bytes(self._holdings_record(fields=fields))
        rc = m.main([str(src), "--split-bib-holdings"])
        assert rc == 0
        out = tmp_path / "mixed_holdings_repaired.mrc"
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in field852.subfields

    def test_cli_repair_holdings_flag_standalone(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
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
        rc = m.main([str(src), "--repair-holdings", "-o", str(out_path)])
        assert rc == 0
        assert out_path.exists()
        parsed = m.read_intact_record(out_path.read_bytes().decode("utf-8"))
        field852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in field852.subfields

    def test_duplicate_001_flagged_across_records(self, tmp_path):
        fields_a = [
            m.Field_("001", None, None, content="dup1"),
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        fields_b = [
            m.Field_("001", None, None, content="dup1"),
            m.Field_("004", None, None, content="ocm456"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Annex"), ("h", "XYZ789")]),
        ]
        result, out, log = self._run(tmp_path, [
            self._holdings_record(fields=fields_a),
            self._holdings_record(fields=fields_b),
        ])
        assert result["total"] == 2
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "duplicate_identifier" in content
        assert content.count("dup1") >= 2
        # both records still made it into the output, unmodified by the
        # (unfixable) duplicate check
        assert m.count_records(str(out)) == 2

    def test_863_868_missing_a_is_removed_and_logged(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
            m.Field_("863", "40", [("z", "no enumeration data")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "field_removed_because_missing_a" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert not any(f.tag == "863" for f in parsed.fields)

    def test_863_868_with_a_is_kept(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
            m.Field_("866", "30", [("a", "v.1-10")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f866 = next(f for f in parsed.fields if f.tag == "866")
        assert ("a", "v.1-10") in f866.subfields

    def test_853_kept_with_g_alternate_and_no_a(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
            m.Field_("853", "20", [("g", "no.")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert any(f.tag == "853" for f in parsed.fields)

    def test_855_kept_with_i_only_chronology_pattern(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
            m.Field_("855", "20", [("i", "(year)")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert any(f.tag == "855" for f in parsed.fields)

    def test_854_removed_when_none_of_a_g_i_present(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
            m.Field_("854", "20", [("z", "public note only")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "field_removed_because_missing_a" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert not any(f.tag == "854" for f in parsed.fields)

    def test_852_missing_h_entirely_left_alone_and_not_logged_by_default(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("missing_call_number").search(content)
        assert not _detail_line_marker("removed_bad_call_number").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("a", "Main Library") in f852.subfields

    def test_852_missing_h_stays_summary_only_even_with_log_full(self, tmp_path):
        # missing_call_number is INFORMATIONAL -- never listed in full,
        # not even via full_categories.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library")]),
        ]
        src = self._write_holdings_file(tmp_path, [self._holdings_record(fields=fields)])
        out = tmp_path / "out.mrc"
        log = tmp_path / "out.log"
        m.repair_holdings_records(
            str(src), str(out), str(log), full_categories={"missing_call_number"},
        )
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("missing_call_number").search(content)
        assert "=== INFORMATIONAL:" in content
        assert not _detail_line_marker("removed_bad_call_number").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("a", "Main Library") in f852.subfields

    def test_852_unusable_h_is_removed_but_rest_of_field_kept(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "--")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "removed_bad_call_number" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        assert not _detail_line_marker("missing_call_number").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("a", "Main Library") in f852.subfields
        assert not any(code == "h" for code, _ in f852.subfields)

    def test_852_with_h_is_kept(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("missing_call_number").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        assert any(f.tag == "852" for f in parsed.fields)

    def test_852_missing_location_gets_migration_placeholder_in_b(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "added_missing_852_location" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "Migration") in f852.subfields

    def test_852_punctuation_only_b_is_replaced_with_migration(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "--"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "added_missing_852_location" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "Migration") in f852.subfields
        assert not any(code == "b" and data == "--" for code, data in f852.subfields)

    def test_852_with_usable_a_is_not_given_a_placeholder(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("added_missing_852_location").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert not any(code == "b" for code, _ in f852.subfields)

    def test_852_with_usable_b_is_untouched(self, tmp_path):
        # Real production convention seen at scale: $a unused throughout
        # an entire export, $b alone carrying the actual location code.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "OFC Main"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("added_missing_852_location").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "OFC Main") in f852.subfields

    def test_852_with_usable_c_is_not_given_a_placeholder(self, tmp_path):
        # Real, different convention: OCLC WMS exports put location in $c.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("c", "Main Stacks"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("added_missing_852_location").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert not any(code == "b" for code, _ in f852.subfields)

    def test_852_with_none_of_a_b_c_gets_placeholder_in_b(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("h", "ABC123"), ("t", "Copy 1")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "added_missing_852_location" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "Migration") in f852.subfields

    def test_852_placeholder_goes_in_c_for_wms_ocm_prefixed_004(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123456"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "added missing 852 $c" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in f852.subfields
        assert not any(code == "b" for code, _ in f852.subfields)

    @pytest.mark.parametrize("prefix", ["on", "ocn", "ocm", "OCM", "Ocn"])
    def test_852_placeholder_goes_in_c_for_every_wms_prefix(self, tmp_path, prefix):
        fields = [
            m.Field_("004", None, None, content=f"{prefix}9999"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("c", "Migration") in f852.subfields

    def test_852_placeholder_stays_in_b_for_non_wms_004(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="sirsi123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "Migration") in f852.subfields

    def test_852_missing_h_with_usable_b_flags_call_number_but_skips_location_fix(
        self, tmp_path,
    ):
        # $h missing (flagged, field left alone) but $b already usable
        # -- fix_missing_852_location must not ALSO insert a placeholder
        # here, since the field already has a usable location.
        fields = [
            m.Field_("004", None, None, content="local123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Annex")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        log_path = _resolve_log(log)
        content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        assert not _detail_line_marker("missing_call_number").search(content)
        assert not _detail_line_marker("added_missing_852_location").search(content)
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "Annex") in f852.subfields

    def test_multiple_b_after_h_recoded_to_i(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "OFC Main"), ("h", "Z678.9 A2"), ("b", "A96 1983"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "recoded_852_b_to_i" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        assert "recoded" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("i", "A96 1983") in f852.subfields
        assert sum(1 for code, _ in f852.subfields if code == "b") == 1
        assert ("b", "OFC Main") in f852.subfields

    def test_multiple_b_not_recoded_when_i_already_present(self, tmp_path):
        # Same before/after-$h shape, but $i is already there -- must
        # NOT recode (would overwrite real item-part data); falls
        # through to the non-location/fallback removal instead.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "LAW"), ("b", "LAW REF"), ("h", "K120"), ("i", ".M69 1993"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("i", ".M69 1993") in f852.subfields
        assert sum(1 for code, _ in f852.subfields if code == "b") == 1

    def test_multiple_b_non_location_value_removed(self, tmp_path):
        # Second $b looks like a bare cutter fragment, not a location --
        # removed outright rather than recoded (the before/after-$h
        # shape doesn't apply here: this one has no $h at all).
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "OFC SC"), ("k", "B"), ("b", "KGL104"), ("i", ".K66 1992"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "removed_extra_852_b" in content
        assert "doesn't look like a location code" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "OFC SC") in f852.subfields
        assert not any(code == "b" and data == "KGL104" for code, data in f852.subfields)
        assert sum(1 for code, _ in f852.subfields if code == "b") == 1

    def test_multiple_b_exact_duplicate_collapsed(self, tmp_path):
        # Both $b's are identical -- unlike a genuine ambiguity between
        # two different candidates, this is actually certain, so it
        # gets its own, more specific log message.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "OFC Main"), ("b", "OFC Main"), ("h", "NA3760"), ("i", ".L46 1998"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "removed_extra_852_b" in content
        assert "identical to another $b already in this field" in content
        assert "no way to tell which $b is the real location" not in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert sum(1 for code, _ in f852.subfields if code == "b") == 1
        assert ("b", "OFC Main") in f852.subfields
        assert ("i", ".L46 1998") in f852.subfields

    def test_multiple_b_ambiguous_removes_last(self, tmp_path):
        # Neither $b looks more or less like a location than the
        # other -- no principled way to choose, so the last one is
        # dropped as an arbitrary (but flagged) fallback.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "LAW"), ("b", "LAW REF"), ("h", "K120"), ("i", ".M69 1993"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "no way to tell which $b is the real location" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "LAW") in f852.subfields
        assert not any(code == "b" and data == "LAW REF" for code, data in f852.subfields)

    def test_multiple_b_empty_second_b_removed(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "OFC SC"), ("b", ""), ("h", "PE1068.B3"), ("i", "R35 1990"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "OFC SC") in f852.subfields
        assert sum(1 for code, _ in f852.subfields if code == "b") == 1

    def test_single_b_untouched(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "OFC Main"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("recoded_852_b_to_i").search(content)
        assert not _detail_line_marker("removed_extra_852_b").search(content)

    def test_duplicate_h_removed_and_flagged_fixed(self, tmp_path):
        # $h (Classification part) is Not Repeatable per the MARC 21
        # 852 spec -- unlike $b/$c, which ARE officially repeatable
        # there (see fix_852_multiple_b's own docstring), so a second
        # $h is a genuine structural violation: removed, keeping the
        # first occurrence.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "OFC Main"), ("h", "Z678.9"), ("h", "A2 1983"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_852_duplicate_nr_subfield" in content
        assert "Not-Repeatable subfield" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("h", "Z678.9") in f852.subfields
        assert ("h", "A2 1983") not in f852.subfields
        assert sum(1 for code, _ in f852.subfields if code == "h") == 1

    def test_duplicate_t_removed_and_flagged_fixed(self, tmp_path):
        # A second non-repeatable code besides $h, to confirm the check
        # isn't hardcoded to $h specifically.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "OFC Main"), ("h", "ABC123"), ("t", "1"), ("t", "2"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_852_duplicate_nr_subfield" in content
        assert "Not-Repeatable subfield" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("t", "1") in f852.subfields
        assert ("t", "2") not in f852.subfields

    def test_duplicate_b_or_c_not_flagged_as_non_repeatable(self, tmp_path):
        # $b and $c are officially Repeatable per the spec -- this
        # detect-only check must not flag them (fix_852_multiple_b
        # handles $b separately, on data-quality grounds, not spec
        # grounds).
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("c", "Main Stacks"), ("c", "Annex"), ("h", "ABC123"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("holdings_852_duplicate_nr_subfield").search(content)

    def test_853_missing_8_flagged_not_fixed(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
            m.Field_("853", "20", [("a", "2nd 1997")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_853_missing_8" in content
        assert "=== NOT FIXED:" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f853 = next(f for f in parsed.fields if f.tag == "853")
        assert ("a", "2nd 1997") in f853.subfields

    def test_856_missing_u_flagged_not_fixed(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
            m.Field_("856", "  ", [("a", "Fulltext Ebsco OA database 1911-2013")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_856_missing_u" in content
        assert "=== NOT FIXED:" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f856 = next(f for f in parsed.fields if f.tag == "856")
        assert ("a", "Fulltext Ebsco OA database 1911-2013") in f856.subfields

    def test_852_b_purely_numeric_replaced_with_migration(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", " 0", [("b", "42"), ("a", "1")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_852_b_suspect_content" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        assert "purely numeric" in content
        assert "$b42" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", m.DEFAULT_852_LOCATION_CONTENT) in f852.subfields
        assert not any(code == "b" and data == "42" for code, data in f852.subfields)

    def test_852_b_flattened_subfield_markers_replaced_with_migration(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", " 0", [("b", "#8 0 #a 1")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "holdings_852_b_suspect_content" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        assert "contains multiple '#' characters" in content
        assert "replaced with placeholder" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", m.DEFAULT_852_LOCATION_CONTENT) in f852.subfields
        assert not any(code == "b" and "#" in data for code, data in f852.subfields)

    def test_852_b_single_digit_replaced_with_migration_by_default(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", " 0", [("b", "0"), ("a", "1")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", m.DEFAULT_852_LOCATION_CONTENT) in f852.subfields

    def test_852_b_single_digit_left_alone_with_allow_flag(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", " 0", [("b", "0"), ("a", "1")]),
        ]
        result, out, log = self._run(
            tmp_path, [self._holdings_record(fields=fields)],
            allow_single_digit_852b=True,
        )
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== FIXED/REQUIRES ATTENTION: holdings_852_b_suspect_content ===" in content
        assert "=== 0 record(s) ===" in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", "0") in f852.subfields

    def test_852_b_multi_digit_still_replaced_with_allow_flag(self, tmp_path):
        # --allow-single-digit-852b only exempts a single digit -- a
        # multi-digit numeric $b is still suspect either way.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", " 0", [("b", "42"), ("a", "1")]),
        ]
        result, out, log = self._run(
            tmp_path, [self._holdings_record(fields=fields)],
            allow_single_digit_852b=True,
        )
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert ("b", m.DEFAULT_852_LOCATION_CONTENT) in f852.subfields

    def test_852_b_normal_text_not_flagged_suspect(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "OFC Main"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("holdings_852_b_suspect_content").search(content)

    def test_empty_852_subfield_removed_and_flagged_fixed(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [
                ("b", "OFC Main"), ("h", "ABC123"), ("2", ""), ("z", "keep this"),
            ]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "removed_empty_852_subfield" in content
        assert "=== FIXED/REQUIRES ATTENTION:" in content
        assert "holdings_null_identifier" not in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f852 = next(f for f in parsed.fields if f.tag == "852")
        assert not any(code == "2" for code, _ in f852.subfields)
        assert ("z", "keep this") in f852.subfields
        assert ("b", "OFC Main") in f852.subfields

    def test_empty_852_h_not_touched_by_generic_strip(self, tmp_path):
        # Empty $h is handled specifically by fix_852_call_number
        # (category "removed_bad_call_number") -- the generic strip
        # must not also touch it or double-log it.
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "OFC Main"), ("h", "")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert _detail_line_marker("removed_bad_call_number").search(content)
        assert not _detail_line_marker("removed_empty_852_subfield").search(content)

    def test_no_empty_852_subfield_is_noop(self, tmp_path):
        fields = [
            m.Field_("004", None, None, content="ocm123"),
            m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
            m.Field_("852", "  ", [("b", "OFC Main"), ("h", "ABC123")]),
        ]
        result, out, log = self._run(tmp_path, [self._holdings_record(fields=fields)])
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert not _detail_line_marker("removed_empty_852_subfield").search(content)

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
            assert result["unfixable"] == 0
            assert m.count_records(out) == n_input
