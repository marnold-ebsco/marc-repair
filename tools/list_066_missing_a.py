"""List records in WTS_bibs_2026-10-01_repaired.mrc whose 066 field has no
subfield $a.

For each such record, writes the record's 907 $a (Sierra bib number) and the
raw contents of the offending 066 field to a tab-separated report at the
repo root.

Run:

    python3 tools/list_066_missing_a.py
"""

import pymarc

INPUT_PATH = "WTS_bibs_2026-10-01_repaired.mrc"
OUTPUT_PATH = "066_missing_subfield_a.tsv"


def field_066_contents(field: pymarc.Field) -> str:
    return " ".join(f"${sub.code}{sub.value}" for sub in field.subfields)


def main() -> None:
    rows = []
    with open(INPUT_PATH, "rb") as f:
        reader = pymarc.MARCReader(f)
        for record in reader:
            if record is None:
                continue
            for field in record.get_fields("066"):
                if field.get_subfields("a"):
                    continue
                field_907 = record["907"]
                bib_number = field_907["a"] if field_907 is not None else ""
                rows.append((bib_number, field_066_contents(field)))

    with open(OUTPUT_PATH, "w", encoding="utf-8") as out:
        out.write("907_a\t066_contents\n")
        for bib_number, contents in rows:
            out.write(f"{bib_number}\t{contents}\n")

    print(f"Wrote {len(rows)} record(s) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
