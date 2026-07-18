"""Contract tests for the uv toolchain version pin (Issue #1598).

`pyproject.toml`'s ``[tool.uv].required-version`` is the canonical source for the
repository's uv version. Every ``astral-sh/setup-uv`` consumer under
``.github/actions/**`` and ``.github/workflows/**`` must:

  - pin a full (40 hex char) commit SHA, never a mutable tag reference
    (``full_sha``);
  - if it declares an explicit ``with.version``, that value must match the
    canonical ``required-version`` (``version_matches_required_version``);

and the ``uv`` binary actually installed in the runtime environment must match
the canonical ``required-version`` (``installed_uv_version_matches``).

All of the above are meaningless if the canonical source itself is missing, so
an autouse fixture makes every test in this module fail immediately (not
skip) when ``pyproject.toml`` has no ``[tool.uv].required-version``
(``required_version_present``).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - repository requires-python >=3.12
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
SETUP_UV_PREFIX = "astral-sh/setup-uv@"

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_OPERATOR_RE = re.compile(r"^[=><!~]+")


def _read_required_version_raw() -> str | None:
    """Return the raw `[tool.uv].required-version` value, or None if absent."""
    if not PYPROJECT_PATH.is_file():
        return None
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return None
    uv_section = tool.get("uv")
    if not isinstance(uv_section, dict):
        return None
    value = uv_section.get("required-version")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _required_version_number() -> str:
    """Return the numeric version (operator prefix stripped), asserting presence."""
    raw = _read_required_version_raw()
    assert raw, (
        "pyproject.toml [tool.uv].required-version is missing or empty; "
        "it is the canonical source for the repository uv version pin "
        "(Issue #1598)."
    )
    return _VERSION_OPERATOR_RE.sub("", raw)


@pytest.fixture(autouse=True)
def _require_canonical_uv_version_present() -> None:
    """AC8 guard: fail every test in this module immediately (not skip) if the
    canonical `[tool.uv].required-version` source is absent."""
    raw = _read_required_version_raw()
    assert raw, (
        "pyproject.toml [tool.uv].required-version is missing; the uv toolchain "
        "pin has no canonical source (Issue #1598 AC8 guard). Every test in "
        "test_uv_toolchain_contract.py depends on this value being present."
    )


def _iter_setup_uv_consumer_files() -> list[Path]:
    """Enumerate every action/workflow YAML file that could reference setup-uv."""
    files: list[Path] = []
    actions_dir = REPO_ROOT / ".github" / "actions"
    if actions_dir.is_dir():
        files.extend(sorted(actions_dir.glob("*/action.yml")))
        files.extend(sorted(actions_dir.glob("*/action.yaml")))
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    if workflows_dir.is_dir():
        files.extend(sorted(workflows_dir.glob("*.yml")))
        files.extend(sorted(workflows_dir.glob("*.yaml")))
    return files


def _iter_setup_uv_steps(path: Path) -> list[dict]:
    """Return every step dict in `path` whose `uses` targets astral-sh/setup-uv."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []

    candidate_step_lists: list[list] = []
    runs = data.get("runs")
    if isinstance(runs, dict) and isinstance(runs.get("steps"), list):
        candidate_step_lists.append(runs["steps"])
    jobs = data.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and isinstance(job.get("steps"), list):
                candidate_step_lists.append(job["steps"])

    matched: list[dict] = []
    for steps in candidate_step_lists:
        for step in steps:
            if not isinstance(step, dict):
                continue
            uses = step.get("uses")
            if isinstance(uses, str) and uses.startswith(SETUP_UV_PREFIX):
                matched.append(step)
    return matched


def _collect_setup_uv_consumers() -> list[tuple[Path, dict]]:
    """Return (file, step) pairs for every astral-sh/setup-uv consumer in repo."""
    consumers: list[tuple[Path, dict]] = []
    for path in _iter_setup_uv_consumer_files():
        for step in _iter_setup_uv_steps(path):
            consumers.append((path, step))
    return consumers


def test_repo_has_at_least_the_two_known_setup_uv_consumers() -> None:
    """Sanity check: the repository-wide scan must not silently find nothing.

    Known consumers as of Issue #1598: .github/actions/setup-python-uv/action.yml
    and .github/workflows/check-hook-integrity.yml.
    """
    consumers = _collect_setup_uv_consumers()
    consumer_files = {str(path.relative_to(REPO_ROOT)) for path, _step in consumers}
    assert (
        ".github/actions/setup-python-uv/action.yml" in consumer_files
    ), f"expected known consumer missing from scan; found: {sorted(consumer_files)}"
    assert (
        ".github/workflows/check-hook-integrity.yml" in consumer_files
    ), f"expected known consumer missing from scan; found: {sorted(consumer_files)}"


def test_all_setup_uv_consumers_use_approved_full_sha() -> None:
    """AC5: every astral-sh/setup-uv consumer must pin a full commit SHA."""
    consumers = _collect_setup_uv_consumers()
    assert consumers, "no astral-sh/setup-uv consumers found in repository"

    failures: list[str] = []
    for path, step in consumers:
        uses = step["uses"]
        ref = uses[len(SETUP_UV_PREFIX) :]
        if not _FULL_SHA_RE.fullmatch(ref):
            rel = path.relative_to(REPO_ROOT)
            failures.append(
                f"{rel}: astral-sh/setup-uv is pinned to {ref!r}, which is not a "
                "full 40-character commit SHA (mutable tag/branch references are "
                "not allowed)"
            )
    assert not failures, "\n".join(failures)


def test_setup_uv_consumer_explicit_version_matches_required_version() -> None:
    """AC6: explicit `with.version` on any setup-uv consumer step must
    match the canonical `pyproject.toml` [tool.uv].required-version."""
    required_version = _required_version_number()
    consumers = _collect_setup_uv_consumers()
    assert consumers, "no astral-sh/setup-uv consumers found in repository"

    checked_any_explicit_version = False
    failures: list[str] = []
    for path, step in consumers:
        with_block = step.get("with")
        if not isinstance(with_block, dict):
            continue
        explicit_version = with_block.get("version")
        if explicit_version is None:
            continue
        checked_any_explicit_version = True
        if str(explicit_version) != required_version:
            rel = path.relative_to(REPO_ROOT)
            failures.append(
                f"{rel}: setup-uv step declares version={explicit_version!r}, "
                f"which does not match pyproject.toml required-version "
                f"({required_version!r})"
            )
    assert checked_any_explicit_version, (
        "expected at least one astral-sh/setup-uv consumer with an explicit "
        "with.version to validate against required-version"
    )
    assert not failures, "\n".join(failures)


def test_installed_uv_version_matches_required_version() -> None:
    """AC7: the uv binary actually installed at runtime must match
    pyproject.toml's [tool.uv].required-version (subprocess, not static)."""
    required_version = _required_version_number()

    result = subprocess.run(
        ["uv", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"`uv --version` exited {result.returncode}; stderr={result.stderr!r}"
    )

    match = re.search(r"uv (\S+)", result.stdout)
    assert match, f"could not parse `uv --version` output: {result.stdout!r}"
    installed_version = match.group(1)

    assert installed_version == required_version, (
        f"installed uv version {installed_version!r} does not match "
        f"pyproject.toml [tool.uv].required-version {required_version!r}"
    )


def test_required_version_present_in_pyproject_toml() -> None:
    """AC8: pyproject.toml must declare [tool.uv].required-version explicitly.

    This is a named, explicit assertion of the same invariant enforced by the
    module-level autouse fixture, so the guard is visible as its own reported
    test result (not only as a side effect of other tests failing).
    """
    raw = _read_required_version_raw()
    assert raw, (
        "pyproject.toml [tool.uv].required-version is missing; it must be the "
        "single canonical source for the repository's pinned uv version "
        "(Issue #1598)."
    )
    # The value must actually resolve to a concrete version number once any
    # comparison operator prefix (e.g. "==") is stripped.
    numeric = _VERSION_OPERATOR_RE.sub("", raw)
    assert numeric, f"required-version {raw!r} did not resolve to a version number"
