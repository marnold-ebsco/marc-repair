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


# ---------------------------------------------------------------------------
# Mode 2 -- fully stripped delimiters (chat-paste scenario)
#
# NOTE on scope: reconstructing a field's subfields from a fixed known
# length works cleanly when the field's valid subfield codes are a small,
# specific set (most control-ish and short fields). It is genuinely
# underdetermined -- not a bug, a property of the missing information --
# for long free-text fields whose valid codes include common English
# letters (245's $a/$b/$c over a title, for instance): many different
# split points are *structurally* valid, and only human judgment about
# what looks like a real word/name/date can tell the intended one from an
# accidental one. The real six-record fixture exercises exactly this: it
# reliably resolves the easy fields and correctly reports the hard ones as
# needing an override rather than silently guessing -- it should never
# hang and never return a wrong-but-claimed-unique answer.
# ---------------------------------------------------------------------------

class TestModeStrippedDelimiters:
    def test_search_runs_fast_and_never_hangs_on_any_record(self):
        import time

        text = _read("six_records_corrupted.txt")
        starts = m.find_record_starts(text)
        starts.append(len(text))
        for i in range(len(starts) - 1):
            rec_text = text[starts[i]:starts[i + 1]]
            t0 = time.time()
            m.repair_record_stripped(rec_text)
            assert time.time() - t0 < 5, f"record {i} took too long"

    def test_never_returns_a_false_unique_answer(self):
        # The one property that actually matters: whenever the solver
        # claims a record is resolved, the result must be internally
        # consistent (round-trips through the same declared field lengths)
        # -- it must never just be *a* self-consistent guess passed off as
        # certain when other equally-valid splits exist but weren't found.
        # We can't inspect "the one true answer" for real free-text data,
        # but we can and do assert the round-trip property below for every
        # record that claims success.
        text = _read("six_records_corrupted.txt")
        for parsed in m.repair_text(text):
            if parsed.unresolved:
                continue
            raw = m.assemble_marc(parsed)
            reparsed = m.read_intact_record(raw.decode("utf-8"))
            assert reparsed is not None
            assert [f.tag for f in reparsed.fields] == [f.tag for f in parsed.fields]

    def test_simple_fields_with_a_tight_code_set_resolve_with_no_override(self):
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("040", "  ", [("a", "N$T"), ("c", "N$T"), ("d", "OCL")]),
        ]
        text = _make_stripped_record(_SYNTHETIC_LEADER, fields)
        starts = m.find_record_starts(text)
        assert len(starts) == 1
        parsed = m.repair_record_stripped(text)
        assert parsed.unresolved == []
        assert [(f.tag, f.subfields) for f in parsed.fields if not f.is_control()] == [
            ("040", [("a", "N$T"), ("c", "N$T"), ("d", "OCL")])
        ]

    def test_ambiguous_field_reported_unresolved_not_guessed(self):
        # 245's code set includes common letters ("abcfghknps"), so a title
        # containing several of them has more than one structurally-valid
        # split -- this must come back UNRESOLVED, never a guess.
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("245", "00", [("a", "Cats and dogs"), ("c", "by Pat.")]),
        ]
        text = _make_stripped_record(_SYNTHETIC_LEADER, fields)
        parsed = m.repair_record_stripped(text)
        assert parsed.unresolved != []

    def test_override_resolves_an_otherwise_ambiguous_field(self):
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("245", "00", [("a", "Cats and dogs"), ("c", "by Pat.")]),
        ]
        text = _make_stripped_record(_SYNTHETIC_LEADER, fields)
        override_245 = ("00", [("a", "Cats and dogs"), ("c", "by Pat.")])
        parsed = m.repair_record_stripped(text, overrides={1: override_245})
        assert parsed.unresolved == []
        title_field = next(f for f in parsed.fields if f.tag == "245")
        assert title_field.subfields == [("a", "Cats and dogs"), ("c", "by Pat.")]
        raw = m.assemble_marc(parsed)
        reparsed = m.read_intact_record(raw.decode("utf-8"))
        assert reparsed is not None
        assert [f.tag for f in reparsed.fields] == [f.tag for f in parsed.fields]

    def test_unsatisfiable_record_is_unresolved_not_hung(self):
        # Truncating the record makes it impossible for any field split to
        # land exactly on the end of the blob. A small node budget keeps
        # this fast (and deterministic) even though the search would
        # otherwise have to exhaust a large space of technically-valid
        # per-field splits before concluding there's no whole-record fit.
        text = _read("six_records_corrupted.txt")
        starts = m.find_record_starts(text)
        rec_text = text[starts[0]:starts[1]]
        truncated = rec_text[:-1]
        parsed = m.repair_record_stripped(truncated, node_budget=5000)
        assert parsed.unresolved != []


# ---------------------------------------------------------------------------
# Mode 1 -- intact delimiters, corrupted leader/directory ("bad length")
# ---------------------------------------------------------------------------

class TestModeIntactDelimiters:
    @pytest.mark.parametrize(
        "fixture",
        [
            "bad_length_bib_nashvillestate_bibs_202693_me.mrc",
            "bad_missing245_bib_nashvillestate_bibs_202693_me.mrc",
            "bad_bib_mandatoryfieldsnashvillestate_bibs_202693_me.mrc",
        ],
    )
    def test_real_world_fixtures_parse_via_mode1(self, fixture):
        text = _read(fixture)
        results = m.repair_text(text)
        assert results, "expected at least one record"
        for parsed in results:
            assert parsed.unresolved == [], f"{fixture} should parse via Mode 1"

    def test_bad_length_repair_fixes_leader_and_round_trips(self):
        text = _read("bad_length_bib_nashvillestate_bibs_202693_me.mrc")
        results = m.repair_text(text)
        for parsed in results:
            raw = m.assemble_marc(parsed)
            declared_len = int(raw.decode("utf-8")[:5])
            assert declared_len == len(raw.decode("utf-8"))
            reparsed = m.read_intact_record(raw.decode("utf-8"))
            assert reparsed is not None

    def test_bad_length_first_record_title_intact(self):
        text = _read("bad_length_bib_nashvillestate_bibs_202693_me.mrc")
        parsed = m.repair_text(text)[0]
        title_field = next(f for f in parsed.fields if f.tag == "245")
        assert dict(title_field.subfields)["a"] == "Same sex :"

    def test_corrupted_next_leader_does_not_swallow_the_next_record(self):
        # Regression test: a real 91MB export had records whose leader's
        # entry-map field ("4500") was itself corrupted (e.g. "45x0"), which
        # made pattern-matching unable to find that leader at all. The old
        # slice-everything-up-front approach then merged that record's
        # bytes into the *previous* record's slice; Mode 1 would happily
        # stop at its own correct end and never notice -- or return -- the
        # extra ~20KB appended after it, silently dropping an entire record.
        # `iter_repair` must instead determine each record's own end from
        # its own real delimiters and keep going from there, so a mangled
        # *next* leader can't affect the current record's boundary at all.
        good = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("001", None, None, content="rec1")],
        )
        second = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("001", None, None, content="rec2")],
        )
        good_bytes = m.assemble_marc(good).decode("utf-8")
        second_bytes = m.assemble_marc(second).decode("utf-8")
        # corrupt the second record's entry-map field ("4500" -> "45x0"),
        # exactly like the real-world defect, so find_record_starts can't
        # find it via its normal "4500" pattern match.
        corrupted_second = second_bytes[:22] + "x0" + second_bytes[24:]
        text = good_bytes + corrupted_second

        results = m.repair_text(text)

        assert len(results) == 2
        assert results[0].unresolved == []
        assert results[0].fields[0].content == "rec1"
        assert results[1].unresolved == []
        assert results[1].fields[0].content == "rec2"

    def test_numeric_001_right_after_directory_does_not_break_parsing(self):
        # Regression test: a real 2.6GB/706K-record export hit this on a
        # record whose 001 content was "on1000049630" (an OCLC number).
        # The real directory terminator (0x1E) immediately followed by
        # that numeric-looking field data forms one more 12-char chunk
        # that also happens to look like a valid directory entry by
        # coincidence ("\x1eon100004963" -- tag "\x1eon", length/start
        # portion all digits). parse_directory used to greedily grab that
        # bogus extra entry, fail its whole-list cumulative-length check,
        # and discard the (perfectly valid) real entries along with it --
        # bailing this genuinely intact record all the way to
        # UNRESOLVED. It must parse cleanly via Mode 1 instead.
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="on1000049630"),
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )
        text = m.assemble_marc(parsed).decode("utf-8")
        results = m.repair_text(text)
        assert len(results) == 1
        assert results[0].unresolved == []
        field001 = next(f for f in results[0].fields if f.tag == "001")
        assert field001.content == "on1000049630"


# ---------------------------------------------------------------------------
# ensure_field -- patch in a missing required field
# ---------------------------------------------------------------------------


class TestNonNumericTagRoundTrip:
    # Real bug found while adding fix_invalid_tags: parse_directory used
    # to require the WHOLE 12-char directory entry (tag+length+start) to
    # be digits, so a non-numeric tag (exactly the case being fixed) made
    # the parser think the directory ended right before that field,
    # corrupting the rest of the record. A non-numeric tag can also never
    # legitimately be a control tag (those are always 001-009), so
    # _read_intact_at/field_candidates crashed on int(entry.tag) instead
    # of just treating it as a data field.
    def test_record_with_non_numeric_tag_round_trips_intact(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("24A", "00", [("a", "Odd tag")]),
            ],
        )
        raw = m.assemble_marc(parsed)
        results = m.repair_text(raw.decode("utf-8"))
        assert len(results) == 1
        assert results[0].unresolved == []
        tags = [f.tag for f in results[0].fields]
        assert tags == ["008", "24A"]
        field24a = next(f for f in results[0].fields if f.tag == "24A")
        assert field24a.subfields == [("a", "Odd tag")]


class TestAssembleMarcByteLengths:
    # Real bug found via MarcEdit's MarcBreaker reporting "Record length
    # doesn't match reported record length" on records with non-ASCII
    # content: ISO 2709 lengths are byte counts, but assemble_marc used
    # to compute them with Python's len() on the decoded string, which
    # counts *characters* -- undercounting by one byte per multi-byte
    # UTF-8 character (accented letters, "$c©2024", etc.).
    def test_field_length_counts_utf8_bytes_not_characters(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                # "é" (e-acute) is 1 character but 2 bytes in UTF-8
                m.Field_("100", "1 ", [("a", "Renée, author.")]),
            ],
        )
        raw = m.assemble_marc(parsed)
        result = m.read_intact_record(raw.decode("utf-8"))
        assert result is not None
        field = next(f for f in result.fields if f.tag == "100")
        assert field.subfields == [("a", "Renée, author.")]

    def test_record_length_matches_actual_byte_length(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("520", "  ", [("a", "Résumé with café and naïve.")]),
            ],
        )
        raw = m.assemble_marc(parsed)
        declared_length = int(raw[:5])
        assert declared_length == len(raw)


class TestOversizedRecordGuard:
    def test_field_over_9999_bytes_raises(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("500", "  ", [("a", "x" * 10000)])],
        )
        with pytest.raises(m.RepairError, match="9999"):
            m.assemble_marc(parsed)

    def test_base_address_over_99999_raises(self):
        # An enormous number of tiny fields pushes the directory itself
        # (and so the base address) past the 5-digit cap -- unlike total
        # record length, there's no documented sentinel for this, since a
        # reader needs the real base address to find field data at all.
        fields = [m.Field_(f"5{i % 100:02d}", "  ", [("a", "x")]) for i in range(9000)]
        parsed = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        with pytest.raises(m.RepairError, match="base address"):
            m.assemble_marc(parsed)

    def test_total_record_over_99999_bytes_uses_marc21_sentinel(self):
        # Many small fields adding up past the leader's 5-digit length cap,
        # rather than one field past its own 4-digit cap. MARC21's leader
        # spec documents 99999 as a sentinel for "actual length exceeds
        # this field" -- the record is still written out correctly (its
        # real end is always found from the terminator, never trusted from
        # the declared length -- see Mode 1), just with that declared
        # value capped.
        fields = [m.Field_(f"5{i:02d}", "  ", [("a", "x" * 9000)]) for i in range(12)]
        parsed = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        raw = m.assemble_marc(parsed)
        real_length = len(raw.decode("utf-8"))
        assert real_length > 99999
        assert raw[:5] == b"99999"
        # still round-trips correctly despite the leader lying about length
        reparsed = m.read_intact_record(raw.decode("utf-8"))
        assert reparsed is not None
        assert [f.tag for f in reparsed.fields] == [f.tag for f in parsed.fields]

    def test_normal_sized_record_is_unaffected(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "A perfectly normal title.")])],
        )
        raw = m.assemble_marc(parsed)
        assert isinstance(raw, bytes)
        assert raw[:5] != b"99999"


# ---------------------------------------------------------------------------
# write_log -- grouped, labeled log output
# ---------------------------------------------------------------------------

class TestFindDuplicateIdentifiers:
    def test_no_entries_when_all_ids_unique(self):
        id_records = [(0, "u1", "00100"), (1, "u2", "00200"), (2, "u3", "00300")]
        assert m.find_duplicate_identifiers(id_records, "t1") == []

    def test_flags_every_record_sharing_a_duplicated_id(self):
        id_records = [
            (0, "u1", "00100"),
            (5, "u2", "00200"),
            (12, "u1", "00150"),
        ]
        entries = m.find_duplicate_identifiers(id_records, "t1")
        assert len(entries) == 2
        assert {e.record_idx for e in entries} == {0, 12}
        for e in entries:
            assert e.category == "duplicate_identifier"
            assert e.fixed is False
            assert e.record_id == "u1"
        # each entry names the length of *its own* record and points at
        # the other occurrence
        by_idx = {e.record_idx: e for e in entries}
        assert "00100" in by_idx[0].detail
        assert "also records [12]" in by_idx[0].detail
        assert "00150" in by_idx[12].detail
        assert "also records [0]" in by_idx[12].detail

    def test_ids_with_three_or_more_occurrences_all_flagged(self):
        id_records = [(0, "u1", "00100"), (1, "u1", "00100"), (2, "u1", "00100")]
        entries = m.find_duplicate_identifiers(id_records, "t1")
        assert len(entries) == 3
        assert all("3 records" in e.detail for e in entries)

    def test_empty_identifiers_ignored(self):
        id_records = [(0, "", "00100"), (1, "", "00200")]
        assert m.find_duplicate_identifiers(id_records, "t1") == []

    def test_main_end_to_end_logs_duplicate_ids(self, tmp_path):
        rec_a = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="dup1"),
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "First.")]),
            ],
        )
        rec_b = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="dup1"),
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Second, a longer title.")]),
            ],
        )
        raw = m.assemble_marc(rec_a) + m.assemble_marc(rec_b)
        src = tmp_path / "dupes.mrc"
        src.write_bytes(raw)
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "duplicate_identifier" in content
        assert content.count("dup1") >= 2


def _category_header_line(lines: list[str], category: str) -> int:
    """Index of the first ("=== SECTION: category ===") of the 3 header
    lines `write_log` now always writes for `category` -- distinct from
    the description/count lines that follow it, which also start with
    "===" but don't have this "SECTION: category" shape."""
    return next(
        i for i, ln in enumerate(lines)
        if ln.startswith("===") and f": {category} ===" in ln
    )


class TestWriteLog:
    def test_groups_by_fixed_then_category_with_headers(self, tmp_path):
        entries = [
            m.LogEntry("missing_008", False, "t1", 0, "u1", "no 008"),
            # synthetic, never-specially-categorized names -- generic
            # so this test doesn't break if some real fixed category
            # later moves into a dedicated section like FIXED/REQUIRES
            # ATTENTION or INFORMATIONAL (both currently fall back to
            # INFORMATIONAL, not a plain "FIXED" section -- see
            # `_section_for`)
            m.LogEntry("some_fixed_thing", True, "t2", 0, "u1", "added 245"),
            m.LogEntry("missing_008", False, "t3", 1, "u2", "no 008 either"),
            m.LogEntry("some_other_fixed_thing", True, "t4", 1, "u2", "removed 650"),
        ]
        log_path = tmp_path / "run.log"
        m.write_log(str(log_path), entries, full_categories={"missing_008"})
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = _category_header_line(lines, "missing_008")
        fixed_added_header = _category_header_line(lines, "some_fixed_thing")
        fixed_removed_header = _category_header_line(lines, "some_other_fixed_thing")
        assert lines[not_fixed_header].startswith("=== NOT FIXED")
        assert "2 record(s)" in lines[not_fixed_header + 2]
        assert not_fixed_header < fixed_added_header
        assert not_fixed_header < fixed_removed_header
        # the two missing_008 entries are adjacent, not interleaved with
        # the unrelated fixed entries
        missing_008_lines = [ln for ln in lines if ln.startswith("[") and "no 008" in ln]
        assert len(missing_008_lines) == 2

    def test_appends_rather_than_overwrites(self, tmp_path):
        log_path = tmp_path / "run.log"
        full = {"cat_a", "cat_b"}
        m.write_log(
            str(log_path), [m.LogEntry("cat_a", True, "t1", 0, "", "first")], full_categories=full,
        )
        m.write_log(
            str(log_path), [m.LogEntry("cat_b", True, "t2", 0, "", "second")], full_categories=full,
        )
        content = log_path.read_text(encoding="utf-8")
        assert "first" in content
        assert "second" in content

    def test_duplicate_records_section_sits_after_fixed(self, tmp_path):
        entries = [
            m.LogEntry("added_field", True, "t1", 0, "u1", "added 245"),
            m.LogEntry("missing_008", False, "t2", 1, "u2", "no 008"),
            m.LogEntry("duplicate_identifier", False, "t3", 2, "u3", "dup"),
        ]
        log_path = tmp_path / "run.log"
        m.write_log(str(log_path), entries)
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== FIXED"))
        dup_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== DUPLICATE"))
        assert not_fixed_header < fixed_header < dup_header
        # the duplicate entry's own line is tagged with its section, not
        # generically "NOT FIXED", even though .fixed is False
        dup_line = next(ln for ln in lines if "dup" in ln and not ln.startswith("==="))
        assert dup_line.startswith("[DUPLICATE RECORDS]")

    def test_informational_section_sits_after_fixed(self, tmp_path):
        entries = [
            m.LogEntry("missing_008", False, "t1", 0, "u1", "no 008"),
            m.LogEntry("added_field", True, "t2", 0, "u1", "added 245"),
            m.LogEntry("duplicate_identifier", False, "t3", 1, "u2", "dup"),
            m.LogEntry("added_default_008", True, "t4", 2, "u3", "added placeholder 008"),
            m.LogEntry("leader_byte_defaulted", True, "t5", 2, "u3", "byte 05 defaulted"),
            m.LogEntry("leader_entry_map_fixed", True, "t6", 2, "u3", "entry map fixed"),
            m.LogEntry("normalized_subfield_9_to_0", True, "t7", 2, "u3", "9 -> 0"),
        ]
        log_path = tmp_path / "run.log"
        informational_categories = {
            "added_default_008", "leader_byte_defaulted", "leader_entry_map_fixed",
            "normalized_subfield_9_to_0",
        }
        m.write_log(str(log_path), entries, full_categories=informational_categories)
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== FIXED"))
        dup_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== DUPLICATE"))
        informational_headers = [
            _category_header_line(lines, c) for c in informational_categories
        ]
        assert not_fixed_header < fixed_header < dup_header < min(informational_headers)
        info_lines = [ln for ln in lines if ln.startswith("[INFORMATIONAL]")]
        assert len(info_lines) == 4

    def test_blank_line_before_each_header_except_the_first(self, tmp_path):
        entries = [
            m.LogEntry("missing_008", False, "t1", 0, "u1", "no 008"),
            m.LogEntry("added_field", True, "t2", 0, "u1", "added 245"),
        ]
        log_path = tmp_path / "run.log"
        m.write_log(str(log_path), entries)
        lines = log_path.read_text(encoding="utf-8").splitlines()
        # 2 categories * 3 header lines each (SECTION: category /
        # description / count)
        header_block_starts = [
            _category_header_line(lines, "missing_008"),
            _category_header_line(lines, "added_field"),
        ]
        assert header_block_starts[0] == 0
        # second category's header block is preceded by a blank line
        assert lines[header_block_starts[1] - 1] == ""


# ---------------------------------------------------------------------------
# ProgressReporter.maybe_print_estimate -- early one-time runtime estimate
# ---------------------------------------------------------------------------

class TestProgressEstimate:
    def test_does_not_fire_before_thresholds(self, capsys):
        reporter = m.ProgressReporter(total_bytes=1_000_000)
        reporter.maybe_print_estimate(10, 1000)  # below the 200-record floor
        assert reporter.estimate_shown is False
        assert capsys.readouterr().err == ""

    def test_fires_once_past_thresholds(self, capsys):
        reporter = m.ProgressReporter(total_bytes=1_000_000)
        reporter.start_time -= 1.0  # simulate 1s elapsed
        reporter.maybe_print_estimate(200, 100_000)
        assert reporter.estimate_shown is True
        err = capsys.readouterr().err
        assert "Estimated total runtime" in err
        assert "1.0 MB" in err

        # a second call is a no-op -- already shown once
        err_before = err
        reporter.maybe_print_estimate(400, 200_000)
        assert capsys.readouterr().err == ""
        assert err_before  # sanity: the first call did print something

    def test_no_op_without_total_bytes(self, capsys):
        reporter = m.ProgressReporter(total_bytes=0)
        reporter.start_time -= 1.0
        reporter.maybe_print_estimate(200, 100_000)
        assert reporter.estimate_shown is False
        assert capsys.readouterr().err == ""

    def test_ignores_bytes_read_from_buffered_lookahead(self, capsys):
        # Regression test for a real bug: using self.bytes_read (which
        # reflects the streaming reader's buffered-ahead chunk, already
        # much larger than what's actually been processed) instead of
        # the caller-tracked bytes_consumed produced a wildly wrong
        # ("~0s" on a run that actually took 12s) early estimate.
        reporter = m.ProgressReporter(total_bytes=1_000_000)
        reporter.start_time -= 1.0
        reporter.on_progress(32_000_000)  # a whole read-ahead chunk
        reporter.maybe_print_estimate(200, 10_000)  # but only 10KB actually consumed
        err = capsys.readouterr().err
        assert "Estimated total runtime: ~0s" not in err


class TestCLIHelpers:
    def test_default_output_path_appends_repaired_before_extension(self):
        assert m._default_output_path("/tmp/foo.mrc") == "/tmp/foo_repaired.mrc"
        assert m._default_output_path("/tmp/foo") == "/tmp/foo_repaired"

    def test_main_writes_repaired_file_next_to_input(self, tmp_path):
        src = tmp_path / "bad_length.mrc"
        src.write_bytes(
            _read("bad_length_bib_nashvillestate_bibs_202693_me.mrc").encode("utf-8")
        )
        rc = m.main([str(src)])
        assert rc == 0
        expected_out = tmp_path / "bad_length_repaired.mrc"
        assert expected_out.exists()

    def test_main_ensure_field_end_to_end(self, tmp_path):
        src = tmp_path / "missing245.mrc"
        src.write_bytes(
            _read("bad_missing245_bib_nashvillestate_bibs_202693_me.mrc").encode("utf-8")
        )
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out), "--ensure-field", "245:00:a=No title"])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        assert all(any(f.tag == "245" for f in r.fields) for r in results)

    def test_245_missing_a_is_patched_in_place_not_stripped_and_replaced(self, tmp_path):
        # Regression test for record 46032 (u71720) in real data: a 245
        # present but missing its required $a (e.g.
        # "=245 00$h[electronic resource]") keeps its other subfields --
        # $a is added to the existing field rather than the field being
        # stripped by --strip-missing-required-a and rebuilt from
        # scratch (245 is deliberately excluded from required_a_tags.txt
        # for exactly this reason).
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="u71720"),
                m.Field_("245", "00", [("h", "[electronic resource]")]),
            ],
        )
        src = tmp_path / "defective245.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        assert len(results) == 1
        title_fields = [f for f in results[0].fields if f.tag == "245"]
        assert len(title_fields) == 1
        assert title_fields[0].subfields == [("a", "No title"), ("h", "[electronic resource]")]
        # the patched 245 is logged as added_default_245
        # (FIXED/REQUIRES ATTENTION, listed in full by default); the
        # missing-008 default is added_default_008 (INFORMATIONAL --
        # header + count always shown, but not listed in full by
        # default)
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "FIXED/REQUIRES ATTENTION: added_default_245" in content
        assert "INFORMATIONAL: added_default_008" in content
        assert "[FIXED/REQUIRES ATTENTION]\tadded_default_245" in content
        assert "[INFORMATIONAL]\tadded_default_008" not in content


# ---------------------------------------------------------------------------
# count_records / --count -- fast record count, no parsing
# ---------------------------------------------------------------------------

class TestCountRecords:
    def test_counts_one_record(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        src = tmp_path / "one.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        assert m.count_records(str(src)) == 1

    def test_counts_multiple_records(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        raw = m.assemble_marc(parsed) * 5
        src = tmp_path / "five.mrc"
        src.write_bytes(raw)
        assert m.count_records(str(src)) == 5

    def test_counts_across_chunk_boundaries(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("500", "  ", [("a", "x" * 500)])],
        )
        raw = m.assemble_marc(parsed) * 20
        src = tmp_path / "chunked.mrc"
        src.write_bytes(raw)
        # force many small reads so a record terminator landing exactly
        # on a chunk boundary is still counted correctly
        assert m.count_records(str(src), chunk_size=17) == 20

    def test_empty_file_counts_zero(self, tmp_path):
        src = tmp_path / "empty.mrc"
        src.write_bytes(b"")
        assert m.count_records(str(src)) == 0

    def test_main_count_flag_prints_count_and_writes_no_output(self, tmp_path, capsys):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        raw = m.assemble_marc(parsed) * 3
        src = tmp_path / "three.mrc"
        src.write_bytes(raw)
        rc = m.main([str(src), "--count"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "3" in out
        assert not (tmp_path / "three_repaired.mrc").exists()


_HOLDINGS_LEADER = _SYNTHETIC_LEADER[:6] + "x" + _SYNTHETIC_LEADER[7:]


class TestSplitBibHoldings:
    def _bib_record(self) -> bytes:
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )
        return m.assemble_marc(parsed)

    def _holdings_record(self, ok: bool = True) -> bytes:
        fields = [m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH)] if ok else []
        parsed = m.ParsedRecord(
            leader=_HOLDINGS_LEADER,
            entries=[],
            fields=fields + [m.Field_("852", "  ", [("a", "Main Library"), ("h", "ABC123")])],
        )
        return m.assemble_marc(parsed)

    def test_classify_bib_or_holdings(self):
        assert m.classify_bib_or_holdings(self._bib_record()) == "bib"
        assert m.classify_bib_or_holdings(self._holdings_record()) == "holdings"
        assert m.classify_bib_or_holdings(b"") == "unclassified"
        authority_leader = _SYNTHETIC_LEADER[:6] + "z" + _SYNTHETIC_LEADER[7:]
        authority = m.assemble_marc(
            m.ParsedRecord(leader=authority_leader, entries=[], fields=[
                m.Field_("100", "1 ", [("a", "Name.")]),
            ])
        )
        assert m.classify_bib_or_holdings(authority) == "unclassified"

    def test_splits_bib_and_holdings_into_separate_files(self, tmp_path):
        raw = self._bib_record() + self._holdings_record() + self._bib_record()
        src = tmp_path / "mixed.mrc"
        src.write_bytes(raw)
        bib_out = tmp_path / "bib.mrc"
        holdings_out = tmp_path / "holdings.mrc"
        unclassified_out = tmp_path / "unclassified.mrc"
        counts = m.split_bib_holdings(
            str(src), str(bib_out), str(holdings_out), str(unclassified_out)
        )
        assert counts == {"bib": 2, "holdings": 1, "unclassified": 0}
        assert m.count_records(str(bib_out)) == 2
        assert m.count_records(str(holdings_out)) == 1
        assert not unclassified_out.exists()

    def test_splits_correctly_across_many_small_chunks(self, tmp_path):
        # Regression test: a tiny chunk_size forces many reads and many
        # buffer compactions, exercising the cursor/compaction logic
        # (rather than a single in-memory buffer) that replaced an
        # earlier, accidentally-quadratic re-slice-on-every-record
        # implementation.
        records = [self._bib_record(), self._holdings_record()] * 15
        raw = b"".join(records)
        src = tmp_path / "many.mrc"
        src.write_bytes(raw)
        bib_out = tmp_path / "bib.mrc"
        holdings_out = tmp_path / "holdings.mrc"
        unclassified_out = tmp_path / "unclassified.mrc"
        counts = m.split_bib_holdings(
            str(src), str(bib_out), str(holdings_out), str(unclassified_out),
            chunk_size=17,
        )
        assert counts == {"bib": 15, "holdings": 15, "unclassified": 0}
        assert m.count_records(str(bib_out)) == 15
        assert m.count_records(str(holdings_out)) == 15

    def test_unclassified_record_is_not_guessed_at(self, tmp_path):
        authority_leader = _SYNTHETIC_LEADER[:6] + "z" + _SYNTHETIC_LEADER[7:]
        parsed = m.ParsedRecord(
            leader=authority_leader, entries=[], fields=[m.Field_("100", "1 ", [("a", "Name.")])]
        )
        raw = self._bib_record() + m.assemble_marc(parsed)
        src = tmp_path / "mixed.mrc"
        src.write_bytes(raw)
        bib_out = tmp_path / "bib.mrc"
        holdings_out = tmp_path / "holdings.mrc"
        unclassified_out = tmp_path / "unclassified.mrc"
        counts = m.split_bib_holdings(
            str(src), str(bib_out), str(holdings_out), str(unclassified_out)
        )
        assert counts == {"bib": 1, "holdings": 0, "unclassified": 1}
        assert unclassified_out.exists()
        assert m.count_records(str(unclassified_out)) == 1

    def test_main_split_flag_writes_expected_files_and_repairs_holdings(self, tmp_path, capsys):
        raw = self._bib_record() + self._holdings_record(ok=False)
        src = tmp_path / "mixed.mrc"
        src.write_bytes(raw)
        rc = m.main([str(src), "--split-bib-holdings"])
        assert rc == 0
        assert (tmp_path / "mixed_bib.mrc").exists()
        assert (tmp_path / "mixed_holdings.mrc").exists()
        assert (tmp_path / "mixed_holdings_repaired.mrc").exists()
        assert (tmp_path / "mixed_bib_repaired.mrc").exists()
        assert not (tmp_path / "mixed_repaired.mrc").exists()
        assert m.count_records(str(tmp_path / "mixed_bib_repaired.mrc")) == 1
        out = capsys.readouterr().out
        assert "1 bib record(s)" in out
        assert "1 holdings record(s)" in out
        assert "holdings record(s) repaired" in out
        logs = list(tmp_path.glob("mixed_holdings_log_*.log"))
        assert len(logs) == 1
        content = logs[0].read_text(encoding="utf-8")
        assert "added_default_holdings_008" in content
        repaired = m.count_records(str(tmp_path / "mixed_holdings_repaired.mrc"))
        assert repaired == 1

    def test_main_split_flag_bib_side_is_actually_repaired(self, tmp_path):
        # The bib output from the split isn't just copied through --
        # it goes through the same default repair pipeline a plain
        # `marc_repair.py bib.mrc` run would apply. Use a record with a
        # deliberately bad declared length so Mode 1 has something real
        # to fix, and confirm the *repaired* bib file no longer has it.
        bad_bib = self._bib_record()
        bad_bib = b"00000" + bad_bib[5:]  # corrupt the declared length
        raw = bad_bib + self._holdings_record()
        src = tmp_path / "mixed.mrc"
        src.write_bytes(raw)
        rc = m.main([str(src), "--split-bib-holdings"])
        assert rc == 0
        bib_repaired = tmp_path / "mixed_bib_repaired.mrc"
        assert bib_repaired.exists()
        assert m.count_records(str(bib_repaired)) == 1
        parsed = m.read_intact_record(bib_repaired.read_bytes().decode("utf-8"))
        assert parsed.leader[:5] != "00000"


# ---------------------------------------------------------------------------
# _sniff_record_types / main()'s holdings-misroute guard -- refuse to run
# the default bib pipeline against a holdings-only file, which would
# otherwise silently corrupt it (e.g. forcing every 008 to bib's 40 bytes
# instead of holdings' own 32) -- a real production mistake.
# ---------------------------------------------------------------------------

class TestHoldingsMisrouteGuard:
    def _holdings_record(self) -> bytes:
        parsed = m.ParsedRecord(
            leader=_HOLDINGS_LEADER, entries=[],
            fields=[
                m.Field_("004", None, None, content="ocm123"),
                m.Field_("008", None, None, content="x" * m.HOLDINGS_008_LENGTH),
                m.Field_("852", "  ", [("b", "Main Library"), ("h", "ABC123")]),
            ],
        )
        return m.assemble_marc(parsed)

    def _bib_record(self) -> bytes:
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        return m.assemble_marc(parsed)

    def test_sniff_record_types_counts_bib_and_holdings(self, tmp_path):
        src = tmp_path / "mixed.mrc"
        src.write_bytes(self._bib_record() * 2 + self._holdings_record() * 3)
        n_bib, n_holdings = m._sniff_record_types(str(src))
        assert (n_bib, n_holdings) == (2, 3)

    def test_holdings_only_file_refused_by_default_pipeline(self, tmp_path, capsys):
        src = tmp_path / "holdings_only.mrc"
        src.write_bytes(self._holdings_record() * 5)
        rc = m.main([str(src), "-o", str(tmp_path / "out.mrc")])
        assert rc == 2
        assert not (tmp_path / "out.mrc").exists()
        err = capsys.readouterr().err
        assert "holdings-only" in err
        assert "--repair-holdings" in err

    def test_holdings_only_file_still_works_via_repair_holdings_flag(self, tmp_path):
        src = tmp_path / "holdings_only.mrc"
        src.write_bytes(self._holdings_record() * 5)
        rc = m.main([str(src), "--repair-holdings"])
        assert rc == 0
        out = tmp_path / "holdings_only_repaired.mrc"
        assert out.exists()
        parsed = m.read_intact_record(
            out.read_bytes().split(b"\x1d")[0].decode("utf-8") + "\x1d"
        )
        field008 = next(f for f in parsed.fields if f.tag == "008")
        assert len(field008.content) == m.HOLDINGS_008_LENGTH

    def test_normal_bib_file_is_unaffected(self, tmp_path):
        src = tmp_path / "bib_only.mrc"
        src.write_bytes(self._bib_record() * 5)
        rc = m.main([str(src), "-o", str(tmp_path / "out.mrc")])
        assert rc == 0
        assert m.count_records(str(tmp_path / "out.mrc")) == 5

    def test_mixed_file_is_unaffected(self, tmp_path):
        # At least one bib-classified record in the sample -- not
        # holdings-only, so the guard must not fire (--split-bib-holdings
        # remains the documented path for a genuinely mixed file, but the
        # guard's job here is only to catch the holdings-ONLY mistake).
        src = tmp_path / "mixed.mrc"
        src.write_bytes(self._bib_record() + self._holdings_record() * 4)
        rc = m.main([str(src), "-o", str(tmp_path / "out.mrc")])
        assert rc == 0


# ---------------------------------------------------------------------------
# --log-informational
# ---------------------------------------------------------------------------

class TestLogInformational:
    def test_omits_informational_section_by_default(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("520", "  ", [("a", "It’s great.")]),
            ],
        )
        src = tmp_path / "rec.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        # every entry for this record is informational-only -- the
        # header + count still gets written (every check that ran
        # always gets one), just not the per-record detail line
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "INFORMATIONAL: normalized_smart_characters" in content
        assert "[INFORMATIONAL]\tnormalized_smart_characters" not in content
        # the fix itself still ran, even though it's not listed in full
        results = m.repair_text(m._read_text(str(out)))
        field = next(f for f in results[0].fields if f.tag == "520")
        assert field.subfields == [("a", "It's great.")]

    def test_log_informational_flag_includes_the_section(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("520", "  ", [("a", "It’s great.")]),
            ],
        )
        src = tmp_path / "rec.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log-informational", "--log", str(log)])
        assert rc == 0
        assert "[INFORMATIONAL]\tnormalized_smart_characters" in _resolve_log(log).read_text(
            encoding="utf-8"
        )
