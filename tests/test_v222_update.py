"""v2.2.2 server-authoritative auto-update. Real git repos in tmp_path (bare origin + server clone); never the working repo."""
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from aureon_mt5 import ops
from aureon_mt5.common import gitinfo
from aureon_mt5.common.updater import update_to_origin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    return subprocess.run(["git", *ID, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f: f.write(text)


def read(path):
    with open(path, encoding="utf-8") as f: return f.read()


@pytest.fixture
def repos(tmp_path):
    """origin (bare), dev (pushes), server (the bot's checkout) — server is one commit behind origin/master."""
    origin, dev, server = str(tmp_path / "origin.git"), str(tmp_path / "dev"), str(tmp_path / "server")
    git(str(tmp_path), "init", "-q", "--bare", "-b", "master", origin)
    git(str(tmp_path), "clone", "-q", origin, dev)
    git(dev, "checkout", "-q", "-b", "master")
    write(os.path.join(dev, ".gitignore"), ".env\nlogs/\nout/\n")
    write(os.path.join(dev, "main.py"), "v1\n")
    write(os.path.join(dev, "news_blackout.txt"), "upstream news\n")
    git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", "v1"); git(dev, "push", "-q", "origin", "master")
    git(str(tmp_path), "clone", "-q", origin, server)
    write(os.path.join(server, ".env"), "AUREON_MODE=ema2050\n")
    write(os.path.join(dev, "main.py"), "v2\n")
    git(dev, "commit", "-q", "-am", "v2"); git(dev, "push", "-q", "origin", "master")
    return SimpleNamespace(origin=origin, dev=dev, server=server, logs=str(tmp_path / "server" / "logs"))


def head_is_origin(server):
    return git(server, "rev-parse", "HEAD") == git(server, "rev-parse", "origin/master")


def recording_run(calls):
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, **kw)
    return run


# ------------------------------------------------------------------ updater
def test_dirty_tree_patch_saved_then_reset(repos):
    write(os.path.join(repos.server, "main.py"), "local hotfix\n")
    res = update_to_origin(repos.logs, cwd=repos.server)
    assert res["status"] == "updated" and res["discarded"] == ["main.py"]
    assert os.path.basename(res["patch"]).startswith("local_changes_") and res["patch"].endswith(".patch")
    assert "+local hotfix" in read(res["patch"])
    assert read(os.path.join(repos.server, "main.py")) == "v2\n" and head_is_origin(repos.server)
    assert read(os.path.join(repos.server, ".env")) == "AUREON_MODE=ema2050\n"      # ignored file untouched


def test_clean_tree_uses_ff_pull(repos):
    calls = []
    res = update_to_origin(repos.logs, run=recording_run(calls), cwd=repos.server)
    assert res["status"] == "updated" and not res["discarded"]
    assert ["git", "pull", "--ff-only", "origin", "master"] in calls
    assert not any(c[:2] == ["git", "reset"] for c in calls) and head_is_origin(repos.server)


def test_diverged_clean_tree_falls_back_to_reset(repos):
    write(os.path.join(repos.server, "local.txt"), "x\n")
    git(repos.server, "add", "local.txt"); git(repos.server, "commit", "-q", "-m", "local commit")
    res = update_to_origin(repos.logs, cwd=repos.server)
    assert res["status"] == "updated" and head_is_origin(repos.server)


def test_news_file_survives_being_untracked_upstream(repos):
    write(os.path.join(repos.server, "news_blackout.txt"), "server's own calendar\n")
    git(repos.dev, "rm", "-q", "--cached", "news_blackout.txt")
    write(os.path.join(repos.dev, ".gitignore"), ".env\nlogs/\nout/\nnews_blackout.txt\n")
    git(repos.dev, "add", ".gitignore"); git(repos.dev, "commit", "-q", "-m", "untrack news"); git(repos.dev, "push", "-q", "origin", "master")
    res = update_to_origin(repos.logs, cwd=repos.server)
    assert res["status"] == "updated" and head_is_origin(repos.server)
    assert read(os.path.join(repos.server, "news_blackout.txt")) == "server's own calendar\n"


def test_up_to_date_does_nothing(repos):
    git(repos.server, "pull", "-q", "--ff-only", "origin", "master")
    assert update_to_origin(repos.logs, cwd=repos.server)["status"] == "current"


# ------------------------------------------------------------------ check_for_updates (cards, execv)
def test_dirty_tree_card_posted_once_and_restart(repos, monkeypatch):
    monkeypatch.setattr(main, "_time", SimpleNamespace(sleep=lambda s: None, time=lambda: 0.0))
    write(os.path.join(repos.server, "main.py"), "local hotfix\n")
    notify, execv = Mock(), Mock()
    main.check_for_updates(notify, execv=execv, log_dir=repos.logs, cwd=repos.server)
    main.check_for_updates(notify, execv=execv, log_dir=repos.logs, cwd=repos.server)       # now current: silent
    titles = [c.args[0] for c in notify.card.call_args_list]
    assert sum("LOCAL EDITS DISCARDED · main.py" in t for t in titles) == 1
    assert "patch saved" in notify.card.call_args_list[0].args[1]
    assert titles[-1] == "AUREON · MT5 · RESTARTING" and execv.call_count == 1


def test_network_failure_never_execs_and_card_once_per_hour(monkeypatch):
    clock = {"t": 10_000.0}
    monkeypatch.setattr(main, "_time", SimpleNamespace(sleep=lambda s: None, time=lambda: clock["t"]))
    monkeypatch.setattr(main, "_update_failed_at", 0.0)
    def run(cmd, **kw):
        return SimpleNamespace(returncode=128, stdout="", stderr="Could not resolve host: github.com")
    notify, execv = Mock(), Mock()
    for _ in range(3):
        main.check_for_updates(notify, run=run, execv=execv, log_dir="unused")
    clock["t"] += 3601
    main.check_for_updates(notify, run=run, execv=execv, log_dir="unused")
    assert not execv.called and notify.card.call_count == 2
    assert all(c.args[0] == "AUREON · MT5 · UPDATE FAILED" for c in notify.card.call_args_list)


# ------------------------------------------------------------------ /restart pulls first
def test_restart_aborts_before_stopping_agents_when_update_fails():
    ag = SimpleNamespace(stop=Mock(), join=Mock())
    journal, notify, execv = Mock(), Mock(), Mock()
    out = ops.restart_process({"XAUUSD": ag}, journal, notify, "op", "test", argv=["main.py"], executor=execv,
                              update=lambda: {"status": "failed", "error": "git fetch failed", "discarded": []})
    assert out is False and not execv.called and not ag.stop.called
    assert "RESTART ABORTED" in notify.card.call_args[0][0]


def test_restart_updates_then_execs(repos, monkeypatch):
    monkeypatch.delenv("AUREON_RESTART_REASON", raising=False)
    ag = SimpleNamespace(stop=Mock(), join=Mock())
    execv = Mock()
    ops.restart_process({"XAUUSD": ag}, Mock(), Mock(), "op", "test", argv=["main.py"], executor=execv,
                        update=lambda: update_to_origin(repos.logs, cwd=repos.server))
    assert head_is_origin(repos.server) and ag.stop.called and execv.called
    monkeypatch.delenv("AUREON_RESTART_REASON", raising=False)


# ------------------------------------------------------------------ guards
def test_runtime_paths_are_gitignored():
    lines = {l.strip() for l in read(os.path.join(ROOT, ".gitignore")).splitlines()}
    for path in (".env", "logs/", "out/", "news_blackout.txt"):
        assert path in lines, f"{path} must be in .gitignore — reset --hard would otherwise overwrite it"


def test_git_history_behind_count_and_local_edits(repos):
    write(os.path.join(repos.server, "main.py"), "local hotfix\n")
    h = gitinfo.history(5, cwd=repos.server)
    assert h["running"]["status"].startswith("behind by 1 commit ·") and "1 local edit" in h["running"]["status"]
    update_to_origin(repos.logs, cwd=repos.server)
    assert gitinfo.history(5, cwd=repos.server)["running"]["status"] == "up to date with origin/master"


def test_news_file_seeded_from_example_only_when_missing(tmp_path):
    news, example = tmp_path / "news_blackout.txt", tmp_path / "news_blackout.example.txt"
    example.write_text("template\n", encoding="utf-8")
    assert main.seed_news_file(str(news)) and news.read_text(encoding="utf-8") == "template\n"
    news.write_text("server's own\n", encoding="utf-8")
    assert not main.seed_news_file(str(news)) and news.read_text(encoding="utf-8") == "server's own\n"
    assert os.path.exists(os.path.join(ROOT, "news_blackout.example.txt"))
