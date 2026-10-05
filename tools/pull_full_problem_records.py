"""Recover full MARC records for a set of stub "problem" records.

marc_repair.py sometimes has to write a bare-bones FOLIO stub in place of a
record it can't safely repair (title/author/control fields only, see e.g.
WTS_source_FOLIO.mrc). This script takes that stub file and reconstructs
the full, pre-repair version of each record by:

  1. Reading each stub's FOLIO instance UUID (field 999 $i).
  2. Looking that UUID up in a FOLIO instances-transform JSON export (one
     JSON object per line) to recover the record's Sierra bib number, held
     in administrativeNotes as "Identifier(s) from previous system: .bXXXXXXXXx".
  3. Scanning a repaired/source bib MARC file for the record whose 907 $a
     matches that Sierra bib number, and writing it byte-for-byte to the
     output file.

Run interactively -- it prompts for each file path:

    python3 tools/pull_full_problem_records.py
"""

import json
import os
import re
import sys

import pymarc

ADMIN_NOTE_RE = re.compile(
    r"Identifier\(s\) from previous system:\s*(\.?[Bb]\d+[Xx]?)"
)


def prompt_path(label: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{label}{suffix}: ").strip()
        path = answer or default
        if not path:
            print("A path is required.")
            continue
        if not os.path.isfile(path):
            print(f"No such file: {path}")
            continue
        return path


def prompt_output_path(label: str, default: str) -> str:
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def normalize_bib_number(value: str) -> str:
    """Sierra bib numbers as seen in 907 $a are lowercase with a leading
    period, e.g. ".b1072090x"; administrativeNotes spells them without the
    period and with inconsistent case. Normalize both to the same form."""
    return value.lower().lstrip(".")


def stub_uuids(stub_path: str) -> list[str]:
    uuids = []
    with open(stub_path, "rb") as f:
        reader = pymarc.MARCReader(f)
        for record in reader:
            if record is None:
                continue
            field_999 = record["999"]
            uuid = field_999["i"] if field_999 is not None else None
            if not uuid:
                print(f"  warning: record missing 999 $i, skipping: {record}")
                continue
            uuids.append(uuid)
    return uuids


def resolve_bib_numbers(json_path: str, uuids: set[str]) -> dict[str, str]:
    """Returns {uuid: normalized_bib_number} for every uuid found."""
    found = {}
    remaining = set(uuids)
    with open(json_path, "r", encoding="utf-8") as f:
        for line in f:
            if not remaining:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec_id = rec.get("id")
            if rec_id not in remaining:
                continue
            bib_number = None
            for note in rec.get("administrativeNotes", []):
                m = ADMIN_NOTE_RE.search(note)
                if m:
                    bib_number = normalize_bib_number(m.group(1))
                    break
            if bib_number:
                found[rec_id] = bib_number
                remaining.discard(rec_id)
    return found


def collect_full_records(
    bibs_path: str, wanted_bib_numbers: set[str]
) -> dict[str, bytes]:
    """Returns {normalized_bib_number: raw_record_bytes}."""
    found = {}
    remaining = set(wanted_bib_numbers)
    with open(bibs_path, "rb") as f:
        reader = pymarc.MARCReader(f)
        for record in reader:
            if not remaining:
                break
            if record is None:
                continue
            field_907 = record["907"]
            value = field_907["a"] if field_907 is not None else None
            if not value:
                continue
            bib_number = normalize_bib_number(value)
            if bib_number in remaining:
                found[bib_number] = reader.current_chunk
                remaining.discard(bib_number)
    return found


def main() -> None:
    print("Reconstruct full problem records from their Sierra bib numbers.\n")

    stub_path = prompt_path(
        "Stub/problem records file (MARC)", "WTS_source_FOLIO.mrc"
    )
    json_path = prompt_path(
        "FOLIO instances-transform JSON file", "folio_instances_transform_bibs.json"
    )
    bibs_path = prompt_path(
        "Bib MARC file to pull full records from", "WTS_bibs_2026-10-01_repaired.mrc"
    )
    default_output = os.path.splitext(os.path.basename(stub_path))[0] + "_full.mrc"
    output_path = prompt_output_path("Output file for full records", default_output)

    print(f"\nReading stub UUIDs from {stub_path} ...")
    uuids = stub_uuids(stub_path)
    print(f"  {len(uuids)} stub record(s) read")

    print(f"\nResolving Sierra bib numbers via {json_path} ...")
    bib_numbers_by_uuid = resolve_bib_numbers(json_path, set(uuids))
    unresolved_uuids = [u for u in uuids if u not in bib_numbers_by_uuid]
    print(f"  {len(bib_numbers_by_uuid)} resolved, {len(unresolved_uuids)} unresolved")
    for u in unresolved_uuids:
        print(f"    unresolved: {u}")

    wanted_bib_numbers = set(bib_numbers_by_uuid.values())

    print(f"\nScanning {bibs_path} for {len(wanted_bib_numbers)} bib number(s) ...")
    full_records = collect_full_records(bibs_path, wanted_bib_numbers)
    missing_bib_numbers = wanted_bib_numbers - full_records.keys()
    print(f"  {len(full_records)} found, {len(missing_bib_numbers)} missing")
    for b in sorted(missing_bib_numbers):
        print(f"    missing: .{b}")

    with open(output_path, "wb") as out:
        for uuid in uuids:
            bib_number = bib_numbers_by_uuid.get(uuid)
            if bib_number is None:
                continue
            chunk = full_records.get(bib_number)
            if chunk is None:
                continue
            out.write(chunk)

    written = sum(
        1
        for uuid in uuids
        if bib_numbers_by_uuid.get(uuid) in full_records
    )
    print(f"\nWrote {written} full record(s) to {output_path}")


if __name__ == "__main__":
    sys.exit(main())
