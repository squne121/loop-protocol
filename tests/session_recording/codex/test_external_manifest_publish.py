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
import shutil
import subprocess
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "scripts" / "session-recording" / "codex-hook-adapter.mjs"
WRITER = REPO_ROOT / "scripts" / "session-recording" / "write-codex-session-manifest.mjs"
RESOLVER = REPO_ROOT / "scripts" / "session-recording" / "resolve-codex-session-manifest-root.mjs"
MANIFEST_VALIDATOR = REPO_ROOT / "scripts" / "validate-agent-session-manifest.mjs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex"


def _run_node_harness(tmp_path: Path, name: str, body: str):
    script = tmp_path / f"{name}.mjs"
    script.write_text(body, encoding="utf-8")
    return subprocess.run(
        ["node", str(script)], cwd=REPO_ROOT, text=True, capture_output=True, check=False
    )


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


def test_state_home_symlink_into_repo_is_rejected(tmp_path: Path) -> None:
    """AC1 (Issue #1546 OWNER Blocker 3): a *static, pre-existing* symlink at
    XDG_STATE_HOME that resolves inside the repository tree is rejected
    fail-closed on realpath containment -- not accepted because the lexical
    (un-resolved) XDG_STATE_HOME string looks like it is outside the repo."""
    repo_internal_target = tmp_path / "repo-internal-state"
    repo_internal_target.mkdir()
    symlinked_state_home = tmp_path / "state-symlink"
    symlinked_state_home.symlink_to(repo_internal_target)

    result = _run_node_harness(
        tmp_path,
        "state_home_symlink_ancestor",
        "import { resolveCanonicalExternalStateRoot } from " + json.dumps(str(RESOLVER)) + "\n"
        "try {\n"
        "  resolveCanonicalExternalStateRoot({\n"
        "    env: { XDG_STATE_HOME: " + json.dumps(str(symlinked_state_home)) + " },\n"
        # Use tmp_path's own parent as a stand-in "repo root" whose realpath
        # equals repo_internal_target's realpath ancestor chain -- simpler
        # and just as decisive: point repoRoot AT the symlink's target so a
        # correct implementation must detect the ancestor-is-symlink escape
        # even though the target itself is genuinely "outside" a naive
        # non-realpath repo comparison.
        "    repoRoot: " + json.dumps(str(repo_internal_target)) + ",\n"
        "  })\n"
        "  process.stdout.write(JSON.stringify({ rejected: false }))\n"
        "} catch (err) {\n"
        "  process.stdout.write(JSON.stringify({ rejected: true, reasonCode: err.reasonCode ?? null, name: err.name }))\n"
        "}\n",
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["rejected"] is True, out
    assert out["reasonCode"] in ("state_home_ancestor_is_symlink", "state_home_resolves_inside_repo"), out


def test_manifest_root_override_inside_repo_via_symlink_is_rejected(tmp_path: Path) -> None:
    """AC1 (Issue #1546 OWNER Blocker 3): a CODEX_HOOK_MANIFEST_ROOT
    override whose *lexical* path looks outside the repo, but whose realpath
    (via a pre-existing symlink) resolves inside the repository, is
    rejected -- the override validator must not trust `resolve()` alone."""
    inside_repo_dir = REPO_ROOT / "tmp" / "override-symlink-target-1546"
    inside_repo_dir.mkdir(parents=True, exist_ok=True)
    override_symlink = tmp_path / "override-symlink"
    try:
        override_symlink.symlink_to(inside_repo_dir)

        result = _run_node_harness(
            tmp_path,
            "override_symlink_into_repo",
            "import { validateManifestRootOverride } from " + json.dumps(str(RESOLVER)) + "\n"
            "try {\n"
            "  validateManifestRootOverride({\n"
            "    overrideRoot: " + json.dumps(str(override_symlink)) + ",\n"
            "    repoRoot: " + json.dumps(str(REPO_ROOT)) + ",\n"
            "  })\n"
            "  process.stdout.write(JSON.stringify({ rejected: false }))\n"
            "} catch (err) {\n"
            "  process.stdout.write(JSON.stringify({ rejected: true, reasonCode: err.reasonCode ?? null }))\n"
            "}\n",
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["rejected"] is True, out
        assert out["reasonCode"] in ("override_ancestor_is_symlink", "override_resolves_inside_repo"), out
    finally:
        override_symlink.unlink(missing_ok=True)
        shutil.rmtree(inside_repo_dir, ignore_errors=True)


def test_manifest_root_override_non_private_existing_directory_is_rejected(tmp_path: Path) -> None:
    """AC1: an existing CODEX_HOOK_MANIFEST_ROOT override directory whose
    mode is not exactly 0700 is rejected (`override_not_private`)."""
    override_dir = tmp_path / "override-not-private"
    override_dir.mkdir(mode=0o755)
    os.chmod(override_dir, 0o755)  # mkdir's mode arg is umask-adjusted; force it explicitly.

    result = _run_node_harness(
        tmp_path,
        "override_not_private",
        "import { validateManifestRootOverride } from " + json.dumps(str(RESOLVER)) + "\n"
        "try {\n"
        "  validateManifestRootOverride({\n"
        "    overrideRoot: " + json.dumps(str(override_dir)) + ",\n"
        "    repoRoot: " + json.dumps(str(REPO_ROOT)) + ",\n"
        "  })\n"
        "  process.stdout.write(JSON.stringify({ rejected: false }))\n"
        "} catch (err) {\n"
        "  process.stdout.write(JSON.stringify({ rejected: true, reasonCode: err.reasonCode ?? null }))\n"
        "}\n",
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out == {"rejected": True, "reasonCode": "override_not_private"}


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


def _fault_injection_harness_body(state_home: Path, fault_step: str) -> str:
    """Issue #1546 OWNER Blocker 2: build a Node harness that injects a
    single-step fault into writeCodexSessionManifest()'s fsOps and asserts
    (a) the original error propagates undisguised and (b) no `.tmp` residue
    survives. `fault_step` selects which real node:fs primitive gets
    wrapped to throw after delegating to (or, for write, truncating) the
    real implementation."""
    fault_snippets = {
        "write": (
            "writeSync: (fd, buffer, offset, length) => {\n"
            "    throw new Error('injected writeSync failure')\n"
            "  },\n"
        ),
        "fsync": (
            "fsyncSync: (fd) => {\n"
            "    throw new Error('injected fsyncSync failure')\n"
            "  },\n"
        ),
        "link": (
            "linkSync: (src, dest) => {\n"
            "    throw new Error('injected linkSync failure')\n"
            "  },\n"
        ),
        "close": (
            "closeSync: (fd) => {\n"
            "    throw new Error('injected closeSync failure')\n"
            "  },\n"
        ),
        "unlink": (
            "unlinkSync: (p) => {\n"
            "    throw new Error('injected unlinkSync failure')\n"
            "  },\n"
        ),
    }
    return (
        "import { writeCodexSessionManifest, defaultFsOps } from " + json.dumps(str(WRITER)) + "\n"
        "const fsOps = { ...defaultFsOps, " + fault_snippets[fault_step] + " }\n"
        "let thrown = null\n"
        "try {\n"
        "  writeCodexSessionManifest({\n"
        "    manifest: { n: 1 },\n"
        "    repoRoot: " + json.dumps(str(REPO_ROOT)) + ",\n"
        "    eventName: 'Stop',\n"
        "    fileName: 'fault-injection.json',\n"
        "    env: { XDG_STATE_HOME: " + json.dumps(str(state_home)) + " },\n"
        "    fsOps,\n"
        "  })\n"
        "} catch (err) {\n"
        "  thrown = { name: err.name, message: err.message }\n"
        "}\n"
        "process.stdout.write(JSON.stringify({ thrown }))\n"
    )


def test_write_failure_propagates_and_leaves_no_temp_residue(tmp_path: Path) -> None:
    """AC6 (Issue #1546 OWNER Blocker 2): a writeSync() failure propagates
    undisguised and leaves no `.tmp` residue behind."""
    state_home = tmp_path / "state"
    result = _run_node_harness(
        tmp_path, "fault_write", _fault_injection_harness_body(state_home, "write")
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["thrown"] is not None
    assert "injected writeSync failure" in out["thrown"]["message"]
    residue = list(state_home.glob("**/*.tmp")) if state_home.exists() else []
    assert residue == [], residue


def test_fsync_failure_propagates_and_leaves_no_temp_residue(tmp_path: Path) -> None:
    """AC6 (Issue #1546 OWNER Blocker 2): an fsyncSync() failure on the temp
    file propagates undisguised and leaves no `.tmp` residue behind."""
    state_home = tmp_path / "state"
    result = _run_node_harness(
        tmp_path, "fault_fsync", _fault_injection_harness_body(state_home, "fsync")
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["thrown"] is not None
    assert "injected fsyncSync failure" in out["thrown"]["message"]
    residue = list(state_home.glob("**/*.tmp")) if state_home.exists() else []
    assert residue == [], residue


def test_link_failure_propagates_and_leaves_no_temp_residue(tmp_path: Path) -> None:
    """AC6 (Issue #1546 OWNER Blocker 2): a non-EEXIST linkSync() failure
    propagates undisguised and leaves no `.tmp` residue -- and no final file
    is ever created."""
    state_home = tmp_path / "state"
    result = _run_node_harness(
        tmp_path, "fault_link", _fault_injection_harness_body(state_home, "link")
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["thrown"] is not None
    assert "injected linkSync failure" in out["thrown"]["message"]
    residue = list(state_home.glob("**/*.tmp")) if state_home.exists() else []
    assert residue == [], residue
    finals = list(state_home.glob("**/fault-injection.json")) if state_home.exists() else []
    assert finals == [], finals


def test_close_failure_propagates_and_leaves_no_temp_residue(tmp_path: Path) -> None:
    """AC6 (Issue #1546 OWNER Blocker 2): a closeSync() failure on the temp
    fd propagates undisguised (never swallowed) and leaves no `.tmp` residue
    behind -- the real content was still written+fsynced+linked
    successfully before close failed, so the final manifest DOES exist; only
    the reported error and temp-cleanup guarantee are under test here."""
    state_home = tmp_path / "state"
    result = _run_node_harness(
        tmp_path, "fault_close", _fault_injection_harness_body(state_home, "close")
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["thrown"] is not None
    assert "injected closeSync failure" in out["thrown"]["message"]
    residue = list(state_home.glob("**/*.tmp")) if state_home.exists() else []
    assert residue == [], residue


def test_unlink_failure_after_successful_publish_propagates(tmp_path: Path) -> None:
    """AC6 (Issue #1546 OWNER Blocker 2): an unlinkSync() failure on the
    temp file's final cleanup step, AFTER a successful publish (link
    succeeded), still propagates as the reported error -- it is never
    silently swallowed even though the final manifest content is already
    durably in place."""
    state_home = tmp_path / "state"
    result = _run_node_harness(
        tmp_path, "fault_unlink", _fault_injection_harness_body(state_home, "unlink")
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["thrown"] is not None
    assert "injected unlinkSync failure" in out["thrown"]["message"]
    # The final manifest was already successfully linked before the
    # injected unlink failure -- its content must still be intact.
    finals = list(state_home.glob("**/fault-injection.json"))
    assert len(finals) == 1, finals
    assert json.loads(finals[0].read_text()) == {"n": 1}


def test_final_manifest_is_a_regular_file_with_single_link_and_intact_content(tmp_path: Path) -> None:
    """AC6 (Issue #1546 OWNER Blocker 2): after a successful publish, the
    final file is a regular file (not a symlink/dir/other), has exactly one
    hard link (the temp name was unlinked, no other links point at the same
    inode), mode 0600, and its content is byte-for-byte the serialized
    manifest."""
    state_home = tmp_path / "state"
    script_body = (
        "import { writeCodexSessionManifest } from " + json.dumps(str(WRITER)) + "\n"
        "const result = writeCodexSessionManifest({\n"
        "  manifest: { hello: 'world' },\n"
        "  repoRoot: " + json.dumps(str(REPO_ROOT)) + ",\n"
        "  eventName: 'Stop',\n"
        "  fileName: 'integrity-check.json',\n"
        "  env: { XDG_STATE_HOME: " + json.dumps(str(state_home)) + " },\n"
        "})\n"
        "process.stdout.write(JSON.stringify(result))\n"
    )
    result = _run_node_harness(tmp_path, "integrity_check", script_body)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    final_path = Path(out["absolutePath"])

    st = final_path.lstat()
    assert not final_path.is_symlink()
    assert final_path.is_file()
    assert st.st_nlink == 1
    assert (st.st_mode & 0o777) == 0o600
    assert json.loads(final_path.read_text()) == {"hello": "world"}


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
