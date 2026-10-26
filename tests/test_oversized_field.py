"""Fields over ISO 2709's 9999-byte directory limit: collapsed, else truncated."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import marc_repair as m  # noqa: E402

ESC = "\x1b"
SF = m.SUBFIELD
FT = m.FIELDTERM


def _raw_record(fields, leader9=" "):
    """Build a record whose directory is correct except that a field over
    9999 bytes gets its length wrapped to 4 digits, the real-world defect
    (later entries' starts still reflect the true layout)."""
    chunks = [content + FT for _, content in fields]
    entries, pos = [], 0
    for (tag, _), chunk in zip(fields, chunks):
        entries.append(f"{tag}{len(chunk) % 10000:04d}{pos:05d}")
        pos += len(chunk)
    directory = "".join(entries) + FT
    base = 24 + len(directory)
    total = base + pos + 1
    leader = f"{total:05d}nam {leader9}22{base:05d}   4500"
    assert len(leader) == 24
    return leader + directory + "".join(chunks) + "\x1d"


def _parse(fields, leader9=" "):
    result = m._read_intact_at(_raw_record(fields, leader9), 0)
    assert result is not None
    return result[0]


def _hebrew_505(n):
    # one Hebrew char per spurious "subscript 9 / reset" pair, like the GTU export
    run = f"{ESC}b9{ESC}s{ESC}(3G{ESC}(B"
    return "8 " + SF + "a" + run * n + " -- end"


def test_collapse_fits_without_truncation():
    content = _hebrew_505(1000)
    assert m._field_byte_length(content, "latin-1") > 9999
    parsed = _parse([("001", "x1"), ("505", content), ("650", " 0" + SF + "aKept.")])
    assert not parsed.truncated_oversized_fields
    (tag, before, after, stripped, _), = parsed.collapsed_oversized_fields
    assert tag == "505" and before > 9999 >= after and stripped == 1000
    assert [f.tag for f in parsed.fields] == ["001", "505", "650"]
    body = "".join(v for f in parsed.fields if f.tag == "505" for _, v in f.subfields)
    assert f"{ESC}b9" not in body and body.endswith("-- end")
    m.assemble_marc(parsed)  # rebuilds without the 9999 RepairError


def test_truncate_when_collapse_not_enough():
    content = "8 " + SF + "a" + " -- ".join(f"Chapter {i}" for i in range(3000))
    parsed = _parse([("001", "x1"), ("505", content), ("650", " 0" + SF + "aKept.")])
    assert not parsed.collapsed_oversized_fields
    (tag, before, after, tail), = parsed.truncated_oversized_fields
    assert tag == "505" and before > 9999 >= after and tail
    kept = [f for f in parsed.fields if f.tag == "505"][0].subfields[0][1]
    assert kept.endswith(" --") and "Chapter 2999" not in kept
    assert [f.tag for f in parsed.fields] == ["001", "505", "650"]
    m.assemble_marc(parsed)


def test_truncate_never_splits_escape_or_multibyte():
    cut = m._truncate_to_fit("x" * 9990 + ESC + "(3GHHH" + "y" * 50, "latin-1")
    assert m._field_byte_length(cut, "latin-1") <= 9999
    assert cut.endswith(m._ASCII_RESET)
    text = "é" * 6000  # 12000 UTF-8 bytes
    cut = m._truncate_to_fit(text, "utf-8")
    assert m._field_byte_length(cut, "utf-8") <= 9999 and set(cut) == {"é"}


def test_merge_redundant_resets_is_lossless():
    s = f"{ESC}(3G{ESC}(B{ESC}(3L{ESC}(B{ESC}(3M{ESC}(B"
    assert m._merge_redundant_g0_resets(s) == f"{ESC}(3GLM{ESC}(B"
