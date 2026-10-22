"""Sanity checks for standard_marc_fields.txt (the accepted-tag list)."""
import re
from pathlib import Path

LIST = Path(__file__).resolve().parent.parent / "standard_marc_fields.txt"


def load():
    sections = {"standard": {}, "obsolete": {}}
    cur = None
    for line in LIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("[standard]", "[obsolete]"):
            cur = line[1:-1]
            continue
        tag, _, title = line.partition("|")
        sections[cur][tag.strip()] = title.strip()
    return sections["standard"], sections["obsolete"]


def test_tags_well_formed_and_disjoint():
    std, obs = load()
    for tag in [*std, *obs]:
        assert re.fullmatch(r"\d{3}", tag), tag
    assert not set(std) & set(obs)


def test_known_standard_fields():
    std, _ = load()
    for tag in ("001", "005", "008", "245", "880", "886", "590", "690"):
        assert tag in std, tag


def test_unlisted_and_other_local_fields_not_standard():
    std, obs = load()
    for tag in ("002", "004", "591", "598", "691", "900"):
        assert tag not in std and tag not in obs, tag


def test_obsolete_fields():
    std, obs = load()
    for tag in ("009", "011", "241", "301"):
        assert tag in obs and tag not in std, tag


def test_local_fields_standard_not_obsolete():
    std, obs = load()
    for tag in ("019", "029", "049", "069", "090", "092", "096", "099",
                "265", "440", "590", "599", "690", "799"):
        assert tag in std and tag not in obs, tag


def test_no_leftover_markers_in_titles():
    std, obs = load()
    for title in [*std.values(), *obs.values()]:
        assert not re.search(r"\[[A-Z-]+\]|\((?:NR|R)\)", title), title
