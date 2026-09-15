"""Repair broken MARC21 (ISO 2709) records and patch missing/required fields.

===============================================================================
HOW TO USE
===============================================================================

Two different failure modes land here, and this tool auto-detects which one
it's looking at:

  MODE 1 -- "intact but the leader/directory lies" (the common real-world
  case: some byte(s) in the leader or directory got mangled -- e.g. a control
  character mis-transcoded through the wrong encoding -- so the declared
  record length or field offsets no longer match reality; import tools
  reject this as "bad length" / "invalid directory"). The actual field data
  still has its real 0x1E field terminators and 0x1F subfield delimiters
  intact, so this mode ignores the stale declared lengths entirely and
  re-derives every field's true boundaries from those real delimiters, then
  rebuilds a correct leader + directory around them. Fast, deterministic, no
  guessing.

  MODE 2 -- "delimiters are just gone" (e.g. someone copy/pasted a record
  through a chat box, email, or spreadsheet cell that silently drops raw
  control bytes). Here there is no real 0x1E/0x1F left to find. This mode
  falls back to the still-intact *directory* (tag/length/start for every
  field -- plain digits, never binary, so paste-proof) as ground truth, and
  searches for the unique way to re-insert delimiters that makes every
  field's reconstructed length match what the directory declares, with the
  very last field ending exactly at the end of the record. When more than
  one insertion is possible, or the search runs out of its work budget
  before finding exactly one, the field is reported UNRESOLVED and needs a
  manual override (see OVERRIDES FILE below) -- it is never silently
  guessed. Be aware this genuinely can't always be automatic: a long
  free-text field whose valid subfield codes include common English
  letters (245's $a/$b/$c over a title, say) can have several
  structurally-valid splits that only human judgment -- recognizing a real
  word or name boundary versus an accidental one -- can tell apart. Short
  fields and fields with a small/unusual code set (dates, numbers, most
  control-like fields) resolve automatically just fine; expect to supply
  overrides for the free-text ones in a record with no real delimiters
  left at all.

Quick examples:

    # Fix a file with a corrupted leader/directory (Mode 1 kicks in
    # automatically). Output defaults to INPUT_fixed.mrc next to the input.
    python marc_repair.py bad_length_bib.mrc

    # Same, but pick the output path explicitly and also dump a
    # human-readable .mrk version for review.
    python marc_repair.py bad_length_bib.mrc -o out.mrc --mrk out.mrk

    # A record is missing 245 or 008 -- a placeholder is added by default
    # (245: 00 $aNo title; 008: a fixed generic default), each logged.
    # Supply real content per-record instead with --ensure-field (which
    # takes priority over the placeholder); repeatable, each spec is
    # TAG:INDICATORS:CODE=VALUE, or TAG:CONTENT for a control field.
    python marc_repair.py bad_missing245_bib.mrc --ensure-field "245:00:a=Real title"
    python marc_repair.py bad_bib.mrc \
        --ensure-field "008:780615s19uu    xx a                    d"

    # Repair text that lost ALL its delimiters (Mode 2), and supply an
    # override for a field the automatic solver flagged as ambiguous.
    python marc_repair.py pasted_records.txt --overrides overrides.json

    # Some fields require a non-empty $a to mean anything (e.g. 650 with no
    # $a is just a bare subject subdivision, not a subject) -- by default
    # such fields are removed (see required_a_tags.txt, editable) and
    # non-empty content that's discarded is timestamp-logged. A subfield
    # code that isn't a lowercase letter or digit, a data field with 0 or 1
    # indicator characters instead of 2, and a field with no non-empty
    # subfields at all are also fixed by default. Turn any of these off
    # with --no-strip-missing-required-a, --no-strip-invalid-subfield-codes,
    # --no-fix-bad-indicators, --no-strip-empty-fields respectively.
    python marc_repair.py bad_bib_mandatoryfields.mrc

    # A record legitimately declares legacy MARC-8/ANSEL encoding (leader
    # byte 9 blank, not "a") -- not a defect on its own, but most modern
    # systems expect UTF-8. Convert it and flip the leader byte accordingly.
    # Requires pymarc: pip install -r requirements.txt
    python marc_repair.py bad_bib_badescape.mrc --transcode-marc8

    # A file too large to comfortably hold in memory as a single string
    # (many millions of records) -- streamed automatically; no flag needed.
    python marc_repair.py huge_export.mrc

OVERRIDES FILE (only needed for Mode 2 UNRESOLVED fields) -- a JSON file
shaped like:

    {
      "<record_index>": {
        "<field_index>": {"indicators": "  ", "subfields": [["a", "..."], ["b", "..."]]}
      }
    }

Both indices are 0-based, in the order fields/records appear. Run once
without --overrides first; any UNRESOLVED field is reported on stderr with
its tag and the raw text still needing a split, which you copy into this file.

Things this tool does NOT do automatically, because they're genuinely
ambiguous, are content (not structural) issues, or need a human-supplied
default:

  * Decide a Mode 2 field's subfield split when more than one split matches
    the declared length and the tag's known subfield codes -- reported as
    UNRESOLVED, fixed via the overrides file above.
  * Detect a literally doubled proxy-URL prefix in $u, or a duplicate
    record identifier across records -- `find_suspicious_fields()` /
    `find_duplicate_identifiers()` flag and timestamp-log these (see
    --log) but never guess a correction; there isn't a safe one.
  * A tag that isn't 3 numeric digits and a missing 008/245 ARE fixed by
    default now (renamed to an unused 9XX tag; a placeholder 008/245
    inserted) -- these are logged as placeholders (008/245: FIXED;
    9XX rename and other default-driven fixes: INFORMATIONAL) rather than
    silently invented, and each has a --no-... flag to leave it flagged
    instead if you'd rather supply the real value yourself.
  * A record over 99,999 bytes total is still written out correctly, using
    the sentinel MARC21's own leader spec documents for exactly this case
    (the leader declares length "99999" instead of the real value; any
    reader that finds the record's real end from its terminator -- as this
    tool's own Mode 1 always does -- reads it back correctly regardless).
    Logged as fixed; nothing is lost. What's genuinely unfixable is a
    single field over 9,999 bytes or a base address over 99,999 (the
    directory's own length/position fields have no such sentinel, and a
    reader needs the real base address to find field data at all) --
    those are passed through unchanged with a log entry.

Every removal, warning, transcode, and added-field notice from a run goes
into ONE combined, timestamped log file (default OUT_log_TIMESTAMP.log, see
--log). A record that can't be auto-repaired at all is still written to the
output -- unchanged, exactly as it came in -- rather than being dropped, and
gets its own UNRESOLVED line in that same log; the output file always has
the same number of records as the input.

===============================================================================
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

SUBFIELD = "\x1f"
FIELDTERM = "\x1e"
RECTERM = "\x1d"

# Known MARC21 bibliographic subfield codes per tag. Not exhaustive -- extend as
# needed. Tags not listed fall back to FALLBACK_CODES (permissive: any lowercase
# letter or digit), which is more likely to produce ambiguous splits.
KNOWN_SUBFIELD_CODES: dict[str, set[str]] = {
    "010": set("az"),
    "015": set("a2"),
    "020": set("acz"),
    "022": set("ayz"),
    "024": set("a2z"),
    "035": set("az9"),
    "040": set("abcde"),
    "041": set("abdefghjkmn2"),
    "043": set("ac2"),
    "049": set("a"),
    "050": set("ab"),
    "082": set("a2"),
    "088": set("az"),
    "090": set("ab"),
    "100": set("abcdq"),
    "110": set("ab"),
    "111": set("acdn"),
    "130": set("a"),
    "240": set("a"),
    "245": set("abcfghknps"),
    "246": set("abfi"),
    "250": set("a"),
    "254": set("a"),
    "255": set("ab"),
    "260": set("abc"),
    "263": set("a"),
    "300": set("abc"),
    "306": set("a"),
    "310": set("a"),
    "336": set("ab2"),
    "337": set("ab2"),
    "338": set("ab2"),
    "362": set("a"),
    "440": set("av"),
    "490": set("a"),
    "500": set("a"),
    "501": set("a"),
    "502": set("abcdgo"),
    "504": set("a"),
    "505": set("a"),
    "506": set("a"),
    "508": set("a"),
    "510": set("abc"),
    "511": set("a"),
    "515": set("a"),
    "520": set("a"),
    "521": set("a"),
    "522": set("a"),
    "524": set("a"),
    "525": set("a"),
    "526": set("a"),
    "530": set("a"),
    "533": set("abcdefmn"),
    "534": set("abc"),
    "538": set("a"),
    "541": set("a"),
    "546": set("a"),
    "550": set("a"),
    "555": set("a"),
    "561": set("a"),
    "563": set("a"),
    "580": set("a"),
    "581": set("a"),
    "586": set("a"),
    "600": set("abcdqt"),
    "610": set("ab"),
    "611": set("a"),
    "630": set("a"),
    "648": set("ay"),
    "650": set("abcdegvxyz012"),
    "651": set("axyz"),
    "653": set("a"),
    "654": set("abey"),
    "655": set("aby2vxyz"),
    "700": set("abcdejqt4"),
    "710": set("ab"),
    "711": set("acdn"),
    "730": set("a"),
    "740": set("a"),
    "752": set("abcd"),
    "760": set("atw"),
    "762": set("atw"),
    "765": set("atw"),
    "767": set("atw"),
    "770": set("atw"),
    "772": set("atw"),
    "773": set("atw"),
    "774": set("atw"),
    "775": set("atw"),
    "776": set("acwz"),
    "777": set("atw"),
    "780": set("atw"),
    "785": set("atw"),
    "786": set("atw"),
    "787": set("atw"),
    "800": set("abcdqt"),
    "810": set("ab"),
    "811": set("acdn"),
    "830": set("av"),
    "852": set("abchj"),
    "856": set("3uyz"),
    "994": set("ab"),
    "999": set("acdefgilmnprstuwx"),
}
FALLBACK_CODES = set("abcdefghijklmnopqrstuvwxyz0123456789")

CONTROL_TAG_LIMIT = 10  # tags below "010" are control fields: no indicators/subfields

# A MARC directory entry is 12 chars; even a record with an unrealistically
# large 2000 fields would need under 24,000 chars of directory. Bounding the
# slice handed to parse_directory() to this matters a lot in practice: when
# scanning a whole multi-record file at an absolute position, slicing
# "everything from here to the end of the file" to look for a directory
# that's actually only ~200 bytes long turns an O(1) lookup into an O(file
# size) one -- and since that slice is taken once per record, the whole
# scan becomes O(records * file size) instead of O(file size).
MAX_DIRECTORY_SCAN_LEN = 24_000


class RepairError(Exception):
    """Raised when a record can't be parsed at all (leader/directory corrupt)."""


class Ambiguous(Exception):
    """A field's subfield split isn't unique; needs a manual override."""

    def __init__(self, tag: str, content: str, candidates: list[list[tuple[str, str]]]):
        self.tag = tag
        self.content = content
        self.candidates = candidates
        super().__init__(f"tag {tag}: {len(candidates)} possible subfield splits for {content!r}")


@dataclass
class DirEntry:
    tag: str
    length: int
    start: int


@dataclass
class Field_:
    tag: str
    indicators: str | None  # None for control fields
    subfields: list[tuple[str, str]] | None  # None for control fields
    content: str | None = None  # control-field raw content

    def is_control(self) -> bool:
        return self.indicators is None

    def delimited(self) -> str:
        if self.is_control():
            return (self.content or "") + FIELDTERM
        body = self.indicators + "".join(
            SUBFIELD + code + data for code, data in self.subfields
        )
        return body + FIELDTERM


@dataclass
class ParsedRecord:
    leader: str
    entries: list[DirEntry]
    fields: list[Field_] = field(default_factory=list)
    unresolved: list[tuple[int, DirEntry, str, Exception]] = field(default_factory=list)
    # (tag, spaces_added) for each field corrected during parsing itself
    # (currently only the default-on indicator-padding fix, see
    # --no-fix-bad-indicators, does this) -- unlike the other fixes, which
    # run as a separate pass after parsing and are logged directly by the
    # caller, this correction has to happen inside Mode 1's own parse, so
    # it's carried here for the caller to log.
    indicator_fixes: list[tuple[str, int]] = field(default_factory=list)


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
            length, start = int(chunk[3:7]), int(chunk[7:12])
            if start != cum:
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
    record's `.indicator_fixes` as (tag, spaces_added) so the caller can log
    it. Off by default at this function's level -- the CLI itself passes
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
    indicator_fixes: list[tuple[str, int]] = []
    for entry in entries:
        end = text.find(FIELDTERM, pos)
        if end == -1:
            return None
        content = text[pos:end]
        # a non-numeric tag (e.g. "24A" from directory corruption) can't
        # be a real control tag -- those are always 001-009 -- so treat
        # it as a data field, the shape real corruption overwhelmingly
        # produces; fix_invalid_tags renames it to a valid 9XX tag later
        if entry.tag.isdigit() and int(entry.tag) < CONTROL_TAG_LIMIT:
            fields.append(Field_(entry.tag, None, None, content=content))
        else:
            if fix_bad_indicators:
                content, spaces_added = _pad_short_indicators(content)
                if spaces_added:
                    indicator_fixes.append((entry.tag, spaces_added))
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
                "holdings_bad_directory", f"tag {entry.tag}: field terminator not found"
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
                f"tag {entry.tag}: bad indicators ({spaces_needed} character(s) "
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
                    f"tag {entry.tag}: invalid subfield code {code!r}",
                ))
            elif not data:
                issues.append((
                    "holdings_null_identifier",
                    f"tag {entry.tag}: subfield ${code} has no data (null identifier)",
                ))
        pos = end + 1
    return issues, record_id


def check_holdings_records(
    path: str, log_path: str, chunk_size: int = 16 * 1024 * 1024
) -> int:
    """Run `check_holdings_record` over every record in a holdings file
    (e.g. one of `split_bib_holdings`'s outputs) and write every issue
    found to its own combined log at `log_path`, grouped the same way as
    the main repair log (see `write_log`) -- everything lands in NOT
    FIXED, since this function only detects, never modifies. For actual
    repair, see `repair_holdings_records`, which the CLI's
    `--split-bib-holdings` uses instead.
    Returns the number of records that had at least one issue.
    """
    encoding = detect_encoding(path)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_entries: list[LogEntry] = []
    n_flagged = 0
    idx = 0
    # See the matching comment in `split_bib_holdings` -- a cursor plus
    # periodic compaction avoids re-copying the whole buffer on every
    # single record.
    compact_threshold = 2 * chunk_size

    def _check(raw: bytes) -> None:
        nonlocal n_flagged, idx
        rec_text = raw.decode(encoding)
        issues, record_id = check_holdings_record(rec_text, encoding)
        if issues:
            n_flagged += 1
            for category, detail in issues:
                log_entries.append(LogEntry(category, False, ts, idx, record_id, detail))
        idx += 1

    with open(path, "rb") as fh:
        buf = b""
        pos = 0
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            buf += chunk
            while True:
                term = buf.find(b"\x1d", pos)
                if term == -1:
                    break
                _check(buf[pos:term + 1])
                pos = term + 1
            if pos > compact_threshold:
                buf = buf[pos:]
                pos = 0
        remainder = buf[pos:]
        if remainder:
            _check(remainder)
    if log_entries:
        write_log(log_path, log_entries)
    return n_flagged


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
    decoder = codecs.getincrementaldecoder("utf-8")() if encoding == "utf-8" else None

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
    field_byte_chunks = [f.delimited().encode(encoding) for f in parsed.fields]
    # build directory from field lengths (they must match declared lengths --
    # verified already during parsing, but recompute here to be self-consistent)
    dir_entries = []
    running = 0
    for f, chunk in zip(parsed.fields, field_byte_chunks):
        length = len(chunk)
        if length > 9999:
            raise RepairError(
                f"tag {f.tag}: field is {length} bytes, but the directory's "
                "length field is only 4 digits (max 9999) -- can't represent "
                "this field in ISO 2709 at all"
            )
        if running > 99999:
            raise RepairError(
                f"tag {f.tag}: starting offset {running} exceeds the directory's "
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
    # back correctly regardless of what the leader claims.
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
    header = (new_leader + directory).encode(encoding)
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
# QA: flag (but don't fix) suspicious content
# ---------------------------------------------------------------------------

_DOUBLED_PROXY_PREFIX = re.compile(r"(https?://[^?]+\?url=)\1")


VALID_SUBFIELD_CODE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789")


# MARC21 leader byte validity sets (per LC's leader documentation:
# https://www.loc.gov/marc/bibliographic/bdleader.html, and the equivalent
# authority/holdings leader pages -- the codes differ slightly by record
# format, so byte 06's set below is the *union* across bib/authority/
# holdings, since which format a given record is meant to be is exactly
# what this check can't assume). See `fix_invalid_leader_bytes` for the
# defaults applied to invalid values in each.
LEADER_05_RECORD_STATUS_VALID = set("acdnp")
LEADER_06_TYPE_OF_RECORD_VALID = set("acdefgijkmoprtzuvxy")
LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID = set("abcdims")
LEADER_08_TYPE_OF_CONTROL_VALID = set("a ")
LEADER_17_ENCODING_LEVEL_VALID = set("12345 78uz")

#: MARC21 Holdings format's OWN byte 17 (Encoding level) code set --
#: unlike the permissive union above, this is holdings-specific:
#: https://www.loc.gov/marc/holdings/hdleader.html documents only
#: 1/2/3/4/5/m/u/z for holdings -- blank ("full level") is a
#: *bibliographic* code, not a defined holdings one, and holdings adds
#: 'm' (Mixed level), which the union set above doesn't include at
#: all. See `repair_holdings_records`, which passes this (and 'u' as
#: the correction default, not the union's blank) to
#: `fix_invalid_leader_bytes` instead of the shared bib-oriented set.
LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS = set("12345muz")

#: A data field's indicators are almost universally either a digit
#: (0-9) or blank across the whole MARC21 Bibliographic format -- this
#: is a broad, format-wide rule rather than a per-tag table, so it
#: generalizes across any file without needing an exhaustive per-field
#: indicator-value reference. See `find_invalid_indicator_values`.
VALID_INDICATOR_CHARS = set("0123456789 ")


def fix_invalid_leader_bytes(
    parsed: ParsedRecord,
    type_of_record_default: str = "a",
    encoding_level_valid: set[str] = LEADER_17_ENCODING_LEVEL_VALID,
    encoding_level_default: str = " ",
) -> list[str]:
    """Default four leader bytes to a known-valid value when they hold
    something outside their valid MARC21 code set (see the
    LEADER_*_VALID sets above), mirroring a widely-used site cleanup
    script's leader-repair logic:

      * byte 05 (Record status) -> 'c' (Corrected or revised)
      * byte 06 (Type of record) -> `type_of_record_default` ('a',
        Language material, for a bib file; the holdings pipeline passes
        'u', Unknown, instead -- defaulting a corrupted byte 06 to a
        *bibliographic* code in a file already classified as holdings
        would silently reclassify the record)
      * byte 08 (Type of control) -> ' ' (not specified)
      * byte 17 (Encoding level) -> `encoding_level_default` against
        `encoding_level_valid` (' ', full level, against the permissive
        bib/authority/holdings union `LEADER_17_ENCODING_LEVEL_VALID`,
        by default; the holdings pipeline passes 'u', Unknown, against
        `LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS` instead -- holdings'
        own spec doesn't define a blank code at all, and does define
        'm', which the shared union set doesn't include)

    Each of these bytes carries real classification information (e.g.
    byte 05 distinguishes a deleted record from a merely corrected one),
    so overwriting an invalid value necessarily picks an arbitrary
    replacement rather than recovering the original intent -- that's a
    real, deliberate trade-off (not a structural fix like the entry-map
    correction, which has exactly one right answer), so every change here
    is logged individually with the byte's original and new value.

    Note on the source script: its own fix for byte 17 has a bug -- it
    reads byte 16 (1-indexed substr(leader,17,1)) instead of byte 17, and
    when that check fails it rewrites byte 08 (via
    substr($0,1,8)" "substr($0,10)) instead of byte 17, so in the
    original script byte 17 is never actually validated or corrected at
    all. This implementation validates and defaults the byte the checks
    are actually named for (17, encoding level) rather than reproducing
    that mismatch.
    """
    details = []
    leader = list(parsed.leader)

    def apply(pos: int, valid: set[str], default: str, label: str):
        if pos < len(leader) and leader[pos] not in valid:
            details.append(
                f"leader byte {pos:02d} ({label}) was {leader[pos]!r}, defaulted to {default!r}"
            )
            leader[pos] = default

    apply(5, LEADER_05_RECORD_STATUS_VALID, "c", "record status")
    apply(6, LEADER_06_TYPE_OF_RECORD_VALID, type_of_record_default, "type of record")
    apply(8, LEADER_08_TYPE_OF_CONTROL_VALID, " ", "type of control")
    apply(17, encoding_level_valid, encoding_level_default, "encoding level")
    if details:
        parsed.leader = "".join(leader)
    return details


def find_suspicious_fields(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Heuristic content-quality warnings -- these are NOT auto-fixed (except
    where a dedicated opt-in flag exists, noted below); they're reported so
    a human can decide. Returns (category, detail) pairs -- category is a
    stable machine-readable label used to group same-type log entries
    together (see `main`); detail is the human-readable specific finding.
    Checks:

      * A literally-repeated proxy prefix in $u (e.g.
        "...ezproxy...?url=...ezproxy...?url=..."). A normal single-hop
        proxy URL has two "://" (one for the proxy, one for the wrapped
        target) and is NOT flagged; only an actually duplicated prefix is.
        category: "doubled_proxy_url"
      * A tag that isn't 3 numeric digits -- MARC21 tags are always
        numeric. Auto-fixed by default (renamed to an unused 9XX tag --
        see `fix_invalid_tags`), so this only fires when that's disabled
        (--no-fix-invalid-tags) or every 9XX slot is already taken.
        category: "non_numeric_tag"
      * A subfield code that isn't a lowercase letter or digit. Not
        auto-fixed here -- see `strip_invalid_subfield_codes`, which the
        CLI runs by default (--no-strip-invalid-subfield-codes to skip
        it); this only fires when that's disabled.
        category: "invalid_subfield_code"
      * A record with no 008 control field, mandatory in every MARC21
        bibliographic/authority/holdings record. Not auto-fixed: the
        correct default content is material-type-specific and genuinely a
        guess, so this is reported for a human to supply via --ensure-field
        rather than silently invented.
        category: "missing_008"
    """
    warnings: list[tuple[str, str]] = []
    if not any(f.tag == "008" for f in parsed.fields):
        warnings.append(("missing_008", "record has no 008 control field (mandatory in MARC21)"))
    for f in parsed.fields:
        if not f.tag.isdigit():
            warnings.append(("non_numeric_tag", f"tag {f.tag!r} is not 3 numeric digits"))
        if f.is_control():
            continue
        for code, data in f.subfields:
            if code == "u" and _DOUBLED_PROXY_PREFIX.search(data):
                warnings.append((
                    "doubled_proxy_url",
                    f"tag {f.tag} $u has a literally duplicated proxy prefix: {data!r}",
                ))
            if code not in VALID_SUBFIELD_CODE_CHARS:
                warnings.append((
                    "invalid_subfield_code",
                    f"tag {f.tag} has invalid subfield code {code!r}",
                ))
    return warnings


def find_invalid_indicator_values(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag a data field indicator character that isn't a digit or
    blank (see `VALID_INDICATOR_CHARS`). Skips the 900-999 locally-
    defined range entirely -- MARC21 doesn't prescribe indicator
    meanings there at all, so a local field's own convention (e.g.
    this tool's own `remap_999_to_945` intentionally sets indicators
    "ff" on 945, which isn't a digit/blank but also isn't wrong) can't
    be judged against the standard fields' rule. Detect-only -- there's
    no safe way to guess the intended value, and unlike a missing
    indicator (see --fix-bad-indicators), a *present but wrong*
    character isn't even structurally broken, just semantically
    suspect. category: "invalid_indicator_value"."""
    findings = []
    for f in parsed.fields:
        if f.is_control() or (f.tag.isdigit() and 900 <= int(f.tag) <= 999):
            continue
        for pos, ch in enumerate(f.indicators, start=1):
            if ch not in VALID_INDICATOR_CHARS:
                findings.append((
                    "invalid_indicator_value",
                    f"tag {f.tag} indicator {pos} is {ch!r}, not a digit or blank",
                ))
    return findings


def find_invalid_bibliographic_level(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag leader byte 07 (bibliographic level) outside its valid
    MARC21 code set (see `LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID`).
    Detect-only, unlike the byte 05/06/08/17 checks in
    `fix_invalid_leader_bytes` -- logged separately as informational
    rather than defaulted. category: "invalid_bibliographic_level"."""
    leader = parsed.leader
    if len(leader) >= 8 and leader[7] not in LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID:
        return [(
            "invalid_bibliographic_level",
            f"leader byte 07 (bibliographic level) is {leader[7]!r}, not one "
            f"of {sorted(LEADER_07_BIBLIOGRAPHIC_LEVEL_VALID)!r}",
        )]
    return []


def find_dangling_880_links(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag an 880 (Alternate Graphic Representation) field whose $6
    linking subfield references a tag that doesn't exist anywhere else
    in the record (e.g. $6 "245-01" but there's no 245). A dangling
    link breaks the record's own romanized/original-script pairing --
    detect-only, since there's no way to know what the correct link
    should have been. category: "dangling_880_link". Off by default in
    the CLI -- see --check-dangling-880-links."""
    findings = []
    tags_present = {f.tag for f in parsed.fields}
    for f in parsed.fields:
        if f.tag != "880" or f.is_control():
            continue
        for code, data in f.subfields:
            if code != "6":
                continue
            ref_tag = data[:3]
            if ref_tag and ref_tag not in tags_present:
                findings.append((
                    "dangling_880_link",
                    f"880 $6 references ={ref_tag}, but no field with that "
                    f"tag exists in this record: {data!r}",
                ))
    return findings


def _isbn10_checksum_valid(digits: str) -> bool:
    if len(digits) != 10:
        return False
    total = 0
    for i, ch in enumerate(digits):
        if ch == "X" and i == 9:
            value = 10
        elif ch.isdigit():
            value = int(ch)
        else:
            return False
        total += (10 - i) * value
    return total % 11 == 0


def _isbn13_checksum_valid(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(digits))
    return total % 10 == 0


def _issn_checksum_valid(digits: str) -> bool:
    if len(digits) != 8:
        return False
    if not digits[:7].isdigit():
        return False
    total = sum((8 - i) * int(ch) for i, ch in enumerate(digits[:7]))
    check = digits[7]
    check_value = 10 if check == "X" else (int(check) if check.isdigit() else None)
    if check_value is None:
        return False
    return (total + check_value) % 11 == 0


def find_invalid_isbn_issn_checksums(parsed: ParsedRecord) -> list[tuple[str, str]]:
    """Flag a 020 $a (ISBN) or 022 $a (ISSN) whose check digit doesn't
    match the standard checksum algorithm for its length (ISBN-10:
    mod-11 weighted 10..1, 'X' = 10; ISBN-13: mod-10 weighted 1/3
    alternating; ISSN: mod-11 weighted 8..2 over 8 digits). A single
    mis-keyed or corrupted digit is exactly what this catches. Ignores
    values whose cleaned length doesn't match a known ISBN/ISSN form at
    all (e.g. a qualifier-only $a) rather than guessing. Detect-only --
    there's no safe way to know which digit was wrong. category:
    "invalid_isbn_issn_checksum". Off by default in the CLI -- see
    --check-isbn-issn-checksum."""
    findings = []
    for f in parsed.fields:
        if f.is_control():
            continue
        if f.tag not in ("020", "022"):
            continue
        for code, data in f.subfields:
            if code != "a":
                continue
            cleaned = re.sub(r"[^0-9Xx]", "", data).upper()
            if f.tag == "020":
                if len(cleaned) == 10 and not _isbn10_checksum_valid(cleaned):
                    findings.append((
                        "invalid_isbn_issn_checksum",
                        f"020 $a {data!r}: ISBN-10 checksum invalid",
                    ))
                elif len(cleaned) == 13 and not _isbn13_checksum_valid(cleaned):
                    findings.append((
                        "invalid_isbn_issn_checksum",
                        f"020 $a {data!r}: ISBN-13 checksum invalid",
                    ))
            elif len(cleaned) == 8 and not _issn_checksum_valid(cleaned):
                findings.append((
                    "invalid_isbn_issn_checksum",
                    f"022 $a {data!r}: ISSN checksum invalid",
                ))
    return findings


def fix_008_length(parsed: ParsedRecord, expected_len: int = 40) -> list[str]:
    """008 must always be exactly `expected_len` characters -- 40 for a
    bibliographic/authority record (the default), or 32 for a holdings
    record (see `HOLDINGS_008_LENGTH`; MARC21's Holdings format defines
    a shorter, differently-laid-out 008 -- https://www.loc.gov/marc/
    holdings/hd008.html -- confirmed against real data: every 008 in a
    real Bucknell holdings export is exactly 32 bytes). Pads a too-short
    one with trailing spaces (the generic "not specified" filler used
    throughout 008's own byte positions) or truncates a too-long one.
    This makes the record loadable (a strict importer like FOLIO can
    reject a wrong-length 008 outright) but is still a real content
    change worth a second look -- logged under FIXED/REQUIRES
    ATTENTION (category "fixed_008_length"), with the original content
    included in full, rather than as an ordinary fixed entry.
    """
    details = []
    for f in parsed.fields:
        if f.tag == "008" and f.content is not None and len(f.content) != expected_len:
            original = f.content
            f.content = original.ljust(expected_len)[:expected_len]
            action = "padded" if len(original) < expected_len else "truncated"
            details.append(
                f"008 was {len(original)} bytes (must be exactly {expected_len}); "
                f"{action} to {expected_len}; original content: {original!r}"
            )
    return details


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


def find_suspect_marc8_escapes(parsed: ParsedRecord) -> list[tuple[str, str]]:
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
    includes a "suggested fix" -- a hypothesis, not a correction:
    "...'s" at a word boundary reads as a miskeyed apostrophe (e.g.
    "who's"); anything else is framed as a likely lost accented letter
    in the surrounding word (e.g. "Schrodinger", "Leonidas"), with a
    prompt to verify against another edition or an authority record
    rather than an invented replacement.
    """
    findings: list[tuple[str, str]] = []
    if parsed.leader[9:10] == UNICODE_ENCODING_BYTE:
        return findings  # already UTF-8 -- no raw MARC-8 escapes to find
    for f in parsed.fields:
        texts = [f.content] if f.is_control() else [d for _, d in f.subfields]
        for text in texts:
            if not text or "\x1b" not in text:
                continue
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
                before = text[match.start() - 1:match.start()]
                after = text[close + 3:close + 4]
                if before.isalpha() and before.isascii() and after.isalpha() and after.isascii():
                    word_before_match = re.search(r"[A-Za-z]+$", text[:match.start()])
                    word_after_match = re.match(r"[A-Za-z]+", text[close + 3:])
                    word_before = word_before_match.group() if word_before_match else before
                    word_after = word_after_match.group() if word_after_match else after
                    next_char = text[close + 3 + len(word_after):close + 4 + len(word_after)]
                    # "...s" ending a word, itself at a word boundary
                    # (not followed by another letter) reads as a
                    # contraction/possessive far more often than an
                    # accented letter would -- e.g. "who's", "it's".
                    # Anything else is more likely an accented vowel
                    # lost from a proper noun (Schrodinger, Leonidas).
                    if word_after == "s" and not next_char.isalpha():
                        suggestion = (
                            f"suggested fix: likely a miskeyed apostrophe -- "
                            f"probably \"{word_before}'s\""
                        )
                    else:
                        suggestion = (
                            "suggested fix: likely a miskeyed accented letter "
                            "(e.g. ö, é, ñ, ü) in "
                            f"\"{word_before}[?]{word_after}\" -- compare "
                            "against another edition or an authority record "
                            "to confirm the correct spelling"
                        )
                    findings.append((
                        "suspect_marc8_escape",
                        f"tag {f.tag}: single {charset_name} character embedded "
                        f"mid-word ({before!r}<escape>{after!r}) -- likely a "
                        "miskeyed diacritic in the source record, not real "
                        f"{charset_name} content; {suggestion}; "
                        f"raw MARC-8: {text!r}",
                    ))
    return findings


def transcode_marc8_to_utf8(parsed: ParsedRecord) -> bool:
    """Convert every field's text from MARC-8 to Unicode in place and flip
    the leader's encoding byte to "a". Returns False (no-op) if the record
    already declares Unicode encoding.

    Raises RuntimeError if pymarc isn't installed -- run `pip install -r
    requirements.txt` first.
    """
    if parsed.leader[9:10] == UNICODE_ENCODING_BYTE:
        return False
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
        # always latin-1-decoded 1:1 from the original bytes (see
        # `_read_text_with_encoding`), so re-encoding with latin-1
        # here recovers those exact original bytes losslessly.
        return marc8_to_unicode(text.encode("latin-1"), hide_utf8_warnings=True)

    # Compute every field's converted value BEFORE mutating `parsed` at
    # all. Without this, a field partway through this record that fails
    # to convert (e.g. genuinely truncated/malformed multi-byte MARC-8
    # data -- a real, if rare, possibility now that this runs by
    # default on every record) would leave the record in an
    # inconsistent state: some fields already converted to Unicode,
    # later ones still raw MARC-8, but the leader never flipped because
    # that only happens at the very end. Computing everything into a
    # plain list first means a failure here leaves `parsed` completely
    # untouched -- the caller can log it and fall back to passing the
    # record through with its original MARC-8 declaration intact.
    new_control_content = {}
    new_subfields = {}
    for idx, f in enumerate(parsed.fields):
        if f.is_control():
            if f.content:
                new_control_content[idx] = convert(f.content)
        else:
            new_subfields[idx] = [(code, convert(data)) for code, data in f.subfields]

    for idx, f in enumerate(parsed.fields):
        if idx in new_control_content:
            f.content = new_control_content[idx]
        if idx in new_subfields:
            f.subfields = new_subfields[idx]

    parsed.leader = parsed.leader[:9] + UNICODE_ENCODING_BYTE + parsed.leader[10:]
    return True


def _is_sierra_number(value: str) -> bool:
    """Sierra bib numbers look like "b1234567" or ".b1234567" (an optional
    leading period, then a leading "b")."""
    return value.lower().lstrip(".").startswith("b")


def record_identifier(parsed: ParsedRecord) -> str:
    """The record's own identifier, for use in log lines: subfield $a of
    field 907 if it looks like a Sierra bib number (".b..." or "b..."),
    otherwise field 001's content. Falls back to whichever of the two is
    actually present if the preferred one is missing; empty string if
    neither is present at all."""
    field_907 = next((f for f in parsed.fields if f.tag == "907"), None)
    value_907 = (
        next((d for c, d in field_907.subfields if c == "a"), None)
        if field_907 is not None
        else None
    )
    if value_907 and _is_sierra_number(value_907):
        return value_907

    field_001 = next((f for f in parsed.fields if f.tag == "001"), None)
    if field_001 is not None and field_001.content:
        return field_001.content

    return value_907 or ""


def find_duplicate_identifiers(
    id_records: list[tuple[int, str, str]], ts: str
) -> list[LogEntry]:
    """Given (record_idx, identifier, declared_length) for every record that
    had a usable identifier (see `record_identifier`; `declared_length` is
    each record's own leader bytes 00-04), find identifiers used on more
    than one record in this run and return a LogEntry -- category
    "duplicate_identifier", not fixed, since there's no way to know which
    occurrence (if any) is the "real" one, only that the same identifier
    was reused -- for every record involved in such a collision. This is
    inherently a whole-file check (a record can't tell it's a duplicate in
    isolation), so it runs once after the full pass rather than per-record.
    """
    by_id: dict[str, list[tuple[int, str]]] = {}
    for idx, ident, length in id_records:
        if ident:
            by_id.setdefault(ident, []).append((idx, length))

    entries = []
    for ident, occurrences in by_id.items():
        if len(occurrences) < 2:
            continue
        all_idx = [idx for idx, _ in occurrences]
        for idx, length in occurrences:
            others = [i for i in all_idx if i != idx]
            entries.append(LogEntry(
                "duplicate_identifier", False, ts, idx, ident,
                f"identifier used on {len(occurrences)} records total "
                f"(also records {others}); this record's length (LDR 00-04) = {length}",
            ))
    return entries


# ---------------------------------------------------------------------------
# Content patching: add a required field where it's missing
# ---------------------------------------------------------------------------

def ensure_field(
    parsed: ParsedRecord,
    tag: str,
    indicators: str | None,
    subfields_or_content: list[tuple[str, str]] | str,
) -> bool:
    """Insert a new field into `parsed` if `tag` isn't already present,
    keeping fields in the conventional ascending-tag order. Returns True if a
    field was added, False if `tag` was already there (left untouched).

    For a control field (tag < "010", e.g. 008 -- no indicators/subfields),
    pass `indicators=None` and `subfields_or_content` as the raw content
    string. For a data field, pass a 2-char `indicators` string and
    `subfields_or_content` as a list of (code, data) tuples.
    """
    if any(f.tag == tag for f in parsed.fields):
        return False
    insert_at = len(parsed.fields)
    for i, f in enumerate(parsed.fields):
        # a non-numeric tag (not yet fixed, e.g. --no-fix-invalid-tags was
        # passed) has no defined position in tag order -- treat it as
        # sorting after every real tag rather than crashing on int()
        existing = int(f.tag) if f.tag.isdigit() else 1000
        if existing > int(tag):
            insert_at = i
            break
    if indicators is None:
        new_field = Field_(tag, None, None, content=subfields_or_content)
    else:
        new_field = Field_(tag, indicators, list(subfields_or_content))
    parsed.fields.insert(insert_at, new_field)
    return True


#: Placeholder 008 content for records missing one entirely, matching a
#: widely-used site cleanup script's own blanket default byte-for-byte.
#: This is a real trade-off, not a correction: 008 is material-type- and
#: date-specific, so this placeholder (single known date 1978-06-15,
#: unknown publication date/place, language eng, "d"/serial-ish trailing
#: byte) will be wrong for most records it's applied to -- it exists so a
#: record has *a* syntactically valid 008 (many downstream systems choke
#: on a record with none at all) rather than none whatsoever, not because
#: it's the correct value. See `add_default_008`.
DEFAULT_008_CONTENT = "780615s19uu    xx a                    d"


def add_default_008(parsed: ParsedRecord) -> list[str]:
    """Insert `DEFAULT_008_CONTENT` for a record with no 008 field at all.
    No-op if 008 is already present (e.g. supplied correctly via
    --ensure-field, which runs before this). Logged (category
    "added_default_008") since the inserted value is a placeholder, not a
    real one -- --no-add-default-008 leaves such records with no 008,
    matching this tool's default posture on ambiguous data everywhere
    else, if you'd rather supply correct values yourself via
    --ensure-field."""
    if ensure_field(parsed, "008", None, DEFAULT_008_CONTENT):
        return [f"added default 008 (was missing): {DEFAULT_008_CONTENT!r}"]
    return []


#: MARC21's Holdings format 008 is a different, shorter fixed field
#: than the bibliographic/authority 008 (32 bytes, not 40 --
#: https://www.loc.gov/marc/holdings/hd008.html; confirmed against real
#: data, where every 008 in a real Bucknell holdings export is exactly
#: 32 bytes). See `fix_008_length`'s `expected_len` for the structural
#: (pad/truncate) side of this; this constant is only the content used
#: when a holdings record has no 008 at all.
HOLDINGS_008_LENGTH = 32

#: Placeholder 008 content for a holdings record missing one entirely.
#: Unlike DEFAULT_008_CONTENT (bibliographic 008, matching a widely-used
#: site cleanup script's own blanket default byte-for-byte), no
#: equivalent holdings-specific default is available -- the 008's
#: content (receipt/acquisition status, expected frequency, completeness,
#: retention policy, etc.) is genuinely institution- and
#: collection-specific, so inventing plausible-looking values for
#: positions this tool can't actually determine would be exactly the
#: kind of silent guess this tool avoids everywhere else. This is a
#: fully blank placeholder instead -- syntactically valid (right length,
#: which is what a strict importer like FOLIO actually enforces) but
#: informationally empty -- logged the same way as the bibliographic
#: placeholder (see `add_default_holdings_008`) so every occurrence is
#: easy to find and review.
DEFAULT_HOLDINGS_008_CONTENT = " " * HOLDINGS_008_LENGTH


#: Placeholder content for a missing 852 (Location) $c (shelving
#: location) -- see `add_missing_852c`. Like the 008/245 placeholders
#: above, this deliberately flags itself as inserted rather than
#: guessing a real shelving location, which this tool has no way to
#: know.
DEFAULT_852_C_CONTENT = "Migration"


def add_missing_852c(parsed: ParsedRecord) -> list[str]:
    """Append subfield $c (shelving location) to every 852 (Location)
    field that's missing it, with placeholder content
    `DEFAULT_852_C_CONTENT`. Off by default (see --fix-missing-852c);
    logged (category "added_missing_852c") every time it runs, since
    it's adding content -- not just correcting structure -- and a
    placeholder rather than a value recovered from the record's own
    data. No-op for an 852 that already has a $c, however placed.
    """
    details = []
    for f in parsed.fields:
        if f.tag != "852" or f.is_control():
            continue
        if any(code == "c" for code, _ in f.subfields):
            continue
        f.subfields = list(f.subfields) + [("c", DEFAULT_852_C_CONTENT)]
        details.append(f"added missing 852 $c: {DEFAULT_852_C_CONTENT!r}")
    return details


def add_default_holdings_008(parsed: ParsedRecord) -> list[str]:
    """Insert `DEFAULT_HOLDINGS_008_CONTENT` for a holdings record with
    no 008 field at all. No-op if 008 is already present. Logged
    (category "added_default_holdings_008") since the inserted value is
    a blank placeholder, not a real one -- see
    `DEFAULT_HOLDINGS_008_CONTENT`.
    """
    if ensure_field(parsed, "008", None, DEFAULT_HOLDINGS_008_CONTENT):
        return [
            "added blank placeholder holdings 008 (was missing): "
            f"{DEFAULT_HOLDINGS_008_CONTENT!r}"
        ]
    return []


#: Placeholder 245 (title statement) for records missing one entirely --
#: a real ILS import will often outright reject a record with no title at
#: all, so a placeholder that flags itself as such ("No title") is safer
#: than leaving the record unimportable, even though a title is never
#: actually invented. See `add_default_245`.
DEFAULT_245_INDICATORS = "00"
DEFAULT_245_SUBFIELDS = [("a", "No title")]


def add_default_245(parsed: ParsedRecord) -> list[str]:
    """Ensure every record has a 245 with a non-empty $a, using the
    placeholder "No title" where one is missing.

    245 is mandatory in every MARC21 record, unlike the heading fields
    `strip_missing_required_a` strips when they lack $a (245 itself is
    deliberately excluded from required_a_tags.txt for this reason) --
    so a 245 that already exists but lacks $a (e.g. one with only
    $h[electronic resource]) is patched in place: $a is inserted at the
    front of its existing subfields, keeping the rest, rather than
    stripping the field and rebuilding it from scratch. Only a record
    with no 245 field at all gets a brand new one. No-op if 245 already
    has a non-empty $a (e.g. supplied correctly via --ensure-field, which
    runs before this). Both cases log the same category
    ("added_default_245") since the net effect is the same: a
    placeholder title where a real one was missing.
    """
    field245 = next((f for f in parsed.fields if f.tag == "245"), None)
    if field245 is None:
        ensure_field(parsed, "245", DEFAULT_245_INDICATORS, DEFAULT_245_SUBFIELDS)
        return ["added placeholder 245 (was missing): 00 $aNo title"]
    if any(code == "a" and data for code, data in field245.subfields):
        return []
    # drop any $a that's present but empty, so the placeholder doesn't
    # end up alongside a second, empty $a
    other_subfields = [(c, d) for c, d in field245.subfields if not (c == "a" and not d)]
    field245.subfields = list(DEFAULT_245_SUBFIELDS) + other_subfields
    return ["added placeholder $aNo title to existing 245 (had no $a)"]


def parse_ensure_field_spec(
    spec: str,
) -> tuple[str, str | None, list[tuple[str, str]] | str]:
    """Parse a CLI --ensure-field spec. Two forms:

      * Control field (tag < "010"): "TAG:CONTENT" -- e.g.
        "008:780615s19uu    xx a                    d". No indicators or
        subfields exist for these, so the rest of the spec after the tag
        is used verbatim as the field's content.
      * Data field: "TAG:INDICATORS:CODE=VALUE[|CODE=VALUE...]" -- e.g.
        "245:00:a=No title" or "500:  :a=Note one|a=Note two".

    Returns (tag, indicators, subfields) for a data field, or
    (tag, None, content) for a control field.
    """
    try:
        tag, rest = spec.split(":", 1)
    except ValueError:
        raise ValueError(f"--ensure-field spec must start with TAG:..., got {spec!r}")
    if not tag.isdigit() or len(tag) != 3:
        raise ValueError(f"tag must be 3 digits, got {tag!r}")
    if int(tag) < CONTROL_TAG_LIMIT:
        return tag, None, rest
    try:
        indicators, subfields_str = rest.split(":", 1)
    except ValueError:
        raise ValueError(
            f"--ensure-field spec must be TAG:INDICATORS:CODE=VALUE, got {spec!r}"
        )
    if len(indicators) != 2:
        raise ValueError(f"indicators must be exactly 2 characters, got {indicators!r}")
    subfields = []
    for part in subfields_str.split("|"):
        code, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"subfield spec must be CODE=VALUE, got {part!r}")
        subfields.append((code, value))
    return tag, indicators, subfields


DEFAULT_REQUIRED_A_TAGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "required_a_tags.txt"
)

DEFAULT_NON_REPEATABLE_TAGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "non_repeatable_tags.txt"
)


def _choose_kept_001(fields_for_tag: list[Field_], is_sirsi: bool) -> Field_:
    """A duplicated 001 in a Sierra/Symphony-sourced record (003
    content "SIRSI", case-insensitive) keeps whichever occurrence
    starts with "u" -- that's this system's own real bib-id convention
    (e.g. "u508261"), so a duplicate 001 there is far more likely to be
    a stray *other* identifier (an OCLC number, a barcode) that ended
    up in 001 by mistake than the record's actual id. Falls back to
    the first occurrence if not SIRSI, or if none of them start with
    "u"."""
    if is_sirsi:
        preferred = next(
            (f for f in fields_for_tag
             if f.content and f.content.strip().lower().startswith("u")),
            None,
        )
        if preferred is not None:
            return preferred
    return fields_for_tag[0]


def _choose_kept_005(fields_for_tag: list[Field_], is_sirsi: bool) -> Field_:
    """005 (date/time of latest transaction) is a fixed-width,
    zero-padded YYYYMMDDHHMMSS.F string with a 4-digit year, so plain
    string comparison IS chronological comparison, no parsing needed.
    Keeps the most recent; ties keep the first occurrence."""
    best = fields_for_tag[0]
    for f in fields_for_tag[1:]:
        if (f.content or "") > (best.content or ""):
            best = f
    return best


def _choose_kept_008(fields_for_tag: list[Field_], is_sirsi: bool) -> Field_:
    """008 bytes 0-5 (date entered on file, YYMMDD) compared as a
    plain string -- keeps the most recent; ties keep the first
    occurrence."""
    best = fields_for_tag[0]
    best_key = (best.content or "")[:6]
    for f in fields_for_tag[1:]:
        key = (f.content or "")[:6]
        if key > best_key:
            best, best_key = f, key
    return best


#: Per-tag tiebreakers for which occurrence of a duplicated
#: non-repeatable field to keep -- anything not listed here just keeps
#: the first occurrence (see `strip_duplicate_non_repeatable_fields`).
_DUPLICATE_FIELD_RESOLVERS = {
    "001": _choose_kept_001,
    "005": _choose_kept_005,
    "008": _choose_kept_008,
}


def strip_duplicate_non_repeatable_fields(
    parsed: ParsedRecord, non_repeatable_tags: set[str]
) -> list[str]:
    """A record must be loadable even when it's genuinely broken --
    MARC21 designates some fields Not Repeatable (see
    non_repeatable_tags.txt), and a second occurrence of one (e.g. two
    245s) is exactly the kind of thing a strict importer like FOLIO can
    reject outright or handle unpredictably. Keeps one occurrence of
    each tag in `non_repeatable_tags`, removes every other one.

    Which occurrence is kept is normally just the first, with tag-
    specific exceptions in `_DUPLICATE_FIELD_RESOLVERS`: 001 prefers a
    "u"-prefixed value on Sierra/Symphony records, 005 and 008 keep
    the most recent by date.

    This discards real data, so unlike this tool's routine removals
    (an empty field, an unusable subfield code) it's deliberately NOT
    filed as an ordinary "fixed" entry -- see FIXED/REQUIRES ATTENTION
    in `main`, which logs the exact removed field content so a human
    can decide whether it needed to go somewhere else instead (real
    example found in production data: a second "245" containing only
    $a "2nd ed." -- almost certainly a mistagged 250, not a genuine
    second title).
    """
    occurrences: dict[str, list[Field_]] = {}
    for f in parsed.fields:
        if f.tag in non_repeatable_tags:
            occurrences.setdefault(f.tag, []).append(f)

    is_sirsi = any(
        f.tag == "003" and f.content and f.content.strip().lower() == "sirsi"
        for f in parsed.fields
    )
    keep: dict[str, Field_] = {}
    for tag, fields_for_tag in occurrences.items():
        if len(fields_for_tag) <= 1:
            continue
        resolver = _DUPLICATE_FIELD_RESOLVERS.get(tag)
        keep[tag] = resolver(fields_for_tag, is_sirsi) if resolver else fields_for_tag[0]

    details = []
    kept_fields = []
    for f in parsed.fields:
        duplicates = occurrences.get(f.tag)
        if duplicates and len(duplicates) > 1:
            if f is keep[f.tag]:
                kept_fields.append(f)
            else:
                if f.is_control():
                    body = f.content or ""
                else:
                    body = f.indicators + "".join(f"${c}{d}" for c, d in f.subfields)
                details.append(f"removed duplicate ={f.tag}  {body}\t(non-repeatable field)")
            continue
        kept_fields.append(f)
    parsed.fields = kept_fields
    return details


def load_tag_list(path: str) -> set[str]:
    """Load a tag list from an external file (one tag per line; blank
    lines and lines starting with # are ignored) -- used for both
    `strip_missing_required_a` (see required_a_tags.txt) and
    `strip_duplicate_non_repeatable_fields` (see
    non_repeatable_tags.txt). Kept outside the script on purpose --
    both lists involve some judgment call, not something to hardcode."""
    tags = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                tags.add(line)
    return tags


_PUNCTUATION_ONLY_A_RE = re.compile(r"^[^\w]*$", re.UNICODE)


def _is_punctuation_only(data: str) -> bool:
    """True if `data` has no letters or digits (unicode-aware), i.e. it's
    made up entirely of punctuation/symbols/whitespace -- a $a like "." or
    "--" that carries no actual content."""
    return bool(data) and bool(_PUNCTUATION_ONLY_A_RE.match(data))


def strip_missing_required_a(parsed: ParsedRecord, required_a_tags: set[str]) -> list[str]:
    """Remove data fields whose tag is in `required_a_tags` (see
    required_a_tags.txt) but that lack a non-empty, non-punctuation-only $a
    subfield, which is required there. A $a whose content is nothing but
    punctuation (e.g. "." or "--") carries no real data and is treated the
    same as a missing $a.

    A field that's entirely empty (no non-empty subfield at all, e.g. a bare
    "$a" with nothing after it and nothing else in the field) is removed
    silently -- there's nothing to lose. A field that has some other
    non-empty subfield data (including a punctuation-only $a) is also
    removed (per the same missing-required-$a rule) but is NOT silent: a
    line describing exactly what was discarded is returned (category
    "field_removed_because_missing_a" -- see `main`) so the caller can log it before the
    content is gone for good.
    """
    details = []
    kept = []
    for f in parsed.fields:
        if f.is_control() or f.tag not in required_a_tags:
            kept.append(f)
            continue
        if any(
            code == "a" and data and not _is_punctuation_only(data)
            for code, data in f.subfields
        ):
            kept.append(f)
            continue
        if any(data for code, data in f.subfields):
            body = "".join(f"${code}{data}" for code, data in f.subfields)
            reason = (
                "$a is punctuation only"
                if any(code == "a" and data for code, data in f.subfields)
                else "missing required $a"
            )
            details.append(
                f"removed ={f.tag}  {f.indicators}{body}\t({reason}; "
                "content discarded)"
            )
        # else: field was entirely empty -- drop it without logging
    parsed.fields = kept
    return details


def strip_invalid_subfield_codes(parsed: ParsedRecord) -> list[str]:
    """Remove subfields whose code isn't a lowercase letter or digit (see
    `VALID_SUBFIELD_CODE_CHARS`) -- a code that isn't one of those is
    unusable data (there's no way to know what it was supposed to be), so
    unlike `strip_missing_required_a` there's no "keep the field" branch:
    just the bad subfield is dropped. Each removal is returned as a detail
    string (category "removed_invalid_subfield" -- see `main`) since it
    discards whatever data it held. A field left with zero subfields
    afterward is removed entirely (silently, matching
    `strip_missing_required_a`'s "nothing left to keep" rule)."""
    details = []
    kept_fields = []
    for f in parsed.fields:
        if f.is_control():
            kept_fields.append(f)
            continue
        good = [(c, d) for c, d in f.subfields if c in VALID_SUBFIELD_CODE_CHARS]
        bad = [(c, d) for c, d in f.subfields if c not in VALID_SUBFIELD_CODE_CHARS]
        if bad:
            removed = "".join(f"${c}{d}" for c, d in bad)
            details.append(
                f"removed invalid subfield(s) {removed!r} from ={f.tag}"
                "\t(invalid subfield code; content discarded)"
            )
        if good:
            f.subfields = good
            kept_fields.append(f)
        # else: nothing valid left in this field -- drop it entirely
    parsed.fields = kept_fields
    return details


def strip_empty_fields(parsed: ParsedRecord) -> None:
    """Remove data fields where every subfield's data is empty (e.g. a
    bare "$a" with nothing after it and nothing else in the field) --
    regardless of tag, unlike `strip_missing_required_a` which only checks
    tags known to require $a specifically. This is unconditional and
    silent (no return value): a field with zero non-empty subfields holds
    no information under any tag, so removing it never discards anything,
    the same "nothing to lose" case `strip_missing_required_a` and
    `strip_invalid_subfield_codes` already drop silently when they empty a
    field out via their own tag/code-scoped rules."""
    parsed.fields = [
        f for f in parsed.fields
        if f.is_control() or any(data for code, data in f.subfields)
    ]


def normalize_subfield_9_to_0(parsed: ParsedRecord) -> list[str]:
    """Rewrite every $9 subfield code to $0, unconditionally, in every data
    field. $0 is MARC21's standard subfield for an authority record control
    number/URI; $9 is formally reserved for locally-defined data, but a
    long-standing convention in widely-used cleanup tooling (this mirrors
    a specific site's battle-tested "marcfix" script) treats $9 as that
    same link stored under a legacy/local code and unconditionally
    promotes it to the standard one. Because $9 is *reserved* rather than
    standardized, a $9 could in principle mean something unrelated in some
    other system's data -- run by default per that script's convention,
    and --no-normalize-subfield-9 is available to skip it. Returns one
    detail string per field changed (category "normalized_subfield_9_to_0"
    if the caller logs them), but by default the CLI does NOT log these --
    see --log-normalized-subfield-9-to-0 -- since a file that uses $9 at
    all often has it on nearly every record.
    """
    details = []
    for f in parsed.fields:
        if f.is_control():
            continue
        if not any(code == "9" for code, _ in f.subfields):
            continue
        f.subfields = [("0" if code == "9" else code, data) for code, data in f.subfields]
        details.append(f"normalized $9 -> $0 in ={f.tag}")
    return details


def remap_999_to_945(parsed: ParsedRecord) -> list[str]:
    """Retag every 999 data field to 945, forcing its indicators to "ff".
    999 is not part of MARC21 at all -- it's Sierra's own internal
    item-linking field, meaningless (and often rejected outright) outside
    that system -- while 945 is a commonly-used locally-defined field for
    the same purpose that other systems (e.g. OCLC) will actually accept.
    Subfield content and order are left untouched -- only the tag and
    indicators change. Returns one detail string per field remapped
    (category "remapped_999_to_945" if the caller logs them), but by
    default the CLI does NOT log these -- see --log-999-to-945 -- since a
    single record can carry many 999s (one per item copy) and logging
    each would dominate the log.
    """
    details = []
    for f in parsed.fields:
        if f.is_control() or f.tag != "999":
            continue
        f.tag = "945"
        f.indicators = "ff"
        details.append("remapped =999 to =945, indicators set to 'ff'")
    return details


#: MARC21 tags are always 3 numeric digits; 900-999 is the block reserved
#: for locally-defined fields, so it's the natural place to park a tag
#: that's genuinely garbage (e.g. "ABC" from directory corruption) --
#: better than leaving a record with an unparseable tag, but still
#: clearly marked as "not a real MARC21 tag" by construction. See
#: `pick_unused_9xx_tag`.
_9XX_CANDIDATES = [str(n) for n in range(900, 1000)]


def pick_unused_9xx_tag(used_tags: set[str]) -> str | None:
    """First tag in 900-999 not present in `used_tags`, or None if all 100
    are somehow already in use (a caller should fall back to leaving
    invalid tags unfixed in that case)."""
    for candidate in _9XX_CANDIDATES:
        if candidate not in used_tags:
            return candidate
    return None


def fix_invalid_tags(parsed: ParsedRecord, replacement_tag: str) -> list[str]:
    """Rename every field whose tag isn't 3 numeric digits (e.g. "ABC"
    from directory corruption -- MARC21 tags are always numeric) to
    `replacement_tag`. Indicators/subfields/content are left untouched --
    only the tag changes. Logged (category "invalid_tag") since the
    replacement tag is a placeholder, not recovered from the record's own
    data; see `pick_unused_9xx_tag` for how it's chosen, and `main`'s
    deferred-fix pass for how the file's used tags are gathered without
    a separate full-file scan.
    """
    details = []
    for f in parsed.fields:
        if not f.tag.isdigit():
            details.append(f"Invalid tag {f.tag!r} renamed to ={replacement_tag}")
            f.tag = replacement_tag
    return details


#: Typographic "smart" Unicode punctuation -> plain ASCII equivalent. These
#: characters are perfectly valid Unicode and not a structural defect on
#: their own (a record correctly declaring UTF-8 can contain them with no
#: problem), but some downstream MARC tooling flags them as suspect, and
#: some legacy/MARC-8-oriented systems mis-render them regardless of a
#: correct UTF-8 declaration.
SMART_CHAR_MAP: dict[str, str] = {
    "‘": "'", "’": "'", "‚": ",", "‛": "'",  # ' ' ‚ ‛
    "“": '"', "”": '"', "„": '"', "‟": '"',  # " " „ ‟
    "–": "-", "—": "--",  # – —
    "‐": "-", "‑": "-", "‒": "-",  # ‐ ‑ ‒
    "…": "...",  # …
    "′": "'", "″": '"',  # ′ ″
    " ": " ",  # non-breaking space
    "­": "",  # soft hyphen
}


#: `str.translate`-compatible table built once from SMART_CHAR_MAP. This
#: runs the substitution in C rather than the Python-level per-character
#: loop `_replace_smart_chars` used to do -- profiling a full run against
#: a real 91MB/48k-record file showed that loop alone (called on every
#: field's content, even fields with no smart characters at all) was
#: responsible for over 40% of total run time.
_SMART_CHAR_TABLE = str.maketrans(SMART_CHAR_MAP)


def _replace_smart_chars(text: str) -> tuple[str, int]:
    # Every SMART_CHAR_MAP key is non-ASCII, so ASCII text can never
    # match -- `isascii()` is an O(1) check on CPython's compact string
    # representation (it already knows its own max codepoint), while
    # `translate()`/`count()` are full O(len) scans. Real-world MARC
    # text is overwhelmingly ASCII, so this short-circuit skips those
    # scans for nearly every field instead of running them just to
    # discover nothing changed.
    if text.isascii():
        return text, 0
    new_text = text.translate(_SMART_CHAR_TABLE)
    if new_text == text:
        return text, 0
    count = sum(text.count(ch) for ch in SMART_CHAR_MAP)
    return new_text, count


def normalize_smart_characters(parsed: ParsedRecord) -> list[str]:
    """Replace typographic "smart" Unicode punctuation (see
    `SMART_CHAR_MAP`) with its plain-ASCII equivalent, in every subfield and
    control field. Runs by default; pass --no-normalize-smart-characters to
    skip it (some catalogers deliberately prefer proper typographic
    quotes/dashes in note fields, so leaving them alone needs to stay
    possible even though the default favors plain ASCII)."""
    details = []
    for f in parsed.fields:
        if f.is_control():
            if f.content:
                new_content, count = _replace_smart_chars(f.content)
                if count:
                    f.content = new_content
                    details.append(f"normalized {count} smart character(s) in ={f.tag}")
            continue
        total_count = 0
        codes_touched: list[str] = []
        new_subfields = []
        for code, data in f.subfields:
            new_data, count = _replace_smart_chars(data)
            if count:
                total_count += count
                codes_touched.append(code)
            new_subfields.append((code, new_data))
        if total_count:
            f.subfields = new_subfields
            codes = ",".join(f"${c}" for c in codes_touched)
            details.append(f"normalized {total_count} smart character(s) in ={f.tag} {codes}")
    return details


#: Telltale characters produced when correctly-decoded UTF-8 text gets
#: re-interpreted a second time as Latin-1/Windows-1252 and re-encoded
#: (a common real-world corruption from Excel/CSV round-trips and some
#: legacy export pipelines) -- e.g. "e" (U+00E9, 2 UTF-8 bytes C3 A9)
#: becomes "Ã©" once those 2 bytes are misread as 2 separate Latin-1
#: characters. "Ã" (U+00C3) is by far the most common marker since it's
#: the lead byte for most accented Latin letters' UTF-8 encoding; "Â"
#: and "â€" cover the rest of the common cases (nbsp/symbols, smart
#: quotes/dashes). See `_fix_mojibake`.
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€")


def _fix_mojibake(text: str) -> str | None:
    """Return the corrected text if `text` looks like it was UTF-8 that
    got double-encoded, or None if not (leave it alone). Re-encoding as
    cp1252 (Windows-1252) recovers the original bytes 1:1 -- cp1252
    rather than strict latin-1, since real-world mojibake overwhelmingly
    comes from Windows "ANSI" tools (Excel, legacy exports) that use
    cp1252's byte assignments for 0x80-0x9F (e.g. byte 0x9F is 'Y with
    diaeresis' in cp1252 vs. an unprintable C1 control code in strict
    latin-1); cp1252 is identical to latin-1 everywhere else, so this
    is strictly more capable, never less. Decoding those bytes as
    UTF-8 only succeeds if they were genuinely valid UTF-8 to begin
    with -- an essentially impossible coincidence for text that wasn't
    actually double-encoded, so a successful round-trip is strong
    confirmation, not a guess."""
    # Same reasoning as _replace_smart_chars: every marker is non-ASCII,
    # so this O(1) check skips the substring scans (and the encode/decode
    # round-trip below) for ordinary ASCII text, which is most of it.
    if text.isascii():
        return None
    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return None
    try:
        candidate = text.encode("cp1252").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None
    return candidate if candidate != text else None


def find_and_fix_mojibake(parsed: ParsedRecord) -> list[str]:
    """Fix double-encoded UTF-8 (see `_fix_mojibake`) in every subfield
    and control field. Only meaningful for text that's actually meant
    to be UTF-8 already -- skipped entirely for a record still
    declaring MARC-8, where the same marker bytes can legitimately
    appear as raw ANSEL diacritic codes and "fixing" them would corrupt
    real content. Logged (category "fixed_mojibake") since it changes
    real content, but this is a genuine, verified recovery of the
    record's own original text (not a placeholder), so it's an
    ordinary FIXED entry, not FIXED/REQUIRES ATTENTION.
    """
    if parsed.leader[9:10] != UNICODE_ENCODING_BYTE:
        return []
    details = []
    for f in parsed.fields:
        if f.is_control():
            if f.content:
                fixed = _fix_mojibake(f.content)
                if fixed is not None:
                    details.append(
                        f"fixed double-encoded UTF-8 in ={f.tag}: {f.content!r} -> {fixed!r}"
                    )
                    f.content = fixed
        else:
            new_subfields = []
            changed_codes = []
            for code, data in f.subfields:
                fixed = _fix_mojibake(data)
                if fixed is not None:
                    changed_codes.append(code)
                    new_subfields.append((code, fixed))
                else:
                    new_subfields.append((code, data))
            if changed_codes:
                f.subfields = new_subfields
                codes = ",".join(f"${c}" for c in changed_codes)
                details.append(f"fixed double-encoded UTF-8 in ={f.tag} {codes}")
    return details


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_output_path(input_path: str) -> str:
    base, ext = os.path.splitext(input_path)
    return f"{base}_fixed{ext}"


def _load_overrides(path: str) -> dict[int, dict[int, list[tuple[str, list[tuple[str, str]]]]]]:
    """overrides.json shape:
    {
      "<record_index>": {
        "<field_index>": {"indicators": "  ", "subfields": [["a", "..."], ["b", "..."]]}
      }
    }
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    out: dict[int, dict[int, list]] = {}
    for rec_idx, fields in raw.items():
        out[int(rec_idx)] = {}
        for field_idx, spec in fields.items():
            indicators = spec["indicators"]
            subfields = [(c, d) for c, d in spec["subfields"]]
            out[int(rec_idx)][int(field_idx)] = (indicators, subfields)
    return out


@contextmanager
def _null_writer():
    """A context manager that yields None -- used in place of an optional
    output file (e.g. --mrk not requested) so callers can write `with
    open(...) as fh, (real_or_null) as maybe_fh:` uniformly and just check
    `if maybe_fh:` before writing, without an extra branch for "not
    requested at all"."""
    yield None


def _read_text(path: str) -> str:
    """Read a MARC file or pasted-text file. See `_read_text_with_encoding`;
    this just discards which encoding was used, for callers that don't need
    to re-encode anything back to bytes later."""
    return _read_text_with_encoding(path)[0]


def _read_text_with_encoding(path: str) -> tuple[str, str]:
    """Read a MARC file or pasted-text file, returning (text, encoding_used).
    Real .mrc files may be UTF-8 or legacy MARC-8/Latin-1; try UTF-8 first
    (the common case for both pasted text and modern exports) and fall back
    to Latin-1, which never fails and preserves every byte 1:1 -- important
    both because this tool searches for exact control-character byte values,
    and so a record that can't be auto-repaired can still be passed through
    byte-for-byte using the same encoding it was read with."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


@dataclass
class LogEntry:
    """One line destined for the run's combined log. `category` is a
    stable machine-readable label (e.g. "missing_008", "field_removed_because_missing_a")
    used to group same-type entries together within their fixed/not-fixed
    block (see `write_log`); `fixed` says which block."""

    category: str
    fixed: bool
    ts: str
    record_idx: int
    record_id: str
    detail: str

    def render(self) -> str:
        tag = f"[{_section_for(self)[1]}]"
        rec = f"record {self.record_idx}"
        if self.record_id:
            rec += f" ({self.record_id})"
        return f"{tag}\t{self.category}\t{self.ts}\t{rec}\t{self.detail}"


#: There is no plain "FIXED" section -- every category that's ever
#: logged with fixed=True is explicitly placed in one of the two
#: sections below (or DUPLICATE RECORDS), so a reader never has to
#: wonder which bucket a given fix landed in.
#:
#: FIXED/REQUIRES ATTENTION: auto-fixed AND still worth a human's
#: attention -- these discard or alter enough real data (or add
#: content a human should double-check) that it's worth a second look
#: even though the record is now loadable. Sits right below NOT
#: FIXED: not as urgent as something left broken, but more urgent
#: than a routine fix.
_FIXED_REQUIRES_ATTENTION = {
    "removed_non_repeatable_duplicate",
    "fixed_008_length",
    "fixed_holdings_008_length",
    "removed_invalid_subfield",
    "field_removed_because_missing_a",
    "added_field",
    "holdings_leader_byte_defaulted",
}

#: INFORMATIONAL, at the very bottom: a fix applied via a fixed
#: default/constant or a systematic, file-wide transformation rather
#: than judgment applied to that record's own content (a placeholder
#: 008/245, a leader byte reset to a default code, $9 promoted to $0,
#: the leader's entry-map constant restored, an unparseable tag
#: renamed to an unused 9XX slot, typographic punctuation flattened,
#: a record transcoded MARC-8 -> UTF-8, double-encoded UTF-8
#: corrected, 999 remapped to 945, an oversized record's leader
#: sentinel applied, short indicators padded with spaces) -- or a
#: detect-only finding not urgent enough for NOT FIXED.
_INFORMATIONAL = {
    "added_default_008",
    "added_default_holdings_008",
    "added_missing_852c",
    "holdings_multiple_004",
    "added_default_245",
    "leader_byte_defaulted",
    "leader_entry_map_fixed",
    "normalized_subfield_9_to_0",
    "invalid_tag",
    "normalized_smart_characters",
    "transcoded_marc8",
    "invalid_indicator_value",
    "invalid_bibliographic_level",
    "dangling_880_link",
    "invalid_isbn_issn_checksum",
    "fixed_mojibake",
    "remapped_999_to_945",
    "oversized_sentinel_fixed",
    "padded_indicators",
}

_DEDICATED_SECTIONS: dict[str, tuple[int, str]] = {
    "duplicate_identifier": (3, "DUPLICATE RECORDS"),
}
for _cat in _FIXED_REQUIRES_ATTENTION:
    _DEDICATED_SECTIONS[_cat] = (1, "FIXED/REQUIRES ATTENTION")
for _cat in _INFORMATIONAL:
    _DEDICATED_SECTIONS[_cat] = (4, "INFORMATIONAL")
del _cat


def _section_for(entry: LogEntry) -> tuple[int, str]:
    """(sort_order, section_label) for `entry` -- NOT FIXED, then
    FIXED/REQUIRES ATTENTION, then any other dedicated sections (see
    `_DEDICATED_SECTIONS`) like DUPLICATE RECORDS, then INFORMATIONAL.
    There is no plain "FIXED" section -- an unrecognized category
    logged with fixed=True falls back to INFORMATIONAL rather than a
    generic bucket, so every new fix category must be added to
    `_FIXED_REQUIRES_ATTENTION` or `_INFORMATIONAL` above to land
    somewhere deliberate."""
    dedicated = _DEDICATED_SECTIONS.get(entry.category)
    if dedicated is not None:
        return dedicated
    return (4, "INFORMATIONAL") if entry.fixed else (0, "NOT FIXED")


def _timestamped_log_path(path: str, run_ts: str) -> str:
    """Insert `run_ts` right before `path`'s extension, so every log this
    tool writes is timestamped -- even one named explicitly via --log --
    and repeat runs never silently overwrite or blend into a prior run's
    log."""
    base, ext = os.path.splitext(path)
    return f"{base}_{run_ts}{ext}"


def write_log(path: str, entries: list[LogEntry]) -> None:
    """Write `entries` grouped into sections -- NOT FIXED, then FIXED/
    REQUIRES ATTENTION, then DUPLICATE RECORDS, then INFORMATIONAL at
    the bottom (there is no plain "FIXED" section -- see
    `_section_for`) -- and then by category within each, with a header
    per group -- so a run with (say) 375 missing-008 warnings and 7
    doubled-proxy-URL warnings shows them as two clearly labeled,
    contiguous blocks instead of interleaved in whatever order the
    records happened to come in."""
    groups: dict[tuple[int, str, str], list[LogEntry]] = {}
    for e in entries:
        order, label = _section_for(e)
        groups.setdefault((order, label, e.category), []).append(e)

    with open(path, "a", encoding="utf-8") as fh:
        for section_num, (order, label, category) in enumerate(sorted(groups)):
            group = groups[(order, label, category)]
            if section_num > 0:
                fh.write("\n")
            fh.write(f"=== {label}: {category} ({len(group)}) ===\n")
            for e in group:
                fh.write(e.render() + "\n")


class ProgressReporter:
    """Prints a periodic progress line to stderr while a long run is in
    flight -- otherwise a multi-minute run on a huge file gives no sign of
    life until it's completely done. Throttled by both time and record
    count so it doesn't spam a redirected/logged stderr, and self-adjusts
    between an in-place updating line (interactive terminal) and full
    lines (piped/redirected output, e.g. into a log file).
    """

    def __init__(self, total_bytes: int, min_interval: float | None = None):
        self.total_bytes = total_bytes
        self.is_tty = sys.stderr.isatty()
        # an interactive terminal can take frequent in-place updates; a
        # redirected/logged stderr should get full lines only occasionally,
        # or a long run would spam the log with thousands of them
        default_interval = 0.5 if self.is_tty else 10.0
        self.min_interval = min_interval if min_interval is not None else default_interval
        self.start_time = time.perf_counter()
        self.last_print_time = 0.0
        self.bytes_read = 0
        self.printed_anything = False
        self.estimate_shown = False

    def on_progress(self, bytes_read: int) -> None:
        self.bytes_read = bytes_read

    def maybe_print_estimate(self, n_records: int, bytes_consumed: int) -> None:
        """Print a one-time, early, rough total-runtime estimate -- separate
        from the ongoing ETA in `maybe_print` below, which only starts
        appearing once that method's own throttle interval has elapsed
        (up to 10s on a non-tty stderr, meaning a modest-sized run could
        finish before it ever prints anything). Waits for at least 200
        records and 0.5s of elapsed time -- a real run showed a much
        shorter sample (50 records / 0.1s) gives a rate dominated by
        one-time startup/warm-up cost rather than steady-state
        throughput, overestimating total time by ~4x -- but otherwise
        fires as early as possible.

        `bytes_consumed` must be actual bytes consumed by the
        `n_records` processed so far (the caller tracks this from each
        record's own text) -- NOT `self.bytes_read`, which reflects the
        streaming reader's buffered read-ahead and can jump to a whole
        32MB+ chunk almost instantly, long before the records in it are
        actually processed. Using that instead here was a real bug: it
        made the very first estimate wildly too optimistic (a "~0s"
        estimate on a run that actually took 12 seconds), because it
        divided a large already-buffered byte count by only a handful
        of actually-finished records, understating the true average
        record size by two orders of magnitude.
        """
        if self.estimate_shown or not self.total_bytes or bytes_consumed <= 0:
            return
        elapsed = time.perf_counter() - self.start_time
        if n_records < 200 or elapsed < 1.0:
            return
        self.estimate_shown = True
        avg_bytes_per_record = bytes_consumed / n_records
        rate = n_records / elapsed
        if avg_bytes_per_record <= 0 or rate <= 0:
            return
        estimated_total_records = self.total_bytes / avg_bytes_per_record
        estimated_total_time = estimated_total_records / rate
        print(
            f"Estimated total runtime: ~{_format_duration(estimated_total_time)} "
            f"for {self.total_bytes / 1_000_000:.1f} MB "
            f"(rough estimate from the first {n_records:,} records; assumes no "
            "record needs the invalid-tag second pass)",
            file=sys.stderr,
        )

    def maybe_print(self, n_records: int) -> None:
        now = time.perf_counter()
        if now - self.last_print_time < self.min_interval:
            return
        self.last_print_time = now
        elapsed = now - self.start_time
        rate = n_records / elapsed if elapsed > 0 else 0
        parts = [
            f"{n_records:,} records",
            f"{rate:,.0f} rec/s",
            f"elapsed {_format_duration(elapsed)}",
        ]
        if self.total_bytes:
            pct = 100 * self.bytes_read / self.total_bytes
            parts.insert(1, f"{pct:4.1f}% of input")
            if self.bytes_read > 0 and pct < 100:
                eta = elapsed * (self.total_bytes - self.bytes_read) / self.bytes_read
                parts.append(f"ETA {_format_duration(eta)}")
        line = " | ".join(parts)
        end = "\r" if self.is_tty else "\n"
        print(line, end=end, file=sys.stderr)
        self.printed_anything = True

    def finish(self) -> None:
        if self.printed_anything and self.is_tty:
            print(file=sys.stderr)  # move off the in-place progress line


def repair_holdings_records(
    input_path: str,
    output_path: str,
    log_path: str,
    fix_missing_852c: bool = False,
    on_progress: Callable[[int], None] | None = None,
    on_record: Callable[[int], None] | None = None,
    on_estimate: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Actually repair a holdings-only MARC file (e.g. one of
    `split_bib_holdings`'s outputs) -- unlike `check_holdings_records`
    (still available, and still purely read-only), this rewrites the
    file. It reuses this tool's same self-determining structural repair
    (`iter_repair_stream`, Mode 1/2 -- fixes bad length/bad directory for
    free, since those are just consequences of a stale declared length
    once the record's real content is re-derived) plus the subset of
    the main bib pipeline's content fixes that are safe and meaningful
    for a holdings record specifically:

      * short-indicator padding (via `iter_repair_stream`'s own
        `fix_bad_indicators`)
      * double-encoded UTF-8 ("mojibake") correction
      * leader byte 05 defaulted the same way as bib records; byte 06
        (type of record) defaults to 'u' (Unknown) instead of bib's
        'a' (Language material); byte 17 (encoding level) is checked
        against holdings' OWN code set
        (`LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS`, which includes 'm'
        and excludes blank, unlike the shared bib/authority/holdings
        union) and defaults to 'u' rather than blank -- see
        `fix_invalid_leader_bytes`. Logged as
        "holdings_leader_byte_defaulted" -- FIXED/REQUIRES ATTENTION,
        not INFORMATIONAL like the bib pipeline's equivalent -- since
        this changes real classification bytes and is worth
        highlighting rather than burying in the bottom, off-by-default
        section
      * $9 -> $0 subfield code normalization
      * typographic "smart" character normalization
      * invalid (non a-z0-9) subfield code removal
      * empty-field removal
      * a missing 008 gets a blank, syntactically-valid placeholder
        (see `add_default_holdings_008` -- there's no institution-
        agnostic *content* default the way bib's placeholder has one)
      * a wrong-length 008 is padded/truncated to the holdings-specific
        32 bytes (see `HOLDINGS_008_LENGTH`), not bib's 40
      * a missing 004 (Control Number linking to the bib record) is
        flagged NOT FIXED (category "holdings_missing_004"); more than
        one is flagged INFORMATIONAL (category "holdings_multiple_004",
        since a legitimate multi-bib link isn't necessarily wrong) --
        see `_find_004_issues`; neither is ever invented or trimmed
      * if `fix_missing_852c` is set (off by default -- see
        --fix-missing-852c), an 852 (Location) field missing $c
        (shelving location) gets a placeholder $c appended (see
        `add_missing_852c`)
      * a non-numeric tag is renamed to an unused 9XX slot, same
        deferred two-pass approach `main` uses for bib records (needs
        every tag in the *holdings* file specifically, so this is
        tracked separately from the bib pass)
      * an 001 (or other record identifier -- see `record_identifier`)
        reused across more than one record in this run is flagged for
        every record involved (category "duplicate_identifier",
        DUPLICATE RECORDS section, same as the bib pipeline) -- see
        `find_duplicate_identifiers`; this is a whole-file check, run
        once after the full pass, same as the bib pipeline's equivalent

    Deliberately NOT applied here (bib-specific, would misfire on a
    holdings record): a placeholder 245 (holdings records have no 245),
    `strip_missing_required_a` / `strip_duplicate_non_repeatable_fields`
    (their tag lists -- required_a_tags.txt / non_repeatable_tags.txt --
    were built against bibliographic field semantics), and
    `remap_999_to_945` (a Sierra/bib-specific convention). MARC-8-to-
    UTF-8 transcoding is also skipped for now (an explicit, temporary
    scope decision, not a permanent one) -- an ESC byte is still
    flagged (category "holdings_escape_sequence", NOT FIXED) rather
    than silently left in a record declared UTF-8, and a null
    identifier (subfield present but empty) is still flagged (category
    "holdings_null_identifier", NOT FIXED) rather than guessed at.

    A record that can't be structurally parsed at all is passed through
    unchanged, exactly like the main bib pipeline (category
    "unresolved_record", NOT FIXED) -- the output always has the same
    number of records as the input.

    `on_progress`/`on_record` mirror `split_bib_holdings`'s parameters
    of the same name -- liveness only, no effect on the repair.
    `on_estimate`, if given, is called after every record with
    (records processed so far, bytes consumed so far) -- meant to be
    wired to a `ProgressReporter`'s `maybe_print_estimate`, which
    self-throttles to fire (at most) once early in a long run and is a
    cheap no-op every other call, the same way `main`'s own bib loop
    uses it.

    Returns {"total": n, "unresolved": n, "log_lines": n, "not_fixed": n}.
    """
    encoding_used = detect_encoding(input_path)
    log_entries: list[LogEntry] = []
    used_tags: set[str] = set()
    pending_tag_fixes: list[tuple[int, int, int, str]] = []
    id_records: list[tuple[int, str, str]] = []
    n_total = 0
    n_unresolved = 0
    bytes_consumed_for_estimate = 0

    def log(category: str, fixed: bool, record_idx: int, rec_id: str, detail: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_entries.append(LogEntry(category, fixed, ts, record_idx, rec_id, detail))

    with open(output_path, "wb") as out_fh:
        record_stream = iter_repair_stream(
            input_path, on_progress=on_progress, fix_bad_indicators=True,
        )
        for i, (parsed, rec_text) in enumerate(record_stream):
            n_total += 1
            if on_record is not None:
                on_record(n_total)
            if on_estimate is not None:
                bytes_consumed_for_estimate += len(rec_text.encode(encoding_used))
                on_estimate(n_total, bytes_consumed_for_estimate)

            if parsed.unresolved:
                reason = parsed.unresolved[0][3]
                log("unresolved_record", False, i, "", f"passed through unchanged: {reason}")
                out_fh.write(rec_text.encode(encoding_used))
                n_unresolved += 1
                continue

            for tag, spaces_added in parsed.indicator_fixes:
                rec_id = record_identifier(parsed)
                log(
                    "padded_indicators", True, i, rec_id,
                    f"padded {spaces_added} space(s) into short indicators on ={tag}",
                )
            if ESCAPE in rec_text:
                rec_id = record_identifier(parsed)
                log(
                    "holdings_escape_sequence", False, i, rec_id,
                    "contains an ESC (0x1B) byte -- possible unconverted MARC-8 "
                    "escape sequence (not transcoded -- MARC-8-to-UTF-8 conversion "
                    "is skipped for holdings records for now)",
                )
            rec_id = record_identifier(parsed)
            for detail in find_and_fix_mojibake(parsed):
                log("fixed_mojibake", True, i, rec_id, detail)
            for detail in fix_invalid_leader_bytes(
                parsed,
                type_of_record_default="u",
                encoding_level_valid=LEADER_17_ENCODING_LEVEL_VALID_HOLDINGS,
                encoding_level_default="u",
            ):
                log("holdings_leader_byte_defaulted", True, i, rec_id, detail)
            for detail in normalize_subfield_9_to_0(parsed):
                log("normalized_subfield_9_to_0", True, i, rec_id, detail)
            for detail in normalize_smart_characters(parsed):
                log("normalized_smart_characters", True, i, rec_id, detail)
            for detail in strip_invalid_subfield_codes(parsed):
                log("removed_invalid_subfield", True, i, rec_id, detail)
            strip_empty_fields(parsed)
            for detail in add_default_holdings_008(parsed):
                log("added_default_holdings_008", True, i, rec_id, detail)
            for detail in fix_008_length(parsed, expected_len=HOLDINGS_008_LENGTH):
                log("fixed_holdings_008_length", True, i, rec_id, detail)
            if fix_missing_852c:
                for detail in add_missing_852c(parsed):
                    log("added_missing_852c", True, i, rec_id, detail)
            for f in parsed.fields:
                if f.is_control():
                    continue
                for code, data in f.subfields:
                    if not data:
                        log(
                            "holdings_null_identifier", False, i, rec_id,
                            f"tag {f.tag}: subfield ${code} has no data "
                            "(null identifier)",
                        )
            n_004 = sum(1 for f in parsed.fields if f.tag == "004")
            for category, detail in _find_004_issues(n_004):
                log(category, False, i, rec_id, detail)

            try:
                assembled = assemble_marc(parsed)
            except RepairError as exc:
                log("oversized_unfixable", False, i, rec_id, f"passed through unchanged: {exc}")
                out_fh.write(rec_text.encode(encoding_used))
                n_unresolved += 1
                if rec_id:
                    id_records.append((i, rec_id, rec_text[:5]))
                continue

            if len(assembled) > 99999:
                log(
                    "oversized_sentinel_fixed", True, i, rec_id,
                    f"record is {len(assembled)} bytes; leader declares the "
                    "MARC21 sentinel 99999 instead (real end is still found "
                    "from the record terminator, nothing lost)",
                )

            offset = out_fh.tell()
            out_fh.write(assembled)
            if rec_id:
                id_records.append((i, rec_id, assembled[:5].decode("ascii")))

            has_invalid_tag = False
            for f in parsed.fields:
                if f.tag.isdigit():
                    used_tags.add(f.tag)
                else:
                    has_invalid_tag = True
            if has_invalid_tag:
                rec_encoding = "utf-8" if parsed.leader[9:10] == "a" else "latin-1"
                pending_tag_fixes.append((i, offset, len(assembled), rec_encoding))

    if pending_tag_fixes:
        replacement_tag = pick_unused_9xx_tag(used_tags)
        fix_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(output_path, "r+b") as fixup_fh:
            for record_idx, offset, length, rec_encoding in pending_tag_fixes:
                fixup_fh.seek(offset)
                raw = fixup_fh.read(length)
                rec = read_intact_record(raw.decode(rec_encoding))
                rec_id = record_identifier(rec)
                if replacement_tag is None:
                    for f in rec.fields:
                        if not f.tag.isdigit():
                            log_entries.append(LogEntry(
                                "non_numeric_tag", False, fix_ts, record_idx, rec_id,
                                f"tag {f.tag!r} is not 3 numeric digits (could not "
                                "fix: every 900-999 tag is already used elsewhere "
                                "in this file)",
                            ))
                    continue
                for detail in fix_invalid_tags(rec, replacement_tag):
                    log_entries.append(
                        LogEntry("invalid_tag", True, fix_ts, record_idx, rec_id, detail)
                    )
                fixed_bytes = assemble_marc(rec)
                assert len(fixed_bytes) == length, (
                    "tag rename must not change a record's total byte length"
                )
                fixup_fh.seek(offset)
                fixup_fh.write(fixed_bytes)

    dup_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_entries.extend(find_duplicate_identifiers(id_records, dup_ts))

    if log_entries:
        write_log(log_path, log_entries)
    n_not_fixed = sum(1 for e in log_entries if _section_for(e)[1] == "NOT FIXED")
    return {
        "total": n_total,
        "unresolved": n_unresolved,
        "log_lines": len(log_entries),
        "not_fixed": n_not_fixed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "input", help="MARC file (.mrc) or pasted-text file with one or more records"
    )
    parser.add_argument(
        "-o", "--out", help="output .mrc file (default: INPUT_fixed.mrc next to the input)"
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="print the number of records in the input file and exit "
        "immediately -- counts raw record-terminator (0x1D) bytes in "
        "big binary chunks without parsing or repairing anything, for "
        "speed on large files. No output file is written",
    )
    parser.add_argument(
        "--split-bib-holdings",
        action="store_true",
        help="split the input into separate bib and holdings files by each "
        "record's leader byte 6 (type of record), then exit immediately "
        "-- no repair is done on either. Output: INPUT_bib.EXT and "
        "INPUT_holdings.EXT next to the input; a record whose leader "
        "byte 6 isn't a recognized bib or holdings code (or is "
        "missing/corrupted) is never guessed at -- it's written "
        "instead to INPUT_unclassified.EXT (only created if needed) "
        "and reported on stderr. The holdings records are then "
        "actually repaired too (see repair_holdings_records()) -- the "
        "same categories of structural/content fix already applied to "
        "bib records (bad length, bad directory, missing 008 [a "
        "holdings-specific 32-byte placeholder, not bib's], bad "
        "indicators, invalid subfield codes, mojibake, smart "
        "characters, invalid tags), except MARC-8-to-UTF-8 transcoding "
        "(skipped for holdings for now) and the bib-specific tag-list "
        "fixes (245/required-$a/duplicate-field, which don't apply to "
        "holdings semantics). See also --fix-missing-852c, an opt-in "
        "holdings-specific content fix. Output: INPUT_holdings_repaired.EXT; a "
        "record that can't be auto-repaired is passed through "
        "unchanged like the main bib pipeline. Every fix/finding is "
        "logged to INPUT_holdings_log_TIMESTAMP.log",
    )
    parser.add_argument(
        "--repair-holdings",
        action="store_true",
        help="treat the input as an already-holdings-only MARC file (e.g. "
        "one of --split-bib-holdings' own INPUT_holdings.EXT outputs) and "
        "repair it directly, then exit immediately -- the exact same "
        "repair --split-bib-holdings applies to its holdings output (see "
        "repair_holdings_records()), without re-splitting anything or "
        "touching bib records. Useful when the bib/holdings split was "
        "already done in an earlier run (or by some other tool) and only "
        "the holdings side needs (re-)repairing. Output: INPUT_repaired.EXT "
        "next to the input (or -o/--out); log: INPUT_log_TIMESTAMP.log (or "
        "--log). See --fix-missing-852c for the one opt-in "
        "holdings-specific content fix",
    )
    parser.add_argument(
        "--fix-missing-852c",
        dest="fix_missing_852c",
        action="store_true",
        default=False,
        help="(holdings records only, used by --split-bib-holdings and "
        "--repair-holdings) add a "
        f"placeholder $c (shelving location) subfield -- content "
        f"{DEFAULT_852_C_CONTENT!r} -- to any 852 (Location) field missing "
        "one. Off by default since 852 $c is real location data this tool "
        "has no way to know; each insertion is logged (see --log) as a "
        "placeholder, not a real value",
    )
    parser.add_argument(
        "--mrk",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="also write a mnemonic .mrk text file; not written unless this "
        "flag is given. PATH is optional -- bare --mrk defaults to OUT.mrk "
        "next to the .mrc output",
    )
    parser.add_argument("--overrides", help="JSON file with manual subfield splits (Mode 2 only)")
    parser.add_argument(
        "--ensure-field",
        action="append",
        default=[],
        metavar="TAG:INDICATORS:CODE=VALUE",
        help="add this field to any record missing TAG (repeatable), "
        "e.g. --ensure-field \"245:00:a=No title\"; for a control field "
        "(tag < 010, e.g. 008), use TAG:CONTENT instead, e.g. "
        "--ensure-field \"008:780615s19uu    xx a                    d\"",
    )
    parser.add_argument(
        "--no-strip-missing-required-a",
        dest="strip_missing_required_a",
        action="store_false",
        default=True,
        help="do NOT remove fields (from the tag list in "
        "--required-a-tags-file) that lack a required, non-empty $a "
        "subfield. By default such fields ARE removed; fields with other "
        "non-empty content are logged (see --log) before being "
        "discarded, empty ones are dropped silently",
    )
    parser.add_argument(
        "--required-a-tags-file",
        default=DEFAULT_REQUIRED_A_TAGS_FILE,
        help=f"tag list for the default $a-required-fields removal (see "
        f"--no-strip-missing-required-a), one tag per line "
        f"(default: {DEFAULT_REQUIRED_A_TAGS_FILE})",
    )
    parser.add_argument(
        "--no-strip-duplicate-non-repeatable-fields",
        dest="strip_duplicate_non_repeatable_fields",
        action="store_false",
        default=True,
        help="do NOT remove later occurrences of a Not-Repeatable field "
        "(from the tag list in --non-repeatable-tags-file, e.g. a "
        "second 245) that appears more than once. By default all but "
        "the first occurrence ARE removed -- a strict importer like "
        "FOLIO can reject or mishandle the duplicate otherwise -- and "
        "every removed field's exact content is logged in full under "
        "FIXED/REQUIRES ATTENTION (see --log)",
    )
    parser.add_argument(
        "--non-repeatable-tags-file",
        default=DEFAULT_NON_REPEATABLE_TAGS_FILE,
        help=f"tag list for the default duplicate-non-repeatable-field "
        f"removal (see --no-strip-duplicate-non-repeatable-fields), one "
        f"tag per line (default: {DEFAULT_NON_REPEATABLE_TAGS_FILE})",
    )
    parser.add_argument(
        "--no-fix-008-length",
        dest="fix_008_length",
        action="store_false",
        default=True,
        help="do NOT pad/truncate an 008 field that isn't exactly 40 "
        "characters. By default this IS done -- a wrong-length 008 "
        "can make a record unloadable in strict importers -- and "
        "logged in full under FIXED/REQUIRES ATTENTION (see --log)",
    )
    parser.add_argument(
        "--no-fix-mojibake",
        dest="fix_mojibake",
        action="store_false",
        default=True,
        help="do NOT fix double-encoded UTF-8 (\"mojibake\", e.g. text "
        "read once as UTF-8 then mis-read again as Latin-1) in "
        "records already declaring UTF-8. By default this IS fixed "
        "and logged (see --log); only applied when re-decoding the "
        "text as UTF-8 actually succeeds, which is effectively "
        "impossible by coincidence for text that wasn't genuinely "
        "double-encoded",
    )
    parser.add_argument(
        "--log-informational",
        dest="log_informational",
        action="store_true",
        default=False,
        help="include the INFORMATIONAL section in the log file. Off "
        "by default -- these are typically the highest-volume "
        "categories (e.g. every MARC-8 record transcoded), so this "
        "keeps the log lean unless you actually need that detail; the "
        "underlying fixes/detections still run and affect the output "
        "either way, only the log content changes",
    )
    parser.add_argument(
        "--no-strip-invalid-subfield-codes",
        dest="strip_invalid_subfield_codes",
        action="store_false",
        default=True,
        help="do NOT remove subfields whose code isn't a lowercase letter "
        "or digit -- unusable data with no safe way to guess what it "
        "should have been. By default such subfields ARE removed, each "
        "removal timestamp-logged (see --log); a field left with no "
        "valid subfields is then dropped entirely",
    )
    parser.add_argument(
        "--no-strip-empty-fields",
        dest="strip_empty_fields",
        action="store_false",
        default=True,
        help="do NOT remove a data field where every subfield's data is "
        "empty (e.g. a bare \"$a\" with nothing after it), for any tag. "
        "By default such fields ARE removed -- unlike "
        "--no-strip-missing-required-a this isn't scoped to a tag list, "
        "since a field with zero non-empty subfields holds no "
        "information under any tag. Nothing is discarded (there's "
        "nothing there), so this isn't logged either way",
    )
    parser.add_argument(
        "--no-fix-bad-indicators",
        dest="fix_bad_indicators",
        action="store_false",
        default=True,
        help="do NOT pad a data field's indicators with space(s) when 0 "
        "or 1 characters precede its first subfield delimiter instead of "
        "the required 2 (a common corruption). By default such fields "
        "ARE padded and logged (see --log); with this flag such a field "
        "fails Mode 1 instead and the whole record falls back to Mode "
        "2/UNRESOLVED",
    )
    parser.add_argument(
        "--no-fix-invalid-leader-bytes",
        dest="fix_invalid_leader_bytes",
        action="store_false",
        default=True,
        help="do NOT default leader bytes 05/06/08/17 (record status, type "
        "of record, type of control, encoding level) when they hold a "
        "value outside their valid MARC21 code set. By default each is "
        "reset to a fixed default (see fix_invalid_leader_bytes()) and "
        "logged (see --log); pass this flag to leave them as-is instead",
    )
    parser.add_argument(
        "--no-add-default-245",
        dest="add_default_245",
        action="store_false",
        default=True,
        help="do NOT insert a placeholder 245 ($a \"No title\") for a "
        "record with no 245 at all. By default one is inserted and "
        "logged (see --log) -- many real-world imports outright reject a "
        "record with no title -- rather than leaving such records with "
        "no 245; --ensure-field \"245:...\" for a specific record takes "
        "priority over this default. Pass this flag to leave 245-less "
        "records untouched instead",
    )
    parser.add_argument(
        "--no-add-default-008",
        dest="add_default_008",
        action="store_false",
        default=True,
        help="do NOT insert a placeholder 008 for a record with no 008 at "
        f"all. By default a fixed placeholder ({DEFAULT_008_CONTENT!r}, "
        "matching a widely-used site cleanup script's own blanket "
        "default) is inserted and logged (see --log) rather than leaving "
        "such records with no 008; --ensure-field \"008:...\" for a "
        "specific record takes priority over this default. Pass this "
        "flag to leave 008-less records untouched instead",
    )
    parser.add_argument(
        "--no-fix-invalid-tags",
        dest="fix_invalid_tags",
        action="store_false",
        default=True,
        help="do NOT rename fields whose tag isn't 3 numeric digits (e.g. "
        "\"ABC\" from directory corruption) to an unused tag in the "
        "900-999 locally-defined-field range. By default this is done "
        "and logged (see --log); pass this flag to leave such fields "
        "with their invalid tag instead. The replacement tag is chosen "
        "from tags seen during the normal single pass over the file, so "
        "this adds no extra full pass -- only records with an invalid "
        "tag (rare) get a second, targeted look afterward",
    )
    parser.add_argument(
        "--remap-999-to-945",
        dest="remap_999_to_945",
        action="store_true",
        default=False,
        help="retag 999 fields to 945 (indicators forced to \"ff\"). "
        "999 (Sierra's internal item-linking field, not part of MARC21) "
        "is left as-is by default; pass this flag to have every one "
        "retagged instead to 945, a locally-defined field other "
        "systems will actually accept. Off by default since it's not a "
        "structural defect and not every source is Sierra-originated. "
        "Not logged unless --log-999-to-945 is also given (this remap "
        "is typically extremely high-volume -- multiple 999s per "
        "record -- so it's excluded from the log by default)",
    )
    parser.add_argument(
        "--log-999-to-945",
        action="store_true",
        help="log each individual 999-to-945 remap (see --remap-999-to-945). "
        "Off by default since a real file can have many 999 fields per "
        "record, which would otherwise dominate the log",
    )
    parser.add_argument(
        "--log-leader-entry-map-fixed",
        action="store_true",
        help="log each individual leader entry-map correction (bytes "
        "20-23 forced back to the fixed constant '4500'). Off by "
        "default since this can be nearly every record in a file with "
        "this specific corruption, which would otherwise dominate the "
        "log; the fix itself always runs regardless of this flag",
    )
    parser.add_argument(
        "--log-transcoded-marc8",
        action="store_true",
        help="log each individual MARC-8-to-UTF-8 transcoding (see "
        "--no-transcode-marc8). Off by default since this can be "
        "nearly every record in a legacy file, which would otherwise "
        "dominate the log",
    )
    parser.add_argument(
        "--log-normalized-smart-characters",
        action="store_true",
        help="log each individual smart-character normalization (see "
        "--no-normalize-smart-characters). Off by default since this "
        "can be nearly every record in a file with typographic "
        "punctuation, which would otherwise dominate the log; the fix "
        "itself always runs regardless of this flag",
    )
    parser.add_argument(
        "--no-normalize-subfield-9",
        dest="normalize_subfield_9",
        action="store_false",
        default=True,
        help="do NOT rewrite $9 subfield codes to $0. By default every "
        "$9 is unconditionally rewritten to $0 (a long-standing cleanup "
        "convention treating $9 as a legacy/local stand-in for the "
        "standard authority-linking subfield); pass this flag to leave "
        "$9 subfields as-is instead. Not logged per-record by default "
        "(see --log-normalized-subfield-9-to-0) since this can be "
        "nearly every record in a file that uses $9",
    )
    parser.add_argument(
        "--log-normalized-subfield-9-to-0",
        action="store_true",
        help="log each individual $9-to-$0 rewrite (see "
        "--no-normalize-subfield-9). Off by default since this can be "
        "nearly every record in a file that uses $9, which would "
        "otherwise dominate the log; the fix itself always runs "
        "regardless of this flag",
    )
    parser.add_argument(
        "--check-isbn-issn-checksum",
        action="store_true",
        help="detect a 020 (ISBN) or 022 (ISSN) $a whose check digit "
        "fails the standard checksum for its length. Off by default "
        "since it's a detect-only, no-fix check on data that's often "
        "already correct; pass this flag to have it run and be logged "
        "as invalid_isbn_issn_checksum (INFORMATIONAL, still needs "
        "--log-informational too)",
    )
    parser.add_argument(
        "--check-dangling-880-links",
        action="store_true",
        help="detect an 880 field whose $6 linking subfield references "
        "a tag that doesn't exist elsewhere in the record. Off by "
        "default since it's a detect-only, no-fix check; pass this "
        "flag to have it run and be logged as dangling_880_link "
        "(INFORMATIONAL, still needs --log-informational too)",
    )
    parser.add_argument(
        "--no-normalize-smart-characters",
        dest="normalize_smart_characters",
        action="store_false",
        default=True,
        help="do NOT replace typographic \"smart\" Unicode punctuation "
        "(curly quotes, em/en dashes, ellipsis -- see SMART_CHAR_MAP) with "
        "plain ASCII equivalents. By default this normalization runs on "
        "every subfield and control field and is logged (see --log); pass "
        "this flag to leave smart characters as-is instead",
    )
    parser.add_argument(
        "--log",
        help="single combined log file for every removal, warning, and "
        "pass-through-unchanged notice from this run (default: "
        "OUT_log_TIMESTAMP.log). The run timestamp is always inserted "
        "before the extension -- even when this is given explicitly -- so "
        "repeat runs never overwrite or blend into a prior run's log",
    )
    parser.add_argument(
        "--no-transcode-marc8",
        dest="transcode_marc8",
        action="store_false",
        default=True,
        help="do NOT convert MARC-8/ANSEL encoded records to UTF-8. By "
        "default such records ARE converted (leader's encoding byte "
        "flipped accordingly; records already declaring Unicode are "
        "left alone); not logged per-record by default (see "
        "--log-transcoded-marc8) since this can be nearly every record "
        "in a legacy file. Requires pymarc (pip install -r "
        "requirements.txt) -- if it's not installed, the run fails "
        "immediately rather than silently producing non-UTF-8 output; "
        "pass this flag to skip transcoding deliberately instead",
    )
    args = parser.parse_args(argv)

    if args.count:
        print(f"{count_records(args.input)} record(s) in {args.input}")
        return 0

    if args.split_bib_holdings:
        base, ext = os.path.splitext(args.input)
        bib_path = f"{base}_bib{ext}"
        holdings_path = f"{base}_holdings{ext}"
        unclassified_path = f"{base}_unclassified{ext}"
        split_start = time.perf_counter()
        split_progress = ProgressReporter(total_bytes=os.path.getsize(args.input))
        counts = split_bib_holdings(
            args.input,
            bib_path,
            holdings_path,
            unclassified_path,
            on_progress=split_progress.on_progress,
            on_record=split_progress.maybe_print,
        )
        split_progress.finish()
        print(f"{counts['bib']} bib record(s) written to {bib_path}")
        print(f"{counts['holdings']} holdings record(s) written to {holdings_path}")
        if counts["unclassified"]:
            print(
                f"{counts['unclassified']} record(s) could not be classified as "
                f"bib or holdings from leader byte 6 -- written unchanged to "
                f"{unclassified_path} instead of being guessed at or dropped",
                file=sys.stderr,
            )
        if counts["holdings"]:
            holdings_repaired_path = f"{base}_holdings_repaired{ext}"
            run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            holdings_log_path = f"{base}_holdings_log_{run_ts}.log"
            holdings_progress = ProgressReporter(total_bytes=os.path.getsize(holdings_path))
            result = repair_holdings_records(
                holdings_path,
                holdings_repaired_path,
                holdings_log_path,
                fix_missing_852c=args.fix_missing_852c,
                on_progress=holdings_progress.on_progress,
                on_record=holdings_progress.maybe_print,
                on_estimate=holdings_progress.maybe_print_estimate,
            )
            holdings_progress.finish()
            print(
                f"{result['total']}/{result['total']} holdings record(s) repaired "
                f"and written to {holdings_repaired_path} "
                f"({result['total'] - result['unresolved']} corrected/passed clean, "
                f"{result['unresolved']} passed through unchanged)"
            )
            if result["log_lines"]:
                print(
                    f"{result['log_lines']} holdings log line(s) written to "
                    f"{holdings_log_path} ({result['not_fixed']} not fixed, "
                    f"{result['log_lines'] - result['not_fixed']} fixed)",
                    file=sys.stderr,
                )
        elapsed = time.perf_counter() - split_start
        print(f"done in {elapsed:.2f}s")
        return 0

    if args.repair_holdings:
        base, ext = os.path.splitext(args.input)
        out_path = args.out or f"{base}_repaired{ext}"
        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = (
            _timestamped_log_path(args.log, run_ts) if args.log
            else f"{base}_log_{run_ts}.log"
        )
        repair_start = time.perf_counter()
        progress = ProgressReporter(total_bytes=os.path.getsize(args.input))
        result = repair_holdings_records(
            args.input,
            out_path,
            log_path,
            fix_missing_852c=args.fix_missing_852c,
            on_progress=progress.on_progress,
            on_record=progress.maybe_print,
            on_estimate=progress.maybe_print_estimate,
        )
        progress.finish()
        elapsed = time.perf_counter() - repair_start
        print(
            f"Wrote {result['total']}/{result['total']} holdings record(s) to "
            f"{out_path} ({result['total'] - result['unresolved']} "
            f"corrected/passed clean, {result['unresolved']} passed through "
            f"unchanged) in {elapsed:.2f}s"
        )
        if result["log_lines"]:
            print(
                f"{result['log_lines']} log line(s) written to {log_path} "
                f"({result['not_fixed']} not fixed, "
                f"{result['log_lines'] - result['not_fixed']} fixed)",
                file=sys.stderr,
            )
        if result["unresolved"]:
            print(
                f"{result['unresolved']} record(s) could not be auto-repaired "
                "and were kept unchanged in the output.",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.transcode_marc8:
        try:
            import pymarc.marc8  # noqa: F401
        except ImportError:
            # UTF-8 output is a hard requirement -- silently skipping
            # the one thing that makes MARC-8 records comply with it
            # would leave the output non-conformant without the user
            # ever explicitly choosing that. Fail loudly instead;
            # --no-transcode-marc8 remains available for anyone who
            # explicitly wants non-UTF-8 output preserved as-is.
            print(
                "pymarc not installed, but MARC-8-to-UTF-8 transcoding runs "
                "by default so this run's output would not be all UTF-8 -- "
                "install it (pip install -r requirements.txt) or pass "
                "--no-transcode-marc8 if you explicitly want non-UTF-8 "
                "records left as-is",
                file=sys.stderr,
            )
            return 1

    start_time = time.perf_counter()

    out_path = args.out or _default_output_path(args.input)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    required_a_tags = (
        load_tag_list(args.required_a_tags_file)
        if args.strip_missing_required_a
        else set()
    )
    non_repeatable_tags = (
        load_tag_list(args.non_repeatable_tags_file)
        if args.strip_duplicate_non_repeatable_fields
        else set()
    )
    ensure_specs = [parse_ensure_field_spec(s) for s in args.ensure_field]

    overrides = _load_overrides(args.overrides) if args.overrides else {}

    # repair_record's overrides param expects int -> (indicators, subfields) tuples
    normalized_overrides = {
        rec_idx: {fi: spec for fi, spec in fields.items()}
        for rec_idx, fields in overrides.items()
    }

    encoding_used = detect_encoding(args.input)
    mrk_path = None
    if args.mrk is not None:
        mrk_path = args.mrk or os.path.splitext(out_path)[0] + ".mrk"

    # Invalid-tag fixing (see fix_invalid_tags) needs to know every tag
    # used anywhere in the file before it can safely pick a 9XX
    # replacement that won't collide with a real field elsewhere -- but
    # that's only knowable once the whole file's been read, and this
    # tool otherwise makes exactly one streaming pass over the input. So
    # any record found with an invalid tag during the main pass below is
    # written out UNCHANGED for now and remembered here (by its exact
    # byte offset + length in the output, both fixed once written -- a
    # tag rename never changes a record's total length); `used_tags`
    # accumulates every valid tag seen along the way. Only if this ends
    # up non-empty does a second, targeted pass run afterward -- seeking
    # directly to just those few byte ranges and overwriting them in
    # place, not re-reading the whole file again.
    used_tags: set[str] = set()
    pending_tag_fixes: list[tuple[int, int, int, str]] = []

    # One pass over the input: each record is repaired, patched, and
    # written out immediately, then discarded -- memory use stays bounded
    # by the streaming buffer (see iter_repair_stream) plus one record's
    # worth of parsed data at a time, never by the total number of
    # records. Log lines are the exception: they're buffered in memory for
    # the whole run so they can be reordered before writing (not-fixable
    # first, fixed second, each clearly labeled -- see below), but even a
    # pathological run where every single record has something to log is
    # nowhere near the size of holding every record itself would be.
    n_total = 0
    n_unresolved = 0
    # Only accumulated until the one-time early estimate fires (see
    # ProgressReporter.maybe_print_estimate) -- cheap to compute (just
    # len() on text already in hand) but no reason to keep paying it for
    # the rest of a long run once it's no longer needed.
    bytes_consumed_for_estimate = 0
    log_entries: list[LogEntry] = []
    # (record_idx, identifier, declared_length) for every record that has a
    # usable identifier -- used after the full pass to find identifiers
    # reused across more than one record (see find_duplicate_identifiers).
    # Tiny compared to holding the records themselves: two short strings
    # per record, not the parsed record data.
    id_records: list[tuple[int, str, str]] = []
    progress = ProgressReporter(total_bytes=os.path.getsize(args.input))

    def log(category: str, fixed: bool, record_idx: int, rec_id: str, detail: str) -> None:
        log_entries.append(LogEntry(category, fixed, ts, record_idx, rec_id, detail))

    with open(out_path, "wb") as out_fh, \
            (open(mrk_path, "w", encoding="utf-8") if mrk_path else _null_writer()) as mrk_fh:
        record_stream = iter_repair_stream(
            args.input,
            normalized_overrides,
            on_progress=progress.on_progress,
            fix_bad_indicators=args.fix_bad_indicators,
        )
        for i, (parsed, rec_text) in enumerate(record_stream):
            n_total += 1
            if not progress.estimate_shown:
                bytes_consumed_for_estimate += len(rec_text.encode(encoding_used))
                progress.maybe_print_estimate(n_total, bytes_consumed_for_estimate)
            progress.maybe_print(n_total)
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            if parsed.unresolved:
                reason = parsed.unresolved[0][3]
                print(f"record {i}: UNRESOLVED ({reason}) -- passing through unchanged",
                      file=sys.stderr)
                log("unresolved_record", False, i, "", f"passed through unchanged: {reason}")
                out_fh.write(rec_text.encode(encoding_used))
                if mrk_fh:
                    mrk_fh.write("=UNRESOLVED  (passed through unchanged)\n\n")
                n_unresolved += 1
            else:
                for tag, spaces_added in parsed.indicator_fixes:
                    rec_id = record_identifier(parsed)
                    log(
                        "padded_indicators", True, i, rec_id,
                        f"padded {spaces_added} space(s) into short indicators on ={tag}",
                    )
                for category, detail in find_suspect_marc8_escapes(parsed):
                    rec_id = record_identifier(parsed)
                    log(category, False, i, rec_id, detail)
                if args.transcode_marc8:
                    try:
                        transcoded = transcode_marc8_to_utf8(parsed)
                    except (UnicodeDecodeError, RuntimeError) as exc:
                        # transcode_marc8_to_utf8 leaves `parsed`
                        # untouched on failure (see its own docstring),
                        # so it's safe to just skip this fix and let
                        # every other one continue -- important now
                        # that this runs by default, so one genuinely
                        # malformed MARC-8 field can't crash the whole
                        # run over records that never asked for this.
                        rec_id = record_identifier(parsed)
                        log(
                            "transcode_marc8_failed", False, i, rec_id,
                            f"could not transcode MARC-8 -> UTF-8: {exc}",
                        )
                    else:
                        if transcoded and args.log_transcoded_marc8:
                            rec_id = record_identifier(parsed)
                            log("transcoded_marc8", True, i, rec_id, "transcoded MARC-8 -> UTF-8")
                if args.fix_mojibake:
                    rec_id = record_identifier(parsed)
                    for detail in find_and_fix_mojibake(parsed):
                        log("fixed_mojibake", True, i, rec_id, detail)
                if args.fix_invalid_leader_bytes:
                    rec_id = record_identifier(parsed)
                    for detail in fix_invalid_leader_bytes(parsed):
                        log("leader_byte_defaulted", True, i, rec_id, detail)
                if args.remap_999_to_945:
                    details = remap_999_to_945(parsed)
                    if args.log_999_to_945:
                        rec_id = record_identifier(parsed)
                        for detail in details:
                            log("remapped_999_to_945", True, i, rec_id, detail)
                if args.normalize_subfield_9:
                    rec_id = record_identifier(parsed)
                    details = normalize_subfield_9_to_0(parsed)
                    if args.log_normalized_subfield_9_to_0:
                        for detail in details:
                            log("normalized_subfield_9_to_0", True, i, rec_id, detail)
                if args.normalize_smart_characters:
                    rec_id = record_identifier(parsed)
                    details = normalize_smart_characters(parsed)
                    if args.log_normalized_smart_characters:
                        for detail in details:
                            log("normalized_smart_characters", True, i, rec_id, detail)
                if args.strip_invalid_subfield_codes:
                    rec_id = record_identifier(parsed)
                    for detail in strip_invalid_subfield_codes(parsed):
                        log("removed_invalid_subfield", True, i, rec_id, detail)
                if args.strip_missing_required_a:
                    rec_id = record_identifier(parsed)
                    for detail in strip_missing_required_a(parsed, required_a_tags):
                        log("field_removed_because_missing_a", True, i, rec_id, detail)
                if args.strip_empty_fields:
                    strip_empty_fields(parsed)
                if args.strip_duplicate_non_repeatable_fields:
                    rec_id = record_identifier(parsed)
                    for detail in strip_duplicate_non_repeatable_fields(
                        parsed, non_repeatable_tags
                    ):
                        log("removed_non_repeatable_duplicate", True, i, rec_id, detail)
                for tag, indicators, subfields in ensure_specs:
                    if ensure_field(parsed, tag, indicators, subfields):
                        rec_id = record_identifier(parsed)
                        log("added_field", True, i, rec_id, f"added missing {tag} field")
                if args.add_default_245:
                    rec_id = record_identifier(parsed)
                    for detail in add_default_245(parsed):
                        log("added_default_245", True, i, rec_id, detail)
                if args.add_default_008:
                    rec_id = record_identifier(parsed)
                    for detail in add_default_008(parsed):
                        log("added_default_008", True, i, rec_id, detail)
                if args.fix_008_length:
                    rec_id = record_identifier(parsed)
                    for detail in fix_008_length(parsed):
                        log("fixed_008_length", True, i, rec_id, detail)
                extra_detect_only_findings = []
                if args.check_dangling_880_links:
                    extra_detect_only_findings += find_dangling_880_links(parsed)
                if args.check_isbn_issn_checksum:
                    extra_detect_only_findings += find_invalid_isbn_issn_checksums(parsed)
                for category, detail in (
                    find_invalid_indicator_values(parsed)
                    + find_invalid_bibliographic_level(parsed)
                    + extra_detect_only_findings
                ):
                    rec_id = record_identifier(parsed)
                    log(category, False, i, rec_id, detail)
                for category, detail in find_suspicious_fields(parsed):
                    # non_numeric_tag is suppressed here when
                    # --fix-invalid-tags is on (the default): it's
                    # deferred and fixed (or, in the rare case no 9XX
                    # slot is free, flagged for real) after the main
                    # pass -- see pending_tag_fixes below.
                    if category == "non_numeric_tag" and args.fix_invalid_tags:
                        continue
                    rec_id = record_identifier(parsed)
                    log(category, False, i, rec_id, detail)
                if parsed.leader[20:24] != "4500" and args.log_leader_entry_map_fixed:
                    rec_id = record_identifier(parsed)
                    log(
                        "leader_entry_map_fixed", True, i, rec_id,
                        f"leader bytes 20-23 were {parsed.leader[20:24]!r}, always "
                        "a fixed constant in MARC21; corrected to '4500'",
                    )
                try:
                    assembled = assemble_marc(parsed)
                except RepairError as exc:
                    # e.g. a single field or the base address too large for
                    # ISO 2709's fixed-width fields to represent at all --
                    # can't write a valid leader/directory for it, so fall
                    # back to the original bytes, same as an UNRESOLVED
                    # record. (Total record length alone has a documented
                    # sentinel and doesn't hit this -- see assemble_marc.)
                    print(f"record {i}: {exc} -- passing through unchanged",
                          file=sys.stderr)
                    log("oversized_unfixable", False, i, "", f"passed through unchanged: {exc}")
                    out_fh.write(rec_text.encode(encoding_used))
                    if mrk_fh:
                        mrk_fh.write("=UNRESOLVED  (passed through unchanged)\n\n")
                    n_unresolved += 1
                    rec_id = record_identifier(parsed)
                    if rec_id:
                        id_records.append((i, rec_id, rec_text[:5]))
                else:
                    if len(assembled) > 99999:
                        rec_id = record_identifier(parsed)
                        log(
                            "oversized_sentinel_fixed", True, i, rec_id,
                            f"record is {len(assembled)} bytes; leader declares the "
                            "MARC21 sentinel 99999 instead (real end is still found "
                            "from the record terminator, nothing lost)",
                        )
                    offset = out_fh.tell()
                    out_fh.write(assembled)
                    if mrk_fh:
                        mrk_fh.write(to_mrk(parsed))
                        mrk_fh.write("\n")
                    rec_id = record_identifier(parsed)
                    if rec_id:
                        id_records.append((i, rec_id, assembled[:5].decode("ascii")))
                    if args.fix_invalid_tags:
                        has_invalid_tag = False
                        for f in parsed.fields:
                            if f.tag.isdigit():
                                used_tags.add(f.tag)
                            else:
                                has_invalid_tag = True
                        if has_invalid_tag:
                            rec_encoding = "utf-8" if parsed.leader[9:10] == "a" else "latin-1"
                            pending_tag_fixes.append((i, offset, len(assembled), rec_encoding))

    progress.finish()

    if pending_tag_fixes:
        # Now that the whole file's been read, we finally know every tag
        # actually in use -- pick the replacement and go fix (or, if
        # every 9XX slot is somehow already taken, flag) just these few
        # records, by seeking directly to their known byte offsets
        # rather than re-reading the whole output file.
        replacement_tag = pick_unused_9xx_tag(used_tags)
        fix_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(out_path, "r+b") as fixup_fh:
            for record_idx, offset, length, rec_encoding in pending_tag_fixes:
                fixup_fh.seek(offset)
                raw = fixup_fh.read(length)
                rec = read_intact_record(raw.decode(rec_encoding))
                rec_id = record_identifier(rec)
                if replacement_tag is None:
                    for f in rec.fields:
                        if not f.tag.isdigit():
                            log_entries.append(LogEntry(
                                "non_numeric_tag", False, fix_ts, record_idx, rec_id,
                                f"tag {f.tag!r} is not 3 numeric digits (could not fix: "
                                "every 900-999 tag is already used elsewhere in this file)",
                            ))
                    continue
                for detail in fix_invalid_tags(rec, replacement_tag):
                    log_entries.append(
                        LogEntry("invalid_tag", True, fix_ts, record_idx, rec_id, detail)
                    )
                fixed_bytes = assemble_marc(rec)
                assert len(fixed_bytes) == length, (
                    "tag rename must not change a record's total byte length"
                )
                fixup_fh.seek(offset)
                fixup_fh.write(fixed_bytes)

    dup_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_entries.extend(find_duplicate_identifiers(id_records, dup_ts))

    if not args.log_informational:
        # --no-log-informational: the underlying fixes/detections still
        # ran and affected the output regardless -- this only trims
        # what gets written to the log file, typically the
        # highest-volume section (e.g. every MARC-8 record transcoded).
        log_entries = [e for e in log_entries if _section_for(e)[1] != "INFORMATIONAL"]

    n_log_lines = len(log_entries)
    if n_log_lines:
        # Not-fixable/not-fixed-in-this-run issues first (still need your
        # attention in the output), fixed ones last; within each, grouped
        # by category with a header and count, so e.g. all 375 missing-008
        # findings sit together instead of scattered by record order.
        log_path = (
            _timestamped_log_path(args.log, run_ts) if args.log
            else os.path.splitext(out_path)[0] + f"_log_{run_ts}.log"
        )
        write_log(log_path, log_entries)
        # Based on which section an entry actually lands in, not the
        # raw `fixed` flag -- INFORMATIONAL can now include detect-only
        # findings (e.g. invalid_indicator_value) logged with
        # fixed=False for correct section placement, which would
        # otherwise inflate this "not fixed" count.
        n_not_fixed = sum(1 for e in log_entries if _section_for(e)[1] == "NOT FIXED")
        n_fixed = n_log_lines - n_not_fixed
        print(
            f"{n_log_lines} log line(s) written to {log_path} "
            f"({n_not_fixed} not fixed, {n_fixed} fixed)",
            file=sys.stderr,
        )

    elapsed = time.perf_counter() - start_time
    print(
        f"Wrote {n_total}/{n_total} record(s) to {out_path} "
        f"({n_total - n_unresolved} corrected/passed clean, "
        f"{n_unresolved} passed through unchanged) in {elapsed:.2f}s"
    )
    if n_unresolved:
        print(
            f"{n_unresolved} record(s) could not be auto-repaired and were kept "
            "unchanged in the output (see stderr above) -- supply overrides via "
            "--overrides and re-run to fix them too.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
