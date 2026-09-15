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

    def test_runs_by_default_but_not_logged_unless_flagged(self, tmp_path):
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
        # normalization itself runs by default, but isn't logged
        # per-record unless --log-normalized-subfield-9-to-0 (and
        # --log-informational) are also given
        resolved_log = _resolve_log(log)
        content = resolved_log.read_text(encoding="utf-8") if resolved_log.exists() else ""
        assert "normalized_subfield_9_to_0" not in content
        results = m.repair_text(m._read_text(str(out)))
        field = next(f for f in results[0].fields if f.tag == "650")
        assert field.subfields == [("a", "Subject"), ("0", "123456")]

    def test_log_normalized_subfield_9_to_0_flag_enables_logging(self, tmp_path):
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
            str(src), "-o", str(out), "--log-normalized-subfield-9-to-0",
            "--log-informational", "--log", str(log),
        ])
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
