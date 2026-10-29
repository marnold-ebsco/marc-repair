"""Sanity checks for standard_marc_fields.txt (the accepted-tag list)."""
import re
from pathlib import Path

LIST = Path(__file__).resolve().parent.parent / "standard_marc_fields.txt"


def load():
    sections = {"standard": {}, "obsolete": {}, "wms": {}}
    cur = None
    for line in LIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("[standard]", "[obsolete]", "[wms]"):
            cur = line[1:-1]
            continue
        tag, _, title = line.partition("|")
        sections[cur][tag.strip()] = title.strip()
    return sections["standard"], sections["obsolete"], sections["wms"]


def test_tags_well_formed_and_disjoint():
    std, obs, wms = load()
    for tag in [*std, *obs, *wms]:
        assert re.fullmatch(r"\d{3}", tag), tag
    assert not set(std) & set(obs)
    # a [wms] tag may also be [obsolete] (851 is, in LC's list); for wms the
    # loader's standard set wins, so it is not reported as obsolete there
    assert not set(wms) & set(std)


def test_wms_only_fields():
    std, _, wms = load()
    assert set(wms) == {"539", "790", "793", "851", "891"}
    assert not set(wms) & set(std)


def test_known_standard_fields():
    std, _, _ = load()
    for tag in ("001", "005", "008", "245", "880", "886", "590", "690"):
        assert tag in std, tag


def test_unlisted_and_other_local_fields_not_standard():
    std, obs, _ = load()
    for tag in ("002", "004", "591", "598", "691", "900"):
        assert tag not in std and tag not in obs, tag


def test_obsolete_fields():
    std, obs, _ = load()
    for tag in ("009", "011", "241", "301"):
        assert tag in obs and tag not in std, tag


def test_local_fields_standard_not_obsolete():
    std, obs, _ = load()
    for tag in ("019", "029", "049", "069", "090", "092", "096", "099",
                "265", "440", "590", "599", "690", "799"):
        assert tag in std and tag not in obs, tag


def test_no_leftover_markers_in_titles():
    std, obs, _ = load()
    for title in [*std.values(), *obs.values()]:
        assert not re.search(r"\[[A-Z-]+\]|\((?:NR|R)\)", title), title
