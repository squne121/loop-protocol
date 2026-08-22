#!/usr/bin/env python3
"""fake_gh.py -- a minimal fake `gh` CLI provider for create-issue workflow tests.

Issue #2299 AC2/AC5: lets the real, unmodified `create-issue` skill workflow
(`create_issue_txn.py` invoked with its default `--gh gh` argument) run end to
end -- including from inside a genuine `issue-creator` SubAgent launched under
`scripts/claude-gpt/launch.sh` -- without ever touching real GitHub. Callers
put a directory containing an executable file literally named `gh` (this
script, or a symlink/copy of it) at the front of `PATH`; the workflow's normal
`gh` invocations are then transparently served by this fixture instead.

State is a single JSON file (path given via the required `FAKE_GH_STATE_FILE`
env var) of the shape:

    {"next_number": 1, "issues": {"1": {"number": 1, "title": ..., "body": ...,
     "url": ..., "state": "open"}}}

Only the subset of `gh` surface actually exercised by
`.claude/skills/create-issue/scripts/create_issue_txn.py`'s default (no
labels/parent/dependency/blocking) happy path is implemented:

  gh issue create --repo <repo> --title <t> [--body <b> | --body-file <f>]
  gh issue list   --repo <repo> --state <s> --search <q> --limit <n> --json <fields>
  gh issue view   <number> --repo <repo> --json <fields>
  gh issue close  <number> --repo <repo> [--reason <r>]
  gh api          repos/<owner>/<repo>/issues/<number> [--method GET]
  gh auth status  (always reports authenticated -- this fixture never touches
                   real credentials; it exists purely to keep the workflow
                   from erroring out if it opportunistically checks auth)

Anything else exits non-zero with a clear stderr message (fail loud, not
silent) so an unsupported call surfaces as a workflow failure rather than a
false PASS.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _state_path() -> Path:
    raw = os.environ.get("FAKE_GH_STATE_FILE", "")
    if not raw:
        sys.stderr.write("fake_gh.py: FAKE_GH_STATE_FILE env var is required but unset\n")
        sys.exit(2)
    return Path(raw)


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {"next_number": 1, "issues": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"next_number": 1, "issues": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _flag_value(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def _has_flag(args: list[str], flag: str) -> bool:
    return flag in args


def _quoted_title_from_search(search: str | None) -> str | None:
    if not search:
        return None
    if '"' not in search:
        return None
    parts = search.split('"')
    if len(parts) < 2:
        return None
    return parts[1]


def cmd_issue_create(args: list[str]) -> int:
    repo = _flag_value(args, "--repo") or ""
    title = _flag_value(args, "--title") or ""
    body_file = _flag_value(args, "--body-file")
    body = _flag_value(args, "--body")
    if body_file:
        body = Path(body_file).read_text(encoding="utf-8")
    body = body or ""

    state_path = _state_path()
    state = _load_state(state_path)
    number = state.get("next_number", 1)
    state["next_number"] = number + 1
    url = f"https://github.com/{repo}/issues/{number}"
    state.setdefault("issues", {})[str(number)] = {
        "number": number,
        "title": title,
        "body": body,
        "url": url,
        "state": "open",
    }
    _save_state(state_path, state)
    print(url)
    return 0


def cmd_issue_list(args: list[str]) -> int:
    state_filter = _flag_value(args, "--state") or "open"
    search = _flag_value(args, "--search")
    target_title = _quoted_title_from_search(search)

    state_path = _state_path()
    state = _load_state(state_path)
    results = []
    for issue in state.get("issues", {}).values():
        if state_filter != "all" and issue.get("state") != state_filter:
            continue
        if target_title is not None and target_title not in issue.get("title", ""):
            continue
        results.append({"number": issue["number"], "title": issue["title"], "url": issue["url"]})
    print(json.dumps(results))
    return 0


def cmd_issue_view(args: list[str]) -> int:
    if not args or args[0].startswith("-"):
        sys.stderr.write("fake_gh.py: issue view requires an issue number\n")
        return 1
    number = args[0]
    state_path = _state_path()
    state = _load_state(state_path)
    issue = state.get("issues", {}).get(number)
    if issue is None:
        sys.stderr.write(f"fake_gh.py: issue #{number} not found\n")
        return 1
    print(json.dumps(issue))
    return 0


def cmd_issue_close(args: list[str]) -> int:
    if not args or args[0].startswith("-"):
        sys.stderr.write("fake_gh.py: issue close requires an issue number\n")
        return 1
    number = args[0]
    state_path = _state_path()
    state = _load_state(state_path)
    issue = state.get("issues", {}).get(number)
    if issue is None:
        sys.stderr.write(f"fake_gh.py: issue #{number} not found\n")
        return 1
    issue["state"] = "closed"
    _save_state(state_path, state)
    return 0


def cmd_api(args: list[str]) -> int:
    endpoint = next((a for a in args if not a.startswith("-") and "issues" in a), None)
    if endpoint is None:
        sys.stderr.write("fake_gh.py: unsupported `gh api` invocation (no issues endpoint found)\n")
        return 1
    number = endpoint.rstrip("/").rsplit("/", 1)[-1]
    state_path = _state_path()
    state = _load_state(state_path)
    issue = state.get("issues", {}).get(number)
    if issue is None:
        sys.stderr.write(f"fake_gh.py: api readback for issue #{number} not found\n")
        return 1
    print(json.dumps(issue))
    return 0


def cmd_auth(args: list[str]) -> int:
    if args and args[0] == "status":
        sys.stderr.write("fake_gh.py: authenticated to github.com as fake-provider (fixture)\n")
        return 0
    sys.stderr.write("fake_gh.py: unsupported `gh auth` subcommand\n")
    return 1


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("fake_gh.py: no subcommand given\n")
        return 1
    top = argv[0]
    rest = argv[1:]
    if top == "issue":
        if not rest:
            sys.stderr.write("fake_gh.py: `gh issue` requires a subcommand\n")
            return 1
        sub, sub_rest = rest[0], rest[1:]
        if sub == "create":
            return cmd_issue_create(sub_rest)
        if sub == "list":
            return cmd_issue_list(sub_rest)
        if sub == "view":
            return cmd_issue_view(sub_rest)
        if sub == "close":
            return cmd_issue_close(sub_rest)
        sys.stderr.write(f"fake_gh.py: unsupported `gh issue {sub}` subcommand\n")
        return 1
    if top == "api":
        return cmd_api(rest)
    if top == "auth":
        return cmd_auth(rest)
    sys.stderr.write(f"fake_gh.py: unsupported `gh {top}` subcommand\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
