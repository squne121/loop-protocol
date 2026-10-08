"""Issue #2981: ``skill_text_counterfactual`` mode of ``run_worktree_agent_runtime_smoke.py``.

Hermetic tests (no real Claude Code process):

* the arm execution boundary (``execute_counterfactual_arm``) is replaced by a scripted
  executor that writes synthetic evidence JSON (AC2-AC5, AC6, AC7), and
* two end-to-end tests run the real runner as the arm child process against a fake
  ``claude`` executable whose output depends on the SKILL.md text in its cwd.

Selector hygiene (the contract's Verification Commands use ``-k`` substring selectors):
test names carry exactly ONE AC token -- ``discriminative`` (AC2; note the substring also
matches ``non_discriminative``), ``non_discriminative`` (AC3), ``control_invalid`` (AC4),
``fail_closed`` (AC5), ``isolation`` (AC6), ``cleanup`` (AC7), ``docs_contract`` (AC8) --
and ``test_selector_partition_*`` proves every selector selects its own tests.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
THIS_FILE = Path(__file__).resolve()

_spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke_issue_2981", SCRIPT)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules["run_worktree_agent_runtime_smoke_issue_2981"] = smoke
_spec.loader.exec_module(smoke)

SKILL_NAME = "fixture-skill"
SKILL_PATH = ".claude/skills/fixture-skill/SKILL.md"
MARKERS = ["MARKER_ONE_2981", "MARKER_TWO_2981"]
PROMPT_TEXT = "/fixture-skill\n"
EXE_SHA = "e" * 64
WORKTREE_NAME_RE = re.compile(r"skill-text-cf-(candidate|base)-[0-9a-f]{32}")


@pytest.fixture(autouse=True)
def _restore_sigterm_handler():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


# ---------------------------------------------------------------------------
# fixture repository
# ---------------------------------------------------------------------------


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    completed = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=False, env=env,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {args} failed: {completed.stderr}")
    return completed.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class CfRepo:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        _write(self.root / "README.md", "seed\n")
        _write(self.root / "extra.txt", "extra-base\n")
        _write(self.root / SKILL_PATH, "BASE TEXT: reply nothing special\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base_sha = git(self.root, "rev-parse", "HEAD")
        git(self.root, "branch", "base-ref")
        _write(self.root / "extra.txt", "extra-candidate\n")
        _write(self.root / SKILL_PATH, "CANDIDATE TEXT: reply MARKER_ONE_2981 MARKER_TWO_2981\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "candidate")
        self.cand_sha = git(self.root, "rev-parse", "HEAD")
        self.worktrees_dir = self.root / ".claude" / "worktrees"
        self.worktrees_dir.mkdir(parents=True)
        self.candidate = self.worktrees_dir / "issue-0000-candidate"
        git(self.root, "worktree", "add", "-q", "-b", "worktree-candidate", str(self.candidate), self.cand_sha)
        self.prompt = tmp_path / "prompt.md"
        self.prompt.write_text(PROMPT_TEXT, encoding="utf-8")
        self.prompt_sha = hashlib.sha256(PROMPT_TEXT.encode("utf-8")).hexdigest()
        self.out_dir = tmp_path / "out"
        self.evidence = tmp_path / "evidence.json"

    def blob(self, commit: str, path: str = SKILL_PATH) -> str:
        return git(self.root, "rev-parse", f"{commit}:{path}")

    def arm_worktrees(self) -> list[str]:
        return sorted(p.name for p in self.worktrees_dir.iterdir() if p.name.startswith("skill-text-cf-"))

    def registered_worktrees(self) -> list[str]:
        out = git(self.root, "worktree", "list", "--porcelain")
        return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]

    def argv(
        self,
        *,
        base_ref: str | None = "base-ref",
        skills: tuple[str, ...] | list[str] = (SKILL_PATH,),
        extra: tuple[str, ...] = (),
        markers: tuple[str, ...] = tuple(MARKERS),
        skill_command: str | None = SKILL_NAME,
        adapter: str = "native",
    ) -> list[str]:
        argv = [
            "--runtime", "claude", "--mode", "structured", "--claude-adapter", adapter,
            "--repo-root", str(self.root), "--worktree", str(self.candidate),
            "--prompt-file", str(self.prompt), "--output-dir", str(self.out_dir),
            "--evidence-json", str(self.evidence),
        ]
        if skill_command is not None:
            argv += ["--expect-skill-command", skill_command]
        for marker in markers:
            argv += ["--expect-ordered-marker", marker]
        if base_ref is not None:
            argv.append(f"--skill-text-counterfactual-base-ref={base_ref}")
        for skill in skills:
            argv.append(f"--skill-text-counterfactual-skill={skill}")
        argv += list(extra)
        return argv

    def reset_outputs(self) -> None:
        self.evidence.unlink(missing_ok=True)
        if self.out_dir.exists():
            for item in sorted(self.out_dir.rglob("*"), reverse=True):
                item.unlink() if item.is_file() or item.is_symlink() else item.rmdir()
            self.out_dir.rmdir()


@pytest.fixture()
def cf_repo(tmp_path: Path) -> CfRepo:
    return CfRepo(tmp_path)


# ---------------------------------------------------------------------------
# scripted arm executor (the injectable function boundary)
# ---------------------------------------------------------------------------


def _opts(argv: list[str]) -> dict[str, list[str]]:
    opts: dict[str, list[str]] = {}
    for item in argv:
        if item.startswith("--"):
            flag, _eq, value = item.partition("=")
            opts.setdefault(flag, []).append(value)
    return opts


def make_evidence(call: dict, *, ordered_verified: bool, exit_code: int | None = None, **overrides) -> dict:
    """Synthetic arm evidence shaped like the runner's own ``--evidence-json`` summary."""
    if exit_code is None:
        exit_code = 0 if ordered_verified else 1
    ordered = {
        "verified": ordered_verified,
        "expected_order": list(MARKERS),
        "observed_positions": {m: i for i, m in enumerate(MARKERS)} if ordered_verified else {},
        "missing_markers": [] if ordered_verified else list(MARKERS),
    }
    evidence = {
        "schema": smoke.SCHEMA,
        "tested_head": call["head"],
        "prompt_sha256": call["prompt_sha256"],
        "exit_code": exit_code,
        "process_exit_code": 0,
        "timed_out": False,
        "terminal_event_observed": True,
        "capability_decision": "runtime_outcome",
        "capability_error_classification": None,
        "expect_skill_command_observed": True,
        "user_prompt_expansion_command_names": [SKILL_NAME],
        "permission_denials": [],
        "postcondition_unexpected_changes": [],
        "ordered_evidence_match": ordered,
        "errors": [] if ordered_verified else [f"ordered evidence match failed: {ordered}"],
        "resolved_executable_sha256": EXE_SHA,
        "runtime_version": "9.9.9 (fake)",
        "settings_provenance": {"digest_sha256": None},
    }
    deleted = overrides.pop("_delete", ())
    evidence.update(overrides)
    for key in deleted:
        evidence.pop(key, None)
    return evidence


def arm_pass(call: dict) -> dict:
    return {"returncode": 0, "evidence": make_evidence(call, ordered_verified=True)}


def arm_ordered_fail(call: dict) -> dict:
    return {"returncode": 1, "evidence": make_evidence(call, ordered_verified=False)}


class Executor:
    """Replacement for ``execute_counterfactual_arm``; records what each arm saw."""

    def __init__(self, behaviors: dict, hook=None):
        self.behaviors = behaviors
        self.hook = hook
        self.calls: list[dict] = []

    def __call__(self, arm_name: str, arm_argv: list[str], *, timeout_seconds: float) -> dict:
        opts = _opts(arm_argv)
        worktree = opts["--worktree"][0]
        prompt_file = opts["--prompt-file"][0]
        call = {
            "arm": arm_name,
            "argv": list(arm_argv),
            "worktree": worktree,
            "evidence_path": opts["--evidence-json"][0],
            "head": git(worktree, "rev-parse", "HEAD"),
            "status": git(worktree, "status", "--porcelain", "--untracked-files=all"),
            "symbolic_head": git(worktree, "symbolic-ref", "-q", "HEAD", check=False),
            "skill_text": (Path(worktree) / SKILL_PATH).read_text(encoding="utf-8"),
            "extra_text": (Path(worktree) / "extra.txt").read_text(encoding="utf-8"),
            "prompt_sha256": hashlib.sha256(Path(prompt_file).read_bytes()).hexdigest(),
            "timeout_seconds": timeout_seconds,
        }
        self.calls.append(call)
        if self.hook is not None:
            self.hook(arm_name, call)
        behavior = self.behaviors[arm_name]
        if isinstance(behavior, BaseException):
            raise behavior
        spec = behavior(call) if callable(behavior) else behavior
        evidence = spec.get("evidence")
        if isinstance(evidence, dict):
            Path(call["evidence_path"]).write_text(json.dumps(evidence), encoding="utf-8")
        elif isinstance(evidence, str):
            Path(call["evidence_path"]).write_text(evidence, encoding="utf-8")
        return {
            "returncode": spec.get("returncode"),
            "timed_out": spec.get("timed_out", False),
            "stderr_excerpt": spec.get("stderr_excerpt", []),
        }


def run_cf(cf_repo: CfRepo, monkeypatch, behaviors: dict, *, hook=None, argv: list[str] | None = None):
    executor = Executor(behaviors, hook=hook)
    monkeypatch.setattr(smoke, "execute_counterfactual_arm", executor)
    returncode = smoke.main(argv if argv is not None else cf_repo.argv())
    summary = None
    if cf_repo.evidence.exists():
        summary = json.loads(cf_repo.evidence.read_text(encoding="utf-8"))
    cf = summary["skill_text_counterfactual"] if summary else None
    return returncode, summary, cf, executor


DISCRIMINATIVE_PLAN = {"candidate": arm_pass, "base": arm_ordered_fail}


# ---------------------------------------------------------------------------
# AC2: discriminative
# ---------------------------------------------------------------------------


def test_discriminative_candidate_pass_and_base_ordered_only_failure_exits_zero(cf_repo, monkeypatch):
    returncode, summary, cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    assert returncode == 0
    assert summary["verdict"] == "discriminative"
    assert summary["exit_code"] == 0
    assert summary["schema"] == smoke.CF_RESULT_SCHEMA
    assert [c["arm"] for c in executor.calls] == ["candidate", "base"]
    assert cf["requested_base_ref"] == "base-ref"
    assert cf["resolved_base_commit_sha"] == cf_repo.base_sha
    assert cf["candidate_head_sha"] == cf_repo.cand_sha
    assert cf["target_skill_path"] == SKILL_PATH
    assert cf["candidate_skill_blob_id"] == cf_repo.blob(cf_repo.cand_sha)
    assert cf["base_skill_blob_id"] == cf_repo.blob(cf_repo.base_sha)
    assert cf["candidate_skill_blob_id"] != cf["base_skill_blob_id"]
    assert cf["ordered_evidence_match"]["candidate"]["verified"] is True
    assert cf["ordered_evidence_match"]["base"]["verified"] is False
    assert cf["prompt_sha256"]["candidate"] == cf["prompt_sha256"]["base"] == cf_repo.prompt_sha
    assert cf["prompt_sha256"]["identical"] is True
    assert cf["reasons"] == []
    for arm in ("candidate", "base"):
        identity = cf["arms"][arm]
        assert identity["tested_head"] == executor.calls[("candidate", "base").index(arm)]["head"]
        assert identity["resolved_executable_sha256"] == EXE_SHA
        assert identity["runtime_version"] == "9.9.9 (fake)"


def test_discriminative_summary_public_hash_fields_are_not_redacted(cf_repo, monkeypatch):
    returncode, summary, cf, _executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    assert returncode == 0
    persisted = cf_repo.evidence.read_text(encoding="utf-8")
    summary_md = (cf_repo.out_dir / "summary.md").read_text(encoding="utf-8")
    for value in (
        cf["resolved_base_commit_sha"], cf["candidate_head_sha"], cf["base_arm_commit_sha"],
        cf["candidate_skill_blob_id"], cf["base_skill_blob_id"], cf_repo.prompt_sha,
    ):
        assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)
        assert value in persisted
        assert value in summary_md
    assert "<redacted>" not in json.dumps(
        {k: v for k, v in cf.items() if k not in ("arms",)}
    )
    # field-path allowlist only: the same text under an unregistered path stays redacted.
    redacted = smoke._redact_evidence_value({"skill_text_counterfactual": {"unregistered": "a" * 40}})
    assert redacted["skill_text_counterfactual"]["unregistered"] == "<redacted>"
    kept = smoke._redact_evidence_value(
        {"skill_text_counterfactual": {"candidate_head_sha": "a" * 40, "prompt_sha256": {"base": "b" * 64}}}
    )
    assert kept["skill_text_counterfactual"]["candidate_head_sha"] == "a" * 40
    assert kept["skill_text_counterfactual"]["prompt_sha256"]["base"] == "b" * 64


def test_discriminative_summary_records_one_sample_limitation(cf_repo, monkeypatch):
    returncode, _summary, cf, _executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    assert returncode == 0
    text = " ".join(cf["evidence_limitations"])
    assert "one LLM sample per arm" in text
    assert "not statistical or causal proof" in text
    assert "input identity, closed classification and exit mapping only" in text
    assert cf["treatment"] == "single_skill_md_blob"


def test_discriminative_requires_both_arms_to_report_identical_runtime_identity(cf_repo, monkeypatch):
    plan = {
        "candidate": arm_pass,
        "base": lambda call: {
            "returncode": 1,
            "evidence": make_evidence(call, ordered_verified=False, resolved_executable_sha256="f" * 64),
        },
    }
    returncode, summary, cf, _executor = run_cf(cf_repo, monkeypatch, plan)
    assert returncode == 1
    assert summary["verdict"] == "control_invalid"
    assert "arm_runtime_executable_mismatch" in cf["reasons"]


# ---------------------------------------------------------------------------
# AC3: non_discriminative / candidate FAIL
# ---------------------------------------------------------------------------


def test_non_discriminative_base_also_satisfies_ordered_assertion_is_non_pass_with_reason(cf_repo, monkeypatch):
    returncode, summary, cf, executor = run_cf(
        cf_repo, monkeypatch, {"candidate": arm_pass, "base": arm_pass}
    )
    assert returncode == 1
    assert summary["verdict"] == "non_discriminative"
    assert summary["exit_code"] == 1
    assert cf["reasons"] == ["base_arm_also_satisfies_ordered_assertion"]
    assert any("non_discriminative" in error for error in summary["errors"])
    assert [c["arm"] for c in executor.calls] == ["candidate", "base"]
    assert cf["ordered_evidence_match"]["base"]["verified"] is True


def test_non_discriminative_candidate_fail_stays_fail_and_base_arm_is_not_run(cf_repo, monkeypatch):
    returncode, summary, cf, executor = run_cf(
        cf_repo, monkeypatch, {"candidate": arm_ordered_fail, "base": arm_ordered_fail}
    )
    assert returncode == 1
    assert summary["verdict"] == "candidate_fail"
    assert [c["arm"] for c in executor.calls] == ["candidate"]
    assert cf["arms"]["base"] is None


def test_non_discriminative_candidate_skip_exit_77_is_never_promoted_to_pass(cf_repo, monkeypatch):
    plan = {
        "candidate": lambda call: {
            "returncode": 77, "evidence": make_evidence(call, ordered_verified=False, exit_code=77)
        },
        "base": arm_ordered_fail,
    }
    returncode, summary, _cf, executor = run_cf(cf_repo, monkeypatch, plan)
    assert returncode == 77
    assert summary["verdict"] == "candidate_skip"
    assert [c["arm"] for c in executor.calls] == ["candidate"]


# ---------------------------------------------------------------------------
# AC4: control_invalid (ordered_evidence_match.verified == False in the BASE evidence
# for every variant that still carries a verdict-looking failure)
# ---------------------------------------------------------------------------


def _base(**overrides):
    def build(call):
        return {"returncode": 1, "evidence": make_evidence(call, ordered_verified=False, **overrides)}

    return build


CONTROL_INVALID_CASES = {
    "timeout": lambda call: {"returncode": None, "timed_out": True, "evidence": None},
    "terminal_event_missing": _base(terminal_event_observed=False),
    "terminal_event_field_absent": _base(_delete=["terminal_event_observed"]),
    "skill_invocation_unobserved": _base(
        expect_skill_command_observed=False, user_prompt_expansion_command_names=[]
    ),
    "permission_denied": _base(permission_denials=[{"tool_name": "Bash"}]),
    "turn_limit_reached": _base(capability_decision="turn_limit_reached"),
    "capability_error": _base(capability_error_classification="unrecognized option"),
    "postcondition_failure": _base(postcondition_unexpected_changes=["path changed: x"]),
    "postcondition_field_absent": _base(_delete=["postcondition_unexpected_changes"]),
    "skip_77": lambda call: {
        "returncode": 77, "evidence": make_evidence(call, ordered_verified=False, exit_code=77)
    },
    "evidence_missing": lambda call: {"returncode": 1, "evidence": None},
    "evidence_unparsable": lambda call: {"returncode": 1, "evidence": "{not json"},
    "ordered_field_missing": _base(_delete=["ordered_evidence_match"]),
    "ordered_verified_not_bool": _base(
        ordered_evidence_match={"verified": None, "expected_order": MARKERS}
    ),
    "additional_unrelated_error": lambda call: {
        "returncode": 1,
        "evidence": make_evidence(
            call, ordered_verified=False,
            errors=["expected markers not observed: ['x']", "ordered evidence match failed: {}"],
        ),
    },
    "failure_not_attributed_to_ordered": _base(errors=["structured lane timed out"]),
    "process_exited_nonzero": _base(process_exit_code=2),
    "process_timed_out_flag": _base(timed_out=True),
    "evidence_bound_to_other_head": _base(tested_head="1" * 40),
    "prompt_hash_differs": _base(prompt_sha256="2" * 64),
    "evidence_schema_wrong": _base(schema="OTHER_SCHEMA"),
    "different_marker_list": _base(
        ordered_evidence_match={
            "verified": False, "expected_order": ["OTHER"], "observed_positions": {},
            "missing_markers": ["OTHER"],
        }
    ),
    "exit_code_zero_but_ordered_false": _base(exit_code=0),
}


@pytest.mark.parametrize("case", sorted(CONTROL_INVALID_CASES))
def test_control_invalid_base_arm_is_never_a_valid_negative_control(cf_repo, monkeypatch, case):
    plan = {"candidate": arm_pass, "base": CONTROL_INVALID_CASES[case]}
    returncode, summary, cf, executor = run_cf(cf_repo, monkeypatch, plan)
    assert returncode != 0
    assert summary["verdict"] == "control_invalid", (case, cf["reasons"])
    assert summary["exit_code"] == 1 == returncode
    assert cf["reasons"], "a control_invalid verdict must carry a machine-readable reason"
    assert [c["arm"] for c in executor.calls] == ["candidate", "base"]


def test_control_invalid_base_pass_with_dirty_postcondition_is_not_a_valid_control(cf_repo, monkeypatch):
    plan = {
        "candidate": arm_pass,
        "base": lambda call: {
            "returncode": 0,
            "evidence": make_evidence(
                call, ordered_verified=True, postcondition_unexpected_changes=["path changed: x"]
            ),
        },
    }
    returncode, summary, _cf, _executor = run_cf(cf_repo, monkeypatch, plan)
    assert returncode == 1
    assert summary["verdict"] == "control_invalid"


def test_control_invalid_arm_exception_is_reported_not_swallowed_into_pass(cf_repo, monkeypatch):
    returncode, summary, cf, _executor = run_cf(
        cf_repo, monkeypatch, {"candidate": arm_pass, "base": RuntimeError("boom")}
    )
    assert returncode == 1
    assert summary["verdict"] == "runner_error"
    assert cf["reasons"] == ["unexpected_error:RuntimeError"]


# ---------------------------------------------------------------------------
# AC5: fail-closed preflight (no arm runs, never PASS)
# ---------------------------------------------------------------------------


def _assert_fail_closed(returncode, summary, executor, reason_prefix: str):
    assert returncode != 0
    assert summary is not None, "a fail-closed preflight must still leave a summary"
    assert summary["verdict"] == "preflight_failed"
    assert summary["exit_code"] == returncode
    reasons = summary["skill_text_counterfactual"]["reasons"]
    assert any(r.startswith(reason_prefix) for r in reasons), reasons
    assert executor.calls == []


def test_fail_closed_base_ref_unresolved(cf_repo, monkeypatch):
    for ref in ("no-such-ref", "--upload-pack=x", "../escape", " base-ref", "base-ref\n"):
        cf_repo.reset_outputs()
        returncode, summary, _cf, executor = run_cf(
            cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, argv=cf_repo.argv(base_ref=ref)
        )
        _assert_fail_closed(returncode, summary, executor, "base_ref_")


@pytest.mark.parametrize(
    "target,reason",
    [
        ("/etc/passwd", "target_path_absolute"),
        ("../outside/SKILL.md", "target_path_not_normalized"),
        (".claude/skills/../../etc/SKILL.md", "target_path_not_normalized"),
        (".claude/skills/fixture-skill/./SKILL.md", "target_path_not_normalized"),
        ("scripts/agent-ops/run_worktree_agent_runtime_smoke.py", "target_path_not_skill_md"),
        (".claude/skills/fixture-skill/references/guide.md", "target_path_not_skill_md"),
        (".claude/skills/fixture-skill/scripts/run.py", "target_path_not_skill_md"),
        (".claude/skills/fixture-skill/SKILL.md/extra", "target_path_not_skill_md"),
        (".claude\\skills\\fixture-skill\\SKILL.md", "target_path_invalid_character"),
        ("", "target_path_empty"),
    ],
)
def test_fail_closed_target_path_is_not_a_single_skill_md(cf_repo, monkeypatch, target, reason):
    returncode, summary, _cf, executor = run_cf(
        cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, argv=cf_repo.argv(skills=[target])
    )
    _assert_fail_closed(returncode, summary, executor, reason)


def test_fail_closed_multiple_treatment_paths_are_rejected(cf_repo, monkeypatch):
    other = ".claude/skills/other-skill/SKILL.md"
    returncode, summary, _cf, executor = run_cf(
        cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, argv=cf_repo.argv(skills=[SKILL_PATH, other])
    )
    _assert_fail_closed(returncode, summary, executor, "treatment_not_single_skill_md")


def test_fail_closed_target_missing_in_candidate_or_base(cf_repo, monkeypatch):
    returncode, summary, _cf, executor = run_cf(
        cf_repo, monkeypatch, DISCRIMINATIVE_PLAN,
        argv=cf_repo.argv(skills=[".claude/skills/absent-skill/SKILL.md"]),
    )
    _assert_fail_closed(returncode, summary, executor, "candidate_target_missing")


def test_fail_closed_target_missing_in_base_commit(tmp_path, monkeypatch):
    repo = CfRepo(tmp_path)
    only = ".claude/skills/only-candidate/SKILL.md"
    # a SKILL.md that exists only in the candidate commit
    _write(repo.candidate / only, "new skill\n")
    git(repo.candidate, "add", only)
    git(repo.candidate, "commit", "-q", "-m", "add only-candidate skill")
    returncode, summary, _cf, executor = run_cf(
        repo, monkeypatch, DISCRIMINATIVE_PLAN, argv=repo.argv(skills=[only])
    )
    _assert_fail_closed(returncode, summary, executor, "base_target_missing")


def test_fail_closed_dirty_candidate_worktree(cf_repo, monkeypatch):
    _write(cf_repo.candidate / "stray.txt", "untracked\n")
    returncode, summary, _cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    _assert_fail_closed(returncode, summary, executor, "candidate_worktree_dirty")
    assert cf_repo.arm_worktrees() == []


def test_fail_closed_modified_tracked_candidate_worktree(cf_repo, monkeypatch):
    _write(cf_repo.candidate / SKILL_PATH, "uncommitted edit\n")
    returncode, summary, _cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    _assert_fail_closed(returncode, summary, executor, "candidate_worktree_dirty")


def test_fail_closed_identical_candidate_and_base_blob(cf_repo, monkeypatch):
    returncode, summary, _cf, executor = run_cf(
        cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, argv=cf_repo.argv(base_ref=cf_repo.cand_sha)
    )
    _assert_fail_closed(returncode, summary, executor, "no_treatment_difference")


def test_fail_closed_symlink_and_executable_mode_targets(tmp_path, monkeypatch):
    repo = CfRepo(tmp_path)
    link_skill = ".claude/skills/link-skill/SKILL.md"
    exec_skill = ".claude/skills/exec-skill/SKILL.md"
    for path in (link_skill, exec_skill):
        _write(repo.candidate / path, "x\n")
    git(repo.candidate, "add", "-A")
    git(repo.candidate, "commit", "-q", "-m", "skills (base)")
    base = git(repo.candidate, "rev-parse", "HEAD")
    (repo.candidate / link_skill).unlink()
    (repo.candidate / link_skill).symlink_to("../fixture-skill/SKILL.md")
    (repo.candidate / exec_skill).chmod(0o755)
    git(repo.candidate, "add", "-A")
    git(repo.candidate, "commit", "-q", "-m", "symlink and exec mode (candidate)")
    cases = (
        (link_skill, "candidate_target_not_regular_file"),
        (exec_skill, "candidate_target_unexpected_mode"),
    )
    for path, reason in cases:
        repo.reset_outputs()
        returncode, summary, _cf, executor = run_cf(
            repo, monkeypatch, DISCRIMINATIVE_PLAN, argv=repo.argv(base_ref=base, skills=[path])
        )
        _assert_fail_closed(returncode, summary, executor, reason)


def test_fail_closed_control_worktree_construction_failure(cf_repo, monkeypatch):
    def broken(*_args, **_kwargs):
        raise smoke.CounterfactualPreflightError("control_worktree_create_failed", "simulated")

    monkeypatch.setattr(smoke, "_cf_create_arm_worktree", broken)
    returncode, summary, _cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    _assert_fail_closed(returncode, summary, executor, "control_worktree_create_failed")


def test_fail_closed_concurrent_runs_sharing_one_output_dir_let_exactly_one_run_proceed(
    cf_repo, monkeypatch, capsys
):
    """TOCTOU: both runs pass the ``prepare_output_dir`` check before either creates the dir."""
    monkeypatch.setattr(smoke, "_install_signal_handlers", lambda: None)  # main thread only
    # The runner's lazy sibling-module loader is not thread-safe (production concurrency is
    # cross-process); warm it so the two in-process threads exercise only the output-dir race.
    smoke._load_approval_contract()
    barrier = threading.Barrier(2)
    original_prepare = smoke.prepare_output_dir
    checked: list[str] = []

    def synced_prepare(output_dir):
        result = original_prepare(output_dir)
        assert result is None, "both runs must pass the check-only fast path"
        checked.append(threading.current_thread().name)
        barrier.wait(timeout=30)  # both passed the check; now race for the actual create
        return result

    monkeypatch.setattr(smoke, "prepare_output_dir", synced_prepare)
    owners: list[str] = []

    def arm_with_run_token(call):
        token = f"tok-{threading.current_thread().name}"
        owners.append(token)
        return {"returncode": 0, "evidence": make_evidence(call, ordered_verified=True, runtime_version=token)}

    def base_with_run_token(call):
        token = f"tok-{threading.current_thread().name}"
        owners.append(token)
        return {
            "returncode": 1,
            "evidence": make_evidence(call, ordered_verified=False, runtime_version=token),
        }

    executor = Executor({"candidate": arm_with_run_token, "base": base_with_run_token})
    monkeypatch.setattr(smoke, "execute_counterfactual_arm", executor)
    results: dict[str, object] = {}

    def run(name: str) -> None:
        try:
            results[name] = smoke.main(cf_repo.argv())
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions below
            results[name] = exc

    threads = [threading.Thread(target=run, args=(name,), name=name) for name in ("runA", "runB")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads), "a run hung"
    assert sorted(checked) == ["runA", "runB"], ("both runs passed the check before racing", results)
    assert set(results) == {"runA", "runB"}
    assert not any(isinstance(v, BaseException) for v in results.values()), results
    winners = [name for name, rc in results.items() if rc == 0]
    losers = [name for name, rc in results.items() if rc == smoke.EXIT_FAIL]
    assert len(winners) == 1 and len(losers) == 1, results
    winner, loser = winners[0], losers[0]
    # only the winner reached arm execution (one run's worth of arms), the loser never did
    assert [c["arm"] for c in executor.calls] == ["candidate", "base"]
    assert set(owners) == {f"tok-{winner}"} and f"tok-{loser}" not in owners
    err = capsys.readouterr().err
    assert "output directory already exists" in err
    # the winner's evidence/summary is composed of its own run only
    summary = json.loads(cf_repo.evidence.read_text(encoding="utf-8"))
    cf = summary["skill_text_counterfactual"]
    assert summary["verdict"] == cf["classification"] == "discriminative"
    for arm in ("candidate", "base"):
        assert cf["arms"][arm]["runtime_version"] == f"tok-{winner}"
    assert f"tok-{loser}" not in cf_repo.evidence.read_text(encoding="utf-8")
    assert f"tok-{loser}" not in (cf_repo.out_dir / "summary.md").read_text(encoding="utf-8")
    assert cf_repo.arm_worktrees() == []


def test_fail_closed_claim_refuses_existing_dir_file_and_symlink_without_touching_them(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    file_target = tmp_path / "file"
    file_target.write_text("keep", encoding="utf-8")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "does-not-exist")
    to_dir = tmp_path / "to-dir"
    to_dir.symlink_to(existing)
    for target in (existing, file_target, dangling, to_dir):
        error = smoke.claim_counterfactual_output_dir(target)
        assert error and "already exists" in error, target
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert file_target.read_text(encoding="utf-8") == "keep"
    assert dangling.is_symlink() and not dangling.exists()
    assert to_dir.is_symlink()
    fresh = tmp_path / "nested" / "fresh"
    assert smoke.claim_counterfactual_output_dir(fresh) is None and fresh.is_dir()
    assert smoke.claim_counterfactual_output_dir(fresh) is not None


def test_fail_closed_preexisting_output_dir_stops_before_any_arm_or_evidence_access(cf_repo, monkeypatch):
    cf_repo.out_dir.mkdir()
    (cf_repo.out_dir / "arms").mkdir()
    stale = cf_repo.out_dir / "arms" / "base.evidence.json"
    stale.write_text("{}", encoding="utf-8")
    returncode, summary, _cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    assert returncode == smoke.EXIT_FAIL and summary is None
    assert executor.calls == [] and cf_repo.arm_worktrees() == []
    assert stale.read_text(encoding="utf-8") == "{}"


def test_fail_closed_base_arm_execution_failure_is_not_pass(cf_repo, monkeypatch):
    returncode, summary, _cf, _executor = run_cf(
        cf_repo, monkeypatch, {"candidate": arm_pass, "base": OSError("cannot spawn")}
    )
    assert returncode == 1
    assert summary["verdict"] == "runner_error"


@pytest.mark.parametrize(
    "variant",
    [
        {"markers": ()},
        {"skill_command": None},
        {"skills": ()},
        {"base_ref": None},
        {"adapter": "claude-gpt", "extra": ("--claude-bin", sys.executable)},
    ],
    ids=[
        "no_ordered_marker", "no_skill_command", "base_ref_without_skill",
        "skill_without_base_ref", "claude_gpt_adapter",
    ],
)
def test_fail_closed_flag_combination_without_control_inputs_is_a_usage_error(cf_repo, monkeypatch, variant):
    executor = Executor(DISCRIMINATIVE_PLAN)
    monkeypatch.setattr(smoke, "execute_counterfactual_arm", executor)
    with pytest.raises(SystemExit) as raised:
        smoke.main(cf_repo.argv(**variant))
    assert raised.value.code == 2
    assert executor.calls == []
    assert cf_repo.arm_worktrees() == []


def test_fail_closed_target_path_validator_closed_set():
    assert smoke.validate_counterfactual_target_path(SKILL_PATH) is None
    assert smoke.validate_counterfactual_target_path(".claude/skills/a.b_c-1/SKILL.md") is None
    for bad in (
        None, 5, "", "SKILL.md", "/abs/.claude/skills/x/SKILL.md", "C:/x/.claude/skills/x/SKILL.md",
        ".claude/skills//x/SKILL.md", ".claude/skills/x/skill.md", ".claude/skills/x/SKILL.md\n",
        ".claude/skills/.hidden/SKILL.md", "docs/.claude/skills/x/SKILL.md",
        ".claude/skills/x/y/SKILL.md", "x\x00/SKILL.md",
    ):
        assert smoke.validate_counterfactual_target_path(bad) is not None, bad


# ---------------------------------------------------------------------------
# AC6: isolation
# ---------------------------------------------------------------------------


def test_isolation_arms_are_detached_clean_ephemeral_worktrees_differing_only_in_the_target_blob(
    cf_repo, monkeypatch
):
    seen: dict = {}

    def hook(arm, call):
        path = Path(call["worktree"])
        seen[arm] = {
            "name": path.name,
            "parent": path.parent.resolve(),
            "identity": smoke.verify_worktree_identity(str(path), str(cf_repo.root)),
        }

    returncode, _summary, cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, hook=hook)
    assert returncode == 0
    candidate_call, base_call = executor.calls
    for arm, call in (("candidate", candidate_call), ("base", base_call)):
        assert seen[arm]["parent"] == cf_repo.worktrees_dir.resolve()
        assert WORKTREE_NAME_RE.fullmatch(seen[arm]["name"]) and arm in seen[arm]["name"]
        assert seen[arm]["identity"] == os.path.realpath(call["worktree"])
        assert call["symbolic_head"] == "", "arms are detached"
        assert call["status"] == "", "arms start clean"
    assert seen["candidate"]["name"] != seen["base"]["name"]
    # candidate arm == captured candidate commit; BASE arm = one runner-owned commit on top of it
    assert candidate_call["head"] == cf_repo.cand_sha
    assert git(cf_repo.root, "rev-parse", f"{base_call['head']}^") == cf_repo.cand_sha
    assert base_call["head"] == cf["base_arm_commit_sha"]
    # tree proof: only the target SKILL.md differs, as the BASE blob
    diff = git(cf_repo.root, "diff-tree", "-r", "--no-renames", "--name-only", cf_repo.cand_sha, base_call["head"])
    assert diff.splitlines() == [SKILL_PATH]
    assert cf["tree_difference"] == {"candidate_arm": [], "base_arm": [SKILL_PATH]}
    assert cf_repo.blob(base_call["head"]) == cf_repo.blob(cf_repo.base_sha)
    assert candidate_call["skill_text"].startswith("CANDIDATE TEXT")
    assert base_call["skill_text"].startswith("BASE TEXT")
    # every other file is the CANDIDATE version in both arms (no revert of other files)
    assert candidate_call["extra_text"] == base_call["extra_text"] == "extra-candidate\n"


def test_isolation_original_candidate_worktree_head_and_status_are_unchanged(cf_repo, monkeypatch):
    head_before = git(cf_repo.candidate, "rev-parse", "HEAD")
    returncode, _summary, cf, _executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    assert returncode == 0
    assert git(cf_repo.candidate, "rev-parse", "HEAD") == head_before
    assert git(cf_repo.candidate, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(cf_repo.candidate, "symbolic-ref", "--short", "HEAD") == "worktree-candidate"
    assert cf["candidate_worktree_unchanged"] is True


def test_isolation_violation_when_the_original_candidate_worktree_is_touched(cf_repo, monkeypatch):
    def hook(arm, _call):
        if arm == "base":
            _write(cf_repo.candidate / "leaked.txt", "mutation\n")

    returncode, summary, cf, _executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, hook=hook)
    assert returncode == 1
    assert summary["verdict"] == "isolation_violation"
    assert cf["candidate_worktree_unchanged"] is False


def test_isolation_base_ref_is_resolved_once_and_later_ref_movement_changes_nothing(cf_repo, monkeypatch):
    def hook(arm, _call):
        if arm == "candidate":
            git(cf_repo.root, "branch", "-f", "base-ref", cf_repo.cand_sha)

    returncode, _summary, cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, hook=hook)
    assert returncode == 0
    assert git(cf_repo.root, "rev-parse", "base-ref") == cf_repo.cand_sha, "the ref really moved"
    assert cf["resolved_base_commit_sha"] == cf_repo.base_sha
    assert cf["base_skill_blob_id"] == cf_repo.blob(cf_repo.base_sha)
    assert executor.calls[1]["skill_text"].startswith("BASE TEXT")


def test_isolation_base_arm_commit_is_deterministic_for_identical_inputs(cf_repo, monkeypatch):
    commits = []
    for _ in range(2):
        cf_repo.reset_outputs()
        _rc, _summary, cf, _executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
        commits.append(cf["base_arm_commit_sha"])
    assert commits[0] == commits[1]
    assert git(cf_repo.root, "rev-parse", f"{commits[0]}^") == cf_repo.cand_sha


def test_isolation_both_arms_receive_identical_observability_flags(cf_repo, monkeypatch):
    extra = ("--scan-forbidden-markers", "--timeout-seconds", "77", "--max-turns", "5")
    returncode, _summary, _cf, executor = run_cf(
        cf_repo, monkeypatch, DISCRIMINATIVE_PLAN, argv=cf_repo.argv(extra=extra)
    )
    assert returncode == 0
    candidate_argv, base_argv = (call["argv"] for call in executor.calls)
    path_flags = ("--worktree=", "--output-dir=", "--evidence-json=")

    def strip(argv):
        return [a for a in argv if not a.startswith(path_flags)]

    assert strip(candidate_argv) == strip(base_argv)
    stripped = strip(candidate_argv)
    # observability flags are forced on / carried identically; counterfactual flags never leak
    assert "--require-clean-postcondition" in stripped
    assert "--scan-forbidden-markers" in stripped
    assert "--timeout-seconds=77" in stripped and "--max-turns=5" in stripped
    assert f"--expect-skill-command={SKILL_NAME}" in stripped
    assert stripped.count("--expect-ordered-marker=MARKER_ONE_2981") == 1
    assert not any(a.startswith("--skill-text-counterfactual") for a in stripped)
    # the three per-arm paths differ and point at the arm's own location
    for call in executor.calls:
        opts = _opts(call["argv"])
        assert opts["--worktree"] == [call["worktree"]]
        assert opts["--output-dir"][0].endswith(f"/arms/{call['arm']}")
        assert opts["--evidence-json"][0].endswith(f"/arms/{call['arm']}.evidence.json")
        assert opts["--repo-root"] == [str(cf_repo.root)]
    assert candidate_argv != base_argv


def test_isolation_flags_omitted_keep_the_default_namespace_and_argv_roundtrip():
    parser = smoke.build_parser()
    args = parser.parse_args(
        ["--runtime", "claude", "--mode", "structured", "--worktree", "w", "--prompt-file", "p", "--output-dir", "o"]
    )
    assert args.skill_text_counterfactual_base_ref is None
    assert args.skill_text_counterfactual_skill == []
    # an omitted-flag namespace round-trips to exactly the flags the caller gave
    argv = smoke._cf_namespace_to_argv(parser, args, exclude=set(), overrides={})
    assert sorted(argv) == sorted(
        ["--runtime=claude", "--mode=structured", "--worktree=w", "--prompt-file=p", "--output-dir=o"]
    )


# ---------------------------------------------------------------------------
# AC7: cleanup
# ---------------------------------------------------------------------------


def _make_foreign_worktrees(cf_repo: CfRepo) -> dict[str, Path]:
    foreign_registered = cf_repo.worktrees_dir / "issue-9999-foreign"
    git(cf_repo.root, "worktree", "add", "-q", "--detach", str(foreign_registered), cf_repo.base_sha)
    sentinel = foreign_registered / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    lookalike_registered = cf_repo.worktrees_dir / ("skill-text-cf-candidate-" + "0" * 32)
    git(cf_repo.root, "worktree", "add", "-q", "--detach", str(lookalike_registered), cf_repo.base_sha)
    lookalike_dir = cf_repo.worktrees_dir / "skill-text-cf-base-unregistered"
    lookalike_dir.mkdir()
    (lookalike_dir / "sentinel.txt").write_text("keep\n", encoding="utf-8")
    return {
        "foreign": foreign_registered,
        "lookalike_registered": lookalike_registered,
        "lookalike_dir": lookalike_dir,
    }


def _assert_foreign_untouched(foreign: dict[str, Path], cf_repo: CfRepo):
    assert (foreign["foreign"] / "sentinel.txt").read_text(encoding="utf-8") == "keep\n"
    assert foreign["lookalike_registered"].is_dir()
    assert (foreign["lookalike_dir"] / "sentinel.txt").exists()
    registered = cf_repo.registered_worktrees()
    assert os.path.realpath(foreign["foreign"]) in [os.path.realpath(p) for p in registered]
    assert os.path.realpath(foreign["lookalike_registered"]) in [os.path.realpath(p) for p in registered]


def _own_arm_leftovers(cf_repo: CfRepo, foreign: dict[str, Path]) -> list[str]:
    ignore = {foreign["lookalike_registered"].name, foreign["lookalike_dir"].name}
    return [name for name in cf_repo.arm_worktrees() if name not in ignore]


@pytest.mark.parametrize(
    "plan,expected_verdict",
    [
        (DISCRIMINATIVE_PLAN, "discriminative"),
        ({"candidate": arm_pass, "base": arm_pass}, "non_discriminative"),
        ({"candidate": arm_ordered_fail, "base": arm_pass}, "candidate_fail"),
        ({"candidate": arm_pass, "base": lambda call: {"returncode": None, "timed_out": True}}, "control_invalid"),
        ({"candidate": RuntimeError("boom"), "base": arm_pass}, "runner_error"),
        ({"candidate": smoke._TerminateRequested("signal 15"), "base": arm_pass}, "runner_error"),
    ],
    ids=["success", "non_pass_classification", "candidate_failure", "base_timeout", "exception", "termination"],
)
def test_cleanup_removes_exactly_the_runner_created_worktrees_on_every_path(
    cf_repo, monkeypatch, plan, expected_verdict
):
    foreign = _make_foreign_worktrees(cf_repo)
    returncode, summary, cf, _executor = run_cf(cf_repo, monkeypatch, plan)
    assert summary["verdict"] == expected_verdict
    assert _own_arm_leftovers(cf_repo, foreign) == []
    _assert_foreign_untouched(foreign, cf_repo)
    assert cf["cleanup"]["failures"] == []
    assert cf["cleanup"]["all_removed"] is True
    assert len(cf["cleanup"]["removed"]) == len(cf["cleanup"]["attempted"]) >= 2
    own = [Path(p).name for p in cf_repo.registered_worktrees() if Path(p).name.startswith("skill-text-cf-")]
    assert own == [foreign["lookalike_registered"].name]
    assert returncode == (0 if expected_verdict == "discriminative" else 1)


def test_cleanup_partial_construction_failure_removes_the_arm_already_created(cf_repo, monkeypatch):
    foreign = _make_foreign_worktrees(cf_repo)
    original = smoke._cf_create_arm_worktree
    state = {"calls": 0}

    def flaky(repo_root, candidate_head, arm, created):
        state["calls"] += 1
        path = original(repo_root, candidate_head, arm, created)
        if state["calls"] == 2:
            raise smoke.CounterfactualPreflightError("control_worktree_not_clean", f"after creating {Path(path).name}")
        return path

    monkeypatch.setattr(smoke, "_cf_create_arm_worktree", flaky)
    returncode, summary, cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    assert returncode == 1 and summary["verdict"] == "preflight_failed"
    assert executor.calls == []
    assert _own_arm_leftovers(cf_repo, foreign) == []
    _assert_foreign_untouched(foreign, cf_repo)
    assert cf["cleanup"]["failures"] == []


def test_cleanup_failure_is_recorded_with_relative_paths_and_blocks_a_passing_verdict(
    cf_repo, monkeypatch
):
    original_git = smoke._cf_git

    def refuse_remove(repo, *git_args, **kwargs):
        if git_args[:2] == ("worktree", "remove"):
            return 1, "", "simulated remove failure"
        return original_git(repo, *git_args, **kwargs)

    monkeypatch.setattr(smoke, "_cf_git", refuse_remove)
    returncode, summary, cf, _executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    assert returncode != 0
    assert summary["verdict"] == "cleanup_failed"
    assert cf["classification"] == "cleanup_failed"
    failures = cf["cleanup"]["failures"]
    assert len(failures) == 2
    for failure in failures:
        assert not os.path.isabs(failure["path"])
        assert re.fullmatch(rf"\.claude/worktrees/{WORKTREE_NAME_RE.pattern}", failure["path"])
        assert failure["reason"] == "path_still_exists"
    persisted = cf_repo.evidence.read_text(encoding="utf-8") + (cf_repo.out_dir / "summary.md").read_text(
        encoding="utf-8"
    )
    assert str(cf_repo.root) not in persisted and "<redacted>" not in json.dumps(failures)
    assert any(r.startswith("cleanup_failed:") for r in cf["reasons"])
    assert cf["cleanup"]["all_removed"] is False
    # test-owned recovery of the deliberately leaked ephemeral paths
    monkeypatch.undo()
    for name in cf_repo.arm_worktrees():
        git(cf_repo.root, "worktree", "remove", "--force", str(cf_repo.worktrees_dir / name))


def test_cleanup_failure_after_passing_arms_makes_verdict_classification_exit_and_errors_agree(
    cf_repo, monkeypatch
):
    original_git = smoke._cf_git

    def refuse_remove(repo, *git_args, **kwargs):
        if git_args[:2] == ("worktree", "remove"):
            return 1, "", "simulated remove failure"
        return original_git(repo, *git_args, **kwargs)

    monkeypatch.setattr(smoke, "_cf_git", refuse_remove)
    returncode, summary, cf, executor = run_cf(cf_repo, monkeypatch, DISCRIMINATIVE_PLAN)
    # both arms really produced a passing/ordered-failing discriminative observation ...
    assert [c["arm"] for c in executor.calls] == ["candidate", "base"]
    assert cf["ordered_evidence_match"]["candidate"]["verified"] is True
    assert cf["ordered_evidence_match"]["base"]["verified"] is False
    # ... yet every final-decision field reports the SAME non-pass verdict (no contradiction)
    assert summary["verdict"] == cf["classification"] == "cleanup_failed"
    assert returncode == summary["exit_code"] == 1
    cleanup_reasons = [r for r in cf["reasons"] if r.startswith("cleanup_failed:")]
    assert len(cleanup_reasons) == 2 == len(cf["cleanup"]["failures"])
    for failure in cf["cleanup"]["failures"]:
        assert any(failure["path"] in reason for reason in cleanup_reasons)
    assert len(summary["errors"]) == 1
    assert summary["errors"][0].startswith("cleanup_failed: ")
    assert all(failure["path"] in summary["errors"][0] for failure in cf["cleanup"]["failures"])
    persisted = json.loads(cf_repo.evidence.read_text(encoding="utf-8"))
    assert persisted["verdict"] == persisted["skill_text_counterfactual"]["classification"] == "cleanup_failed"
    monkeypatch.undo()
    for name in cf_repo.arm_worktrees():
        git(cf_repo.root, "worktree", "remove", "--force", str(cf_repo.worktrees_dir / name))


def test_cleanup_failure_keeps_the_other_verdicts_unchanged_and_exit_non_zero(cf_repo, monkeypatch):
    original_git = smoke._cf_git

    def refuse_remove(repo, *git_args, **kwargs):
        if git_args[:2] == ("worktree", "remove"):
            return 1, "", "simulated remove failure"
        return original_git(repo, *git_args, **kwargs)

    monkeypatch.setattr(smoke, "_cf_git", refuse_remove)
    returncode, summary, cf, _executor = run_cf(
        cf_repo, monkeypatch, {"candidate": arm_pass, "base": arm_pass}
    )
    assert returncode == 1
    assert summary["verdict"] == cf["classification"] == "non_discriminative"
    assert any(r.startswith("cleanup_failed:") for r in cf["reasons"])
    monkeypatch.undo()
    for name in cf_repo.arm_worktrees():
        git(cf_repo.root, "worktree", "remove", "--force", str(cf_repo.worktrees_dir / name))


def test_cleanup_refuses_paths_that_the_runner_does_not_own(cf_repo):
    foreign = _make_foreign_worktrees(cf_repo)
    created = [
        {"arm": "base", "path": str(foreign["foreign"])},
        {"arm": "candidate", "path": str(cf_repo.root)},
        {"arm": "base", "path": "/"},
    ]
    outcome = smoke._cf_cleanup_worktrees(str(cf_repo.root), created)
    assert outcome["all_removed"] is False
    assert [f["reason"] for f in outcome["failures"]] == ["refused_non_owned_path"] * 3
    _assert_foreign_untouched(foreign, cf_repo)
    assert cf_repo.root.is_dir() and (cf_repo.candidate / SKILL_PATH).exists()


# ---------------------------------------------------------------------------
# AC7 (cleanup, F2): an interrupted run stops its children BEFORE worktree cleanup
# ---------------------------------------------------------------------------


def _pid_gone(pid: int, *, strict: bool = False) -> bool:
    """True when ``pid`` no longer runs.  A zombie still answers ``kill(pid, 0)`` (it is only
    waiting for its parent / init to reap it), so non-strict mode accepts state ``Z``."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if strict:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state == "Z"


def _wait_until(predicate, *, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _kill_leftover_pids(piddir: Path) -> None:
    """Test-owned safety net: never leave a sleeping fake process behind if an assertion fails."""
    for name in ("child", "same_group", "other_session"):
        pid = _read_pid(piddir / f"{name}.pid")
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


FAKE_TREE_CHILD = textwrap.dedent(
    r"""
    import os, signal, subprocess, sys, time
    piddir = sys.argv[1]
    cooperative = sys.argv[2] == "cooperative"
    sleeper = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(600)"
    same_group = subprocess.Popen([sys.executable, "-c", sleeper, os.path.join(piddir, "same_group.pid")])
    other_session = subprocess.Popen(
        [sys.executable, "-c", sleeper, os.path.join(piddir, "other_session.pid")],
        start_new_session=True,
    )
    if cooperative:
        # models the arm runner: on SIGTERM it stops the runtime it started in another session
        def on_term(signum, frame):
            other_session.kill()
            os._exit(0)
        signal.signal(signal.SIGTERM, on_term)
    else:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(os.path.join(piddir, "child.pid"), "w").write(str(os.getpid()))
    time.sleep(600)
    """
)


@pytest.mark.parametrize(
    "sig,exception,behavior",
    [
        (signal.SIGTERM, smoke._TerminateRequested, "cooperative"),
        (signal.SIGINT, KeyboardInterrupt, "cooperative"),
        (signal.SIGTERM, smoke._TerminateRequested, "ignores_sigterm"),
        (signal.SIGINT, KeyboardInterrupt, "ignores_sigterm"),
    ],
    ids=["sigterm_cooperative", "sigint_cooperative", "sigterm_stubborn", "sigint_stubborn"],
)
def test_cleanup_interrupted_run_stops_the_child_group_and_its_descendants_then_reraises(
    tmp_path, sig, exception, behavior
):
    smoke._install_signal_handlers()
    piddir = tmp_path / "pids"
    piddir.mkdir()
    names = ("child", "same_group", "other_session")

    def send_when_running():
        if _wait_until(lambda: all(_read_pid(piddir / f"{n}.pid") for n in names), timeout=30):
            os.kill(os.getpid(), sig)

    sender = threading.Thread(target=send_when_running, daemon=True)
    sender.start()
    started = time.monotonic()
    try:
        with pytest.raises(exception):
            smoke._run(
                [sys.executable, "-c", FAKE_TREE_CHILD, str(piddir), behavior],
                timeout=120.0, term_grace=1.0,
            )
    finally:
        sender.join(timeout=5)
        pids = {n: _read_pid(piddir / f"{n}.pid") for n in names}
    try:
        assert all(pids.values()), pids
        assert time.monotonic() - started < 60
        # the direct child was reaped (strict), its group descendants are gone
        assert _pid_gone(pids["child"], strict=True), "the direct child must be reaped"
        assert _pid_gone(pids["same_group"])
        if behavior == "cooperative":
            assert _pid_gone(pids["other_session"]), "the cooperating child stopped its own session"
    finally:
        _kill_leftover_pids(piddir)  # also reclaims the stubborn case's escaped-session sleeper


def test_cleanup_interrupted_run_reaps_the_direct_child_without_leaving_a_zombie(tmp_path):
    smoke._install_signal_handlers()
    piddir = tmp_path / "pids"
    piddir.mkdir()
    names = ("child", "same_group", "other_session")

    def send_when_running():
        if _wait_until(lambda: all(_read_pid(piddir / f"{n}.pid") for n in names), timeout=30):
            os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_when_running, daemon=True)
    sender.start()
    try:
        with pytest.raises(smoke._TerminateRequested):
            smoke._run([sys.executable, "-c", FAKE_TREE_CHILD, str(piddir), "cooperative"], timeout=120.0)
        sender.join(timeout=5)
        child = _read_pid(piddir / "child.pid")
        assert child is not None
        assert _pid_gone(child, strict=True), "direct child must be fully reaped, not left as a zombie"
        for name in ("same_group", "other_session"):
            assert _wait_until(lambda n=name: _pid_gone(_read_pid(piddir / f"{n}.pid")), timeout=15), name
    finally:
        _kill_leftover_pids(piddir)


def test_cleanup_timeout_path_still_kills_the_group_and_reports_timed_out(tmp_path):
    piddir = tmp_path / "pids"
    piddir.mkdir()
    try:
        rc, _out, _err, timed_out = smoke._run(
            [sys.executable, "-c", FAKE_TREE_CHILD, str(piddir), "cooperative"], timeout=2.0
        )
        assert timed_out is True and rc is None
        child = _read_pid(piddir / "child.pid")
        assert child is not None and _pid_gone(child, strict=True)
        same_group = _read_pid(piddir / "same_group.pid")
        assert same_group is not None and _wait_until(lambda: _pid_gone(same_group), timeout=15)
    finally:
        # an escaped-session descendant is outside the timeout contract: reclaim it ourselves
        _kill_leftover_pids(piddir)


def test_cleanup_keyboard_interrupt_is_runner_error_never_a_passing_verdict(cf_repo, monkeypatch):
    foreign = _make_foreign_worktrees(cf_repo)
    returncode, summary, cf, executor = run_cf(
        cf_repo, monkeypatch, {"candidate": KeyboardInterrupt(), "base": arm_pass}
    )
    assert returncode == 1
    assert summary["verdict"] == cf["classification"] == "runner_error"
    assert any("interrupted" in r for r in cf["reasons"])
    assert [c["arm"] for c in executor.calls] == ["candidate"]
    assert _own_arm_leftovers(cf_repo, foreign) == []
    _assert_foreign_untouched(foreign, cf_repo)
    assert cf["cleanup"]["all_removed"] is True
    assert (cf_repo.out_dir / "summary.md").exists()


BLOCKING_FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys, time
if "-p" in sys.argv:
    sys.stdin.read()
skill = os.path.join(os.getcwd(), ".claude", "skills", "fixture-skill", "SKILL.md")
try:
    text = open(skill, encoding="utf-8").read()
except OSError:
    text = ""
if "-p" in sys.argv and "BLOCK-FOREVER" in text:
    piddir = "__PIDDIR__"
    for name, value in (("claude", os.getpid()), ("arm_runner", os.getppid())):
        tmp = os.path.join(piddir, name + ".tmp")
        open(tmp, "w").write(str(value))
        os.replace(tmp, os.path.join(piddir, name + ".pid"))
    while True:
        time.sleep(1)
expansion = json.dumps({"hook_event_name": "UserPromptExpansion", "command_name": "fixture-skill",
                        "command_args": "", "prompt": "/fixture-skill"})
for event in (
    {"type": "system", "subtype": "init"},
    {"type": "system", "subtype": "hook_response", "hook_event": "UserPromptExpansion",
     "hook_name": "UserPromptExpansion", "stdout": expansion, "output": expansion},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "nothing"}]}},
    {"type": "result", "subtype": "success", "result": "nothing"},
):
    print(json.dumps(event))
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_cleanup_interrupting_the_top_level_runner_stops_arm_runner_and_runtime_before_worktree_removal(
    tmp_path, monkeypatch, sig
):
    piddir = tmp_path / "pids"
    piddir.mkdir()
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(BLOCKING_FAKE_CLAUDE.replace("__PIDDIR__", str(piddir)), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    repo = CfRepo(tmp_path)
    _commit_skill_texts(repo, "BASE: nothing.\n", "CANDIDATE: BLOCK-FOREVER.\n")
    foreign = _make_foreign_worktrees(repo)
    candidate_head = git(repo.candidate, "rev-parse", "HEAD")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), *repo.argv()],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(repo.root),
    )
    claude_pid = arm_runner_pid = None
    try:
        assert _wait_until(
            lambda: _read_pid(piddir / "claude.pid") and _read_pid(piddir / "arm_runner.pid"), timeout=90
        ), "the blocking fake claude never started"
        claude_pid = _read_pid(piddir / "claude.pid")
        arm_runner_pid = _read_pid(piddir / "arm_runner.pid")
        assert claude_pid and arm_runner_pid and not _pid_gone(claude_pid) and not _pid_gone(arm_runner_pid)
        # the arm worktree exists while the runtime is blocked: cleanup must wait for the stop
        assert len(_own_arm_leftovers(repo, foreign)) == 2
        proc.send_signal(sig)
        stdout, stderr = proc.communicate(timeout=90)
        # judged BEFORE the safety net below can hide a leak
        claude_stopped = _wait_until(lambda: _pid_gone(claude_pid), timeout=15)
        arm_runner_stopped = _wait_until(lambda: _pid_gone(arm_runner_pid), timeout=15)
        arm_worktrees_after_exit = _own_arm_leftovers(repo, foreign)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        for pid in (claude_pid, arm_runner_pid):
            if pid and not _pid_gone(pid):
                os.kill(pid, signal.SIGKILL)  # test-owned safety net only
    # (iv) non-zero exit, never a passing classification; (v) summary written
    assert proc.returncode not in (0, None), (stdout, stderr)
    summary = json.loads(repo.evidence.read_text(encoding="utf-8"))
    cf = summary["skill_text_counterfactual"]
    assert summary["verdict"] == cf["classification"] == "runner_error"
    assert summary["exit_code"] == proc.returncode
    assert (repo.out_dir / "summary.md").exists()
    # (i) neither the runtime nor the arm runner survives
    assert claude_stopped, "the fake claude runtime survived the interruption"
    assert arm_runner_stopped, "the arm runner survived the interruption"
    # (ii) runner-owned arm worktrees were reclaimed, (iii) foreign worktrees are untouched
    assert arm_worktrees_after_exit == [] == _own_arm_leftovers(repo, foreign)
    own_registered = [Path(p).name for p in repo.registered_worktrees() if Path(p).name.startswith("skill-text-cf-")]
    assert own_registered == [foreign["lookalike_registered"].name]
    assert cf["cleanup"]["failures"] == [] and cf["cleanup"]["all_removed"] is True
    _assert_foreign_untouched(foreign, repo)
    assert git(repo.candidate, "rev-parse", "HEAD") == candidate_head
    assert git(repo.candidate, "status", "--porcelain", "--untracked-files=all") == ""


# ---------------------------------------------------------------------------
# end-to-end with the real runner as arm child process and a fake claude whose output is
# determined ONLY by the SKILL.md text in its cwd
# ---------------------------------------------------------------------------

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
if "-p" in sys.argv:
    sys.stdin.read()
skill = os.path.join(os.getcwd(), ".claude", "skills", "fixture-skill", "SKILL.md")
try:
    text = open(skill, encoding="utf-8").read()
except OSError:
    text = ""
expansion = json.dumps({"hook_event_name": "UserPromptExpansion", "command_name": "fixture-skill",
                        "command_args": "", "prompt": "/fixture-skill"})
reply = "MARKER_ONE_2981 then MARKER_TWO_2981" if "EMIT-ORDERED" in text else "nothing to report"
for event in (
    {"type": "system", "subtype": "init"},
    {"type": "system", "subtype": "hook_response", "hook_event": "UserPromptExpansion",
     "hook_name": "UserPromptExpansion", "stdout": expansion, "output": expansion},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}},
    {"type": "result", "subtype": "success", "result": reply},
):
    print(json.dumps(event))
'''


def _install_fake_claude(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def _commit_skill_texts(repo: CfRepo, base_text: str, candidate_text: str) -> None:
    # rewrite history so base-ref / candidate differ only in the SKILL.md prose under test
    git(repo.candidate, "checkout", "-q", "--detach", repo.base_sha)
    _write(repo.candidate / SKILL_PATH, base_text)
    git(repo.candidate, "commit", "-q", "-am", "base text")
    git(repo.root, "branch", "-f", "base-ref", git(repo.candidate, "rev-parse", "HEAD"))
    _write(repo.candidate / SKILL_PATH, candidate_text)
    git(repo.candidate, "commit", "-q", "-am", "candidate text")
    git(repo.candidate, "checkout", "-q", "-B", "worktree-candidate")


def test_discriminative_end_to_end_runs_real_runner_arms_against_text_only_treatment(
    tmp_path, monkeypatch
):
    _install_fake_claude(tmp_path, monkeypatch)
    repo = CfRepo(tmp_path)
    _commit_skill_texts(repo, "BASE: say nothing.\n", "CANDIDATE: EMIT-ORDERED markers.\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *repo.argv()],
        capture_output=True, text=True, check=False, cwd=str(repo.root),
    )
    assert result.returncode == 0, result.stderr
    cf = json.loads(repo.evidence.read_text(encoding="utf-8"))["skill_text_counterfactual"]
    assert cf["classification"] == "discriminative"
    assert cf["ordered_evidence_match"]["candidate"]["verified"] is True
    assert cf["ordered_evidence_match"]["base"]["verified"] is False
    assert cf["arms"]["base"]["tested_head"] == cf["base_arm_commit_sha"]
    assert cf["arms"]["candidate"]["tested_head"] == cf["candidate_head_sha"]
    assert cf["cleanup"]["all_removed"] is True
    assert repo.arm_worktrees() == []


def test_non_discriminative_end_to_end_when_base_text_also_yields_the_marker(tmp_path, monkeypatch):
    _install_fake_claude(tmp_path, monkeypatch)
    repo = CfRepo(tmp_path)
    _commit_skill_texts(
        repo, "Intro v1. EMIT-ORDERED markers.\n", "Intro v2 reworded. EMIT-ORDERED markers.\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *repo.argv()],
        capture_output=True, text=True, check=False, cwd=str(repo.root),
    )
    assert result.returncode == 1, result.stderr
    summary = json.loads(repo.evidence.read_text(encoding="utf-8"))
    assert summary["verdict"] == "non_discriminative"
    assert summary["skill_text_counterfactual"]["ordered_evidence_match"]["base"]["verified"] is True
    assert repo.arm_worktrees() == []


# ---------------------------------------------------------------------------
# AC8: documentation contract
# ---------------------------------------------------------------------------

SKILL_DOC = REPO_ROOT / ".claude" / "skills" / "worktree-agent-runtime-smoke" / "SKILL.md"
POLICY_DOC = REPO_ROOT / "docs" / "dev" / "runtime-verification-policy.md"
VERDICT_ROWS = ("`discriminative`", "`non_discriminative`", "`control_invalid`", "候補 FAIL")


def _table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|")]


@pytest.mark.parametrize("doc", [SKILL_DOC, POLICY_DOC], ids=["skill", "policy"])
def test_docs_contract_decision_table_covers_every_verdict(doc):
    rows = _table_rows(doc.read_text(encoding="utf-8"))
    for verdict in VERDICT_ROWS:
        matching = [row for row in rows if row.split("|")[1].strip() == verdict]
        assert len(matching) == 1, (doc.name, verdict)
    exit_codes = {row.split("|")[1].strip(): row.split("|")[3].strip() for row in rows
                  if row.split("|")[1].strip() in VERDICT_ROWS}
    assert exit_codes["`discriminative`"] == "0"
    assert exit_codes["`non_discriminative`"].startswith("1")
    assert exit_codes["`control_invalid`"].startswith("1")


@pytest.mark.parametrize("doc", [SKILL_DOC, POLICY_DOC], ids=["skill", "policy"])
def test_docs_contract_states_limitations_and_one_sample_boundary(doc):
    text = doc.read_text(encoding="utf-8")
    assert "skill_text_counterfactual" in text
    assert "production script" in text and "BASE に戻らない" in text
    assert "`non_discriminative` になり得る" in text
    assert "複数 path" in text and "根拠にしない" in text
    assert "1 sample" in text and "統計的・因果的証明ではない" in text
    assert "入力同一性・closed classification・exit mapping" in text
    assert "N 回実行の harness は提供しない" in text


def test_docs_contract_skill_exception_is_limited_to_the_opt_in_mode():
    text = SKILL_DOC.read_text(encoding="utf-8")
    non_trigger = text.split("## Non-trigger", 1)[1].split("## Input", 1)[0]
    assert "worktree の新規作成／削除" in non_trigger
    assert "唯一の例外" in non_trigger and "skill_text_counterfactual" in non_trigger
    assert "この例外を他の mode へ広げない" in non_trigger
    boundary = text.split("## Safety Boundary", 1)[1].split("## Reference Map", 1)[0]
    assert "skill_text_counterfactual" in boundary and "exact path" in boundary
    assert "foreign worktree" in boundary
    section = text.split("## Skill Text Counterfactual", 1)[1].split("\n## 手順", 1)[0]
    assert "判定の所有" in section and "closed classification と exit mapping だけ" in section
    assert "--skill-text-counterfactual-base-ref" in text
    assert "--skill-text-counterfactual-skill" in text
    assert "--end-of-options" in section


def test_docs_contract_policy_adds_optional_procedure_without_changing_profile_assertions():
    text = POLICY_DOC.read_text(encoding="utf-8")
    assert "skill-invocation smoke の対照実行（任意の discrimination 証跡" in text
    assert "既存 profile の assertion 集合・適用要否" in text
    assert "`docs/dev/extension-surface-runtime-policy.yaml` も変更しない" in text
    policy_yaml = (REPO_ROOT / "docs" / "dev" / "extension-surface-runtime-policy.yaml").read_text(
        encoding="utf-8"
    )
    assert "skill_text_counterfactual" not in policy_yaml
    assert "procedure_steps_executed_in_declared_order" in policy_yaml


# ---------------------------------------------------------------------------
# selector hygiene for the Issue's ``-k`` Verification Commands
# ---------------------------------------------------------------------------


def _collected(expression: str) -> set[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(THIS_FILE), "--collect-only", "-q", "-k", expression,
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, check=False, cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return {line.split("::", 1)[1].split("[", 1)[0] for line in completed.stdout.splitlines() if "::" in line}


def test_selector_partition_every_ac_selector_selects_only_its_own_tests():
    ac_tokens = ["non_discriminative", "control_invalid", "fail_closed", "isolation", "cleanup", "docs_contract"]
    selected = {token: _collected(token) for token in ac_tokens}
    discriminative_only = _collected("discriminative and not non_discriminative")
    for token, names in {**selected, "discriminative_only": discriminative_only}.items():
        assert names, f"-k {token} selects no test"
    assert len(discriminative_only) >= 3
    assert selected["non_discriminative"] < _collected("discriminative")
    for token, names in selected.items():
        others = [t for t in ac_tokens if t != token]
        for name in names:
            assert token in name, (token, name)
            assert not any(other in name for other in others), (token, name)
            if token != "non_discriminative":
                assert "discriminative" not in name, (token, name)
    for name in discriminative_only:
        assert "discriminative" in name and not any(t in name for t in ac_tokens), name
