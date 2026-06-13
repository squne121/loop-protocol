"""Unit tests for get_ci_failed_log.py using mocked gh CLI."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent
    / ".claude/skills/impl-review-loop/scripts/get_ci_failed_log.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("get_ci_failed_log", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_module()

HEAD_SHA = "abc123def456abc123def456abc123def456"


def make_run(
    head_sha=HEAD_SHA,
    status="completed",
    conclusion="failure",
    workflow="CI",
    run_id=1001,
    attempt=1,
):
    return {
        "databaseId": run_id,
        "attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "headSha": head_sha,
        "workflowName": workflow,
        "event": "push",
        "createdAt": "2026-06-13T00:00:00Z",
        "updatedAt": "2026-06-13T00:01:00Z",
        "url": "https://github.com/example/repo/actions/runs/1001",
    }


def parse_args_for(head_sha=HEAD_SHA, workflow=None, max_bytes=60000):
    import argparse

    ns = argparse.Namespace(
        repo="owner/repo",
        pr=1,
        head_sha=head_sha,
        workflow=workflow,
        job=None,
        max_bytes=max_bytes,
    )
    return ns


# ---------------------------------------------------------------------------
# AC2: no matching run
# ---------------------------------------------------------------------------
class TestNoMatchingRun:
    def test_empty_run_list(self, monkeypatch):
        monkeypatch.setattr(mod, "run_gh", lambda *a, **k: (0, "[]", ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1

    def test_sha_mismatch(self, monkeypatch):
        runs = [make_run(head_sha="different_sha")]
        monkeypatch.setattr(mod, "run_gh", lambda *a, **k: (0, json.dumps(runs), ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1

    def test_gh_run_list_failure(self, monkeypatch):
        monkeypatch.setattr(mod, "run_gh", lambda *a, **k: (1, "", "auth error"))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# AC3: pending
# ---------------------------------------------------------------------------
class TestPending:
    @pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
    def test_pending_statuses(self, monkeypatch, status):
        run = make_run(status=status, conclusion=None)
        monkeypatch.setattr(mod, "run_gh", lambda *a, **k: (0, json.dumps([run]), ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# AC4: success
# ---------------------------------------------------------------------------
class TestSuccess:
    def test_ci_passed(self, monkeypatch):
        run = make_run(status="completed", conclusion="success")
        monkeypatch.setattr(mod, "run_gh", lambda *a, **k: (0, json.dumps([run]), ""))
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# AC5: failure primary
# ---------------------------------------------------------------------------
class TestFailurePrimary:
    def test_primary_log_retrieved(self, monkeypatch):
        run = make_run(conclusion="failure")
        list_out = json.dumps([run])
        log_out = "##[error] step failed\nsome log line"

        def fake_run_gh(*args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if "list" in joined:
                return (0, list_out, "")
            if "log-failed" in joined:
                return (0, log_out, "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# AC6: fallback
# ---------------------------------------------------------------------------
class TestFallback:
    def test_fallback_success(self, monkeypatch):
        run = make_run(conclusion="failure")
        list_out = json.dumps([run])
        jobs_out = json.dumps([{"id": 9, "name": "build", "conclusion": "failure"}])
        job_log = "Error in build step\nFailed"

        def fake_run_gh(*args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if "list" in joined:
                return (0, list_out, "")
            if "log-failed" in joined:
                return (0, "", "")
            if "attempts" in joined and "jobs" in joined:
                return (0, jobs_out, "")
            if "jobs" in joined and "logs" in joined:
                return (0, job_log, "")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1

    def test_fallback_failure(self, monkeypatch):
        run = make_run(conclusion="failure")
        list_out = json.dumps([run])

        def fake_run_gh(*args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if "list" in joined:
                return (0, list_out, "")
            return (1, "", "error")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# AC5/AC6: multiple attempts — latest attempt is selected
# ---------------------------------------------------------------------------
class TestMultipleAttempts:
    def test_latest_attempt_selected(self, monkeypatch):
        runs = [
            make_run(conclusion="failure", attempt=1, run_id=1001),
            make_run(conclusion="failure", attempt=2, run_id=1001),
        ]
        list_out = json.dumps(runs)
        log_out = "latest attempt log"
        calls = []

        def fake_run_gh(*args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if "list" in joined:
                return (0, list_out, "")
            if "log-failed" in joined:
                calls.append(joined)
                if "--attempt=2" in joined:
                    return (0, log_out, "")
                return (1, "", "wrong attempt")
            return (1, "", "unexpected")

        monkeypatch.setattr(mod, "run_gh", fake_run_gh)
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        assert any("--attempt=2" in c for c in calls), "latest attempt (2) must be used"


# ---------------------------------------------------------------------------
# AC8: redaction / truncation / ANSI
# ---------------------------------------------------------------------------
class TestRedaction:
    def test_token_redacted(self):
        text = "token: ghp_" + "A" * 36
        redacted, applied = mod.redact_tokens(text)
        assert "[REDACTED]" in redacted
        assert applied is True

    def test_truncation(self):
        text = "x" * 200
        result, truncated = mod.truncate(text, 100)
        assert truncated is True
        assert "[TRUNCATED]" in result

    def test_ansi_stripped(self):
        text = "\x1b[31mred\x1b[0m"
        assert mod.strip_ansi(text) == "red"


# ---------------------------------------------------------------------------
# Auth failure
# ---------------------------------------------------------------------------
class TestAuthFailure:
    def test_auth_failure_returns_no_run(self, monkeypatch):
        monkeypatch.setattr(
            mod, "run_gh", lambda *a, **k: (1, "", "HTTP 401: Requires authentication")
        )
        monkeypatch.setattr("argparse.ArgumentParser.parse_args", lambda *a, **k: parse_args_for())
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
