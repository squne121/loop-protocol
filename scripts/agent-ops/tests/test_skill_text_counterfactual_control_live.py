"""Issue #2981 AC9: real Claude Code structured runs for the ``skill_text_counterfactual`` mode.

Two ``claude_live`` cases (deselected by default; opt in with ``-m claude_live``):

* positive: a text-only fixture skill whose BASE text yields NO ordered marker and whose candidate
  text yields it -> the runner as a whole is ``discriminative`` / exit 0.
* negative: a fixture skill whose BASE text ALSO yields the ordered marker (candidate and BASE differ
  only in non-behavioral prose) -> the runner as a whole is ``non_discriminative`` / non-zero exit.

Isolation: fixture sources live under ``fixtures/skill_text_counterfactual_control/`` and are
materialized ONLY into a test-owned throwaway linked worktree under ``.claude/worktrees/`` (committed
there, run, then removed by its exact path). No fixture skill is ever added to the repository's skill
directory and no foreign worktree is touched. The marker values are random per run and appear nowhere
in the repository.

Scope statement (also written to the artifact): this evidence verifies the counterfactual
classification on a FIXTURE skill. It is NOT causal proof that the procedure steps of
``worktree-agent-runtime-smoke/SKILL.md`` itself drive behavior, and each arm is one LLM sample.

Unavailable CLI / auth -> stdout ``SKIP:`` + ``pytest.exit(returncode=77)`` (never ``pytest.skip``);
SKIP / unverified is never promoted to PASS. The artifact is worktree-local
(``artifacts/runtime-verification-AC9-<UTC timestamp>.log``, git-ignored, never committed).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import pytest

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent.parent.parent
RUNNER = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
FIXTURES = THIS_DIR / "fixtures" / "skill_text_counterfactual_control"
FIXTURE_SKILL = "counterfactual-fixture-skill"
FIXTURE_SKILL_PATH = f".claude/skills/{FIXTURE_SKILL}/SKILL.md"
THROWAWAY_PREFIX = "sktcf-live-"
LOG_TIMESTAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
ARTIFACT = REPO_ROOT / "artifacts" / f"runtime-verification-AC9-{LOG_TIMESTAMP}.log"
SCOPE_STATEMENT = (
    "Scope: this evidence verifies the skill_text_counterfactual classification on a fixture skill "
    "(one LLM sample per arm). It is NOT a causal proof that the procedure steps of "
    "worktree-agent-runtime-smoke/SKILL.md itself drive behavior."
)
_UNAVAILABLE_MARKERS = (
    "Please run /login",
    "Not authenticated",
    "invalid_grant",
    "command not found",
    "unrecognized_model",
    "WebSocket upgrade was rejected",
)
_MASK_RE = re.compile(r"(/(?:home|root|Users)/[^\s\"',)]+)|(sk-[A-Za-z0-9_-]{16,})|(gh[pousr]_[A-Za-z0-9]{20,})")


def _mask(text: str) -> str:
    return _MASK_RE.sub("<masked>", text)


def _git(directory: Path | str, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(directory), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=False, timeout=120,
        env={**os.environ, "GIT_AUTHOR_NAME": "cf-live", "GIT_AUTHOR_EMAIL": "cf-live@invalid",
             "GIT_COMMITTER_NAME": "cf-live", "GIT_COMMITTER_EMAIL": "cf-live@invalid"},
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {args} failed: {_mask(completed.stderr)}")
    return completed.stdout.strip()


def _canonical_root() -> Path:
    common = _git(THIS_DIR, "rev-parse", "--git-common-dir")
    return (THIS_DIR / common).resolve().parent


def _claude_version() -> str | None:
    exe = shutil.which("claude")
    if exe is None:
        return None
    completed = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False, timeout=60)
    return (completed.stdout or completed.stderr).strip().splitlines()[0] if completed.returncode == 0 else None


def _write_log(case: str, *, result: str, exit_code: int | None, reason: str, environment: dict, input_: dict,
               output: dict) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    block = {
        "AC": "AC9 (skill_text_counterfactual live) / " + case,
        "Timestamp": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Environment": environment,
        "Input": input_,
        "Output": output,
        "Verdict": {"Result": result, "Exit Code": exit_code, "Reason": reason},
        "Scope": SCOPE_STATEMENT,
    }
    text = _mask(json.dumps(block, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    with ARTIFACT.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _skip(case: str, reason: str, environment: dict, input_: dict, output: dict | None = None) -> NoReturn:
    _write_log(case, result="SKIP", exit_code=77, reason=reason, environment=environment, input_=input_,
               output=output or {})
    print(f"SKIP: {reason}")
    pytest.exit(f"SKIP: {reason}", returncode=77)


def _fill(text: str, markers: tuple[str, str]) -> str:
    return text.replace("{{MARKER_ONE}}", markers[0]).replace("{{MARKER_TWO}}", markers[1])


def _materialize_and_commit(worktree: Path, case: str, markers: tuple[str, str]) -> tuple[str, str, dict]:
    """Commit the fixture BASE text, then the candidate text, in the throwaway worktree only."""
    target = worktree / FIXTURE_SKILL_PATH
    shas: dict[str, str] = {}
    digests: dict[str, str] = {}
    for stage in ("base", "candidate"):
        source = (FIXTURES / case / stage / "SKILL.md").read_text(encoding="utf-8")
        rendered = _fill(source, markers)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        digests[stage] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        _git(worktree, "add", "--", FIXTURE_SKILL_PATH)
        _git(worktree, "commit", "--no-verify", "-q", "-m", f"fixture {case} {stage} text")
        shas[stage] = _git(worktree, "rev-parse", "HEAD")
    return shas["base"], shas["candidate"], digests


def _run_case(case: str, expect_discriminative: bool) -> None:
    case_label = f"{case}-{'discriminative' if expect_discriminative else 'non-discriminative'}"
    version = _claude_version()
    nonce = secrets.token_hex(4)
    markers = (f"CF2981-A-{nonce}", f"CF2981-B-{nonce}")
    environment = {
        "Claude Code version": version,
        "runner": "scripts/agent-ops/run_worktree_agent_runtime_smoke.py",
        "test_head": _git(REPO_ROOT, "rev-parse", "HEAD"),
        "python": sys.version.split()[0],
    }
    input_ = {
        "fixture_case": case,
        "fixture_skill": FIXTURE_SKILL,
        "ordered_markers": list(markers),
        "prompt": f"/{FIXTURE_SKILL}",
        "cli_flags": "--runtime claude --mode structured --claude-adapter native --expect-skill-command "
                     f"{FIXTURE_SKILL} --expect-ordered-marker <A> --expect-ordered-marker <B> "
                     "--skill-text-counterfactual-base-ref <fixture BASE commit> "
                     f"--skill-text-counterfactual-skill {FIXTURE_SKILL_PATH}",
    }
    if version is None:
        _skip(case_label, "claude CLI is not available (or --version failed)", environment, input_)

    root = _canonical_root()
    throwaway = root / ".claude" / "worktrees" / f"{THROWAWAY_PREFIX}{case}-{secrets.token_hex(8)}"
    artifacts_dir = REPO_ROOT / "artifacts" / f"skill-text-cf-live-{LOG_TIMESTAMP}" / case
    artifacts_dir.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=False)
    prompt = artifacts_dir / "prompt.md"
    prompt.write_text(f"/{FIXTURE_SKILL}\n", encoding="utf-8")
    out_dir = artifacts_dir / "runner-out"
    evidence = artifacts_dir / "runner-evidence.json"
    created = False
    try:
        _git(root, "worktree", "add", "--detach", "-q", str(throwaway), environment["test_head"])
        created = True
        base_sha, candidate_sha, digests = _materialize_and_commit(throwaway, case, markers)
        input_["fixture_rendered_sha256"] = digests
        input_["fixture_base_commit"] = base_sha
        input_["fixture_candidate_commit"] = candidate_sha
        argv = [
            sys.executable, str(RUNNER),
            "--runtime", "claude", "--mode", "structured", "--claude-adapter", "native",
            "--worktree", str(throwaway), "--prompt-file", str(prompt),
            "--output-dir", str(out_dir), "--evidence-json", str(evidence),
            "--timeout-seconds", "300", "--max-turns", "10",
            "--expect-skill-command", FIXTURE_SKILL,
            "--expect-ordered-marker", markers[0], "--expect-ordered-marker", markers[1],
            f"--skill-text-counterfactual-base-ref={base_sha}",
            f"--skill-text-counterfactual-skill={FIXTURE_SKILL_PATH}",
        ]
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=1500)
        summary = json.loads(evidence.read_text(encoding="utf-8")) if evidence.exists() else {}
        cf = summary.get("skill_text_counterfactual") or {}
        output = {
            "runner_exit_code": completed.returncode,
            "verdict": summary.get("verdict"),
            "reasons": cf.get("reasons"),
            "requested_base_ref": cf.get("requested_base_ref"),
            "resolved_base_commit_sha": cf.get("resolved_base_commit_sha"),
            "candidate_head_sha": cf.get("candidate_head_sha"),
            "candidate_skill_blob_id": cf.get("candidate_skill_blob_id"),
            "base_skill_blob_id": cf.get("base_skill_blob_id"),
            "base_arm_commit_sha": cf.get("base_arm_commit_sha"),
            "prompt_sha256": cf.get("prompt_sha256"),
            "ordered_evidence_match": cf.get("ordered_evidence_match"),
            "arms": cf.get("arms"),
            "tree_difference": cf.get("tree_difference"),
            "candidate_worktree_unchanged": cf.get("candidate_worktree_unchanged"),
            "cleanup": cf.get("cleanup"),
            "runner_stderr_tail": _mask("\n".join(completed.stderr.splitlines()[-8:])),
        }
        blob = json.dumps(summary, default=str) + completed.stderr
        expected_verdict = "discriminative" if expect_discriminative else "non_discriminative"
        if (
            completed.returncode == 77
            or summary.get("verdict") == "candidate_skip"
            or (summary.get("verdict") != expected_verdict and any(m in blob for m in _UNAVAILABLE_MARKERS))
        ):
            _skip(case_label, "runtime unavailable (CLI/auth/capability); never promoted to PASS",
                  environment, input_, output)
        expected_exit = 0 if expect_discriminative else 1
        problems = []
        if summary.get("verdict") != expected_verdict:
            problems.append(f"verdict={summary.get('verdict')!r} expected {expected_verdict!r}")
        if completed.returncode != expected_exit:
            problems.append(f"exit={completed.returncode} expected {expected_exit}")
        ordered = cf.get("ordered_evidence_match") or {}
        candidate_ok = (ordered.get("candidate") or {}).get("verified")
        base_ok = (ordered.get("base") or {}).get("verified")
        if candidate_ok is not True:
            problems.append(f"candidate ordered_evidence_match.verified={candidate_ok!r}")
        if base_ok is not (not expect_discriminative):
            problems.append(f"base ordered_evidence_match.verified={base_ok!r}")
        if (cf.get("cleanup") or {}).get("all_removed") is not True:
            problems.append("runner-owned worktrees were not all removed")
        if cf.get("candidate_worktree_unchanged") is not True:
            problems.append("candidate worktree changed")
        if cf.get("tree_difference") != {"candidate_arm": [], "base_arm": [FIXTURE_SKILL_PATH]}:
            problems.append(f"tree_difference={cf.get('tree_difference')!r}")
        if not version or not (cf.get("arms") or {}).get("candidate") or not (cf.get("arms") or {}).get("base"):
            problems.append("arm identities / Claude Code version missing from the evidence")
        _write_log(
            case_label,
            result="PASS" if not problems else "FAIL",
            exit_code=completed.returncode,
            reason="; ".join(problems) or f"{expected_verdict} observed on a real structured run",
            environment=environment, input_=input_, output=output,
        )
        assert not problems, problems
    finally:
        if created:
            # exact path only; never a glob and never a foreign worktree
            _git(root, "worktree", "remove", "--force", str(throwaway), check=False)
            assert not throwaway.exists(), "test-owned throwaway worktree was not removed"


@pytest.mark.claude_live
def test_live_discriminative_positive_text_only_fixture_skill():
    """BASE text yields no ordered marker (valid control), candidate text yields it -> discriminative / exit 0."""
    _run_case("positive", expect_discriminative=True)


@pytest.mark.claude_live
def test_live_non_discriminative_negative_base_text_also_yields_the_marker():
    """BASE text also yields the ordered marker (only non-behavioral prose differs).

    Expected: the runner as a whole is non_discriminative with a non-zero exit."""
    _run_case("negative", expect_discriminative=False)
