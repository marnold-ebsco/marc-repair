"""PDF rendering of a finished marc_repair text log.

The text log is the source of truth; this module reads it back and lays it
out as a PDF with the same name (extension aside): a run-summary page, a
clickable table of contents (kind -> category, with page numbers), a heading
per section, a PDF outline (bookmarks) and "Page X of Y" footers.

Why parse the finished log instead of hooking `write_log`: one parser covers
the bib, holdings and any future log writer. The format it relies on is the
one `report.write_log` documents (`=== KIND: category ===` header, wrapped
`=== description` lines, a data-loss call line, a `=== N record(s) ===` count
line, then entry lines). tests/test_pdf_log.py runs `write_log` and checks
this parser against its real output, so a format change fails a test instead
of silently producing a wrong PDF. Anything unrecognised is kept as plain
text, never dropped.

Layout runs in two passes over the same code (`_render`): a plan pass with no
canvas that records which page each heading lands on and the total page
count, then a draw pass that uses those numbers for the table of contents and
the footers. The log is streamed three times (index, plan, draw), never held
in memory, because a real log can run to tens of thousands of lines.
"""
from __future__ import annotations

import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Iterator

#: Directories searched for DejaVu (a Unicode font, so accented/Cyrillic/Greek
#: text in a log line shows correctly). Without it the PDF falls back to the
#: built-in Courier/Helvetica, which only cover Latin-1 -- anything else
#: becomes "?" (and the run summary says how many).
_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/dejavu-sans-mono-fonts",
    "/usr/share/fonts/dejavu-sans-fonts",
    "/usr/share/fonts/TTF",
    "/usr/local/share/fonts",
    "/Library/Fonts",
    "C:/Windows/Fonts",
)
_FONT_FILES = {
    "mono": "DejaVuSansMono.ttf",
    "mono_b": "DejaVuSansMono-Bold.ttf",
    "sans": "DejaVuSans.ttf",
    "sans_b": "DejaVuSans-Bold.ttf",
}

_KIND_TITLES = {
    "UNFIXABLE": "Unfixable",
    "NOT FIXED": "Not fixed",
    "FIXED/REQUIRES ATTENTION": "Fixed / requires attention",
    "NEEDS REVIEW": "Needs review",
    "DUPLICATE RECORDS": "Duplicate records",
    "INFORMATIONAL": "Informational",
    "INFORMATION ONLY": "Information only",
    "CHECKS PERFORMED WITH NO FINDINGS": "Checks performed with no findings",
}

_SECTION_RE = re.compile(r"^=== ([A-Z][A-Z /]*[A-Z]): (.+) ===$")
_NO_FINDINGS_RE = re.compile(r"^=== CHECKS PERFORMED WITH NO FINDINGS \((\d+)\) ===$")
_COUNT_RE = re.compile(r"^\d+ (record|instance)")
_LOSS_CALLS = ("DATA LOSS", "POSSIBLE DATA LOSS", "NO DATA LOSS")
_NO_FINDINGS_KIND = "CHECKS PERFORMED WITH NO FINDINGS"

# Page geometry (US Letter, points).
_PAGE_W, _PAGE_H = 612.0, 792.0
_LM = _RM = 36.0
_TOP = _PAGE_H - 46.0
_BOTTOM = 46.0
_BODY_SIZE = 7.5
_BODY_LEAD = 9.5
_WRAP_INDENT = "    "

# (r, g, b)
_LOSS_COLORS = {
    "DATA LOSS": (0.70, 0.10, 0.10),
    "POSSIBLE DATA LOSS": (0.75, 0.42, 0.00),
    "NO DATA LOSS": (0.10, 0.42, 0.20),
}
_GREY = (0.40, 0.40, 0.40)
_LINK = (0.05, 0.25, 0.60)


def kind_title(kind: str) -> str:
    return _KIND_TITLES.get(kind, kind.capitalize())


# -- parsing --------------------------------------------------------------

def _events(path: str) -> Iterator[tuple]:
    """Yield the log as ("run", [lines]), ("section", kind, category),
    ("nofind", n), ("desc", text), ("loss", text), ("count", text),
    ("line", text) and ("blank",) events, in file order."""
    state = "run"            # run -> (header -> body)*
    run: list[str] = []
    pending_blank = False
    run_sent = False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if state == "run" and line.startswith("=== RUN:"):
                run.append("Run:" + line[len("=== RUN:"):].removesuffix(" ==="))
                continue
            m = _SECTION_RE.match(line)
            nf = _NO_FINDINGS_RE.match(line)
            if m or nf:
                if not run_sent:
                    run_sent = True
                    yield ("run", _trim_blank(run))
                pending_blank = False
                if nf:
                    yield ("section", _NO_FINDINGS_KIND, None)
                    yield ("nofind", int(nf.group(1)))
                    state = "body"
                else:
                    yield ("section", m.group(1), m.group(2))
                    state = "header"
                continue
            if state == "run":
                run.append(line)
                continue
            if state == "header":
                if line.startswith("==="):
                    text = line[3:].strip()
                    if text.endswith("==="):
                        text = text[:-3].strip()
                    if text in _LOSS_CALLS:
                        yield ("loss", text)
                    elif _COUNT_RE.match(text):
                        yield ("count", text)
                        state = "body"
                    else:
                        yield ("desc", text)
                    continue
                # a header with no count line: treat the rest as body
                yield ("count", "")
                state = "body"
            if not line.strip():
                pending_blank = True
                continue
            if pending_blank:
                yield ("blank",)
                pending_blank = False
            yield ("line", line)
    if not run_sent:
        yield ("run", _trim_blank(run))


def _trim_blank(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


@dataclass
class _Section:
    kind: str
    category: str | None
    key: str
    count: str = ""
    loss: str = ""
    desc: str = ""


@dataclass
class _Index:
    run_lines: list[str] = field(default_factory=list)
    sections: list[_Section] = field(default_factory=list)
    unsupported: int = 0


# -- fonts ----------------------------------------------------------------

class _Fonts:
    """Registered font names plus a cleaner that swaps characters the font
    cannot draw for "?" (length-preserving, so layout never depends on it)."""

    def __init__(self) -> None:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        self.pdfmetrics = pdfmetrics
        found = {}
        for key, fname in _FONT_FILES.items():
            for d in _FONT_DIRS:
                p = os.path.join(d, fname)
                if os.path.exists(p):
                    found[key] = p
                    break
        self.supported: set[int] | None = None
        if len(found) == len(_FONT_FILES):
            try:
                for key, p in found.items():
                    name = f"MRPdf-{key}"
                    if name not in pdfmetrics.getRegisteredFontNames():
                        pdfmetrics.registerFont(TTFont(name, p))
                self.mono, self.mono_b = "MRPdf-mono", "MRPdf-mono_b"
                self.sans, self.sans_b = "MRPdf-sans", "MRPdf-sans_b"
                self.supported = set(pdfmetrics.getFont(self.mono).face.charToGlyph)
                self.supported &= set(pdfmetrics.getFont(self.sans).face.charToGlyph)
                self.supported &= set(pdfmetrics.getFont(self.sans_b).face.charToGlyph)
            except Exception:  # noqa: BLE001 - any font problem -> built-in fonts
                self.supported = None
        if self.supported is None:
            self.mono, self.mono_b = "Courier", "Courier-Bold"
            self.sans, self.sans_b = "Helvetica", "Helvetica-Bold"
        self.char_w = pdfmetrics.stringWidth("M", self.mono, _BODY_SIZE)
        self.cols = max(20, int((_PAGE_W - _LM - _RM) / self.char_w))

    def clean(self, text: str) -> str:
        if self.supported is None:
            return text.encode("cp1252", "replace").decode("cp1252", "replace")
        sup = self.supported
        if all(ord(c) in sup for c in set(text)):
            return text
        return "".join(c if ord(c) in sup else "?" for c in text)

    def count_unsupported(self, text: str) -> int:
        if self.supported is None:
            bad = {c for c in set(text) if not _cp1252_ok(c)}
        else:
            bad = {c for c in set(text) if ord(c) not in self.supported}
        return sum(text.count(c) for c in bad) if bad else 0


def _cp1252_ok(c: str) -> bool:
    try:
        c.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


# -- layout ---------------------------------------------------------------

class _Doc:
    """Cursor over the pages. `c` is None in the plan pass: every method
    still moves the cursor and records page marks, but draws nothing."""

    def __init__(self, c, fonts: _Fonts, label: str, total: int, marks_in: dict) -> None:
        self.c = c
        self.f = fonts
        self.label = label
        self.total = total
        self.marks_in = marks_in
        self.marks: dict[str, int] = {}
        self.page = 0
        self.y = _TOP
        self.cont: str | None = None
        self._open_page(first=True)

    # pages
    def _open_page(self, first: bool = False) -> None:
        if not first and self.c is not None:
            self._footer()
            self.c.showPage()
        self.page += 1
        self.y = _TOP
        if self.cont:
            self.set_font(self.f.sans, 7.5, _GREY)
            self.text(_LM, self.y, f"{self.cont} (continued)")
            self.y -= 14

    def new_page(self) -> None:
        self._open_page()

    def new_page_if_used(self) -> None:
        if self.y < _TOP - 0.5:
            self._open_page()

    def ensure(self, height: float) -> None:
        if self.y - height < _BOTTOM:
            self._open_page()

    def finish(self) -> None:
        if self.c is not None:
            self._footer()
            self.c.showPage()
            self.c.save()

    def _footer(self) -> None:
        c = self.c
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.setLineWidth(0.5)
        c.line(_LM, _BOTTOM - 8, _PAGE_W - _RM, _BOTTOM - 8)
        c.setFont(self.f.sans, 7.5)
        c.setFillColorRGB(*_GREY)
        c.drawString(_LM, _BOTTOM - 20, self.label)
        c.drawCentredString(_PAGE_W / 2, _BOTTOM - 20, "Contents")
        w = self.f.pdfmetrics.stringWidth("Contents", self.f.sans, 7.5)
        c.linkRect(
            "", "toc",
            (_PAGE_W / 2 - w / 2 - 2, _BOTTOM - 24, _PAGE_W / 2 + w / 2 + 2, _BOTTOM - 12),
            relative=0, thickness=0,
        )
        c.drawRightString(_PAGE_W - _RM, _BOTTOM - 20, f"Page {self.page} of {self.total}")

    # drawing primitives (no-ops in the plan pass)
    def set_font(self, name: str, size: float, rgb=(0, 0, 0)) -> None:
        if self.c is not None:
            self.c.setFont(name, size)
            self.c.setFillColorRGB(*rgb)

    def text(self, x: float, y: float, s: str) -> None:
        if self.c is not None:
            self.c.drawString(x, y, self.f.clean(s))

    def rtext(self, x: float, y: float, s: str) -> None:
        if self.c is not None:
            self.c.drawRightString(x, y, self.f.clean(s))

    def rule(self, y: float) -> None:
        if self.c is not None:
            self.c.setStrokeColorRGB(0.6, 0.6, 0.6)
            self.c.setLineWidth(0.8)
            self.c.line(_LM, y, _PAGE_W - _RM, y)

    def mark(self, key: str, title: str | None, level: int) -> None:
        """Record that `key` starts here; in the draw pass also add the PDF
        destination and (when titled) an outline entry."""
        self.marks[key] = self.page
        if self.c is not None:
            self.c.bookmarkHorizontalAbsolute(key, self.y + 14)
            if title:
                self.c.addOutlineEntry(self.f.clean(title), key, level=level, closed=False)

    def link(self, key: str, x0: float, y0: float, x1: float, y1: float) -> None:
        if self.c is not None:
            self.c.linkRect("", key, (x0, y0, x1, y1), relative=0, thickness=0)

    def page_of(self, key: str) -> int:
        return self.marks_in.get(key, 0)

    # text helpers
    def wrap_proportional(self, s: str, font: str, size: float, width: float) -> list[str]:
        from reportlab.lib.utils import simpleSplit

        return simpleSplit(self.f.clean(s), font, size, width) or [""]

    def body_line(self, raw: str) -> None:
        """One log line in monospace, wrapped at the page width."""
        s = raw[1:] if raw.startswith("\t") else raw
        s = s.replace("\t", "  ")
        if len(s) <= self.f.cols:
            parts = [s]
        else:
            parts = textwrap.wrap(
                s, width=self.f.cols, subsequent_indent=_WRAP_INDENT,
                break_long_words=True, break_on_hyphens=False,
                replace_whitespace=False, drop_whitespace=False,
            ) or [""]
        for part in parts:
            self.ensure(_BODY_LEAD)
            self.set_font(self.f.mono, _BODY_SIZE)
            self.text(_LM, self.y - _BODY_SIZE, part)
            self.y -= _BODY_LEAD


def _scan(path: str, fonts: _Fonts) -> _Index:
    """Pass 0: section list, run header, and a count of characters the PDF
    font cannot show -- all of which the front matter needs before layout."""
    idx = _Index()
    cur: _Section | None = None
    for ev in _events(path):
        kind = ev[0]
        if kind == "run":
            idx.run_lines = ev[1]
            idx.unsupported += sum(fonts.count_unsupported(s) for s in ev[1])
        elif kind == "section":
            cur = _Section(ev[1], ev[2], f"s{len(idx.sections)}")
            idx.sections.append(cur)
        elif kind == "nofind" and cur is not None:
            cur.count = f"{ev[1]} check(s)"
        elif kind == "desc" and cur is not None:
            cur.desc = (cur.desc + " " + ev[1]).strip()
        elif kind == "loss" and cur is not None:
            cur.loss = ev[1]
        elif kind == "count" and cur is not None:
            cur.count = ev[1]
        elif kind == "line":
            # tabs are drawn as spaces (see `_Doc.body_line`), so not counted
            idx.unsupported += fonts.count_unsupported(ev[1].replace("\t", " "))
    return idx


def _front(doc: _Doc, idx: _Index, log_name: str) -> None:
    f = doc.f
    width = _PAGE_W - _LM - _RM
    doc.mark("top", "Run summary", 0)
    doc.set_font(f.sans_b, 18)
    doc.text(_LM, doc.y - 18, "marc_repair log")
    doc.y -= 26
    doc.set_font(f.sans, 9, _GREY)
    doc.text(_LM, doc.y - 9, log_name)
    doc.y -= 22
    doc.set_font(f.sans_b, 12)
    doc.text(_LM, doc.y - 12, "Run summary")
    doc.y -= 18
    for line in idx.run_lines:
        doc.body_line(line)
    if idx.unsupported:
        doc.y -= 6
        note = (
            f"{idx.unsupported} character(s) in this log are not available in the PDF "
            "font and are shown as '?'. The .log file has the exact text."
        )
        for part in doc.wrap_proportional(note, f.sans, 8, width):
            doc.ensure(11)
            doc.set_font(f.sans, 8, _GREY)
            doc.text(_LM, doc.y - 8, part)
            doc.y -= 11
    doc.y -= 12
    doc.ensure(60)
    doc.mark("toc", "Contents", 0)
    doc.set_font(f.sans_b, 12)
    doc.text(_LM, doc.y - 12, "Contents")
    doc.y -= 22

    cur_kind = None
    right = _PAGE_W - _RM
    for sec in idx.sections:
        if sec.kind != cur_kind:
            cur_kind = sec.kind
            doc.ensure(30)
            doc.y -= 6
            title = kind_title(sec.kind)
            doc.set_font(f.sans_b, 10, _LINK)
            doc.text(_LM, doc.y - 10, title)
            doc.rtext(right, doc.y - 10, str(doc.page_of("k:" + sec.kind)))
            doc.link("k:" + sec.kind, _LM, doc.y - 12, right, doc.y + 2)
            doc.y -= 15
        if sec.category is None:
            continue
        doc.ensure(13)
        doc.set_font(f.sans, 9, _LINK)
        doc.text(_LM + 14, doc.y - 9, sec.category)
        doc.set_font(f.sans, 8, _GREY)
        doc.rtext(right - 40, doc.y - 9, sec.count.split(" - ")[0])
        doc.set_font(f.sans, 9, _LINK)
        doc.rtext(right, doc.y - 9, str(doc.page_of(sec.key)))
        doc.link(sec.key, _LM + 14, doc.y - 11, right, doc.y + 2)
        doc.y -= 13


def _category_block(doc: _Doc, sec: _Section, level: int = 1) -> None:
    f = doc.f
    width = _PAGE_W - _LM - _RM
    desc_lines = doc.wrap_proportional(sec.desc, f.sans, 8.5, width) if sec.desc else []
    doc.cont = None
    doc.ensure(20 + 11 * len(desc_lines) + 16 + 3 * _BODY_LEAD)
    doc.y -= 6
    doc.mark(sec.key, sec.category, level)
    doc.set_font(f.sans_b, 11)
    doc.text(_LM, doc.y - 11, sec.category or "")
    doc.y -= 17
    for part in desc_lines:
        doc.set_font(f.sans, 8.5)
        doc.text(_LM, doc.y - 8.5, part)
        doc.y -= 11
    doc.y -= 2
    x = _LM
    if sec.loss:
        doc.set_font(f.sans_b, 8.5, _LOSS_COLORS.get(sec.loss, (0, 0, 0)))
        doc.text(x, doc.y - 8.5, sec.loss)
        x += f.pdfmetrics.stringWidth(sec.loss, f.sans_b, 8.5) + 10
    doc.set_font(f.sans, 8.5, _GREY)
    doc.text(x, doc.y - 8.5, sec.count)
    doc.y -= 14
    doc.cont = sec.category


def _body(doc: _Doc, idx: _Index, path: str) -> None:
    f = doc.f
    doc.new_page()
    cur_kind = None
    sec_no = -1
    sec: _Section | None = None
    header_open = False
    for ev in _events(path):
        kind = ev[0]
        if kind == "run":
            continue
        if kind == "section":
            sec_no += 1
            sec = idx.sections[sec_no]
            header_open = sec.category is not None
            if sec.kind != cur_kind:
                cur_kind = sec.kind
                doc.cont = None
                doc.new_page_if_used()
                title = kind_title(sec.kind)
                doc.mark("k:" + sec.kind, title, 0)
                doc.set_font(f.sans_b, 16)
                doc.text(_LM, doc.y - 16, title)
                doc.y -= 22
                doc.rule(doc.y)
                doc.y -= 8
            if sec.category is None:
                doc.mark(sec.key, None, 1)
                doc.cont = kind_title(sec.kind)
            continue
        if kind in ("nofind", "desc", "loss"):
            continue
        if kind == "count":
            if header_open and sec is not None:
                _category_block(doc, sec)
                header_open = False
            continue
        if header_open and sec is not None:      # entry before a count line
            _category_block(doc, sec)
            header_open = False
        if kind == "blank":
            doc.y -= _BODY_LEAD / 2
        elif kind == "line":
            doc.body_line(ev[1])


def _render(c, fonts: _Fonts, idx: _Index, path: str, label: str,
            total: int, marks_in: dict) -> _Doc:
    doc = _Doc(c, fonts, label, total, marks_in)
    _front(doc, idx, os.path.basename(path))
    _body(doc, idx, path)
    doc.finish()
    return doc


# -- public ---------------------------------------------------------------

def pdf_path_for(log_path: str) -> str:
    """The PDF's path: the log's path with the extension swapped for .pdf."""
    return os.path.splitext(log_path)[0] + ".pdf"


def write_pdf_log(log_path: str) -> tuple[str, int]:
    """Render `log_path` as a PDF next to it. Returns (pdf_path, page_count).
    Raises ImportError when reportlab is missing; written via a temp file so
    a failure never leaves a half-written PDF behind."""
    from reportlab.pdfgen import canvas as rl_canvas

    fonts = _Fonts()
    idx = _scan(log_path, fonts)
    label = os.path.basename(pdf_path_for(log_path))
    plan = _render(None, fonts, idx, log_path, label, 0, {})
    pdf_path = pdf_path_for(log_path)
    tmp = pdf_path + ".tmp"
    try:
        c = rl_canvas.Canvas(
            tmp, pagesize=(_PAGE_W, _PAGE_H), pageCompression=1, invariant=1,
        )
        c.setTitle(label)
        c.setCreator("marc_repair")
        _render(c, fonts, idx, log_path, label, plan.page, plan.marks)
        os.replace(tmp, pdf_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return pdf_path, plan.page


def write_pdf_log_safe(log_path: str) -> str | None:
    """`write_pdf_log` for the CLI: any failure is a warning on stderr, never
    an error -- the text log is already written and the run must not fail
    over its PDF copy."""
    try:
        pdf_path, pages = write_pdf_log(log_path)
    except ImportError:
        print(
            "PDF log skipped: reportlab is not installed "
            "(pip install reportlab, or pass --no-pdf-log)",
            file=sys.stderr,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - never fail a run over the PDF copy
        print(f"PDF log skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    print(f"PDF log written to {pdf_path} ({pages} page(s))", file=sys.stderr)
    return pdf_path
