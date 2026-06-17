"""AC6: the materializer never calls raw ``gh issue create`` / ``gh issue edit`` for issue
creation. All creation flows through create_issue_txn.py (including the triage_only label
profile). A fake-gh integration check proves creation reaches gh only via the transaction.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import materialize_child_issues as m


def _ok_validate(body, kind, title):
    return m.RunResult(0, '{"status": "pass"}')


def test_materializer_routes_creation_through_create_runner_not_gh(valid_plan):
    gh_calls: list[list[str]] = []
    create_calls: list[dict] = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "")

    def create(**kw):
        create_calls.append(kw)
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    res = m.materialize(valid_plan, m.Runners(validate=_ok_validate, create=create, gh=gh))
    assert res["status"] == "ok"
    # Creation happened exactly once, through the create runner (the txn path).
    assert len(create_calls) == 1
    # The materializer NEVER issued a raw `gh issue create` (nor `gh issue create`-like).
    assert all(a[:2] != ["issue", "create"] for a in gh_calls)


def test_default_create_runner_invokes_txn_with_label_profile(monkeypatch):
    captured = {}

    def fake_run(argv, *a, **kw):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = '{"status":"success","issue_number":1,"issue_url":"u"}'
            stderr = ""

        return R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    m._default_create_runner(
        repo="o/r", title="実装: x", body="b", kind="implementation",
        label_profile="triage_only", dependencies=[], parent_issue=0, gh_bin="gh",
    )
    argv = captured["argv"]
    assert str(argv[1]).endswith("create_issue_txn.py")
    assert "issue" not in argv or "create" not in argv  # never builds a raw `gh issue create`
    assert "--label-profile" in argv
    assert argv[argv.index("--label-profile") + 1] == "triage_only"


def _write_fake_gh(tmp_path: Path) -> tuple[Path, Path]:
    """A stateful fake gh that lets create_issue_txn.py complete the create happy-path
    quickly: the first `issue list` (dedupe) returns no match; after `issue create`, the
    poll `issue list` returns our issue immediately; label read-back graphql confirms the
    triage-required label. This keeps the integration test fast (no retry/sleep loops)."""
    log = tmp_path / "gh_calls.log"
    fake = tmp_path / "fake_gh.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, re, json\n"
        "args = sys.argv[1:]\n"
        "log = os.environ['GH_LOG']\n"
        "prior = open(log).read() if os.path.exists(log) else ''\n"
        "open(log, 'a').write(' '.join(args) + '\\n')\n"
        "if args[:2] == ['issue', 'create']:\n"
        "    print('https://github.com/o/r/issues/999')\n"
        "elif args[:2] == ['issue', 'list']:\n"
        "    created_yet = 'issue create' in prior\n"
        "    if not created_yet:\n"
        "        print('[]')  # pre-create dedupe: no existing match\n"
        "    else:\n"
        "        q = ''\n"
        "        for i, a in enumerate(args):\n"
        "            if a == '--search':\n"
        "                q = args[i + 1]\n"
        "        m2 = re.search(r'\\\"(.+)\\\"', q)\n"
        "        title = m2.group(1) if m2 else ''\n"
        "        print(json.dumps([{'number': 999, 'title': title, 'url': 'https://github.com/o/r/issues/999'}]))\n"
        "elif args[:2] == ['api', 'graphql']:\n"
        "    issue = {'id': 'I_kw999', 'databaseId': 999, 'number': 999,\n"
        "             'labels': {'nodes': [{'name': 'triage-required'}]}}\n"
        "    print(json.dumps({'data': {'repository': {'issue': issue}}}))\n"
        "else:\n"
        "    print('')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake, log


def test_fake_gh_integration_creation_only_via_txn(valid_child, tmp_path, monkeypatch):
    fake_gh, log = _write_fake_gh(tmp_path)
    monkeypatch.setenv("GH_LOG", str(log))

    # A template-valid body is required because create_issue_txn now validates with
    # --kind/--title (AC3) before any gh call.
    body = m.render_canonical_body(valid_child, parent_issue=254)

    # Drive the default create runner (the real txn path) against the fake gh. No parent /
    # dependency stages (parent_issue=0, deps=[]) keep the integration focused and fast.
    res = m._default_create_runner(
        repo="o/r", title=valid_child["title"], body=body,
        kind="implementation", label_profile="triage_only", dependencies=[],
        parent_issue=0, gh_bin=str(fake_gh),
    )

    import json as _json
    log_lines = log.read_text(encoding="utf-8").splitlines()
    # create_issue_txn.py drove a `gh issue create` against the fake gh.
    create_calls = [ln for ln in log_lines if ln.startswith("issue create")]
    assert len(create_calls) == 1, log_lines
    # The transaction owns the creation step (proves the create flowed through the txn,
    # not a raw materializer shortcut). Downstream GitHub-link steps are not emulated here.
    txn_out = _json.loads(res.stdout.strip().splitlines()[-1])
    assert "create" in txn_out["completed_steps"]
    assert txn_out["issue_number"] == 999


def test_materialize_full_run_never_raw_creates(valid_plan):
    """End-to-end materialize with recording runners: zero raw `gh issue create`."""
    gh_calls: list[list[str]] = []

    def gh(args):
        gh_calls.append(args)
        return m.RunResult(0, "")

    def create(**kw):
        return m.RunResult(0, '{"status":"success","issue_number":330,"issue_url":"u"}')

    m.materialize(valid_plan, m.Runners(validate=_ok_validate, create=create, gh=gh))
    assert all(a[:2] != ["issue", "create"] for a in gh_calls)
    assert all(a[:2] != ["issue", "edit"] or "--body-file" in a for a in gh_calls)
