"""v2.0.2 — /git-history: merges on master, the running build, tags. Pure parsers here; git is called with timeouts and never raises."""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
PR_RE = re.compile(r"#(\d+)")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_URL = "https://github.com/SVSKHD/Aureon_agentic_bot"
LOG_FORMAT = "%h|%ad|%an|%s"


def parse_log_line(line: str) -> dict | None:
    """'8d6ba37|2026-10-11 09:42:10 +0530|Hithesh|Merge pull request #6 …' → {sha, date (aware), raw_date, author, subject, prs}."""
    parts = line.strip().split("|", 3)
    if len(parts) < 4 or not parts[0]:
        return None
    sha, raw, author, subject = parts
    date = parse_git_date(raw)
    return {"sha": sha, "raw_date": raw.strip(), "date": date, "author": author, "subject": subject.strip(), "prs": [int(n) for n in PR_RE.findall(subject)]}


def parse_git_date(raw: str) -> datetime | None:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.strptime(raw, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fmt_dates(d: datetime | None, raw: str = "") -> str:
    """'2026-10-11 09:42 IST (04:12 +0000)': IST and the commit's own offset."""
    if d is None:
        return raw or "—"
    own = d.strftime("%H:%M %z"); ist = d.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    return f"{ist} ({own})"


def pr_links(subject: str, repo_url: str = REPO_URL) -> str:
    """Every '#N' in the subject becomes a markdown link to the PR."""
    return PR_RE.sub(lambda m: f"[#{m.group(1)}]({repo_url}/pull/{m.group(1)})", subject)


def parse_tag_line(line: str) -> dict | None:
    parts = line.strip().split("|", 1)
    if len(parts) < 2 or not parts[0]:
        return None
    return {"tag": parts[0], "raw_date": parts[1].strip(), "date": parse_git_date(parts[1])}


def _git(args: list[str], timeout: float = 10.0, cwd: str | None = None, run=subprocess.run) -> tuple[bool, str]:
    try:
        r = run(["git"] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd or ROOT)
        return r.returncode == 0, (r.stdout or "").strip() if r.returncode == 0 else (r.stderr or r.stdout or "").strip()
    except FileNotFoundError:
        return False, "git not on PATH"
    except subprocess.TimeoutExpired:
        return False, "git timed out"
    except Exception as e:
        return False, f"{e!r}"


def is_repo(cwd: str | None = None, run=subprocess.run) -> bool:
    ok, out = _git(["rev-parse", "--is-inside-work-tree"], 5, cwd, run)
    return ok and out.strip() == "true"


def history(n: int = 10, cwd: str | None = None, run=subprocess.run, fetch_timeout: float = 5.0) -> dict:
    """{ok, error, running: {describe, head, status}, merges: [...], tags: [...]} — never raises."""
    if not is_repo(cwd, run):
        return {"ok": False, "error": "not a git checkout", "running": {}, "merges": [], "tags": []}
    n = max(1, min(int(n), 50))
    _, describe = _git(["describe", "--tags", "--always"], 10, cwd, run)
    _, head = _git(["rev-parse", "--short", "HEAD"], 10, cwd, run)
    fetched, ferr = _git(["fetch", "origin", "master", "--quiet"], fetch_timeout, cwd, run)
    if fetched:
        ok_b, behind = _git(["rev-list", "--count", "HEAD..origin/master"], 10, cwd, run)
        ok_a, ahead = _git(["rev-list", "--count", "origin/master..HEAD"], 10, cwd, run)
        b = int(behind) if ok_b and behind.isdigit() else None; a = int(ahead) if ok_a and ahead.isdigit() else None
        if b == 0 and (a or 0) == 0:
            status = "up to date with origin/master"
        elif b is not None:
            status = f"behind by {b} commit{'s' if b != 1 else ''}" + (f", ahead by {a}" if a else "")
        else:
            status = "origin/master unknown"
    else:
        status = f"fetch failed — showing local ({ferr[:60]})" if ferr else "fetch failed — showing local"
    ok_l, log = _git(["log", "master", "--merges", "--date=iso-local", f"-n{n}", f"--format={LOG_FORMAT}"], 10, cwd, run)
    merges = [m for m in (parse_log_line(l) for l in (log.splitlines() if ok_l else [])) if m]
    ok_t, tags = _git(["tag", "--sort=-creatordate", "--format=%(refname:short)|%(creatordate:iso)"], 10, cwd, run)
    tag_rows = [t for t in (parse_tag_line(l) for l in (tags.splitlines() if ok_t else [])) if t][:10]
    _, head_date = _git(["log", "-1", "--date=iso-local", "--format=%ad", "HEAD"], 10, cwd, run)
    return {"ok": True, "error": "", "running": {"describe": describe, "head": head, "status": status, "head_date": head_date,
                                                 "head_date_text": fmt_dates(parse_git_date(head_date), head_date)},
            "merges": merges, "tags": tag_rows}


def head_merge_date(cwd: str | None = None, run=subprocess.run) -> str:
    """For the start-up banner: the (merge) date of HEAD, IST + own offset. '' when not a repo."""
    if not is_repo(cwd, run):
        return ""
    _, raw = _git(["log", "-1", "--date=iso-local", "--format=%ad", "HEAD"], 5, cwd, run)
    return fmt_dates(parse_git_date(raw), raw) if raw else ""


def card(h: dict, version: str, n: int = 10, tags: bool = False) -> dict:
    """The 'AUREON · GIT' embed dict (title/description/fields/color)."""
    if not h.get("ok"):
        return {"title": "AUREON · GIT", "description": h.get("error") or "not a git checkout", "fields": [], "color": 0x95A5A6}
    r = h["running"]
    desc = f"Running: **v{version}** · `{r.get('describe') or r.get('head') or '—'}` · {r.get('status', '—')}" + \
           (f"\nHEAD committed {r['head_date_text']}" if r.get("head_date_text") else "")
    if tags:
        lines = [f"`{t['tag']}` · {fmt_dates(t['date'], t['raw_date'])}" for t in h["tags"]] or ["no tags"]
        fields = [{"name": "Tags (newest first)", "value": "\n".join(lines)[:1024], "inline": False}]
    else:
        lines = [f"{fmt_dates(m['date'], m['raw_date'])} · `{m['sha']}` · {pr_links(m['subject'])}" for m in h["merges"][:n]] or ["no merges on master"]
        fields = [{"name": f"Merges on master (newest first, {min(n, len(h['merges']))})", "value": "\n".join(lines)[:1024], "inline": False}]
    return {"title": "AUREON · GIT", "description": desc[:4096], "fields": fields, "color": 0x3498DB}
