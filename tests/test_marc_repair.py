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
            str(src), "-o", str(out), "--no-strip-invalid-subfield-codes",
            "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        lines = _resolve_log(log).read_text(encoding="utf-8").splitlines()
        not_fixed_idx = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        info_idx = next(i for i, ln in enumerate(lines) if ln.startswith("=== INFORMATIONAL"))
        assert not_fixed_idx < info_idx, "NOT FIXED block must come before INFORMATIONAL block"
        assert any("[INFORMATIONAL]" in ln and "padded" in ln for ln in lines[info_idx:])
        assert any("[NOT FIXED]" in ln for ln in lines[not_fixed_idx:info_idx])


# ---------------------------------------------------------------------------
# transcode_marc8_to_utf8 -- ANSEL diacritics -> Unicode
# ---------------------------------------------------------------------------

class TestFindSuspectMarc8Escapes:
    def _record(self, raw_a):
        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "  # declare MARC-8
        return m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[m.Field_("880", "10", [("a", raw_a)])],
        )

    def test_flags_single_cjk_char_welded_to_ascii_letters(self):
        # Real production example: "Schr" + <CJK escape, 1 char> +
        # "inger" -- a miskeyed "ö" in "Schrödinger", not real Chinese.
        parsed = self._record("Schr\x1b$1)36\x1b(Binger")
        findings = m.find_suspect_marc8_escapes(parsed)
        assert len(findings) == 1
        category, detail = findings[0]
        assert category == "suspect_marc8_escape"
        assert "CJK" in detail
        assert "suggested fix: likely a miskeyed accented letter" in detail
        assert "Schr[?]inger" in detail

    def test_flags_single_cyrillic_char_welded_to_ascii_letters(self):
        # Real production example: "who" + <Cyrillic escape, 1 char> +
        # "s ever" -- a miskeyed apostrophe in "who's".
        parsed = self._record("who\x1b(QS\x1b(Bs ever")
        findings = m.find_suspect_marc8_escapes(parsed)
        assert len(findings) == 1
        detail = findings[0][1]
        assert "Cyrillic" in detail
        assert "suggested fix: likely a miskeyed apostrophe" in detail
        assert "who's" in detail

    def test_does_not_flag_genuine_multi_character_cjk(self):
        # Real production example: a genuine parallel Chinese title,
        # several characters long -- not a mistake.
        parsed = self._record("\x1b$1!04!7o!V1KWF\x1b(B")
        assert m.find_suspect_marc8_escapes(parsed) == []

    def test_does_not_flag_when_not_welded_to_letters(self):
        # Surrounded by spaces/punctuation, not letters -- looks like
        # deliberately embedded content, not a stray mis-keyed escape.
        parsed = self._record("see also \x1b(2k\x1b(B (in Hebrew)")
        assert m.find_suspect_marc8_escapes(parsed) == []

    def test_no_op_when_already_unicode(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,  # leader[9] == "a" already
            entries=[],
            fields=[m.Field_("880", "10", [("a", "Schr\x1b$1)36\x1b(Binger")])],
        )
        assert m.find_suspect_marc8_escapes(parsed) == []


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

    def test_charset_switching_escape_is_honored_not_passed_through(self):
        # Real bug found via a real Hebrew 880 field in production data:
        # pymarc's marc8_to_unicode detects charset-switching escapes
        # (Hebrew, Arabic, Cyrillic, Greek, CJK/EACC, super/subscripts)
        # by comparing byte slices against byte literals, which is
        # always False when given a Python str -- so passing `text`
        # directly (instead of `text.encode("latin-1")`, which recovers
        # the exact original MARC-8 bytes) silently skipped ALL escape
        # recognition. Plain ANSEL diacritics (no escape needed) still
        # worked, which is why this wasn't caught by the fixture-based
        # test above. ESC ( 2 designates Basic Hebrew.
        pytest.importorskip("pymarc")
        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "  # declare MARC-8
        hebrew_marc8 = "\x1b(2kz`a `lgbd\x1b(B"
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[
                m.Field_("001", None, None, content="u508261"),
                m.Field_("880", "10", [("6", "240-01/(2/r"), ("a", hebrew_marc8)]),
            ],
        )
        m.transcode_marc8_to_utf8(parsed)
        field880 = next(f for f in parsed.fields if f.tag == "880")
        converted = dict(field880.subfields)["a"]
        assert converted == "כתאב אלחגה"
        assert "\x1b" not in converted  # no leftover raw escape byte

    def test_runs_by_default_via_cli(self, tmp_path):
        pytest.importorskip("pymarc")
        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("100", "1 ", [("a", "Bal\xe5asim, \xf2Hasan.")]),
            ],
        )
        src = tmp_path / "marc8.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        # transcoding itself runs by default, but isn't logged
        # per-record unless --log-transcoded-marc8 (and --log-informational)
        # are also given -- with no other loggable entries, no log file
        # is written at all
        assert not _resolve_log(log).exists()
        results = m.repair_text(m._read_text(str(out)))
        assert results[0].leader[9] == "a"

    def test_log_transcoded_marc8_flag_enables_logging(self, tmp_path):
        pytest.importorskip("pymarc")
        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("100", "1 ", [("a", "Bal\xe5asim, \xf2Hasan.")]),
            ],
        )
        src = tmp_path / "marc8.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--log-transcoded-marc8",
            "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "transcoded_marc8" in content

    def test_no_transcode_marc8_flag_leaves_it_as_marc8(self, tmp_path):
        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("100", "1 ", [("a", "Bal\xe5asim, \xf2Hasan.")]),
            ],
        )
        src = tmp_path / "marc8.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out), "--no-transcode-marc8"])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        assert results[0].leader[9] == " "

    def test_atomic_on_failure_partway_through_record(self, monkeypatch):
        # A field partway through a record failing to convert must not
        # leave the record in a mixed state (some fields converted,
        # some not, leader never flipped) -- transcode_marc8_to_utf8
        # computes every field's new value before mutating anything, so
        # a failure on the second field must leave the first field's
        # original (unconverted) value in place too, and the leader
        # untouched.
        pytest.importorskip("pymarc")
        import pymarc.marc8

        calls = []

        def fake_marc8_to_unicode(data, hide_utf8_warnings=False):
            calls.append(data)
            if len(calls) == 2:
                raise UnicodeDecodeError("marc8_to_unicode", data, 0, len(data), "boom")
            return "CONVERTED"

        monkeypatch.setattr(pymarc.marc8, "marc8_to_unicode", fake_marc8_to_unicode)

        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            # non-ASCII (\xe5) so these hit the slow path this test is
            # exercising, rather than the ASCII/no-escape fast path
            fields=[
                m.Field_("100", "1 ", [("a", "F\xe5rst")]),
                m.Field_("245", "00", [("a", "S\xe5cond")]),
            ],
        )
        with pytest.raises(UnicodeDecodeError):
            m.transcode_marc8_to_utf8(parsed)
        # nothing changed -- not even the first field, which would have
        # "succeeded" if fields were converted one at a time
        assert parsed.fields[0].subfields == [("a", "F\xe5rst")]
        assert parsed.fields[1].subfields == [("a", "S\xe5cond")]
        assert parsed.leader[9] == " "

    def test_main_falls_back_gracefully_on_transcode_failure(self, tmp_path, monkeypatch):
        pytest.importorskip("pymarc")
        import pymarc.marc8

        def failing_marc8_to_unicode(data, hide_utf8_warnings=False):
            raise UnicodeDecodeError("marc8_to_unicode", data, 0, len(data), "boom")

        monkeypatch.setattr(pymarc.marc8, "marc8_to_unicode", failing_marc8_to_unicode)

        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            # non-ASCII (\xe5) so this hits the slow path being tested
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Titl\xe5.")]),
            ],
        )
        src = tmp_path / "bad_marc8.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0  # must not crash the whole run
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== NOT FIXED: transcode_marc8_failed (1) ===" in content
        results = m.repair_text(m._read_text(str(out)))
        assert results[0].leader[9] == " "  # left declaring MARC-8

    def test_missing_pymarc_fails_the_run_by_default(self, tmp_path, monkeypatch):
        # UTF-8 output is a hard requirement -- silently skipping
        # transcoding because pymarc isn't installed would leave
        # non-conformant output without the user ever choosing that,
        # so this must fail loudly instead of just warning and
        # continuing.
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pymarc.marc8" or name == "pymarc":
                raise ImportError("simulated missing pymarc")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "marc8.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out)])
        assert rc != 0
        assert not out.exists()

    def test_missing_pymarc_is_fine_with_explicit_no_transcode(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pymarc.marc8" or name == "pymarc":
                raise ImportError("simulated missing pymarc")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "marc8.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out), "--no-transcode-marc8"])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        assert results[0].leader[9] == " "  # left as MARC-8, explicitly requested


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
        tags = m.load_tag_list(m.DEFAULT_REQUIRED_A_TAGS_FILE)
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

    def test_field_with_punctuation_only_a_is_removed_and_logged(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "--"), ("z", "United States.")])],
        )
        details = m.strip_missing_required_a(parsed, {"650"})
        assert parsed.fields == []
        assert len(details) == 1
        assert "650" in details[0]
        assert "punctuation only" in details[0]

    def test_field_with_only_punctuation_a_and_nothing_else_is_removed_and_logged(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", ".")])],
        )
        details = m.strip_missing_required_a(parsed, {"650"})
        assert parsed.fields == []
        assert len(details) == 1
        assert "punctuation only" in details[0]

    def test_real_fixture_505_and_260_survive(self):
        text = _read("bad_bib_mandatoryfieldsnashvillestate_bibs_202693_me.mrc")
        results = m.repair_text(text)
        required_a_tags = m.load_tag_list(m.DEFAULT_REQUIRED_A_TAGS_FILE)
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
        rc = m.main([str(src), "-o", str(out), "--log-informational", "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
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

    def _corrupted_entry_map_bytes(self):
        # assemble_marc always force-corrects bytes 20-23 itself, so
        # building the test's *input* file with it would immediately
        # "fix" the very corruption being injected -- splice it into
        # the raw bytes afterward instead, bypassing that correction.
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        raw = bytearray(m.assemble_marc(parsed))
        raw[20:24] = b"45x0"
        return bytes(raw)

    def test_fix_runs_by_default_but_not_logged_unless_flagged(self, tmp_path):
        src = tmp_path / "badmap.mrc"
        src.write_bytes(self._corrupted_entry_map_bytes())
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        resolved_log = _resolve_log(log)
        content = resolved_log.read_text(encoding="utf-8") if resolved_log.exists() else ""
        assert "leader_entry_map_fixed" not in content
        assert m.assemble_marc(m.repair_text(m._read_text(str(out)))[0])[20:24] == b"4500"

    def test_log_leader_entry_map_fixed_flag_enables_logging(self, tmp_path):
        src = tmp_path / "badmap.mrc"
        src.write_bytes(self._corrupted_entry_map_bytes())
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--log-leader-entry-map-fixed",
            "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "leader_entry_map_fixed" in content


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

    def test_runs_by_default_via_cli_and_logged_as_informational(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        src = tmp_path / "no245.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log-informational", "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== INFORMATIONAL: added_default_245 (1) ===" in content
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
        rc = m.main([str(src), "-o", str(out), "--log-informational", "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
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
        content = _resolve_log(log).read_text(encoding="utf-8")
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
            str(src), "-o", str(out), "--ensure-field", "008:realcontent" + "x" * 29,
            "--log", str(log),
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        field008 = next(f for f in results[0].fields if f.tag == "008")
        assert field008.content == "realcontent" + "x" * 29


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
        rc = m.main([str(src), "-o", str(out), "--log-informational", "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
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
        content = _resolve_log(log).read_text(encoding="utf-8")
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
        # 999->945 remapping is off by default (see TestRemap999To945),
        # so the filler "999" field here keeps its slot taken without
        # needing any extra flag, satisfying this test's "every 9XX
        # slot taken" setup.
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
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

    def test_off_by_default_via_cli(self, tmp_path):
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
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        tags = [f.tag for f in results[0].fields]
        assert "999" in tags
        assert "945" not in tags

    def test_remap_999_to_945_flag_enables_it(self, tmp_path):
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
        rc = m.main([str(src), "-o", str(out), "--remap-999-to-945", "--log", str(log)])
        assert rc == 0
        assert not _resolve_log(log).exists()  # runs, but not logged unless --log-999-to-945 too
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
        rc = m.main([
            str(src), "-o", str(out), "--remap-999-to-945", "--log-999-to-945",
            "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "remapped_999_to_945" in content


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
        rc = m.main([str(src), "-o", str(out), "--log-informational", "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
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

    def test_runs_by_default_via_cli_but_is_not_logged(self, tmp_path):
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
        # normalization itself runs by default, but isn't logged
        # per-record unless --log-normalized-smart-characters (and
        # --log-informational) are also given -- with no other loggable
        # entries, no log file is written at all
        assert not _resolve_log(log).exists()
        results = m.repair_text(m._read_text(str(out)))
        title_field = next(f for f in results[0].fields if f.tag == "520")
        assert title_field.subfields == [("a", "It's great.")]

    def test_log_normalized_smart_characters_flag_enables_logging(self, tmp_path):
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
            str(src), "-o", str(out), "--log-normalized-smart-characters",
            "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# find_and_fix_mojibake -- double-encoded UTF-8
# ---------------------------------------------------------------------------

class TestFindAndFixMojibake:
    def _record(self, data, leader9="a"):
        leader = list(_SYNTHETIC_LEADER)
        leader[9] = leader9
        return m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[m.Field_("500", "  ", [("a", data)])],
        )

    def test_fixes_double_encoded_copyright_symbol(self):
        # "©2024" (U+00A9, U+00E9 doesn't apply here) double-encoded:
        # © is UTF-8 bytes 0xC2 0xA9, mis-read as cp1252 gives "Â©"
        parsed = self._record("Â©2024")
        details = m.find_and_fix_mojibake(parsed)
        assert len(details) == 1
        assert parsed.fields[0].subfields == [("a", "©2024")]

    def test_fixes_double_encoded_eszett(self):
        # "Großbritannien" -- ß is UTF-8 bytes 0xC3 0x9F; mis-read as
        # cp1252 (not strict latin-1, where 0x9F is an unprintable
        # control code) gives "GroÃŸbritannien" with a capital Y-with-
        # diaeresis standing in for ß. This is why cp1252, not latin-1,
        # is used for the reverse re-encode in _fix_mojibake.
        parsed = self._record("GroÃŸbritannien")
        details = m.find_and_fix_mojibake(parsed)
        assert len(details) == 1
        assert parsed.fields[0].subfields == [("a", "Großbritannien")]

    def test_leaves_normal_text_untouched(self):
        parsed = self._record("Ordinary title with no issues.")
        assert m.find_and_fix_mojibake(parsed) == []

    def test_skips_records_still_declaring_marc8(self):
        # the marker characters can legitimately appear as raw ANSEL
        # byte values in real MARC-8 -- must not be "fixed" there
        parsed = self._record("GroÃŸbritannien", leader9=" ")
        assert m.find_and_fix_mojibake(parsed) == []
        assert parsed.fields[0].subfields == [("a", "GroÃŸbritannien")]

    def test_control_field_mojibake(self):
        leader = list(_SYNTHETIC_LEADER)
        parsed = m.ParsedRecord(
            leader="".join(leader),
            entries=[],
            fields=[m.Field_("500", None, None, content="GroÃŸbritannien")],
        )
        details = m.find_and_fix_mojibake(parsed)
        assert len(details) == 1
        assert parsed.fields[0].content == "Großbritannien"

    def test_runs_by_default_via_cli(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("500", "  ", [("a", "GroÃŸbritannien")]),
            ],
        )
        src = tmp_path / "moji.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log-informational", "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== INFORMATIONAL: fixed_mojibake (1) ===" in content
        results = m.repair_text(m._read_text(str(out)))
        field = next(f for f in results[0].fields if f.tag == "500")
        assert field.subfields == [("a", "Großbritannien")]

    def test_no_fix_mojibake_flag_skips_it(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("500", "  ", [("a", "GroÃŸbritannien")]),
            ],
        )
        src = tmp_path / "moji.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out), "--no-fix-mojibake"])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        field = next(f for f in results[0].fields if f.tag == "500")
        assert field.subfields == [("a", "GroÃŸbritannien")]


# ---------------------------------------------------------------------------
# strip_duplicate_non_repeatable_fields
# ---------------------------------------------------------------------------

class TestStripDuplicateNonRepeatableFields:
    def test_removes_second_245_keeps_first(self):
        # real production example: record u52599, a second "245"
        # containing only an edition statement (should have been 250)
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("245", "14", [("a", "The geology of Nashville /")]),
                m.Field_("245", "  ", [("a", "2nd ed.")]),
            ],
        )
        details = m.strip_duplicate_non_repeatable_fields(parsed, {"245"})
        assert len(details) == 1
        assert "2nd ed." in details[0]
        remaining = [f for f in parsed.fields if f.tag == "245"]
        assert len(remaining) == 1
        assert remaining[0].subfields == [("a", "The geology of Nashville /")]

    def test_leaves_single_occurrence_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        assert m.strip_duplicate_non_repeatable_fields(parsed, {"245"}) == []
        assert len(parsed.fields) == 1

    def test_leaves_tags_not_in_the_list_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("650", " 0", [("a", "Subject one")]),
                m.Field_("650", " 0", [("a", "Subject two")]),
            ],
        )
        assert m.strip_duplicate_non_repeatable_fields(parsed, {"245"}) == []
        assert len(parsed.fields) == 2

    def test_sirsi_duplicate_001_keeps_the_one_starting_with_u(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="ocm12345678"),
                m.Field_("003", None, None, content="SIRSI"),
                m.Field_("001", None, None, content="u508261"),
            ],
        )
        details = m.strip_duplicate_non_repeatable_fields(parsed, {"001"})
        assert len(details) == 1
        assert "ocm12345678" in details[0]
        remaining = [f for f in parsed.fields if f.tag == "001"]
        assert len(remaining) == 1
        assert remaining[0].content == "u508261"

    def test_sirsi_check_is_case_insensitive(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="ocm12345678"),
                m.Field_("003", None, None, content="sirsi"),
                m.Field_("001", None, None, content="U508261"),
            ],
        )
        m.strip_duplicate_non_repeatable_fields(parsed, {"001"})
        remaining = [f for f in parsed.fields if f.tag == "001"]
        assert remaining[0].content == "U508261"

    def test_non_sirsi_duplicate_001_keeps_first_as_usual(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="ocm12345678"),
                m.Field_("003", None, None, content="OCoLC"),
                m.Field_("001", None, None, content="u508261"),
            ],
        )
        m.strip_duplicate_non_repeatable_fields(parsed, {"001"})
        remaining = [f for f in parsed.fields if f.tag == "001"]
        assert remaining[0].content == "ocm12345678"

    def test_duplicate_005_keeps_most_recent(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("005", None, None, content="20200101120000.0"),
                m.Field_("005", None, None, content="20240615093000.0"),
            ],
        )
        details = m.strip_duplicate_non_repeatable_fields(parsed, {"005"})
        assert len(details) == 1
        assert "20200101120000.0" in details[0]
        remaining = [f for f in parsed.fields if f.tag == "005"]
        assert remaining[0].content == "20240615093000.0"

    def test_duplicate_005_tie_keeps_first(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("005", None, None, content="20240615093000.0"),
                m.Field_("005", None, None, content="20240615093000.0"),
            ],
        )
        m.strip_duplicate_non_repeatable_fields(parsed, {"005"})
        remaining = [f for f in parsed.fields if f.tag == "005"]
        assert len(remaining) == 1

    def test_duplicate_008_keeps_most_recent_by_date_entered(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="000101s2000    xxu           000 0 eng d"),
                m.Field_("008", None, None, content="240615s2024    xxu           000 0 eng d"),
            ],
        )
        details = m.strip_duplicate_non_repeatable_fields(parsed, {"008"})
        assert len(details) == 1
        remaining = [f for f in parsed.fields if f.tag == "008"]
        assert remaining[0].content.startswith("240615")

    def test_duplicate_008_tie_keeps_first(self):
        content = "240615s2024    xxu           000 0 eng d"
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("008", None, None, content=content),
                m.Field_("008", None, None, content=content),
            ],
        )
        m.strip_duplicate_non_repeatable_fields(parsed, {"008"})
        remaining = [f for f in parsed.fields if f.tag == "008"]
        assert len(remaining) == 1

    def test_sirsi_but_none_start_with_u_falls_back_to_first(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="ocm12345678"),
                m.Field_("003", None, None, content="SIRSI"),
                m.Field_("001", None, None, content="ocm99999999"),
            ],
        )
        m.strip_duplicate_non_repeatable_fields(parsed, {"001"})
        remaining = [f for f in parsed.fields if f.tag == "001"]
        assert remaining[0].content == "ocm12345678"

    def test_runs_by_default_via_cli_logged_as_fixed_requires_attention(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "14", [("a", "Real title /")]),
                m.Field_("245", "  ", [("a", "2nd ed.")]),
            ],
        )
        src = tmp_path / "dup245.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== FIXED/REQUIRES ATTENTION: removed_non_repeatable_duplicate (1) ===" in content
        assert "2nd ed." in content
        results = m.repair_text(m._read_text(str(out)))
        remaining = [f for f in results[0].fields if f.tag == "245"]
        assert len(remaining) == 1

    def test_no_strip_flag_leaves_duplicates(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "14", [("a", "Real title /")]),
                m.Field_("245", "  ", [("a", "2nd ed.")]),
            ],
        )
        src = tmp_path / "dup245.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        rc = m.main([
            str(src), "-o", str(out), "--no-strip-duplicate-non-repeatable-fields",
        ])
        assert rc == 0
        results = m.repair_text(m._read_text(str(out)))
        remaining = [f for f in results[0].fields if f.tag == "245"]
        assert len(remaining) == 2


# ---------------------------------------------------------------------------
# fix_008_length
# ---------------------------------------------------------------------------

class TestFix008Length:
    def test_pads_short_008(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 30)],
        )
        details = m.fix_008_length(parsed)
        assert len(details) == 1
        assert "padded" in details[0]
        field008 = parsed.fields[0]
        assert len(field008.content) == 40
        assert field008.content == "x" * 30 + " " * 10

    def test_truncates_long_008(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 45)],
        )
        details = m.fix_008_length(parsed)
        assert len(details) == 1
        assert "truncated" in details[0]
        assert parsed.fields[0].content == "x" * 40

    def test_leaves_correct_length_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        assert m.fix_008_length(parsed) == []

    def test_runs_by_default_via_cli_logged_as_fixed_requires_attention(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 35),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )
        src = tmp_path / "short008.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== FIXED/REQUIRES ATTENTION: fixed_008_length (1) ===" in content
        results = m.repair_text(m._read_text(str(out)))
        field008 = next(f for f in results[0].fields if f.tag == "008")
        assert len(field008.content) == 40


# ---------------------------------------------------------------------------
# find_invalid_indicator_values
# ---------------------------------------------------------------------------

class TestFindInvalidIndicatorValues:
    def test_flags_non_digit_non_blank_indicator(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "X0", [("a", "Title.")])],
        )
        findings = m.find_invalid_indicator_values(parsed)
        assert len(findings) == 1
        assert findings[0][0] == "invalid_indicator_value"

    def test_does_not_flag_digits_or_blanks(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Subject")])],
        )
        assert m.find_invalid_indicator_values(parsed) == []

    def test_does_not_flag_locally_defined_9xx_fields(self):
        # real false-positive found: this tool's own remap_999_to_945
        # sets indicators "ff" on 945, which isn't a digit/blank but
        # also isn't wrong -- MARC21 doesn't define indicator meanings
        # for the 900-999 locally-defined range at all.
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("945", "ff", [("i", "12345")])],
        )
        assert m.find_invalid_indicator_values(parsed) == []

    def test_control_fields_are_skipped(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        assert m.find_invalid_indicator_values(parsed) == []


# ---------------------------------------------------------------------------
# find_invalid_bibliographic_level
# ---------------------------------------------------------------------------

class TestFindInvalidBibliographicLevel:
    def test_flags_invalid_byte_07(self):
        leader = _VALID_LEADER[:7] + "9" + _VALID_LEADER[8:]
        parsed = m.ParsedRecord(leader=leader, entries=[], fields=[])
        findings = m.find_invalid_bibliographic_level(parsed)
        assert len(findings) == 1
        assert findings[0][0] == "invalid_bibliographic_level"

    def test_valid_byte_07_not_flagged(self):
        parsed = m.ParsedRecord(leader=_VALID_LEADER, entries=[], fields=[])
        assert m.find_invalid_bibliographic_level(parsed) == []


# ---------------------------------------------------------------------------
# find_dangling_880_links
# ---------------------------------------------------------------------------

class TestFindDangling880Links:
    def test_flags_reference_to_missing_field(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("880", "1 ", [("6", "245-01"), ("a", "Some title")])],
        )
        findings = m.find_dangling_880_links(parsed)
        assert len(findings) == 1
        assert findings[0][0] == "dangling_880_link"

    def test_does_not_flag_valid_reference(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("880", "1 ", [("6", "245-01"), ("a", "Some title")]),
            ],
        )
        assert m.find_dangling_880_links(parsed) == []


# ---------------------------------------------------------------------------
# find_invalid_isbn_issn_checksums
# ---------------------------------------------------------------------------

class TestFindInvalidIsbnIssnChecksums:
    def test_flags_bad_isbn10_checksum(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("020", "  ", [("a", "0596000271")])],  # wrong check digit
        )
        findings = m.find_invalid_isbn_issn_checksums(parsed)
        assert len(findings) == 1

    def test_valid_isbn10_not_flagged(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("020", "  ", [("a", "0596000278")])],
        )
        assert m.find_invalid_isbn_issn_checksums(parsed) == []

    def test_valid_isbn13_not_flagged(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("020", "  ", [("a", "9780596000271")])],
        )
        assert m.find_invalid_isbn_issn_checksums(parsed) == []

    def test_flags_bad_issn_checksum(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("022", "  ", [("a", "03785956")])],  # wrong check digit
        )
        findings = m.find_invalid_isbn_issn_checksums(parsed)
        assert len(findings) == 1

    def test_valid_issn_not_flagged(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("022", "  ", [("a", "0378-5955")])],  # real ISSN
        )
        assert m.find_invalid_isbn_issn_checksums(parsed) == []

    def test_ignores_non_isbn_length_values(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("020", "  ", [("z", "cancelled, no valid form")])],
        )
        assert m.find_invalid_isbn_issn_checksums(parsed) == []


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
        # every entry for this record is informational-only, so once
        # filtered out there's nothing left to log at all -- no file
        assert not _resolve_log(log).exists()
        # the fix itself still ran, even though it's not in the log
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
        assert "INFORMATIONAL" in _resolve_log(log).read_text(encoding="utf-8")


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
        m.write_log(str(log_path), entries)
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = next(i for i, ln in enumerate(lines) if "NOT FIXED: missing_008" in ln)
        fixed_added_header = next(
            i for i, ln in enumerate(lines) if "INFORMATIONAL: some_fixed_thing" in ln
        )
        fixed_removed_header = next(
            i for i, ln in enumerate(lines) if "INFORMATIONAL: some_other_fixed_thing" in ln
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
        m.write_log(str(log_path), entries)
        lines = log_path.read_text(encoding="utf-8").splitlines()

        not_fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        fixed_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== FIXED"))
        dup_header = next(i for i, ln in enumerate(lines) if ln.startswith("=== DUPLICATE"))
        informational_headers = [
            i for i, ln in enumerate(lines) if ln.startswith("=== INFORMATIONAL")
        ]
        assert len(informational_headers) == 4
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
        # both the patched 245 and the missing-008 default are logged as
        # added_default_245/added_default_008, both INFORMATIONAL and
        # off by default -- nothing else fired, so no log file at all
        assert not _resolve_log(log).exists()


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
        assert not (tmp_path / "three_fixed.mrc").exists()


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
            fields=fields + [m.Field_("852", "  ", [("a", "Main Library")])],
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
        assert not (tmp_path / "mixed_fixed.mrc").exists()
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
