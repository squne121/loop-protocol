"""
tests/session_recording/codex/test_external_manifest_publish.py

Issue #1546: Codex Stop/SubagentStop session manifest canonical external
per-user state root migration. Covers AC1/AC2/AC6/AC7/AC8/AC9 (AC3/AC5 live
in scripts/agent-guards/tests/test_skill_runtime_exec_session_manifest.py;
AC4 is the plain `git diff` regression gate in the Issue's Verification
Commands; AC10 is "run the whole runtime-exec test module").
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "scripts" / "session-recording" / "codex-hook-adapter.mjs"
WRITER = REPO_ROOT / "scripts" / "session-recording" / "write-codex-session-manifest.mjs"
MANIFEST_VALIDATOR = REPO_ROOT / "scripts" / "validate-agent-session-manifest.mjs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex"


def run_adapter(event: str, payload, expect_exit: int = 0, env=None):
    result = subprocess.run(
        ["node", str(ADAPTER), "--event", event],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
        env=env,
    )
    assert result.returncode == expect_exit, result.stderr
    return result


def _isolated_env(state_home: Path) -> dict:
    env = os.environ.copy()
    env.pop("CODEX_HOOK_MANIFEST_ROOT", None)
    env["XDG_STATE_HOME"] = str(state_home)
    return env


def _git_status(repo_root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )
    return set(result.stdout.splitlines())


def _snapshot_dir(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_canonical_root_resolves_outside_repo_under_xdg_state_home(tmp_path: Path) -> None:
    """AC1: env-unset default resolves under XDG_STATE_HOME (absolute
    override case), and a relative XDG_STATE_HOME is ignored in favor of the
    spec default ($HOME/.local/state), never a repo-tree path."""
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())

    state_home = tmp_path / "state1"
    result = run_adapter("Stop", payload, env=_isolated_env(state_home))
    assert json.loads(result.stdout) == {"continue": True}
    assert result.stderr == ""

    manifests = list(state_home.glob("loop-protocol/session-manifests/v1/*/codex/stop/*.json"))
    assert len(manifests) == 1, manifests
    resolved = manifests[0].resolve()
    repo_root_real = REPO_ROOT.resolve()
    state_home_real = state_home.resolve()
    assert not (resolved == repo_root_real or str(resolved).startswith(f"{repo_root_real}{os.sep}"))
    assert str(resolved).startswith(f"{state_home_real}{os.sep}")

    # A relative XDG_STATE_HOME must be ignored (fall back to the spec
    # default). Isolate $HOME itself so the fallback lands under a
    # controlled tmp dir, never the real developer home.
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env2 = os.environ.copy()
    env2.pop("CODEX_HOOK_MANIFEST_ROOT", None)
    env2["XDG_STATE_HOME"] = "relative/not/absolute"
    env2["HOME"] = str(fake_home)
    result2 = run_adapter("Stop", payload, env=env2)
    assert json.loads(result2.stdout) == {"continue": True}
    assert result2.stderr == ""
    default_manifests = list(
        (fake_home / ".local" / "state").glob("loop-protocol/session-manifests/v1/*/codex/stop/*.json")
    )
    assert len(default_manifests) == 1, default_manifests


def test_stop_and_subagentstop_hooks_write_external_manifest_with_empty_repo_delta(tmp_path: Path) -> None:
    """AC2: real Stop and SubagentStop entrypoints each write exactly one
    valid external manifest, stderr is empty, and the repository before/after
    delta is zero."""
    state_home = tmp_path / "state"
    env = _isolated_env(state_home)
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())

    for event in ("Stop", "SubagentStop"):
        before = _git_status(REPO_ROOT)
        result = run_adapter(event, payload, env=env)
        assert json.loads(result.stdout) == {"continue": True}
        assert result.stderr == ""
        after = _git_status(REPO_ROOT)
        assert before == after, f"{event}: unexpected repo delta {after ^ before}"

    manifests = list(state_home.glob("loop-protocol/session-manifests/v1/*/codex/*/*.json"))
    events_seen = sorted({m.parent.name for m in manifests})
    assert events_seen == ["stop", "subagentstop"]
    assert len(manifests) == 2

    for manifest_path in manifests:
        validation = subprocess.run(
            ["node", str(MANIFEST_VALIDATOR), str(manifest_path.parent)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert validation.returncode == 0, validation.stderr


def test_create_once_publish_rejects_overwrite_and_leaves_no_temp_residue(tmp_path: Path) -> None:
    """AC6: a colliding final-path write is rejected without overwriting the
    existing final manifest, and no `.tmp` residue is left behind."""
    state_home = tmp_path / "state"
    script = tmp_path / "harness.mjs"
    script.write_text(
        "import { writeCodexSessionManifest } from " + json.dumps(str(WRITER)) + "\n"
        "const env = { XDG_STATE_HOME: " + json.dumps(str(state_home)) + " }\n"
        "const common = {\n"
        "  repoRoot: " + json.dumps(str(REPO_ROOT)) + ",\n"
        "  eventName: 'Stop',\n"
        "  fileName: 'fixed-collision-name.json',\n"
        "  env,\n"
        "}\n"
        "const first = writeCodexSessionManifest({ manifest: { n: 1 }, ...common })\n"
        "let secondError = null\n"
        "try {\n"
        "  writeCodexSessionManifest({ manifest: { n: 2 }, ...common })\n"
        "} catch (err) {\n"
        "  secondError = { name: err.name, message: err.message }\n"
        "}\n"
        "process.stdout.write(JSON.stringify({ first, secondError }))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(script)], cwd=REPO_ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["secondError"] is not None
    assert out["secondError"]["name"] == "ManifestPublishConflictError"

    directory = Path(out["first"]["directory"])
    entries = list(directory.iterdir())
    assert entries == [Path(out["first"]["absolutePath"])], entries
    assert not any(entry.name.endswith(".tmp") for entry in entries)

    manifest_content = json.loads(Path(out["first"]["absolutePath"]).read_text())
    assert manifest_content == {"n": 1}


def test_concurrent_writers_produce_distinct_manifests_without_overwrite(tmp_path: Path) -> None:
    """AC7: N concurrent Stop writers each produce a distinct, fully valid
    manifest -- no duplicate collision, no partial/truncated JSON, no
    overwrite of an existing final manifest."""
    state_home = tmp_path / "state"
    env = _isolated_env(state_home)
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())

    writer_count = 8
    results: list[subprocess.CompletedProcess | None] = [None] * writer_count

    def _run(index: int) -> None:
        results[index] = run_adapter("Stop", payload, env=env)

    threads = [threading.Thread(target=_run, args=(index,)) for index in range(writer_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for result in results:
        assert result is not None
        assert json.loads(result.stdout) == {"continue": True}
        assert result.stderr == ""

    manifests = list(state_home.glob("loop-protocol/session-manifests/v1/*/codex/stop/*.json"))
    assert len(manifests) == writer_count, manifests
    for manifest_path in manifests:
        # A truncated/partial concurrent write would fail JSON parsing here.
        json.loads(manifest_path.read_text())


def test_evidence_source_ref_resolves_to_canonical_external_final_file(tmp_path: Path) -> None:
    """AC8: evidence[].source_ref and secret_policy.runtime_boundary.evidence_ref
    agree, are a state-root-relative locator (no raw home/repo absolute path,
    no `..`), and resolve to the exact external final manifest file."""
    state_home = tmp_path / "state"
    env = _isolated_env(state_home)
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())

    result = run_adapter("Stop", payload, env=env)
    assert json.loads(result.stdout) == {"continue": True}

    manifests = list(state_home.glob("loop-protocol/session-manifests/v1/*/codex/stop/*.json"))
    assert len(manifests) == 1, manifests
    manifest_path = manifests[0]
    manifest = json.loads(manifest_path.read_text())

    evidence_source_ref = manifest["evidence"][0]["source_ref"]
    runtime_boundary_ref = manifest["secret_policy"]["runtime_boundary"]["evidence_ref"]
    assert evidence_source_ref == runtime_boundary_ref

    assert not evidence_source_ref.startswith("/")
    assert ".." not in Path(evidence_source_ref).parts

    resolved = (state_home / evidence_source_ref).resolve()
    assert resolved == manifest_path.resolve()


def test_no_new_writes_to_legacy_repo_local_session_manifest_root(tmp_path: Path) -> None:
    """AC9: env-unset (isolated) writers never create new files under the
    legacy repo-local tmp/session-manifests/codex/** root."""
    legacy_root = REPO_ROOT / "tmp" / "session-manifests" / "codex"
    before = _snapshot_dir(legacy_root)

    state_home = tmp_path / "state"
    env = _isolated_env(state_home)
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())
    for event in ("Stop", "SubagentStop"):
        result = run_adapter(event, payload, env=env)
        assert json.loads(result.stdout) == {"continue": True}

    after = _snapshot_dir(legacy_root)
    assert after == before
