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
from dataclasses import dataclass, field
from typing import Callable

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

    def test_main_end_to_end_logs_as_fixed_at_bottom(self, tmp_path, monkeypatch):
        # transcode_marc8_failed is one of the few categories still left
        # genuinely NOT FIXED by default -- everything else easy to
        # trigger (unfixed_non_numeric_tag, incomplete_852) is now
        # FIXED/REQUIRES ATTENTION, and missing_008/invalid_subfield_code have
        # been retired entirely (008/invalid subfield codes are now
        # always fixed unconditionally). Forcing a real transcode
        # failure (rather than just disabling a flag) needs pymarc's
        # marc8_to_unicode to actually raise, same technique as
        # test_main_falls_back_gracefully_on_transcode_failure above.
        pytest.importorskip("pymarc")
        import pymarc.marc8

        def failing_marc8_to_unicode(data, hide_utf8_warnings=False):
            raise UnicodeDecodeError("marc8_to_unicode", data, 0, len(data), "boom")

        monkeypatch.setattr(pymarc.marc8, "marc8_to_unicode", failing_marc8_to_unicode)

        leader = list(_SYNTHETIC_LEADER)
        leader[9] = " "
        fields = [
            m.Field_("001", None, None, content="abc123"),
            m.Field_("008", None, None, content="x" * 40),
            # non-ASCII so this hits the MARC-8 transcode path, which the
            # monkeypatch above forces to fail
            m.Field_("245", "00", [("a", "Titl\xe5.")]),
        ]
        parsed = m.ParsedRecord(leader="".join(leader), entries=[], fields=fields)
        raw = m.assemble_marc(parsed).decode("utf-8", errors="surrogateescape")
        corrupted = _drop_indicator_chars(raw, 1)
        src = tmp_path / "bad_indicators.mrc"
        src.write_bytes(corrupted.encode("utf-8", errors="surrogateescape"))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        lines = _resolve_log(log).read_text(encoding="utf-8").splitlines()
        not_fixed_idx = next(i for i, ln in enumerate(lines) if ln.startswith("=== NOT FIXED"))
        attention_idx = next(
            i for i, ln in enumerate(lines)
            if ln.startswith("=== FIXED/REQUIRES ATTENTION")
        )
        assert not_fixed_idx < attention_idx, (
            "NOT FIXED block must come before FIXED/REQUIRES ATTENTION block"
        )
        assert any(
            "[FIXED/REQUIRES ATTENTION]" in ln and "padded" in ln
            for ln in lines[attention_idx:]
        )
        assert any("[NOT FIXED]" in ln for ln in lines[not_fixed_idx:attention_idx])


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

    def test_log_informational_flag_enables_transcoded_marc8_logging(self, tmp_path):
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
            str(src), "-o", str(out), "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\ttranscoded_marc8\t" in content

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
        with pytest.raises(RuntimeError, match=r"tag 245 \$a"):
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
        assert "=== NOT FIXED: transcode_marc8_failed ===" in content
        assert "\ttranscode_marc8_failed\t" in content
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
# strip_null_identifiers -- empty subfield removal, main()'s bib pipeline
# ---------------------------------------------------------------------------

class TestStripNullIdentifiersBibPipeline:
    def _record_with_empty_035_a(self):
        return m.ParsedRecord(
            leader=_VALID_LEADER,
            entries=[],
            fields=[
                m.Field_("001", None, None, content="997"),
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("035", "  ", [("a", ""), ("0", "COLOFB  1492")]),
                m.Field_("245", "00", [("a", "Some title.")]),
            ],
        )

    def test_removed_but_not_logged_by_default(self, tmp_path):
        src = tmp_path / "bib.mrc"
        src.write_bytes(m.assemble_marc(self._record_with_empty_035_a()))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== INFORMATIONAL: removed_null_identifier ===" in content
        assert "\tremoved_null_identifier\t" not in content
        parsed = m.read_intact_record(out.read_bytes().decode("utf-8"))
        f035 = next(f for f in parsed.fields if f.tag == "035")
        assert not any(code == "a" for code, _ in f035.subfields)
        assert ("0", "COLOFB  1492") in f035.subfields

    def test_logged_when_flag_enabled(self, tmp_path):
        src = tmp_path / "bib.mrc"
        src.write_bytes(m.assemble_marc(self._record_with_empty_035_a()))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--log", str(log),
            "--log-full", "removed_null_identifier",
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tremoved_null_identifier\t" in content
        assert "[INFORMATIONAL]" in content


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

    def test_flags_unfixed_non_numeric_tag(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("24A", "00", [("a", "Odd tag")]),
            ],
        )
        warnings = m.find_suspicious_fields(parsed)
        assert any(
            cat == "unfixed_non_numeric_tag" and "24A" in detail for cat, detail in warnings
        )


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

class TestFixMisplacedSubfieldCodes:
    def test_recovers_letter_code_with_stray_space(self):
        # Raw bytes "\x1f c2000." parse as code=" ", data="c2000." --
        # real production defect: the actual intended subfield is $c
        # "c2000." (a common AACR2-era copyright-date convention) with
        # one extra space accidentally inserted before its code.
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("260", "  ", [("a", "New York :"), (" ", "c2000.")])],
        )
        details = m.fix_misplaced_subfield_codes(parsed)
        assert len(details) == 1
        assert parsed.fields[0].subfields == [("a", "New York :"), ("c", "2000.")]

    def test_recovers_digit_code_with_stray_space(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("035", "  ", [(" ", "2 21")])],
        )
        details = m.fix_misplaced_subfield_codes(parsed)
        assert len(details) == 1
        assert parsed.fields[0].subfields == [("2", " 21")]

    def test_leaves_valid_codes_untouched(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("650", " 0", [("a", "Subject"), ("2", "local")])],
        )
        details = m.fix_misplaced_subfield_codes(parsed)
        assert details == []
        assert parsed.fields[0].subfields == [("a", "Subject"), ("2", "local")]

    def test_leaves_unrecoverable_space_code_alone(self):
        # Code is a space, but the very next character isn't a valid
        # code either -- nothing safe to recover here, so this is left
        # for strip_invalid_subfield_codes to remove instead.
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("500", "  ", [(" ", " still no valid code")])],
        )
        details = m.fix_misplaced_subfield_codes(parsed)
        assert details == []
        assert parsed.fields[0].subfields == [(" ", " still no valid code")]

    def test_leaves_empty_data_after_space_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("500", "  ", [(" ", "")])],
        )
        details = m.fix_misplaced_subfield_codes(parsed)
        assert details == []
        assert parsed.fields[0].subfields == [(" ", "")]

    def test_control_fields_are_left_alone(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("001", None, None, content="abc123")],
        )
        details = m.fix_misplaced_subfield_codes(parsed)
        assert details == []
        assert parsed.fields[0].content == "abc123"

    def test_runs_before_strip_invalid_subfield_codes_in_the_cli(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[
                m.Field_("001", None, None, content="misplacedtest"),
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("260", "  ", [
                    ("a", "New York :"), ("b", "Wiley,"), (" ", "c2000."),
                ]),
            ],
        )
        src = tmp_path / "in.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--log", str(log),
            "--log-full", "fixed_misplaced_subfield_code",
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tfixed_misplaced_subfield_code\t" in content
        assert "[INFORMATIONAL]" in content
        assert "\tremoved_invalid_subfield\t" not in content
        parsed_out = m.read_intact_record(m._read_text(str(out)))
        f260 = next(f for f in parsed_out.fields if f.tag == "260")
        assert ("c", "2000.") in f260.subfields

    def test_fix_still_applies_but_not_logged_by_default(self, tmp_path):
        # Not FIXED/REQUIRES ATTENTION anymore -- this doesn't need a
        # human's attention (nothing is discarded or guessed), so by
        # default it's not even in the log, even though the fix itself
        # always runs.
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[
                m.Field_("001", None, None, content="misplacedtest"),
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("260", "  ", [
                    ("a", "New York :"), ("b", "Wiley,"), (" ", "c2000."),
                ]),
            ],
        )
        src = tmp_path / "in.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tfixed_misplaced_subfield_code\t" not in content
        parsed_out = m.read_intact_record(m._read_text(str(out)))
        f260 = next(f for f in parsed_out.fields if f.tag == "260")
        assert ("c", "2000.") in f260.subfields

    def test_disabled_via_flag_falls_back_to_removal(self, tmp_path):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[
                m.Field_("001", None, None, content="misplacedtest"),
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("260", "  ", [
                    ("a", "New York :"), ("b", "Wiley,"), (" ", "c2000."),
                ]),
            ],
        )
        src = tmp_path / "in.mrc"
        src.write_bytes(m.assemble_marc(parsed))
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--log", str(log),
            "--no-fix-misplaced-subfield-codes",
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "fixed_misplaced_subfield_code" not in content
        assert "removed_invalid_subfield" in content
        parsed_out = m.read_intact_record(m._read_text(str(out)))
        f260 = next(f for f in parsed_out.fields if f.tag == "260")
        assert all(code != "c" for code, _ in f260.subfields)


# ---------------------------------------------------------------------------
# strip_invalid_subfield_codes -- remove subfields with genuinely
# unrecoverable codes (see fix_misplaced_subfield_codes above for the one
# recoverable shape)
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

    def test_log_informational_flag_enables_leader_entry_map_fixed_logging(self, tmp_path):
        src = tmp_path / "badmap.mrc"
        src.write_bytes(self._corrupted_entry_map_bytes())
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tleader_entry_map_fixed\t" in content

    def _corrupted_entry_map_bytes_invalid_utf8(self):
        # Same idea as `_corrupted_entry_map_bytes`, but the corrupted byte
        # (0x92) isn't valid UTF-8 anywhere in the sequence -- unlike "45x0",
        # which is still plain ASCII. Real production data has been seen
        # with exactly this: the file as a whole is genuine UTF-8, but one
        # byte inside the leader's fixed "4500" constant is corrupted.
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        raw = bytearray(m.assemble_marc(parsed))
        raw[20:24] = b"45\x920"
        return bytes(raw)

    def test_invalid_utf8_byte_in_entry_map_does_not_crash_the_stream(self, tmp_path, monkeypatch):
        # Regression test: `iter_repair_stream`'s UTF-8 decoder used to be
        # strict, so this byte raised UnicodeDecodeError and killed the
        # whole run before the leader-repair logic above ever got a chance
        # to run -- even though the file as a whole is meant to be read as
        # UTF-8. `detect_encoding` only probes the first 4MB, so in the real
        # production file this crash came from, the file-wide guess came
        # back "utf-8" despite one bad byte tens of MB in; a tiny test file
        # would instead get correctly probed in full and fall back to
        # latin-1 on its own, sidestepping the bug entirely -- so
        # `detect_encoding` is forced to "utf-8" here to isolate exactly
        # what's actually being regression-tested: the streaming decoder
        # itself, not the probe heuristic. A tiny chunk_size forces the bad
        # byte into a chunk of its own, exercising the decoder the same way
        # a real 32MB chunk boundary did in production.
        monkeypatch.setattr(m, "detect_encoding", lambda path: "utf-8")
        src = tmp_path / "badmap_invalid_utf8.mrc"
        src.write_bytes(self._corrupted_entry_map_bytes_invalid_utf8())
        records = list(m.iter_repair_stream(str(src), chunk_size=5))
        assert len(records) == 1
        parsed, _ = records[0]
        assert m.assemble_marc(parsed)[20:24] == b"4500"

    def test_invalid_utf8_byte_in_entry_map_fixed_via_cli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "detect_encoding", lambda path: "utf-8")
        src = tmp_path / "badmap_invalid_utf8.mrc"
        src.write_bytes(self._corrupted_entry_map_bytes_invalid_utf8())
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        assert out.read_bytes()[20:24] == b"4500"


# ---------------------------------------------------------------------------
# reattach_orphaned_trailing_fields -- a trailing field physically present
# in the file but missing its own directory entry (so excluded from its
# record's own declared length), always shaped like a personal name
# heading -- a real defect found in a University of Bahamas bib export.
# ---------------------------------------------------------------------------

class TestTryParseOrphanedField:
    def test_valid_single_field_parses(self):
        result = m._try_parse_orphaned_field("1 \x1faQuinn, Frances,\x1fd1963-\x1e")
        assert result == ("1 ", [("a", "Quinn, Frances,"), ("d", "1963-")])

    def test_valid_single_field_with_trailing_recterm(self):
        result = m._try_parse_orphaned_field("1 \x1faQuinn, Frances,\x1fd1963-\x1e\x1d")
        assert result == ("1 ", [("a", "Quinn, Frances,"), ("d", "1963-")])

    def test_multi_field_chunk_rejected(self):
        chunk = "1 \x1faOne\x1e1 \x1faTwo\x1e"
        assert m._try_parse_orphaned_field(chunk) is None

    def test_no_field_terminator_rejected(self):
        assert m._try_parse_orphaned_field("1 \x1faNo terminator") is None

    def test_data_before_first_subfield_rejected(self):
        assert m._try_parse_orphaned_field("1 garbage\x1faReal\x1e") is None

    def test_no_subfields_rejected(self):
        assert m._try_parse_orphaned_field("1 \x1e") is None

    def test_too_short_rejected(self):
        assert m._try_parse_orphaned_field("1\x1e") is None


class TestReattachOrphanedTrailingFields:
    def _resolved(self):
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )
        return parsed, m.assemble_marc(parsed).decode("utf-8")

    def _orphan_chunk(self, indicators, subfields):
        text = indicators + "".join(f"\x1f{c}{d}" for c, d in subfields) + m.FIELDTERM
        placeholder = m.ParsedRecord(leader="?" * 24, entries=[])
        placeholder.unresolved.append((
            0, m.DirEntry("???", len(text), 0), text[:200],
            m.RepairError("no consistent directory found for this record at all"),
        ))
        return placeholder, text + m.RECTERM

    def test_merges_personal_name_shaped_orphan_into_preceding_record(self):
        prev = self._resolved()
        orphan = self._orphan_chunk("1 ", [("a", "Quinn, Frances,"), ("d", "1963-")])
        results = list(m.reattach_orphaned_trailing_fields(iter([prev, orphan])))
        assert len(results) == 1
        parsed, text = results[0]
        assert not parsed.unresolved
        assert parsed.fields[-1].tag == "700"
        assert parsed.fields[-1].subfields == [("a", "Quinn, Frances,"), ("d", "1963-")]
        assert parsed.reattached_orphaned_fields
        assert text == prev[1] + orphan[1]

    def test_does_not_merge_when_preceding_record_is_itself_unresolved(self):
        orphan1 = self._orphan_chunk("1 ", [("a", "no home record")])
        orphan2 = self._orphan_chunk("1 ", [("a", "Quinn, Frances,"), ("d", "1963-")])
        results = list(m.reattach_orphaned_trailing_fields(iter([orphan1, orphan2])))
        assert len(results) == 2
        assert results[0][0].unresolved
        assert results[1][0].unresolved

    def test_does_not_merge_subfield_codes_outside_personal_name_whitelist(self):
        prev = self._resolved()
        # $u (URL) isn't a personal-name-heading code -- too ambiguous to
        # safely guess tag 700 for.
        orphan = self._orphan_chunk("4 ", [("u", "http://example.com/resource")])
        results = list(m.reattach_orphaned_trailing_fields(iter([prev, orphan])))
        assert len(results) == 2
        assert results[1][0].unresolved

    def test_does_not_merge_without_dollar_a(self):
        prev = self._resolved()
        orphan = self._orphan_chunk("1 ", [("d", "1963-")])  # no $a at all
        results = list(m.reattach_orphaned_trailing_fields(iter([prev, orphan])))
        assert len(results) == 2
        assert results[1][0].unresolved

    def test_does_not_merge_multi_field_chunks(self):
        prev = self._resolved()
        text = "1 \x1faOne\x1e1 \x1faTwo\x1e"
        placeholder = m.ParsedRecord(leader="?" * 24, entries=[])
        placeholder.unresolved.append((
            0, m.DirEntry("???", len(text), 0), text[:200],
            m.RepairError("no consistent directory found for this record at all"),
        ))
        results = list(
            m.reattach_orphaned_trailing_fields(iter([prev, (placeholder, text + m.RECTERM)]))
        )
        assert len(results) == 2

    def test_unresolved_chunk_at_start_of_stream_passes_through(self):
        orphan = self._orphan_chunk("1 ", [("a", "Quinn, Frances,"), ("d", "1963-")])
        results = list(m.reattach_orphaned_trailing_fields(iter([orphan])))
        assert len(results) == 1
        assert results[0][0].unresolved

    def test_passthrough_when_wrapping_empty_stream(self):
        assert list(m.reattach_orphaned_trailing_fields(iter([]))) == []


class TestReattachOrphanedFieldsCLI:
    def _record_with_orphaned_trailing_field(self) -> bytes:
        """Build a record via assemble_marc (self-consistent leader,
        directory, and declared length), then splice one extra field's
        real bytes in directly before the trailing record terminator --
        bypassing assemble_marc so the directory/length never account
        for it. This is exactly the real-world shape found in
        production: internally self-consistent leader/directory, but a
        genuine trailing field's bytes still physically in the file,
        unaccounted for by either."""
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[
                m.Field_("001", None, None, content="orphantest"),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )
        raw = bytearray(m.assemble_marc(parsed))
        assert raw[-1:] == b"\x1d"
        orphan = m.Field_(
            "700", "1 ", [("a", "Quinn, Frances,"), ("d", "1963-")]
        ).delimited().encode("utf-8")
        return bytes(raw[:-1]) + orphan + b"\x1d"

    def test_reattached_by_default_and_logged(self, tmp_path):
        src = tmp_path / "orphan.mrc"
        src.write_bytes(self._record_with_orphaned_trailing_field())
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 0
        assert m.count_records(str(out)) == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\treattached_orphaned_field\t" in content
        assert "[FIXED/REQUIRES ATTENTION]" in content
        parsed = m.read_intact_record(m._read_text(str(out)))
        assert parsed.fields[-1].tag == "700"
        assert parsed.fields[-1].subfields == [("a", "Quinn, Frances,"), ("d", "1963-")]

    def test_disabled_via_flag_diverts_to_error_file(self, tmp_path):
        src = tmp_path / "orphan.mrc"
        src.write_bytes(self._record_with_orphaned_trailing_field())
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([
            str(src), "-o", str(out), "--log", str(log),
            "--no-reattach-orphaned-fields",
        ])
        assert rc == 1  # an UNFIXABLE record makes the CLI exit non-zero
        assert m.count_records(str(out)) == 1
        assert m.count_records(str(tmp_path / "out_error.mrc")) == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "unfixable" in content
        assert "[UNFIXABLE]" in content
        assert "reattached_orphaned_field" not in content


# ---------------------------------------------------------------------------
# UNFIXABLE -- a record neither mode can repair at all (no consistent
# directory found, or a field/base address too large for ISO 2709) is
# diverted to a separate "_error" file instead of the main output, and
# logged at the very top of the log.
# ---------------------------------------------------------------------------

class TestUnfixableErrorFile:
    def _clean_record(self, id_="clean") -> bytes:
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[
                m.Field_("001", None, None, content=id_),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )
        return m.assemble_marc(parsed)

    def _unresolvable_garbage(self) -> bytes:
        return b"not a marc record at all, no leader here whatsoever\x1d"

    def _oversized_field_record(self) -> bytes:
        placeholder = "P" * 50
        parsed = m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[
                m.Field_("001", None, None, content="oversized"),
                m.Field_("500", "  ", [("a", placeholder)]),
            ],
        )
        raw = m.assemble_marc(parsed)
        needle = placeholder.encode()
        assert raw.count(needle) == 1
        return raw.replace(needle, b"x" * 10000)

    def test_unresolvable_record_diverted_to_error_file(self, tmp_path):
        src = tmp_path / "in.mrc"
        src.write_bytes(self._clean_record() + self._unresolvable_garbage())
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 1
        assert m.count_records(str(out)) == 1
        error_path = tmp_path / "out_error.mrc"
        assert error_path.exists()
        assert m.count_records(str(error_path)) == 1
        assert error_path.read_bytes() == self._unresolvable_garbage()
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== UNFIXABLE: unfixable" in content
        assert "no consistent directory found for this record at all" in content

    def test_oversized_field_diverted_to_error_file(self, tmp_path):
        raw = self._oversized_field_record()
        src = tmp_path / "in.mrc"
        src.write_bytes(raw)
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log)])
        assert rc == 1
        assert m.count_records(str(out)) == 0
        error_path = tmp_path / "out_error.mrc"
        assert error_path.read_bytes() == raw
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== UNFIXABLE: unfixable" in content
        # the log explains *why* -- the same reason assemble_marc itself raises
        assert "directory's length field is only 4 digits" in content

    def test_error_file_not_created_when_nothing_unfixable(self, tmp_path):
        src = tmp_path / "in.mrc"
        src.write_bytes(self._clean_record())
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out)])
        assert rc == 0
        assert not (tmp_path / "out_error.mrc").exists()

    def test_unfixable_section_appears_before_every_other_section(self, tmp_path):
        # A file with one of everything: unresolvable garbage (UNFIXABLE),
        # a record missing both 008 and 245 -- both land in FIXED/
        # REQUIRES ATTENTION -- and a clean record. UNFIXABLE must render
        # first regardless of write_log's usual NOT FIXED-first ordering.
        missing_245 = m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER, entries=[],
            fields=[m.Field_("001", None, None, content="no245")],
        ))
        src = tmp_path / "in.mrc"
        # Leading garbage before any real leader is silently skipped by
        # iter_repair_stream (nothing to resync *from* yet), so the
        # unresolvable chunk needs a real record ahead of it to exercise
        # the UNFIXABLE path at all -- same as
        # test_unresolvable_record_diverted_to_error_file above.
        src.write_bytes(
            self._clean_record() + self._unresolvable_garbage() + missing_245
        )
        out = tmp_path / "out.mrc"
        log = tmp_path / "run.log"
        rc = m.main([str(src), "-o", str(out), "--log", str(log), "--log-informational"])
        assert rc == 1
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert content.index("=== UNFIXABLE") < content.index("=== INFORMATIONAL")

    def test_clean_records_unaffected_by_a_later_unfixable_one(self, tmp_path):
        src = tmp_path / "in.mrc"
        src.write_bytes(
            self._clean_record("first") + self._unresolvable_garbage()
            + self._clean_record("second")
        )
        out = tmp_path / "out.mrc"
        rc = m.main([str(src), "-o", str(out)])
        assert rc == 1
        assert m.count_records(str(out)) == 2
        text = m._read_text(str(out))
        assert "first" in text and "second" in text


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
        assert "=== INFORMATIONAL: invalid_tag ===" in content
        assert "\tinvalid_tag\t" in content
        assert "\tunfixed_non_numeric_tag\t" not in content
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
        rc = m.main([
            str(src), "-o", str(out), "--no-fix-invalid-tags",
            "--log-full", "unfixed_non_numeric_tag", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== FIXED/REQUIRES ATTENTION: unfixed_non_numeric_tag ===" in content
        assert "\tunfixed_non_numeric_tag\t" in content
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

    def test_falls_back_to_stripping_when_every_9xx_slot_is_taken(self, tmp_path):
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
        rc = m.main([
            str(src), "-o", str(out), "--log-full", "unfixed_non_numeric_tag", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "=== FIXED/REQUIRES ATTENTION: unfixed_non_numeric_tag ===" in content
        assert "content discarded" in content
        results = m.repair_text(m._read_text(str(out)))
        assert not any(f.tag == "24A" for f in results[1].fields)
        # rec1's own 100 filler 9XX fields are untouched -- only the
        # genuinely invalid tag on rec2 gets stripped
        assert sum(1 for f in results[0].fields if f.tag.isdigit() and f.tag[0] == "9") == 100


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
        # runs, header + count always shown, but not listed in full
        # unless --log-full remapped_999_to_945 (or --log-informational)
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tremapped_999_to_945\t" not in content
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
            str(src), "-o", str(out), "--remap-999-to-945",
            "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tremapped_999_to_945\t" in content


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

    def test_log_informational_flag_enables_normalized_subfield_9_to_0_logging(self, tmp_path):
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
            str(src), "-o", str(out), "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tnormalized_subfield_9_to_0\t" in content
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

    def test_log_informational_flag_enables_normalized_smart_characters_logging(self, tmp_path):
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
            str(src), "-o", str(out), "--log-informational", "--log", str(log),
        ])
        assert rc == 0
        content = _resolve_log(log).read_text(encoding="utf-8")
        assert "\tnormalized_smart_characters\t" in content
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


# ---------------------------------------------------------------------------
# Detect-only checks: find_invalid_indicator_values,
# find_invalid_bibliographic_level, find_dangling_880_links,
# find_invalid_isbn_issn_checksums -- no fix, no CLI wiring of their own
# to speak of (see TestLogInformational and each function's own
# --check-*/--log-* flag tests elsewhere for that), so grouped here as
# one class of small, purely-detection unit tests.
# ---------------------------------------------------------------------------

class TestDetectOnlyChecks:
    # -- find_invalid_indicator_values --

    def test_flags_non_digit_non_blank_indicator(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("245", "X0", [("a", "Title.")])],
        )
        findings = m.find_invalid_indicator_values(parsed)
        assert len(findings) == 1
        assert findings[0][0] == "invalid_indicator_value"
        # detail includes both indicators together, not just the bad one,
        # so a reader has the full context without going back to the record
        assert "'X0'" in findings[0][1]

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

    def test_indicator_values_control_fields_are_skipped(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )
        assert m.find_invalid_indicator_values(parsed) == []

    # -- find_dangling_880_links --

    def test_flags_reference_to_missing_field(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[m.Field_("880", "1 ", [("6", "245-01"), ("a", "Some title")])],
        )
        findings = m.find_dangling_880_links(parsed)
        assert len(findings) == 1
        assert findings[0][0] == "dangling_880_link"

    def test_does_not_flag_valid_880_reference(self):
        parsed = m.ParsedRecord(
            leader="0" * 24,
            entries=[],
            fields=[
                m.Field_("245", "00", [("a", "Title.")]),
                m.Field_("880", "1 ", [("6", "245-01"), ("a", "Some title")]),
            ],
        )
        assert m.find_dangling_880_links(parsed) == []

    # -- find_invalid_isbn_issn_checksums --

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
# CLI-default-behavior wiring, parametrized across categories that share
# the same shape of test: build one triggering record, run `main` with a
# given set of extra flags, check the log for required/forbidden
# substrings, then check one thing about the repaired output. Categories
# whose verification needs more than that (branching logic, multiple
# records, etc.) keep their own dedicated test instead -- see e.g.
# TestFixInvalidTags.test_runs_by_default_via_cli_picking_an_unused_9xx.
# ---------------------------------------------------------------------------

@dataclass
class _CliDefaultCase:
    id: str
    build: Callable[[], bytes]
    verify: Callable[[list], bool]
    extra_cli_args: list = field(default_factory=list)
    required_log_substrings: list = field(default_factory=list)
    forbidden_log_substrings: list = field(default_factory=list)
    requires_pymarc: bool = False


def _detail_line_marker(category: str) -> str:
    """Substring that only appears in an actual per-record detail line
    for `category` (see `LogEntry.render`, tab-separated), never in
    the "=== SECTION: category ===" header `write_log` always writes
    for every active category regardless of whether it's listed in
    full -- headers use no literal tabs at all."""
    return f"\t{category}\t"


def _run_cli_default_case(case: "_CliDefaultCase", tmp_path):
    if case.requires_pymarc:
        pytest.importorskip("pymarc")
    src = tmp_path / "in.mrc"
    src.write_bytes(case.build())
    out = tmp_path / "out.mrc"
    log = tmp_path / "run.log"
    rc = m.main([str(src), "-o", str(out), *case.extra_cli_args, "--log", str(log)])
    assert rc == 0
    resolved_log = _resolve_log(log)
    content = resolved_log.read_text(encoding="utf-8") if resolved_log.exists() else ""
    for substring in case.required_log_substrings:
        assert substring in content
    for substring in case.forbidden_log_substrings:
        assert substring not in content
    results = m.repair_text(m._read_text(str(out)))
    assert case.verify(results)


_INFORMATIONAL_BY_DEFAULT_CASES = [
    _CliDefaultCase(
        id="leader_byte_defaulted",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_VALID_LEADER[:5] + "0" + _VALID_LEADER[6:],
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )),
        extra_cli_args=["--log-informational"],
        required_log_substrings=[
            "=== INFORMATIONAL: leader_byte_defaulted ===",
            _detail_line_marker("leader_byte_defaulted"),
        ],
        verify=lambda results: results[0].leader[5] == "c",
    ),
    _CliDefaultCase(
        id="fixed_mojibake",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("500", "  ", [("a", "GroÃŸbritannien")]),
            ],
        )),
        extra_cli_args=["--log-informational"],
        required_log_substrings=[
            "=== INFORMATIONAL: fixed_mojibake (character encoding issue) ===",
            _detail_line_marker("fixed_mojibake"),
        ],
        verify=lambda results: (
            next(f for f in results[0].fields if f.tag == "500").subfields
            == [("a", "Großbritannien")]
        ),
    ),
]


def _build_corrupted_entry_map_record() -> bytes:
    parsed = m.ParsedRecord(
        leader=_SYNTHETIC_LEADER,
        entries=[],
        fields=[m.Field_("008", None, None, content="x" * 40)],
    )
    raw = bytearray(m.assemble_marc(parsed))
    raw[20:24] = b"45x0"
    return bytes(raw)


_NOT_LOGGED_UNLESS_FLAGGED_CASES = [
    _CliDefaultCase(
        id="leader_entry_map_fixed",
        build=_build_corrupted_entry_map_record,
        required_log_substrings=["=== INFORMATIONAL: leader_entry_map_fixed ==="],
        forbidden_log_substrings=[_detail_line_marker("leader_entry_map_fixed")],
        verify=lambda results: m.assemble_marc(results[0])[20:24] == b"4500",
    ),
    _CliDefaultCase(
        id="transcoded_marc8",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER[:9] + " " + _SYNTHETIC_LEADER[10:],
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("100", "1 ", [("a", "Bal\xe5asim, \xf2Hasan.")]),
            ],
        )),
        forbidden_log_substrings=[_detail_line_marker("transcoded_marc8")],
        verify=lambda results: results[0].leader[9] == "a",
        requires_pymarc=True,
    ),
    _CliDefaultCase(
        id="normalized_subfield_9_to_0",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("650", " 0", [("a", "Subject"), ("9", "123456")]),
            ],
        )),
        forbidden_log_substrings=[_detail_line_marker("normalized_subfield_9_to_0")],
        verify=lambda results: (
            next(f for f in results[0].fields if f.tag == "650").subfields
            == [("a", "Subject"), ("0", "123456")]
        ),
    ),
    _CliDefaultCase(
        id="normalized_smart_characters",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("520", "  ", [("a", "It’s great.")]),
            ],
        )),
        forbidden_log_substrings=[_detail_line_marker("normalized_smart_characters")],
        verify=lambda results: (
            next(f for f in results[0].fields if f.tag == "520").subfields
            == [("a", "It's great.")]
        ),
    ),
]

_FIXED_REQUIRES_ATTENTION_CASES = [
    _CliDefaultCase(
        id="removed_non_repeatable_duplicate",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "14", [("a", "Real title /")]),
                m.Field_("245", "  ", [("a", "2nd ed.")]),
            ],
        )),
        required_log_substrings=[
            "=== FIXED/REQUIRES ATTENTION: removed_non_repeatable_duplicate ===",
            "2nd ed.",
        ],
        verify=lambda results: len([f for f in results[0].fields if f.tag == "245"]) == 1,
    ),
    _CliDefaultCase(
        id="invalid_bibliographic_level",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER[:7] + "9" + _SYNTHETIC_LEADER[8:],
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 40),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )),
        required_log_substrings=[
            "=== FIXED/REQUIRES ATTENTION: invalid_bibliographic_level ===",
            _detail_line_marker("invalid_bibliographic_level"),
        ],
        verify=lambda results: results[0].leader[7] == "m",
    ),
    _CliDefaultCase(
        id="fixed_008_length",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[
                m.Field_("008", None, None, content="x" * 35),
                m.Field_("245", "00", [("a", "Title.")]),
            ],
        )),
        required_log_substrings=["=== FIXED/REQUIRES ATTENTION: fixed_008_length ==="],
        verify=lambda results: (
            len(next(f for f in results[0].fields if f.tag == "008").content) == 40
        ),
    ),
    _CliDefaultCase(
        id="added_default_245",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("008", None, None, content="x" * 40)],
        )),
        required_log_substrings=[
            "=== FIXED/REQUIRES ATTENTION: added_default_245 ===",
            _detail_line_marker("added_default_245"),
        ],
        verify=lambda results: (
            next(f for f in results[0].fields if f.tag == "245").subfields
            == [("a", "No title")]
        ),
    ),
    _CliDefaultCase(
        id="added_default_008",
        build=lambda: m.assemble_marc(m.ParsedRecord(
            leader=_SYNTHETIC_LEADER,
            entries=[],
            fields=[m.Field_("245", "00", [("a", "Title.")])],
        )),
        required_log_substrings=[
            "=== FIXED/REQUIRES ATTENTION: added_default_008 ===",
            _detail_line_marker("added_default_008"),
        ],
        verify=lambda results: (
            next(f for f in results[0].fields if f.tag == "008").content
            == m.DEFAULT_008_CONTENT
        ),
    ),
]


class TestFixInvalidBibliographicLevel:
    def test_defaults_invalid_byte_07(self):
        leader = _VALID_LEADER[:7] + "9" + _VALID_LEADER[8:]
        parsed = m.ParsedRecord(leader=leader, entries=[], fields=[])
        details = m.fix_invalid_bibliographic_level(parsed)
        assert len(details) == 1
        assert parsed.leader[7] == "m"

    def test_valid_byte_07_left_alone(self):
        parsed = m.ParsedRecord(leader=_VALID_LEADER, entries=[], fields=[])
        assert m.fix_invalid_bibliographic_level(parsed) == []
        assert parsed.leader == _VALID_LEADER

    def test_custom_default(self):
        leader = _VALID_LEADER[:7] + "9" + _VALID_LEADER[8:]
        parsed = m.ParsedRecord(leader=leader, entries=[], fields=[])
        m.fix_invalid_bibliographic_level(parsed, default="s")
        assert parsed.leader[7] == "s"


class TestCliDefaultBehaviors:
    """Each fix/detect category runs by default (or not) and gets logged
    by default (or not) in one of three shapes -- see the case tables
    above. This exercises that CLI wiring once per category without
    repeating the same four-step test body for each one."""

    @pytest.mark.parametrize("case", _INFORMATIONAL_BY_DEFAULT_CASES, ids=lambda c: c.id)
    def test_runs_by_default_and_logged_as_informational(self, case, tmp_path):
        _run_cli_default_case(case, tmp_path)

    @pytest.mark.parametrize("case", _NOT_LOGGED_UNLESS_FLAGGED_CASES, ids=lambda c: c.id)
    def test_runs_by_default_but_not_logged_unless_flagged(self, case, tmp_path):
        _run_cli_default_case(case, tmp_path)

    @pytest.mark.parametrize("case", _FIXED_REQUIRES_ATTENTION_CASES, ids=lambda c: c.id)
    def test_runs_by_default_logged_as_fixed_requires_attention(self, case, tmp_path):
        _run_cli_default_case(case, tmp_path)
