"""Split the first N raw MARC records off a file into a separate sample file.

Pure stdlib -- doesn't need pymarc, since splitting only requires the
leader's record-length field (first 5 bytes of each record), not decoding
any content. Usage:

    python3 tools/split_sample.py working/GTU_bibs_matched.mrc \
        working/GTU_bibs_sample50.mrc -n 50
"""

import argparse
import sys


def iter_raw_records(path: str):
    with open(path, "rb") as f:
        while True:
            leader = f.read(5)
            if not leader:
                return
            if len(leader) < 5:
                raise ValueError(
                    f"{path}: truncated record (partial leader at EOF)"
                )
            length = int(leader.decode("ascii"))
            rest = f.read(length - 5)
            if len(rest) < length - 5:
                raise ValueError(
                    f"{path}: truncated record (expected {length} bytes)"
                )
            yield leader + rest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Source MARC file")
    parser.add_argument("output", help="Output MARC file for the sample")
    parser.add_argument(
        "-n", type=int, default=50, help="Number of records (default: 50)"
    )
    args = parser.parse_args()

    written = 0
    with open(args.output, "wb") as out:
        for chunk in iter_raw_records(args.input):
            if written >= args.n:
                break
            out.write(chunk)
            written += 1

    print(f"Wrote {written} record(s) to {args.output}")


if __name__ == "__main__":
    sys.exit(main())
