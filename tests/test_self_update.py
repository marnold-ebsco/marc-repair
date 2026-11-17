"""Startup update notice and --self-update (marc_repair_lib/self_update.py)."""
import argparse
import os

from marc_repair_lib import self_update as su


def _boom(*a):
    raise AssertionError("must not be called")


def _install(tmp_path, sha="aaa111aaa111"):
    (tmp_path / ".marc_repair_install_version").write_text(sha)
    return str(tmp_path)


def _args(**kw):
    return argparse.Namespace(**{"self_update": False, "update_check": True, **kw})


def _setup(tmp_path, monkeypatch, remote="bbb222bbb222"):
    base = _install(tmp_path)
    monkeypatch.setattr(su, "base_dir", lambda: base)
    monkeypatch.setattr(su, "latest_commit", lambda ref: remote)
    monkeypatch.delenv(su.GUARD_ENV, raising=False)
    return base


def test_check_reports_newer_commit(tmp_path, monkeypatch):
    base = _setup(tmp_path, monkeypatch)
    assert su.check(base) == ("aaa111aaa111", "bbb222bbb222", "main")


def test_check_silent_when_current_offline_or_git(tmp_path, monkeypatch):
    base = _setup(tmp_path, monkeypatch, remote="aaa111aaa111")
    assert su.check(base) is None
    monkeypatch.setattr(su, "latest_commit", lambda ref: None)
    assert su.check(base) is None
    os.mkdir(os.path.join(base, ".git"))
    monkeypatch.setattr(su, "latest_commit", lambda ref: "bbb222bbb222")
    assert su.check(base) is None


def test_default_prints_notice_and_does_not_update(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(su, "run_installer", _boom)
    su.handle(_args(), [])
    assert "update available: aaa111aaa111 -> bbb222bbb222" in capsys.readouterr().err


def test_no_update_check_is_silent(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    su.handle(_args(update_check=False), [])
    assert capsys.readouterr().err == ""


def test_self_update_installs_then_relaunches(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(su, "run_installer", lambda *a: True)
    seen = {}
    monkeypatch.setattr(os, "execv", lambda exe, argv: seen.update(argv=argv))
    su.handle(_args(self_update=True), ["in.mrc", "--self-update", "--log-zero"])
    assert seen["argv"][2:] == ["in.mrc", "--log-zero"]
    assert os.environ[su.GUARD_ENV] == "1"
    monkeypatch.delenv(su.GUARD_ENV)


def test_failed_update_continues(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(su, "run_installer", lambda *a: False)
    monkeypatch.setattr(os, "execv", _boom)
    su.handle(_args(self_update=True), [])
    assert "continuing with the installed version" in capsys.readouterr().err


def test_git_checkout_never_updates(tmp_path, monkeypatch, capsys):
    base = _setup(tmp_path, monkeypatch)
    os.mkdir(os.path.join(base, ".git"))
    monkeypatch.setattr(su, "run_installer", _boom)
    su.handle(_args(self_update=True), [])
    assert "git checkout" in capsys.readouterr().err


def test_up_to_date_says_so(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, remote="aaa111aaa111")
    su.handle(_args(), [])
    assert "marc_repair is up to date (aaa111aaa111)" in capsys.readouterr().err


def test_unreachable_says_skipped(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, remote=None)
    su.handle(_args(), [])
    assert "update check skipped (GitHub unreachable)" in capsys.readouterr().err
