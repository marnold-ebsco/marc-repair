# marc_repair.py -- repairs, defaults, and log categories

Two separate pipelines: **bib** (default `python marc_repair.py INPUT.mrc`)
and **holdings** (`--split-bib-holdings` / `--repair-holdings`).

"Logged by default" accounts for two independent gates: (1) whether the fix
itself runs by default, and (2) whether its section is shown by default --
the bib pipeline hides the whole INFORMATIONAL section unless
`--log-informational` is passed. **The holdings pipeline has no such gate at
all -- every holdings INFORMATIONAL entry is always written.**

## Bib pipeline (`main()`, no split/holdings flags)

| Repair / check | Action taken | On by default? | Category | Section | Logged by default? |
|---|---|---|---|---|---|
| Mode 1/2 structural repair (bad length/directory) | Rebuilt record length/directory | Yes | -- (silent, just works) | -- | -- |
| Short-indicator padding | Padded indicators to 2 chars | Yes (`--no-fix-bad-indicators` to disable) | `padded_indicators` | INFORMATIONAL | No |
| MARC-8 -> UTF-8 transcoding | Transcoded field data | Yes (`--no-transcode-marc8`) | `transcoded_marc8` | INFORMATIONAL | No (needs `--log-transcoded-marc8` *and* `--log-informational`) |
| Transcode failure | Left field untranscoded | n/a (only if it occurs) | `transcode_marc8_failed` | NOT FIXED | **Yes** |
| Mojibake (double-encoded UTF-8) fix | Re-decoded field data | Yes (`--no-fix-mojibake`) | `fixed_mojibake` | INFORMATIONAL | No |
| Leader bytes 05/06/08/17 defaulted | Defaulted leader byte | Yes (`--no-fix-invalid-leader-bytes`) | `leader_byte_defaulted` | INFORMATIONAL | No |
| 999 -> 945 remap | Renamed field tag | **No** (`--remap-999-to-945` to enable) | `remapped_999_to_945` | INFORMATIONAL | No (needs `--log-999-to-945` too) |
| $9 -> $0 normalization | Renamed subfield code | Yes (`--no-normalize-subfield-9`) | `normalized_subfield_9_to_0` | INFORMATIONAL | No |
| Smart-character normalization | Replaced characters | Yes (`--no-normalize-smart-characters`) | `normalized_smart_characters` | INFORMATIONAL | No |
| Invalid subfield code removal | Removed subfield | Yes (`--no-strip-invalid-subfield-codes`) | `removed_invalid_subfield` | FIXED/REQUIRES ATTENTION | **Yes** |
| Missing-required-$a field removal | Removed field | Yes (`--no-strip-missing-required-a`) | `removed_missing_a` | FIXED/REQUIRES ATTENTION | **Yes** |
| Empty-field removal | Removed field | Yes (`--no-strip-empty-fields`) | -- (unconditional, unlogged either way) | -- | -- |
| Duplicate non-repeatable field removal | Removed field | Yes (`--no-strip-duplicate-non-repeatable-fields`) | `removed_non_repeatable_duplicate` | FIXED/REQUIRES ATTENTION | **Yes** |
| `--ensure-field` insertion | Added field | n/a (only if flag given) | `added_field` | FIXED/REQUIRES ATTENTION | **Yes** |
| Placeholder 245 added | Added default field | Yes (`--no-add-default-245`) | `added_default_245` | INFORMATIONAL | No |
| Placeholder 008 added | Added default field | Yes (`--no-add-default-008`) | `added_default_008` | INFORMATIONAL | No |
| 008 length pad/truncate (40 bytes) | Padded/truncated field | Yes (`--no-fix-008-length`) | `fixed_008_length` | FIXED/REQUIRES ATTENTION | **Yes** |
| Invalid (non-numeric) tag -> 9XX rename | Renamed field tag | Yes (`--no-fix-invalid-tags`) | `invalid_tag` | INFORMATIONAL | No |
| Invalid tag, no 9XX slot free | Left tag unchanged | (fallback of above) | `non_numeric_tag` | NOT FIXED | **Yes** |
| Leader entry-map (bytes 20-23) correction | Corrected leader bytes | Yes, always | `leader_entry_map_fixed` | INFORMATIONAL | No (needs `--log-leader-entry-map-fixed` too) |
| Oversized record (>99999 bytes) sentinel | Wrote sentinel length | n/a (only if it occurs) | `oversized_sentinel_fixed` | INFORMATIONAL | No |
| Unfixable oversized field/base address | Left record unchanged | n/a (only if it occurs) | `oversized_unfixable` | NOT FIXED | **Yes** |
| Unresolvable record (passed through unchanged) | Passed through unchanged | n/a (only if it occurs) | `unresolved_record` | NOT FIXED | **Yes** |
| Suspect MARC-8 escape (miskeyed diacritic) | Flagged only, no change | detect-only | `suspect_marc8_escape` | NOT FIXED | **Yes** |
| Missing 008 | Flagged only, no change | detect-only (superseded by add_default_008 fixing it) | `missing_008` | NOT FIXED | **Yes** |
| Doubled proxy URL prefix | Flagged only, no change | detect-only | `doubled_proxy_url` | NOT FIXED | **Yes** |
| Invalid indicator *value* (present but not digit/blank) | Flagged only, no change | detect-only | `invalid_indicator_value` | INFORMATIONAL | No |
| Invalid bibliographic level (leader byte 07) | Flagged only, no change | detect-only | `invalid_bibliographic_level` | INFORMATIONAL | No |
| Dangling 880 $6 link | Flagged only, no change | detect-only | `dangling_880_link` | INFORMATIONAL | No |
| Invalid ISBN/ISSN checksum | Flagged only, no change | detect-only | `invalid_isbn_issn_checksum` | INFORMATIONAL | No |
| Duplicate identifier across records | Flagged only, no change | detect-only | `duplicate_identifier` | DUPLICATE RECORDS | **Yes** |

## Holdings pipeline (`repair_holdings_records`, via `--split-bib-holdings` or `--repair-holdings`)

| Repair / check | Action taken | On by default? | Category | Section | Logged by default? |
|---|---|---|---|---|---|
| Mode 1/2 structural repair (bad length/directory) | Rebuilt record length/directory | Yes | -- | -- | -- |
| Short-indicator padding | Padded indicators to 2 chars | Yes (always, hardcoded) | `padded_indicators` | INFORMATIONAL | **Yes** (no informational gate for holdings) |
| Mojibake fix | Re-decoded field data | Yes, always | `fixed_mojibake` | INFORMATIONAL | **Yes** |
| Leader bytes 05/06/17 defaulted (holdings-specific byte 6 -> `u`, byte 17 code set) | Defaulted leader byte | Yes, always | `holdings_leader_byte_defaulted` | FIXED/REQUIRES ATTENTION | **Yes** |
| $9 -> $0 normalization | Renamed subfield code | Yes, always | `normalized_subfield_9_to_0` | INFORMATIONAL | **Yes** |
| Smart-character normalization | Replaced characters | Yes, always | `normalized_smart_characters` | INFORMATIONAL | **Yes** |
| Invalid subfield code removal | Removed subfield | Yes, always | `removed_invalid_subfield` | FIXED/REQUIRES ATTENTION | **Yes** |
| Empty-field removal | Removed field | Yes, always | -- (unlogged) | -- | -- |
| Placeholder 008 added (32-byte blank) | Added default field | Yes, always | `added_default_holdings_008` | INFORMATIONAL | **Yes** |
| 008 length pad/truncate (32 bytes) | Padded/truncated field | Yes, always | `fixed_holdings_008_length` | FIXED/REQUIRES ATTENTION | **Yes** |
| 852 $c placeholder ("Migration") added | Added subfield | **No** (`--fix-missing-852c` to enable) | `added_missing_852c` | INFORMATIONAL | **Yes** (once enabled) |
| Invalid (non-numeric) tag -> 9XX rename | Renamed field tag | Yes, always | `invalid_tag` | INFORMATIONAL | **Yes** |
| Invalid tag, no 9XX slot free | Left tag unchanged | (fallback) | `non_numeric_tag` | NOT FIXED | **Yes** |
| MARC-8 escape sequence present | Flagged only, no change | detect-only (transcoding skipped) | `holdings_escape_sequence` | NOT FIXED | **Yes** |
| Null identifier (empty subfield) | Flagged only, no change | detect-only | `holdings_null_identifier` | NOT FIXED | **Yes** |
| Missing 004 (link to bib record) | Flagged only, no change | detect-only | `holdings_missing_004` | NOT FIXED | **Yes** |
| Multiple 004 fields | Flagged only, no change | detect-only | `holdings_multiple_004` | INFORMATIONAL | **Yes** |
| Unresolvable record (passed through unchanged) | Passed through unchanged | n/a (only if it occurs) | `unresolved_record` | NOT FIXED | **Yes** |
| Unfixable oversized field/base address | Left record unchanged | n/a (only if it occurs) | `oversized_unfixable` | NOT FIXED | **Yes** |

Holdings has no equivalent of bib's `strip_missing_required_a`,
`strip_duplicate_non_repeatable_fields`, `remap_999_to_945`,
`add_default_245`, `duplicate_identifier`, or the
ISBN/ISSN/880-link/indicator-value/bib-level checks -- those are
bib-specific and deliberately not applied.
