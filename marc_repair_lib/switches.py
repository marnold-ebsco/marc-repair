"""The on/off switch registry: one toggle per repair/check, and the groups of them.

Every repair or check that can sensibly be turned off is one `Switch`: ONE
setting (one argparse dest, one profile key) with two spellings,
`--NAME` and `--NO-NAME`, and a default. Old opt-out spellings stay as
aliases, so existing command lines and profiles keep working.

Naming grammar -- `--<verb>-<what-it-touches>`; verbs: fix, remove, add,
merge, move, recode, normalize, split, check (detect and report only).

A `--group-NAME` / `--no-group-NAME` switch sets every member of a group at
once. Precedence: built-in default < profile < group < an individually
given switch, whatever order they appear on the command line.

Deliberately NOT switchable (turning them off yields unreadable or
unimportable output): the Mode 1/2 structural repair, leader entry-map
correction, invalid-subfield-code removal, `unfixable` diversion and its
fallbacks, and the categories owned by --ensure-field / --guarantee-importable
/ --source-system.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

#: Namespace attribute holding the dests given individually on the command line.
EXPLICIT_ATTR = "_explicit_toggles"


@dataclass(frozen=True)
class Switch:
    dest: str
    name: str           # positive spelling, without the leading dashes
    default: bool
    scope: str          # "bib", "holdings" or "both"
    categories: tuple[str, ...] = ()   # log categories this switch produces
    aliases: tuple[str, ...] = ()      # legacy option strings, still accepted
    help: str = ""      # only used when the parser has no older definition


def _s(dest, name, default, scope, cats="", aliases="", help=""):
    return Switch(dest, name, default, scope, tuple(cats.split()), tuple(aliases.split()), help)


SWITCHES: tuple[Switch, ...] = (
    # ---- both pipelines -------------------------------------------------
    _s("fix_bad_indicators", "fix-bad-indicators", True, "both", "padded_indicators"),
    _s("fix_mojibake", "fix-mojibake", True, "both", "fixed_mojibake"),
    _s("fix_invalid_leader_bytes", "fix-invalid-leader-bytes", True, "both",
       "leader_byte_defaulted holdings_leader_byte_defaulted"),
    _s("normalize_subfield_9", "normalize-subfield-9", True, "both", "normalized_subfield_9_to_0"),
    _s("normalize_smart_characters", "normalize-smart-characters", True, "both",
       "normalized_smart_characters"),
    _s("fix_misplaced_subfield_codes", "fix-misplaced-subfield-codes", True, "both",
       "fixed_misplaced_subfield_code"),
    _s("strip_empty_fields", "remove-empty-fields", True, "both", "",
       "--no-strip-empty-fields"),
    _s("remove_null_identifiers", "remove-null-identifiers", True, "both",
       "removed_null_identifier",
       help="remove a subfield that has no data at all (e.g. a bare $8, or an empty $a "
       "immediately followed by another subfield)"),
    _s("strip_missing_required_a", "remove-fields-missing-required-a", True, "both",
       "field_removed_because_missing_a", "--no-strip-missing-required-a"),
    _s("fix_invalid_tags", "fix-invalid-tags", True, "both", "invalid_tag"),
    _s("fix_008_length", "fix-008-length", True, "both",
       "fixed_008_length fixed_holdings_008_length"),
    _s("add_default_008", "add-default-008", True, "both",
       "added_default_008 added_default_holdings_008",
       help="add a blank placeholder 008 to a record that has none"),
    _s("collapse_oversized_field_escapes", "collapse-oversized-field-escapes", True, "both",
       "collapsed_oversized_field_escapes",
       help="shrink a field over 9999 bytes by merging redundant MARC-8 escapes. Off: the "
       "field is truncated instead (if --truncate-oversized-field is on)"),
    _s("truncate_oversized_field", "truncate-oversized-field", True, "both",
       "truncated_oversized_field",
       help="truncate a field still over 9999 bytes at a subfield boundary. Off: such a "
       "record cannot be written and is diverted to the _error file"),
    _s("trim_oversized_record", "trim-oversized-record", True, "both", "trimmed_oversized_record"),
    _s("check_duplicate_identifiers", "check-duplicate-identifiers", True, "both",
       "duplicate_identifier",
       help="flag records that share an identifier within the file"),
    # ---- bib only -------------------------------------------------------
    _s("transcode_marc8", "transcode-marc8", True, "bib",
       "transcoded_marc8 transcode_marc8_failed removed_untranscodable_subfield"),
    _s("fix_marc8_diacritic_escapes", "fix-marc8-diacritic-escapes", True, "bib",
       "fixed_marc8_diacritic"),
    _s("marc8_corpus_lookup", "marc8-corpus-lookup", True, "bib"),
    _s("remap_999_to_945", "remap-999-to-945", False, "bib", "remapped_999_to_945"),
    _s("merge_duplicate_040", "merge-duplicate-040", True, "bib", "merged_duplicate_040"),
    _s("move_duplicate_100_to_700", "move-duplicate-100-to-700", True, "bib",
       "moved_duplicate_to_added_entry"),
    _s("move_duplicate_240_to_730", "move-duplicate-240-to-730", True, "bib",
       "moved_duplicate_to_added_entry"),
    _s("move_duplicate_245_to_246", "move-duplicate-245-to-246", True, "bib",
       "moved_duplicate_to_added_entry"),
    _s("recode_misfiled_duplicate_245", "recode-misfiled-duplicate-245", True, "bib",
       "moved_duplicate_to_added_entry"),
    _s("recode_title_only_7xx", "recode-title-only-7xx", True, "bib",
       "recoded_title_only_7xx_to_730"),
    _s("recode_6xx_b_to_a", "recode-6xx-b-to-a", True, "bib", "recoded_6xx_b_to_a"),
    _s("strip_duplicate_non_repeatable_fields", "remove-duplicate-non-repeatable-fields", True,
       "bib", "removed_non_repeatable_duplicate", "--no-strip-duplicate-non-repeatable-fields"),
    _s("remove_010_whitespace_a", "remove-010-whitespace-a", True, "bib",
       "removed_010_whitespace_a",
       help="remove an 010 $a made up entirely of whitespace"),
    _s("merge_duplicate_010", "merge-duplicate-010", True, "bib", "deduped_010_lccn",
       help="merge a record's several 010s (or several $a in one 010) into exactly one"),
    _s("remove_880_missing_a", "remove-880-missing-a", True, "bib", "removed_880_missing_a",
       help="remove an 880 whose $a is missing or effectively empty"),
    _s("add_default_245", "add-default-245", True, "bib", "added_default_245"),
    _s("fix_856_missing_u", "fix-856-missing-u", True, "bib", "bib_856_missing_u",
       help="recode an unambiguous 856 $a/$z URL to $u, and flag the rest (an 856 with no $u)"),
    _s("reattach_orphaned_fields", "reattach-orphaned-fields", True, "bib",
       "reattached_orphaned_field"),
    _s("fix_invalid_bibliographic_level", "fix-invalid-bibliographic-level", True, "bib",
       "invalid_bibliographic_level",
       help="default an invalid bibliographic level (leader byte 07) to 'm'"),
    _s("check_isbn_issn_checksum", "check-isbn-issn-checksum", False, "bib",
       "invalid_isbn_issn_checksum"),
    _s("check_dangling_880_links", "check-dangling-880-links", False, "bib", "dangling_880_link"),
    _s("check_non_standard_fields", "check-non-standard-fields", True, "bib",
       "non_standard_marc_fields"),
    _s("check_suspect_marc8_escape", "check-suspect-marc8-escape", True, "bib",
       "suspect_marc8_escape",
       help="flag a suspect (probably miskeyed) MARC-8 escape sequence"),
    _s("check_doubled_proxy_url", "check-doubled-proxy-url", True, "bib", "doubled_proxy_url",
       help="flag a URL with a doubled proxy prefix"),
    _s("check_suspect_hex_encoded_marc8", "check-suspect-hex-encoded-marc8", True, "bib",
       "suspect_hex_encoded_marc8",
       help="flag runs of {xxxxxx} hex-encoded MARC-8 text"),
    _s("check_invalid_indicator_values", "check-invalid-indicator-values", True, "bib",
       "invalid_indicator_value",
       help="flag an indicator that is present but is not a digit or blank"),
    _s("allow_oversized_sentinel", "allow-oversized-sentinel", False, "bib"),
    # ---- holdings only --------------------------------------------------
    _s("recode_852_b_to_i", "recode-852-b-to-i", True, "holdings", "recoded_852_b_to_i",
       help="recode the second 852 $b (after $h, no $i yet) as $i"),
    _s("remove_extra_852_b", "remove-extra-852-b", True, "holdings", "removed_extra_852_b",
       help="remove the extra 852 $b when an 852 has more than one"),
    _s("remove_duplicate_852_nr_subfields", "remove-duplicate-852-nr-subfields", True,
       "holdings", "holdings_852_duplicate_nr_subfield",
       help="remove repeats of an 852 subfield the MARC spec defines as Not Repeatable"),
    _s("fix_852_b_suspect_content", "fix-852-b-suspect-content", True, "holdings",
       "holdings_852_b_suspect_content",
       help="replace a purely numeric (or '#'-flattened) 852 $b with a placeholder"),
    _s("remove_bad_call_number", "remove-bad-call-number", True, "holdings",
       "removed_bad_call_number",
       help="remove an 852 $h that is empty or punctuation-only"),
    _s("check_missing_call_number", "check-missing-call-number", True, "holdings",
       "missing_call_number", help="flag an 852 with no $h"),
    _s("add_missing_852_location", "add-missing-852-location", True, "holdings",
       "added_missing_852_location",
       help="add a placeholder location to an 852 with no usable $a, $b or $c"),
    _s("fix_missing_852c", "add-missing-852c", True, "holdings", "added_missing_852c",
       "--no-fix-missing-852c"),
    _s("remove_empty_852_subfields", "remove-empty-852-subfields", True, "holdings",
       "removed_empty_852_subfield",
       help="remove an 852 subfield with no data at all"),
    _s("split_multiple_852", "split-multiple-852", True, "holdings",
       "split_holdings_multiple_852 incomplete_852",
       help="split a record with several 852s into one record per usable 852 (an 852 with "
       "no $b can't be split out and is dropped, logged as incomplete_852)"),
    _s("split_multiple_004", "split-multiple-004", True, "holdings", "holdings_multiple_004",
       help="write each additional 004 as its own record in the _additional004s file"),
    _s("check_853_missing_8", "check-853-missing-8", True, "holdings", "holdings_853_missing_8",
       help="flag an 853 with no $8"),
    _s("check_holdings_856_missing_u", "check-holdings-856-missing-u", True, "holdings",
       "holdings_856_missing_u", help="flag an 856 with no $u"),
    _s("check_holdings_escape_sequences", "check-holdings-escape-sequences", True, "holdings",
       "holdings_escape_sequence", help="flag a record containing an ESC (0x1B) byte"),
    _s("check_holdings_missing_004", "check-holdings-missing-004", True, "holdings",
       "holdings_missing_004", help="flag a holdings record with no 004"),
)

BY_DEST = {sw.dest: sw for sw in SWITCHES}

_CHECKS = tuple(sw.dest for sw in SWITCHES if sw.name.startswith("check-"))

#: group name -> member dests
GROUPS: dict[str, tuple[str, ...]] = {
    "duplicate-fields": (
        "merge_duplicate_040", "move_duplicate_100_to_700", "move_duplicate_240_to_730",
        "move_duplicate_245_to_246", "recode_misfiled_duplicate_245",
        "strip_duplicate_non_repeatable_fields", "merge_duplicate_010",
    ),
    "marc8-encoding": (
        "transcode_marc8", "fix_marc8_diacritic_escapes", "marc8_corpus_lookup", "fix_mojibake",
    ),
    "normalization": (
        "normalize_smart_characters", "normalize_subfield_9", "fix_bad_indicators",
        "fix_008_length", "fix_invalid_leader_bytes",
    ),
    "structural-salvage": (
        "reattach_orphaned_fields", "fix_invalid_tags", "fix_misplaced_subfield_codes",
        "strip_empty_fields", "remove_null_identifiers",
    ),
    "placeholders": (
        "add_default_245", "add_default_008", "add_missing_852_location", "fix_missing_852c",
    ),
    "heading-cleanup": (
        "recode_title_only_7xx", "recode_6xx_b_to_a", "strip_missing_required_a",
        "remove_880_missing_a",
    ),
    "oversized": (
        "collapse_oversized_field_escapes", "truncate_oversized_field", "trim_oversized_record",
    ),
    "holdings-852": (
        "recode_852_b_to_i", "remove_extra_852_b", "remove_duplicate_852_nr_subfields",
        "fix_852_b_suspect_content", "remove_bad_call_number", "remove_empty_852_subfields",
    ),
    "holdings-record-splitting": ("split_multiple_852", "split_multiple_004"),
    "detect-only": _CHECKS,
}


def group_dest(name: str) -> str:
    return "group_" + name.replace("-", "_")


GROUP_DESTS = frozenset(group_dest(g) for g in GROUPS)


def option_strings(sw: Switch) -> list[str]:
    return [f"--{sw.name}", f"--no-{sw.name}", *sw.aliases]


def _mark_explicit(namespace: argparse.Namespace, dest: str) -> None:
    explicit = getattr(namespace, EXPLICIT_ATTR, None)
    if explicit is None:
        explicit = set()
        setattr(namespace, EXPLICIT_ATTR, explicit)
    explicit.add(dest)


class ToggleAction(argparse.Action):
    """`--X` sets True, any `--no-X` spelling (including legacy aliases) sets False."""

    def __init__(self, option_strings, dest, default=None, required=False, help=None):
        super().__init__(option_strings, dest, nargs=0, default=default,
                         required=required, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, not option_string.startswith("--no-"))
        _mark_explicit(namespace, self.dest)


class GroupAction(argparse.Action):
    """`--group-X` / `--no-group-X`: set every member that wasn't given individually."""

    def __init__(self, option_strings, dest, members=(), default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)
        self.members = tuple(members)

    def __call__(self, parser, namespace, values, option_string=None):
        value = not option_string.startswith("--no-")
        explicit = getattr(namespace, EXPLICIT_ATTR, None) or set()
        for dest in self.members:
            if dest not in explicit:
                setattr(namespace, dest, value)


def add_switch_arguments(parser: argparse.ArgumentParser) -> None:
    """Make every registered switch a ToggleAction (converting an older store_true /
    store_false / BooleanOptionalAction definition in place, keeping its help text and
    position), and add the group switches."""
    existing = {a.dest: a for a in parser._actions if a.option_strings}
    for sw in SWITCHES:
        strings = option_strings(sw)
        action = existing.get(sw.dest)
        if action is None:
            parser.add_argument(
                *strings, dest=sw.dest, action=ToggleAction, default=sw.default,
                help=f"({sw.scope}; default {'on' if sw.default else 'off'}) {sw.help}".strip(),
            )
            continue
        for old in action.option_strings:
            parser._option_string_actions.pop(old, None)
        action.__class__ = ToggleAction
        action.option_strings = strings
        action.nargs = 0
        action.default = sw.default
        for s in strings:
            parser._option_string_actions[s] = action
    for name, members in GROUPS.items():
        names = ", ".join(BY_DEST[d].name for d in members)
        parser.add_argument(
            f"--group-{name}", f"--no-group-{name}", dest=group_dest(name),
            action=GroupAction, members=members,
            help=f"turn on / off every one of: {names} (a switch given individually wins)",
        )


def switch_values(args: argparse.Namespace) -> dict[str, bool]:
    """dest -> bool for every registered switch, from the final parsed arguments."""
    return {sw.dest: bool(getattr(args, sw.dest, sw.default)) for sw in SWITCHES}


def inactive_categories(values: dict[str, bool], pipeline: str) -> set[str]:
    """Log categories whose every producing switch (in this pipeline) is off -- these get
    no log header, since the check never ran."""
    produced: dict[str, list[bool]] = {}
    for sw in SWITCHES:
        if sw.scope in ("both", pipeline):
            for cat in sw.categories:
                produced.setdefault(cat, []).append(values.get(sw.dest, sw.default))
    return {cat for cat, states in produced.items() if not any(states)}
