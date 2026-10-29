"""Records over 99999 bytes: detected, then trimmed 9xx -> 5xx(+880) -> 6xx."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marc_repair as m  # noqa: E402

LEADER = "00000nam a2200000 a 4500"


def _rec(fields):
    return m.ParsedRecord(leader=LEADER, entries=[], fields=fields)


def _f(tag, size, ind="  ", sub6=None):
    subs = ([("6", sub6)] if sub6 else []) + [("a", "x" * size)]
    return m.Field_(tag, ind, subs)


def _base():
    return [
        m.Field_("001", None, None, content="id1"),
        m.Field_("008", None, None, content="x" * 40),
        m.Field_("245", "00", [("a", "Title.")]),
    ]


def _tags(rec):
    return [f.tag for f in rec.fields]


def test_detection_boundary():
    filler = _f("500", 1)
    rec = _rec(_base() + [_f("700", 9000) for _ in range(10)] + [filler])
    assert m.record_exceeds_max_length(rec) is None
    # grow the last field until the assembled size is exactly 99999, then 100000
    overhead = len(m.assemble_marc(rec)) - 1
    filler.subfields = [("a", "x" * (99999 - overhead))]
    assert len(m.assemble_marc(rec)) == 99999
    assert m.record_exceeds_max_length(rec) is None
    filler.subfields = [("a", "x" * (100000 - overhead))]
    assert m.record_exceeds_max_length(rec) == 100000


def test_9xx_removed_first_but_999_ff_kept():
    rec = _rec(_base() + [
        _f("999", 9900, "ff"), _f("900", 9000), _f("500", 9000), _f("650", 9000),
    ] + [_f("700", 9000) for _ in range(7)])
    assert m.record_exceeds_max_length(rec)
    detail = m.trim_oversized_record(rec)
    assert detail and "=900 " in detail
    assert "999" in _tags(rec)
    assert "500" in _tags(rec) and "650" in _tags(rec)
    assert m.record_exceeds_max_length(rec) is None


def test_999_without_ff_is_removable():
    rec = _rec(_base() + [_f("999", 9900, "  ")] + [_f("700", 9000) for _ in range(10)])
    m.trim_oversized_record(rec)
    assert "999" not in _tags(rec)


def test_5xx_largest_first_with_linked_880_then_stops():
    rec = _rec(_base() + [
        _f("520", 9500, sub6="880-01"),
        _f("880", 3000, sub6="520-01"),
        _f("500", 9000), _f("504", 2000), _f("586", 200),
        _f("650", 9000),
    ] + [_f("700", 9000) for _ in range(8)])
    before = m.record_exceeds_max_length(rec)
    assert before and before - 12500 - 18 <= m.MAX_RECORD_BYTES
    detail = m.trim_oversized_record(rec)
    assert "=520" in detail and "=880" in detail
    tags = _tags(rec)
    assert "520" not in tags and "880" not in tags
    assert {"500", "504", "586", "650"} <= set(tags)  # stopped once it fit


def test_6xx_only_after_5xx_exhausted_and_7xx_never():
    rec = _rec(_base() + [_f("500", 1000)] + [_f("650", 9000) for _ in range(4)]
               + [_f("700", 9000) for _ in range(8)])
    detail = m.trim_oversized_record(rec)
    assert detail and "=500" in detail and "=650" in detail
    assert _tags(rec).count("700") == 8
    assert m.record_exceeds_max_length(rec) is None


def test_untrimmable_record_left_unchanged():
    rec = _rec(_base() + [_f("999", 100, "ff")] + [_f("700", 9000) for _ in range(12)])
    n = len(rec.fields)
    assert m.trim_oversized_record(rec) is None
    assert len(rec.fields) == n


def test_record_that_fits_is_untouched():
    rec = _rec(_base() + [_f("900", 100)])
    assert m.trim_oversized_record(rec) is None
    assert "900" in _tags(rec)
