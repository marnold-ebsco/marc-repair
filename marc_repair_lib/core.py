"""Record boundary detection, Mode 1/Mode 2 reconstruction, streaming and assembly for
marc_repair. Imports only marc_repair_lib.common."""

from __future__ import annotations

import codecs
import re
from typing import Callable, Iterator

from .common import (
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
