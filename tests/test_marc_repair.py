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

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marc_repair as m  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


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


# ---------------------------------------------------------------------------
# ensure_field -- patch in a missing required field
# ---------------------------------------------------------------------------

class TestEnsureField:
    def test_adds_245_only_where_missing(self):
        text = _read("bad_missing245_bib_nashvillestate_bibs_202693_me.mrc")
        results = m.repair_text(text)
        added_count = 0
        for parsed in results:
            had_245_before = any(f.tag == "245" for f in parsed.fields)
            added = m.ensure_field(parsed, "245", "00", [("a", "No title")])
            assert added != had_245_before
            if added:
                added_count += 1
            assert any(f.tag == "245" for f in parsed.fields)
        assert added_count > 0, "fixture should have at least one record missing 245"

    def test_does_not_duplicate_existing_field(self):
        text = _read("bad_length_bib_nashvillestate_bibs_202693_me.mrc")
        parsed = m.repair_text(text)[0]
        assert any(f.tag == "245" for f in parsed.fields)
        added = m.ensure_field(parsed, "245", "00", [("a", "No title")])
        assert added is False
        assert sum(1 for f in parsed.fields if f.tag == "245") == 1

    def test_inserted_field_keeps_ascending_tag_order(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("100", "1 ", [("a", "Someone")]),
                m.Field_("260", "  ", [("a", "Place")]),
            ],
        )
        m.ensure_field(parsed, "245", "00", [("a", "No title")])
        assert [f.tag for f in parsed.fields] == ["100", "245", "260"]

    def test_parse_ensure_field_spec_multi_subfield(self):
        tag, indicators, subfields = m.parse_ensure_field_spec(
            "500:  :a=Note one|a=Note two"
        )
        assert tag == "500"
        assert indicators == "  "
        assert subfields == [("a", "Note one"), ("a", "Note two")]

    def test_parse_ensure_field_spec_rejects_bad_indicators(self):
        with pytest.raises(ValueError):
            m.parse_ensure_field_spec("245:0:a=No title")

    def test_parse_ensure_field_spec_control_field(self):
        default_008 = "780615s19uu    xx a                    d"
        tag, indicators, content = m.parse_ensure_field_spec(f"008:{default_008}")
        assert tag == "008"
        assert indicators is None
        assert content == default_008

    def test_ensure_field_adds_control_field_when_missing(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("001", None, None, content="abc123")],
        )
        default_008 = "780615s19uu    xx a                    d"
        added = m.ensure_field(parsed, "008", None, default_008)
        assert added is True
        field_008 = next(f for f in parsed.fields if f.tag == "008")
        assert field_008.is_control()
        assert field_008.content == default_008

    def test_ensure_field_control_field_not_duplicated(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="existing")],
        )
        added = m.ensure_field(parsed, "008", None, "new default")
        assert added is False
        assert sum(1 for f in parsed.fields if f.tag == "008") == 1
        assert next(f for f in parsed.fields if f.tag == "008").content == "existing"

    def test_ensure_field_control_field_keeps_ascending_tag_order(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="abc123"),
                m.Field_("100", "1 ", [("a", "Someone")]),
            ],
        )
        m.ensure_field(parsed, "008", None, "780615s19uu    xx a                    d")
        assert [f.tag for f in parsed.fields] == ["001", "008", "100"]

    def test_main_fills_missing_008_end_to_end(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "A title with no 008.")])],
        )
        src = tmp_path / "no008.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        default_008 = "780615s19uu    xx a                    d"
        rc = m.main([
            str(src), "-o", str(out), "--ensure-field", f"008:{default_008}",
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        assert len(results) == 1
        field_008 = next(f for f in results[0].fields if f.tag == "008")
        assert field_008.content == default_008


# ---------------------------------------------------------------------------
# _pad_short_indicators / --fix-bad-indicators
# ---------------------------------------------------------------------------

def _drop_indicator_chars(raw: str, n: int) -> str:
    """Corrupt a well-formed record's 245 field by removing `n` (1 or 2)
    of its indicator characters, simulating the real-world defect where
    indicator byte(s) get dropped. Everything else in the record --
    including the directory's declared lengths, now stale -- is left
    untouched, matching how this defect looks in practice: only the field
    data shifts, nothing about the leader/directory is touched."""
    needle = "00" + m.SUBFIELD + "a"
    assert needle in raw
    return raw.replace(needle, needle[n:])


class TestFixBadIndicators:
    def test_default_off_rejects_short_indicators(self):
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("245", "00", [("a", "Title.")]),
        ]
        parsed = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        raw = m.assemble_marc(parsed).decode("utf-8")
        corrupted = _drop_indicator_chars(raw, 1)
        assert m._read_intact_at(corrupted, 0) is None

    def test_pads_one_missing_indicator_and_records_fix(self):
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("245", "00", [("a", "Title.")]),
        ]
        parsed = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        raw = m.assemble_marc(parsed).decode("utf-8")
        corrupted = _drop_indicator_chars(raw, 1)
        result = m._read_intact_at(corrupted, 0, fix_bad_indicators=True)
        assert result is not None
        fixed, _end = result
        title_field = next(f for f in fixed.fields if f.tag == "245")
        assert title_field.indicators == " 0"
        assert title_field.subfields == [("a", "Title.")]
        assert fixed.indicator_fixes == [("245", 1)]

    def test_pads_two_missing_indicators_and_records_fix(self):
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("245", "00", [("a", "Title.")]),
        ]
        parsed = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        raw = m.assemble_marc(parsed).decode("utf-8")
        corrupted = _drop_indicator_chars(raw, 2)
        result = m._read_intact_at(corrupted, 0, fix_bad_indicators=True)
        assert result is not None
        fixed, _end = result
        title_field = next(f for f in fixed.fields if f.tag == "245")
        assert title_field.indicators == "  "
        assert fixed.indicator_fixes == [("245", 2)]

    def test_untouched_record_has_no_fixes_recorded(self):
        fields = [m.Field_("245", "00", [("a", "Title.")])]
        parsed = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        raw = m.assemble_marc(parsed).decode("utf-8")
        result = m._read_intact_at(raw, 0, fix_bad_indicators=True)
        assert result is not None
        fixed, _end = result
        assert fixed.indicator_fixes == []

    def test_main_end_to_end_logs_as_fixed_at_bottom(self, tmp_path):
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("008", None, None, content="x" * 40),
            m.Field_("245", "00", [("a", "Title.")]),
            # invalid subfield code -- fixed by default, so this test
            # passes --no-strip-invalid-subfield-codes to keep it flagged
            # instead, giving this test a genuine NOT FIXED entry to
            # check ordering against
            m.Field_("500", "  ", [("Z", "bad code")]),
        ]
        parsed = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        raw = m.assemble_marc(parsed).decode("utf-8")
        corrupted = _drop_indicator_chars(raw, 1)
        src = tmp_path / "bad_indicators.mrc"
        src.write_bytes(corrupted.encode("utf-8"))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--no-strip-invalid-subfield-codes", "--log", str(log),
        ])
        assert rc == 0
        lines = log.read_text(encoding="utf-8").splitlines()
        not_fixed_idx = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        fixed_idx = next(i for i, ln in enumerate(lines) if ln.startswith("=== FIXED"))
        assert not_fixed_idx < fixed_idx, "NOT FIXED block must come before FIXED block"
        assert any("[FIXED]" in ln and "padded" in ln for ln in lines[fixed_idx:])
        assert any("[NOT FIXED]" in ln for ln in lines[not_fixed_idx:fixed_idx])


# ---------------------------------------------------------------------------
# transcode_marc8_to_utf8 -- ANSEL diacritics -> Unicode
# ---------------------------------------------------------------------------

class TestTranscodeMarc8:
    def test_converts_combining_diacritics_and_flips_leader_byte(self):
        pytest.importorskip("pymarc")
        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "  # declare MARC-8
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[
                m.Field_("001", None, None, content="abc123"),
                m.Field_("100", "1 ", [("a", "Bal\xe5asim, \xf2Hasan.")]),
            ],
        )
        changed = m.transcode_marc8_to_utf8(parsed)
        assert changed is True
        assert parsed.leader[9] == "a"
        name_field = next(f for f in parsed.fields if f.tag == "100")
        assert name_field.subfields == [("a", "Balāsim, Ḥasan.")]
        # control fields (plain ASCII) pass through unchanged
        control = next(f for f in parsed.fields if f.tag == "001")
        assert control.content == "abc123"

    def test_noop_when_already_unicode(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,  # position 9 is "a" already
            entries=[],
            fields=[m.Field_("100", "1 ", [("a", "Plain name")])],
        )
        assert _SYNTHETIC_LEADER[9] == "a"
        changed = m.transcode_marc8_to_utf8(parsed)
        assert changed is False
        name_field = next(f for f in parsed.fields if f.tag == "100")
        assert name_field.subfields == [("a", "Plain name")]

    def test_real_fixture_transcodes_and_round_trips(self):
        pytest.importorskip("pymarc")
        text = _read("nscc_bad_bib_badescape.mrc")
        results = m.repair_text(text)
        assert results, "expected at least one record"
        parsed = results[0]
        assert parsed.unresolved == []
        assert parsed.leader[9] != "a"  # fixture is genuinely MARC-8
        m.transcode_marc8_to_utf8(parsed)
        assert parsed.leader[9] == "a"
        raw = m.assemble_marc(parsed)
        raw.decode("utf-8")  # must not raise -- proves it's valid UTF-8 now


class TestRecordIdentifier:
    def test_prefers_sierra_907a_over_001(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="u476474"),
                m.Field_("907", "  ", [("a", ".b12345678")]),
            ],
        )
        assert m.record_identifier(parsed) == ".b12345678"

    def test_recognizes_sierra_number_without_leading_period(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="u476474"),
                m.Field_("907", "  ", [("a", "b12345678")]),
            ],
        )
        assert m.record_identifier(parsed) == "b12345678"

    def test_uses_001_when_907a_is_not_a_sierra_number(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="u476474"),
                m.Field_("907", "  ", [("a", "some-other-id")]),
            ],
        )
        assert m.record_identifier(parsed) == "u476474"

    def test_uses_001_when_no_907_field(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("001", None, None, content="u476474")],
        )
        assert m.record_identifier(parsed) == "u476474"

    def test_falls_back_to_non_sierra_907a_when_no_001(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("907", "  ", [("a", "some-other-id")])],
        )
        assert m.record_identifier(parsed) == "some-other-id"

    def test_empty_string_when_neither_present(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        assert m.record_identifier(parsed) == ""


# ---------------------------------------------------------------------------
# strip_missing_required_a -- remove fields lacking a required $a
# ---------------------------------------------------------------------------

class TestStripMissingRequiredA:
    def test_default_tag_list_excludes_505_and_260(self):
        tags = m.load_required_a_tags(m.DEFAULT_REQUIRED_A_TAGS_FILE)
        assert "505" not in tags
        assert "260" not in tags
        assert "264" not in tags
        assert "650" in tags
        assert "100" in tags

    def test_field_missing_a_with_content_is_removed_and_logged(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("z", "United States.")])],
        )
        details = m.strip_missing_required_a(parsed, {"650"})
        assert parsed.fields == []
        assert len(details) == 1
        assert "650" in details[0]
        assert "United States." in details[0]

    def test_field_entirely_empty_is_removed_silently(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", "  ", [("a", "")])],
        )
        details = m.strip_missing_required_a(parsed, {"650"})
        assert parsed.fields == []
        assert details == []

    def test_field_with_nonempty_a_is_kept(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Suicide")])],
        )
        details = m.strip_missing_required_a(parsed, {"650"})
        assert len(parsed.fields) == 1
        assert details == []

    def test_tag_not_in_list_is_left_alone_even_without_a(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("505", "0 ", [("t", "Some title --")])],
        )
        details = m.strip_missing_required_a(parsed, {"650"})
        assert len(parsed.fields) == 1
        assert details == []

    def test_real_fixture_505_and_260_survive(self):
        text = _read("bad_bib_mandatoryfieldsnashvillestate_bibs_202693_me.mrc")
        results = m.repair_text(text)
        required_a_tags = m.load_required_a_tags(m.DEFAULT_REQUIRED_A_TAGS_FILE)
        for parsed in results:
            m.strip_missing_required_a(parsed, required_a_tags)
        all_tags = {f.tag for parsed in results for f in parsed.fields}
        # at least one 505/260 existed in the raw fixture and neither tag
        # should have been wiped out entirely by the required-$a pass
        assert "505" in all_tags or "260" in all_tags


# ---------------------------------------------------------------------------
# find_suspicious_fields -- doubled-URL heuristic
# ---------------------------------------------------------------------------

class TestFindSuspiciousFields:
    def test_flags_literally_doubled_proxy_prefix(self):
        parsed = m.ParsedRecord(
            leader=_VALID_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_(
                    "856",
                    "4 ",
                    [(
                        "u",
                        "https://ezproxy.example.edu/login?url="
                        "https://ezproxy.example.edu/login?url="
                        "http://vendor.example.com/book/123",
                    )],
                ),
            ],
        )
        warnings = m.find_suspicious_fields(parsed)
        assert len(warnings) == 1

    def test_does_not_flag_normal_single_hop_proxy_url(self):
        parsed = m.ParsedRecord(
            leader=_VALID_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_(
                    "856",
                    "4 ",
                    [(
                        "u",
                        "https://ezproxy.example.edu/login?url="
                        "http://vendor.example.com/book/123",
                    )],
                ),
            ],
        )
        assert m.find_suspicious_fields(parsed) == []

    def test_flags_non_numeric_tag(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("24A", "00", [("a", "Odd tag")]),
            ],
        )
        warnings = m.find_suspicious_fields(parsed)
        assert any(cat == "non_numeric_tag" and "24A" in detail for cat, detail in warnings)

    def test_flags_invalid_subfield_code(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("A", "Uppercase code")]),
            ],
        )
        warnings = m.find_suspicious_fields(parsed)
        assert any(cat == "invalid_subfield_code" and "'A'" in detail for cat, detail in warnings)

    def test_flags_missing_008(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        warnings = m.find_suspicious_fields(parsed)
        assert any(cat == "missing_008" for cat, detail in warnings)


class TestFixInvalidLeaderBytes:
    def _record(self, leader):
        return m.ParsedRecord(
            leader=leader,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )

    def test_valid_leader_is_untouched(self):
        parsed = self._record(_VALID_LEADER)
        details = m.fix_invalid_leader_bytes(parsed)
        assert details == []
        assert parsed.leader == _VALID_LEADER

    def test_defaults_invalid_record_status_to_c(self):
        leader = _VALID_LEADER[:5] + "0" + _VALID_LEADER[6:]
        parsed = self._record(leader)
        details = m.fix_invalid_leader_bytes(parsed)
        assert len(details) == 1
        assert parsed.leader[5] == "c"

    def test_defaults_invalid_type_of_record_to_a(self):
        leader = _VALID_LEADER[:6] + "9" + _VALID_LEADER[7:]
        parsed = self._record(leader)
        details = m.fix_invalid_leader_bytes(parsed)
        assert len(details) == 1
        assert parsed.leader[6] == "a"

    def test_defaults_invalid_type_of_control_to_blank(self):
        leader = _VALID_LEADER[:8] + "9" + _VALID_LEADER[9:]
        parsed = self._record(leader)
        details = m.fix_invalid_leader_bytes(parsed)
        assert len(details) == 1
        assert parsed.leader[8] == " "

    def test_defaults_invalid_encoding_level_to_blank(self):
        leader = _VALID_LEADER[:17] + "6" + _VALID_LEADER[18:]
        parsed = self._record(leader)
        details = m.fix_invalid_leader_bytes(parsed)
        assert len(details) == 1
        assert parsed.leader[17] == " "

    def test_multiple_invalid_bytes_all_fixed_and_logged(self):
        leader = (
            _VALID_LEADER[:5] + "0" + _VALID_LEADER[6:8] + "9" + _VALID_LEADER[9:17]
            + "6" + _VALID_LEADER[18:]
        )
        parsed = self._record(leader)
        details = m.fix_invalid_leader_bytes(parsed)
        assert len(details) == 3
        assert parsed.leader[5] == "c"
        assert parsed.leader[8] == " "
        assert parsed.leader[17] == " "

    def test_runs_by_default_via_cli(self, tmp_path):
        leader = _VALID_LEADER[:5] + "0" + _VALID_LEADER[6:]
        parsed = m.ParsedRecord(
            leader=leader,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "badleader.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "leader_byte_defaulted" in content
        results = m.repair_text(m._read_text(str(out)))
        assert results[0].leader[5] == "c"

    def test_no_fix_invalid_leader_bytes_flag_skips_it(self, tmp_path):
        leader = _VALID_LEADER[:5] + "0" + _VALID_LEADER[6:]
        parsed = m.ParsedRecord(
            leader=leader,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "badleader.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--no-fix-invalid-leader-bytes", "--log", str(log),
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        assert results[0].leader[5] == "0"


# ---------------------------------------------------------------------------
# strip_invalid_subfield_codes -- remove unusable subfield codes
# ---------------------------------------------------------------------------

class TestStripInvalidSubfieldCodes:
    def test_removes_bad_subfield_keeps_good_ones(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("A", "Bad"), ("a", "Good title.")])],
        )
        log_lines = m.strip_invalid_subfield_codes(parsed)
        assert len(log_lines) == 1
        assert len(parsed.fields) == 1
        assert parsed.fields[0].subfields == [("a", "Good title.")]

    def test_drops_field_with_no_valid_subfields_left(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("Z", "All invalid")])],
        )
        log_lines = m.strip_invalid_subfield_codes(parsed)
        assert len(log_lines) == 1
        assert parsed.fields == []

    def test_control_fields_are_left_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("001", None, None, content="abc123")],
        )
        log_lines = m.strip_invalid_subfield_codes(parsed)
        assert log_lines == []
        assert parsed.fields[0].content == "abc123"

    def test_valid_codes_are_untouched(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Subject"), ("2", "local")])],
        )
        log_lines = m.strip_invalid_subfield_codes(parsed)
        assert log_lines == []
        assert parsed.fields[0].subfields == [("a", "Subject"), ("2", "local")]


# ---------------------------------------------------------------------------
# strip_empty_fields -- remove fields with no non-empty subfields, any tag
# ---------------------------------------------------------------------------

class TestStripEmptyFields:
    def test_removes_field_with_only_empty_subfield(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("020", "  ", [("a", "")]),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )
        m.strip_empty_fields(parsed)
        assert [f.tag for f in parsed.fields] == ["245"]

    def test_keeps_field_with_any_non_empty_subfield(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("020", "  ", [("a", ""), ("z", "cancelled")])],
        )
        m.strip_empty_fields(parsed)
        assert len(parsed.fields) == 1

    def test_control_fields_are_left_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="")],
        )
        m.strip_empty_fields(parsed)
        assert len(parsed.fields) == 1

    def test_not_scoped_to_required_a_tags(self):
        # 020 isn't in required_a_tags.txt, so strip_missing_required_a
        # would leave this alone -- strip_empty_fields still removes it
        # since it holds no data under any tag.
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("020", "  ", [("a", "")])],
        )
        m.strip_empty_fields(parsed)
        assert parsed.fields == []


# ---------------------------------------------------------------------------
# assemble_marc -- oversized-record guard
# ---------------------------------------------------------------------------

class TestLeaderEntryMapCorrection:
    def test_corrupted_entry_map_is_forced_back_to_4500(self):
        # Real defect seen in production data: leader bytes 20-23 (always
        # the fixed constant "4500" in MARC21) get corrupted to something
        # like "45x0". Parsing doesn't depend on it (Mode 1 uses real
        # delimiters, not this byte), but writing must not copy the
        # corruption through uncorrected.
        leader = list(_SYNTHETIC_LEADER)
        leader[20:24] = list("45x0")
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[m.Field_("001", None, None, content="abc123")],
        )
        raw = m.assemble_marc(parsed)
        assert raw[20:24] == b"4500"

    def test_correct_entry_map_is_left_alone(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("001", None, None, content="abc123")],
        )
        raw = m.assemble_marc(parsed)
        assert raw[20:24] == b"4500"

    def test_real_fixture_entry_map_is_corrected_in_output(self):
        text = _read("bad_length_bib_nashvillestate_bibs_202693_me.mrc")
        parsed = m.repair_text(text)[0]
        raw = m.assemble_marc(parsed)
        assert raw[20:24] == b"4500"


# ---------------------------------------------------------------------------
# add_default_245 -- placeholder 245 for records missing one entirely
# ---------------------------------------------------------------------------

class TestAddDefault245:
    def test_inserts_placeholder_when_missing(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        details = m.add_default_245(parsed)
        assert len(details) == 1
        field245 = next(f for f in parsed.fields if f.tag == "245")
        assert field245.indicators == "00"
        assert field245.subfields == [("a", "No title")]

    def test_patches_existing_245_missing_a_keeping_other_subfields(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("h", "[electronic resource]")])],
        )
        details = m.add_default_245(parsed)
        assert len(details) == 1
        assert len([f for f in parsed.fields if f.tag == "245"]) == 1
        field245 = next(f for f in parsed.fields if f.tag == "245")
        assert field245.subfields == [("a", "No title"), ("h", "[electronic resource]")]

    def test_patches_existing_245_with_empty_a(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("a", ""), ("h", "[electronic resource]")])],
        )
        details = m.add_default_245(parsed)
        assert len(details) == 1
        field245 = next(f for f in parsed.fields if f.tag == "245")
        assert field245.subfields == [("a", "No title"), ("h", "[electronic resource]")]

    def test_no_op_when_245_already_present(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Real title.")])],
        )
        details = m.add_default_245(parsed)
        assert details == []
        assert parsed.fields[0].subfields == [("a", "Real title.")]

    def test_runs_by_default_via_cli_and_logged_as_fixed(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "no245.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "=== FIXED: added_default_245 (1) ===" in content
        results = m.repair_text(m._read_text(str(out)))
        field245 = next(f for f in results[0].fields if f.tag == "245")
        assert field245.subfields == [("a", "No title")]

    def test_no_add_default_245_flag_leaves_it_missing(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "no245.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--no-add-default-245", "--log", str(log)])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        assert not any(f.tag == "245" for f in results[0].fields)

    def test_explicit_ensure_field_245_takes_priority(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "no245.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--ensure-field", "245:00:a=Real supplied title.",
            "--log", str(log),
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        field245 = next(f for f in results[0].fields if f.tag == "245")
        assert field245.subfields == [("a", "Real supplied title.")]


# ---------------------------------------------------------------------------
# add_default_008 -- placeholder 008 for records missing one entirely
# ---------------------------------------------------------------------------

class TestAddDefault008:
    def test_inserts_default_when_missing(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        details = m.add_default_008(parsed)
        assert len(details) == 1
        field008 = next(f for f in parsed.fields if f.tag == "008")
        assert field008.content == m.DEFAULT_008_CONTENT

    def test_no_op_when_008_already_present(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        details = m.add_default_008(parsed)
        assert details == []
        assert parsed.fields[0].content == "x" * 40

    def test_runs_by_default_via_cli_and_logged_as_informational(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        src = tmp_path / "no008.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "=== INFORMATIONAL: added_default_008 (1) ===" in content
        assert "missing_008" not in content
        results = m.repair_text(m._read_text(str(out)))
        field008 = next(f for f in results[0].fields if f.tag == "008")
        assert field008.content == m.DEFAULT_008_CONTENT

    def test_no_add_default_008_flag_leaves_it_missing_and_flagged(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        src = tmp_path / "no008.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--no-add-default-008", "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "=== NOT FIXED: missing_008 (1) ===" in content
        results = m.repair_text(m._read_text(str(out)))
        assert not any(f.tag == "008" for f in results[0].fields)

    def test_explicit_ensure_field_008_takes_priority(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        src = tmp_path / "no008.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--ensure-field", "008:realcontent" + "x" * 30,
            "--log", str(log),
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        field008 = next(f for f in results[0].fields if f.tag == "008")
        assert field008.content == "realcontent" + "x" * 30


# ---------------------------------------------------------------------------
# fix_invalid_tags -- non-numeric tags -> an unused 9XX slot
# ---------------------------------------------------------------------------

class TestFixInvalidTags:
    def test_renames_non_numeric_tag(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("24A", "00", [("a", "Odd tag")])],
        )
        details = m.fix_invalid_tags(parsed, "900")
        assert len(details) == 1
        assert parsed.fields[0].tag == "900"
        assert parsed.fields[0].subfields == [("a", "Odd tag")]

    def test_leaves_numeric_tags_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Subject")])],
        )
        details = m.fix_invalid_tags(parsed, "900")
        assert details == []
        assert parsed.fields[0].tag == "650"

    def test_pick_unused_9xx_tag_skips_used_ones(self):
        used = {str(n) for n in range(900, 999)}  # 900-998 used, 999 free
        assert m.pick_unused_9xx_tag(used) == "999"

    def test_pick_unused_9xx_tag_returns_none_when_all_used(self):
        used = {str(n) for n in range(900, 1000)}
        assert m.pick_unused_9xx_tag(used) is None

    def test_runs_by_default_via_cli_picking_an_unused_9xx(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("24A", "00", [("a", "Odd tag")]),
            ],
        )
        src = tmp_path / "badtag.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "=== INFORMATIONAL: invalid_tag (1) ===" in content
        assert "non_numeric_tag" not in content
        results = m.repair_text(m._read_text(str(out)))
        tags = [f.tag for f in results[0].fields]
        assert "24A" not in tags
        assert any(900 <= int(t) <= 999 for t in tags if t.isdigit() and t != "008")

    def test_no_fix_invalid_tags_flag_leaves_it_and_flags_it(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("24A", "00", [("a", "Odd tag")]),
            ],
        )
        src = tmp_path / "badtag.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--no-fix-invalid-tags", "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "=== NOT FIXED: non_numeric_tag (1) ===" in content
        results = m.repair_text(m._read_text(str(out)))
        tags = [f.tag for f in results[0].fields]
        assert "24A" in tags

    def test_replacement_tag_avoids_9xx_used_by_a_later_record(self, tmp_path):
        # The invalid-tag fix is deferred to a second, targeted pass so
        # a single streaming pass suffices -- this confirms the chosen
        # replacement still avoids a 9XX tag used by a record that comes
        # AFTER the invalid-tag record in the file, not just ones before it.
        rec1 = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("24A", "00", [("a", "Odd tag")]),
            ],
        )
        rec2 = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("900", "  ", [("a", "Local data")]),
            ],
        )
        src = tmp_path / "two.mrc"
        src.write_bytes(m.assemble_marc(rec1) + m.assemble_marc(rec2))
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out)])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        fixed_tag = next(f.tag for f in results[0].fields if f.tag not in ("008", "245"))
        assert fixed_tag != "900"
        # the second record's real 900 field must survive untouched
        field900 = next(f for f in results[1].fields if f.tag == "900")
        assert field900.subfields == [("a", "Local data")]

    def test_falls_back_to_flagging_when_every_9xx_slot_is_taken(self, tmp_path):
        fields = [m.Field_("008", None, None, content="x" * 40),
                  m.Field_("245", "00", [("a", "Title.")])]
        for n in range(900, 1000):
            fields.append(m.Field_(str(n), "  ", [("a", "filler")]))
        rec1 = m.ParsedRecord(leader=_SYNTHETIC_LEADER, entries=[], fields=fields)
        rec2 = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="abc123"),
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("24A", "00", [("a", "Odd tag")]),
            ],
        )
        src = tmp_path / "full9xx.mrc"
        src.write_bytes(m.assemble_marc(rec1) + m.assemble_marc(rec2))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        # --no-remap-999-to-945: without it, the default 999->945 remap
        # would retag the filler "999" field, freeing a slot and
        # defeating this test's "every 9XX slot taken" setup.
        rc = m.main([
            str(src), "-o", str(out), "--no-remap-999-to-945", "--log", str(log),
        ])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "=== NOT FIXED: non_numeric_tag (1) ===" in content
        assert "could not fix" in content
        results = m.repair_text(m._read_text(str(out)))
        assert any(f.tag == "24A" for f in results[1].fields)


# ---------------------------------------------------------------------------
# remap_999_to_945 -- Sierra's internal 999 -> locally-defined 945
# ---------------------------------------------------------------------------

class TestRemap999To945:
    def test_retags_999_to_945_with_ff_indicators(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("999", "  ", [("i", "12345"), ("l", "MAIN")])],
        )
        details = m.remap_999_to_945(parsed)
        assert len(details) == 1
        assert parsed.fields[0].tag == "945"
        assert parsed.fields[0].indicators == "ff"
        assert parsed.fields[0].subfields == [("i", "12345"), ("l", "MAIN")]

    def test_control_fields_are_left_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        details = m.remap_999_to_945(parsed)
        assert details == []
        assert parsed.fields[0].tag == "008"

    def test_no_op_when_no_999_present(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Subject")])],
        )
        details = m.remap_999_to_945(parsed)
        assert details == []
        assert parsed.fields[0].tag == "650"

    def test_runs_by_default_via_cli_but_is_not_logged(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("999", "  ", [("i", "12345"), ("l", "MAIN")]),
            ],
        )
        src = tmp_path / "s999.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "remapped_999_to_945" not in content
        results = m.repair_text(m._read_text(str(out)))
        tags = [f.tag for f in results[0].fields]
        assert "999" not in tags
        field = next(f for f in results[0].fields if f.tag == "945")
        assert field.indicators == "ff"
        assert field.subfields == [("i", "12345"), ("l", "MAIN")]

    def test_log_999_to_945_flag_enables_logging(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("999", "  ", [("i", "12345"), ("l", "MAIN")]),
            ],
        )
        src = tmp_path / "s999.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log-999-to-945", "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "remapped_999_to_945" in content

    def test_no_remap_999_to_945_flag_skips_it(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("999", "  ", [("i", "12345")]),
            ],
        )
        src = tmp_path / "s999.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--no-remap-999-to-945", "--log", str(log),
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        tags = [f.tag for f in results[0].fields]
        assert "999" in tags
        assert "945" not in tags


# ---------------------------------------------------------------------------
# normalize_subfield_9_to_0 -- $9 -> $0
# ---------------------------------------------------------------------------

class TestNormalizeSubfield9To0:
    def test_rewrites_9_to_0(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Subject"), ("9", "123456")])],
        )
        details = m.normalize_subfield_9_to_0(parsed)
        assert len(details) == 1
        assert parsed.fields[0].subfields == [("a", "Subject"), ("0", "123456")]

    def test_control_fields_are_left_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        details = m.normalize_subfield_9_to_0(parsed)
        assert details == []

    def test_no_op_when_no_9_present(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Subject")])],
        )
        details = m.normalize_subfield_9_to_0(parsed)
        assert details == []
        assert parsed.fields[0].subfields == [("a", "Subject")]

    def test_runs_by_default_via_cli(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("650", " 0", [("a", "Subject"), ("9", "123456")]),
            ],
        )
        src = tmp_path / "sub9.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "normalized_subfield_9_to_0" in content
        results = m.repair_text(m._read_text(str(out)))
        field = next(f for f in results[0].fields if f.tag == "650")
        assert field.subfields == [("a", "Subject"), ("0", "123456")]

    def test_no_normalize_subfield_9_flag_skips_it(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("650", " 0", [("a", "Subject"), ("9", "123456")]),
            ],
        )
        src = tmp_path / "sub9.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--no-normalize-subfield-9", "--log", str(log),
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        field = next(f for f in results[0].fields if f.tag == "650")
        assert field.subfields == [("a", "Subject"), ("9", "123456")]


# ---------------------------------------------------------------------------
# normalize_smart_characters -- typographic Unicode -> plain ASCII
# ---------------------------------------------------------------------------

class TestNormalizeSmartCharacters:
    def test_replaces_curly_quotes_and_em_dash(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("520", "  ", [("a", "It’s “great”—really.")])],
        )
        details = m.normalize_smart_characters(parsed)
        assert len(details) == 1
        assert parsed.fields[0].subfields == [("a", "It's \"great\"--really.")]

    def test_leaves_plain_ascii_untouched(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("520", "  ", [("a", "Nothing fancy here.")])],
        )
        details = m.normalize_smart_characters(parsed)
        assert details == []
        assert parsed.fields[0].subfields == [("a", "Nothing fancy here.")]

    def test_control_fields_are_normalized_too(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("500", None, None, content="odd’ content")],
        )
        details = m.normalize_smart_characters(parsed)
        assert len(details) == 1
        assert parsed.fields[0].content == "odd' content"

    def test_nonbreaking_space_and_soft_hyphen(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("520", "  ", [("a", "a b­c")])],
        )
        m.normalize_smart_characters(parsed)
        assert parsed.fields[0].subfields == [("a", "a bc")]

    def test_main_end_to_end_logs_as_fixed(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("520", "  ", [("a", "It’s great.")]),
            ],
        )
        src = tmp_path / "smart.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = log.read_text(encoding="utf-8")
        assert "normalized_smart_characters" in content
        results = m.repair_text(m._read_text(str(out)))
        title_field = next(f for f in results[0].fields if f.tag == "520")
        assert title_field.subfields == [("a", "It's great.")]

    def test_no_normalize_smart_characters_flag_skips_it(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("520", "  ", [("a", "It’s great.")]),
            ],
        )
        src = tmp_path / "smart.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--no-normalize-smart-characters", "--log", str(log),
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        title_field = next(f for f in results[0].fields if f.tag == "520")
        assert title_field.subfields == [("a", "It’s great.")]


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
        content = log.read_text(encoding="utf-8")
        assert "duplicate_identifier" in content
        assert content.count("dup1") >= 2


class TestWriteLog:
    def test_groups_by_fixed_then_category_with_headers(self, tmp_path):
        entries = [
            m.LogEntry("missing_008", False, "t1", 0, "u1", "no 008"),
            m.LogEntry("added_field", True, "t2", 0, "u1", "added 245"),
            m.LogEntry("missing_008", False, "t3", 1, "u2", "no 008 either"),
            m.LogEntry("removed_missing_a", True, "t4", 1, "u2", "removed 650"),
        ]
        log_path = tmp_path / "run.log"
        m.write_log(str(log_path), entries)
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = next(i for i, ln in enumerate(lines) if "NOT FIXED: missing_008" in ln)
        fixed_added_header = next(i for i, ln in enumerate(lines) if "FIXED: added_field" in ln)
        fixed_removed_header = next(
            i for i, ln in enumerate(lines) if "FIXED: removed_missing_a" in ln
        )
        assert "(2)" in lines[not_fixed_header]
        assert not_fixed_header < fixed_added_header
        assert not_fixed_header < fixed_removed_header
        # the two missing_008 entries are adjacent, not interleaved with
        # the unrelated fixed entries
        missing_008_lines = [ln for ln in lines if "no 008" in ln]
        assert len(missing_008_lines) == 2

    def test_appends_rather_than_overwrites(self, tmp_path):
        log_path = tmp_path / "run.log"
        m.write_log(str(log_path), [m.LogEntry("cat_a", True, "t1", 0, "", "first")])
        m.write_log(str(log_path), [m.LogEntry("cat_b", True, "t2", 0, "", "second")])
        content = log_path.read_text(encoding="utf-8")
        assert "first" in content
        assert "second" in content

    def test_duplicate_records_section_sits_between_not_fixed_and_fixed(self, tmp_path):
        entries = [
            m.LogEntry("added_field", True, "t1", 0, "u1", "added 245"),
            m.LogEntry("missing_008", False, "t2", 1, "u2", "no 008"),
            m.LogEntry("duplicate_identifier", False, "t3", 2, "u3", "dup"),
        ]
        log_path = tmp_path / "run.log"
        m.write_log(str(log_path), entries)
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        dup_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== DUPLICATE"))
        fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== FIXED"))
        assert not_fixed_header < dup_header < fixed_header
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
        m.write_log(str(log_path), entries)
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        dup_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== DUPLICATE"))
        fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== FIXED"))
        informational_headers = [
            i for i, ln in enumerate(lines) if ln.startswith("=== INFORMATIONAL")
        ]
        assert len(informational_headers) == 4
        assert not_fixed_header < dup_header < fixed_header < min(informational_headers)
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
        header_indices = [i for i, ln in enumerate(lines) if ln.startswith("===")]
        assert len(header_indices) == 2
        # first header has no blank line before it -- it's the first line
        assert header_indices[0] == 0
        # second header is preceded by a blank line
        assert lines[header_indices[1] - 1] == ""


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
    def test_default_output_path_appends_fixed_before_extension(self):
        assert m._default_output_path("/tmp/foo.mrc") == "/tmp/foo_fixed.mrc"
        assert m._default_output_path("/tmp/foo") == "/tmp/foo_fixed"

    def test_main_writes_fixed_file_next_to_input(self, tmp_path):
        src = tmp_path / "bad_length.mrc"
        src.write_bytes(
            _read("bad_length_bib_nashvillestate_bibs_202693_me.mrc").encode("utf-8")
        )
        rc = m.main([str(src)])
        assert rc == 0
        expected_out = tmp_path / "bad_length_fixed.mrc"
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
        content = log.read_text(encoding="utf-8")
        assert "removed_missing_a" not in content
        assert "added_default_245" in content
