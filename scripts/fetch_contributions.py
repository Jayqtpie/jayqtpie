#!/usr/bin/env python3
"""Fetch the profile's contribution calendar and public repos into data/.

The Cards workflow runs this every morning, then render_cards.py draws the
cards from what it wrote. Locally:

    GH_TOKEN=$(gh auth token) python scripts/fetch_contributions.py

Writes data/contributions.json (one entry per day of the calendar GitHub shows
on the profile) and data/repos.json (every public repo the user owns). Both are
sorted and written without timestamps, so a day with no new activity commits
nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN = os.environ.get("PROFILE_LOGIN") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "Jayqtpie"

QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount contributionLevel } }
      }
    }
    repositories(first: 100, privacy: PUBLIC, ownerAffiliations: OWNER,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes {
        name description url homepageUrl stargazerCount forkCount
        pushedAt isFork isArchived primaryLanguage { name }
      }
    }
  }
}
"""

LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


def token() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:  # A local run can borrow the gh CLI's login.
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("No token: set GH_TOKEN (in Actions, github.token) or log in with `gh auth login`.")


def query() -> dict:
    body = json.dumps({"query": QUERY, "variables": {"login": LOGIN}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={"Authorization": f"bearer {token()}", "Content-Type": "application/json",
                 "User-Agent": "profile-cards"},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        out = json.load(res)
    if out.get("errors"):
        sys.exit(f"GraphQL errors: {out['errors']}")
    return out["data"]["user"]


def write(path: Path, data: dict, rows: str) -> None:
    """Write `data` as JSON with each item of the list `rows` on one line, so a
    daily refresh shows up in the diff as the handful of lines that changed."""
    head = {k: v for k, v in data.items() if k != rows}
    lines = ",\n".join("  " + json.dumps(item, ensure_ascii=False) for item in data[rows])
    text = json.dumps(head, indent=1, ensure_ascii=False)[:-2] + f',\n "{rows}": [\n{lines}\n ]\n}}\n'
    json.loads(text)  # Never write a file the renderer can't read.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    user = query()
    cal = user["contributionsCollection"]["contributionCalendar"]
    days = [
        {"date": d["date"], "count": d["contributionCount"], "level": LEVELS[d["contributionLevel"]]}
        for w in cal["weeks"] for d in w["contributionDays"]
    ]
    days.sort(key=lambda d: d["date"])
    write(ROOT / "data" / "contributions.json",
          {"login": LOGIN, "total": cal["totalContributions"], "days": days}, "days")

    repos = [
        {
            "name": r["name"],
            "description": (r["description"] or "").strip(),
            "url": r["url"],
            "homepage": (r["homepageUrl"] or "").strip(),
            "language": (r["primaryLanguage"] or {}).get("name", ""),
            "stars": r["stargazerCount"],
            "forks": r["forkCount"],
            "pushed": r["pushedAt"][:10],
            "fork": r["isFork"],
            "archived": r["isArchived"],
        }
        for r in user["repositories"]["nodes"]
    ]
    repos.sort(key=lambda r: r["name"].lower())
    write(ROOT / "data" / "repos.json",
          {"login": LOGIN, "count": user["repositories"]["totalCount"], "repos": repos}, "repos")

    print(f"{LOGIN}: {cal['totalContributions']} contributions over {len(days)} days, {len(repos)} public repos")


if __name__ == "__main__":
    main()
