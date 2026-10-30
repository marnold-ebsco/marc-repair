"""Record boundary detection, Mode 1/Mode 2 reconstruction, streaming, assembly and
MARC-8 transcoding for marc_repair. Imports marc_repair_common and marc_repair_qa."""

from __future__ import annotations

import codecs
import io
import json
import os
import re
import unicodedata
from contextlib import redirect_stderr
from typing import Callable, Iterator

from marc_repair_common import (
    Ambiguous,
    CONTROL_TAG_LIMIT,
    DirEntry,
    FALLBACK_CODES,
    FIELDTERM,
    Field_,
    KNOWN_SUBFIELD_CODES,
    MAX_DIRECTORY_SCAN_LEN,
    ParsedRecord,
    RECTERM,
    RepairError,
    SUBFIELD,
)
from marc_repair_qa import (
    _record_lccn,
    _record_oclc,
)


# ---------------------------------------------------------------------------
# Record boundary + leader/directory detection
# ---------------------------------------------------------------------------

def find_record_starts(text: str) -> list[int]:
    """Find offsets of genuine MARC leaders in concatenated corrupted text.

    A real leader has "22" at positions 10-11 (indicator/subfield-code counts,
    fixed by the MARC21 spec) and "4500" at positions 20-23 (entry map, also
    fixed). Directory digits can accidentally spell "4500" too, so we require
    both anchors plus a digit record-length at position 0-4 to avoid false
    positives.
    """
    starts = []
    for m in re.finditer("4500", text):
        p = m.start() - 20
        if p < 0:
            continue
        leader = text[p:p + 24]
        if len(leader) != 24:
            continue
        if not leader[0:5].isdigit():
            continue
        if leader[10:12] != "22":
            continue
        starts.append(p)
    return starts


def _looks_like_leader(text: str, pos: int) -> bool:
    """Looser leader check than `find_record_starts` -- digit length and
    "22" indicator count, but NOT requiring a literal "4500" entry map.
    Used for streaming, where scanning the whole file with a regex up front
    isn't an option: this doubles as finding the very first leader and as a
    resync point after an unparseable record, and being tolerant of a
    corrupted entry map (the real defect that motivated `iter_repair`'s
    self-determining redesign in the first place) means it also catches
    leaders `find_record_starts` would miss entirely.
    """
    leader = text[pos:pos + 24]
    return len(leader) == 24 and leader[0:5].isdigit() and leader[10:12] == "22"


def _find_next_leader_from(text: str, from_pos: int, limit: int | None = None) -> int | None:
    """First position >= from_pos that looks like a leader (see
    `_looks_like_leader`), or None if none found in text[from_pos:limit]."""
    end = len(text) if limit is None else min(limit, len(text))
    for p in range(from_pos, end):
        if _looks_like_leader(text, p):
            return p
    return None


def parse_directory(rest: str) -> tuple[int, list[DirEntry]]:
    """Try skip offsets 0..3 chars after the leader to find where the intact
    12-char (tag+length+start) directory entries begin, tolerating however many
    stray/mangled bytes replaced the original terminator(s). Returns
    (skip_used, entries).

    Only the length+start portion (the last 9 characters) of each 12-char
    entry is required to be digits -- the tag itself is deliberately NOT
    digit-checked here, so a directory entry with a corrupted, non-numeric
    tag (e.g. "24A" from a flipped byte) still parses instead of making
    this function think the directory ended early right before it (which
    would misparse everything from that field onward). The tag's numeric
    validity is checked and fixed separately -- see `fix_invalid_tags`.

    The start/length cumulative check is applied as each chunk is read,
    not after greedily grabbing every digit-shaped chunk first -- a real
    directory terminator (0x1E) followed by a numeric-looking field (e.g.
    001 content like "on1000049630") can otherwise look like one more
    valid 12-char entry purely by digit-shape coincidence, silently
    swallowing the real terminator and its first field's data into a
    bogus final entry. Checking the cumulative math immediately stops
    there instead, keeping the genuinely consistent entries found so far
    rather than discarding all of them over one coincidental false match.
    """
    best = None
    for skip in range(0, 4):
        chunk_stream = rest[skip:]
        pos = 0
        entries: list[DirEntry] = []
        cum = 0
        while True:
            chunk = chunk_stream[pos:pos + 12]
            if len(chunk) < 12 or not chunk[3:12].isdigit():
                break
            # A real directory entry is always plain ASCII tag+digits --
            # FIELDTERM/SUBFIELD are reserved delimiter bytes that never
            # appear inside one. Without this check, a chunk starting
            # *at* the real directory terminator (so chunk[0] is that
            # 0x1E, "tag" is actually the first 2 bytes of field 001's
            # content, and "length"/"start" are drawn from that content's
            # own digits) can still pass the digit check above and even
            # coincidentally satisfy `start == cum` below -- a real
            # production record hit exactly that: an OCLC number like
            # "on1341033027" right after the terminator produced a
            # phantom 40th entry whose bogus start (3302) coincidentally
            # equalled the true cumulative length of the 39 real entries,
            # desyncing every field boundary from there on.
            if FIELDTERM in chunk or SUBFIELD in chunk:
                break
            length, start = int(chunk[3:7]), int(chunk[7:12])
            if start != cum:
                # This entry's own declared start wasn't touched by
                # whatever corrupted the *previous* entry's length --
                # ISO 2709's directory length slot is only 4 digits (max
                # 9999), so a field whose real content is longer than
                # that gets written with a truncated/wrapped length no
                # matter what produced the file. If this gap is fully
                # explained by the previous entry actually being that
                # much longer, recover its true length instead of
                # abandoning every entry after it (which otherwise
                # silently drops the rest of the record -- see
                # truncated_oversized_field, which is what actually
                # discards the one oversized field once a real
                # delimiter-bounded length confirms it).
                recovered_length = (
                    entries[-1].length + (start - cum) if entries and start > cum else 0
                )
                if recovered_length > 9999:
                    prev = entries[-1]
                    entries[-1] = DirEntry(prev.tag, recovered_length, prev.start)
                    cum = start
                else:
                    break
            entries.append(DirEntry(chunk[0:3], length, start))
            cum += length
            pos += 12
        if not entries:
            continue
        if best is None or len(entries) > len(best[1]):
            best = (skip, entries)
        # a clean parse -- the entries ended exactly at the real
        # directory terminator, meaning there's no stray/mangled byte
        # anywhere in the directory for a larger skip to route around --
        # means no other skip can ever do better, so skip trying them.
        # This is the common case (most records aren't corrupted at
        # all); the full 4-skip search only matters when this fails.
        if chunk_stream[pos:pos + 1] == FIELDTERM:
            break
    if best is None:
        raise RepairError("could not locate a consistent directory after the leader")
    return best


# ---------------------------------------------------------------------------
# Subfield reconstruction
# ---------------------------------------------------------------------------

def valid_codes_for(tag: str) -> set[str]:
    return KNOWN_SUBFIELD_CODES.get(tag, FALLBACK_CODES)


MAX_SPLITS_PER_FIELD = 25


MAX_SUBFIELDS_PER_FIELD = 30  # real MARC fields don't realistically have more


class Budget:
    """A single work counter shared across the *entire* record search --
    every recursive step anywhere (subfield-split search, field-candidate
    generation, whole-record backtracking) draws from the same pool. A long
    free-text field with common-letter subfield codes (245's $a/$b/$c, say)
    can look "ambiguous" in a combinatorial sense even though only one split
    is real, and per-call budgets don't compose: capping each call locally
    still lets total work multiply across nested calls. One shared counter
    bounds the *total* work for a record, however it's spent, so a
    pathological field reliably degrades to "give up, needs an override"
    instead of a multi-minute hang.
    """

    __slots__ = ("remaining",)

    def __init__(self, total: int):
        self.remaining = total

    def take(self) -> bool:
        """Spend one unit; returns False once exhausted (sticky -- stays
        exhausted, never goes negative in any way that matters)."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def exhausted(self) -> bool:
        return self.remaining <= 0


MAX_STEPS_PER_SPLIT_CALL = 30_000


def all_splits(
    content: str,
    tag: str,
    n_subfields: int,
    budget: Budget,
    cap: int = MAX_SPLITS_PER_FIELD,
    local_step_limit: int = MAX_STEPS_PER_SPLIT_CALL,
) -> list[list[tuple[str, str]]]:
    """Every way to split `content` (indicator-stripped field body, no
    delimiters) into exactly `n_subfields` (code, data) pairs, where every
    split point lands on a character in the tag's known valid-code set.

    Returns a possibly-empty list; does not raise. Callers combine this with
    a whole-record search (see `repair_record_stripped`) because a single
    field in isolation is almost always satisfiable by *some* split -- e.g.
    one big subfield is always "valid" if the first character happens to be
    a known code -- so uniqueness can only be judged across the whole
    record, not field-by-field.

    Stops after finding `cap` solutions, or after `local_step_limit` steps
    *of this call*, or once the shared `budget` (see `Budget`) runs out --
    whichever comes first. The local limit matters on top of the shared one:
    without it, a single combinatorially-rich (tag, subfield-count)
    combination -- e.g. field 245 with a permissive code set like "abc" over
    a long title -- could burn the *entire* shared budget by itself, when in
    reality most of that budget should go toward trying other candidate
    subfield-counts and other fields in the record. A capped or
    budget-exhausted result is treated the same downstream as "can't tell --
    needs a manual override," never as a false "unique."
    """
    codes = valid_codes_for(tag)
    n = len(content)
    solutions: list[list[tuple[str, str]]] = []
    ran_out = [False]
    local_steps = [0]

    def dfs(pos: int, remaining: int, acc: list[tuple[str, str]]):
        if len(solutions) >= cap or local_steps[0] >= local_step_limit:
            return
        local_steps[0] += 1
        if not budget.take():
            ran_out[0] = True
            return
        if remaining == 0:
            if pos == n:
                solutions.append(list(acc))
            return
        if pos >= n or content[pos] not in codes:
            return
        code = content[pos]
        for data_end in range(pos + 1, n + 1):
            if len(solutions) >= cap or budget.exhausted() or local_steps[0] >= local_step_limit:
                if budget.exhausted():
                    ran_out[0] = True
                return
            if remaining > 1 and data_end >= n:
                break  # need at least 1 more char left for the next code
            data = content[pos + 1:data_end]
            acc.append((code, data))
            dfs(data_end, remaining - 1, acc)
            acc.pop()

    dfs(0, n_subfields, [])
    if (ran_out[0] or local_steps[0] >= local_step_limit) and len(solutions) < 2:
        # ran out of shared or local budget without a clear answer -- report
        # as ambiguous (2 dummy-distinct entries) rather than risk a false
        # "unique" result
        solutions = solutions[:1] + [[("?", "budget exhausted, needs override")]]
    return solutions


def _stripped_len_for(declared_length: int, n_subfields: int) -> int:
    """stripped_length (indicators + code+data, no delimiters/terminator) given
    declared_length and a candidate subfield count."""
    return declared_length - n_subfields - 1


def field_candidates(
    entry: DirEntry,
    blob: str,
    pos: int,
    override: tuple[str, list[tuple[str, str]]] | None,
    budget: Budget,
) -> list[tuple[Field_, int]]:
    """Every way this field could plausibly consume blob[pos:], paired with the
    resulting new position. Control fields and overridden fields yield at most
    one candidate; free data fields yield one candidate per (subfield-count,
    split) combination that fits the declared length. Stops early (returning
    whatever's found so far) once `budget` runs out."""
    if entry.tag.isdigit() and int(entry.tag) < CONTROL_TAG_LIMIT:
        content_len = entry.length - 1
        content = blob[pos:pos + content_len]
        if len(content) != content_len:
            return []
        return [(Field_(entry.tag, None, None, content=content), pos + content_len)]

    if len(blob) - pos < 2:
        return []
    indicators = blob[pos:pos + 2]
    data_start = pos + 2

    if override is not None:
        ind_override, subfields = override
        content_len = _stripped_len_for(entry.length, len(subfields)) - 2
        if content_len < 0:
            return []
        content = blob[data_start:data_start + content_len]
        expected = "".join(c + d for c, d in subfields)
        if content != expected:
            return []
        return [
            (Field_(entry.tag, ind_override, subfields), data_start + content_len)
        ]

    candidates = []
    max_n = min(entry.length - 1, MAX_SUBFIELDS_PER_FIELD)
    for n in range(1, max_n + 1):
        if budget.exhausted():
            break
        content_len = _stripped_len_for(entry.length, n) - 2
        if content_len < n:
            break  # larger n only shrinks content_len further; no point continuing
        if content_len < 0:
            continue
        content = blob[data_start:data_start + content_len]
        if len(content) != content_len:
            continue
        for split in all_splits(content, entry.tag, n, budget):
            candidates.append(
                (Field_(entry.tag, indicators, split), data_start + content_len)
            )
    return candidates


# ---------------------------------------------------------------------------
# Mode 1: delimiters are intact, only the leader/directory lies
# ---------------------------------------------------------------------------

def _pad_short_indicators(content: str) -> tuple[str, int]:
    """A data field's content must start with exactly 2 indicator
    characters and then a subfield delimiter. If it instead starts with 0
    or 1 characters before the first delimiter (a common corruption --
    indicator byte(s) dropped), pad on the left with enough spaces to
    restore the expected 2-character indicator width, matching the fix a
    real-world MARC cleanup script uses (pad, don't guess real indicator
    values). Returns (possibly-padded content, spaces_added); spaces_added
    is 0 if nothing needed fixing (including when there's no delimiter in
    `content` at all, or already >= 2 characters precede it)."""
    first_delim = content.find(SUBFIELD)
    if 0 <= first_delim < 2:
        pad = 2 - first_delim
        return " " * pad + content, pad
    return content, 0


_MAX_FIELD_BYTES = 9999  # ISO 2709's 4-digit directory length slot, terminator included


# `ESC b <digit> ESC s` (and the superscript `ESC p` form): a one-character
# subscript/superscript run immediately reset to ASCII. One real GTU export
# wrapped nearly every character of a Hebrew 505 in a pair like this,
# bloating it past the 9999-byte limit.
_SPURIOUS_SCRIPT_PAIR_RE = re.compile(r"\x1b[bp][0-9]\x1bs")


# A G0 set designation, then the same set designated again right after an
# ASCII reset with nothing in between -- the reset/re-designation is a no-op.
_REDUNDANT_G0_RESET_RE = re.compile(r"(\x1b\((.)[^\x1b\x1f\x1e]*)\x1b\(B\x1b\(\2")


_TRAILING_PARTIAL_ESCAPE_RE = re.compile(r"\x1b[^\x1b\x1f\x1e]{0,2}\Z")


_ASCII_RESET = "\x1b(B"


def _field_byte_length(content: str, encoding: str) -> int:
    """Directory-slot length of a field: encoded bytes plus its terminator."""
    return len(content.encode(encoding, errors="surrogateescape")) + 1


def _merge_redundant_g0_resets(content: str) -> str:
    while True:
        merged = _REDUNDANT_G0_RESET_RE.sub(lambda mo: mo.group(1), content)
        if merged == content:
            return content
        content = merged


def _truncate_to_fit(content: str, encoding: str) -> str:
    """Cut `content` to at most 9998 encoded bytes (leaving room for the
    terminator), at the nicest boundary available: the last `--` title
    separator, else the last subfield delimiter, else a hard cut. Never
    splits a multibyte character or an ESC sequence, and re-asserts ASCII
    afterward if the kept text ended inside a non-ASCII escape state."""
    budget = _MAX_FIELD_BYTES - 1 - len(_ASCII_RESET)  # reserve a possible reset
    raw = content.encode(encoding, errors="surrogateescape")[:budget]
    cut = None
    for _ in range(4):
        try:
            cut = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    if cut is None:
        cut = raw.decode(encoding, errors="surrogateescape")
    # prefer a clean boundary, but only if it keeps most of what fit
    floor = len(cut) // 2
    sep = cut.rfind("--")
    if sep >= floor:
        cut = cut[:sep].rstrip() + " --" if cut[:sep].rstrip() else cut
    else:
        sub = cut.rfind(SUBFIELD)
        if sub >= floor:
            cut = cut[:sub]
    cut = _TRAILING_PARTIAL_ESCAPE_RE.sub("", cut)
    last_esc = max(cut.rfind(_ASCII_RESET), cut.rfind("\x1bs"))
    if ESCAPE in cut and cut.rfind(ESCAPE) > last_esc:
        cut += _ASCII_RESET
    return cut


def _shrink_oversized_content(content: str, encoding: str) -> tuple[str, int, bool]:
    """Bring an over-9999-byte field back under the directory limit.
    Tries, in order, stopping as soon as it fits: merging redundant G0
    reset/re-designate escapes (lossless), stripping spurious one-character
    subscript/superscript pairs (technically drops a digit, but is a
    producer artifact), merging again, and finally truncating. Returns
    (new_content, escape_pairs_stripped, truncated)."""
    stripped = 0
    content = _merge_redundant_g0_resets(content)
    if _field_byte_length(content, encoding) <= _MAX_FIELD_BYTES:
        return content, stripped, False
    stripped = len(_SPURIOUS_SCRIPT_PAIR_RE.findall(content))
    content = _merge_redundant_g0_resets(_SPURIOUS_SCRIPT_PAIR_RE.sub("", content))
    if _field_byte_length(content, encoding) <= _MAX_FIELD_BYTES:
        return content, stripped, False
    return _truncate_to_fit(content, encoding), stripped, True


def _read_intact_at(
    text: str,
    start: int,
    fix_bad_indicators: bool = False,
) -> tuple[ParsedRecord, int] | None:
    """Core of Mode 1, operating on an absolute position within a (possibly
    much larger) `text` rather than a pre-sliced single record. Determines
    the record's own true end from its real 0x1E terminators -- it never
    needs to know in advance where the record "should" stop, which matters
    because the next record's leader can itself be corrupted (e.g. a
    mangled entry-map "4500") and so undetectable by pattern-matching; a
    pre-slicing approach would then merge that next record's bytes into
    this one and silently lose them. Returns (ParsedRecord, end) where
    `end` is the absolute index right after the last field's terminator
    (NOT including a following record terminator, if any -- callers check
    for that separately), or None if this doesn't look like intact-delimiter
    data at all.

    If `fix_bad_indicators` is set, a field with fewer than 2 characters
    before its first subfield delimiter is padded (see
    `_pad_short_indicators`) rather than causing the whole record to bail
    out to Mode 2/UNRESOLVED; each correction is recorded on the returned
    record's `.indicator_fixes` as (tag, spaces_added, original_indicators,
    first_subfield_preview) so the caller can log it. Off by default at
    this function's level -- the CLI itself passes
    True by default (see --no-fix-bad-indicators to turn it off there).
    """
    leader = text[start:start + 24]
    if len(leader) != 24:
        return None
    rest_start = start + 24
    try:
        skip, entries = parse_directory(text[rest_start:rest_start + MAX_DIRECTORY_SCAN_LEN])
    except RepairError:
        return None

    dir_end = rest_start + skip + len(entries) * 12
    term_idx = text.find(FIELDTERM, dir_end)
    if term_idx == -1:
        return None  # no real terminator anywhere -- not this mode

    pos = term_idx + 1
    fields: list[Field_] = []
    indicator_fixes: list[tuple[str, int, str, str]] = []
    collapsed_oversized_fields: list[tuple[str, int, int, int, str]] = []
    truncated_oversized_fields: list[tuple[str, int, int, str]] = []
    encoding = "utf-8" if leader[9:10] == "a" else "latin-1"
    for entry in entries:
        end = text.find(FIELDTERM, pos)
        if end == -1:
            return None
        content = text[pos:end]
        if entry.length > _MAX_FIELD_BYTES:
            # A real, uncorrected directory entry can never have a
            # length over 9999 -- ISO 2709's length slot is only 4
            # digits. This only happens when `parse_directory` recovered
            # a field whose real, delimiter-bounded content is itself
            # that long (see its own comment); there's no way to declare
            # that length in a rebuilt directory either, so the field is
            # shrunk to fit (collapsed, else truncated) rather than left
            # to desync everything after it or break the rebuild.
            before = _field_byte_length(content, encoding)
            original = content
            content, stripped, truncated = _shrink_oversized_content(content, encoding)
            after = _field_byte_length(content, encoding)
            if truncated:
                truncated_oversized_fields.append(
                    (entry.tag, before, after, original[len(content):][:200])
                )
            else:
                collapsed_oversized_fields.append(
                    (entry.tag, before, after, stripped, content[:200])
                )
        # a non-numeric tag (e.g. "24A" from directory corruption) can't
        # be a real control tag -- those are always 001-009 -- so treat
        # it as a data field, the shape real corruption overwhelmingly
        # produces; fix_invalid_tags renames it to a valid 9XX tag later
        if entry.tag.isdigit() and int(entry.tag) < CONTROL_TAG_LIMIT:
            fields.append(Field_(entry.tag, None, None, content=content))
        else:
            if fix_bad_indicators:
                original_content = content
                content, spaces_added = _pad_short_indicators(content)
                if spaces_added:
                    first_delim = original_content.find(SUBFIELD)
                    original_indicators = (
                        original_content[:first_delim] if first_delim >= 0 else original_content
                    )
                    preview = (
                        original_content[first_delim:first_delim + 11]
                        if first_delim >= 0 else ""
                    )
                    indicator_fixes.append(
                        (entry.tag, spaces_added, original_indicators, preview)
                    )
            indicators, body = content[:2], content[2:]
            if body and SUBFIELD not in body:
                return None  # not actually delimited -- bail to Mode 2
            parts = body.split(SUBFIELD)
            if parts[0] != "":
                return None  # data before the first subfield delimiter
            subfields = [(p[0], p[1:]) for p in parts[1:] if p]
            fields.append(Field_(entry.tag, indicators, subfields))
        pos = end + 1

    parsed = ParsedRecord(leader=leader, entries=entries, fields=fields)
    parsed.indicator_fixes = indicator_fixes
    parsed.collapsed_oversized_fields = collapsed_oversized_fields
    parsed.truncated_oversized_fields = truncated_oversized_fields
    return parsed, pos


def read_intact_record(rec_text: str) -> ParsedRecord | None:
    """Try to read a single, already-isolated record's text (e.g. one
    already known to span exactly one record) whose field data still has
    real 0x1E/0x1F bytes, trusting those over the (possibly stale) declared
    lengths in the leader and directory. Returns None if the data doesn't
    actually look delimited this way, or if it does but doesn't consume
    `rec_text` exactly (a sign `rec_text` wasn't really just one record) --
    either way, the caller should fall back to `repair_record_stripped`.

    For parsing a whole file/stream where record boundaries aren't already
    known, use `iter_repair` instead, which determines each record's own
    boundary from its content and so isn't vulnerable to merging two
    records together when something (e.g. the next leader's entry-map
    bytes) prevents boundary detection up front.
    """
    result = _read_intact_at(rec_text, 0)
    if result is None:
        return None
    parsed, end = result
    if end == len(rec_text):
        return parsed
    if end + 1 == len(rec_text) and rec_text[end] == RECTERM:
        return parsed
    return None  # leftover text after the record -- rec_text wasn't just one record


# ---------------------------------------------------------------------------
# Mode 2: delimiters are gone, reconstruct from the directory's declared
# lengths via whole-record backtracking search
# ---------------------------------------------------------------------------

DEFAULT_WORK_BUDGET = 1_000_000


def _repair_stripped_at(
    text: str,
    start: int,
    end_hint: int | None = None,
    overrides: dict[int, tuple[str, list[tuple[str, str]]]] | None = None,
    node_budget: int = DEFAULT_WORK_BUDGET,
) -> tuple[ParsedRecord, int] | None:
    """Core of Mode 2, operating on an absolute position within a (possibly
    much larger) `text`.

    Unlike Mode 1, this genuinely cannot self-determine where the record
    ends: the directory's declared per-field lengths count the delimiter
    bytes that stripped text no longer has, so their sum overestimates the
    remaining span by an amount that depends on each field's subfield
    count -- exactly what the search below is trying to work out, making it
    circular to use as the boundary. So this needs `end_hint`, the best
    available guess at where the record ends (typically the next detected
    leader, or end-of-text) -- unless the record is fully self-contained in
    an already-isolated `rec_text` (see `repair_record_stripped`), in which
    case `end_hint` can be left as len(text).

    A field's subfield split can't be judged in isolation (see `all_splits`),
    so this does a whole-record search: try candidate splits field by field,
    and only accept a path once every field after it *also* parses, ending
    with the last field consuming the blob exactly at `end_hint`. If more
    than one such path exists the record is genuinely ambiguous and the
    first field where the two paths diverge is reported so the caller knows
    what to fix with an override.

    Returns None only if no consistent directory can be found at all.
    Otherwise always returns (ParsedRecord, end_hint) -- even when the split
    search itself is ambiguous or budget-exhausted (recorded in
    `.unresolved`), since `end_hint` is the caller's best boundary estimate
    regardless of whether the internal split was resolved.

    `node_budget` seeds a single `Budget` shared across every step of the
    search -- both this function's own field-by-field backtracking and every
    nested subfield-split search inside `field_candidates`/`all_splits`. That
    sharing matters: a long free-text field with common-letter codes can
    generate many structurally-valid splits on its own, and without a single
    pooled budget that cost multiplies across branches instead of adding up,
    turning a "give up after N steps" bound into an unbounded one.

    `overrides`: map of field-index (0-based, in directory order) -> (indicators,
    subfields) to force a specific split for fields the solver can't resolve
    on its own.
    """
    overrides = overrides or {}
    if end_hint is None:
        end_hint = len(text)
    leader = text[start:start + 24]
    if len(leader) != 24:
        return None
    rest_start = start + 24
    try:
        dir_scan_end = min(end_hint, rest_start + MAX_DIRECTORY_SCAN_LEN)
        skip, entries = parse_directory(text[rest_start:dir_scan_end])
    except RepairError:
        return None

    dir_end = rest_start + skip + len(entries) * 12
    blob = text[dir_end:end_hint]
    blob_len = len(blob)
    n_fields = len(entries)

    path: list[Field_ | None] = [None] * n_fields
    solutions: list[list[Field_]] = []
    deepest = {"idx": -1, "pos": 0}
    budget = Budget(node_budget)

    def dfs(idx: int, pos: int) -> None:
        if len(solutions) >= 2 or not budget.take():
            return
        if idx > deepest["idx"] or (idx == deepest["idx"] and pos > deepest["pos"]):
            deepest["idx"] = idx
            deepest["pos"] = pos
        if idx == n_fields:
            if pos == blob_len:
                solutions.append(list(path))
            return
        for f, new_pos in field_candidates(entries[idx], blob, pos, overrides.get(idx), budget):
            path[idx] = f
            dfs(idx + 1, new_pos)
            if len(solutions) >= 2 or budget.exhausted():
                return

    dfs(0, 0)

    parsed = ParsedRecord(leader=leader, entries=entries)
    if len(solutions) == 1:
        parsed.fields = solutions[0]
    elif len(solutions) == 0:
        idx = max(deepest["idx"], 0)
        reason = (
            "search exhausted (work budget hit -- record may be too ambiguous "
            "to auto-solve)"
            if budget.exhausted()
            else "no split satisfies the rest of the record"
        )
        parsed.unresolved.append((idx, entries[idx], blob[deepest["pos"]:], RepairError(reason)))
    else:
        first_diff = next(
            i for i in range(n_fields) if solutions[0][i] != solutions[1][i]
        )
        exc = Ambiguous(
            entries[first_diff].tag,
            "(see candidates)",
            [
                [(f.tag, f.indicators, f.subfields)]
                for f in (solutions[0][first_diff], solutions[1][first_diff])
            ],
        )
        parsed.unresolved.append((first_diff, entries[first_diff], "", exc))
    return parsed, end_hint


def repair_record_stripped(
    rec_text: str,
    overrides: dict[int, tuple[str, list[tuple[str, str]]]] | None = None,
    node_budget: int = DEFAULT_WORK_BUDGET,
) -> ParsedRecord:
    """Repair one already-isolated record's worth of corrupted text (leader +
    mangled directory + concatenated field data, no leading/trailing junk).
    See `_repair_stripped_at` for how the search itself works; this just
    unwraps it for callers that already have exactly one record's text.

    For a whole file/stream where record boundaries aren't already known,
    use `iter_repair` instead.
    """
    result = _repair_stripped_at(rec_text, 0, len(rec_text), overrides, node_budget)
    if result is None:
        raise RepairError("could not locate a consistent directory after the leader")
    parsed, _end = result
    return parsed


# ---------------------------------------------------------------------------
# Dispatcher: pick Mode 1 or Mode 2 automatically
# ---------------------------------------------------------------------------

def repair_record(
    rec_text: str,
    overrides: dict[int, tuple[str, list[tuple[str, str]]]] | None = None,
) -> ParsedRecord:
    """Repair one already-isolated record, trying the fast delimiter-intact
    path (Mode 1) first and falling back to the slower stripped-delimiter
    search (Mode 2) only when the data doesn't actually have real
    delimiters to trust.

    This assumes `rec_text` already spans exactly one record. For a whole
    file/stream, use `iter_repair` instead -- it determines each record's
    boundary from the record's own content rather than trusting that the
    caller sliced correctly, which matters when a later record's leader is
    itself corrupted in a way that makes it undetectable by pattern
    matching (see `iter_repair`'s docstring).
    """
    intact = read_intact_record(rec_text)
    if intact is not None:
        return intact
    return repair_record_stripped(rec_text, overrides)


OverridesByField = dict[int, tuple[str, list[tuple[str, str]]]]


def iter_repair(
    text: str, overrides: dict[int, OverridesByField] | None = None
) -> list[tuple[ParsedRecord, str]]:
    """Walk `text` sequentially, repairing one record at a time, and return
    each (ParsedRecord, original_text) pair in order.

    Unlike slicing the whole text up front via `find_record_starts` and
    processing each slice independently, this determines every record's own
    end from its own content (Mode 1's real delimiters, or Mode 2's
    directory-declared lengths) and advances by exactly that amount. That
    self-determination matters: `find_record_starts` finds the *next*
    record's leader by pattern-matching its literal "4500" entry-map bytes,
    but that field can itself be corrupted (seen in real exports) --
    pattern-matching then simply fails to find that leader, silently
    merging its record's bytes into the *previous* slice, whose Mode 1/2
    search would stop at its own correct end and never notice -- or read --
    the extra bytes appended after it. Determining the end from the current
    record's own content sidesteps needing to detect the next leader at all
    in the common case where the current record parses successfully.

    `find_record_starts` is used only to locate the very first record, and
    as a resync point when a record can't be parsed by either mode at all
    (no consistent directory found) -- there, the next known leader is the
    best available restart point, and everything between is reported
    UNRESOLVED as one chunk.
    """
    overrides = overrides or {}
    n = len(text)
    starts = find_record_starts(text)
    if not starts:
        raise RepairError("no MARC leader found in input text")

    results: list[tuple[ParsedRecord, str]] = []
    pos = starts[0]
    idx = 0
    resync_points = starts[1:] + [n]
    resync_i = 0

    while pos < n:
        while resync_i < len(resync_points) and resync_points[resync_i] <= pos:
            resync_i += 1
        next_known_leader = resync_points[resync_i] if resync_i < len(resync_points) else n

        intact = _read_intact_at(text, pos)
        if intact is not None:
            parsed, end = intact
        else:
            # Mode 2 can't self-determine its own end (see _repair_stripped_at),
            # so give it the best boundary estimate we have: the next leader
            # we already know about, or end-of-text if there isn't one.
            stripped = _repair_stripped_at(text, pos, next_known_leader, overrides.get(idx))
            parsed, end = stripped if stripped is not None else (None, None)

        if parsed is not None:
            if end < n and text[end] == RECTERM:
                end += 1
            results.append((parsed, text[pos:end]))
            pos = end
            idx += 1
            continue

        # neither mode could even find a consistent directory here -- resync
        # to the next known leader and report everything in between as one
        # unresolved chunk (better than getting stuck or guessing).
        chunk = text[pos:next_known_leader]
        placeholder = ParsedRecord(leader=text[pos:pos + 24], entries=[])
        placeholder.unresolved.append((
            0,
            DirEntry("???", len(chunk), 0),
            chunk[:200],
            RepairError("no consistent directory found for this record at all"),
        ))
        results.append((placeholder, chunk))
        pos = next_known_leader
        idx += 1

    return results


# ---------------------------------------------------------------------------
# Streaming: process a file too large to hold entirely in memory
# ---------------------------------------------------------------------------

DEFAULT_STREAM_CHUNK_SIZE = 32 * 1024 * 1024  # 32MB per read


# Real MARC records are essentially never more than a few hundred KB, even
# for outliers with a huge 505/520 note; 4MB of guaranteed lookahead before
# a record is ever attempted is a very comfortable margin, while still
# being tiny next to typical multi-GB inputs -- so memory use stays
# bounded by (chunk size + this margin), not by file size.
STREAM_LOOKAHEAD_MARGIN = 4 * 1024 * 1024


# How far past the currently-processed position to keep buffered text
# before compacting (discarding the consumed prefix) -- keeps the buffer
# from growing unboundedly across a huge file while avoiding compacting
# (an O(remaining buffer) copy) on every single record.
STREAM_COMPACT_THRESHOLD = 2 * DEFAULT_STREAM_CHUNK_SIZE


def detect_encoding(path: str, probe_size: int = 4 * 1024 * 1024) -> str:
    """Guess a whole file's encoding from its first `probe_size` bytes,
    without reading the rest -- "utf-8" if that probe decodes cleanly as
    UTF-8, else "latin-1" (which never fails and preserves every byte,
    appropriate for legacy MARC-8/ANSEL data; see `transcode_marc8_to_utf8`
    to actually convert it). A real file's encoding doesn't change partway
    through, so a multi-MB probe is enough to be confident either way.
    """
    with open(path, "rb") as fh:
        probe = fh.read(probe_size)
    try:
        probe.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def count_records(path: str, chunk_size: int = 16 * 1024 * 1024) -> int:
    """Count the records in a MARC file as fast as possible: stream it in
    big raw binary chunks and count the record-terminator byte (0x1D),
    one per record, with no decoding or parsing at all. 0x1D can't occur
    as a UTF-8 continuation byte (those are always >= 0x80) and Latin-1
    maps bytes 1:1, so this is exact for any encoding this tool handles.
    """
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                return total
            total += chunk.count(b"\x1d")


# MARC21 leader byte 6 ("type of record") codes for a bibliographic record
# (https://www.loc.gov/marc/bibliographic/bdleader.html).
BIB_LEADER_TYPES = set("acdefgijkmoprt")


# ...and for a holdings record instead
# (https://www.loc.gov/marc/holdings/hdleader.html): u=unknown, v=multipart
# item, x=single-part item, y=serial item.
HOLDINGS_LEADER_TYPES = set("uvxy")


def classify_bib_or_holdings(record_bytes: bytes) -> str:
    """Classify one raw MARC record (including its trailing record
    terminator) as "bib", "holdings", or "unclassified" from leader byte 6
    (the "type of record" position -- always plain ASCII per the MARC21
    spec, even in a MARC-8/legacy-encoded record), with no decoding or
    full parsing needed. "unclassified" covers both a genuinely different
    record type (e.g. "z" authority) and a record too short/corrupted to
    even have a readable leader byte -- never guessed at, so the caller
    can route it somewhere visible instead of silently mis-filing it.
    """
    if len(record_bytes) < 7:
        return "unclassified"
    b6 = record_bytes[6]
    if b6 >= 128:
        return "unclassified"
    ch = chr(b6)
    if ch in BIB_LEADER_TYPES:
        return "bib"
    if ch in HOLDINGS_LEADER_TYPES:
        return "holdings"
    return "unclassified"


def _sniff_record_types(path: str, sample_size: int = 1024 * 1024) -> tuple[int, int]:
    """Read the first `sample_size` bytes of `path` and classify (see
    `classify_bib_or_holdings`) however many whole records fall
    entirely within that sample -- a cheap, representative probe, not
    a full-file scan (same "probe, don't scan everything" approach as
    `detect_encoding`). Returns (n_bib, n_holdings); the last, likely
    truncated fragment in the sample is never counted, and neither is
    an "unclassified" record (e.g. an authority record, or one too
    short/corrupted to even have a readable leader byte 6).
    """
    with open(path, "rb") as fh:
        sample = fh.read(sample_size)
    n_bib = n_holdings = 0
    for raw in sample.split(RECTERM.encode())[:-1]:
        cls = classify_bib_or_holdings(raw)
        if cls == "bib":
            n_bib += 1
        elif cls == "holdings":
            n_holdings += 1
    return n_bib, n_holdings


def split_bib_holdings(
    path: str,
    bib_path: str,
    holdings_path: str,
    unclassified_path: str,
    chunk_size: int = 16 * 1024 * 1024,
    on_progress: Callable[[int], None] | None = None,
    on_record: Callable[[int], None] | None = None,
) -> dict[str, int]:
    """Split a MARC file into separate bib/holdings/unclassified files by
    each record's leader byte 6 alone, streaming raw bytes with no
    decoding or repair -- every record is copied through byte-for-byte,
    untouched, so this works regardless of any structural corruption
    elsewhere in the record (the leader is the only part inspected).
    `unclassified_path`'s file is only created if at least one record
    actually needs it, so a clean two-way split doesn't leave a stray
    empty file behind.

    `on_progress`, if given, is called after every chunk read from disk
    with total bytes read so far (see `iter_repair_stream`). `on_record`,
    if given, is called after every record is classified and written,
    with the running total record count so far -- both exist purely so a
    caller can show liveness on a large file; neither affects the split.

    Returns counts: {"bib": n, "holdings": n, "unclassified": n}.
    """
    counts = {"bib": 0, "holdings": 0, "unclassified": 0}
    unclassified_fh = None
    # Compact (drop the already-consumed prefix of `buf`) only once this
    # much of it has been consumed, rather than on every single record --
    # slicing `buf` is itself an O(len(buf)) copy, so doing it per record
    # while `buf` still holds most of a multi-MB chunk made this whole
    # function accidentally quadratic in chunk size (a real, measured
    # slowdown on a multi-GB file). A `pos` cursor advances through `buf`
    # in between compactions instead.
    compact_threshold = 2 * chunk_size
    with open(bib_path, "wb") as bib_fh, open(holdings_path, "wb") as holdings_fh:
        try:
            with open(path, "rb") as in_fh:
                buf = b""
                pos = 0
                while True:
                    chunk = in_fh.read(chunk_size)
                    if on_progress is not None:
                        on_progress(in_fh.tell())
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        idx = buf.find(b"\x1d", pos)
                        if idx == -1:
                            break
                        record = buf[pos:idx + 1]
                        pos = idx + 1
                        kind = classify_bib_or_holdings(record)
                        counts[kind] += 1
                        if kind == "bib":
                            bib_fh.write(record)
                        elif kind == "holdings":
                            holdings_fh.write(record)
                        else:
                            if unclassified_fh is None:
                                unclassified_fh = open(unclassified_path, "wb")
                            unclassified_fh.write(record)
                        if on_record is not None:
                            on_record(counts["bib"] + counts["holdings"] + counts["unclassified"])
                    if pos > compact_threshold:
                        buf = buf[pos:]
                        pos = 0
                remainder = buf[pos:]
                if remainder:
                    # trailing bytes with no terminator -- not a real
                    # record (every genuine MARC record ends in 0x1D); no
                    # safe way to classify or drop it, so it goes to
                    # unclassified too rather than being silently lost.
                    counts["unclassified"] += 1
                    if unclassified_fh is None:
                        unclassified_fh = open(unclassified_path, "wb")
                    unclassified_fh.write(remainder)
                    if on_record is not None:
                        on_record(counts["bib"] + counts["holdings"] + counts["unclassified"])
        finally:
            if unclassified_fh is not None:
                unclassified_fh.close()
    return counts


ESCAPE = "\x1b"


def _find_004_issues(count: int) -> list[tuple[str, str]]:
    """Categorize a holdings record's 004 (Control Number -- the
    associated bibliographic record's own control number, MARC21
    Holdings format's linking mechanism back to its bib record) field
    count. Detect-only in both directions: a missing 004 can't be
    safely invented -- there's no way to know which bib record it
    should point to -- and more than one isn't necessarily wrong (a
    holdings record can legitimately link to more than one bib
    record), so it's surfaced for awareness rather than trimmed.
    categories: "holdings_missing_004" (none found -- lands in NOT
    FIXED, the default for an unregistered fixed=False category, see
    `_section_for`), "holdings_multiple_004" (more than one found --
    registered in `_INFORMATIONAL` below, since it's not necessarily a
    defect)."""
    if count == 0:
        return [(
            "holdings_missing_004",
            "record has no 004 field (Control Number linking to the "
            "bibliographic record)",
        )]
    if count > 1:
        return [("holdings_multiple_004", f"record has {count} 004 field(s)")]
    return []


def check_holdings_record(rec_text: str, encoding: str) -> tuple[list[tuple[str, str]], str]:
    """Read-only structural/content checks for one already-isolated
    holdings record's text -- the same categories of defect this tool
    detects (and, for bib records -- and now holdings records too, see
    `repair_holdings_records` -- fixes) elsewhere: bad length, bad
    directory, missing 008, bad indicators, invalid subfield codes, and
    MARC-8 escape sequences. Nothing here is modified; this function
    itself stays a pure detector -- the CLI's `--split-bib-holdings`
    now calls `repair_holdings_records` instead of this for its
    holdings step, but this remains available as a standalone read-only
    check.

    Returns (issues, record_id): issues is a list of (category, detail)
    pairs suitable for `LogEntry`; record_id is field 001's content if
    present, else "".
    """
    issues: list[tuple[str, str]] = []
    leader = rec_text[:24]
    if len(leader) != 24:
        return [("holdings_bad_leader", "record too short to contain a leader")], ""

    declared = leader[0:5]
    actual = len(rec_text.encode(encoding))
    if not declared.isdigit() or int(declared) != actual:
        issues.append((
            "holdings_bad_length",
            f"bad length (leader declares {declared!r}, actual record is {actual} bytes)",
        ))

    if ESCAPE in rec_text:
        issues.append((
            "holdings_escape_sequence",
            "contains an ESC (0x1B) byte -- possible unconverted MARC-8 escape sequence",
        ))

    try:
        skip, entries = parse_directory(rec_text[24:24 + MAX_DIRECTORY_SCAN_LEN])
    except RepairError:
        issues.append((
            "holdings_bad_directory",
            "bad directory (no consistent directory found after the leader)",
        ))
        return issues, ""

    if not any(e.tag == "008" for e in entries):
        issues.append(("holdings_missing_008", "missing 008 field"))

    issues.extend(_find_004_issues(sum(1 for e in entries if e.tag == "004")))

    dir_end = 24 + skip + len(entries) * 12
    pos = rec_text.find(FIELDTERM, dir_end)
    record_id = ""
    if pos == -1:
        issues.append(("holdings_bad_directory", "directory terminator not found"))
        return issues, record_id
    pos += 1
    for entry in entries:
        end = rec_text.find(FIELDTERM, pos)
        if end == -1:
            issues.append((
                "holdings_bad_directory", f"={entry.tag}: field terminator not found"
            ))
            break
        content = rec_text[pos:end]
        if entry.tag.isdigit() and int(entry.tag) < CONTROL_TAG_LIMIT:
            if entry.tag == "001" and not record_id:
                record_id = content
            pos = end + 1
            continue
        _, spaces_needed = _pad_short_indicators(content)
        if spaces_needed:
            issues.append((
                "holdings_bad_indicators",
                f"={entry.tag}: bad indicators ({spaces_needed} character(s) "
                "missing before the first subfield)",
            ))
        body = content[2:]
        for part in body.split(SUBFIELD)[1:]:
            if not part:
                continue
            code, data = part[0], part[1:]
            if code not in FALLBACK_CODES:
                issues.append((
                    "holdings_invalid_subfield_code",
                    f"={entry.tag}: invalid subfield code {code!r}",
                ))
            elif not data:
                issues.append((
                    "holdings_null_identifier",
                    f"={entry.tag}: subfield ${code} has no data (null identifier)",
                ))
        pos = end + 1
    return issues, record_id


def iter_repair_stream(
    path: str,
    overrides: dict[int, OverridesByField] | None = None,
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
    on_progress: Callable[[int], None] | None = None,
    fix_bad_indicators: bool = False,
) -> Iterator[tuple[ParsedRecord, str]]:
    """Like `iter_repair`, but reads `path` incrementally in `chunk_size`
    pieces instead of loading the whole file into memory first -- for
    inputs too large to comfortably hold as a single decoded string (tens
    of millions of records, multi-GB files). Memory use stays bounded by
    roughly `chunk_size` plus one record's worth of parsed data at a time,
    regardless of total file size.

    This reuses the exact same self-determining parsing primitives as
    `iter_repair` (`_read_intact_at`, `_repair_stripped_at`) -- they already
    operate on an absolute position within whatever text they're given, so
    the only new work here is managing a sliding buffer: keep enough
    lookahead buffered that a record's true end can always be found (or we
    can honestly conclude we're at end-of-file), and periodically discard
    the already-consumed prefix so the buffer doesn't grow to the size of
    the whole file.

    `on_progress`, if given, is called after every chunk actually read from
    disk with the total bytes read so far (via the file handle's own
    `tell()`, so it's exact) -- a caller can use this to show a progress
    indicator without this function needing to know anything about how
    that's displayed.
    """
    overrides = overrides or {}
    encoding = detect_encoding(path)
    # errors="surrogateescape" rather than strict: real exports occasionally
    # have a single corrupted byte that isn't valid UTF-8 (seen in practice --
    # a leader's fixed "4500" entry-map constant with one byte replaced by
    # 0x92) even though the file as a whole is genuinely UTF-8 (which is why
    # `detect_encoding`'s probe didn't already route the whole file to
    # latin-1). Surrogate-escaping that byte instead of raising loses nothing
    # (it round-trips 1:1 back to the original byte on re-encode) and lets
    # parsing continue -- it only looks for real delimiters, never byte
    # validity -- so the usual repair logic (e.g. assemble_marc's hardcoded
    # "4500", never copied through from the original leader) can fix it same
    # as any other corrupted leader byte, instead of the whole run dying.
    decoder = (
        codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
        if encoding == "utf-8" else None
    )

    buf = ""
    pos = 0
    idx = 0
    at_eof = False

    with open(path, "rb") as fh:
        def fill() -> None:
            nonlocal buf, at_eof
            raw = fh.read(chunk_size)
            if on_progress is not None:
                on_progress(fh.tell())
            if not raw:
                if decoder is not None:
                    buf += decoder.decode(b"", final=True)
                at_eof = True
                return
            buf += decoder.decode(raw) if decoder is not None else raw.decode("latin-1")

        def ensure_lookahead() -> None:
            while not at_eof and len(buf) - pos < STREAM_LOOKAHEAD_MARGIN:
                fill()

        def compact() -> None:
            nonlocal buf, pos
            if pos > STREAM_COMPACT_THRESHOLD:
                buf = buf[pos:]
                pos = 0

        fill()
        if not buf:
            raise RepairError("input file is empty")

        ensure_lookahead()
        first = _find_next_leader_from(buf, 0)
        while first is None and not at_eof:
            fill()
            ensure_lookahead()
            first = _find_next_leader_from(buf, 0)
        if first is None:
            raise RepairError("no MARC leader found in input text")
        pos = first

        while pos < len(buf) or not at_eof:
            ensure_lookahead()
            compact()
            if pos >= len(buf):
                break

            intact = _read_intact_at(buf, pos, fix_bad_indicators=fix_bad_indicators)
            if intact is not None:
                parsed, end = intact
            else:
                next_leader = _find_next_leader_from(buf, pos + 24)
                while next_leader is None and not at_eof:
                    fill()
                    next_leader = _find_next_leader_from(buf, pos + 24)
                end_hint = next_leader if next_leader is not None else len(buf)
                stripped = _repair_stripped_at(buf, pos, end_hint, overrides.get(idx))
                parsed, end = stripped if stripped is not None else (None, None)

            if parsed is not None:
                if end >= len(buf) and not at_eof:
                    fill()  # might be a record terminator just past what's buffered
                if end < len(buf) and buf[end] == RECTERM:
                    end += 1
                yield parsed, buf[pos:end]
                pos = end
                idx += 1
                continue

            next_leader = _find_next_leader_from(buf, pos + 1)
            while next_leader is None and not at_eof:
                fill()
                next_leader = _find_next_leader_from(buf, pos + 1)
            next_pos = next_leader if next_leader is not None else len(buf)
            chunk = buf[pos:next_pos]
            placeholder = ParsedRecord(leader=buf[pos:pos + 24], entries=[])
            placeholder.unresolved.append((
                0,
                DirEntry("???", len(chunk), 0),
                chunk[:200],
                RepairError("no consistent directory found for this record at all"),
            ))
            yield placeholder, chunk
            pos = next_pos
            idx += 1


#: A real defect seen in production data: a record's own leader/directory
#: is fully self-consistent, but genuinely falls one field short of the
#: record's real byte content -- whatever wrote the file appended one more
#: field's bytes without ever adding a directory entry for it (so neither
#: the directory nor the declared length account for those bytes). Mode 1
#: correctly stops after exactly as many real fields as the directory
#: has entries for (see _read_intact_at), leaving that trailing field's
#: bytes dangling right after -- with no leader/directory of their own,
#: they can't be attributed to any tag, so iter_repair_stream reports
#: them UNRESOLVED, immediately after the record they actually belong to.
#:
#: Reattaching those bytes to that record requires *guessing* the tag
#: that would have been in the missing directory entry -- something this
#: tool otherwise never does. The one shape narrow enough to guess safely
#: is a personal name heading: subfields drawn only from this set, with
#: $a (the name itself) always present, is essentially unambiguous as tag
#: 700 (Added Entry -- Personal Name) -- $b numeration, $c titles/other
#: words, $d dates, $e relator term, $q fuller form of name, $4 relator
#: code are, together, specific enough to this one heading type that no
#: other common tag's content looks like this by coincidence.
_ORPHANED_FIELD_TAG = "700"


_ORPHANED_FIELD_VALID_CODES = set("abcdeq4")


def _try_parse_orphaned_field(chunk: str) -> tuple[str, list[tuple[str, str]]] | None:
    """If `chunk` (an UNRESOLVED record/chunk with no leader or directory
    of its own) is exactly the real bytes of one intact MARC data field --
    2 indicator characters, then a subfield-delimited body, terminated by
    exactly one 0x1E, optionally followed by a trailing 0x1D -- return its
    (indicators, subfields). Returns None for anything else, including
    more than one field's worth of content: reattach_orphaned_trailing_
    fields only ever reattaches a single trailing field, the one shape
    this defect (see above) actually produces."""
    text = chunk[:-1] if chunk.endswith(RECTERM) else chunk
    if not text.endswith(FIELDTERM):
        return None
    body_all = text[:-1]
    if FIELDTERM in body_all or len(body_all) < 2:
        return None
    indicators, body = body_all[:2], body_all[2:]
    if not body.startswith(SUBFIELD):
        return None
    parts = body.split(SUBFIELD)
    if parts[0] != "":
        return None
    subfields = [(p[0], p[1:]) for p in parts[1:] if p]
    if not subfields:
        return None
    return indicators, subfields


def reattach_orphaned_trailing_fields(
    record_stream: Iterator[tuple[ParsedRecord, str]],
) -> Iterator[tuple[ParsedRecord, str]]:
    """Wrap a repair stream (iter_repair_stream's or iter_repair's output)
    to fix the defect described above `_ORPHANED_FIELD_TAG`: merge an
    UNRESOLVED chunk into the record immediately before it, as a new tag
    700 field, whenever that chunk is exactly one field's worth of real
    bytes shaped like a personal name heading (see
    `_try_parse_orphaned_field`) and the record before it parsed
    successfully. The merged record's `reattached_orphaned_fields` records
    (tag, detail) so the caller can log it -- under FIXED/REQUIRES
    ATTENTION, since the tag is a guess, however confident.

    Every other UNRESOLVED chunk (multi-field, not personal-name-shaped,
    or not immediately following a resolved record) passes through
    unchanged, exactly as before this wrapper existed. This does mean a
    successful merge yields one fewer (parsed, rec_text) pair than the
    stream it wraps produced for that same input -- seen as `main`'s own
    output record count deliberately, since the two chunks were never
    really two separate records to begin with, just one record whose
    trailing field lost its directory entry.
    """
    pending: tuple[ParsedRecord, str] | None = None
    for parsed, rec_text in record_stream:
        if pending is not None:
            prev_parsed, prev_text = pending
            merged = False
            if not prev_parsed.unresolved and parsed.unresolved:
                field_shape = _try_parse_orphaned_field(rec_text)
                if field_shape is not None:
                    indicators, subfields = field_shape
                    codes = {code for code, _ in subfields}
                    if "a" in codes and codes <= _ORPHANED_FIELD_VALID_CODES:
                        new_field = Field_(_ORPHANED_FIELD_TAG, indicators, subfields)
                        prev_parsed.fields.append(new_field)
                        prev_parsed.entries.append(
                            DirEntry(_ORPHANED_FIELD_TAG, len(new_field.delimited()), 0)
                        )
                        detail = (
                            f"reattached a trailing ={_ORPHANED_FIELD_TAG} field, "
                            f"present in the file but missing its own directory "
                            f"entry (so excluded from this record's own declared "
                            f"length): {new_field.delimited()!r}"
                        )
                        prev_parsed.reattached_orphaned_fields.append(
                            (_ORPHANED_FIELD_TAG, detail)
                        )
                        pending = (prev_parsed, prev_text + rec_text)
                        merged = True
            if not merged:
                yield pending
                pending = (parsed, rec_text)
        else:
            pending = (parsed, rec_text)
    if pending is not None:
        yield pending


def repair_text(
    text: str, overrides: dict[int, OverridesByField] | None = None
) -> list[ParsedRecord]:
    """Repair every record found in `text`. `overrides` maps record-index ->
    (field-index -> (indicators, subfields)) and only matters for records that
    fall back to Mode 2."""
    return [parsed for parsed, _rec_text in iter_repair(text, overrides)]


# ---------------------------------------------------------------------------
# Assembly: emit real ISO 2709 bytes + mnemonic .mrk text
# ---------------------------------------------------------------------------

MAX_RECORD_BYTES = 99999  # ISO 2709's 5-digit leader record-length slot


def _field_chunk_lengths(parsed: ParsedRecord) -> list[int]:
    encoding = "utf-8" if parsed.leader[9:10] == "a" else "latin-1"
    return [
        len(f.delimited().encode(encoding, errors="surrogateescape")) for f in parsed.fields
    ]


def record_exceeds_max_length(parsed: ParsedRecord) -> int | None:
    """The record's assembled size in bytes if it is over MAX_RECORD_BYTES
    (what `assemble_marc` would produce), else None."""
    # leader + 12-byte directory entry per field + directory terminator +
    # field data + record terminator
    overhead = 24 + 12 * len(parsed.fields) + 1 + 1
    # Cheap pass first: a char count from the field structure directly,
    # without building delimited() strings or encode()-ing anything. Every
    # char is at most 4 UTF-8 bytes (1 for latin-1, where char count IS the
    # byte count), so if even that pessimistic bound fits, the real,
    # encoded total can only be smaller -- skip the expensive exact pass
    # that almost every record (never near MAX_RECORD_BYTES) doesn't need.
    char_total = sum(
        len(f.content or "") + 1 if f.is_control()
        else len(f.indicators) + sum(2 + len(data) for _code, data in f.subfields) + 1
        for f in parsed.fields
    )
    bytes_per_char = 1 if parsed.leader[9:10] != "a" else 4
    if overhead + bytes_per_char * char_total <= MAX_RECORD_BYTES:
        return None
    total = overhead + sum(_field_chunk_lengths(parsed))
    return total if total > MAX_RECORD_BYTES else None


def _linked_880s(parsed: ParsedRecord, field_: Field_) -> list[Field_]:
    """The 880 field(s) whose $6 points back at `field_` (its own $6 is
    "880-NN"; theirs is "<tag>-NN")."""
    if field_.is_control():
        return []
    for code, data in field_.subfields:
        if code == "6" and data.startswith("880-"):
            occurrence = data[4:6]
            wanted = f"{field_.tag}-{occurrence}"
            return [
                f for f in parsed.fields
                if f.tag == "880" and not f.is_control()
                and any(c == "6" and d.startswith(wanted) for c, d in f.subfields)
            ]
    return []


def trim_oversized_record(parsed: ParsedRecord) -> str | None:
    """Remove whole fields until a record over MAX_RECORD_BYTES fits.

    Removal order, largest field first within each group: 9xx (except a
    999 with indicators "ff", which FOLIO needs), then 5xx (each with its
    linked 880), then 6xx (likewise). Nothing else is ever removed, so
    leader, 001/003/005/008, 245, 1xx, 7xx, etc. are never touched. If
    even removing every candidate would not get the record under the
    limit, the record is left unchanged and None is returned (the
    caller then diverts it as before). Returns the log detail, or None
    when nothing was removed."""
    if record_exceeds_max_length(parsed) is None:
        return None
    lengths = {id(f): n for f, n in zip(parsed.fields, _field_chunk_lengths(parsed))}
    position = {id(f): i for i, f in enumerate(parsed.fields)}
    size = 24 + 12 * len(parsed.fields) + 1 + sum(lengths.values()) + 1
    before = size
    removals: list[list[Field_]] = []
    taken: set[int] = set()
    for group in ("9", "5", "6"):
        units = []
        for f in parsed.fields:
            if not f.tag.startswith(group) or id(f) in taken:
                continue
            if f.tag == "999" and f.indicators == "ff":
                continue
            unit = [f] + [x for x in _linked_880s(parsed, f) if id(x) not in taken]
            units.append(unit)
        units.sort(key=lambda u: (-sum(lengths[id(x)] for x in u), -position[id(u[0])]))
        for unit in units:
            if size <= MAX_RECORD_BYTES:
                break
            removals.append(unit)
            taken.update(id(x) for x in unit)
            size -= sum(lengths[id(x)] + 12 for x in unit)
    if size > MAX_RECORD_BYTES:
        return None
    parts = []
    for unit in removals:
        for f in unit:
            preview = "".join(d for c, d in f.subfields if c != "6")[:40]
            parts.append(f"={f.tag} ({lengths[id(f)]} bytes, {preview!r})")
    parsed.fields = [f for f in parsed.fields if id(f) not in taken]
    return (
        f"record was {before} bytes, over the {MAX_RECORD_BYTES}-byte limit -- "
        f"removed {len(parts)} field(s), now {size} bytes: " + "; ".join(parts)
    )


def assemble_marc(parsed: ParsedRecord) -> bytes:
    if parsed.unresolved:
        raise RepairError(
            f"record has {len(parsed.unresolved)} unresolved field(s); "
            "supply overrides before assembling"
        )
    # ISO 2709's length/offset fields are all BYTE counts, not character
    # counts -- for a UTF-8-declared record (leader byte 9 == "a"), any
    # non-ASCII character (accented names, "$c\xa9" copyright marks, etc.)
    # is more than one byte, so `len()` on the decoded string undercounts
    # by exactly the number of multi-byte characters present. Every length
    # below is measured on the ENCODED bytes for this reason -- this was a
    # real bug: MarcEdit's MarcBreaker reported "Record length doesn't
    # match reported record length" (off by a handful of bytes) on exactly
    # the records with non-ASCII content in a real 48k-record run.
    encoding = "utf-8" if parsed.leader[9:10] == "a" else "latin-1"
    # Each field's delimited-and-encoded bytes are computed exactly once
    # here and reused for both its directory length and the final
    # field_data below, rather than encoding every field twice (once per
    # field for its length, once more as part of the whole record at the
    # end) -- this doubled encode() work was a real, measurable cost on
    # a real 91MB/48k-record file.
    # errors="surrogateescape": a field's (or, below, the leader's own
    # untouched bytes') text can contain a surrogate-escaped character if
    # the source file had a corrupted byte that wasn't valid UTF-8 (see
    # iter_repair_stream's matching comment) -- round-tripping it back to
    # its original byte here, rather than raising, keeps that corruption
    # exactly as harmless on the way out as it already is on the way in.
    field_byte_chunks = [
        f.delimited().encode(encoding, errors="surrogateescape") for f in parsed.fields
    ]
    # build directory from field lengths (they must match declared lengths --
    # verified already during parsing, but recompute here to be self-consistent)
    dir_entries = []
    running = 0
    for f, chunk in zip(parsed.fields, field_byte_chunks):
        length = len(chunk)
        if length > 9999:
            raise RepairError(
                f"={f.tag}: field is {length} bytes, but the directory's "
                "length field is only 4 digits (max 9999) -- can't represent "
                "this field in ISO 2709 at all"
            )
        if running > 99999:
            raise RepairError(
                f"={f.tag}: starting offset {running} exceeds the directory's "
                "5-digit starting-position field (max 99999)"
            )
        dir_entries.append(f"{f.tag:>3}{length:04d}{running:05d}")
        running += length
    directory = "".join(dir_entries) + FIELDTERM
    base_address = 24 + len(directory)  # directory is always pure ASCII digits
    if base_address > 99999:
        # Unlike total record length (see below), MARC21 has no documented
        # sentinel for base address -- a reader NEEDS the real value to
        # find where field data starts at all, so there's no way to record
        # "the real value doesn't fit" and still have the record parseable.
        # This needs an enormous directory (tens of thousands of fields) to
        # happen in practice.
        raise RepairError(
            f"base address of data is {base_address}, but the leader's "
            "base-address field is only 5 digits (max 99999) -- can't "
            "represent this record's directory in ISO 2709 at all"
        )
    field_data_bytes = b"".join(field_byte_chunks)
    total_length = base_address + len(field_data_bytes) + 1  # + record terminator
    # MARC21's leader spec documents 99999 as a sentinel for "actual length
    # exceeds this field's 5 digits, recompute it" -- exactly what happens
    # here: the real length is still written (`record`, below), only the
    # leader's own declared value is capped. Nothing is lost: any reader
    # that finds the record's real end via its terminator (as this tool's
    # own Mode 1 already does, never trusting the declared length) reads it
    # back correctly regardless of what the leader claims. In practice,
    # though, not every real-world importer is that tolerant -- FOLIO Data
    # Import aborts the entire batch with no job log at all on a record
    # using this sentinel, confirmed empirically, rather than rejecting
    # just that one record. Callers that need every record in their main
    # output to be importable should divert a record this large elsewhere
    # instead of calling this; see --guarantee-importable in main().
    declared_length = min(total_length, 99999)

    leader = parsed.leader
    new_leader = (
        f"{declared_length:05d}"
        + leader[5:12]
        + f"{base_address:05d}"
        + leader[17:20]
        + "4500"  # entry map: always this literal constant, never copied
        # through from the original -- a corrupted leader byte here (seen
        # in real data, e.g. "45x0") doesn't affect parsing (nothing in
        # this tool depends on it -- see find_record_starts/_read_intact_at
        # using real delimiters and looser leader checks instead) but must
        # not be written back out uncorrected.
    )
    header = (new_leader + directory).encode(encoding, errors="surrogateescape")
    return header + field_data_bytes + RECTERM.encode(encoding)


def to_mrk(parsed: ParsedRecord) -> str:
    lines = [f"=LDR  {parsed.leader}"]
    for f in parsed.fields:
        if f.is_control():
            lines.append(f"={f.tag}  {f.content}")
        else:
            body = "".join(f"${code}{data}" for code, data in f.subfields)
            lines.append(f"={f.tag}  {f.indicators}{body}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# MARC-8 (ANSEL) -> UTF-8 transcoding
#
# Leader byte 9 (position [9:10]) declares the character encoding: "a" means
# UCS/Unicode (UTF-8); anything else (usually blank) means legacy MARC-8,
# which is NOT a defect on its own -- it's a real, valid MARC encoding, just
# one most modern systems (including FOLIO) expect converted to UTF-8. This
# is content transcoding, not structural repair, and needs the actual
# character-set mapping tables (including MARC-8's combining diacritics,
# which come *before* the base letter they modify, unlike Unicode combining
# marks which follow it) -- accurately reimplementing that from scratch is
# error-prone, so this delegates to pymarc's marc8_to_unicode(), which
# already carries the full LC-published mapping tables. Only this one
# function needs pymarc installed (see requirements.txt); everything else
# in this tool is pure Python / stdlib.
# ---------------------------------------------------------------------------

UNICODE_ENCODING_BYTE = "a"


#: MARC-8 escape final bytes for genuine non-Latin SCRIPTS (Hebrew,
#: Arabic, Cyrillic, Greek letters, CJK/EACC) -- as opposed to
#: superscript/subscript/Greek-symbols, which are ordinary in Latin
#: bibliographic text (chemical formulas, math notation like "E=mc2")
#: and would false-positive constantly if included here. Maps the
#: final byte to (bytes consumed per resulting character, name).
#: EACC is a 94x94 multi-byte set (3 raw bytes/char); the rest are
#: single-byte sets (1 raw byte/char). See `find_suspect_marc8_escapes`.
_MARC8_SCRIPT_CHARSETS = {
    "1": (3, "CJK/EACC"),
    "2": (1, "Basic Hebrew"),
    "3": (1, "Basic Arabic"),
    "4": (1, "Extended Arabic"),
    "N": (1, "Basic Cyrillic"),
    "Q": (1, "Extended Cyrillic"),
    "S": (1, "Basic Greek"),
}


_MARC8_SCRIPT_ESCAPE = re.compile(
    r"\x1b([($])(" + "|".join(re.escape(k) for k in _MARC8_SCRIPT_CHARSETS) + r")"
)


#: pymarc's marc8_to_unicode() doesn't raise on a truncated multi-byte
#: (CJK/EACC) character -- it writes this warning straight to stderr
#: and silently substitutes a blank space, so the defect would
#: otherwise vanish into the console with no record/field attribution
#: at all. `transcode_marc8_to_utf8` captures stderr around the call
#: and matches this pattern to turn it into a proper, logged failure
#: instead (see `_Marc8MultibyteTruncated`).
_MARC8_MULTIBYTE_TRUNCATED_RE = re.compile(
    r"Multi-byte position (\d+) exceeds length of marc8 string (\d+)"
)


class _Marc8MultibyteTruncated(Exception):
    """Raised by `transcode_marc8_to_utf8.convert` when pymarc reports
    (via stderr, not an exception -- see `_MARC8_MULTIBYTE_TRUNCATED_RE`)
    that a multi-byte MARC-8 character ran off the end of the field.
    `byte_pos` is the offset into the latin-1-encoded field bytes where
    the truncated character starts, for building a context snippet."""

    def __init__(self, message: str, byte_pos: int) -> None:
        super().__init__(message)
        self.byte_pos = byte_pos


#: One escape-sequence byte run, recognized or not: either a
#: `_MARC8_SCRIPT_ESCAPE`-shaped switch all the way through its own
#: closing "\x1b(B" (first alternative -- payload bounded to 6 bytes,
#: well above the longest real payload, 3 bytes for CJK/EACC), or (if
#: that doesn't close within a few bytes) a bare `\x1b` plus up to 3
#: more bytes, which is what real corrupted data's stray/unrecognized
#: escape bytes look like (see docs/MARC8_ESCAPE_ANALYSIS.md and
#: `_marc8_escape_run_clusters`'s docstring below). Purely cosmetic/
#: clustering use -- never used to decide whether something IS a
#: defect, only to find the full extent of escape noise around one
#: already-validated via `_MARC8_SCRIPT_ESCAPE` below.
_MARC8_ANY_ESCAPE_RUN_RE = re.compile(r"\x1b(?:[^\x1b]{1,6}?\x1b\(B|[^\x1b]{0,3})")


_REPEATED_MASK_RE = re.compile(r"(?:\[\?\])+")


def _marc8_escape_run_clusters(text: str) -> list[tuple[int, int]]:
    """Every escape-sequence byte run in `text` -- recognized MARC-8
    script switches and any other `\x1b`-led bytes that don't parse as
    one -- as (start, end) spans, with any immediately-adjacent runs
    merged into one. Real corrupted data mixes stray unrecognized
    escape bytes directly against the one genuine script-switching
    escape (confirmed on real production data -- see
    docs/MARC8_ESCAPE_ANALYSIS.md and HANDOFF.md): e.g. "Haure" +
    `\x1bp+` + `\x1bs` (unrecognized) + `\x1b(QB\x1b(B` (the real,
    recognized Extended-Cyrillic escape) + "au". Treating the whole
    run as one cluster is what lets `find_suspect_marc8_escapes` find
    the real surrounding word ("Haure"/"au") instead of stopping at a
    stray escape byte that happens to look like an ASCII letter
    ("s", from `\x1bs`)."""
    spans = [m.span() for m in _MARC8_ANY_ESCAPE_RUN_RE.finditer(text)]
    clusters: list[tuple[int, int]] = []
    for start, end in spans:
        if clusters and clusters[-1][1] == start:
            clusters[-1] = (clusters[-1][0], end)
        else:
            clusters.append((start, end))
    return clusters


def _cluster_containing(clusters: list[tuple[int, int]], pos: int) -> tuple[int, int]:
    for start, end in clusters:
        if start <= pos < end:
            return start, end
    return pos, pos  # pos is always inside some escape run in practice


def _mask_marc8_escapes(s: str) -> str:
    """Replace every escape-sequence byte run in `s` with a single
    "[?]" marker, collapsing adjacent runs into one -- for a
    human-readable log context window. Used on the text surrounding an
    already-validated finding, so it may also mask *other* escapes
    nearby (a neighboring defect, or genuine multi-character foreign
    content) -- that's intentional: raw `\x1b`/high-bit bytes are
    unreadable in a log line regardless of what they decode to, and
    masking them doesn't create a new finding for them."""
    return _REPEATED_MASK_RE.sub("[?]", _MARC8_ANY_ESCAPE_RUN_RE.sub("[?]", s))


#: Payload byte -> the correct raw ANSEL combining-diacritic byte
#: (0xE0-0xFF range, latin-1-storable -- see the module-level comment
#: on MARC-8/ANSEL above), for the single-raw-byte MARC-8 script-switch
#: escapes (`_MARC8_SCRIPT_CHARSETS` entries with bytes_per_char == 1;
#: the 3-byte CJK/EACC case "1" is excluded on purpose, see below).
#: Derived empirically from `working/GTU_bibs.mrc`: grouping every
#: occurrence of this escape shape by (charset, payload byte) and
#: reading the real word fragments on both sides showed the payload
#: byte reliably predicts one specific diacritic -- not a specific
#: letter -- across thousands of self-consistent examples per payload
#: (e.g. charset "Q" payload "B" -> acute, confirmed by
#: "the\x1bp+\x1bs\x1b(QB\x1b(Bologie" = "theologie" + acute = "théologie",
#: recurring 1,690 times; "sie\x1bp+\x1bs\x1b(QA\x1b(Bcle" = "siecle" +
#: grave = "siècle", recurring 2,360 times -- see conversation notes,
#: not yet written up as a docs/ file). That is a different, better-
#: behaved situation than the 3-byte CJK/EACC case analyzed in
#: docs/MARC8_ESCAPE_ANALYSIS.md: there, the escape swallows 3 raw bytes
#: (the original diacritic byte, its base letter, AND one more plain
#: letter), so the base letter is genuinely destroyed and no payload ->
#: letter table exists. Here, only 1 raw byte (the diacritic itself) is
#: swallowed -- the base letter survives intact immediately before the
#: escape -- so the fix is "reapply this mark to the letter already
#: there," not "guess a replacement letter."
#:
#: Each value is the genuine ANSEL byte for that mark (confirmed
#: against pymarc's own `CHARSET_45` combining-character table, e.g.
#: 0xE2 -> U+0301 combining acute), inserted *before* the base letter
#: per real MARC-8/ANSEL convention (see the module comment above) --
#: not a precomposed Unicode character. That keeps the fixed field
#: entirely within the record's existing MARC-8/latin-1 byte space, so
#: `transcode_marc8_to_utf8` (which runs right after this, unchanged)
#: converts it to UTF-8 the normal way, through pymarc's own mapping
#: tables, exactly as if the cataloger had typed the accent correctly
#: in the first place -- rather than this function inventing its own
#: partial Unicode conversion that the real transcoder downstream
#: wouldn't recognize as already-done MARC-8 content.
#:
#: Deliberately NOT a complete mapping of every payload byte seen in
#: that file: payloads with low occurrence counts, or that resolve to
#: something other than "one diacritic mark on the adjacent letter"
#: (e.g. charset "Q" payload 0x60 -> German sharp s "ß", a standalone
#: letter substitution rather than a mark on the neighbor; charset "3"
#: payload "C" -> the "oe" ligature "œ"; charset "3" payload "E" ->
#: an apostrophe, not a diacritic at all) are left out of this table on
#: purpose -- anything not in here still falls through to the
#: detect-only `find_suspect_marc8_escapes` below, same as before this
#: fixer existed.
#:
#: ("Q", "G") was briefly removed after checking it against a single
#: real example (working/GTU_bibs_sample50.mrc record .b10001888,
#: "Yanbu<G>" / "aya<G>h", Arabic "Yanbū' al-Ḥayāh") that seemed to
#: need MACRON where the payload normally means BREVE (Korean
#: McCune-Reischauer romanization, e.g. "Sŏul", "Yŏn"). Checked against
#: every occurrence of this payload in the full working/GTU_bibs.mrc
#: (2,920 of them) before deciding what to do about it: the Arabic
#: case is the rare exception, not a 50/50 split -- the overwhelming
#: majority are legitimate BREVE, mostly Russian "-iĭ" name endings
#: (Krachkovskii, Georgii Valentinovich) and Korean vowels, which is
#: why it's back in this table. The 8 genuine Arabic occurrences (out
#: of 2,920) all have the literal "al-" article within a few
#: characters -- Korean/Russian words never do -- so that's checked as
#: a narrow, cheap exception in `_marc8_diacritic_replacement` instead
#: of a blanket skip: see `_MARC8_AMBIGUOUS_NEAR_AL_PAYLOADS` below.
_MARC8_DIACRITIC_PAYLOADS: dict[tuple[str, str], str] = {
    ("Q", "A"): "\xe1",  # ANSEL combining grave accent (e.g. e -> è)
    ("Q", "B"): "\xe2",  # ANSEL combining acute accent (e.g. e -> é)
    ("Q", "C"): "\xe3",  # ANSEL combining circumflex accent (e.g. e -> ê)
    ("Q", "D"): "\xe4",  # ANSEL combining tilde (e.g. n -> ñ)
    ("Q", "E"): "\xe5",  # ANSEL combining macron (e.g. e -> ē)
    ("Q", "G"): "\xe6",  # ANSEL combining breve (e.g. o -> ŏ) -- except near "al-", see above
    ("Q", "K"): "\xea",  # ANSEL combining ring above (e.g. a -> å)
    ("Q", "M"): "\xe9",  # ANSEL combining caron/háček (e.g. z -> ž)
    ("3", "L"): "\xf0",  # ANSEL combining cedilla (e.g. c -> ç)
}


#: Payloads that are reliable almost everywhere but have one confirmed
#: language-dependent exception -- currently just ("Q", "G"): BREVE,
#: except within a few characters of the literal Arabic article "al-",
#: where the one confirmed real example needed MACRON instead (see the
#: table comment above). Checked at a 15-character radius, which is
#: generous enough to span this fixer's own stray-junk noise around the
#: escape without reaching into an unrelated neighboring word.
_MARC8_AMBIGUOUS_NEAR_AL_PAYLOADS = {("Q", "G")}


_MARC8_AMBIGUOUS_CONTEXT_RADIUS = 15


#: A second, related corruption found in the same GTU_bibs.mrc sample
#: (confirmed against three records referencing the same Maimonides
#: title, and cross-checked against Sierra's own OPAC display -- see
#: conversation notes): the stray escape-junk run this fixer already
#: swallows (`\x1bp+\x1bs`/`\x1bp(\x1bs`, etc.) sometimes precedes a bare,
#: un-escaped raw byte instead of (or alongside) a real
#: `_MARC8_SCRIPT_ESCAPE` switch -- almost always Arabic transliteration
#: marks: combining dot-below (ḥ/ṣ/ḏ, e.g. "al-h<junk>\xa3ujjah" ->
#: "al-ḥujjah") and the standalone hamza/ayn modifier letters (e.g.
#: "Ihya<junk>\xbc" -> "Ihyā'", "<junk>\xbbulum" -> "'ulum"). Each raw
#: byte here is itself a *valid* ANSEL byte for an unrelated letter
#: (0xA3 is really "Đ", 0xBC a different unrelated letter) -- it's not
#: a bare byte the real MARC-8 spec would ever leave broken on its own;
#: the giveaway that it's corrupted is the ESC bytes immediately before
#: it, which never legitimately precede ordinary text. That's also why
#: fixing this is safe: without that ESC-byte signal directly attached,
#: a bare 0xA3/0xBC/0xBB could be genuine (unrelated) ANSEL content and
#: must NOT be touched -- see `_MARC8_BARE_COMBINING_RE`/
#: `_MARC8_BARE_STANDALONE_RE` below, which both require it.
#:
#: 0xA7 was added after the same methodology turned up more candidate
#: bytes: checked against all occurrences in the full working/GTU_bibs.mrc
#: (6,182 of them), overwhelmingly common French/Occitan words missing a
#: cedilla ("franc<junk>\xa7ais" -> "français", "Pourc<junk>\xa7ain" ->
#: "Pourçain"). 0xA7 is itself a valid standalone ANSEL byte (U+02B9
#: MODIFIER LETTER PRIME, unrelated to cedilla) when *not* preceded by
#: the junk signal -- same safety argument as 0xA3/0xBC/0xBB above.
#:
#: 0xAE, 0xA5, 0xA8, and 0xA4 were added after validating the remaining
#: candidates from that same scan against the full corpus. Unlike the
#: bytes above, each of these turned out to be *overloaded* -- the same
#: byte stands in for more than one mark depending on the word/language
#: it corrupted, so (unlike 0xA3/0xA7 above) they're only safe to fix
#: in the specific before-letter context where the evidence is actually
#: clean; see `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE` below for what
#: each byte is NOT applied to, and why (and
#: `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES` for the one case, 0xA4,
#: where a *different* before-letter needs a genuinely different mark
#: rather than just being excluded):
#: - 0xAE: dot below, but only after "h" (109 of 135 occurrences, 81% --
#:   recounted; unlike 0xA4/0xA5/0xA8 this figure was NOT stale, same 135
#:   both before and after the close-escape fix) -- confirmed-by-volume
#:   Near Eastern/biblical names ("H<junk>\xaeammurabi" -> "Ḥammurabi",
#:   "Yarih<junk>\xae" -> "Yariḥ"). Re-investigated the other 26
#:   before-letters (i/9, a/8, u/4, o/3, s/2) the same way as 0xA4/0xA5/0xA8's
#:   leftovers; none cluster into a second confirmed override. They split
#:   across: (1) Italian "pi<junk>\xae" / "Ges<junk>\xae" (più/Gesù, 4
#:   occurrences) -- the destroyed-base-letter failure mode already
#:   documented for 0xA4 (the whole "ù" is missing, not just its mark, so
#:   there's no letter to attach dot-below to even if that were the right
#:   mark here, which it isn't -- più/Gesù need grave, not dot below); (2)
#:   an Akkadian/Hittite/Sumerian cluster (Hattusa, Harranu/-shubu,
#:   Ninhursag, Asalluhi, Kalhu, Hirbet, Hissar; i/u/a, ~11 occurrences)
#:   where the dot-below mark is real but belongs on the "h" one letter
#:   BEFORE the matched before-letter (e.g. "Ninhu<junk>\xaersag" is
#:   "Ninḥursag" -- the mark is on "h", not "u") -- this before-letter
#:   mechanism only ever attaches to the letter immediately before the
#:   junk, so it structurally can't reach one letter further back, same
#:   shape as 0xA5's Sanskrit "t"/"a" leftovers (mark lands the wrong
#:   distance from where it belongs, just the opposite direction); (3) a
#:   Germanic/Scandinavian cluster ("underst<junk>\xaedd", "Gr<junk>\xaeeres",
#:   "Knau<junk>\xae") needing umlaut/o-with-stroke, not dot below -- a
#:   different mark entirely, same as 0xA4's u-vs-z overload but without
#:   enough volume or a clean split to add as a second override; (4) a
#:   genuinely ambiguous al-Ghazali/"Ihya" cluster (5 occurrences) that
#:   doesn't read as a clean dot-below case at all (common transliteration
#:   of "al-Ghazali" carries no diacritic here; "Ihya" needs the unrelated
#:   hamza byte, see `_MARC8_BARE_STANDALONE_BYTES`); and (5) one "où il"
#:   (French, needs grave not dot below) and one singleton too garbled to
#:   confirm. No override added -- 0xAE stays restricted to "h".
#: - 0xA5: dot below, but only after "r" (251 of 291 occurrences, 86% --
#:   recounted after the close-escape fix removed 27 stale false-positive
#:   matches from the original 318, the same stale-count effect already
#:   seen for 0xA4/0xA8) -- confirmed-by-volume Sanskrit IAST vocalic r
#:   ("R<junk>\xa5gveda" -> "Ṛgveda", "Kr<junk>\xa5s<junk>\xa3n<junk>\xa3a"
#:   -> "Kṛṣṇa", both textbook-unambiguous). "h" (3 occurrences) looked
#:   like a second clean case in isolation -- same dot-below mark
#:   0xA3/0xAE already produce, confirmed-by-volume-of-2
#:   ("Muh<junk>\xa5ammad" -> "Muḥammad") -- but was NOT added: real
#:   record .b18157713 (working/GTU_bibs_0xA5_sample.mrc) has the third
#:   "h" occurrence ("Yah<junk>\xa5 at Elephantine") sitting in the same
#:   subfield as an unrelated, pre-existing escape-designator corruption
#:   earlier in the string (an unrecognized "\x1bp+\x1b\xe4..." sequence)
#:   that leaves pymarc's marc8_to_unicode decoder stuck in a bad G1
#:   charset state for the rest of the field. Confirmed by diffing
#:   transcoded output with the override enabled vs. disabled on that
#:   exact subfield: leaving the bare \xa5 byte untouched happens to let
#:   the decoder resync and recover the ~100 characters of legible text
#:   after it ("Bob Becking -- The Judeans/Arameans..."); replacing it
#:   with the correct \xf2 (dot below) + "h" keeps the decoder in the
#:   broken state and that whole tail renders as placeholder glyphs
#:   instead -- i.e. the semantically-correct fix makes this specific
#:   real record's output *more* garbled, not less. Not generalizable by
#:   checking the "h" occurrence in isolation; would need per-occurrence
#:   verification against the surrounding field, which this before-letter
#:   mechanism has no way to do. Left unfixed pending either a fix to the
#:   upstream escape-designator decoder bug or a narrower guard; see
#:   HANDOFF.md for the full writeup. Other before-letters (t/10, n/10,
#:   s/7, a/6, g/1, d/1, b/1, m/1 occurrences) were also investigated and
#:   don't cluster into another confirmed mark: the "t"/"a" groups are
#:   Sanskrit words where the vocalic r mark lands after an entire
#:   following consonant-vowel run instead of right after the "r" it
#:   belongs to (e.g. "Smrt<junk>\xa5i" is "smṛti", not "smrṭi" -- the dot
#:   below belongs 2 letters before the junk, which this before-letter
#:   mechanism structurally can't reach); the "n"/"s" groups are Greek
#:   words with a destroyed vowel before the junk, not a missing mark on
#:   the letter immediately before it (e.g. "Orthodoxn<junk>\xa5" is
#:   "Orthodoxōn", missing the whole "ō", not a mark on "n" -- same
#:   destroyed-base-letter failure mode already documented for 0xA4); and
#:   the last 4 (g/d/b/m, one occurrence each) are singletons too
#:   ambiguous to confirm on their own.
#: - 0xA8: ogonek, but only after "a", "e", or "u" (248 of 260
#:   occurrences, 95% -- recounted after the close-escape fix removed
#:   64 stale false-positive matches from the original 324, the same
#:   stale-count effect already seen for 0xA4) -- confirmed-by-volume
#:   Polish a/e ogonek ("Ksia<junk>\xa8zka" -> "Książka",
#:   "We<junk>\xa8gierski" -> "Węgierski") and Lithuanian u ogonek
#:   ("Kataliku<junk>\xa8" -> "Katalikų"). Unlike 0xA4's "z", the
#:   remaining 12 occurrences (re-investigated via the same methodology)
#:   do NOT cluster into one new confirmed mark -- they split across
#:   four small, unrelated groups, none large or clean enough to add as
#:   an override: 4 "c" occurrences need cedilla, not ogonek (e.g.
#:   "Franc<junk>\xa8ais" -> "français") but that's already produced by
#:   the unrestricted 0xA7 row, so an 0xA8 override here would be
#:   redundant, not new; 3 occurrences ("o", "o", "i" -- "Muo<junk>\xa8z"
#:   -> "Muñoz", "Qui<junk>\xa8ones" -> "Quiñones") are the
#:   destroyed-base-letter failure mode already documented for 0xA4 (the
#:   missing "ñ" sits two letters before the junk, not attachable to the
#:   "o"/"i" immediately before it); 1 "o" occurrence ("nieuwe series
#:   o<junk>\xa8f" -> "...of Verhandelingen") needs no diacritic at all;
#:   and the last 4 ("s"/"S"/"r"/"d"/"h", one pair of words) are a mix of
#:   a confirmed-but-singleton cedilla ("Karakas<junk>\xa8oglu" ->
#:   "Karakaşoğlu"), a confirmed-but-singleton dot below
#:   ("S<junk>\xa8uhbat" -> "Ṣuḥbat"), and three occurrences in one
#:   heavily-garbled title ("Ar<junk>\xa8hd<junk>\xa8zerh<junk>\xa8n
#:   bar<junk>\xa8haran") too corrupted to confirm at all. No override
#:   added for 0xA8; left restricted to a/e/u as before.
#: - 0xA4: diaeresis, but only after "u" (10 of 79 occurrences, 13% --
#:   recounted after the close-escape fix below removed 14 false-positive
#:   matches from the original 93) -- confirmed-by-volume German ü
#:   ("U<junk>\xa4bersetzung" -> "Übersetzung", "Beschlu<junk>\xa4sse" ->
#:   "Beschlüsse", "Seegru<junk>\xa4n" -> "Seegrün"); dot below after "z"
#:   instead (21 occurrences, confirmed-by-volume Persian/Arabic
#:   transliteration -- see `_MARC8_BARE_COMBINING_BEFORE_OVERRIDES`
#:   for the full word list). The remaining ~48 occurrences were
#:   investigated and left unfixed, for two different reasons, NOT just
#:   "needs a different mark" the way the overload above does:
#:   - A large share (Spanish/Hungarian/Icelandic/Portuguese/Polish/
#:     Catalan names and terms: "Cat<junk>\xa4lica" -> "Católica",
#:     "Gastn<junk>\xa4 Espinosa" -> "Gastón Espinosa", "T<junk>\xa4th"
#:     -> "Tóth", "L<junk>\xa4pez" -> "López", "Jernimos" ->
#:     "Jerónimos") turned out NOT to be a simple wrong-mark case at
#:     all on closer reading: the escape destroyed an entire base
#:     vowel (almost always "ó"), not just a mark -- the same failure
#:     mode already documented for the 3-byte CJK/EACC escape, just
#:     never previously confirmed coming through this bare-byte path.
#:     This mechanism (attach one mark to the letter immediately
#:     before the junk) has no safe way to handle that; attaching a
#:     mark to "t" or "n" or "L" here would be wrong on every level.
#:   - A smaller, genuinely mixed share uses the SAME before-letter for
#:     BOTH the dot-below case above AND this destroyed-vowel case
#:     depending on the specific word ("l"/"L": Tamil "Tamil<junk>\xa4akam"
#:     -> "Tamiḻakam" needs dot-below, but "Theol<junk>\xa4gicos" ->
#:     "Theológicos" is destroyed-vowel; "t": Arabic "Khut<junk>\xa4ba"
#:     -> "Khuṭba" needs dot-below, but "Cat<junk>\xa4lica" is
#:     destroyed-vowel) -- can't be generalized by before-letter alone
#:     the way the confirmed overrides above can.
#: - 0xA6: cedilla (the SAME mark 0xA7 above already produces, not a new
#:   one), but only after "c" (57 of the 97 changed-subfield diffs found
#:   when confirming this, ~59% -- occurrence counts for this byte are
#:   not directly comparable to the others above since "s"/"t" below
#:   were trialed and rejected rather than left uncounted) --
#:   confirmed-by-volume French/Occitan/Portuguese/Turkish words
#:   ("franc<junk>\xa6ais" -> "français", "revelac<junk>\xa6a<junk>\xa8o"
#:   -> "revelação", "Kac<junk>\xa6ar" -> "Kaçar") -- the full-field diff
#:   (fix on vs. off, every record in working/GTU_bibs_0xA6_sample.mrc)
#:   found zero regressions for "c" specifically. Unlike every other
#:   overloaded byte above, this one's dominant before-letters were
#:   actually "s" and "t" (Romanian "ş"/"ţ" comma-below/cedilla, e.g.
#:   "Bucures<junk>\xa6ti" -> "Bucureşti", "Colect<junk>\xa6ia" ->
#:   "Colecţia" -- the same ANSEL cedilla byte 0xF0 Romanian conventionally
#:   reuses for ş/ţ) -- a much larger, cleaner-*looking* cluster than "c"
#:   itself, but NOT added: the same full-field diff found real
#:   regressions the "c" cluster didn't. Record .b11564295 ("Serviciul
#:   Relati<junk>\xa6i Externe Bisericest<junk>\xa6i") has the junk
#:   landing one letter later than the letter that actually needs the
#:   mark in both of its two occurrences (correct target is "Bisericeşti"
#:   -- mark on "s", but the captured before-letter is "t", one position
#:   later) -- the same off-by-one shape already documented for 0xAE's
#:   Akkadian cluster, just discovered via "t" here instead of "h".
#:   Record .b13281069 ("Bucurest<junk>\xa6i :") has the identical
#:   off-by-one on the SAME word ("Bucureşti") that fixes correctly
#:   everywhere else in the corpus -- i.e. the underlying corruption
#:   itself places the junk inconsistently for this cluster, not just
#:   this fixer's regex, so no before-letter refinement can make "t"
#:   safe here. Record .b12911008 is worse: applying the trial override
#:   there doesn't just mis-place one mark, it leaves pymarc's
#:   marc8_to_unicode decoder stuck, turning a long stretch of the
#:   subfield after the first mark into unreadable combining-mark debris
#:   -- the same decoder-interaction regression already documented for
#:   0xA5's rejected "h" case, just via "s"/"t" instead. "s"/"t" stay
#:   unfixed; see HANDOFF.md for the full diff output this is based on.
#:
#: Combining (modifies the letter immediately before the junk run, same
#: mark-before-letter ANSEL order as `_MARC8_DIACRITIC_PAYLOADS`):
_MARC8_BARE_COMBINING_BYTES: dict[str, str] = {
    "\xa3": "\xf2",  # -> ANSEL combining dot below (e.g. h -> ḥ, s -> ṣ)
    "\xa7": "\xf0",  # -> ANSEL combining cedilla (e.g. c -> ç)
    "\xae": "\xf2",  # -> ANSEL combining dot below, restricted (e.g. H -> Ḥ)
    "\xa5": "\xf2",  # -> ANSEL combining dot below, restricted (e.g. r -> ṛ)
    "\xa8": "\xf1",  # -> ANSEL combining ogonek, restricted (e.g. a -> ą)
    "\xa4": "\xe8",  # -> ANSEL combining diaeresis, restricted (e.g. u -> ü)
    "\xa6": "\xf0",  # -> ANSEL combining cedilla, restricted (e.g. c -> ç)
}


#: Byte -> the lowercase before-letters it's safe to fire on, for the
#: overloaded bytes documented in the table comment above. A byte not
#: listed here (0xA3, 0xA7) fires on any letter, same as before this
#: restriction existed. Checked case-insensitively against the matched
#: before-letter; anything outside the listed set is left untouched for
#: `find_suspect_marc8_escapes` to flag instead of risking the wrong mark.
_MARC8_BARE_COMBINING_RESTRICTED_BEFORE: dict[str, str] = {
    "\xae": "h",
    "\xa5": "r",
    "\xa8": "aeu",
    "\xa4": "u",
    "\xa6": "c",
}


#: Byte -> {before-letter (lowercase): the DIFFERENT mark that specific
#: before-letter needs instead of the byte's default mark above.
#: 0xA4 is overloaded with more than one *confirmed* mark depending on
#: the before-letter, not just "default mark or leave alone": after "z"
#: it's combining dot below, not diaeresis (confirmed against all 21
#: occurrences in the full working/GTU_bibs.mrc -- consistently Persian/
#: Arabic transliteration, e.g. "qaz<junk>\xa4iyya" -> "qaẕiyya",
#: "Riz<junk>\xa4a" -> "Riẕa", "Ramaz<junk>\xa4a<junk>\xa4n" ->
#: "Ramaẕān", "Afz<junk>\xa4al" -> "Afẕal", "al-Faz<junk>\xa4l" ->
#: "al-Faẕl", "Rawz<junk>\xa4at" -> "Rawẕat", "Murtaz<junk>\xa4a" ->
#: "Murtaẕa"). An entry here is checked before
#: `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE` and, when it matches,
#: overrides the byte's default mark/restriction entirely for that
#: before-letter.
#:
#: The other ~48 non-u/z occurrences of 0xA4 in the same corpus were
#: investigated and deliberately NOT added here -- see
#: docs/MARC8_DIACRITIC_HANDLING.md's "Investigated, not implemented"
#: section for why (mostly a different, harder corruption shape this
#: mechanism can't safely handle at all: the escape destroyed an entire
#: base vowel, not just its mark).
_MARC8_BARE_COMBINING_BEFORE_OVERRIDES: dict[str, dict[str, str]] = {
    "\xa4": {"z": "\xf2"},  # -> ANSEL combining dot below (e.g. z -> ẕ)
}


#: Standalone (a modifier letter in its own right, not combining with
#: any neighbor -- inserted as-is wherever the junk run sat, letter or
#: word-boundary on either side):
#:
#: 0xB9 was added via the same full-corpus check (2,948 occurrences):
#: Russian "soft sign" romanizations, both word-medial ("Il<junk>\xb9ich"
#: -> "Il'ich") and word-final before punctuation
#: ("nravstvennost<junk>\xb9." -> "nravstvennost'."). 0xB9 is itself a
#: valid, unrelated ANSEL byte (£, the pound sign) when not preceded by
#: the junk signal -- same safety argument as the other bytes above.
_MARC8_BARE_STANDALONE_BYTES: dict[str, str] = {
    "\xbc": "\xae",  # -> ANSEL hamza (modifier letter apostrophe, e.g. Ihya' )
    "\xbb": "\xb0",  # -> ANSEL ayn (modifier letter turned comma, e.g. 'ulum)
    "\xb9": "\xa7",  # -> ANSEL modifier letter prime (soft sign, e.g. Il'ich)
}


#: Zero or more unrecognized, stray escape-byte runs (see
#: `_marc8_escape_run_clusters`'s docstring) directly preceding the
#: real, recognized script-switch escape this fixer targets -- real
#: corrupted data mixes the two, e.g. "Haure" + `\x1bp+\x1bs` (stray,
#: unrecognized) + `\x1b(QB\x1b(B` (the real escape this table resolves).
#: Lazy (`*?`) so the following literal anchor is matched as soon as
#: possible rather than this swallowing part of it.
#:
#: Excludes the bare corruption bytes (`_MARC8_BARE_COMBINING_BYTES`/
#: `_MARC8_BARE_STANDALONE_BYTES`) from what a junk fragment can
#: swallow -- without that exclusion, a single fragment could span
#: right across one of those bytes (and real letters alongside it,
#: e.g. "\x1bs\xa3ya" as ONE fragment), hiding it from
#: `_MARC8_BARE_COMBINING_RE`/`_MARC8_BARE_STANDALONE_RE` entirely and
#: silently dropping it instead of fixing it -- confirmed against
#: record .b10001463's "Ihya" case while building this.
#:
#: Also excludes a fragment whose first byte is "(" -- every real
#: recognized escape piece this fixer cares about, open or close,
#: starts with "(" right after the ESC ("\x1b(QG" opens a switch,
#: "\x1b(B" closes one back to ASCII); every real stray-junk fragment
#: actually seen starts with "p" or "s" instead ("\x1bp+", "\x1bs",
#: "\x1bp("). Without this, a fragment could still swallow a whole
#: recognized escape -- its open ("\x1b(QG" is ESC + 3 non-ESC bytes,
#: beyond a naive length cap alone) or, just as easily, its close
#: ("\x1b(B" is only ESC + 2) -- as "junk", hiding an escape this
#: fixer deliberately left alone (e.g. the Q/G payload's "al-"
#: exception, see `_MARC8_AMBIGUOUS_NEAR_AL_PAYLOADS`) from the
#: bare-byte passes that run after it, letting them erase it even
#: though the escape regex just chose not to -- found and fixed while
#: testing that exact case.
_MARC8_BARE_TRIGGER_BYTES = "".join(
    set(_MARC8_BARE_COMBINING_BYTES) | set(_MARC8_BARE_STANDALONE_BYTES)
)


_MARC8_STRAY_ESCAPE_JUNK = (
    r"(?:\x1b(?!\()[^\x1b" + re.escape(_MARC8_BARE_TRIGGER_BYTES) + r"]{1,2})*?"
)


#: Excludes a "before letter" that's actually the trailing byte of a
#: `\x1b(X` escape sequence (most commonly the "B" of a `\x1b(B` close,
#: but any single-byte charset designator works the same way) rather
#: than real text. Without this, back-to-back script-switches with no
#: real letter between them (e.g. "...\x1b(B\x1b(3L\x1b(B..." -- a close,
#: immediately followed by a *different*, confirmed-payload escape) let
#: the designator byte itself ("B" in that example) get mistaken for the
#: letter that lost its mark, attaching a real mark to a byte that was
#: never text at all -- confirmed against a real production example
#: (working/WTS_bibs_2026-10-01.out record .b11749192, an "Imperfect:"
#: note listing damaged signatures, no diacritic anywhere near it) where
#: this silently spliced a cedilla mark onto the escape machinery itself.
_MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND = r"(?<!\x1b\()"


_MARC8_DIACRITIC_ESCAPE_RE = re.compile(
    _MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND + r"([A-Za-z])" + _MARC8_STRAY_ESCAPE_JUNK
    + r"\x1b\(([" + "".join(re.escape(c) for c, _ in _MARC8_DIACRITIC_PAYLOADS) + r"])(.)"
    # The payload byte is sometimes followed by extra plain-ASCII
    # punctuation/whitespace *before* the closing \x1b(B, instead of
    # after it -- e.g. "Facolta" + grave + " " (trapped here) + close +
    # "di lettere", which should read "Facoltà di lettere" (a common
    # Italian phrase). Checked against the full working/GTU_bibs.mrc:
    # ~81,000 occurrences, the trapped content almost always exactly
    # this -- a space, comma, period, dash, quote, or a short run of
    # them (max 10 chars seen) -- consistent with the closing escape
    # and whatever ordinary punctuation was meant to follow it getting
    # swapped, not with genuine foreign-script text (which would often
    # run longer and isn't restricted to the plain-ASCII-punctuation
    # range this capture is). Restricting the class to printable ASCII
    # only (0x20-0x7E) is what keeps this from ever misfiring on actual
    # multi-character Cyrillic/Arabic content under the same charset
    # switch -- real foreign-script bytes live outside that range.
    + r"([\x20-\x7e]{0,15})\x1b\(B"
    # "after" is normally the next plain letter -- but real examples
    # (working/GTU_bibs_sample50.mrc record .b10001463, "Ihya" + this
    # escape + more stray junk leading straight into a bare hamza byte,
    # see _MARC8_BARE_STANDALONE_BYTES) show this escape sometimes
    # butts directly against a *second*, different corruption instead
    # of a letter. A zero-width lookahead for the next corruption's own
    # leading ESC byte covers that without consuming it, so the
    # following regex pass (bare-combining/bare-standalone) still gets
    # to handle it normally.
    + r"(?:([A-Za-z])|(?=\x1b))"
)


def _marc8_diacritic_replacement(match: "re.Match[str]") -> str:
    before, charset, payload, trapped, after = match.groups()
    mark = _MARC8_DIACRITIC_PAYLOADS.get((charset, payload))
    if mark is None:
        return match.group()  # not one of the confirmed payloads -- leave untouched
    if (charset, payload) in _MARC8_AMBIGUOUS_NEAR_AL_PAYLOADS:
        text = match.string
        radius = _MARC8_AMBIGUOUS_CONTEXT_RADIUS
        window = text[max(0, match.start() - radius):match.end() + radius]
        if "al-" in window:
            return match.group()  # Arabic context -- leave for human review, see table comment
    # ANSEL order: combining byte precedes its base letter; the
    # trapped punctuation belongs right after that letter, same as it
    # would in correctly-encoded text, then whatever plain letter (or
    # nothing, see the lookahead above) follows the escape itself.
    return mark + before + trapped + (after or "")


#: One or more stray escape-byte fragments (same shape and same
#: excluded-byte class as `_MARC8_STRAY_ESCAPE_JUNK`, but non-empty:
#: `+?` not `*?`) directly preceding one of the bare corrupted bytes
#: below -- that ESC-byte run is what makes touching the byte after it
#: safe (see the table's own comment above): an ESC byte never
#: legitimately precedes ordinary text, so its presence is the signal
#: this is the same corruption, not genuine unrelated ANSEL content.
_MARC8_STRAY_ESCAPE_JUNK_NONEMPTY = (
    r"(?:\x1b(?!\()[^\x1b" + re.escape(_MARC8_BARE_TRIGGER_BYTES) + r"]{1,2})+?"
)


_MARC8_BARE_COMBINING_RE = re.compile(
    _MARC8_NOT_ESCAPE_DESIGNATOR_LOOKBEHIND + r"([A-Za-z])" + _MARC8_STRAY_ESCAPE_JUNK_NONEMPTY
    + r"([" + "".join(re.escape(b) for b in _MARC8_BARE_COMBINING_BYTES) + r"])"
)


def _marc8_bare_combining_replacement(match: "re.Match[str]") -> str:
    before, byte = match.group(1), match.group(2)
    override_mark = _MARC8_BARE_COMBINING_BEFORE_OVERRIDES.get(byte, {}).get(before.lower())
    if override_mark is not None:
        return override_mark + before  # ANSEL order: combining byte precedes its base letter
    mark = _MARC8_BARE_COMBINING_BYTES.get(byte)
    if mark is None:
        return match.group()
    allowed_before = _MARC8_BARE_COMBINING_RESTRICTED_BEFORE.get(byte)
    if allowed_before is not None and before.lower() not in allowed_before:
        return match.group()  # overloaded byte outside its confirmed context -- see table comment
    return mark + before  # ANSEL order: combining byte precedes its base letter


_MARC8_BARE_STANDALONE_RE = re.compile(
    _MARC8_STRAY_ESCAPE_JUNK_NONEMPTY
    + r"([" + "".join(re.escape(b) for b in _MARC8_BARE_STANDALONE_BYTES) + r"])"
)


def _marc8_bare_standalone_replacement(match: "re.Match[str]") -> str:
    byte = match.group(1)
    return _MARC8_BARE_STANDALONE_BYTES.get(byte, match.group())


_MARC8_SIGNATURES_NOTE_PREFIXES = ("signatures:", "signature:")


def _fix_marc8_diacritics_in_text(text: str) -> str:
    # Early-printed-book "Signatures: A-C⁴ D²." collation notes
    # (ESTC-style) reuse this same escape machinery for a lost
    # superscript leaf-count digit, not a lost diacritic -- the escape
    # fix can't tell the two apart, so skip it entirely for any
    # subfield/control-field text that is itself such a note. Confirmed
    # empirically: excludes all 8 confirmed false positives in
    # working/WTS_bibs_2026-10-01.out and 0 genuine fixes in
    # working/GTU_bibs.mrc's 310,036 (its lone "Signatures:" match is
    # the identical false positive, not a loss). See
    # docs/MARC8_DIACRITIC_HANDLING.md.
    if text.lstrip().lower().startswith(_MARC8_SIGNATURES_NOTE_PREFIXES):
        return text
    text = _MARC8_DIACRITIC_ESCAPE_RE.sub(_marc8_diacritic_replacement, text)
    text = _MARC8_BARE_COMBINING_RE.sub(_marc8_bare_combining_replacement, text)
    text = _MARC8_BARE_STANDALONE_RE.sub(_marc8_bare_standalone_replacement, text)
    return text


def fix_marc8_diacritic_escapes(parsed: ParsedRecord) -> list[str]:
    """Recover a MARC-8 diacritic mark lost to the same escape-wrapping
    corruption `find_suspect_marc8_escapes` flags, for the specific
    subset of that corruption where the mark alone was swallowed (see
    `_MARC8_DIACRITIC_PAYLOADS`) -- a genuine, verified fix, not a
    guess, because the base letter it restores is the one already
    sitting in the record: "Haure" + <escape> + "au" becomes "Haur" +
    <genuine ANSEL combining-acute byte> + "eau" (the "e" and "au" were
    never touched, only the acute mark between them was). That's still
    raw MARC-8, not the final "Hauréau" -- this function only repairs
    the escape; `transcode_marc8_to_utf8` (which runs immediately after
    this in the real pipeline) turns it into actual UTF-8, through the
    normal MARC-8 transcoding path, same as any other correctly-encoded
    ANSEL diacritic in the file. Only the confirmed high-volume payloads
    in `_MARC8_DIACRITIC_PAYLOADS` are handled; anything else (the
    3-byte CJK/EACC case, or a single-byte payload not in the table) is
    left alone and still reaches `find_suspect_marc8_escapes` for a
    human to review, same as before this function existed.

    Also recovers several related, bare-byte (no recognized escape at
    all) corruptions found in the same way, each preceded by the same
    stray ESC-byte junk this function already strips: Arabic combining
    dot-below (from three different corrupted bytes), French/Occitan
    combining cedilla, Polish/Lithuanian combining ogonek, German
    combining diaeresis, the standalone hamza/ayn modifier letters, and
    the standalone modifier-letter prime (Russian "soft sign"
    romanizations) -- see `_MARC8_BARE_COMBINING_BYTES`/
    `_MARC8_BARE_STANDALONE_BYTES`. Several of these bare bytes are
    overloaded (the same byte stands in for more than one mark
    depending on language/context) and are only fixed within the
    specific before-letter context the full corpus confirmed is safe;
    see `_MARC8_BARE_COMBINING_RESTRICTED_BEFORE`.

    Also recovers a third, much more common variant of the escape
    corruption itself (~81,000 occurrences checked against the full
    working/GTU_bibs.mrc, vs. a few hundred of the plain case above):
    ordinary ASCII punctuation/whitespace that should follow the
    closing escape instead got trapped inside it, before the close --
    "Facolta" + grave + " " (trapped) + close + "di lettere" is really
    "Facoltà di lettere". See `_MARC8_DIACRITIC_ESCAPE_RE`'s own
    comment for why this is restricted to the plain-ASCII-punctuation
    byte range rather than accepting any byte there.

    Skipped entirely for a record already declaring UTF-8 (leader byte
    9 == "a") -- same reasoning as `find_suspect_marc8_escapes`: no raw
    MARC-8 escapes should exist there, and "fixing" one would risk
    corrupting genuine content that happens to look similar.
    """
    if parsed.leader[9:10] == UNICODE_ENCODING_BYTE:
        return []
    details = []
    for f in parsed.fields:
        if f.is_control():
            if f.content and "\x1b" in f.content:
                fixed = _fix_marc8_diacritics_in_text(f.content)
                if fixed != f.content:
                    details.append(
                        f"recovered lost MARC-8 diacritic in ={f.tag}: "
                        f"{f.content!r} -> {fixed!r}"
                    )
                    f.content = fixed
        else:
            new_subfields = []
            changed_codes = []
            for code, data in f.subfields:
                if data and "\x1b" in data:
                    fixed = _fix_marc8_diacritics_in_text(data)
                else:
                    fixed = data
                if fixed != data:
                    changed_codes.append(code)
                    new_subfields.append((code, fixed))
                else:
                    new_subfields.append((code, data))
            if changed_codes:
                f.subfields = new_subfields
                codes = ",".join(f"${c}" for c in changed_codes)
                details.append(f"recovered lost MARC-8 diacritic in ={f.tag} {codes}")
    return details


# --- 0xB2 (destroyed umlaut vowel), lexicon-gated ---------------------
#
# Unlike every other bare byte above, 0xB2 doesn't stand for a combining
# mark: the whole German a/o/u-umlaut letter was destroyed, escape junk
# + 0xB2 sit where it was ("G" + junk + "cke" is "Göcke"). Which vowel
# it was can't be told from the byte or the letter before it, so it is
# only filled when a word list (`--b2-lexicon`, built by
# tools/context_probe/lang_lexicon_probe.py `trim`) agrees: the gap is
# filled with each of a/o/u, and the word is rewritten only when the
# winning vowel has enough lexicon support, a clear majority, and the
# word is long enough (short fragments such as "gr_" were the bad fills).
# The record must also show German evidence (008/041). The fill is a
# lexicon-backed guess, not a verified fix -- it is logged per word as
# `fixed_marc8_b2_umlaut`. On by default when a lexicon file exists at
# DEFAULT_B2_LEXICON_PATH (working/b2_lexicon.json next to this script);
# --b2-lexicon PATH points at a different file, --no-b2-lexicon disables it.
# Measured on the 396 GTU records holding 0xB2: see HANDOFF.md.
_B2_MARKER = r"(?:\x1b(?!\()[^\x1b\xb2]{1,2})+\xb2"


_B2_MARKER_RE = re.compile(_B2_MARKER)


_B2_WORD_RE = re.compile(r"(?:[A-Za-z]|" + _B2_MARKER + r")+")


_B2_GAP = "\x00"


_B2_VOWELS = "aou"


_B2_ANSEL_DIAERESIS = "\xe8"


_B2_MIN_LENGTH = 4


_B2_MIN_SUPPORT = 2


_B2_MIN_CONFIDENCE = 0.8


_B2_MAX_GAPS = 2


# Default location the 0xB2 lexicon is expected at when --b2-lexicon isn't
# given explicitly (same place tools/context_probe/build_b2_lexicon.py writes
# it by default). Resolved relative to this script, not the cwd, so it's
# found the same way no matter where marc_repair.py is invoked from.
DEFAULT_B2_LEXICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "working", "b2_lexicon.json"
)


def load_b2_lexicon(path: str) -> dict[str, dict[str, int]]:
    """Load the JSON file written by `lang_lexicon_probe.py trim`:
    {flattened lowercase key: {NFD variant: count}}."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _b2_record_is_german(parsed: ParsedRecord) -> bool:
    for f in parsed.fields:
        if f.tag == "008" and f.is_control() and (f.content or "")[35:38] == "ger":
            return True
        if f.tag == "041" and not f.is_control():
            if any(code == "a" and data and "ger" in data for code, data in f.subfields):
                return True
    return False


def _b2_resolve_word(word: str, lexicon: dict[str, dict[str, int]]) -> tuple[str, ...] | None:
    """-> the winning vowel per gap in `word` (lowercase, GAP chars), or
    None if the lexicon doesn't back one with enough support/confidence."""
    gaps = [i for i, c in enumerate(word) if c == _B2_GAP]
    if not gaps or len(gaps) > _B2_MAX_GAPS or len(word) < _B2_MIN_LENGTH:
        return None
    fills = [([], word)]
    for _ in gaps:
        fills = [(vs + [v], w.replace(_B2_GAP, v, 1)) for vs, w in fills for v in _B2_VOWELS]
    votes: dict[tuple[str, ...], int] = {}
    for vowels, filled in fills:
        for variant, count in lexicon.get(filled, {}).items():
            marked = set()
            base = -1
            for c in unicodedata.normalize("NFD", variant):
                if unicodedata.category(c) == "Mn":
                    if c == "̈":
                        marked.add(base)
                else:
                    base += 1
            if all(g in marked for g in gaps):
                votes[tuple(vowels)] = votes.get(tuple(vowels), 0) + count
    total = sum(votes.values())
    if total < _B2_MIN_SUPPORT:
        return None
    pick, top = max(votes.items(), key=lambda kv: kv[1])
    if top / total < _B2_MIN_CONFIDENCE:
        return None
    return pick


def _b2_fill_text(text: str, lexicon: dict[str, dict[str, int]]) -> tuple[str, list[str]]:
    """Fill every lexicon-confirmed 0xB2 gap in `text`; -> (new text,
    "before -> after" per word)."""
    out: list[str] = []
    changes: list[str] = []
    pos = 0
    for wm in _B2_WORD_RE.finditer(text):
        word = wm.group()
        if "\xb2" not in word:
            continue
        marked = _B2_MARKER_RE.sub(_B2_GAP, word)
        vowels = _b2_resolve_word(marked.lower(), lexicon)
        if vowels is None:
            continue
        others = [c for c in marked if c != _B2_GAP]
        all_caps = len(others) > 0 and all(c.isupper() for c in others)
        preceding = text[:wm.start()].rstrip(" ")
        sentence_start = (text[wm.start() - 1:wm.start()] != "-" and (
            not preceding or not preceding[-1].isalpha()
            or preceding.split(" ")[-1][:1].isupper()))
        it = iter(vowels)
        pieces = []
        idx = 0
        for piece in re.split("(" + _B2_MARKER + ")", word):
            if piece.endswith("\xb2") and piece.startswith("\x1b"):
                v = next(it)
                upper = all_caps or (idx == 0 and sentence_start)
                pieces.append(_B2_ANSEL_DIAERESIS + (v.upper() if upper else v))
                idx += 1
            else:
                pieces.append(piece)
                idx += len(piece)
        new_word = "".join(pieces)
        out.append(text[pos:wm.start()])
        out.append(new_word)
        pos = wm.end()
        changes.append(f"{marked.replace(_B2_GAP, '_')} -> {new_word.replace(_B2_ANSEL_DIAERESIS, '')}")
    out.append(text[pos:])
    result = "".join(out)
    return result, changes


def fix_marc8_b2_umlaut(parsed: ParsedRecord, lexicon: dict[str, dict[str, int]]) -> list[str]:
    """Fill a destroyed German umlaut vowel (bare 0xB2 behind escape
    junk) with the ANSEL diaeresis + the vowel the lexicon backs. Only
    for records with German evidence, only for confirmed words; raw
    MARC-8 out, `transcode_marc8_to_utf8` makes it UTF-8 afterwards.
    Skipped for UTF-8-declared records. See the block comment above."""
    if parsed.leader[9:10] == UNICODE_ENCODING_BYTE or not _b2_record_is_german(parsed):
        return []
    details = []
    for f in parsed.fields:
        if f.is_control():
            continue
        new_subfields = []
        changed = []
        for code, data in f.subfields:
            if data and "\xb2" in data:
                fixed, changes = _b2_fill_text(data, lexicon)
                if changes:
                    changed.append(f"${code}: " + "; ".join(changes))
                    data = fixed
            new_subfields.append((code, data))
        if changed:
            f.subfields = new_subfields
            details.append(f"filled lost umlaut vowel in ={f.tag} " + " | ".join(changed))
    return details


def find_suspect_marc8_escapes(
    parsed: ParsedRecord,
    corpus_index: "_Marc8CorpusIndex | None" = None,
) -> list[tuple[str, str]]:
    """Flag a MARC-8 script-switching escape (Hebrew/Arabic/Cyrillic/
    Greek/CJK) that produces only a single character, immediately
    embedded between two plain ASCII letters with no word boundary --
    e.g. real production data found via this exact pattern: "Schr" +
    <switch to CJK/EACC> + one CJK character + <switch back> + "inger",
    which decodes correctly per the MARC-8 spec but is almost certainly
    a cataloger-decades-ago mistake for an accented "o" (intending
    "Schrödinger"), not real embedded Chinese.

    This is a strong, narrow heuristic, not a general "any foreign
    script is suspicious" check: genuine embedded non-Latin content
    (a parallel 880 title, a whole Hebrew/CJK phrase) is normally
    several characters long and doesn't sit welded to Latin letters on
    both sides with zero characters of separation. Never auto-fixed --
    there's no safe way to guess what the original character should
    have been; category "suspect_marc8_escape" is for a human to
    review and correct the source cataloging record. Each finding
    includes a suggestion -- a hypothesis, not a correction: "...'s"
    at a word boundary reads as a miskeyed apostrophe (e.g. "who's");
    anything else is framed as a likely lost accented letter in the
    surrounding word (e.g. "Schrodinger", "Leonidas"), with a prompt
    to verify against another edition or an authority record rather
    than an invented replacement -- unless `corpus_index` (see
    `build_marc8_corpus_index`) resolves the surrounding fragment to
    exactly one word already spelled correctly elsewhere in this same
    file, in which case the suggestion names that word specifically.

    Measured against a real 404,957-record file: same-file corpus
    matching only resolves ~2% of findings this way (see HANDOFF.md);
    the rest need an external authority source this tool doesn't
    attempt to reach, so `_record_lccn`/`_record_oclc` surface
    whatever identifier the record already carries instead.

    All findings for one record are merged into a single (category,
    detail) entry, not one per occurrence: the same broken word often
    repeats verbatim across more than one field in the same record
    (e.g. a name in both 100 and 245, or a series title duplicated in
    245 and 830) -- these collapse onto one line listing every tag it
    appears in, rather than repeating the full line per tag. The
    merged detail always has the same two-part shape: a header line
    (the record's own column in `LogEntry.render`) followed by one
    indented bullet line per distinct occurrence. When every distinct
    defect in the record carries the same suggestion, that suggestion
    (and the record's LCCN/OCLC) is stated once in the header and the
    bullets stay bare; otherwise the header holds just the LCCN/OCLC
    (if any) and each bullet states its own suggestion.
    """
    if parsed.leader[9:10] == UNICODE_ENCODING_BYTE:
        return []  # already UTF-8 -- no raw MARC-8 escapes to find
    _CONTEXT_CHARS = 25
    occurrences: list[tuple[str, str, str]] = []  # (window, suggestion, tag)
    for f in parsed.fields:
        texts = [f.content] if f.is_control() else [d for _, d in f.subfields]
        for text in texts:
            if not text or "\x1b" not in text:
                continue
            clusters = _marc8_escape_run_clusters(text)
            for match in _MARC8_SCRIPT_ESCAPE.finditer(text):
                bytes_per_char, charset_name = _MARC8_SCRIPT_CHARSETS[match.group(2)]
                span_start = match.end()
                close = text.find("\x1b", span_start)
                if close == -1:
                    continue  # never switches back -- can't locate "after"
                span = text[span_start:close]
                if len(span) != bytes_per_char:
                    continue  # more than one character -- likely genuine
                # standard return-to-ASCII is "\x1b(B" (3 chars); be
                # conservative and skip anything else rather than
                # guess at where the closing escape actually ends
                if text[close:close + 3] != "\x1b(B":
                    continue
                region_start, region_end = _cluster_containing(clusters, match.start())
                before = text[region_start - 1:region_start]
                after = text[region_end:region_end + 1]
                if not (
                    before.isalpha() and before.isascii()
                    and after.isalpha() and after.isascii()
                ):
                    continue
                word_before_match = re.search(r"[A-Za-z]+$", text[:region_start])
                word_after_match = re.match(r"[A-Za-z]+", text[region_end:])
                word_before = word_before_match.group() if word_before_match else before
                word_after = word_after_match.group() if word_after_match else after
                next_char = text[region_end + len(word_after):region_end + len(word_after) + 1]
                # "...s" ending a word, itself at a word boundary (not
                # followed by another letter) reads as a contraction/
                # possessive far more often than an accented letter
                # would -- e.g. "who's", "it's". Anything else is more
                # likely an accented vowel lost from a proper noun
                # (Schrodinger, Leonidas).
                resolved_word = None
                if word_after == "s" and not next_char.isalpha():
                    suggestion = (
                        f"likely a miskeyed apostrophe, probably "
                        f"\"{word_before}'s\""
                    )
                else:
                    if corpus_index is not None:
                        resolved_word = lookup_marc8_corpus_word(
                            corpus_index, charset_name, word_before, word_after,
                        )
                    if resolved_word is not None:
                        suggestion = (
                            f"likely {resolved_word!r} (found spelled "
                            "correctly elsewhere in this file)"
                        )
                    elif charset_name == "CJK/EACC":
                        suggestion = (
                            "likely an accented letter and the letter "
                            "right after it; verify against another "
                            "source"
                        )
                    else:
                        suggestion = (
                            "likely a miskeyed accented letter; verify "
                            "against another source"
                        )
                window_start = max(0, region_start - _CONTEXT_CHARS)
                window_end = min(len(text), region_end + _CONTEXT_CHARS)
                # a fixed-width window can cut a *neighboring* escape
                # run in half, leaving a leading-\x1b-less fragment
                # that _mask_marc8_escapes can no longer recognize as
                # noise -- snap each boundary out to that cluster's
                # own edge so it's always included whole (and masked
                # whole) rather than left as a raw partial leftover.
                cluster_start, _ = _cluster_containing(clusters, window_start)
                window_start = min(window_start, cluster_start)
                if window_end > 0:
                    _, cluster_end = _cluster_containing(clusters, window_end - 1)
                    window_end = max(window_end, cluster_end)
                window = (
                    _mask_marc8_escapes(text[window_start:region_start])
                    + "[?]"
                    + _mask_marc8_escapes(text[region_end:window_end])
                )
                occurrences.append((window, suggestion, f.tag))
    if not occurrences:
        return []

    merged: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    for window, suggestion, tag in occurrences:
        key = (window, suggestion)
        if key not in merged:
            merged[key] = []
            order.append(key)
        if tag not in merged[key]:
            merged[key].append(tag)

    identifier_parts = []
    lccn = _record_lccn(parsed)
    if lccn:
        identifier_parts.append(f"LCCN: {lccn}")
    oclc = _record_oclc(parsed)
    if oclc:
        identifier_parts.append(f"OCLC: {oclc}")
    identifiers = ". ".join(identifier_parts)

    defect_lines = []
    for window, suggestion in order:
        tags = merged[(window, suggestion)]
        tag_label = ", ".join(f"={t}" for t in tags)
        defect_lines.append((f"{tag_label}: {window!r}", suggestion))

    # Header line states whatever is common to every occurrence --
    # the shared suggestion (if all occurrences agree) and the
    # record's LCCN/OCLC (if known) -- so a reader gets the gist
    # without opening every bullet. Each occurrence then gets its own
    # indented bullet (two tabs deeper than the header's own leading
    # tab in `LogEntry.render`, so a multi-occurrence record's bullets
    # can never be mistaken for the next record's own header line),
    # repeating the suggestion on the bullet only when it isn't
    # already stated once in the header.
    distinct_suggestions = {s for _, s in defect_lines}
    header_parts = []
    if len(distinct_suggestions) == 1:
        header_parts.append(next(iter(distinct_suggestions)))
    if identifiers:
        header_parts.append(identifiers)
    lines = [". ".join(header_parts)]
    for body, suggestion in defect_lines:
        bullet = f"\t\t- {body}"
        if len(distinct_suggestions) > 1:
            bullet += f" -- {suggestion}"
        lines.append(bullet)
    detail = "\n".join(lines)
    return [("suspect_marc8_escape", detail)]


#: (ascii_before, ascii_after) -> set of resolved words, where
#: ascii_before/ascii_after are the plain-ASCII letter runs immediately
#: surrounding one correctly-encoded ANSEL diacritic+base-letter pair
#: found anywhere in this input file. Built once by
#: `build_marc8_corpus_index` and consulted by
#: `find_suspect_marc8_escapes` to upgrade its generic "could be ö, é,
#: ñ, ü" suggestion into a specific word when the surrounding
#: fragment resolves unambiguously. A second index
#: (`minus_one_letter`) is keyed the same way but with the leading
#: letter of `ascii_after` dropped, since the validated CJK/EACC
#: mechanism (see docs/MARC8_ESCAPE_ANALYSIS.md) swallows the
#: diacritic byte, its base letter, AND one more plain ASCII letter --
#: so a CJK/EACC finding's own `word_after` is always missing that
#: extra leading letter relative to the real, correctly-encoded word.
_Marc8CorpusIndex = tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], set[str]]]


#: Combined length of a finding's word_before + word_after below which
#: a corpus lookup is skipped entirely (falls back to the generic
#: hint) -- a 1-2 letter fragment matches too many unrelated words to
#: trust an "unambiguous" hit.
_MIN_CORPUS_MATCH_FRAGMENT_LEN = 3


#: Any MARC-8/ANSEL single-byte combining diacritic (grave, acute,
#: circumflex, tilde, macron, umlaut, etc.) sits in this range and is
#: always followed immediately by the plain ASCII base letter it
#: modifies -- see the real example in docs/MARC8_ESCAPE_ANALYSIS.md
#: ("T" + 0xE8 (diaeresis) + "u" + "bingen" == "Tübingen").
_MARC8_DIACRITIC_BYTE_RE = re.compile("[\xe0-\xfe][A-Za-z]")


def build_marc8_corpus_index(input_path: str, encoding_used: str) -> _Marc8CorpusIndex:
    """Scan the raw bytes of `input_path` once, up front, for every
    correctly-encoded ANSEL diacritic+letter pair (plain MARC-8, no
    escape needed) anywhere in the file, and index each one by the
    plain-ASCII letter runs immediately before/after it. This is the
    "opportunistic same-file corpus lookup" `find_suspect_marc8_escapes`
    uses to turn a generic "could be ö, é, ñ, ü" suggestion into a
    specific word (e.g. "Tübingen") when this same batch already spells
    that word correctly somewhere else -- common in institutional
    cataloging data (place names, publisher cities, repeated subject
    vocabulary), per docs/MARC8_ESCAPE_ANALYSIS.md.

    Degrades gracefully to two empty indexes -- callers then just get
    `None` back from every `lookup_marc8_corpus_word` call, identical
    to not having an index at all -- when `encoding_used` isn't
    "latin-1" (a file already read as UTF-8 has no raw MARC-8 bytes to
    scan) or when pymarc isn't installed (no new hard dependency).
    """
    index_exact: dict[tuple[str, str], set[str]] = {}
    index_minus_one: dict[tuple[str, str], set[str]] = {}
    if encoding_used != "latin-1":
        return index_exact, index_minus_one
    try:
        from pymarc.marc8 import marc8_to_unicode
    except ImportError:
        return index_exact, index_minus_one

    with open(input_path, "rb") as fh:
        text = fh.read().decode("latin-1")

    # Bound the before/after slices to a short window -- a real word
    # fragment is never more than a few dozen letters long, but
    # `text[:pos]`/`text[pos + 2:]` would otherwise copy an ever-growing
    # chunk of the *whole file* on every single match. On a real
    # 421MB/149k-match file this turned a one-pass scan into O(n^2)
    # copying (hundreds of MB copied per match, ~150k times over) and
    # the process never finished -- see HANDOFF.md.
    _MAX_WORD_FRAGMENT_WINDOW = 100

    captured_stderr = io.StringIO()
    for match in _MARC8_DIACRITIC_BYTE_RE.finditer(text):
        pos = match.start()
        before_match = re.search(
            r"[A-Za-z]+$", text[max(0, pos - _MAX_WORD_FRAGMENT_WINDOW):pos]
        )
        after_match = re.match(
            r"[A-Za-z]+", text[pos + 2:pos + 2 + _MAX_WORD_FRAGMENT_WINDOW]
        )
        before = before_match.group() if before_match else ""
        after = after_match.group() if after_match else ""
        try:
            with redirect_stderr(captured_stderr):
                accented = marc8_to_unicode(
                    text[pos:pos + 2].encode("latin-1"), hide_utf8_warnings=True,
                )
        except Exception:
            continue
        if len(accented) != 1:
            continue  # not a clean single accented character -- skip
        word = before + accented + after
        index_exact.setdefault((before, after), set()).add(word)
        index_minus_one.setdefault((before, after[1:]), set()).add(word)
    return index_exact, index_minus_one


def lookup_marc8_corpus_word(
    corpus_index: _Marc8CorpusIndex, charset_name: str, word_before: str, word_after: str,
) -> str | None:
    """Resolve a `find_suspect_marc8_escapes` finding's surrounding
    fragment against `corpus_index` (see `build_marc8_corpus_index`).
    Returns the resolved word only when exactly one word, correctly
    encoded elsewhere in this file, matches -- `None` (never a guess)
    on no match, an ambiguous match (more than one distinct resolved
    word for the same fragment), or too short a fragment to trust.

    CJK/EACC findings are looked up against `index_minus_one` instead
    of the direct fragment: the validated 3-byte mechanism (see
    docs/MARC8_ESCAPE_ANALYSIS.md) swallows one extra plain-ASCII
    letter beyond the diacritic+base-letter pair, so a CJK/EACC
    finding's own `word_after` is always short that one leading letter
    relative to how the word is correctly spelled elsewhere.
    """
    if len(word_before) + len(word_after) < _MIN_CORPUS_MATCH_FRAGMENT_LEN:
        return None
    index_exact, index_minus_one = corpus_index
    key_index = index_minus_one if charset_name == "CJK/EACC" else index_exact
    candidates = key_index.get((word_before, word_after))
    if candidates and len(candidates) == 1:
        return next(iter(candidates))
    return None


#: Detects a different, much rarer MARC-8 corruption than
#: `find_suspect_marc8_escapes`: a run of one or more "{xxxxxx}" groups
#: -- 6 ASCII hex-digit characters wrapped in a literal pair of curly
#: braces, e.g. "{7b6138}{316232}{387d7b}..." -- found in real
#: production data inside 880 (Alternate Graphic Representation)
#: fields. Confirmed against 5 real examples (the only 5 records out
#: of 263,595 in one real export that had this at all): the record's
#: own real MARC-8/EACC bytes got hex-encoded (each byte -> 2 ASCII
#: hex chars) and wrapped in braces every 3 bytes, and in some cases
#: that SAME hex+brace transform was applied a second time to the
#: already-encoded text. Reversing it (strip the braces, concatenate
#: the hex digits, `bytes.fromhex()`, then repeat once more if the
#: result is still plain ASCII and still matches the same brace
#: pattern) recovered either well-formed MARC-8 escape sequences or,
#: in two of the five real cases, literal readable English text (e.g.
#: "= J. Gresham Machen", "(Ian M. Duguid)" -- a romanized name in a
#: linking field).
#:
#: Never auto-fixed: decoding is boundary-sensitive (a stray byte or
#: an incomplete trailing hex digit commonly survives at a chunk's
#: edge in the real examples), so an automatic rewrite risks silently
#: replacing one corruption with a different, equally-wrong one.
#: Category "suspect_hex_encoded_marc8" is for a human to review --
#: see `_decode_hex_brace_run`'s docstring for how the decoded preview
#: in each finding is produced, and how its own DATA LOSS/NO DATA LOSS
#: call is made.
_HEX_BRACE_GROUP_RE = re.compile(r"\{([0-9a-f]{6})\}")


_HEX_BRACE_RUN_RE = re.compile(r"(?:\{[0-9a-f]{6}\}){1,}")


def _decode_hex_brace_run(run: str) -> tuple[bytes, int, bool]:
    """Decode one contiguous run of "{xxxxxx}" groups (as matched by
    `_HEX_BRACE_RUN_RE`) back to raw bytes. Strips the braces,
    concatenates the hex digits, and hex-decodes; if that result is
    itself a plain-ASCII string that still matches the same brace
    pattern, decodes it a second time (see
    `find_suspect_hex_encoded_marc8`'s docstring for why the real
    examples that produced this needed anywhere from one to two
    passes). Never raises: an odd number of leftover hex digits (half
    a byte with nowhere to go) is simply dropped and reported back via
    the `clean` flag, since that itself is evidence of boundary damage
    rather than a reason to give up on the rest.

    Returns (decoded_bytes, layers_decoded, clean) -- `layers_decoded`
    is 1 or 2, and `clean` is False if any layer had to drop a
    leftover odd hex digit to decode at all.
    """
    groups = _HEX_BRACE_GROUP_RE.findall(run)
    hexstr = "".join(groups)
    clean = len(hexstr) % 2 == 0
    if not clean:
        hexstr = hexstr[:-1]
    layer1 = bytes.fromhex(hexstr)
    try:
        layer1_text = layer1.decode("ascii")
    except UnicodeDecodeError:
        return layer1, 1, clean
    if not _HEX_BRACE_RUN_RE.search(layer1_text):
        return layer1, 1, clean
    groups2 = _HEX_BRACE_GROUP_RE.findall(layer1_text)
    hexstr2 = "".join(groups2)
    if len(hexstr2) % 2:
        hexstr2 = hexstr2[:-1]
        clean = False
    return bytes.fromhex(hexstr2), 2, clean


def _hex_brace_decode_looks_recoverable(decoded: bytes, clean: bool) -> bool:
    """True if `decoded` (from `_decode_hex_brace_run`) looks like
    genuine MARC-8/plain-text content rather than noise -- i.e. this
    finding's "NO DATA LOSS" vs. "POSSIBLE DATA LOSS" call. A dropped
    leftover hex digit (`clean` False) is itself proof at least one
    byte couldn't be reconstructed, so that alone forces "POSSIBLE
    DATA LOSS" regardless of how the rest decodes.

    A literal "{" or "}" surviving in the decoded text is also treated
    as unrecoverable, even if the rest of the heuristic below would
    pass -- real production example (WTS_bibs_2026-10-01 record
    .b11188236, 880 $b): a boundary-shifted decode left a SECOND,
    differently-shaped brace run behind ("{uD574}{uC11D}{uC790}",
    5 characters per group, not `_HEX_BRACE_GROUP_RE`'s 6 hex digits)
    mixed into otherwise-readable text ("...oP_)· ... = Introduction
    to biblical hermeneutics /c..."), which the printable-ratio check
    alone was happy to call "recoverable" even though it plainly isn't
    -- ordinary MARC-8/plain-text content essentially never contains a
    literal curly brace, so its presence here is itself the signal.
    """
    if not clean or not decoded:
        return False
    text = decoded.decode("latin-1")
    if "{" in text or "}" in text:
        return False
    if "\x1b" in text:
        return True  # a MARC-8 escape sequence survived the decode
    printable = sum(1 for c in text if c.isprintable() or c in "\t\n")
    return printable / len(text) >= 0.9


def _marc8_bytes_to_readable_preview(decoded: bytes) -> str:
    """Render `decoded` (raw MARC-8 bytes, from `_decode_hex_brace_run`)
    as human-readable Unicode text for a log preview, the same way
    `transcode_marc8_to_utf8` converts real record content -- without
    this, a genuinely-recovered MARC-8 escape sequence or high-bit
    EACC/CJK byte shows up `repr()`'d as raw `\\x1b`/`\\xNN` control
    bytes, unreadable to a cataloger even though the decode succeeded.
    Falls back to the raw latin-1 string (the previous behavior) if
    pymarc isn't installed or the bytes don't actually transcode
    cleanly -- this is a best-effort preview, not a correctness check,
    so any failure here is swallowed rather than raised.
    """
    try:
        from pymarc.marc8 import marc8_to_unicode
    except ImportError:
        return decoded.decode("latin-1")
    try:
        captured_stderr = io.StringIO()
        with redirect_stderr(captured_stderr):
            result = marc8_to_unicode(decoded, hide_utf8_warnings=True)
        if captured_stderr.getvalue():
            return decoded.decode("latin-1")
        return str(result)
    except Exception:
        return decoded.decode("latin-1")


def find_suspect_hex_encoded_marc8(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag a run of "{xxxxxx}" (6 hex-digit characters in literal
    curly braces) in any field's content -- see the module-level
    comment above `_HEX_BRACE_GROUP_RE` for what actually produces
    this and how it's confirmed to decode. Each finding includes the
    decoded preview -- rendered as readable Unicode text via
    `_marc8_bytes_to_readable_preview` when the decode looks
    recoverable, or the raw latin-1 bytes otherwise -- followed by an
    explicit data-loss call (see `_hex_brace_decode_looks_recoverable`)
    and a succinct description of the hex-brace run itself. Original
    surrounding context -- up to 10 characters immediately before and
    after the run, never the run itself, which can be very long -- is
    only appended when the decode is NOT recoverable; once the real
    text has been recovered, showing where the raw "{xxxxxx}" run sat
    in the field adds nothing a reviewer needs. Detect-only -- never
    auto-fixed.
    """
    findings: list[tuple[str, str]] = []
    _CONTEXT_CHARS = 10
    for f in parsed.fields:
        texts = [("", f.content)] if f.is_control() else list(f.subfields)
        for code, text in texts:
            if not text or "{" not in text:
                continue
            for match in _HEX_BRACE_RUN_RE.finditer(text):
                run = match.group()
                n_groups = len(_HEX_BRACE_GROUP_RE.findall(run))
                decoded, layers, clean = _decode_hex_brace_run(run)
                recoverable = _hex_brace_decode_looks_recoverable(decoded, clean)
                preview = (
                    _marc8_bytes_to_readable_preview(decoded)
                    if recoverable else decoded.decode("latin-1")
                )
                # Original context only earns its keep when the decode
                # isn't recoverable -- if we already recovered the real
                # text, showing where the "{xxxxxx}" run sat in the raw
                # field adds nothing a reviewer needs. Even then, show
                # only up to 10 characters on each side of the run, not
                # the run itself -- it's already summarized above by
                # n_groups/layers and can be very long (many groups).
                context_suffix = ""
                if not recoverable:
                    before = text[max(0, match.start() - _CONTEXT_CHARS):match.start()]
                    after = text[match.end():match.end() + _CONTEXT_CHARS]
                    context_suffix = f"; context: {before!r}...{after!r}"
                subfield_label = f" ${code}" if code else ""
                counts = (
                    f"{n_groups} group{'s' if n_groups != 1 else ''}, "
                    f"{layers} layer{'s' if layers != 1 else ''}"
                )
                if recoverable:
                    body = (
                        f"recovered {preview!r} ({counts}); "
                        "verify against source"
                    )
                else:
                    body = (
                        f"POSSIBLE DATA LOSS, doesn't decode: {preview!r} "
                        f"({counts}){context_suffix}"
                    )
                findings.append((
                    "suspect_hex_encoded_marc8",
                    f"={f.tag}{subfield_label}: {body}",
                ))
    return findings


def transcode_marc8_to_utf8(parsed: ParsedRecord) -> tuple[bool, list[str]]:
    """Convert every field's text from MARC-8 to Unicode in place and flip
    the leader's encoding byte to "a". Returns (False, []) (no-op) if the
    record already declares Unicode encoding.

    A data field's subfield whose MARC-8 content fails to transcode
    (genuinely malformed bytes, e.g. a truncated escape sequence or a
    multi-byte CJK/EACC character cut off at the end of a field) is
    isolated to just that subfield: it's dropped (and the whole field
    along with it, if the dropped subfield was $a or nothing usable is
    left in the field afterward), logged in the returned `removed_details`
    list (category "removed_untranscodable_subfield" -- see `main`), and
    every other field still converts and the leader still flips to UTF-8
    normally. This is deliberate: the alternative (aborting the whole
    record over one already-corrupted field) would leave the rest of a
    large, otherwise-clean record stuck in MARC-8 for no reason, and the
    unrecoverable bytes are being discarded outright here, not guessed
    at, so isolating the damage is safe.

    Raises RuntimeError if pymarc isn't installed -- run `pip install -r
    requirements.txt` first -- or if a *control* field's content fails to
    transcode. Control fields are fixed-format and essentially never
    carry MARC-8 escape/high-bit content, so this rare case isn't given
    the same per-subfield isolation as data fields; it still aborts the
    whole record (nothing in `parsed` is changed), the same as every
    field used to behave before this per-subfield isolation existed. The
    raised message names the specific tag and shows ~10 characters of
    context before and ~20 after the actual error position (raw `\x1b`
    escape bytes replaced with a readable `<escape>` marker, same
    convention as `find_suspect_marc8_escapes`), so a human reviewing
    the log has enough to actually find and judge the defect, not just
    a bare byte offset. For a truncated multi-byte character
    specifically, the error position is always past the field's own
    end (that's what "truncated" means), so the context necessarily
    stops at the field's last byte -- marked explicitly with a trailing
    "<-- field ends here", with the message itself stating how many
    more bytes the encoding expected but didn't get.
    """
    if parsed.leader[9:10] == UNICODE_ENCODING_BYTE:
        return False, []
    try:
        from pymarc.marc8 import marc8_to_unicode
    except ImportError as exc:
        raise RuntimeError(
            "--transcode-marc8 requires pymarc (pip install -r requirements.txt)"
        ) from exc

    def convert(text: str) -> str:
        # Fast path: MARC-8 only diverges from plain ASCII via either a
        # charset-switching escape (always starts with the ESC control
        # character, U+001B) or a high-bit byte (>= 0x80, used by
        # ANSEL/Hebrew/Cyrillic/Greek/Arabic single-byte letters). If
        # neither is present, the text is identical whether read as
        # MARC-8 or already-UTF-8, so there's nothing to convert --
        # skipping this pure-Python per-character state machine for
        # those fields is a real, measured win: on a real 91MB/48k
        # record file, 99.4% of field values qualified for this
        # fast path, and it cut a 3.4x slowdown down to roughly 1.1x.
        if "\x1b" not in text and text.isascii():
            return text
        # pymarc's marc8_to_unicode expects raw MARC-8 BYTES -- it
        # detects charset-switching escape sequences by comparing
        # slices against byte literals (e.g. `marc8_string[pos:pos+1]
        # == b"\x1b"`), which is always False when given a str, so
        # escape recognition silently never fires. This was a real
        # bug: any content needing an escape (Hebrew, Arabic,
        # Cyrillic, Greek, CJK/EACC, super/subscripts) came out as
        # garbled Latin-looking text instead of raising an error --
        # e.g. a real Hebrew 880 field decoded as "(2kzlgbd(B" instead
        # of "כתלחגה". Plain ANSEL diacritics (no escape needed) still
        # worked, which is why this wasn't caught earlier. `text` is
        # latin-1-decoded 1:1 from the original bytes when
        # `_read_text_with_encoding` used latin-1 for the whole file, so
        # re-encoding with latin-1 recovers those exact original bytes
        # losslessly -- but when the whole file was instead decoded as
        # UTF-8 (the common case for a file that's mostly already UTF-8
        # with a handful of leftover un-transcoded MARC-8 records), any
        # byte that wasn't valid UTF-8 was preserved not as a literal
        # latin-1 code point but as a surrogate-escaped character
        # (U+DC80-U+DCFF, see `errors="surrogateescape"` in
        # `_read_text_with_encoding`) -- plain `.encode("latin-1")`
        # can't represent those at all and raises UnicodeEncodeError.
        # `errors="surrogateescape"` on the encode reverses that escaping
        # exactly (recovering the original byte) when it was applied,
        # and is a no-op otherwise, so it's correct for both cases.
        #
        # pymarc doesn't raise for a truncated multi-byte (CJK/EACC)
        # character -- it writes a warning to stderr and substitutes a
        # blank space, which would otherwise discard this defect
        # silently (see `_MARC8_MULTIBYTE_TRUNCATED_RE`). Capture
        # stderr around the call so that warning can be turned into a
        # proper logged failure instead.
        captured_stderr = io.StringIO()
        with redirect_stderr(captured_stderr):
            result = marc8_to_unicode(
                text.encode("latin-1", errors="surrogateescape"), hide_utf8_warnings=True
            )
        warning = captured_stderr.getvalue()
        if warning:
            match = _MARC8_MULTIBYTE_TRUNCATED_RE.search(warning)
            if match:
                raise _Marc8MultibyteTruncated(warning.strip(), int(match.group(1)))
            raise RuntimeError(warning.strip())
        return result

    def convert_labeled(text: str, label: str) -> str:
        # Wraps `convert` to re-raise with `label` (e.g. "=500 $a")
        # and ~10 characters of context before / ~20 after the actual
        # error position -- a bare UnicodeDecodeError only names a byte
        # offset, giving a human nothing to go on to find the field
        # that actually failed or judge whether the surrounding text
        # gives away what was intended.
        try:
            return convert(text)
        except UnicodeDecodeError as exc:
            before = text[max(0, exc.start - 10):exc.start]
            bad = text[exc.start:exc.end]
            after = text[exc.end:exc.end + 20]
            raise RuntimeError(
                f"{label} ({exc.reason}); context: {before!r} >>> {bad!r} <<< {after!r}"
            ) from exc
        except _Marc8MultibyteTruncated as exc:
            raw = text.encode("latin-1")
            start = max(0, exc.byte_pos - 10)
            # `end` is always exactly `len(raw)` here -- `exc.byte_pos`
            # (the position pymarc expected the character to extend to)
            # is by definition past the actual end of the field, which
            # is the whole reason this is "truncated" -- so there is
            # never anything to show past the field's own last byte,
            # no matter how far `end` reaches.
            end = min(len(raw), exc.byte_pos + 20)
            # decode defensively -- this is a byte-offset window into
            # raw MARC-8, which may not land on a clean latin-1
            # boundary around a multi-byte escape, but latin-1 maps
            # every byte value so this never actually raises.
            context = raw[start:end].decode("latin-1")
            # replace the raw escape byte with a short readable marker --
            # repr()'ing \x1b as-is is unreadable to a non-technical
            # reader, same convention as find_suspect_marc8_escapes.
            context_display = context.replace("\x1b", "<escape>")
            missing = exc.byte_pos - len(raw)
            raise RuntimeError(
                f"{label} (MARC-8 encoding expected {missing} more byte(s) "
                "than the field provided -- character cut off); "
                f"context: {context_display!r} <-- field ends here"
            ) from exc

    # Compute every field's converted value BEFORE mutating `parsed` at
    # all. A *control* field that fails to convert still aborts
    # everything (see docstring) -- computing into plain dicts first
    # means that failure leaves `parsed` completely untouched, same as
    # before this function could isolate a data-field failure. A *data*
    # field subfield that fails is instead recorded in `removed_details`/
    # `drop_field` below and never raises past this point.
    new_control_content = {}
    new_subfields = {}
    drop_field = set()
    removed_details = []
    for idx, f in enumerate(parsed.fields):
        if f.is_control():
            if f.content:
                new_control_content[idx] = convert_labeled(f.content, f"={f.tag}")
            continue
        kept_subfields = []
        whole_field_broken = False
        for code, data in f.subfields:
            try:
                converted = convert_labeled(data, f"={f.tag} ${code}")
            except RuntimeError as exc:
                if code == "a":
                    removed_details.append(
                        f"{exc}; dropped $a and the rest of ={f.tag} with it "
                        "(nothing usable without it)"
                    )
                    whole_field_broken = True
                    break
                removed_details.append(
                    f"{exc}; dropped just ${code}, rest of ={f.tag} kept"
                )
                continue
            kept_subfields.append((code, converted))
        if whole_field_broken or (f.subfields and not kept_subfields):
            drop_field.add(idx)
        else:
            new_subfields[idx] = kept_subfields

    new_fields = []
    for idx, f in enumerate(parsed.fields):
        if idx in drop_field:
            continue
        if idx in new_control_content:
            f.content = new_control_content[idx]
        if idx in new_subfields:
            f.subfields = new_subfields[idx]
        new_fields.append(f)
    parsed.fields = new_fields

    parsed.leader = parsed.leader[:9] + UNICODE_ENCODING_BYTE + parsed.leader[10:]
    return True, removed_details
