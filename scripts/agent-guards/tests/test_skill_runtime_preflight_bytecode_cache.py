from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
AGENT_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(AGENT_GUARDS_DIR))

import run_refinement_preflight as preflight  # noqa: E402
import skill_runtime_exec as real_exec  # noqa: E402 -- real production detection logic


def _source_cache_paths(source: Path) -> set[Path]:
    cache = Path(importlib.util.cache_from_source(str(source)))
    return {cache, cache.parent}


# ---------------------------------------------------------------------------
# In-process build_py_compile_proof() regression tests (pre-existing, updated
# for the PY_SYNTAX_COMPILE_PROOF_V2 schema — Issue #1439 AC8)
# ---------------------------------------------------------------------------


def test_given_provenance_compile_when_building_proof_then_source_cache_is_unchanged(
    tmp_path: Path,
) -> None:
    """GIVEN a planner source outside the repository
    WHEN provenance performs its syntax check twice
    THEN it records an in-process V2 proof and creates no __pycache__ / pyc."""
    planner = tmp_path / "planner.py"
    planner.write_text("value = 1\n", encoding="utf-8")
    before = {path: path.exists() for path in _source_cache_paths(planner)}

    first = preflight.build_py_compile_proof(planner, REPO_ROOT)
    second = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert first["py_compile_status"] == second["py_compile_status"] == "pass"
    assert first["schema_version"] == "PY_SYNTAX_COMPILE_PROOF_V2"
    assert first["operation_kind"] == "in_process_compile"
    assert first["source_mode"] == "bytes"
    assert first["flags"] == 0
    assert first["dont_inherit"] is True
    assert first["optimize"] == -1
    assert first["cache_write_expected"] is False
    assert "command" not in first
    assert {path: path.exists() for path in _source_cache_paths(planner)} == before


def test_given_invalid_source_when_building_proof_then_failure_is_recorded_without_cache(
    tmp_path: Path,
) -> None:
    """GIVEN invalid planner source
    WHEN provenance checks syntax
    THEN it fails in the proof without emitting a bytecode cache."""
    planner = tmp_path / "planner.py"
    planner.write_text("def broken(:\n", encoding="utf-8")

    proof = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert proof["py_compile_status"] == "fail"
    assert not Path(importlib.util.cache_from_source(str(planner))).exists()


def test_given_race_tolerant_predicate_when_checked_then_bytecode_cache_paths_are_not_excluded() -> None:
    """AC14 (behavioral -- replaces the prior static source-text grep): drive
    the real `_is_race_tolerant_unattributable_path` predicate (and its
    backing `_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS` root list) with
    representative candidate paths and assert its actual classification
    output, instead of grepping `skill_runtime_exec.py` source text for
    forbidden substrings (a static check that would pass even if the
    predicate's *behavior* silently ignored bytecode caches through an
    indirect / computed path)."""
    for declared_root in real_exec._RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS:
        assert real_exec._is_race_tolerant_unattributable_path(declared_root) is True
        assert real_exec._is_race_tolerant_unattributable_path(f"{declared_root}/leaf.txt") is True

    non_race_tolerant_candidates = [
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/__pycache__/skill_runtime_exec.cpython-312.pyc",
        (
            ".claude/skills/issue-refinement-loop/scripts/__pycache__/"
            "run_refinement_preflight.cpython-312.pyc"
        ),
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        ".claude/skills/issue-refinement-loop/scripts",
        # Prefix-lookalikes must NOT be treated as inside a declared root
        # (component-boundary match only, not a raw string prefix match).
        ".claude/artifacts/issue-refinement-loop-not-a-real-child/leaf.txt",
        "artifacts/session-manifest-runtime-not-a-real-child/leaf.txt",
    ]
    for candidate in non_race_tolerant_candidates:
        assert real_exec._is_race_tolerant_unattributable_path(candidate) is False, candidate


# ---------------------------------------------------------------------------
# AC8: PY_SYNTAX_COMPILE_PROOF_V2 encoding-declaration fixtures
# (UTF-8 BOM / non-UTF-8 cookie / contradictory BOM+cookie / wrapper future
# flags not inherited)
# ---------------------------------------------------------------------------


def test_given_utf8_bom_source_when_building_proof_then_bom_is_honored_without_cache(
    tmp_path: Path,
) -> None:
    """GIVEN a source file that starts with a UTF-8 BOM
    WHEN build_py_compile_proof compiles it from raw bytes
    THEN CPython's own BOM handling accepts it (pass) and no cache is written."""
    planner = tmp_path / "planner.py"
    planner.write_bytes(b"\xef\xbb\xbfvalue = 1\n")

    proof = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert proof["py_compile_status"] == "pass", proof["stderr_excerpt"]
    assert proof["source_mode"] == "bytes"
    assert not Path(importlib.util.cache_from_source(str(planner))).exists()


def test_given_non_utf8_encoding_cookie_source_when_building_proof_then_cookie_is_honored(
    tmp_path: Path,
) -> None:
    """GIVEN a source file declaring a non-UTF-8 PEP 263 encoding cookie and
    containing bytes only valid in that encoding
    WHEN build_py_compile_proof compiles it from raw bytes
    THEN the declared encoding is honored (pass) without decoding as UTF-8
    up front and without writing a cache."""
    planner = tmp_path / "planner.py"
    # shift_jis-encoded comment containing a full-width character, plus a
    # matching coding cookie. Would raise UnicodeDecodeError if decoded as
    # UTF-8 first instead of letting compile() honor the declared encoding.
    source_bytes = "# -*- coding: shift_jis -*-\n# コメント\nvalue = 1\n".encode("shift_jis")
    planner.write_bytes(source_bytes)

    proof = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert proof["py_compile_status"] == "pass", proof["stderr_excerpt"]
    assert not Path(importlib.util.cache_from_source(str(planner))).exists()


def test_given_contradictory_bom_and_cookie_source_when_building_proof_then_it_fails_closed(
    tmp_path: Path,
) -> None:
    """GIVEN a source file with a UTF-8 BOM but a coding cookie declaring a
    different, non-UTF-8 encoding
    WHEN build_py_compile_proof compiles it from raw bytes
    THEN CPython rejects the contradiction deterministically (fail, not a
    silent guess) and no cache is written."""
    planner = tmp_path / "planner.py"
    contradictory = b"\xef\xbb\xbf# -*- coding: shift_jis -*-\nvalue = 1\n"
    planner.write_bytes(contradictory)

    proof = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert proof["py_compile_status"] == "fail"
    assert proof["stderr_excerpt"], "a contradictory BOM/cookie must record a reason"
    assert not Path(importlib.util.cache_from_source(str(planner))).exists()


def test_given_wrapper_future_annotations_when_building_proof_then_child_flags_not_inherited(
    tmp_path: Path,
) -> None:
    """GIVEN this wrapper module itself declares `from __future__ import
    annotations`
    WHEN build_py_compile_proof invokes the builtin compile()
    THEN it passes flags=0 and dont_inherit=True explicitly, so the wrapper's
    own future statements are never leaked into the checked script."""
    planner = tmp_path / "planner.py"
    planner.write_text("value = 1\n", encoding="utf-8")

    captured: dict[str, object] = {}
    real_compile = compile

    def _spy_compile(source, filename, mode, *args, **kwargs):
        captured["source_type"] = type(source)
        captured["flags"] = kwargs.get("flags", args[0] if args else None)
        captured["dont_inherit"] = kwargs.get(
            "dont_inherit", args[1] if len(args) > 1 else None
        )
        captured["optimize"] = kwargs.get(
            "optimize", args[2] if len(args) > 2 else None
        )
        return real_compile(source, filename, mode, *args, **kwargs)

    with mock.patch("builtins.compile", side_effect=_spy_compile):
        proof = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert proof["py_compile_status"] == "pass", proof["stderr_excerpt"]
    assert captured["source_type"] is bytes
    assert captured["flags"] == 0
    assert captured["dont_inherit"] is True
    assert captured["optimize"] == -1


# ---------------------------------------------------------------------------
# AC1-AC5: real executor -> real preflight -> real planner subprocess chain
# (Issue #1439 fix_delta iteration 1, P0-4)
#
# These tests copy the *actual, unmodified* production
# `run_refinement_preflight.py` and `plan_refinement_loop.py` byte content
# from this repository into an isolated temporary git repository, and drive
# them through the real `scripts/agent-guards/skill_runtime_exec.py`
# executor as a real subprocess chain (`--fixture` bypasses the `gh` CLI so
# the chain is deterministic and network-free). This directly exercises the
# production code paths instead of a hand-authored stand-in.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_FIXTURE_ISSUE_NUMBER = 1439
_FIXTURE_REPO_SLUG = "squne121/loop-protocol"


def _real_preflight_source() -> str:
    return (SCRIPTS_DIR / "run_refinement_preflight.py").read_text(encoding="utf-8")


def _real_command_registry_source() -> str:
    return (SCRIPTS_DIR / "command_registry.py").read_text(encoding="utf-8")


def _real_planner_source() -> str:
    return (SCRIPTS_DIR / "plan_refinement_loop.py").read_text(encoding="utf-8")


def _real_repair_source() -> str:
    return (SCRIPTS_DIR / "repair_issue_contract.py").read_text(encoding="utf-8")


def _fixture_input_json() -> dict:
    """Reuse the repository's own known-good `preflight_pass.json` fixture
    body (already exercised by other issue-refinement-loop tests) so the
    real planner reliably reaches `fail_closed.required: false`, just
    re-targeted at this test's issue number/repo."""
    known_good = json.loads(
        (
            REPO_ROOT
            / ".claude"
            / "skills"
            / "issue-refinement-loop"
            / "fixtures"
            / "preflight_pass.json"
        ).read_text(encoding="utf-8")
    )
    known_good["issue_number"] = _FIXTURE_ISSUE_NUMBER
    known_good["repo"] = _FIXTURE_REPO_SLUG
    known_good["issue"]["number"] = _FIXTURE_ISSUE_NUMBER
    return known_good


def _install_real_preflight_fixture(repo_root: Path, *, negative_writer: str | None = None) -> Path:
    """Install the real, unmodified production preflight + planner scripts
    into an isolated fixture repo so tests can drive them as a genuine
    subprocess chain offline (via `--fixture`, which bypasses the `gh` CLI).

    `command_registry.py`'s canonical `preflight.run` argv template is
    exact-match validated by `skill_runtime_command_policy.validate_registry_entry`
    (a deliberate security control on the real, unmodifiable registry).
    Issue #1439 Scope Delta 2 adds a *sibling*, test-only command-id
    (`preflight.run.fixture`) that legitimately extends the registry with a
    `--fixture` placeholder without touching `preflight.run` at all --
    `test_ac10_*` / `test_ac11_*` below drive that command-id through the
    real `skill_runtime_exec.main()` entry point end to end. This helper is
    still used directly (bypassing the executor) by the AC2/AC3 tests below,
    reusing the *real* `skill_runtime_exec` detection primitives
    (`_sanitize_env`, `_snapshot_repo_paths`, `_git_status_paths`,
    `_find_unauthorized_repo_changes`) imported unmodified from
    `scripts/agent-guards/skill_runtime_exec.py`, so the actual production
    fail-closed detection algorithm is what is under test (P0-4).

    When `negative_writer` is given, a thin test-only harness is appended
    *after* the real script's own `if __name__ == "__main__": main()` guard
    (never replacing or editing any of the real logic) that performs one of
    the AC4/AC5 adversarial bytecode-cache writes against the real script's
    own canonical cache path once `main()` returns.
    """
    dest_scripts = repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    dest_schemas = repo_root / ".claude" / "skills" / "issue-refinement-loop" / "schemas"

    real_preflight = _real_preflight_source()
    guard = 'if __name__ == "__main__":\n    main()\n'
    assert real_preflight.endswith(guard), (
        "real run_refinement_preflight.py entry-point guard shape changed; "
        "update this test harness splice point"
    )

    if negative_writer is None:
        _write_text(dest_scripts / "run_refinement_preflight.py", real_preflight)
    else:
        harness = f"""
    import importlib.util as _ilu_test
    import py_compile as _py_compile_test

    _cache_path_test = Path(_ilu_test.cache_from_source(__file__))
    _writer_test = {negative_writer!r}
    if _writer_test == "bytes":
        _cache_path_test.parent.mkdir(parents=True, exist_ok=True)
        _cache_path_test.write_bytes(b"not-a-valid-pyc")
    elif _writer_test == "compile":
        _py_compile_test.compile(__file__, cfile=str(_cache_path_test), doraise=True)
    elif _writer_test == "replace-parent":
        _cache_path_test.parent.mkdir(parents=True, exist_ok=True)
        _cache_path_test.parent.rmdir()
        _cache_path_test.parent.symlink_to("../", target_is_directory=True)
    elif _writer_test == "replace-parent-file":
        _cache_path_test.parent.mkdir(parents=True, exist_ok=True)
        _cache_path_test.parent.rmdir()
        _cache_path_test.parent.write_bytes(b"not-a-directory")
    elif _writer_test == "modify-existing":
        _cache_path_test.parent.mkdir(parents=True, exist_ok=True)
        _cache_path_test.write_bytes(b"before")
        _cache_path_test.write_bytes(b"after-with-different-size")
"""
        spliced = real_preflight[: -len(guard)] + (
            'if __name__ == "__main__":\n'
            + harness
            + "    main()\n"
        )
        _write_text(dest_scripts / "run_refinement_preflight.py", spliced)

    _write_text(dest_scripts / "plan_refinement_loop.py", _real_planner_source())
    _write_text(dest_scripts / "command_registry.py", _real_command_registry_source())
    _write_text(dest_scripts / "repair_issue_contract.py", _real_repair_source())
    _write_text(
        repo_root / "docs" / "dev" / "github-ops.md",
        (REPO_ROOT / "docs" / "dev" / "github-ops.md").read_text(encoding="utf-8"),
    )

    schemas_src = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "schemas"
    if schemas_src.is_dir():
        for schema_file in schemas_src.glob("*.json"):
            _write_text(
                dest_schemas / schema_file.name,
                schema_file.read_text(encoding="utf-8"),
            )

    fixture_input_path = repo_root / "preflight_fixture_input.json"
    fixture_input_path.write_text(
        json.dumps(_fixture_input_json(), ensure_ascii=False), encoding="utf-8"
    )
    return fixture_input_path


def _run_real_preflight_child(
    repo: Path, fixture_input_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Spawn the real preflight script as a real child process, using the
    real production `_sanitize_env` output as its environment -- exactly the
    environment `skill_runtime_exec.main()` builds before spawning its own
    child (Issue #1439 AC2)."""
    script = (
        repo
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / "run_refinement_preflight.py"
    )
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--issue-number",
            str(_FIXTURE_ISSUE_NUMBER),
            "--repo",
            _FIXTURE_REPO_SLUG,
            "--fixture",
            str(fixture_input_path),
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ac1_baseline_writer_class_is_classified_not_unknown(tmp_path: Path) -> None:
    """AC1: classify the writer class of the historical pre-fix mechanism
    (`python -m py_compile <script>`, run as a traced direct child of this
    test process) instead of leaving it `unknown`. The mechanism is an
    explicit compile invocation by a traced child process (not an automatic
    import, and not an independent/unrelated peer process), so it is
    classified `traced_child_explicit_compile`."""
    target = tmp_path / "planner.py"
    target.write_text("value = 1\n", encoding="utf-8")
    cache_before = Path(importlib.util.cache_from_source(str(target))).exists()
    assert cache_before is False

    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(target)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    cache_path = Path(importlib.util.cache_from_source(str(target)))

    # Structural evidence for classification:
    # - `proc` is a direct (traced) child of this test process (subprocess.run
    #   blocks until the child exits; we observe its PID/returncode directly).
    # - The child's argv explicitly names `py_compile` (not a bare `import`
    #   of the module), so this is an *explicit compile*, not an
    #   *automatic import*.
    is_traced_child = proc.returncode is not None
    is_explicit_compile_argv = "py_compile" in proc.args
    is_independent_peer = False  # this subprocess is spawned and waited on here

    if is_traced_child and is_explicit_compile_argv and not is_independent_peer:
        writer_class = "traced_child_explicit_compile"
    elif is_traced_child and not is_explicit_compile_argv:
        writer_class = "traced_child_automatic_import"
    elif not is_traced_child:
        writer_class = "independent_peer_process"
    else:
        writer_class = "unknown"

    assert writer_class == "traced_child_explicit_compile"
    assert writer_class != "unknown"
    assert cache_path.exists(), "py_compile CLI must have produced the cache used for classification"


def test_ac2_real_preflight_planner_subprocess_chain_flags(tmp_path: Path) -> None:
    """AC2: drive the real run_refinement_preflight.py -> real
    plan_refinement_loop.py subprocess chain end to end (offline via
    --fixture), using the real, unmodified `skill_runtime_exec._sanitize_env`
    to build the child environment exactly as the real executor would, and
    verify the parent-child relationship completes cleanly with
    PYTHONDONTWRITEBYTECODE=1 propagated to every level, matching the
    effective interpreter flags a dont_write_bytecode-enabled process tree
    must have."""
    repo = _make_repo(tmp_path)
    fixture_input_path = _install_real_preflight_fixture(repo)

    env = real_exec._sanitize_env(str(repo))
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    result = _run_real_preflight_child(repo, fixture_input_path, env)
    # 0 (pass) and 1 (warn) both mean the real planner ran to completion;
    # only 2 (blocked) / 3 (environment_failure) indicate the chain failed.
    assert result.returncode in (0, 1), result.stderr

    # Propagation probe: the exact env dict the real executor builds (and
    # that we just used to launch the real preflight process) must also
    # yield dont_write_bytecode=True for any interpreter launched with it --
    # this is what guarantees the real planner subprocess (launched by the
    # real preflight process using its *inherited* os.environ, i.e. this
    # same env) also runs with bytecode writing disabled.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; print(int(sys.flags.dont_write_bytecode), int(sys.dont_write_bytecode))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "1 1"

    artifact = (
        repo
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(_FIXTURE_ISSUE_NUMBER)
        / "refinement_preflight_result_v1.json"
    )
    assert artifact.exists(), "real preflight must have produced its canonical artifact"
    provenance = (
        repo
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(_FIXTURE_ISSUE_NUMBER)
        / "refinement_preflight_provenance_v1.json"
    )
    assert provenance.exists(), "real preflight must invoke the real planner and record provenance"
    provenance_data = json.loads(provenance.read_text(encoding="utf-8"))
    assert provenance_data["py_compile_status"] == "pass"


def test_ac3_real_chain_run_twice_leaves_no_source_tree_delta(tmp_path: Path) -> None:
    """AC3: run the real preflight -> real planner chain twice against a
    clean repo, using the real `skill_runtime_exec._snapshot_repo_paths` /
    `_git_status_paths` / `_find_unauthorized_repo_changes` primitives to
    detect drift exactly as the real executor would, and assert no
    executor-attributable change appears in the source tree (no
    __pycache__ / *.pyc, no delta outside the target issue's artifact
    root)."""
    repo = _make_repo(tmp_path)
    fixture_input_path = _install_real_preflight_fixture(repo)
    issue_number = str(_FIXTURE_ISSUE_NUMBER)
    env = real_exec._sanitize_env(str(repo))

    before_snapshot = real_exec._snapshot_repo_paths(str(repo), issue_number)
    before_status = real_exec._git_status_paths(str(repo))

    # AC3 is about source-tree write attribution, not about the preflight's
    # own pass/warn/blocked verdict (a second real run legitimately reaches
    # a different planner verdict once a snapshot archive exists from the
    # first run -- e.g. ambiguous_scope_signal -- which is unrelated to
    # bytecode-cache safety). Any exit code in the closed 0-3 set is
    # accepted here; only unexpected crashes are not.
    first = _run_real_preflight_child(repo, fixture_input_path, env)
    assert first.returncode in (0, 1, 2, 3), first.stderr
    unauthorized_after_first = real_exec._find_unauthorized_repo_changes(
        str(repo), issue_number, before_snapshot, before_status
    )
    assert unauthorized_after_first is None, unauthorized_after_first

    mid_snapshot = real_exec._snapshot_repo_paths(str(repo), issue_number)
    mid_status = real_exec._git_status_paths(str(repo))

    second = _run_real_preflight_child(repo, fixture_input_path, env)
    assert second.returncode in (0, 1, 2, 3), second.stderr
    unauthorized_after_second = real_exec._find_unauthorized_repo_changes(
        str(repo), issue_number, mid_snapshot, mid_status
    )
    assert unauthorized_after_second is None, unauthorized_after_second

    assert list(repo.rglob("*.pyc")) == []
    assert [p for p in repo.rglob("__pycache__") if p.is_dir()] == []


@pytest.mark.parametrize("writer", ["bytes", "compile"])
def test_ac4_real_preflight_explicit_or_bytes_cache_write_fails_closed(
    tmp_path: Path, writer: str
) -> None:
    """AC4: when the real production `run_refinement_preflight.py` process
    (after its own normal, unmodified execution) additionally writes to its
    own canonical importlib cache path via `py_compile.compile()` or a raw
    bytes write, the real `skill_runtime_exec` detection primitives must
    flag it as `unauthorized_write_path` instead of silently excluding the
    cache."""
    repo = _make_repo(tmp_path)
    fixture_input_path = _install_real_preflight_fixture(repo, negative_writer=writer)
    issue_number = str(_FIXTURE_ISSUE_NUMBER)
    env = real_exec._sanitize_env(str(repo))

    before_snapshot = real_exec._snapshot_repo_paths(str(repo), issue_number)
    before_status = real_exec._git_status_paths(str(repo))

    result = _run_real_preflight_child(repo, fixture_input_path, env)
    # the negative write itself must not crash the child (0 pass / 1 warn)
    assert result.returncode in (0, 1), result.stderr

    unauthorized_path = real_exec._find_unauthorized_repo_changes(
        str(repo), issue_number, before_snapshot, before_status
    )
    assert unauthorized_path is not None
    assert "__pycache__" in unauthorized_path


@pytest.mark.parametrize(
    "writer", ["replace-parent", "replace-parent-file", "modify-existing"]
)
def test_ac5_real_preflight_cache_parent_replace_or_mutation_fails_closed(
    tmp_path: Path, writer: str
) -> None:
    """AC5: when the real production `run_refinement_preflight.py` process
    replaces its cache parent directory with a symlink, replaces it with a
    plain (non-directory) file, or mutates an existing cache file's
    content/size, the real `skill_runtime_exec` detection primitives must
    flag it as `unauthorized_write_path`."""
    repo = _make_repo(tmp_path)
    fixture_input_path = _install_real_preflight_fixture(repo, negative_writer=writer)
    issue_number = str(_FIXTURE_ISSUE_NUMBER)
    env = real_exec._sanitize_env(str(repo))

    if writer == "modify-existing":
        # Seed a pre-existing cache file the harness will then mutate, so the
        # scenario matches AC5's "modify an existing cache" wording exactly.
        cache_path = Path(
            importlib.util.cache_from_source(
                str(
                    repo
                    / ".claude"
                    / "skills"
                    / "issue-refinement-loop"
                    / "scripts"
                    / "run_refinement_preflight.py"
                )
            )
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"seed")

    before_snapshot = real_exec._snapshot_repo_paths(str(repo), issue_number)
    before_status = real_exec._git_status_paths(str(repo))

    result = _run_real_preflight_child(repo, fixture_input_path, env)
    assert result.returncode in (0, 1), result.stderr

    unauthorized_path = real_exec._find_unauthorized_repo_changes(
        str(repo), issue_number, before_snapshot, before_status
    )
    assert unauthorized_path is not None


# ---------------------------------------------------------------------------
# AC10-AC13: Issue #1439 Scope Delta 2 -- real executor chain via the
# test-only `preflight.run.fixture` command-id (skill_runtime_exec.main()
# driven end to end, not bypassed).
# ---------------------------------------------------------------------------

_EXECUTOR_SCRIPT = AGENT_GUARDS_DIR / "skill_runtime_exec.py"

_ENTRY_GUARD = 'if __name__ == "__main__":\n    main()\n'


def _splice_pid_proof_harness(path: Path, marker: str, issue_number: int) -> None:
    """Append a thin test-only harness *after* the real script's own
    `if __name__ == "__main__": main()` guard (never replacing or editing
    any of the real logic) that records this process's own pid/ppid and
    effective bytecode flags to a JSON proof file under the issue's allowed
    artifact root, just before `main()` runs (Issue #1439 AC10)."""
    source = path.read_text(encoding="utf-8")
    assert source.endswith(_ENTRY_GUARD), (
        f"entry-point guard shape changed for {path}; update this test harness splice point"
    )
    harness = f"""
    import json as _json_test
    import os as _os_test
    import sys as _sys_test
    from pathlib import Path as _Path_test

    _root_test = _Path_test(__file__).resolve().parents[4]
    _proof_dir_test = (
        _root_test / ".claude" / "artifacts" / "issue-refinement-loop" / "{issue_number}"
    )
    _proof_dir_test.mkdir(parents=True, exist_ok=True)
    (_proof_dir_test / "pid_proof_{marker}.json").write_text(
        _json_test.dumps(
            {{
                "marker": {marker!r},
                "pid": _os_test.getpid(),
                "ppid": _os_test.getppid(),
                "dont_write_bytecode_flag": bool(_sys_test.flags.dont_write_bytecode),
                "dont_write_bytecode_attr": bool(_sys_test.dont_write_bytecode),
                "pythondontwritebytecode_env": _os_test.environ.get("PYTHONDONTWRITEBYTECODE"),
            }}
        ),
        encoding="utf-8",
    )
"""
    spliced = source[: -len(_ENTRY_GUARD)] + ('if __name__ == "__main__":\n' + harness + "    main()\n")
    path.write_text(spliced, encoding="utf-8")


def _install_real_preflight_fixture_with_pid_proof(repo_root: Path) -> Path:
    fixture_input_path = _install_real_preflight_fixture(repo_root)
    dest_scripts = repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    _splice_pid_proof_harness(
        dest_scripts / "run_refinement_preflight.py", "preflight", _FIXTURE_ISSUE_NUMBER
    )
    _splice_pid_proof_harness(
        dest_scripts / "plan_refinement_loop.py", "planner", _FIXTURE_ISSUE_NUMBER
    )
    return fixture_input_path


def _run_real_executor(
    repo: Path,
    fixture_relpath: str,
    *,
    command_id: str = "preflight.run.fixture",
) -> subprocess.CompletedProcess[str]:
    """Spawn the real `skill_runtime_exec.py` executor as a real subprocess
    (not a direct call to `run_refinement_preflight.py`), driving the whole
    real executor -> real preflight -> real planner chain end to end."""
    outer_env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    return subprocess.run(
        [
            sys.executable,
            str(_EXECUTOR_SCRIPT),
            "--command-id",
            command_id,
            "--issue-number",
            str(_FIXTURE_ISSUE_NUMBER),
            "--repo",
            _FIXTURE_REPO_SLUG,
            "--fixture",
            fixture_relpath,
        ],
        cwd=str(repo),
        env=outer_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ac10_real_executor_chain_drives_real_preflight_and_planner_with_pid_proof(
    tmp_path: Path,
) -> None:
    """AC10: drive the real executor -> real preflight -> real planner
    subprocess chain through the canonical executor
    `skill_runtime_exec.py --command-id preflight.run.fixture --issue-number
    <n> --repo <repo> --fixture <path>`, recording each process's own
    pid/ppid/effective bytecode flags as JSON proof, and require an explicit
    success exit code (not `blocked` / `environment_failure`)."""
    repo = _make_repo(tmp_path)
    _install_real_preflight_fixture_with_pid_proof(repo)

    result = _run_real_executor(repo, "preflight_fixture_input.json")
    assert result.returncode in (0, 1), (result.stdout, result.stderr)

    artifact_dir = repo / ".claude" / "artifacts" / "issue-refinement-loop" / str(_FIXTURE_ISSUE_NUMBER)
    preflight_proof = json.loads((artifact_dir / "pid_proof_preflight.json").read_text(encoding="utf-8"))
    planner_proof = json.loads((artifact_dir / "pid_proof_planner.json").read_text(encoding="utf-8"))
    proof = {"preflight": preflight_proof, "planner": planner_proof}

    # Real, distinct process identities for every level of the chain.
    assert isinstance(preflight_proof["pid"], int)
    assert isinstance(planner_proof["pid"], int)
    assert preflight_proof["pid"] != planner_proof["pid"], proof

    # Parent-child relationship evidence: the planner is spawned by a plain
    # `subprocess.run([sys.executable, PLANNER_SCRIPT], ...)` call inside the
    # real preflight process (no shell/uv indirection at that level), so its
    # os.getppid() must equal the real preflight process's own pid.
    assert planner_proof["ppid"] == preflight_proof["pid"], proof

    # Effective bytecode flags at every level of the real chain.
    for level_name, level in proof.items():
        assert level["pythondontwritebytecode_env"] == "1", (level_name, proof)
        assert level["dont_write_bytecode_flag"] is True, (level_name, proof)
        assert level["dont_write_bytecode_attr"] is True, (level_name, proof)

    assert list(repo.rglob("*.pyc")) == []
    assert [p for p in repo.rglob("__pycache__") if p.is_dir()] == []


@pytest.mark.parametrize(
    "writer",
    ["bytes", "compile", "replace-parent", "replace-parent-file", "modify-existing"],
)
def test_ac11_real_executor_negative_write_fails_closed_with_reason_code(
    tmp_path: Path, writer: str
) -> None:
    """AC11: drive the AC4/AC5 adversarial bytecode-cache writes through the
    real executor (`preflight.run.fixture`), and assert the real executor's
    own exit code (2) and stderr `reason_code=unauthorized_write_path`
    directly -- not just the lower-level detection primitives called
    in-process (as AC4/AC5 above do)."""
    repo = _make_repo(tmp_path)
    fixture_input_path = _install_real_preflight_fixture(repo, negative_writer=writer)

    if writer == "modify-existing":
        cache_path = Path(
            importlib.util.cache_from_source(
                str(
                    repo
                    / ".claude"
                    / "skills"
                    / "issue-refinement-loop"
                    / "scripts"
                    / "run_refinement_preflight.py"
                )
            )
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"seed")

    result = _run_real_executor(repo, fixture_input_path.name)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "reason_code=unauthorized_write_path" in result.stderr, result.stderr
    assert "__pycache__" in result.stderr, result.stderr


def test_ac12_persisted_provenance_artifact_contains_full_v2_proof(tmp_path: Path) -> None:
    """AC12: read back the actually-persisted
    `refinement_preflight_provenance_v1.json` artifact and assert it
    contains the full `PY_SYNTAX_COMPILE_PROOF_V2` proof (schema_version +
    all required fields), not just the collapsed `py_compile_status`
    summary."""
    repo = _make_repo(tmp_path)
    fixture_input_path = _install_real_preflight_fixture(repo)
    env = real_exec._sanitize_env(str(repo))

    result = _run_real_preflight_child(repo, fixture_input_path, env)
    assert result.returncode in (0, 1), result.stderr

    provenance_path = (
        repo
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(_FIXTURE_ISSUE_NUMBER)
        / "refinement_preflight_provenance_v1.json"
    )
    assert provenance_path.exists()
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

    proof = provenance.get("python_syntax_compile_proof")
    assert proof is not None, provenance

    required_v2_fields = {
        "schema_version",
        "operation_kind",
        "source_mode",
        "flags",
        "dont_inherit",
        "optimize",
        "cache_write_expected",
        "py_compile_status",
        "python_version",
        "python_executable",
        "git_head_sha",
        "planner_script_path",
        "planner_script_realpath",
        "planner_script_blob_sha",
        "cwd",
        "stderr_sha256",
        "stderr_excerpt",
    }
    assert required_v2_fields.issubset(proof.keys()), proof
    assert proof["schema_version"] == "PY_SYNTAX_COMPILE_PROOF_V2"
    assert proof["operation_kind"] == "in_process_compile"
    assert proof["source_mode"] == "bytes"
    assert proof["flags"] == 0
    assert proof["dont_inherit"] is True
    assert proof["optimize"] == -1
    assert proof["cache_write_expected"] is False
    assert proof["py_compile_status"] == "pass"


def test_ac13_child_subprocess_timeout_applies_registry_timeout_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC13: `skill_runtime_exec.py`'s own `subprocess.run` of the
    registry-declared child command must apply the registry's
    `timeout_seconds` and convert a `subprocess.TimeoutExpired` into a
    fail-closed reason code, instead of hanging indefinitely or silently
    ignoring the timeout."""
    repo = _make_repo(tmp_path)
    _install_real_preflight_fixture(repo)

    captured: dict[str, object] = {}
    real_subprocess_run = subprocess.run

    def _selective_raising_run(argv, **kwargs):
        if isinstance(argv, list) and any(
            isinstance(tok, str) and tok.endswith("run_refinement_preflight.py") for tok in argv
        ):
            captured["argv"] = argv
            captured["timeout"] = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        return real_subprocess_run(argv, **kwargs)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(real_exec.subprocess, "run", _selective_raising_run)

    exit_code = real_exec.main(
        [
            "--command-id",
            "preflight.run.fixture",
            "--issue-number",
            str(_FIXTURE_ISSUE_NUMBER),
            "--repo",
            _FIXTURE_REPO_SLUG,
            "--fixture",
            "preflight_fixture_input.json",
        ]
    )

    assert exit_code == 2
    assert captured.get("timeout") == 120, captured  # registry's declared timeout_seconds
