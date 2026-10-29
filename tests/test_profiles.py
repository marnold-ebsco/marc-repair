"""Run profiles: discovery, selection, validation, precedence, and marc_repair_profile.py."""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import marc_repair as m  # noqa: E402
import marc_repair_profile as tool  # noqa: E402

FIXTURE = os.path.join(
    ROOT, "tests", "fixtures", "bad_missing245_bib_nashvillestate_bibs_202693_me.mrc"
)


def _write(directory, filename, options, name=None, description=""):
    data = {"options": options}
    if name:
        data["name"] = name
    if description:
        data["description"] = description
    path = os.path.join(str(directory), filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def _args(parser, *argv):
    return parser.parse_known_args(list(argv))[0]


@pytest.fixture
def parser():
    return m.build_parser()


# --- validation / loading -------------------------------------------------

def test_load_valid_profile(tmp_path, parser):
    path = _write(tmp_path, "a.json", {"fix_mojibake": False, "log_full": ["x"]}, "mine", "desc")
    p = m.load_profile_file(path, parser)
    assert (p["name"], p["description"]) == ("mine", "desc")
    assert p["options"] == {"fix_mojibake": False, "log_full": ["x"]}


def test_name_defaults_to_file_stem(tmp_path, parser):
    assert m.load_profile_file(_write(tmp_path, "stem.json", {}), parser)["name"] == "stem"


@pytest.mark.parametrize("options, fragment", [
    ({"fix_mojibak": False}, "unknown option"),
    ({"input": "x.mrc"}, "unknown option"),
    ({"fix_mojibake": "no"}, "must be a bool"),
    ({"sample_limit": True}, "must be a int"),
    ({"sample_limit": "5"}, "must be a int"),
    ({"log_full": "x"}, "must be a list"),
    ({"source_system": "bogus"}, "must be one of"),
])
def test_invalid_options_rejected(tmp_path, parser, options, fragment):
    with pytest.raises(m.ProfileError, match=fragment):
        m.load_profile_file(_write(tmp_path, "a.json", options), parser)


def test_bad_json_and_bad_shape_rejected(tmp_path, parser):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(m.ProfileError):
        m.load_profile_file(str(bad), parser)
    bad.write_text('{"option": {}}')
    with pytest.raises(m.ProfileError, match="options"):
        m.load_profile_file(str(bad), parser)
    bad.write_text('{"options": {}, "extra": 1}')
    with pytest.raises(m.ProfileError, match="top-level"):
        m.load_profile_file(str(bad), parser)


def test_excluded_dests_cover_only_real_options(parser):
    dests = {a.dest for a in parser._actions}
    assert m.PROFILE_EXCLUDED_DESTS <= dests


def test_example_profiles_are_valid(parser):
    found = m.discover_profiles(os.path.join(ROOT, "profiles", "examples"), parser)
    assert {p["name"] for p in found} >= {"conservative", "full", "sierra"}


# --- discovery / directory ------------------------------------------------

def test_discover_skips_invalid_with_warning(tmp_path, parser, capsys):
    _write(tmp_path, "good.json", {})
    _write(tmp_path, "bad.json", {"nope": 1})
    (tmp_path / "notes.txt").write_text("ignored")
    assert [p["name"] for p in m.discover_profiles(str(tmp_path), parser)] == ["good"]
    assert "skipping invalid profile" in capsys.readouterr().err


def test_missing_profile_dir_falls_back_with_warning(tmp_path, capsys):
    assert m.resolve_profile_dir(str(tmp_path / "nope")) == m.DEFAULT_PROFILE_DIR
    assert "not a readable directory" in capsys.readouterr().err
    assert m.resolve_profile_dir(str(tmp_path)) == str(tmp_path)
    assert m.resolve_profile_dir(None) == m.DEFAULT_PROFILE_DIR


# --- selection --------------------------------------------------------------

def test_no_profiles_returns_none(tmp_path, parser):
    assert m.select_profile(parser, _args(parser, "--profile-dir", str(tmp_path))) is None


def test_single_profile_used_without_prompt(tmp_path, parser, monkeypatch):
    _write(tmp_path, "only.json", {}, "only")
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not prompt"))
    p = m.select_profile(parser, _args(parser, "--profile-dir", str(tmp_path)))
    assert p["name"] == "only"


def test_multiple_profiles_prompt_by_number_and_name(tmp_path, parser, monkeypatch, capsys):
    _write(tmp_path, "a.json", {}, "alpha", "first one")
    _write(tmp_path, "b.json", {}, "beta")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    args = _args(parser, "--profile-dir", str(tmp_path))
    answers = iter(["9", "2"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert m.select_profile(parser, args)["name"] == "beta"
    out = capsys.readouterr()
    assert "alpha -- first one" in out.out and "not a choice" in out.err
    monkeypatch.setattr("builtins.input", lambda *_: "ALPHA")
    assert m.select_profile(parser, args)["name"] == "alpha"
    monkeypatch.setattr("builtins.input", lambda *_: "0")
    assert m.select_profile(parser, args) is None


def test_multiple_profiles_non_tty_errors(tmp_path, parser, monkeypatch):
    _write(tmp_path, "a.json", {})
    _write(tmp_path, "b.json", {})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        m.select_profile(parser, _args(parser, "--profile-dir", str(tmp_path)))


def test_profile_flag_by_name_stem_and_path(tmp_path, parser):
    _write(tmp_path, "a.json", {}, "alpha")
    path_b = _write(tmp_path, "b.json", {}, "beta")
    d = ("--profile-dir", str(tmp_path))
    assert m.select_profile(parser, _args(parser, *d, "--profile", "beta"))["name"] == "beta"
    assert m.select_profile(parser, _args(parser, *d, "--profile", "A"))["name"] == "alpha"
    assert m.select_profile(parser, _args(parser, "--profile", path_b))["name"] == "beta"
    with pytest.raises(SystemExit):
        m.select_profile(parser, _args(parser, *d, "--profile", "missing"))


def test_no_profile_ignores_profiles(tmp_path, parser):
    _write(tmp_path, "a.json", {})
    args = _args(parser, "--profile-dir", str(tmp_path), "--no-profile")
    assert m.select_profile(parser, args) is None


# --- precedence -------------------------------------------------------------

def test_precedence_defaults_profile_switches(tmp_path, parser):
    _write(tmp_path, "a.json", {"fix_mojibake": False, "sample_limit": 7, "log_full": ["p"]})
    p = m.select_profile(parser, _args(parser, "--profile-dir", str(tmp_path)))
    parser.set_defaults(**p["options"])
    args = parser.parse_args(["--sample-limit", "3", "--log-full", "cli"])
    assert args.fix_mojibake is False       # from the profile
    assert args.sample_limit == 3           # explicit switch beats the profile
    assert args.log_full == ["p", "cli"]    # repeatable switches add to the profile's list
    assert args.fix_008_length is True      # untouched default


# --- main() wiring ----------------------------------------------------------

def test_main_list_profiles(tmp_path, capsys):
    _write(tmp_path, "a.json", {}, "alpha", "first")
    assert m.main(["--list-profiles", "--profile-dir", str(tmp_path)]) == 0
    assert "alpha (a.json) -- first" in capsys.readouterr().out


def test_main_reports_profile_used(tmp_path, capsys):
    _write(tmp_path, "only.json", {"log_zero": True}, "only")
    out = tmp_path / "out.mrc"
    rc = m.main([FIXTURE, "-o", str(out), "--log", str(tmp_path / "x.log"),
                 "--profile-dir", str(tmp_path), "--source-system", "other"])
    assert rc == 0 and out.exists()
    assert "Using profile: only" in capsys.readouterr().out


def test_split_forwards_profile_to_bib_repair_without_reselecting(tmp_path, monkeypatch, capsys):
    """A profile that sets split_bib_holdings must not make the bib half split (or pick a
    profile) again, and the bib half must run with the profile's other switches."""
    import shutil
    src = tmp_path / "mixed.mrc"
    shutil.copy(os.path.join(ROOT, "tests", "fixtures", "kitchen_sink_mixed.mrc"), src)
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    _write(pdir, "wms.json", {"split_bib_holdings": True, "fix_mojibake": False}, "wms")
    calls = []
    real_run = m.run

    def spy(parser, args):
        calls.append(args)
        return real_run(parser, args)

    monkeypatch.setattr(m, "run", spy)
    rc = m.main([str(src), "--profile-dir", str(pdir), "--source-system", "other",
                 "--log", str(tmp_path / "shared.log"), "--mrk"])
    assert rc == 0
    assert capsys.readouterr().out.count("Using profile: wms") == 1
    assert len(calls) == 2
    top, bib = calls
    assert top.split_bib_holdings is True
    assert bib.split_bib_holdings is False and bib.fix_mojibake is False
    assert bib.input == str(tmp_path / "mixed_bib.mrc")
    assert bib.out == str(tmp_path / "mixed_bib_repaired.mrc")
    assert bib.log is None and bib.mrk == ""
    assert (tmp_path / "mixed_bib_repaired.mrc").exists()


def test_missing_input_path_errors_without_a_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        m.main([str(tmp_path / "nope.mrc"), "--no-profile"])
    assert "is not a file" in capsys.readouterr().err


def test_missing_input_path_prompts_on_a_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p: prompts.append(p) or FIXTURE)
    rc = m.main([str(tmp_path / "nope.mrc"), "--no-profile", "--source-system", "other",
                 "-o", str(tmp_path / "out.mrc"), "--log", str(tmp_path / "x.log")])
    assert rc == 0 and prompts == ["Input MARC file: "]
    assert "is not a file -- enter another path" in capsys.readouterr().err


# --- marc_repair_profile.py -------------------------------------------------

def test_save_stores_only_non_defaults(tmp_path, capsys):
    rc = tool.main(["save", "my prof", "--profile-dir", str(tmp_path), "--description", "d",
                    "--no-fix-mojibake", "--sample-limit", "25", "--log-full", "a",
                    "--trim-oversized-record"])
    assert rc == 0
    data = json.loads((tmp_path / "my-prof.json").read_text())
    assert data == {"name": "my prof", "description": "d",
                    "options": {"fix_mojibake": False, "log_full": ["a"]}}
    saved = m.load_profile_file(str(tmp_path / "my-prof.json"), m.build_parser())
    assert saved["name"] == "my prof"


def test_save_creates_a_missing_profile_dir(tmp_path):
    target = tmp_path / "new" / "profiles"
    assert tool.main(["save", "p", "--profile-dir", str(target), "--log-zero"]) == 0
    assert (target / "p.json").exists()


def test_save_excludes_per_run_paths(tmp_path):
    argv = ["save", "p", "--profile-dir", str(tmp_path), "-o", "x.mrc", "--log-zero"]
    assert tool.main(argv) == 0
    assert json.loads((tmp_path / "p.json").read_text())["options"] == {"log_zero": True}


def test_save_refuses_overwrite_unless_forced(tmp_path, monkeypatch):
    argv = ["save", "p", "--profile-dir", str(tmp_path), "--log-zero"]
    assert tool.main(argv) == 0
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert tool.main(argv[:-1] + ["--no-fix-mojibake"]) == 1
    assert json.loads((tmp_path / "p.json").read_text())["options"] == {"log_zero": True}
    assert tool.main(argv[:-1] + ["--no-fix-mojibake", "--force"]) == 0
    assert json.loads((tmp_path / "p.json").read_text())["options"] == {"fix_mojibake": False}


def test_save_bad_name_and_bad_switch(tmp_path):
    assert tool.main(["save", "///", "--profile-dir", str(tmp_path)]) == 1
    assert tool.main(["save", "p", "--profile-dir", str(tmp_path), "--bogus"]) == 2


def test_wizard_collects_changed_options(parser):
    scripted = {
        "--no-fix-mojibake": ["y"],
        "--sample-limit": ["40"],
        "--log-full": ["cat1", ""],
        "--source-system": ["sierra"],
        "--trim-oversized-record": ["false"],
    }
    queue = []

    def say(text):
        header = text.strip().split("\n")[0].split(" / ")
        if header[0].startswith("-"):  # an option's heading: load that option's scripted answers
            queue[:] = next((v for k, v in scripted.items() if k in header), [])

    def ask(prompt):
        return queue.pop(0) if queue else ""

    assert tool.run_wizard(parser, ask=ask, say=say) == {
        "fix_mojibake": False, "sample_limit": 40, "log_full": ["cat1"],
        "source_system": "sierra", "trim_oversized_record": False,
    }


def test_wizard_q_finishes_early(parser):
    first = []

    def ask(prompt):
        first.append(prompt)
        return "q"

    assert tool.run_wizard(parser, ask=ask, say=lambda _t: None) == {}
    assert len(first) == 1


def test_init_writes_profile(tmp_path, monkeypatch):
    answers = iter(["q", "my description"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert tool.main(["init", "wiz", "--profile-dir", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "wiz.json").read_text()) == {
        "name": "wiz", "description": "my description", "options": {},
    }
