"""The PDF copy of each log (marc_repair_lib/pdf_log.py), and the 856-style
log-line truncation that keeps both log formats readable."""
import glob
import os
import re

import pytest

import marc_repair as m
import marc_repair_lib.cli as cli
from marc_repair_lib import pdf_log

pytest.importorskip("reportlab")

_LEADER = "00000nam a2200000ka 4500"


def _write_mrc(path, n=3):
    """`n` records with no 008, so every run logs added_default_008."""
    data = b""
    for i in range(n):
        fields = [
            m.Field_("001", None, None, content=f"id{i}"),
            m.Field_("245", "00", [("a", f"Title {i}.")]),
        ]
        parsed = m.ParsedRecord(leader=_LEADER, entries=[], fields=fields)
        data += m.assemble_marc(parsed)
    path.write_bytes(data)


def _run(tmp_path, *extra):
    src = tmp_path / "in.mrc"
    _write_mrc(src)
    out = tmp_path / "out.mrc"
    rc = m.main([str(src), "-o", str(out), "--log", str(tmp_path / "run.log"), *extra])
    assert rc == 0
    logs = glob.glob(str(tmp_path / "run_*.log"))
    assert len(logs) == 1
    return logs[0]


def _page_count(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type /Page\b", pdf_bytes))


def test_pdf_is_written_by_default_with_the_log_name(tmp_path):
    log = _run(tmp_path)
    pdf = os.path.splitext(log)[0] + ".pdf"
    assert os.path.exists(pdf)
    data = open(pdf, "rb").read()
    assert data.startswith(b"%PDF")
    assert _page_count(data) >= 2          # front matter + at least one body page


def test_no_pdf_log_switch_skips_it(tmp_path):
    log = _run(tmp_path, "--no-pdf-log")
    assert os.path.exists(log)
    assert not os.path.exists(os.path.splitext(log)[0] + ".pdf")


def test_parser_sections_match_the_real_log(tmp_path):
    """The contract with report.write_log: every `=== KIND: category ===`
    header it really writes is found by the parser, with its count."""
    log = _run(tmp_path, "--no-pdf-log", "--log-zero")
    text = open(log, encoding="utf-8").read().splitlines()
    headers = [ln for ln in text if pdf_log._SECTION_RE.match(ln)
               and not ln.startswith("=== RUN:")]
    idx = pdf_log._scan(log, pdf_log._Fonts())
    named = [s for s in idx.sections if s.category is not None]
    assert [f"=== {s.kind}: {s.category} ===" for s in named] == headers
    assert all(s.count for s in named)
    assert idx.run_lines and idx.run_lines[0].startswith("Run:")
    added = next(s for s in named if s.category == "added_default_008")
    assert added.count == "3 record(s)" and added.loss == "NO DATA LOSS"
    assert added.desc


def test_toc_pages_are_ascending_and_every_section_is_linked(tmp_path):
    log = _run(tmp_path, "--no-pdf-log")
    fonts = pdf_log._Fonts()
    idx = pdf_log._scan(log, fonts)
    plan = pdf_log._render(None, fonts, idx, log, "x.pdf", 0, {})
    pages = [plan.marks[s.key] for s in idx.sections if s.category is not None]
    assert pages == sorted(pages) and pages[0] >= 2
    pdf, n = pdf_log.write_pdf_log(log)
    assert n == plan.page
    data = open(pdf, "rb").read()
    # one link per section + one per kind heading + one per page footer
    assert data.count(b"/Subtype /Link") >= len(pages) + n


def test_unknown_lines_are_kept_not_dropped(tmp_path):
    log = tmp_path / "odd.log"
    log.write_text(
        "=== RUN: started x, finished y, elapsed 1s ===\n\n"
        "=== NEEDS REVIEW: some_new_category ===\n"
        "=== A brand new check. ===\n=== POSSIBLE DATA LOSS ===\n=== 2 record(s) ===\n"
        "\trecord 1 (a)\tfirst\n\tweird line with no tab structure\n",
        encoding="utf-8",
    )
    lines = [e[1] for e in pdf_log._events(str(log)) if e[0] == "line"]
    assert lines == ["\trecord 1 (a)\tfirst", "\tweird line with no tab structure"]
    pdf, _ = pdf_log.write_pdf_log(str(log))
    assert os.path.exists(pdf)


def test_unicode_text_and_very_long_lines_do_not_break_it(tmp_path):
    log = tmp_path / "wide.log"
    log.write_text(
        "=== RUN: started x, finished y, elapsed 1s ===\n\n"
        "=== NOT FIXED: odd_text ===\n=== Text. ===\n=== NO DATA LOSS ===\n"
        "=== 1 record(s) ===\n"
        "\trecord 1 (a)\t" + "café 中文 " + "x" * 3000 + "\n",
        encoding="utf-8",
    )
    idx = pdf_log._scan(str(log), pdf_log._Fonts())
    assert idx.unsupported >= 2            # the two CJK characters
    pdf, pages = pdf_log.write_pdf_log(str(log))
    assert pages >= 2 and os.path.exists(pdf)


def test_failure_is_a_warning_not_an_error(tmp_path, monkeypatch, capsys):
    log = tmp_path / "x.log"
    log.write_text("=== RUN: a ===\n", encoding="utf-8")

    def boom(_):
        raise ImportError("no reportlab")

    monkeypatch.setattr(pdf_log, "write_pdf_log", boom)
    assert pdf_log.write_pdf_log_safe(str(log)) is None
    assert "reportlab is not installed" in capsys.readouterr().err

    monkeypatch.setattr(pdf_log, "write_pdf_log", lambda _: 1 / 0)
    assert pdf_log.write_pdf_log_safe(str(log)) is None
    assert "PDF log skipped" in capsys.readouterr().err
    assert cli.write_pdf_log_safe is pdf_log.write_pdf_log_safe


def test_removed_invalid_subfield_log_line_is_truncated():
    """A long discarded subfield is quoted as its first characters only,
    like every other field quote in the log -- not in full."""
    huge = "x" * 3000
    parsed = m.ParsedRecord(
        leader="0" * 24, entries=[],
        fields=[m.Field_("505", "00", [("A", huge), ("a", "kept")])],
    )
    (line,) = m.strip_invalid_subfield_codes(parsed)
    assert len(line) < 200
    assert "..." in line and huge not in line
    assert parsed.fields[0].subfields == [("a", "kept")]
