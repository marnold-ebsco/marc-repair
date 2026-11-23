"""Display formatting of Sierra bib numbers (.b + 7 digits + check digit) in reports."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from marc_repair_lib import qa  # noqa: E402
from marc_repair_lib.common import LogEntry, RecordId, display_record_id, join_004  # noqa: E402


@pytest.mark.parametrize("digits, check", [("1234567", "2"), ("1000001", "x"), ("1000297", "2")])
def test_check_digit(digits, check):
    assert qa.sierra_check_digit(digits) == check


@pytest.mark.parametrize("value, expected", [
    ("1234567", ".b12345672"),        # bare number: add .b and check digit
    ("b1234567", ".b12345672"),       # no period, no check digit
    (".b1234567", ".b12345672"),      # no check digit
    ("1000001x", ".b1000001x"),       # check digit present, .b missing
    ("b12345672", ".b12345672"),      # .b missing its period
    (".b12345672", ".b12345672"),     # already complete
    (".b12345679", ".b12345679 [check digit mismatch, expected 2]"),   # never replaced
    (".B1000001X", ".b1000001x"),
    ("12345672", ".b12345672"),       # bare 8 digits, check digit correct
    ("12345678", "12345678 [check digit mismatch, expected 2]"),  # bare 8, wrong: left as is
    ("some-other-id", "some-other-id"),
])
def test_format(value, expected):
    assert qa.format_sierra_bib_number(value) == expected


def test_only_log_display_changes_and_only_for_sierra():
    entry = LogEntry("c", True, "t", 3, join_004(RecordId("1234567"), "9876"), "d")
    try:
        qa.set_source_system("sierra")
        qa.set_sierra_bib_format(True)
        assert entry.render() == "\trecord 3 (.b12345672 / 9876)\td"
        assert entry.record_id == "1234567 / 9876"   # the stored id is untouched
        qa.set_sierra_bib_format(False)
        assert display_record_id(RecordId("1234567")) == "1234567"
        qa.set_sierra_bib_format(True)
        assert display_record_id("1234567") == "1234567"  # not from 907 $a (e.g. 001): untouched
        qa.set_source_system("wms")
        assert display_record_id(RecordId("1234567")) == "1234567"
    finally:
        qa.set_source_system(None)
        qa.set_sierra_bib_format(False)
