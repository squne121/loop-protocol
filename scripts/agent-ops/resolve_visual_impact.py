#!/usr/bin/env python3
"""resolve_visual_impact.py (Issue #2019)

Orchestration + policy layer:

1. Registry (docs/dev/visual-surfaces.yml) load/validate/diff and
   TypeScript-compiler-API-backed affected-surface resolution (delegates the
   actual static import-graph walk to resolve_visual_impact.mjs -- the
   CANDIDATE/producer-side resolve() path here never re-implements
   TypeScript/Vite import semantics with regex).
2. VISUAL_IMPACT_DECLARATION_V1 parsing (untrusted PR body input).
3. VISUAL_IMPACT_DECISION_V1 generation (trusted CI observation) and
   disposition evaluation (verified_unchanged / baseline_changed / waived),
   including trusted-base-branch CODEOWNERS-derived waiver authority.
4. Issue #2099: a SEPARATE, narrower, base-locked TRUSTED-side static
   import resolver (`resolve_trusted_transitive_graph()` /
   `TrustedGraphWalker`) that IS a deliberate regex-based
   specifier-extraction walker -- unlike (1) above, it never invokes the
   TypeScript compiler API or Node, and never checks out/materializes
   candidate PR head content to disk; it reads candidate head file
   content exclusively via `git ls-tree`/`git cat-file` object reads. This
   is intentional (Issue #2099 In Scope: "base-locked existing
   static-import resolver semantics", not "TypeScript compiler import
   graph") and does not contradict (1)'s "never re-implements ... with
   regex" note, which describes only the candidate-side `resolve()` path.

Both the registry/resolver Allowed Path (`resolve_visual_impact.py`) and the
policy-evaluator responsibility described in Issue #2019's In Scope B/C are
implemented in this single module because the Issue's Allowed Paths list
does not provide a separate production script path for the declaration
parser / decision generator (only their test files are listed there).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import posixpath
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import jsonschema
import yaml

RESOLVER_ORCHESTRATION_VERSION = "1"
POLICY_VERSION = "1"
SCHEMA_NAME = "RESOLVE_VISUAL_IMPACT_RESULT_V1"

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.yml"
DEFAULT_SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.schema.json"
DEFAULT_MJS_PATH = REPO_ROOT / "scripts" / "agent-ops" / "resolve_visual_impact.mjs"
DEFAULT_VISUAL_IMPACT_SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-impact.schema.json"

# Closed enum -> real argv mapping (registry `update_command_id` /
# `verify_command_id` never carry a raw shell string; Issue #2019 In Scope A).
COMMAND_ID_MAP: dict[str, list[str]] = {
    "vitest_component_vrt_update": ["pnpm", "run", "test:vrt:update:component"],
    "vitest_component_vrt_verify": ["pnpm", "run", "test:vrt:component"],
    "playwright_e2e_vrt_update": ["pnpm", "run", "test:vrt:update:e2e"],
    "playwright_e2e_vrt_verify": ["pnpm", "run", "test:vrt:e2e"],
}

# PR #2045 OWNER fix_delta P0-4/P0-6: paths whose own content constitutes the
# visual-impact evaluator/policy itself. A change to any of these must never
# be silently no-impact -- it invalidates the trustworthiness of every other
# affected-surface determination in the same diff, so it is treated as a
# meta policy change that affects ALL registered surfaces (same shape as a
# `global_invalidator` hit, but independent of registry content so a
# candidate PR cannot remove itself from this set).
META_POLICY_PATHS: frozenset[str] = frozenset(
    {
        "docs/dev/visual-surfaces.yml",
        "docs/dev/visual-surfaces.schema.json",
        "scripts/agent-ops/resolve_visual_impact.py",
        "scripts/agent-ops/resolve_visual_impact.mjs",
        ".github/workflows/ci.yml",
        ".github/workflows/visual-impact-trusted-consumer.yml",
    }
)


class RegistryError(Exception):
    pass


class MissingRefObjectError(RegistryError):
    """Issue #2091 AC4: raised when a git ref's commit object itself cannot
    be resolved locally (e.g. a shallow checkout that never fetched that
    commit). This is NEVER the same condition as "the registry file does
    not exist at this ref" (a legitimate bootstrap case) -- conflating the
    two let a shallow-checkout trusted-consumer job silently degrade a
    genuinely-unknown ref into a synthetic empty registry. Callers MUST
    fail closed on this (never substitute a synthetic empty registry)."""


class DeclarationError(Exception):
    """Raised when a VISUAL_IMPACT_DECLARATION_V1 fails to parse/validate
    (Issue #2019 AC9/AC10). Never raised for trusted decision generation."""


MAX_DECLARATION_BLOCK_BYTES = 8192

_FENCE_RE = re.compile(r"```ya?ml\r?\n(.*?)\r?\n```", re.DOTALL)


@dataclass
class ResolveResult:
    changed_paths: list[str]
    affected_surfaces: list[dict[str, Any]] = field(default_factory=list)
    unknown_impact: list[dict[str, Any]] = field(default_factory=list)
    unmapped_visual_candidates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # PR #2045 OWNER fix_delta P0-2: the caller-side policy evaluator
    # previously re-parsed/re-validated the registry from the working tree
    # instead of reusing the already-validated head registry this resolve()
    # call produced. `head_doc` lets `_run_policy_check` reuse the single
    # validated document (never a second, unvalidated `yaml.safe_load`).
    head_doc: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_NAME,
            "resolver_orchestration_version": RESOLVER_ORCHESTRATION_VERSION,
            "changed_paths": self.changed_paths,
            "affected_surfaces": self.affected_surfaces,
            "unknown_impact": self.unknown_impact,
            "unmapped_visual_candidates": self.unmapped_visual_candidates,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Registry load / validate / diff / resolve (Issue #2019 In Scope A / D)
# ---------------------------------------------------------------------------


BOOTSTRAP_EMPTY_REGISTRY_TEXT = "schema_version: 1\nsurfaces: {}\n"


def load_registry_text(registry_path: Path, git_ref: str | None, repo_root: Path) -> tuple[str, bool]:
    """Load registry YAML text either from the working tree or a git ref.
    Returns `(text, existed_at_ref)` -- PR #2045 OWNER fix_delta P0-2:
    `existed_at_ref` lets the caller distinguish the legitimate "this ref
    predates the registry entirely" bootstrap case (a synthetic empty
    document that must NOT be schema-validated -- the schema legitimately
    requires a non-empty `surfaces` map, which no genuinely-missing file
    can ever satisfy) from a genuinely broken/invalid registry that DID
    exist at this ref."""
    if git_ref is None:
        return registry_path.read_text(encoding="utf-8"), True
    # Issue #2091 AC4: first confirm the ref's COMMIT OBJECT is present
    # locally at all. A shallow/partial checkout (e.g. the trusted-consumer
    # workflow's default `actions/checkout` depth) can have a ref string
    # that names a commit whose object was simply never fetched -- `git
    # show <ref>:<path>` returns the SAME non-zero exit code for that case
    # as for "the ref exists but the file genuinely doesn't", so the two
    # must be distinguished BEFORE inspecting the path-lookup result.
    ref_check = subprocess.run(
        ["git", "cat-file", "-e", f"{git_ref}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if ref_check.returncode != 0:
        raise MissingRefObjectError(
            f"commit object for ref {git_ref!r} not found locally (e.g. shallow "
            "checkout never fetched it) -- refusing to treat this as a "
            "legitimate missing-registry bootstrap case"
        )
    rel = registry_path.relative_to(repo_root).as_posix()
    proc = subprocess.run(
        ["git", "show", f"{git_ref}:{rel}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # The commit object DOES exist (checked above); the registry file
        # genuinely did not exist at this ref (e.g. base ref predates this
        # Issue) -- treat as an empty registry rather than failing closed on
        # git plumbing noise; downstream union logic tolerates an empty map.
        return BOOTSTRAP_EMPTY_REGISTRY_TEXT, False
    return proc.stdout, True


def validate_registry(doc: dict[str, Any], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        messages = [f"{list(e.path)}: {e.message}" for e in errors]
        raise RegistryError("; ".join(messages))


def _yaml_safe_load_no_duplicate_keys(text: str, error_cls: type[Exception]) -> Any:
    """Generic duplicate-top-level/nested-key-rejecting YAML loader (PR
    #2045 OWNER fix_delta P0-2: "duplicate key は全て非ゼロ終了にする").
    Shared by both registry loading and declaration parsing so a
    silently-shadowed duplicate `surfaces:` key (or duplicate surface_id
    key inside it) is never resolved by "last write wins"."""

    class _UniqueKeyLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False) -> dict:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise error_cls(f"duplicate key in YAML mapping: {key!r}")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    _UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
    return yaml.load(text, Loader=_UniqueKeyLoader)


def load_and_validate_registry(
    registry_path: Path, schema_path: Path, git_ref: str | None, repo_root: Path
) -> dict[str, Any]:
    text, existed_at_ref = load_registry_text(registry_path, git_ref, repo_root)
    if not existed_at_ref:
        # Bootstrap case (PR #2045 OWNER fix_delta P0-2): this ref predates
        # the registry file entirely. The synthetic empty document is
        # intentionally schema-invalid (`surfaces` must be non-empty) and
        # must never be run through `validate_registry` -- doing so turned
        # every legitimate bootstrap union (e.g. this very Issue's own PR,
        # whose base branch has no registry yet) into a fabricated
        # `RegistryError`.
        return {"schema_version": 1, "surfaces": {}}
    try:
        doc = _yaml_safe_load_no_duplicate_keys(text, RegistryError) or {}
    except RegistryError:
        raise
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise RegistryError("registry root must be a mapping")
    validate_registry(doc, schema_path)
    return doc


def collect_producer_paths(surface_def: dict[str, Any]) -> set[str]:
    producers = surface_def.get("producers", {})
    paths: set[str] = set()
    for key in ("modules", "styles", "assets", "config"):
        for p in producers.get(key, []) or []:
            paths.add(p)
    return paths


def collect_contract_paths(surface_def: dict[str, Any]) -> set[str]:
    """PR #2045 OWNER fix_delta P0-4: a surface's registered baseline PNG or
    VRT spec file changing (with NO producer-module change) must also mark
    that surface as affected -- these are the VC contract for the surface
    and are never covered by `coverage_roots` (which is producer-source
    scoped), so without this a baseline-only or spec-only edit bypassed
    disposition entirely."""
    contracts = surface_def.get("contracts", {}) or {}
    paths: set[str] = set()
    for key in ("baseline", "spec"):
        value = contracts.get(key)
        if value:
            paths.add(value)
    return paths


def diff_producer_mappings(base_doc: dict[str, Any], head_doc: dict[str, Any]) -> set[str]:
    """Surfaces whose head-side producer mapping dropped an entry present in base
    (deletion-as-bypass detection, Issue #2019 In Scope A/D)."""
    base_surfaces = base_doc.get("surfaces", {}) or {}
    head_surfaces = head_doc.get("surfaces", {}) or {}
    affected: set[str] = set()
    for surface_id, base_def in base_surfaces.items():
        base_paths = collect_producer_paths(base_def)
        head_def = head_surfaces.get(surface_id)
        if head_def is None:
            # Whole surface entry deleted -- registry schema `required` already
            # fails this closed at validate time for malformed registries, but
            # a *clean* deletion (still schema-valid) must still be surfaced.
            affected.add(surface_id)
            continue
        head_paths = collect_producer_paths(head_def)
        if base_paths - head_paths:
            affected.add(surface_id)
    return affected


def build_mjs_request(repo_root: Path, surfaces: dict[str, Any]) -> dict[str, Any]:
    request_surfaces = {}
    for surface_id, surface_def in surfaces.items():
        producers = surface_def.get("producers", {})
        request_surfaces[surface_id] = {
            "modules": producers.get("modules", []) or [],
            "styles": producers.get("styles", []) or [],
            "assets": producers.get("assets", []) or [],
            "config": producers.get("config", []) or [],
        }
    return {"repo_root": str(repo_root), "surfaces": request_surfaces}


MJS_RESULT_SCHEMA_NAME = "RESOLVE_VISUAL_IMPACT_MJS_RESULT_V1"
# Must track `RESOLVER_VERSION` in scripts/agent-ops/resolve_visual_impact.mjs.
MJS_EXPECTED_RESOLVER_VERSION = "1"


# Issue #2099 AC4 (Current Validated Scope): `run_mjs()` previously had no
# wall-clock upper bound on the Node subprocess it spawns -- a candidate PR
# whose source triggers pathological TS-compiler-API behaviour (or simply
# hangs) could block the calling CI job indefinitely. Bounded and always
# surfaced as a resolver error (fail-closed), never silently treated as "no
# impact".
MJS_SUBPROCESS_TIMEOUT_SECONDS = 60


def run_mjs(
    mjs_path: Path,
    request: dict[str, Any],
    node_bin: str = "node",
    timeout_seconds: float = MJS_SUBPROCESS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """PR #2045 OWNER fix_delta P0-2: a non-zero mjs exit code must always be
    surfaced as a resolver error, even when stdout happens to parse as valid
    JSON (the mjs script sets `process.exitCode = 1` on per-surface
    exceptions/invalid input while still emitting a best-effort JSON body).
    Silently trusting the JSON body regardless of exit code let a crashed
    resolver invocation degrade to an empty/partial `surfaces` map that was
    then treated as "fully resolved, no impact"."""
    try:
        proc = subprocess.run(
            [node_bin, str(mjs_path)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        # Issue #2099 AC4: a wall-clock bound violation is a resolver error,
        # never a silent partial/empty result.
        raise RegistryError(
            f"resolve_visual_impact.mjs exceeded wall-clock timeout ({timeout_seconds}s): {exc}"
        ) from exc
    if not proc.stdout.strip():
        raise RegistryError(
            f"resolve_visual_impact.mjs produced no output (exit {proc.returncode}, stderr: {proc.stderr})"
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"resolve_visual_impact.mjs produced invalid JSON: {exc}\n{proc.stdout}") from exc

    if result.get("schema") != MJS_RESULT_SCHEMA_NAME:
        raise RegistryError(f"resolve_visual_impact.mjs produced unexpected schema: {result.get('schema')!r}")
    if result.get("resolver_version") != MJS_EXPECTED_RESOLVER_VERSION:
        raise RegistryError(
            f"resolve_visual_impact.mjs resolver_version mismatch: "
            f"expected {MJS_EXPECTED_RESOLVER_VERSION!r}, got {result.get('resolver_version')!r}"
        )

    request_surface_ids = set((request.get("surfaces") or {}).keys())
    result_surface_ids = set((result.get("surfaces") or {}).keys())
    if result_surface_ids != request_surface_ids:
        raise RegistryError(
            "resolve_visual_impact.mjs surface key set mismatch: "
            f"requested={sorted(request_surface_ids)} returned={sorted(result_surface_ids)}"
        )

    if proc.returncode != 0:
        mjs_errors = result.get("errors") or [f"mjs exited {proc.returncode} with no explicit errors[] entry"]
        raise RegistryError(f"resolve_visual_impact.mjs exited {proc.returncode}: {mjs_errors}")

    return result


def match_coverage_roots(changed_path: str, coverage_roots: list[str]) -> bool:
    for pattern in coverage_roots:
        if fnmatch.fnmatch(changed_path, pattern):
            return True
        # `src/ui/**` should also match `src/ui/HudController.ts` (fnmatch
        # already handles this) and nested files under any depth.
        if pattern.endswith("/**") and changed_path.startswith(pattern[:-3]):
            return True
    return False


# PR #2045 OWNER fix_delta P1-4: this parser previously existed ONLY as an
# inline Python heredoc embedded in `.github/workflows/ci.yml`'s "Compute
# changed paths" step -- untestable as production code (a test could only
# ever reproduce the SAME logic in a synthetic fixture, never actually
# exercise the real producer). Extracted here as an importable function; the
# CI step below now calls it via `--mode changed-paths` instead of
# re-implementing it inline.
def parse_git_diff_name_status_z(raw: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Parse `git diff --name-status -z --find-renames` NUL-delimited output
    into (typed_entries, flat_paths). `flat_paths` is the union of every
    `path` and (for renames) `old_path`, matching `_flat_paths_for_resolve`'s
    semantics exactly."""
    fields = [f.decode("utf-8") for f in raw.split(b"\x00") if f != b""]

    entries: list[dict[str, str]] = []
    flat: list[str] = []
    i = 0
    while i < len(fields):
        token = fields[i]
        status = token[0] if token else ""
        if status in ("R", "C"):
            old_path = fields[i + 1]
            new_path = fields[i + 2]
            entries.append({"status": "renamed", "path": new_path, "old_path": old_path})
            flat.extend([old_path, new_path])
            i += 3
        else:
            path = fields[i + 1]
            kind = {"A": "added", "D": "removed"}.get(status, "modified")
            entries.append({"status": kind, "path": path})
            flat.append(path)
            i += 2

    return entries, sorted(set(flat))


def resolve(
    changed_paths: list[str],
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    mjs_path: Path = DEFAULT_MJS_PATH,
    repo_root: Path = REPO_ROOT,
    base_ref: str | None = None,
    head_ref: str | None = None,
    node_bin: str = "node",
) -> ResolveResult:
    result = ResolveResult(changed_paths=list(changed_paths))

    try:
        head_doc = load_and_validate_registry(registry_path, schema_path, head_ref, repo_root)
    except RegistryError as exc:
        result.errors.append(f"head registry invalid: {exc}")
        return result
    result.head_doc = head_doc

    base_doc: dict[str, Any] = head_doc
    if base_ref is not None:
        try:
            base_doc = load_and_validate_registry(registry_path, schema_path, base_ref, repo_root)
        except RegistryError as exc:
            # PR #2045 OWNER fix_delta P0-2: this is a genuinely invalid base
            # registry (not the "base predates the registry entirely"
            # bootstrap case -- `load_registry_text` already substitutes a
            # synthetic empty-but-valid registry for a missing file, which
            # never raises here). Record it as a resolver error so the
            # caller fails closed instead of silently continuing on a
            # fabricated empty base.
            result.errors.append(f"base registry invalid: {exc}")
            base_doc = {"schema_version": 1, "surfaces": {}}

    # registry-first union: producers used for graph resolution come from
    # BOTH base and head registries so that a head-side mapping deletion
    # cannot silently narrow what the resolver even looks at.
    union_surfaces: dict[str, Any] = dict(head_doc.get("surfaces", {}) or {})
    for surface_id, base_def in (base_doc.get("surfaces", {}) or {}).items():
        if surface_id not in union_surfaces:
            union_surfaces[surface_id] = base_def

    # PR #2045 OWNER fix_delta P0-3: union global_invalidators/coverage_roots
    # too, not just surface entries -- a head-side diff that *narrows* these
    # (removes an invalidator, shrinks a coverage root) must not silently
    # lose the base-side coverage.
    global_invalidators = set(head_doc.get("global_invalidators", []) or []) | set(
        base_doc.get("global_invalidators", []) or []
    )
    coverage_roots = list(
        dict.fromkeys((head_doc.get("coverage_roots", []) or []) + (base_doc.get("coverage_roots", []) or []))
    )

    changed_set = set(changed_paths)

    affected_surface_ids: dict[str, str] = {}

    # 0. Meta policy paths: a change to the evaluator/registry/schema/CI
    # wiring itself invalidates every other affected-surface determination
    # in the same diff (PR #2045 OWNER fix_delta P0-4/P0-6).
    if changed_set & META_POLICY_PATHS:
        for surface_id in union_surfaces:
            affected_surface_ids.setdefault(surface_id, "meta_policy_change")

    # 1. Global invalidators: any hit affects ALL surfaces.
    if changed_set & global_invalidators:
        for surface_id in union_surfaces:
            affected_surface_ids.setdefault(surface_id, "global_invalidator")

    # 2. Direct producer path match (styles/assets/config listed verbatim),
    # plus each surface's own contract paths (baseline PNG / VRT spec) --
    # PR #2045 OWNER fix_delta P0-4: a baseline-only or spec-only edit must
    # also mark its surface affected (these are outside `coverage_roots`).
    for surface_id, surface_def in union_surfaces.items():
        if surface_id in affected_surface_ids:
            continue
        direct_paths = collect_producer_paths(surface_def) | collect_contract_paths(surface_def)
        if direct_paths & changed_set:
            affected_surface_ids[surface_id] = "direct_producer"

    # 3. Registry-union mapping deletion (base had it, head dropped it).
    for surface_id in diff_producer_mappings(base_doc, head_doc):
        affected_surface_ids.setdefault(surface_id, "mapping_deleted")

    # 4. TypeScript-compiler-API graph resolution (transitive reachability).
    request = build_mjs_request(repo_root, union_surfaces)
    try:
        mjs_result = run_mjs(mjs_path, request, node_bin=node_bin)
    except RegistryError as exc:
        result.errors.append(str(exc))
        mjs_result = {"surfaces": {}, "errors": [str(exc)]}

    result.errors.extend(mjs_result.get("errors", []))

    for surface_id, surface_result in mjs_result.get("surfaces", {}).items():
        reachable = set(surface_result.get("reachable_files", []))
        if reachable & changed_set and surface_id not in affected_surface_ids:
            affected_surface_ids[surface_id] = "producer_reachable"
        for entry in surface_result.get("unknown_impact", []):
            result.unknown_impact.append({"surface_id": surface_id, **entry})

    # unknown_impact fail-closed: any surface whose producer graph walk hit
    # an unresolvable/dynamic construct must NOT be silently treated as "no
    # impact" for that surface.
    for surface_id, surface_result in mjs_result.get("surfaces", {}).items():
        if surface_result.get("unknown_impact") and surface_id not in affected_surface_ids:
            affected_surface_ids[surface_id] = "unknown_impact"

    for surface_id, reason in affected_surface_ids.items():
        result.affected_surfaces.append({"surface_id": surface_id, "reason": reason})
    result.affected_surfaces.sort(key=lambda e: e["surface_id"])

    # 5. Coverage boundary: any changed candidate path under coverage_roots
    # that is not covered by ANY surface's producers/global invalidators is
    # `unmapped_visual_candidate` (fail-closed, never no-impact PASS).
    all_producer_paths: set[str] = set(global_invalidators)
    for surface_def in union_surfaces.values():
        all_producer_paths |= collect_producer_paths(surface_def)
        all_producer_paths |= collect_contract_paths(surface_def)
    for surface_result in mjs_result.get("surfaces", {}).values():
        all_producer_paths |= set(surface_result.get("reachable_files", []))

    for changed_path in changed_paths:
        if changed_path in all_producer_paths:
            continue
        if match_coverage_roots(changed_path, coverage_roots):
            result.unmapped_visual_candidates.append(changed_path)

    return result


# ---------------------------------------------------------------------------
# VISUAL_IMPACT_DECLARATION_V1 (untrusted PR body input) -- Issue #2019
# In Scope B/C, AC9/AC10
# ---------------------------------------------------------------------------


def _load_yaml_no_duplicate_keys(text: str) -> Any:
    class _UniqueKeyLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False) -> dict:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise DeclarationError(f"duplicate key in declaration block: {key!r}")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    _UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise DeclarationError(f"invalid YAML in declaration block: {exc}") from exc


def extract_declaration_fenced_blocks(pr_body: str) -> list[str]:
    """Return the raw text of every fenced ```yaml block that mentions
    VISUAL_IMPACT_DECLARATION_V1 (untrusted input; never shell-expanded)."""
    return [block for block in _FENCE_RE.findall(pr_body) if "VISUAL_IMPACT_DECLARATION_V1" in block]


def parse_declaration(pr_body: str, schema_path: Path = DEFAULT_VISUAL_IMPACT_SCHEMA_PATH) -> dict[str, Any]:
    """Parse and strictly validate a VISUAL_IMPACT_DECLARATION_V1 block from
    an untrusted PR body string. Raises DeclarationError (reject, never
    fabricate a default) on any AC9 violation."""
    blocks = extract_declaration_fenced_blocks(pr_body)
    if len(blocks) == 0:
        raise DeclarationError("no VISUAL_IMPACT_DECLARATION_V1 fenced block found")
    if len(blocks) > 1:
        raise DeclarationError(f"expected exactly one VISUAL_IMPACT_DECLARATION_V1 block, found {len(blocks)}")

    block = blocks[0]
    if len(block.encode("utf-8")) > MAX_DECLARATION_BLOCK_BYTES:
        raise DeclarationError(
            f"declaration block exceeds {MAX_DECLARATION_BLOCK_BYTES} bytes (oversized input rejected)"
        )

    doc = _load_yaml_no_duplicate_keys(block)
    if not isinstance(doc, dict):
        raise DeclarationError("declaration block must parse to a YAML mapping")

    surfaces = doc.get("surfaces")
    if isinstance(surfaces, list):
        ids = [s.get("surface_id") for s in surfaces if isinstance(s, dict)]
        if len(ids) != len(set(ids)):
            raise DeclarationError("duplicate surface_id in declaration surfaces list")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    sub_schema = {"$defs": schema["$defs"], **schema["$defs"]["VISUAL_IMPACT_DECLARATION_V1"]}
    try:
        jsonschema.validate(doc, sub_schema)
    except jsonschema.ValidationError as exc:
        raise DeclarationError(f"declaration failed strict schema validation: {exc.message}") from exc

    return doc


# ---------------------------------------------------------------------------
# VISUAL_IMPACT_DECISION_V1 (trusted CI-generated output) -- AC11/AC12/AC13
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# PR #2045 OWNER fix_delta P0-5 (V2 manifest): a per-surface "contract
# digest" binds an evidence manifest record to the EXACT registered contract
# (runner/spec/baseline/job/update+verify command ids) it was produced
# against -- never a bare surface_id string match, which cannot detect a
# registry contract change made concurrently with (or after) evidence
# generation.
def compute_contract_digest(surface_def: dict[str, Any]) -> str:
    contracts = surface_def.get("contracts", {}) or {}
    canonical = json.dumps(contracts, sort_keys=True, separators=(",", ":"))
    return sha256_hex(canonical.encode("utf-8"))


def build_changed_paths_digest(entries: list[dict[str, str]]) -> dict[str, Any]:
    """entries: [{"status": "modified"|"added"|"removed"|"renamed", "path": str, "old_path"?: str}]"""
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return {
        "algorithm": "sha256",
        "digest": sha256_hex(canonical.encode("utf-8")),
        "entries": entries,
    }


def build_decision(
    *,
    repository: str,
    pull_request_number: int,
    base_sha: str,
    head_sha: str,
    base_registry_blob_sha: str,
    head_registry_blob_sha: str,
    pr_body: str,
    changed_path_entries: list[dict[str, str]],
    affected_surfaces: list[dict[str, Any]],
    component_vrt_report_check_run_id: str | None,
    github_actions_app_identity: str,
    artifact_id: str | None,
    artifact_digest: str | None,
    workflow_run_id: int | None = None,
    run_attempt: int | None = None,
) -> dict[str, Any]:
    """Build VISUAL_IMPACT_DECISION_V1 from TRUSTED observation inputs only.
    Never copies a declaration's self-reported disposition verbatim -- the
    caller supplies `affected_surfaces` (each entry already carrying an
    independently-evaluated `disposition` + `evidence`), never the raw
    declaration dict itself (Issue #2019 AC11/AC12).

    Issue #2230 AC2: `workflow_run_id` / `run_attempt` bind this decision
    artifact's CONTENT (not merely its attempt-specific artifact NAME) to
    the exact triggering `workflow_run.id` / `run_attempt` -- the producer
    job's OWN `${{ github.run_id }}` / `${{ github.run_attempt }}` context,
    never a value re-derived by any other party. `verify_trusted_artifact()`
    cross-checks these fields against the trusted consumer's independently
    authenticated `component-vrt-report` CheckRun provenance."""
    return {
        "schema": "VISUAL_IMPACT_DECISION_V1",
        "repository": repository,
        "pull_request_number": pull_request_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "base_registry_blob_sha": base_registry_blob_sha,
        "head_registry_blob_sha": head_registry_blob_sha,
        "pr_body_sha256": sha256_hex(pr_body.encode("utf-8")),
        "changed_paths_digest": build_changed_paths_digest(changed_path_entries),
        "resolver_version": RESOLVER_ORCHESTRATION_VERSION,
        "policy_version": POLICY_VERSION,
        "affected_surfaces": affected_surfaces,
        "component_vrt_report_check_run_id": component_vrt_report_check_run_id,
        "github_actions_app_identity": github_actions_app_identity,
        "artifact_id": artifact_id,
        "artifact_digest": artifact_digest,
        "workflow_run_id": workflow_run_id,
        "run_attempt": run_attempt,
    }


@dataclass
class EvidenceObservation:
    """Trusted, independently-observed evidence for a single surface
    (never taken from the declaration). All booleans are computed by the
    caller from real CI artifacts (never fabricated -- Runtime Verification
    Applicability fallback_policy)."""

    baseline_diff_present: bool
    canonical_verify_success: bool
    evidence_manifest_surface_matches: bool
    evidence_manifest_contract_matches: bool
    evidence_manifest_head_matches: bool
    canonical_update_then_verify_success: bool = False
    expected_actual_diff_available: bool = False
    evidence_manifest_digest: str | None = None


def evaluate_verified_unchanged(evidence: EvidenceObservation) -> tuple[bool, str]:
    """AC12: declaration alone is never sufficient -- PASS requires baseline
    unchanged AND canonical VRT success on the same head AND evidence
    manifest binding."""
    if evidence.baseline_diff_present:
        return False, "baseline_diff_present (claimed verified_unchanged but baseline actually changed)"
    if not evidence.canonical_verify_success:
        return False, "canonical_verify_lane_did_not_succeed_on_current_head"
    if not (
        evidence.evidence_manifest_surface_matches
        and evidence.evidence_manifest_contract_matches
        and evidence.evidence_manifest_head_matches
    ):
        return False, "evidence_manifest_binding_mismatch"
    return True, "ok"


def evaluate_baseline_changed(evidence: EvidenceObservation) -> tuple[bool, str]:
    """AC13: "PNG changed" alone never passes -- requires canonical
    update-then-verify success on the SAME head, expected/actual/diff
    availability, and evidence manifest binding (which itself proves
    artifact digest + CheckRun provenance binding upstream)."""
    if not evidence.baseline_diff_present:
        return False, "claimed baseline_changed but no baseline diff observed"
    if not evidence.canonical_update_then_verify_success:
        return False, "canonical_update_then_verify_lane_did_not_succeed_on_current_head"
    if not evidence.expected_actual_diff_available:
        return False, "expected_actual_diff_not_available"
    if not (
        evidence.evidence_manifest_surface_matches
        and evidence.evidence_manifest_contract_matches
        and evidence.evidence_manifest_head_matches
    ):
        return False, "evidence_manifest_binding_mismatch"
    if not evidence.evidence_manifest_digest:
        return False, "evidence_manifest_digest_missing"
    return True, "ok"


# ---------------------------------------------------------------------------
# Waiver authority (AC14/AC15) -- trusted base-branch CODEOWNERS ONLY.
# ---------------------------------------------------------------------------


_CODEOWNERS_LINE_RE = re.compile(r"^\s*(?!#)(\S+)\s+(.+)$")


def parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    """Parse CODEOWNERS text into an ordered list of (pattern, [owners])
    rules. Standard CODEOWNERS semantics: the LAST matching rule wins."""
    rules: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _CODEOWNERS_LINE_RE.match(line)
        if not match:
            continue
        pattern, owners_str = match.groups()
        owners = [o for o in owners_str.split() if o.startswith("@")]
        if owners:
            rules.append((pattern, owners))
    return rules


def _codeowners_pattern_matches(pattern: str, path: str) -> bool:
    normalized = pattern.lstrip("/")
    if normalized.endswith("/"):
        return path.startswith(normalized) or fnmatch.fnmatch(path, normalized + "**")
    if "*" in normalized:
        return fnmatch.fnmatch(path, normalized)
    return path == normalized or path.startswith(normalized.rstrip("/") + "/")


def authorized_owners_for_paths(codeowners_rules: list[tuple[str, list[str]]], paths: list[str]) -> set[str]:
    """Union, across each path, of the LAST-matching CODEOWNERS rule's
    owners (never PR-body self-report, never a head-branch-introduced
    CODEOWNERS -- callers MUST supply `codeowners_rules` parsed from the
    TRUSTED BASE BRANCH, e.g. `git show <base_sha>:.github/CODEOWNERS`)."""
    authorized: set[str] = set()
    for path in paths:
        last_match: list[str] | None = None
        for pattern, owners in codeowners_rules:
            if _codeowners_pattern_matches(pattern, path):
                last_match = owners
        if last_match:
            authorized.update(last_match)
    return authorized


@dataclass
class WaiverEvaluation:
    ok: bool
    reason: str


def evaluate_waiver(
    *,
    actor: str,
    waiver: dict[str, Any],
    authorized_owners: set[str],
    today: date,
    tracking_issue_exists_and_open: bool | None,
) -> WaiverEvaluation:
    """AC14/AC15: owner authority derives from `authorized_owners`
    (caller-supplied from trusted base-branch CODEOWNERS -- this function
    itself never reads a file or trusts a self-reported owner string).
    Fails on: unauthorized owner, missing/closed tracking issue, expiry."""
    actor_handle = actor if actor.startswith("@") else f"@{actor}"
    if actor_handle not in authorized_owners:
        return WaiverEvaluation(False, "unauthorized_owner")
    if not waiver.get("tracking_issue"):
        return WaiverEvaluation(False, "missing_tracking_issue")
    if tracking_issue_exists_and_open is False:
        return WaiverEvaluation(False, "tracking_issue_missing_or_closed")
    if tracking_issue_exists_and_open is None:
        return WaiverEvaluation(False, "tracking_issue_state_unverified")
    expiry_str = waiver.get("expiry")
    if not expiry_str:
        return WaiverEvaluation(False, "missing_expiry")
    try:
        expiry = date.fromisoformat(expiry_str)
    except ValueError:
        return WaiverEvaluation(False, "invalid_expiry_format")
    if expiry < today:
        return WaiverEvaluation(False, "expired")
    if not waiver.get("reason"):
        return WaiverEvaluation(False, "missing_reason")
    return WaiverEvaluation(True, "ok")


# ---------------------------------------------------------------------------
# PR-level policy evaluation (CI job driver). Ties together resolve() +
# declaration parsing + evidence-manifest-derived disposition evaluation +
# waiver authority into a single pass/fail decision for the
# `visual-impact-policy` CI job (Issue #2019 In Scope F).
# ---------------------------------------------------------------------------


def find_declaration_entry(declaration_doc: dict[str, Any] | None, surface_id: str) -> dict[str, Any] | None:
    if declaration_doc is None:
        return None
    for entry in declaration_doc.get("surfaces", []) or []:
        if entry.get("surface_id") == surface_id:
            return entry
    return None


def build_evidence_from_manifest(
    manifest: dict[str, Any] | None,
    head_sha: str,
    surface_id: str,
    contract_job: str | None,
) -> "EvidenceObservation":
    """Derive trusted EvidenceObservation fields from a
    VISUAL_BASELINE_REVIEW_EVIDENCE_V1 manifest (docs/dev/visual-baseline-registry.md
    §3.5). The manifest today covers exactly the two registered
    combat-hud-* surfaces (component-vrt-report job); `evidence_manifest_*_matches`
    fields degrade to False for anything else -- fail-closed rather than
    assuming coverage that does not exist yet."""
    if manifest is None:
        return EvidenceObservation(
            baseline_diff_present=False,
            canonical_verify_success=False,
            evidence_manifest_surface_matches=False,
            evidence_manifest_contract_matches=False,
            evidence_manifest_head_matches=False,
        )
    head_matches = manifest.get("head_sha") == head_sha
    contract_matches = contract_job == "component-vrt-report"
    surface_matches = surface_id in {"combat-hud-running", "combat-hud-critical"}
    # PR #2045 OWNER fix_delta P0-5: `mismatched_pixels` in the live
    # artifact is a JSON *string* (the manifest-building workflow step
    # passes it through a bash env var into `sys.argv`, never casting it
    # back to int). A bare `mismatched == 0` comparison is therefore always
    # False for the manifest's string "0", which made `verify_success`
    # permanently False regardless of the real VRT outcome. Coerce
    # defensively here (never trust the producer's declared type) and treat
    # anything that does not parse as an integer as a non-zero mismatch
    # (fail-closed).
    mismatched = manifest.get("mismatched_pixels")
    if isinstance(mismatched, bool):
        mismatched_int: int | None = None
    elif isinstance(mismatched, int):
        mismatched_int = mismatched
    elif isinstance(mismatched, str):
        try:
            mismatched_int = int(mismatched.strip())
        except ValueError:
            mismatched_int = None
    else:
        mismatched_int = None
    verify_success = mismatched_int == 0
    artifact_id = manifest.get("artifact_id")
    return EvidenceObservation(
        baseline_diff_present=False,  # caller overrides from real changed_paths
        canonical_verify_success=verify_success,
        evidence_manifest_surface_matches=surface_matches,
        evidence_manifest_contract_matches=contract_matches,
        evidence_manifest_head_matches=head_matches,
        canonical_update_then_verify_success=verify_success,
        expected_actual_diff_available=True,
        evidence_manifest_digest=str(artifact_id) if artifact_id else None,
    )


# ---------------------------------------------------------------------------
# VISUAL_BASELINE_REVIEW_EVIDENCE_V2 -- per-surface evidence manifest
# (PR #2045 OWNER fix_delta P0-5 second-round REQUEST_CHANGES).
#
# Replaces the single flat V1 manifest (one record for the whole
# component-vrt-report job) with one record PER registered surface, each
# self-binding to its own contract, CheckRun provenance, artifact ids, and a
# per-record `manifest_sha256` tamper-evidence digest.
# ---------------------------------------------------------------------------

EVIDENCE_MANIFEST_V2_SCHEMA = "VISUAL_BASELINE_REVIEW_EVIDENCE_V2"

# The field set below is the canonical shape of one V2 record (Issue #2019 /
# PR #2045 OWNER REQUEST_CHANGES). `manifest_sha256` is NEVER included in the
# digest input -- it is the digest of every OTHER field.
_MANIFEST_V2_RECORD_FIELDS: tuple[str, ...] = (
    "surface_id",
    "contract_digest",
    "head_sha",
    "workflow_run_id",
    "run_attempt",
    "check_run_id",
    "check_suite_id",
    "github_app_id",
    "github_app_slug",
    "check_conclusion",
    "baseline_path",
    "baseline_sha256",
    "actual_sha256",
    "mismatched_pixels",
    "verify_command_id",
    "verify_succeeded",
    "update_command_id",
    "update_executed",
    "update_succeeded",
    "expected_artifact_id",
    "actual_artifact_id",
    "diff_artifact_id",
)


def _evidence_record_digest(record_without_digest: dict[str, Any]) -> str:
    canonical = json.dumps(record_without_digest, sort_keys=True, separators=(",", ":"))
    return sha256_hex(canonical.encode("utf-8"))


def build_evidence_manifest_v2_record(**fields: Any) -> dict[str, Any]:
    """Build one VISUAL_BASELINE_REVIEW_EVIDENCE_V2 surface record. Unknown
    kwargs are rejected (closed field set); missing kwargs default to
    `None`/`False` per field semantics so a caller can never silently omit a
    binding field."""
    unknown = set(fields) - set(_MANIFEST_V2_RECORD_FIELDS)
    if unknown:
        raise ValueError(f"build_evidence_manifest_v2_record: unknown field(s) {sorted(unknown)}")
    record = {name: fields.get(name) for name in _MANIFEST_V2_RECORD_FIELDS}
    record["manifest_sha256"] = _evidence_record_digest(record)
    return record


def verify_evidence_manifest_v2_record_digest(record: dict[str, Any]) -> bool:
    """Recompute `manifest_sha256` over the record's own OTHER fields and
    compare -- catches a tampered/corrupted record (fail-closed: any record
    that does not self-verify is never trusted, regardless of what its
    individual fields claim)."""
    claimed = record.get("manifest_sha256")
    if not claimed:
        return False
    body = {name: record.get(name) for name in _MANIFEST_V2_RECORD_FIELDS}
    return _evidence_record_digest(body) == claimed


def find_evidence_manifest_v2_record(manifest: dict[str, Any] | None, surface_id: str) -> dict[str, Any] | None:
    if not manifest or manifest.get("schema") != EVIDENCE_MANIFEST_V2_SCHEMA:
        return None
    for record in manifest.get("surfaces", []) or []:
        if isinstance(record, dict) and record.get("surface_id") == surface_id:
            return record
    return None


def _coerce_mismatched_pixels(mismatched: Any) -> int | None:
    """Never trust the producer's declared type (PR #2045 OWNER fix_delta
    P0-5): coerce defensively and treat anything that does not parse as an
    integer as a non-zero mismatch (fail-closed)."""
    if isinstance(mismatched, bool):
        return None
    if isinstance(mismatched, int):
        return mismatched
    if isinstance(mismatched, str):
        try:
            return int(mismatched.strip())
        except ValueError:
            return None
    return None


def build_evidence_from_manifest_v2(
    manifest: dict[str, Any] | None,
    *,
    surface_id: str,
    head_sha: str,
    expected_contract_digest: str,
    trusted_check_run_id: str | None,
    trusted_check_suite_id: str | None,
    trusted_github_app_id: str | None,
    trusted_github_app_slug: str | None,
    trusted_check_conclusion: str | None,
) -> "EvidenceObservation":
    """Derive a trusted EvidenceObservation from a
    VISUAL_BASELINE_REVIEW_EVIDENCE_V2 manifest's per-surface record.

    CheckRun provenance (check_run_id/check_suite_id/github_app_id/
    github_app_slug/check_conclusion) is NEVER read back from the record
    itself -- the component-vrt-report job that produces the manifest cannot
    know its own CheckRun id while it is still running, so those manifest
    fields are always producer-side `None`. The only trustworthy source is
    the CALLER's independently-fetched CheckRun API lookup (performed by the
    `visual-impact-policy` job AFTER component-vrt-report concludes),
    threaded through here as the `trusted_*` parameters. This mirrors the
    existing "never trust the producer's declared type" principle already
    applied to `mismatched_pixels` (V1 fix_delta P0-5)."""
    record = find_evidence_manifest_v2_record(manifest, surface_id)
    if record is None:
        return EvidenceObservation(
            baseline_diff_present=False,
            canonical_verify_success=False,
            evidence_manifest_surface_matches=False,
            evidence_manifest_contract_matches=False,
            evidence_manifest_head_matches=False,
        )
    if not verify_evidence_manifest_v2_record_digest(record):
        # Tampered / corrupted record -- fail closed rather than trusting any
        # individual field of an unverifiable record.
        return EvidenceObservation(
            baseline_diff_present=False,
            canonical_verify_success=False,
            evidence_manifest_surface_matches=False,
            evidence_manifest_contract_matches=False,
            evidence_manifest_head_matches=False,
        )

    surface_matches = record.get("surface_id") == surface_id
    contract_matches = bool(expected_contract_digest) and record.get("contract_digest") == expected_contract_digest
    head_matches = record.get("head_sha") == head_sha

    checkrun_bound = bool(trusted_check_run_id) and trusted_check_conclusion == "success"

    mismatched_int = _coerce_mismatched_pixels(record.get("mismatched_pixels"))
    verify_succeeded = bool(record.get("verify_succeeded")) and mismatched_int == 0
    update_then_verify_succeeded = (
        bool(record.get("update_executed")) and bool(record.get("update_succeeded")) and verify_succeeded
    )
    diff_available = bool(
        record.get("expected_artifact_id") and record.get("actual_artifact_id") and record.get("diff_artifact_id")
    )

    return EvidenceObservation(
        baseline_diff_present=False,  # caller overrides from real changed_paths
        canonical_verify_success=verify_succeeded and checkrun_bound,
        evidence_manifest_surface_matches=surface_matches,
        evidence_manifest_contract_matches=contract_matches,
        evidence_manifest_head_matches=head_matches,
        canonical_update_then_verify_success=update_then_verify_succeeded and checkrun_bound,
        expected_actual_diff_available=diff_available,
        evidence_manifest_digest=record.get("manifest_sha256"),
    )


def _evidence_payload_for_decision(
    disposition: str,
    ok: bool,
    reason: str,
    evidence: "EvidenceObservation | None",
    actor_handle: str | None,
) -> dict[str, Any]:
    """PR #2045 OWNER fix_delta P1-2: build a decision `evidence` payload
    that structurally matches the canonical `evidence` $def in
    docs/dev/visual-impact.schema.json (additionalProperties: false,
    fields: baseline_unchanged / canonical_verify_success /
    evidence_manifest_digest / expected_actual_diff_available /
    authorized_owner). The previous `{"ok": bool, "reason": str}` shape did
    not validate against that schema at all -- nothing ever caught this
    because the decision artifact was never schema-validated before
    upload."""
    payload: dict[str, Any] = {}
    if disposition == "waived":
        if ok and actor_handle:
            payload["authorized_owner"] = actor_handle
        return payload
    if evidence is None:
        return payload
    payload["baseline_unchanged"] = not evidence.baseline_diff_present
    payload["canonical_verify_success"] = bool(
        evidence.canonical_verify_success or evidence.canonical_update_then_verify_success
    )
    payload["expected_actual_diff_available"] = bool(evidence.expected_actual_diff_available)
    if evidence.evidence_manifest_digest:
        payload["evidence_manifest_digest"] = evidence.evidence_manifest_digest
    return payload


def evaluate_pr_policy(
    *,
    resolve_result: ResolveResult,
    declaration_doc: dict[str, Any] | None,
    registry_doc: dict[str, Any],
    evidence_manifest: dict[str, Any] | None,
    head_sha: str,
    changed_paths: list[str],
    actor: str,
    authorized_owners: set[str],
    today: date,
    tracking_issue_checker: Any = None,
    codeowners_rules: list[tuple[str, list[str]]] | None = None,
    trusted_check_run_id: str | None = None,
    trusted_check_suite_id: str | None = None,
    trusted_github_app_id: str | None = None,
    trusted_github_app_slug: str | None = None,
    trusted_check_conclusion: str | None = None,
) -> dict[str, Any]:
    """Evaluate every affected surface's disposition. Never passes on
    declaration self-report alone (AC12); unmapped/unknown_impact always
    fail closed (AC8).

    PR #2045 OWNER fix_delta P0-2: `resolve_result.errors` (resolver/schema
    internal failures -- invalid base registry, mjs crash/schema mismatch,
    etc.) are now themselves policy failures; they were previously silently
    swallowed, letting a broken resolver run degrade to "no affected
    surfaces found" -> unconditional PASS.

    PR #2045 OWNER fix_delta P1-3: when `codeowners_rules` is supplied,
    waiver authority is scoped to the SPECIFIC surface's own contract paths
    (baseline/spec) rather than the union of every registered surface's
    contract paths, so an owner authorized for surface A cannot waive a
    regression on unrelated surface B.
    """
    surfaces = registry_doc.get("surfaces", {}) or {}
    failures: list[str] = []
    surface_results: list[dict[str, Any]] = []

    if resolve_result.errors:
        failures.append(f"resolver_error: {resolve_result.errors}")
    if resolve_result.unmapped_visual_candidates:
        failures.append(f"unmapped_visual_candidate: {resolve_result.unmapped_visual_candidates}")
    if resolve_result.unknown_impact:
        failures.append(f"unknown_impact: {resolve_result.unknown_impact}")

    actor_handle = actor if actor.startswith("@") else f"@{actor}" if actor else None

    for entry in resolve_result.affected_surfaces:
        surface_id = entry["surface_id"]
        surface_def = surfaces.get(surface_id, {})
        if not surface_def.get("policy", {}).get("disposition_required", True):
            continue
        decl_entry = find_declaration_entry(declaration_doc, surface_id)
        if decl_entry is None:
            failures.append(f"{surface_id}: missing VISUAL_IMPACT_DECLARATION_V1 entry")
            continue
        disposition = decl_entry["disposition"]
        # PR #2045 OWNER fix_delta P0-5 (V2 manifest): `contract_job` is no
        # longer read here -- contract binding now goes through
        # `compute_contract_digest(surface_def)` (the full contracts dict),
        # never a bare job-name string match.
        baseline_path = surface_def.get("contracts", {}).get("baseline")
        baseline_diff_present = baseline_path in changed_paths

        evidence_for_decision: EvidenceObservation | None = None
        if disposition == "waived":
            waiver = decl_entry.get("waiver", {}) or {}
            tracking_open = tracking_issue_checker(waiver.get("tracking_issue")) if tracking_issue_checker else None
            surface_authorized_owners = authorized_owners
            if codeowners_rules is not None:
                surface_contract_paths = sorted(collect_contract_paths(surface_def))
                surface_authorized_owners = authorized_owners_for_paths(codeowners_rules, surface_contract_paths)
            evaluation = evaluate_waiver(
                actor=actor,
                waiver=waiver,
                authorized_owners=surface_authorized_owners,
                today=today,
                tracking_issue_exists_and_open=tracking_open,
            )
            ok, reason = evaluation.ok, evaluation.reason
        else:
            # PR #2045 OWNER fix_delta P0-5 (V2 manifest): bind against the
            # per-surface record's OWN contract digest (never a bare
            # surface_id string match) and never trust the record's
            # self-reported CheckRun fields -- those come from the caller's
            # independently-fetched CheckRun API lookup instead.
            evidence = build_evidence_from_manifest_v2(
                evidence_manifest,
                surface_id=surface_id,
                head_sha=head_sha,
                expected_contract_digest=compute_contract_digest(surface_def),
                trusted_check_run_id=trusted_check_run_id,
                trusted_check_suite_id=trusted_check_suite_id,
                trusted_github_app_id=trusted_github_app_id,
                trusted_github_app_slug=trusted_github_app_slug,
                trusted_check_conclusion=trusted_check_conclusion,
            )
            evidence.baseline_diff_present = baseline_diff_present
            evidence_for_decision = evidence
            if disposition == "verified_unchanged":
                ok, reason = evaluate_verified_unchanged(evidence)
            elif disposition == "baseline_changed":
                ok, reason = evaluate_baseline_changed(evidence)
            else:
                ok, reason = False, "unknown_disposition"

        evidence_payload = _evidence_payload_for_decision(disposition, ok, reason, evidence_for_decision, actor_handle)
        surface_results.append(
            {
                "surface_id": surface_id,
                "disposition": disposition,
                "ok": ok,
                "reason": reason,
                "evidence": evidence_payload,
            }
        )
        if not ok:
            failures.append(f"{surface_id}: {disposition} failed ({reason})")

    return {"ok": len(failures) == 0, "surface_results": surface_results, "failures": failures}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_changed_paths(args: argparse.Namespace) -> list[str]:
    if args.changed_paths_file:
        text = Path(args.changed_paths_file).read_text(encoding="utf-8")
        return [line.strip() for line in text.splitlines() if line.strip()]
    return list(args.changed_path or [])


def _read_changed_path_entries(args: argparse.Namespace) -> list[dict[str, str]]:
    """PR #2045 OWNER fix_delta P0-4: prefer a typed
    (status/path[/old_path]) changed-path record set over the flat
    path-only list -- the flat list previously forced every entry to
    `status: "modified"`, losing rename/add/remove semantics in the
    VISUAL_IMPACT_DECISION_V1 changed_paths_digest."""
    typed_file = getattr(args, "changed_paths_typed_file", None)
    if typed_file and Path(typed_file).exists():
        data = json.loads(Path(typed_file).read_text(encoding="utf-8"))
        entries: list[dict[str, str]] = []
        for item in data:
            entry = {"status": item["status"], "path": item["path"]}
            if item.get("old_path"):
                entry["old_path"] = item["old_path"]
            entries.append(entry)
        return entries
    return [{"status": "modified", "path": p} for p in _read_changed_paths(args)]


def _flat_paths_for_resolve(entries: list[dict[str, str]]) -> list[str]:
    """Union of `path` and (when present) `old_path` -- PR #2045 OWNER
    fix_delta P0-4: a rename that moves a producer file out of/into a
    surface's registered producer set must be evaluated on BOTH sides."""
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        for key in ("path", "old_path"):
            value = entry.get(key)
            if value and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def _build_decision_surface_entry(result_entry: dict[str, Any], registry_doc: dict[str, Any]) -> dict[str, Any]:
    surface_id = result_entry["surface_id"]
    contracts = registry_doc.get("surfaces", {}).get(surface_id, {}).get("contracts", {})
    runner = contracts.get("runner", "unknown")
    return {
        "surface_id": surface_id,
        "contract_id": f"{surface_id}:{runner}",
        "disposition": result_entry["disposition"],
        "evidence": result_entry.get("evidence", {}),
    }


def _check_tracking_issue_open(tracking_issue: str, repository: str | None) -> bool | None:
    """PR #2045 OWNER fix_delta P1-3: actually verify the waiver's tracking
    issue is open (previously always `None` -> `tracking_issue_state_unverified`,
    which fails closed but never let a genuinely valid waiver pass). Any
    subprocess/parse failure returns None (fail-closed unverified), never a
    fabricated True."""
    if not tracking_issue or not repository:
        return None
    issue_ref = tracking_issue.lstrip("#").strip()
    if not issue_ref.isdigit():
        return None
    try:
        proc = subprocess.run(
            ["gh", "issue", "view", issue_ref, "--repo", repository, "--json", "state"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    state = data.get("state")
    if not isinstance(state, str):
        return None
    return state.upper() == "OPEN"


def _validate_decision_schema(decision: dict[str, Any], visual_impact_schema_path: Path) -> list[str]:
    """PR #2045 OWNER fix_delta P1-2: validate the built decision artifact
    against its own canonical schema BEFORE upload -- previously nothing
    ever checked that the `evidence` sub-object we emitted actually matched
    the schema's closed `evidence` $def."""
    try:
        schema_doc = json.loads(visual_impact_schema_path.read_text(encoding="utf-8"))
        sub_schema = {"$defs": schema_doc["$defs"], **schema_doc["$defs"]["VISUAL_IMPACT_DECISION_V1"]}
        jsonschema.validate(decision, sub_schema)
    except jsonschema.ValidationError as exc:
        return [f"decision_schema_invalid: {exc.message}"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        return [f"decision_schema_validation_error: {exc}"]
    return []


# --- Issue #2019 AC30-AC37 (P0-6 trust boundary): trusted consumer -------
# `verify_trusted_artifact()` below is invoked ONLY by the separate
# `workflow_run`-triggered `.github/workflows/visual-impact-trusted-consumer.yml`
# job (via this module's `--mode verify-trusted-artifact` CLI entry point).
# That workflow's own definition is always resolved from the base/default
# branch -- a GitHub Actions `workflow_run` trigger property that a
# candidate PR head cannot influence -- and it never checks out or executes
# the candidate PR head's code. This function therefore treats every byte
# of the producer's `visual-impact-decision-v1` artifact and the
# `component-vrt-evidence-manifest` artifact as fully UNTRUSTED input
# produced by a run whose job definition/steps a candidate PR CAN alter
# (e.g. forcing `exit 0`, deleting the materialize step, weakening schema
# checks): it independently re-derives trust from schema/digest/identity
# checks alone, never from the producer job's own reported conclusion.

TRUSTED_ARTIFACT_MAX_DECISION_BYTES = 1_000_000
TRUSTED_ARTIFACT_MAX_EVIDENCE_MANIFEST_BYTES = 5_000_000


@dataclass
class TrustedArtifactVerdict:
    ok: bool
    reason_codes: list[str] = field(default_factory=list)
    decision: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "VISUAL_IMPACT_TRUSTED_CONSUMER_VERDICT_V1",
            "ok": self.ok,
            "reason_codes": self.reason_codes,
        }


COMPONENT_VRT_CHECKRUN_NAME = "component-vrt-report"
GITHUB_ACTIONS_APP_ID = 15368
GITHUB_ACTIONS_APP_SLUG = "github-actions"


@dataclass(frozen=True)
class ComponentVrtCheckrunProvenanceResult:
    """Result of the base-locked component-VRT job/CheckRun authentication.

    Issue #2100 PR #2229 review fix_delta P1-1: when `ok` is True, the
    fields below carry the AUTHENTICATED identity independently fetched
    from the GitHub API (never copied from the producer's self-reported
    decision artifact), so `verify_trusted_artifact()` can cross-check a
    producer's self-reported `component_vrt_report_check_run_id` /
    `github_actions_app_identity` claims against this authenticated
    identity rather than trusting the self-report unconditionally. All
    fields remain `None` when `ok` is False (a rejected/partial
    authentication carries no trustworthy identity to cross-check
    against)."""

    ok: bool
    reason_codes: list[str]
    check_run_id: int | None = None
    job_id: int | None = None
    workflow_run_id: int | None = None
    run_attempt: int | None = None
    head_sha: str | None = None
    app_id: int | None = None
    app_slug: str | None = None


def verify_component_vrt_checkrun_provenance(
    *,
    check_run: object,
    workflow_jobs: object,
    jobs_complete: bool,
    expected_workflow_run_id: object,
    expected_run_attempt: object,
    expected_head_sha: object,
    expected_repository: object,
) -> ComponentVrtCheckrunProvenanceResult:
    """Fail closed unless an exact CheckRun belongs to this run *attempt*.

    Both payloads are independently obtained by the base-locked consumer.
    This deliberately verifies only cross-run/attempt substitution; a
    candidate-controlled producer inside the same run remains #2101's
    attestation boundary.
    """
    reasons: list[str] = []
    if jobs_complete is not True:
        reasons.append("component_vrt_jobs_incomplete")
    if type(expected_workflow_run_id) is not int or expected_workflow_run_id <= 0:
        reasons.append("component_vrt_expected_workflow_run_invalid")
    if type(expected_run_attempt) is not int or expected_run_attempt <= 0:
        reasons.append("component_vrt_expected_run_attempt_invalid")
    if not isinstance(expected_head_sha, str) or not expected_head_sha:
        reasons.append("component_vrt_expected_head_sha_invalid")
    if not isinstance(expected_repository, str) or expected_repository.count("/") != 1:
        reasons.append("component_vrt_expected_repository_invalid")

    if not isinstance(workflow_jobs, list) or any(not isinstance(job, dict) for job in workflow_jobs):
        reasons.append("component_vrt_jobs_payload_invalid")
        matches: list[dict[str, Any]] = []
    else:
        job_ids: set[int] = set()
        for candidate in workflow_jobs:
            job_id = candidate.get("id")
            if type(job_id) is not int or job_id <= 0:
                reasons.append("component_vrt_job_id_invalid")
                continue
            if job_id in job_ids:
                reasons.append("component_vrt_job_id_duplicate")
                continue
            job_ids.add(job_id)
        matches = [job for job in workflow_jobs if job.get("name") == COMPONENT_VRT_CHECKRUN_NAME]
    if len(matches) != 1:
        reasons.append("component_vrt_job_cardinality_invalid")
        job: dict[str, Any] = {}
    else:
        job = matches[0]

    check_run_id: int | None = None
    if not isinstance(check_run, dict):
        reasons.append("component_vrt_check_run_payload_invalid")
        check: dict[str, Any] = {}
    else:
        check = check_run
        raw_id = check.get("id")
        if type(raw_id) is not int or raw_id <= 0:
            reasons.append("component_vrt_check_run_id_invalid")
        else:
            check_run_id = raw_id
        if check.get("name") != COMPONENT_VRT_CHECKRUN_NAME:
            reasons.append("component_vrt_check_run_name_mismatch")
        if check.get("head_sha") != expected_head_sha:
            reasons.append("component_vrt_check_run_head_sha_mismatch")
        if check.get("status") != "completed":
            reasons.append("component_vrt_check_run_status_invalid")
        if check.get("conclusion") != "success":
            reasons.append("component_vrt_check_run_conclusion_invalid")
        app = check.get("app")
        if not isinstance(app, dict):
            reasons.append("component_vrt_check_run_app_invalid")
        else:
            if type(app.get("id")) is not int:
                reasons.append("component_vrt_check_run_app_id_invalid")
            elif app.get("id") != GITHUB_ACTIONS_APP_ID:
                reasons.append("component_vrt_check_run_app_id_mismatch")
            if not isinstance(app.get("slug"), str):
                reasons.append("component_vrt_check_run_app_slug_invalid")
            elif app.get("slug") != GITHUB_ACTIONS_APP_SLUG:
                reasons.append("component_vrt_check_run_app_slug_mismatch")

    if job:
        if type(job.get("run_id")) is not int:
            reasons.append("component_vrt_job_run_id_invalid")
        elif job.get("run_id") != expected_workflow_run_id:
            reasons.append("component_vrt_check_run_workflow_mismatch")
        if type(job.get("run_attempt")) is not int:
            reasons.append("component_vrt_job_run_attempt_invalid")
        elif job.get("run_attempt") != expected_run_attempt:
            reasons.append("component_vrt_check_run_attempt_mismatch")
        if job.get("head_sha") != expected_head_sha:
            reasons.append("component_vrt_job_head_sha_mismatch")
        if job.get("name") != COMPONENT_VRT_CHECKRUN_NAME:
            reasons.append("component_vrt_job_name_mismatch")
        if job.get("conclusion") != "success":
            reasons.append("component_vrt_job_conclusion_invalid")
        expected_url = (
            f"https://api.github.com/repos/{expected_repository}/check-runs/{check_run_id}"
            if check_run_id is not None
            else None
        )
        if expected_url is None or job.get("check_run_url") != expected_url:
            reasons.append("component_vrt_job_check_run_relation_mismatch")

    ok = not reasons
    app = check.get("app") if isinstance(check, dict) else None
    return ComponentVrtCheckrunProvenanceResult(
        ok=ok,
        reason_codes=reasons,
        check_run_id=check_run_id if ok else None,
        job_id=job.get("id") if ok and job else None,
        workflow_run_id=job.get("run_id") if ok and job else None,
        run_attempt=job.get("run_attempt") if ok and job else None,
        head_sha=expected_head_sha if ok else None,
        app_id=app.get("id") if ok and isinstance(app, dict) else None,
        app_slug=app.get("slug") if ok and isinstance(app, dict) else None,
    )


# --- Issue #2100 PR #2229 review fix_delta P1-3: executable, injectable- --
# transport acquisition of the attempt-scoped Actions jobs list and the
# exact `component-vrt-report` CheckRun. This REPLACES the equivalent logic
# formerly implemented only as inline `gh api`/`jq` shell text inside
# `.github/workflows/visual-impact-trusted-consumer.yml` -- that inline
# shell had no executable/current-head runtime test coverage (only
# grep-style string assertions against the workflow YAML text). This
# function is a pure, side-effect-free acquisition path: the CALLER injects
# an HTTP transport callable (a fake/fixture in tests, a real `gh api`
# subprocess wrapper in production -- see `_gh_api_transport()` below), so
# every pagination/cardinality/URL/response-shape edge case can be exercised
# directly, without a real GitHub API or network dependency.


@dataclass(frozen=True)
class HttpTransportResponse:
    """A single injected HTTP response for `acquire_component_vrt_checkrun()`'s
    transport callable. `status_code` is the REAL HTTP status code (never
    inferred from `json_body`); `json_body` is the parsed JSON response body
    (`None` when the body was not valid JSON -- callers MUST treat that as a
    malformed response, never silently coerce to an empty/default value)."""

    status_code: int
    json_body: Any = None
    raw_text: str = ""


@dataclass(frozen=True)
class ComponentVrtCheckrunAcquisitionResult:
    """Result of `acquire_component_vrt_checkrun()`."""

    ok: bool
    reason_codes: list[str]
    jobs: list[dict[str, Any]] | None = None
    check_run: dict[str, Any] | None = None
    check_run_id: int | None = None


def acquire_component_vrt_checkrun(
    *,
    transport: Callable[[str], HttpTransportResponse],
    repository: str,
    run_id: int,
    run_attempt: int,
    page_size: int = 100,
    max_pages: int = 1000,
) -> ComponentVrtCheckrunAcquisitionResult:
    """Trusted-side (base-locked consumer) acquisition of the attempt-scoped
    Actions jobs list and the exact `component-vrt-report` CheckRun.

    `transport` receives an API path (e.g. `"repos/<repo>/actions/runs/<id>/
    attempts/<attempt>/jobs?per_page=100&page=1"`) and MUST return an
    `HttpTransportResponse` -- a non-2xx response is a normal, fail-closed
    -handled outcome here, never an exception. Every failure path returns
    `ok=False` with a stable reason code; there is no partial/best-effort
    success path."""
    reasons: list[str] = []
    jobs: list[dict[str, Any]] = []
    job_ids: set[int] = set()
    total_count: int | None = None
    page = 1
    while True:
        if page > max_pages:
            reasons.append("component_vrt_acquire_max_pages_exceeded")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        path = (
            f"repos/{repository}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
            f"?per_page={page_size}&page={page}"
        )
        response = transport(path)
        if response.status_code != 200:
            reasons.append("component_vrt_acquire_jobs_http_status_invalid")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        body = response.json_body
        if not isinstance(body, dict):
            reasons.append("component_vrt_acquire_jobs_response_invalid")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        page_total = body.get("total_count")
        if type(page_total) is not int or page_total < 0:
            reasons.append("component_vrt_acquire_jobs_total_count_invalid")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        page_jobs = body.get("jobs")
        if not isinstance(page_jobs, list) or any(not isinstance(job, dict) for job in page_jobs):
            reasons.append("component_vrt_acquire_jobs_response_invalid")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        if len(page_jobs) > page_size:
            reasons.append("component_vrt_acquire_jobs_page_oversized")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        if total_count is None:
            total_count = page_total
        elif total_count != page_total:
            reasons.append("component_vrt_acquire_jobs_total_count_changed")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)

        for job in page_jobs:
            job_id = job.get("id")
            if type(job_id) is not int or job_id <= 0:
                reasons.append("component_vrt_acquire_job_id_invalid")
                return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
            if job_id in job_ids:
                reasons.append("component_vrt_acquire_job_id_duplicate")
                return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
            job_ids.add(job_id)
        jobs.extend(page_jobs)

        if len(jobs) > total_count:
            reasons.append("component_vrt_acquire_jobs_pagination_exceeded_total")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        if len(jobs) == total_count:
            break
        if len(page_jobs) == 0:
            reasons.append("component_vrt_acquire_jobs_pagination_incomplete")
            return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons)
        page += 1

    matches = [job for job in jobs if job.get("name") == COMPONENT_VRT_CHECKRUN_NAME]
    if len(matches) != 1:
        reasons.append("component_vrt_acquire_component_job_cardinality_invalid")
        return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons, jobs=jobs)
    job = matches[0]

    check_run_url = job.get("check_run_url")
    if not isinstance(check_run_url, str) or not check_run_url:
        reasons.append("component_vrt_acquire_check_run_url_invalid")
        return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons, jobs=jobs)
    expected_prefix = f"https://api.github.com/repos/{repository}/check-runs/"
    if not check_run_url.startswith(expected_prefix):
        reasons.append("component_vrt_acquire_check_run_url_not_canonical")
        return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons, jobs=jobs)
    raw_id = check_run_url[len(expected_prefix) :]
    if not re.fullmatch(r"[1-9][0-9]*", raw_id):
        reasons.append("component_vrt_acquire_check_run_id_invalid")
        return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons, jobs=jobs)
    check_run_id = int(raw_id)

    check_response = transport(f"repos/{repository}/check-runs/{check_run_id}")
    if check_response.status_code != 200:
        reasons.append("component_vrt_acquire_check_run_http_status_invalid")
        return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons, jobs=jobs)
    if not isinstance(check_response.json_body, dict):
        reasons.append("component_vrt_acquire_check_run_response_invalid")
        return ComponentVrtCheckrunAcquisitionResult(ok=False, reason_codes=reasons, jobs=jobs)

    return ComponentVrtCheckrunAcquisitionResult(
        ok=True,
        reason_codes=[],
        jobs=jobs,
        check_run=check_response.json_body,
        check_run_id=check_run_id,
    )


@dataclass(frozen=True)
class TrustedArtifactAcquisitionResult:
    """Result of `acquire_trusted_artifact()` (Issue #2230)."""

    ok: bool
    reason_codes: list[str]
    artifact_id: int | None = None
    artifacts: list[dict[str, Any]] | None = None


def acquire_trusted_artifact(
    *,
    transport: Callable[[str], HttpTransportResponse],
    repository: str,
    run_id: int,
    expected_artifact_name: str,
    page_size: int = 100,
    max_pages: int = 1000,
) -> TrustedArtifactAcquisitionResult:
    """Issue #2230 AC3/AC4: trusted-side (base-locked consumer) acquisition
    of a single attempt-specific producer artifact by exact name, replacing
    the formerly inline `gh api .../artifacts?per_page=100 | jq
    '[.artifacts[] | select(.name=="...")][0]'` pattern in
    `visual-impact-trusted-consumer.yml` (no pagination-completion check, no
    cardinality check, no `expired` check -- it silently adopted the FIRST
    same-named artifact regardless of which run attempt produced it).

    `transport` receives an API path (e.g. `"repos/<repo>/actions/runs/<id>/
    artifacts?name=<name>&per_page=100&page=1"`) and MUST return an
    `HttpTransportResponse` -- a non-2xx response is a normal, fail-closed
    -handled outcome here, never an exception. The GitHub REST artifacts-list
    endpoint's `name` query parameter IS server-side supported (Issue #2230
    Current Validated Scope fact-check), but this function never trusts the
    server-side filter alone: every returned artifact is re-checked against
    `expected_artifact_name` (and `expired == false`) client-side too, and
    every failure path returns `ok=False` with a stable reason code -- there
    is no partial/best-effort success path."""
    reasons: list[str] = []
    artifacts: list[dict[str, Any]] = []
    artifact_ids: set[int] = set()
    total_count: int | None = None
    page = 1
    while True:
        if page > max_pages:
            reasons.append("trusted_artifact_max_pages_exceeded")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        path = (
            f"repos/{repository}/actions/runs/{run_id}/artifacts"
            f"?name={expected_artifact_name}&per_page={page_size}&page={page}"
        )
        response = transport(path)
        if response.status_code != 200:
            reasons.append("trusted_artifact_http_status_invalid")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        body = response.json_body
        if not isinstance(body, dict):
            reasons.append("trusted_artifact_response_invalid")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        page_total = body.get("total_count")
        if type(page_total) is not int or page_total < 0:
            reasons.append("trusted_artifact_total_count_invalid")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        page_artifacts = body.get("artifacts")
        if not isinstance(page_artifacts, list) or any(not isinstance(a, dict) for a in page_artifacts):
            reasons.append("trusted_artifact_response_invalid")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        if len(page_artifacts) > page_size:
            reasons.append("trusted_artifact_page_oversized")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        if total_count is None:
            total_count = page_total
        elif total_count != page_total:
            reasons.append("trusted_artifact_total_count_changed")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)

        for artifact in page_artifacts:
            artifact_id = artifact.get("id")
            if type(artifact_id) is not int or artifact_id <= 0:
                reasons.append("trusted_artifact_id_invalid")
                return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
            if artifact_id in artifact_ids:
                reasons.append("trusted_artifact_id_duplicate")
                return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
            artifact_ids.add(artifact_id)
        artifacts.extend(page_artifacts)

        if len(artifacts) > total_count:
            reasons.append("trusted_artifact_pagination_exceeded_total")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        if len(artifacts) == total_count:
            break
        if len(page_artifacts) == 0:
            reasons.append("trusted_artifact_pagination_incomplete")
            return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons)
        page += 1

    # Issue #2230 AC3/AC4: never trust the server-side `name=` filter alone
    # -- re-check client-side, reject `expired` artifacts, and require
    # cardinality exactly one for the expected attempt-specific name (0 or
    # >1 matches, including old-attempt/current-attempt coexistence under
    # the SAME name, is fail-closed).
    matches = [a for a in artifacts if a.get("name") == expected_artifact_name and a.get("expired") is False]
    if len(matches) != 1:
        reasons.append(f"trusted_artifact_cardinality_invalid:{expected_artifact_name}")
        return TrustedArtifactAcquisitionResult(ok=False, reason_codes=reasons, artifacts=artifacts)

    return TrustedArtifactAcquisitionResult(ok=True, reason_codes=[], artifact_id=matches[0]["id"], artifacts=artifacts)


def _gh_api_transport(path: str) -> HttpTransportResponse:
    """Production transport for `acquire_component_vrt_checkrun()`: shells
    out to the `gh` CLI (which reads `GH_TOKEN`/`GITHUB_TOKEN` from the
    environment -- this function never handles or echoes a credential
    itself). Issue #2100 PR #2229 review fix_delta P2: pins the GitHub REST
    API version and `Accept` media type explicitly rather than relying on
    `gh api`'s current defaults."""
    result = subprocess.run(
        [
            "gh",
            "api",
            "--include",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout
    separator = "\r\n\r\n" if "\r\n\r\n" in output else "\n\n"
    head, _, body = output.partition(separator)
    status_line = head.splitlines()[0] if head else ""
    status_match = re.search(r"\s(\d{3})\s", f" {status_line} ")
    status_code = int(status_match.group(1)) if status_match else 0
    json_body: Any = None
    if body.strip():
        try:
            json_body = json.loads(body)
        except json.JSONDecodeError:
            json_body = None
    return HttpTransportResponse(status_code=status_code, json_body=json_body, raw_text=body)


def _run_acquire_trusted_artifact(args: argparse.Namespace) -> int:
    if not args.repository or not args.run_id or not args.expected_artifact_name:
        output = {
            "schema": "TRUSTED_ARTIFACT_ACQUISITION_RESULT_V1",
            "ok": False,
            "reason_codes": ["trusted_artifact_arguments_invalid"],
            "artifact_id": None,
        }
        print(json.dumps(output, indent=2))
        return 1
    result = acquire_trusted_artifact(
        transport=_gh_api_transport,
        repository=args.repository,
        run_id=args.run_id,
        expected_artifact_name=args.expected_artifact_name,
    )
    output = {
        "schema": "TRUSTED_ARTIFACT_ACQUISITION_RESULT_V1",
        "ok": result.ok,
        "reason_codes": result.reason_codes,
        "artifact_id": result.artifact_id,
    }
    print(json.dumps(output, indent=2))
    if args.artifact_id_output_file and result.ok:
        Path(args.artifact_id_output_file).write_text(str(result.artifact_id), encoding="utf-8")
    return 0 if result.ok else 1


def _run_acquire_component_vrt_checkrun(args: argparse.Namespace) -> int:
    if not args.repository or not args.run_id or not args.run_attempt:
        output = {
            "schema": "COMPONENT_VRT_CHECKRUN_ACQUISITION_RESULT_V1",
            "ok": False,
            "reason_codes": ["component_vrt_acquire_arguments_invalid"],
            "check_run_id": None,
        }
        print(json.dumps(output, indent=2))
        return 1
    result = acquire_component_vrt_checkrun(
        transport=_gh_api_transport,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    output = {
        "schema": "COMPONENT_VRT_CHECKRUN_ACQUISITION_RESULT_V1",
        "ok": result.ok,
        "reason_codes": result.reason_codes,
        "check_run_id": result.check_run_id,
    }
    print(json.dumps(output, indent=2))
    if args.jobs_output_file and result.jobs is not None:
        Path(args.jobs_output_file).write_text(json.dumps(result.jobs, indent=2), encoding="utf-8")
    if args.check_run_output_file and result.check_run is not None:
        Path(args.check_run_output_file).write_text(json.dumps(result.check_run, indent=2), encoding="utf-8")
    return 0 if result.ok else 1


@dataclass
class TrustedRederivation:
    """Issue #2091 AC1: bundles every input the trusted consumer workflow
    independently re-obtained/re-computed itself (NEVER copied from the
    producer's decision artifact), so `verify_trusted_artifact()` can cross
    check the producer's self-reported decision fields against them.
    Any field left `None` (or `changed_path_entries=None`) means the caller
    could not independently obtain that value -- the corresponding cross
    check is skipped for that field, never treated as an automatic pass.
    `changed_paths_complete=False` means the caller's changed-path set is
    KNOWN to be incomplete (e.g. it hit a paginated API's page-size cap) --
    this always fails closed regardless of digest matching, because an
    incomplete set can never prove the producer's `affected_surfaces`
    self-report is safe to trust."""

    expected_base_sha: str | None = None
    pr_body_raw: bytes | None = None
    changed_path_entries: list[dict[str, str]] | None = None
    changed_paths_complete: bool = True
    expected_base_registry_blob_sha: str | None = None
    expected_head_registry_blob_sha: str | None = None
    base_registry_doc: dict[str, Any] | None = None
    head_registry_doc: dict[str, Any] | None = None
    # Issue #2099 AC1: the candidate PR's live head SHA, as a Git commit
    # object the caller has ALREADY fetched (`git fetch --depth=1 origin
    # <sha>`) -- never a working-tree checkout. When set,
    # `resolve_trusted_minimum()` additionally walks the TS/CSS static
    # import graph reachable from each surface's registered producer entry
    # points via `git ls-tree`/`git cat-file` object reads only (see
    # `resolve_trusted_transitive_graph()` below). `None` means the caller
    # could not supply this (e.g. unit tests with no real git objects, or a
    # workflow invocation predating this capability) -- the coarse 5-signal
    # check still runs; this is a scoped, non-fatal omission of the
    # transitive-only detection, matching `resolve_trusted_minimum()`'s
    # existing "omits step 4" docstring contract for the non-git-backed path.
    candidate_head_ref: str | None = None
    # Issue #2099: the local Git repository to run `git ls-tree`/`git
    # cat-file`/`git show` against for the above. Test-only override --
    # production callers always leave this `None` (defaults to this
    # module's own `REPO_ROOT`, i.e. the checked-out base branch that
    # already has `candidate_head_ref` fetched as an object).
    repo_root: Path | None = None

    # Issue #2100: exact Actions attempt jobs and exact CheckRun response,
    # both independently fetched by the base-locked consumer. Legacy callers
    # retain their pre-#2100 behavior unless they opt into this required gate.
    component_vrt_checkrun_provenance: ComponentVrtCheckrunProvenanceResult | None = None
    require_component_vrt_checkrun_provenance: bool = False

# --- Issue #2099 AC1/AC2 (trusted-side TS import graph transitive --------
# reachability): a base-locked, read-only static import resolver that walks
# the import/export/CSS-@import/CSS-url()/`new URL(...)` graph of the
# CANDIDATE PR head using ONLY `git ls-tree`/`git cat-file` object reads
# against a commit the caller already fetched with `git fetch --depth=1`.
# It NEVER checks out, materializes to disk, or executes a single byte of
# candidate PR head code -- every byte read is parsed as inert text (regex
# specifier extraction only, never `eval`/`import()`/`require()`/any
# interpreter). This is deliberately NOT the TypeScript-compiler-API-backed
# resolver in `resolve_visual_impact.mjs` (that resolver requires a Node
# `typescript` package install and, more importantly, is only ever invoked
# by the untrusted PRODUCER job against an already-checked-out working
# tree) -- this is a narrower, independently-implemented "base-locked
# existing static-import resolver" (Issue #2099 In Scope) that intentionally
# does not understand tsconfig `paths`/`baseUrl`, Vite `resolve.alias`, or
# package.json `imports`/`exports` (same documented limitation as the
# producer-side resolver).


class GitReadOutcome(str, Enum):
    """Issue #2099 AC7: `git show <ref>:<path>` / `git cat-file` failure
    taxonomy. `MISSING` is the normal "this path does not exist at this
    ref" case (fail-open is safe: nothing to read). `PLUMBING_ERROR` is any
    OTHER git failure (network, shallow-fetch missing object, corrupted
    object, malformed ref) and must never be treated the same as `MISSING`
    -- doing so previously let a genuine plumbing failure silently degrade
    to "file doesn't exist" (`.github/workflows/visual-impact-trusted-consumer.yml`
    pre-#2099 `git show ... 2>/dev/null || rm -f ...` pattern)."""

    OK = "ok"
    MISSING = "missing"
    PLUMBING_ERROR = "plumbing_error"


_GIT_MISSING_PATH_PATTERNS = (
    "does not exist in",
    "exists on disk, but not in",
    "Invalid object name",
)


def read_git_blob_at_ref(
    repo_root: Path, ref: str, path: str, *, timeout_seconds: float | None = None
) -> tuple[GitReadOutcome, bytes | None, str]:
    """Issue #2099 AC7: read `<path>` as it exists at commit `<ref>` via
    `git show <ref>:<path>`, classifying the result into the three-way
    `GitReadOutcome` above instead of collapsing "file missing" and "git
    plumbing failure" into a single branch.

    `git show <ref>:<path>`'s own stderr text for an UNRESOLVABLE `ref`
    (never fetched, malformed, or otherwise not a valid object) can be
    indistinguishable from its "path missing at this (valid) ref" message
    when `path` happens to already exist in the caller's own working tree
    (git substitutes a "exists on disk, but not in '<ref>'" message either
    way) -- so `ref` resolvability is checked FIRST and independently via
    `git cat-file -e <ref>^{commit}`, before ever inspecting the `git show`
    stderr text for path-missing classification. Returns
    `(outcome, content_or_None, diagnostic_message)`. `timeout_seconds`
    (PR #2144 review fix_delta P1-3) bounds each subprocess call -- a
    stalled `git cat-file -e`/`git show` is classified as `PLUMBING_ERROR`
    (a genuine git failure), never an unbounded hang."""
    effective_timeout = timeout_seconds if timeout_seconds is not None else TRUSTED_TRANSITIVE_MAX_WALL_SECONDS
    try:
        ref_check = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-e", f"{ref}^{{commit}}"],
            capture_output=True,
            check=False,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            GitReadOutcome.PLUMBING_ERROR,
            None,
            f"git cat-file -e timed out after {effective_timeout}s resolving ref {ref!r}",
        )
    if ref_check.returncode != 0:
        return (
            GitReadOutcome.PLUMBING_ERROR,
            None,
            f"ref not resolvable as a commit object: {ref_check.stderr.decode('utf-8', 'replace')}",
        )
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{ref}:{path}"],
            capture_output=True,
            check=False,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            GitReadOutcome.PLUMBING_ERROR,
            None,
            f"git show timed out after {effective_timeout}s reading {path!r} at {ref!r}",
        )
    if proc.returncode == 0:
        return GitReadOutcome.OK, proc.stdout, ""
    stderr_text = proc.stderr.decode("utf-8", "replace")
    if any(pattern in stderr_text for pattern in _GIT_MISSING_PATH_PATTERNS):
        return GitReadOutcome.MISSING, None, stderr_text
    return GitReadOutcome.PLUMBING_ERROR, None, stderr_text


@dataclass
class GitTreeEntry:
    mode: str
    obj_type: str
    blob_sha: str
    size: int
    path: str


class TrustedTransitiveResourceBoundExceeded(RegistryError):
    """Issue #2099 AC4: raised (never silently swallowed into a truncated
    partial result) when a per-file byte / total byte / file count /
    import-hop depth / wall-clock bound is exceeded while walking the
    candidate head's static import graph."""


class TrustedGitPlumbingError(RegistryError):
    """Issue #2099 AC7 (PR #2144 review fix_delta P1-4): raised when a `git
    ls-tree` / `git cat-file blob` call in the trusted transitive graph
    walk fails for a GENUINE git plumbing reason (network hiccup, shallow
    fetch never fetched the object, corrupted object, unresolvable ref) --
    deliberately a DIFFERENT exception type from
    `TrustedTransitiveResourceBoundExceeded`, so `verify_trusted_artifact()`
    can report `trusted_transitive_git_plumbing_error:*` as a distinct
    reason code, never conflated with a resource-bound violation (Issue
    #2099 AC7 requires "missing / resource exhaustion / git plumbing
    failure" to stay distinct)."""


TRUSTED_TRANSITIVE_MAX_FILES = 4000
TRUSTED_TRANSITIVE_MAX_FILE_BYTES = 2_000_000
TRUSTED_TRANSITIVE_MAX_TOTAL_BYTES = 40_000_000
TRUSTED_TRANSITIVE_MAX_GRAPH_DEPTH = 40
TRUSTED_TRANSITIVE_MAX_WALL_SECONDS = 20.0

TRUSTED_TRANSITIVE_ASSET_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".mp3", ".wav",
    }
)
TRUSTED_TRANSITIVE_CANDIDATE_EXTENSIONS = ["", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs"]
TRUSTED_TRANSITIVE_INDEX_SUFFIXES = ["/index.ts", "/index.tsx"]

_TRUSTED_FROM_SPEC_RE = re.compile(r"""from\s+['"]([^'"]+)['"]""")
_TRUSTED_BARE_IMPORT_RE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.MULTILINE)
_TRUSTED_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*([^)]*?)\s*\)""")
_TRUSTED_STRING_LITERAL_RE = re.compile(r"""^['"]([^'"]+)['"]$""")
_TRUSTED_IMPORT_META_GLOB_RE = re.compile(r"""import\.meta\.glob\s*\(""")
_TRUSTED_NEW_URL_RE = re.compile(r"""new\s+URL\s*\(\s*['"]([^'"]+)['"]\s*,[^)]*import\.meta\.url[^)]*\)""")
_TRUSTED_CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\()?['"]([^'"]+)['"]\)?""")
_TRUSTED_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")


def _git_subprocess_timeout(deadline: float) -> float:
    """Issue #2099 AC4 (PR #2144 review fix_delta P1-3): every `git`
    subprocess.run() call inside the trusted transitive graph walk is
    bounded by a REMAINING-time timeout derived from one shared absolute
    wall-clock `deadline`, so a single stalled `git` process can never
    block past `TRUSTED_TRANSITIVE_MAX_WALL_SECONDS` regardless of how many
    subprocess calls preceded it -- `_check_bounds()`'s own deadline check
    only fires when control RETURNS to Python between subprocess calls, so
    it alone cannot bound a single stalled call."""
    return max(0.001, deadline - time.monotonic())


def git_ls_tree(
    repo_root: Path, ref: str, *, max_entries: int, timeout_seconds: float = TRUSTED_TRANSITIVE_MAX_WALL_SECONDS
) -> tuple[dict[str, GitTreeEntry], list[str]]:
    """Issue #2099 AC1/AC4: enumerate every path/blob-sha/size/mode at
    commit `ref` in ONE `git ls-tree -r -z -l` call (metadata only -- no
    blob content is read here). Bounded by `max_entries` (AC4 file-count
    resource bound); a plumbing failure (e.g. `ref` was never fetched
    locally) is reported as an error, never a silently-empty tree.
    `timeout_seconds` (PR #2144 review fix_delta P1-3) bounds the
    subprocess call itself -- a stalled `git ls-tree` is converted into a
    `resource_bound_exceeded:wall_time` error, never an unbounded hang."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-tree", "-r", "-z", "-l", ref],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {}, ["resource_bound_exceeded:wall_time:ls_tree"]
    if proc.returncode != 0:
        return {}, [f"git_plumbing_error:ls_tree:{proc.stderr.decode('utf-8', 'replace')}"]
    entries: dict[str, GitTreeEntry] = {}
    for raw in proc.stdout.split(b"\x00"):
        if not raw:
            continue
        text_line = raw.decode("utf-8", "replace")
        meta, _, path = text_line.partition("	")
        parts = meta.split()
        if len(parts) != 4:
            continue
        mode, obj_type, blob_sha, size_str = parts
        size = 0 if size_str == "-" else int(size_str)
        entries[path] = GitTreeEntry(mode=mode, obj_type=obj_type, blob_sha=blob_sha, size=size, path=path)
        if len(entries) > max_entries:
            return {}, ["resource_bound_exceeded:file_count"]
    return entries, []


def _trusted_is_relative_specifier(spec: str) -> bool:
    return spec.startswith("./") or spec.startswith("../")


def _trusted_is_absolute_specifier(spec: str) -> bool:
    return spec.startswith("/")


def _trusted_is_virtual_specifier(spec: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", spec)) and not spec.startswith("node:")


def _trusted_is_css_relative_specifier(spec: str) -> bool:
    """Issue #2099 AC2 (PR #2144 review fix_delta P1-2): CSS `url()` /
    `@import` relative-URL classification is DELIBERATELY different from
    the JS/TS module-specifier classification above
    (`_trusted_is_relative_specifier()`, which only accepts an explicit
    `./`/`../` prefix -- correct for JS/TS, where a bare specifier means a
    `node_modules` package). Per the CSS spec, a bare `bg.png` or
    `theme.css` (no `./` prefix) IS a valid relative URL resolved against
    the containing stylesheet -- treating it as an external/bare import
    (as the JS classifier would) causes it to silently vanish from the
    graph instead of being resolved or explicitly rejected. Returns `True`
    for any path-like target that is not a scheme-qualified URL (`data:`,
    `http:`, `https:`, or any other `scheme:` virtual specifier) and not
    root-absolute (`/...`, handled separately as `absolute_specifier_rejected`)."""
    if spec.startswith(("data:", "http://", "https://")):
        return False
    if _trusted_is_absolute_specifier(spec):
        return False
    if _trusted_is_virtual_specifier(spec):
        return False
    return True


def _trusted_resolve_relative_path(containing_path: str, spec: str) -> str | None:
    """Issue #2099 AC2/AC3: POSIX-join + normalize; returns `None` (reject)
    when the result would escape the repo root (`..` traversal) or is
    absolute -- this is the single confinement chokepoint every specifier
    kind below (module import/export, CSS `@import`/`url()`, `new URL(...)`)
    is routed through."""
    containing_dir = posixpath.dirname(containing_path)
    joined = posixpath.normpath(posixpath.join(containing_dir, spec))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        return None
    return joined


def _trusted_resolve_file_candidate(entries: dict[str, GitTreeEntry], no_ext_path: str) -> str | None:
    for ext in TRUSTED_TRANSITIVE_CANDIDATE_EXTENSIONS:
        candidate = no_ext_path + ext
        entry = entries.get(candidate)
        if entry is not None and entry.obj_type == "blob" and entry.mode != "120000":
            return candidate
    for suffix in TRUSTED_TRANSITIVE_INDEX_SUFFIXES:
        candidate = no_ext_path + suffix
        entry = entries.get(candidate)
        if entry is not None and entry.obj_type == "blob" and entry.mode != "120000":
            return candidate
    return None


def _trusted_find_symlink_conflict(entries: dict[str, GitTreeEntry], no_ext_path: str) -> str | None:
    """Issue #2099 AC3: when extension/index probing (above) finds no
    regular-file candidate, check whether a symlink entry (git mode
    `120000`) exists under one of the SAME candidate names -- so a
    specifier that would otherwise silently fall through to
    `unresolvable_static_import` is instead correctly reported as
    `symlink_entry_rejected` (never silently followed, never conflated with
    "the file just doesn't exist")."""
    for ext in TRUSTED_TRANSITIVE_CANDIDATE_EXTENSIONS:
        candidate = no_ext_path + ext
        entry = entries.get(candidate)
        if entry is not None and entry.mode == "120000":
            return candidate
    for suffix in TRUSTED_TRANSITIVE_INDEX_SUFFIXES:
        candidate = no_ext_path + suffix
        entry = entries.get(candidate)
        if entry is not None and entry.mode == "120000":
            return candidate
    return None


class TrustedGraphWalker:
    """Issue #2099 AC1/AC2/AC4: BFS-style walker over the candidate head's
    static import graph, backed entirely by `entries` (a `git ls-tree`
    metadata map) and on-demand `git cat-file blob <sha>` content reads.
    Every specifier is routed through `_trusted_resolve_relative_path()`
    (single confined resolver, AC2) and every resource bound raises
    `TrustedTransitiveResourceBoundExceeded` (fail-closed, AC4) instead of
    silently truncating the walk."""

    def __init__(self, entries: dict[str, GitTreeEntry], repo_root: Path, deadline: float) -> None:
        self.entries = entries
        self.repo_root = repo_root
        self.deadline = deadline
        self.visited: set[str] = set()
        self.reachable: set[str] = set()
        self.unknown_impact: list[dict[str, str]] = []
        self.total_bytes = 0
        self._blob_cache: dict[str, bytes] = {}

    def _check_bounds(self) -> None:
        if time.monotonic() > self.deadline:
            raise TrustedTransitiveResourceBoundExceeded("wall_time")
        if len(self.visited) > TRUSTED_TRANSITIVE_MAX_FILES:
            raise TrustedTransitiveResourceBoundExceeded("file_count")

    def _read_blob(self, path: str, entry: GitTreeEntry) -> bytes:
        if entry.size > TRUSTED_TRANSITIVE_MAX_FILE_BYTES:
            raise TrustedTransitiveResourceBoundExceeded("per_file_bytes")
        self.total_bytes += entry.size
        if self.total_bytes > TRUSTED_TRANSITIVE_MAX_TOTAL_BYTES:
            raise TrustedTransitiveResourceBoundExceeded("total_bytes")
        cached = self._blob_cache.get(entry.blob_sha)
        if cached is not None:
            return cached
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "cat-file", "blob", entry.blob_sha],
                capture_output=True,
                check=False,
                timeout=_git_subprocess_timeout(self.deadline),
            )
        except subprocess.TimeoutExpired as exc:
            # PR #2144 review fix_delta P1-3: a stalled `git cat-file blob`
            # is a wall-clock resource-bound violation, never an unbounded
            # hang past `TRUSTED_TRANSITIVE_MAX_WALL_SECONDS`.
            raise TrustedTransitiveResourceBoundExceeded(f"wall_time:cat_file:{path}") from exc
        if proc.returncode != 0:
            raise TrustedGitPlumbingError(f"cat_file:{path}:{proc.stderr.decode('utf-8', 'replace')}")
        self._blob_cache[entry.blob_sha] = proc.stdout
        return proc.stdout

    def _mark_asset(self, path: str) -> None:
        self.visited.add(path)
        self.reachable.add(path)

    def visit(self, path: str, depth: int) -> None:
        self._check_bounds()
        if depth > TRUSTED_TRANSITIVE_MAX_GRAPH_DEPTH:
            raise TrustedTransitiveResourceBoundExceeded("max_graph_depth")
        if path in self.visited:
            return
        self.visited.add(path)
        self.reachable.add(path)
        entry = self.entries.get(path)
        if entry is None or entry.mode == "120000":
            return
        raw = self._read_blob(path, entry)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.unknown_impact.append({"file": path, "kind": "binary_or_non_utf8", "detail": path})
            return
        ext = posixpath.splitext(path)[1].lower()
        if ext == ".css":
            self._visit_css_text(path, text, depth)
        else:
            self._visit_module_text(path, text, depth)

    def _dispatch_specifier(self, containing_path: str, spec: str, depth: int) -> None:
        base = spec
        suffix: str | None = None
        if base.endswith("?url"):
            base, suffix = base[:-4], "url"
        elif base.endswith("?raw"):
            base, suffix = base[:-4], "raw"

        if _trusted_is_virtual_specifier(base):
            self.unknown_impact.append({"file": containing_path, "kind": "virtual_module", "detail": spec})
            return
        if _trusted_is_absolute_specifier(base):
            # Issue #2099 AC2/AC3: unlike `resolve_visual_impact.mjs`'s
            # `isRelativeSpecifier()`, a leading `/` is NEVER treated as
            # relative here -- reject outright rather than resolve it.
            self.unknown_impact.append({"file": containing_path, "kind": "absolute_specifier_rejected", "detail": spec})
            return
        if not _trusted_is_relative_specifier(base):
            return  # bare/package import (node_modules) -- external, not an impact source.

        resolved_no_ext = _trusted_resolve_relative_path(containing_path, base)
        if resolved_no_ext is None:
            self.unknown_impact.append({"file": containing_path, "kind": "path_escape_rejected", "detail": spec})
            return

        target_entry = self.entries.get(resolved_no_ext)
        if target_entry is not None and target_entry.mode == "120000":
            self.unknown_impact.append({"file": containing_path, "kind": "symlink_entry_rejected", "detail": spec})
            return

        ext = posixpath.splitext(base)[1].lower()

        if suffix:
            candidate = resolved_no_ext if resolved_no_ext in self.entries else _trusted_resolve_file_candidate(
                self.entries, resolved_no_ext
            )
            if candidate is None:
                self.unknown_impact.append(
                    {"file": containing_path, "kind": "unresolvable_static_import", "detail": spec}
                )
                return
            self._mark_asset(candidate)
            return

        if ext == ".css":
            entry = self.entries.get(resolved_no_ext)
            if entry is not None and entry.mode != "120000":
                self.visit(resolved_no_ext, depth + 1)
            else:
                self.unknown_impact.append(
                    {"file": containing_path, "kind": "unresolvable_static_import", "detail": spec}
                )
            return

        if ext in TRUSTED_TRANSITIVE_ASSET_EXTENSIONS:
            entry = self.entries.get(resolved_no_ext)
            if entry is not None and entry.mode != "120000":
                self._mark_asset(resolved_no_ext)
            else:
                self.unknown_impact.append(
                    {"file": containing_path, "kind": "unresolvable_static_import", "detail": spec}
                )
            return

        candidate = _trusted_resolve_file_candidate(self.entries, resolved_no_ext)
        if candidate is None:
            symlink_conflict = _trusted_find_symlink_conflict(self.entries, resolved_no_ext)
            if symlink_conflict is not None:
                self.unknown_impact.append(
                    {"file": containing_path, "kind": "symlink_entry_rejected", "detail": spec}
                )
            else:
                self.unknown_impact.append(
                    {"file": containing_path, "kind": "unresolvable_static_import", "detail": spec}
                )
            return
        self.visit(candidate, depth + 1)

    def _visit_module_text(self, path: str, text: str, depth: int) -> None:
        for m in _TRUSTED_FROM_SPEC_RE.finditer(text):
            self._dispatch_specifier(path, m.group(1), depth)
        for m in _TRUSTED_BARE_IMPORT_RE.finditer(text):
            self._dispatch_specifier(path, m.group(1), depth)
        for m in _TRUSTED_NEW_URL_RE.finditer(text):
            self._dispatch_specifier(path, m.group(1), depth)
        for _m in _TRUSTED_IMPORT_META_GLOB_RE.finditer(text):
            self.unknown_impact.append({"file": path, "kind": "import_meta_glob", "detail": path})
        for m in _TRUSTED_DYNAMIC_IMPORT_RE.finditer(text):
            arg = m.group(1).strip()
            literal_match = _TRUSTED_STRING_LITERAL_RE.match(arg)
            if literal_match:
                self._dispatch_specifier(path, literal_match.group(1), depth)
            else:
                self.unknown_impact.append(
                    {"file": path, "kind": "dynamic_variable_import", "detail": arg or "import(...)"}
                )

    def _visit_css_text(self, path: str, text: str, depth: int) -> None:
        # PR #2144 review fix_delta P1-1/P1-2: CSS `@import`/`url()`
        # targets are routed through `_trusted_is_css_relative_specifier()`
        # (bare `theme.css`/`bg.png`, not just `./`-prefixed, is a valid
        # CSS relative URL -- P1-2) and normalized to a `./`-prefixed spec
        # before reaching the shared confined resolver / `_dispatch_specifier()`
        # (which otherwise treats a bare specifier as an external
        # bare/package import, the JS/TS semantics). A target that fails to
        # resolve at the candidate head (e.g. a previously-reachable CSS
        # asset that this PR deletes) is reported as `unresolvable_static_import`
        # -- fail-closed, never silently dropped from the graph (P1-1).
        for m in _TRUSTED_CSS_IMPORT_RE.finditer(text):
            spec = m.group(1)
            if _trusted_is_css_relative_specifier(spec):
                normalized = spec if _trusted_is_relative_specifier(spec) else f"./{spec}"
                self._dispatch_specifier(path, normalized, depth)
            elif _trusted_is_absolute_specifier(spec):
                self.unknown_impact.append({"file": path, "kind": "absolute_specifier_rejected", "detail": spec})
            # else: data:/http(s):/other scheme-qualified @import target --
            # external, not a local impact source.
        for m in _TRUSTED_CSS_URL_RE.finditer(text):
            spec = m.group(2)
            if spec.startswith(("data:", "http://", "https://", "#")):
                continue
            spec = spec.split("#")[0].split("?")[0]
            if _trusted_is_css_relative_specifier(spec):
                normalized = spec if _trusted_is_relative_specifier(spec) else f"./{spec}"
                resolved = _trusted_resolve_relative_path(path, normalized)
                if resolved is None:
                    self.unknown_impact.append({"file": path, "kind": "path_escape_rejected", "detail": spec})
                    continue
                entry = self.entries.get(resolved)
                if entry is not None and entry.mode == "120000":
                    self.unknown_impact.append({"file": path, "kind": "symlink_entry_rejected", "detail": spec})
                    continue
                if entry is not None:
                    self._mark_asset(resolved)
                else:
                    self.unknown_impact.append({"file": path, "kind": "unresolvable_static_import", "detail": spec})
            elif _trusted_is_absolute_specifier(spec):
                self.unknown_impact.append({"file": path, "kind": "absolute_specifier_rejected", "detail": spec})


def resolve_trusted_transitive_graph(
    entry_paths: list[str],
    entries: dict[str, GitTreeEntry],
    repo_root: Path,
    *,
    max_wall_seconds: float = TRUSTED_TRANSITIVE_MAX_WALL_SECONDS,
    deadline: float | None = None,
) -> tuple[set[str], list[dict[str, str]]]:
    """Issue #2099 AC1/AC2: walk the static import graph reachable from
    `entry_paths` (a surface's registered producer modules/styles/assets)
    at the candidate head represented by `entries`, entirely via Git object
    reads. Raises `TrustedTransitiveResourceBoundExceeded` (AC4, fail
    closed) rather than returning a silently-truncated partial graph.

    `deadline` (PR #2144 review fix_delta P1-3), when supplied, is an
    ALREADY-COMPUTED absolute `time.monotonic()` deadline shared across
    the entire verification (e.g. across every registered surface's walk
    in `resolve_trusted_minimum()`) -- this prevents the resource budget
    from silently resetting per-surface. When omitted (e.g. direct-call
    unit tests), a fresh deadline is computed from `max_wall_seconds`,
    matching the prior single-call behaviour."""
    effective_deadline = deadline if deadline is not None else time.monotonic() + max_wall_seconds
    walker = TrustedGraphWalker(entries, repo_root, effective_deadline)
    for entry_path in entry_paths:
        joined = posixpath.normpath(entry_path)
        if joined == ".." or joined.startswith("../") or joined.startswith("/"):
            walker.unknown_impact.append({"file": entry_path, "kind": "path_escape_rejected", "detail": entry_path})
            continue
        entry = entries.get(joined)
        if entry is None:
            continue  # not present at candidate head -- nothing to walk.
        if entry.mode == "120000":
            walker.unknown_impact.append({"file": joined, "kind": "symlink_entry_rejected", "detail": joined})
            continue
        walker.visit(joined, 0)
    return walker.reachable, walker.unknown_impact


def resolve_trusted_minimum(
    changed_paths: list[str],
    base_doc: dict[str, Any],
    head_doc: dict[str, Any],
    *,
    candidate_head_ref: str | None = None,
    repo_root: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Issue #2091 AC1/AC2 + Issue #2099 AC1: an independently-computable
    subset of `resolve()`'s affected-surface logic that the trusted-consumer
    workflow can run WITHOUT checking out or executing any candidate-PR-head
    code.

    Reuses steps 0/1/2/3/5 of `resolve()` verbatim (meta policy paths /
    global invalidators / direct producer+contract paths / registry-union
    mapping deletion / coverage-root boundary) against TRUSTED-side
    (base_sha/head_sha) registries and a TRUSTED-side changed-path set.

    When `candidate_head_ref` is supplied (a candidate PR head commit the
    caller has ALREADY fetched as a Git object via `git fetch --depth=1`,
    never checked out to a working tree), this additionally runs a step 4:
    a base-locked, read-only static-import-graph walk
    (`resolve_trusted_transitive_graph()`) over that fetched commit's Git
    objects (`git ls-tree`/`git cat-file` reads only -- never TypeScript's
    own compiler API, never `resolve_visual_impact.mjs`/Node, and never any
    disk materialization of candidate PR head content). This is
    deliberately a DIFFERENT, narrower resolver than the TS-compiler-API
    one in `resolve_visual_impact.mjs` (see that walker's module docstring)
    -- a "base-locked existing static-import resolver", not a TypeScript
    compiler import graph (Issue #2099 In Scope). When `candidate_head_ref`
    is `None` (e.g. unit tests with no real git objects, or a caller that
    predates this capability), step 4 is skipped entirely -- a scoped,
    documented omission, never a silent gap in the CALLER's overall
    fail-closed posture, because `verify_trusted_artifact()` still requires
    every OTHER signal to pass.

    Returns `(affected_surface_ids, unmapped_visual_candidates)` -- same
    shapes as the corresponding parts of `ResolveResult`. Raises
    `TrustedTransitiveResourceBoundExceeded` (Issue #2099 AC4) if step 4 is
    active and exceeds a resource bound -- the caller must treat this as an
    unconditional fail-closed verdict, never a silent partial result."""
    union_surfaces: dict[str, Any] = dict(head_doc.get("surfaces", {}) or {})
    for surface_id, base_def in (base_doc.get("surfaces", {}) or {}).items():
        if surface_id not in union_surfaces:
            union_surfaces[surface_id] = base_def

    global_invalidators = set(head_doc.get("global_invalidators", []) or []) | set(
        base_doc.get("global_invalidators", []) or []
    )
    coverage_roots = list(
        dict.fromkeys((head_doc.get("coverage_roots", []) or []) + (base_doc.get("coverage_roots", []) or []))
    )

    changed_set = set(changed_paths)
    affected_surface_ids: dict[str, str] = {}

    if changed_set & META_POLICY_PATHS:
        for surface_id in union_surfaces:
            affected_surface_ids.setdefault(surface_id, "meta_policy_change")

    if changed_set & global_invalidators:
        for surface_id in union_surfaces:
            affected_surface_ids.setdefault(surface_id, "global_invalidator")

    for surface_id, surface_def in union_surfaces.items():
        if surface_id in affected_surface_ids:
            continue
        direct_paths = collect_producer_paths(surface_def) | collect_contract_paths(surface_def)
        if direct_paths & changed_set:
            affected_surface_ids[surface_id] = "direct_producer"

    for surface_id in diff_producer_mappings(base_doc, head_doc):
        affected_surface_ids.setdefault(surface_id, "mapping_deleted")

    all_producer_paths: set[str] = set(global_invalidators)
    for surface_def in union_surfaces.values():
        all_producer_paths |= collect_producer_paths(surface_def)
        all_producer_paths |= collect_contract_paths(surface_def)

    if candidate_head_ref is not None:
        root = repo_root if repo_root is not None else REPO_ROOT
        # PR #2144 review fix_delta P1-3: ONE absolute deadline shared
        # across `git_ls_tree()` and every surface's subsequent graph walk
        # below -- never a fresh per-surface deadline (which would let the
        # resource budget silently reset/amplify across many registered
        # surfaces).
        transitive_deadline = time.monotonic() + TRUSTED_TRANSITIVE_MAX_WALL_SECONDS
        entries, ls_tree_errors = git_ls_tree(
            root,
            candidate_head_ref,
            max_entries=TRUSTED_TRANSITIVE_MAX_FILES,
            timeout_seconds=_git_subprocess_timeout(transitive_deadline),
        )
        if ls_tree_errors:
            # PR #2144 review fix_delta P1-4: a genuine git plumbing
            # failure (`git_plumbing_error:ls_tree:...`) must never be
            # conflated with a resource-bound violation
            # (`resource_bound_exceeded:...`) -- Issue #2099 AC7 requires
            # "missing / resource exhaustion / git plumbing failure" to
            # stay distinct all the way to the final verdict's reason code.
            joined_errors = ";".join(ls_tree_errors)
            if any(err.startswith("resource_bound_exceeded:") for err in ls_tree_errors):
                raise TrustedTransitiveResourceBoundExceeded(joined_errors)
            raise TrustedGitPlumbingError(f"ls_tree:{joined_errors}")
        request = build_mjs_request(root, union_surfaces)
        for surface_id, surface_entries in request["surfaces"].items():
            entry_paths: list[str] = []
            for key in ("modules", "styles", "assets", "config"):
                entry_paths.extend(surface_entries.get(key, []) or [])
            reachable, unknown = resolve_trusted_transitive_graph(
                entry_paths, entries, root, deadline=transitive_deadline
            )
            all_producer_paths |= reachable
            if reachable & changed_set and surface_id not in affected_surface_ids:
                affected_surface_ids[surface_id] = "producer_reachable_transitive"
            if unknown and surface_id not in affected_surface_ids:
                affected_surface_ids[surface_id] = "trusted_transitive_unknown_impact"

    unmapped_visual_candidates: list[str] = []
    for changed_path in changed_paths:
        if changed_path in all_producer_paths:
            continue
        if match_coverage_roots(changed_path, coverage_roots):
            unmapped_visual_candidates.append(changed_path)

    return affected_surface_ids, unmapped_visual_candidates


# PR #2229 review fix_delta P1-2 (scope narrowing, not an implementation
# gap in THIS function): `verify_trusted_artifact()` proves that the
# component-vrt CheckRun/decision/pr_body/changed-paths/registry blobs it
# cross-checks belong to the exact triggering run_attempt (via
# `verify_component_vrt_checkrun_provenance()`). It does NOT additionally
# bind the `visual-impact-decision-v1` / `component-vrt-evidence-manifest`
# ARTIFACT bytes themselves to that same run_attempt -- the GitHub REST
# artifact-list API has no `attempt_number` filter and artifact objects
# carry no attempt-identity field, so the caller workflow's `[0]` pick of a
# same-named artifact cannot be made attempt-exact without a `ci.yml`
# change, which is outside this Issue's Allowed Paths. This is tracked as
# a separate, explicit follow-up: Issue #2230.
def verify_trusted_artifact(
    *,
    decision_raw: bytes | None,
    evidence_manifest_raw: bytes | None,
    visual_impact_schema_path: Path,
    expected_head_sha: str,
    expected_repository: str,
    expected_pr_number: int,
    max_decision_bytes: int = TRUSTED_ARTIFACT_MAX_DECISION_BYTES,
    max_evidence_manifest_bytes: int = TRUSTED_ARTIFACT_MAX_EVIDENCE_MANIFEST_BYTES,
    trusted_rederivation: TrustedRederivation | None = None,
) -> TrustedArtifactVerdict:
    """AC31-AC35: independently re-verify a producer-authored
    VISUAL_IMPACT_DECISION_V1 artifact (and its referenced evidence
    manifest) as untrusted input. Every failure path is fail-closed --
    missing/oversized/malformed/mismatched/tampered input never yields
    `ok=True`, regardless of what the producer job itself reported."""
    if decision_raw is None:
        return TrustedArtifactVerdict(False, ["missing_decision_artifact"], None)
    if len(decision_raw) > max_decision_bytes:
        return TrustedArtifactVerdict(False, ["decision_artifact_too_large"], None)
    try:
        decision = json.loads(decision_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return TrustedArtifactVerdict(False, [f"decision_not_json:{exc}"], None)
    if not isinstance(decision, dict):
        return TrustedArtifactVerdict(False, ["decision_not_object"], None)

    schema_errors = _validate_decision_schema(decision, visual_impact_schema_path)
    if schema_errors:
        return TrustedArtifactVerdict(False, schema_errors, decision)

    reasons: list[str] = []
    if decision.get("head_sha") != expected_head_sha:
        reasons.append("head_sha_mismatch")
    if decision.get("repository") != expected_repository:
        reasons.append("repository_mismatch")
    if decision.get("pull_request_number") != expected_pr_number:
        reasons.append("pull_request_number_mismatch")
    if decision.get("policy_version") != POLICY_VERSION:
        # Issue #2091 AC1: cross-checked against THIS base-locked module's
        # own constant -- never a caller-supplied/producer-supplied value --
        # so a candidate PR cannot smuggle a stale/forged policy_version
        # through a producer job it controls.
        reasons.append("policy_version_mismatch")

    # Issue #2091 AC3: `evidence: {}` coherent forgery -- a surface whose
    # disposition requires real evidence (anything other than `waived`)
    # but whose `evidence` object carries no `evidence_manifest_digest`
    # claim at all is rejected outright, independent of whether the
    # `surface_id` itself happens to be correct. A legitimate `ok=True`
    # verified_unchanged/baseline_changed surface always has a digest here
    # (see `_evidence_payload_for_decision`/`build_evidence_from_manifest_v2`
    # -- `manifest_sha256` is unconditionally computed for every real
    # manifest record), so this can never reject a genuine PASS.
    for surface in decision.get("affected_surfaces", []) or []:
        if not isinstance(surface, dict):
            continue
        if surface.get("disposition") == "waived":
            continue
        evidence = surface.get("evidence") or {}
        if not evidence.get("evidence_manifest_digest"):
            reasons.append(f"evidence_digest_claim_missing:{surface.get('surface_id')}")

    if trusted_rederivation is not None:
        tr = trusted_rederivation
        provenance = tr.component_vrt_checkrun_provenance
        if tr.require_component_vrt_checkrun_provenance:
            if provenance is None:
                reasons.append("component_vrt_checkrun_provenance_missing")
            elif not provenance.ok and not provenance.reason_codes:
                reasons.append("component_vrt_checkrun_provenance_rejected")
            else:
                reasons.extend(provenance.reason_codes)
        elif provenance is not None:
            # Preserve legacy optional rederivation behavior while retaining
            # any explicit provenance rejection supplied by a caller.
            reasons.extend(provenance.reason_codes)

        # Issue #2100 PR #2229 review fix_delta P1-1: the producer's
        # self-reported `component_vrt_report_check_run_id` /
        # `github_actions_app_identity` decision fields are UNTRUSTED input
        # -- the whole point of this trusted consumer is to never rely on
        # the producer job's own self-report. When the AUTHENTICATED
        # provenance above independently confirmed a real CheckRun belongs
        # to this exact run/attempt/head, the decision's self-reported
        # identity claims MUST match that authenticated identity, or the
        # decision is rejected even though the authenticated CheckRun
        # itself is genuine (otherwise a producer could report an
        # unrelated-but-genuine CheckRun ID/App identity untouched by the
        # provenance check above).
        if provenance is not None and provenance.ok:
            reported_check_run_id = decision.get("component_vrt_report_check_run_id")
            if (
                provenance.check_run_id is None
                or reported_check_run_id is None
                or str(reported_check_run_id) != str(provenance.check_run_id)
            ):
                reasons.append("component_vrt_report_check_run_id_decision_mismatch")

            reported_app_identity = decision.get("github_actions_app_identity")
            if provenance.app_id == GITHUB_ACTIONS_APP_ID and provenance.app_slug == GITHUB_ACTIONS_APP_SLUG:
                expected_app_identity = f"{GITHUB_ACTIONS_APP_SLUG}[bot]"
                if reported_app_identity != expected_app_identity:
                    reasons.append("github_actions_app_identity_decision_mismatch")
            else:
                reasons.append("github_actions_app_identity_decision_mismatch")

            # Issue #2230 AC2/AC5: the decision artifact's CONTENT (not
            # merely its attempt-specific artifact NAME, which the caller
            # workflow's `name=` filter already binds -- see
            # `acquire_trusted_artifact()`) must carry the exact
            # `(workflow_run_id, run_attempt, head_sha)` tuple the trusted
            # consumer already independently authenticated via the
            # `component-vrt-report` CheckRun provenance above. `head_sha`
            # itself is already cross-checked against `expected_head_sha`
            # earlier in this function; this only adds the two fields that
            # were previously unchecked (rerun stale-artifact freshness).
            reported_workflow_run_id = decision.get("workflow_run_id")
            if (
                provenance.workflow_run_id is None
                or reported_workflow_run_id is None
                or reported_workflow_run_id != provenance.workflow_run_id
            ):
                reasons.append("decision_workflow_run_id_mismatch")
            reported_run_attempt = decision.get("run_attempt")
            if (
                provenance.run_attempt is None
                or reported_run_attempt is None
                or reported_run_attempt != provenance.run_attempt
            ):
                reasons.append("decision_run_attempt_mismatch")

        if tr.expected_base_sha is not None and decision.get("base_sha") != tr.expected_base_sha:
            reasons.append("base_sha_mismatch")
        if tr.pr_body_raw is not None:
            expected_pr_body_sha256 = sha256_hex(tr.pr_body_raw)
            if decision.get("pr_body_sha256") != expected_pr_body_sha256:
                reasons.append("pr_body_digest_mismatch")
        if tr.expected_base_registry_blob_sha is not None:
            if decision.get("base_registry_blob_sha") != tr.expected_base_registry_blob_sha:
                reasons.append("base_registry_blob_sha_mismatch")
        if tr.expected_head_registry_blob_sha is not None:
            if decision.get("head_registry_blob_sha") != tr.expected_head_registry_blob_sha:
                reasons.append("head_registry_blob_sha_mismatch")
        if not tr.changed_paths_complete:
            # Issue #2091 In Scope: an incomplete changed-path set (e.g. a
            # paginated API page-size cap was hit) can never be trusted to
            # prove the producer's `affected_surfaces` claim is safe --
            # fail closed regardless of what else matches.
            reasons.append("changed_paths_incomplete_unknown")
        elif tr.changed_path_entries is not None:
            expected_digest = build_changed_paths_digest(tr.changed_path_entries)["digest"]
            actual_digest = (decision.get("changed_paths_digest") or {}).get("digest")
            if actual_digest != expected_digest:
                reasons.append("changed_paths_digest_mismatch")
            if tr.base_registry_doc is not None and tr.head_registry_doc is not None:
                trusted_changed_paths = _flat_paths_for_resolve(tr.changed_path_entries)
                try:
                    trusted_affected, trusted_unmapped = resolve_trusted_minimum(
                        trusted_changed_paths,
                        tr.base_registry_doc,
                        tr.head_registry_doc,
                        candidate_head_ref=tr.candidate_head_ref,
                        repo_root=tr.repo_root,
                    )
                except TrustedTransitiveResourceBoundExceeded as exc:
                    # Issue #2099 AC4: a resource bound violation while
                    # walking the candidate head's static import graph is a
                    # trust-signal failure, never a silent skip -- fail
                    # closed unconditionally, same as every other reason
                    # code in this function.
                    reasons.append(f"trusted_transitive_resource_bound_exceeded:{exc}")
                    trusted_affected, trusted_unmapped = {}, []
                except TrustedGitPlumbingError as exc:
                    # PR #2144 review fix_delta P1-4: a genuine git
                    # plumbing failure (ls-tree/cat-file) is a DISTINCT
                    # trust-signal failure from a resource-bound violation
                    # (Issue #2099 AC7) -- still fails closed
                    # unconditionally, but with its own stable reason code
                    # so the two failure classes are never conflated.
                    reasons.append(f"trusted_transitive_git_plumbing_error:{exc}")
                    trusted_affected, trusted_unmapped = {}, []
                claimed_surface_ids = {
                    s.get("surface_id")
                    for s in (decision.get("affected_surfaces") or [])
                    if isinstance(s, dict)
                }
                # Issue #2091 AC2: this is what actually rejects a forged
                # `affected_surfaces: []` (or any other undercounted claim)
                # -- independent of the producer's own evidence/artifact
                # self-report, and independent of `evidence_manifest_raw`
                # having been retrievable at all.
                for surface_id in trusted_affected:
                    if surface_id not in claimed_surface_ids:
                        reasons.append(f"affected_surfaces_undercount:{surface_id}")
                for path in trusted_unmapped:
                    reasons.append(f"unmapped_visual_candidate_undetected:{path}")

    if evidence_manifest_raw is None:
        # No evidence manifest artifact could be retrieved at all -- any
        # affected surface whose decision entry claims an
        # `evidence_manifest_digest` cannot be corroborated, so fail-closed
        # for those surfaces rather than silently accepting the decision's
        # self-report as sufficient (this is what catches a candidate PR
        # deleting the base-locked evaluator's materialize step, or the
        # producer job's evidence-manifest upload step, entirely).
        for surface in decision.get("affected_surfaces", []) or []:
            evidence = surface.get("evidence") or {}
            if evidence.get("evidence_manifest_digest"):
                reasons.append(f"evidence_manifest_missing:{surface.get('surface_id')}")
    else:
        if len(evidence_manifest_raw) > max_evidence_manifest_bytes:
            reasons.append("evidence_manifest_too_large")
        else:
            manifest: dict[str, Any] | None
            try:
                manifest = json.loads(evidence_manifest_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                manifest = None
                reasons.append(f"evidence_manifest_not_json:{exc}")
            if manifest is not None:
                if not isinstance(manifest, dict) or manifest.get("schema") != EVIDENCE_MANIFEST_V2_SCHEMA:
                    reasons.append("evidence_manifest_schema_invalid")
                for surface in decision.get("affected_surfaces", []) or []:
                    evidence = surface.get("evidence") or {}
                    digest_claim = evidence.get("evidence_manifest_digest")
                    if not digest_claim:
                        continue
                    record = find_evidence_manifest_v2_record(manifest, surface.get("surface_id"))
                    if record is None:
                        reasons.append(f"evidence_manifest_record_missing:{surface.get('surface_id')}")
                        continue
                    if not verify_evidence_manifest_v2_record_digest(record):
                        reasons.append(f"evidence_manifest_digest_tamper:{surface.get('surface_id')}")
                        continue
                    if record.get("manifest_sha256") != digest_claim:
                        reasons.append(f"evidence_manifest_digest_mismatch:{surface.get('surface_id')}")
                    # Issue #2230 AC2/AC5: the evidence manifest record's
                    # OWN `(workflow_run_id, run_attempt, head_sha)` tuple
                    # must also match the trusted consumer's authenticated
                    # CheckRun provenance -- an old-attempt evidence record
                    # (same digest scheme, different attempt) must never be
                    # accepted just because its per-record tamper-evidence
                    # digest self-verifies.
                    if trusted_rederivation is not None and trusted_rederivation.component_vrt_checkrun_provenance:
                        surf_provenance = trusted_rederivation.component_vrt_checkrun_provenance
                        if surf_provenance.ok:
                            if (
                                surf_provenance.workflow_run_id is None
                                or _coerce_mismatched_pixels(record.get("workflow_run_id"))
                                != surf_provenance.workflow_run_id
                            ):
                                reasons.append(
                                    f"evidence_manifest_workflow_run_id_mismatch:{surface.get('surface_id')}"
                                )
                            if (
                                surf_provenance.run_attempt is None
                                or _coerce_mismatched_pixels(record.get("run_attempt")) != surf_provenance.run_attempt
                            ):
                                reasons.append(f"evidence_manifest_run_attempt_mismatch:{surface.get('surface_id')}")
                        if record.get("head_sha") != expected_head_sha:
                            reasons.append(f"evidence_manifest_head_sha_mismatch:{surface.get('surface_id')}")

    return TrustedArtifactVerdict(ok=not reasons, reason_codes=reasons, decision=decision)


def _build_trusted_rederivation_from_args(args: argparse.Namespace) -> tuple[TrustedRederivation, list[str]]:
    """Issue #2091 AC1: assemble a `TrustedRederivation` from CLI-supplied
    file paths -- every value here MUST come from data the trusted-consumer
    workflow independently fetched itself (never the producer artifact).
    Returns `(trusted_rederivation, load_errors)`; a non-empty
    `load_errors` means a trusted-side input the caller claimed to supply
    could not actually be loaded/validated -- the caller must treat this as
    an unconditional fail-closed verdict (never silently degrade to
    "field not supplied")."""
    load_errors: list[str] = []

    provenance_inputs = (
        getattr(args, "component_vrt_jobs_file", None),
        getattr(args, "component_vrt_check_run_file", None),
        getattr(args, "expected_workflow_run_id", None),
        getattr(args, "expected_workflow_run_attempt", None),
        getattr(args, "component_vrt_jobs_complete", None),
    )
    if all(value is None for value in provenance_inputs):
        component_vrt_provenance = ComponentVrtCheckrunProvenanceResult(
            ok=False, reason_codes=["component_vrt_trusted_provenance_missing"]
        )
    elif any(value is None for value in provenance_inputs):
        component_vrt_provenance = ComponentVrtCheckrunProvenanceResult(
            ok=False, reason_codes=["component_vrt_trusted_provenance_partial"]
        )
    else:
        try:
            workflow_jobs = json.loads(Path(args.component_vrt_jobs_file).read_text(encoding="utf-8"))
            check_run = json.loads(Path(args.component_vrt_check_run_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            component_vrt_provenance = ComponentVrtCheckrunProvenanceResult(
                ok=False, reason_codes=["component_vrt_trusted_api_payload_invalid"]
            )
        else:
            component_vrt_provenance = verify_component_vrt_checkrun_provenance(
                check_run=check_run,
                workflow_jobs=workflow_jobs,
                jobs_complete=args.component_vrt_jobs_complete,
                expected_workflow_run_id=args.expected_workflow_run_id,
                expected_run_attempt=args.expected_workflow_run_attempt,
                expected_head_sha=args.expected_head_sha,
                expected_repository=args.expected_repository,
            )

    pr_body_raw: bytes | None = None
    if args.pr_body_file and Path(args.pr_body_file).exists():
        pr_body_raw = Path(args.pr_body_file).read_bytes()

    changed_path_entries: list[dict[str, str]] | None = None
    if args.changed_paths_typed_file and Path(args.changed_paths_typed_file).exists():
        raw_entries = json.loads(Path(args.changed_paths_typed_file).read_text(encoding="utf-8"))
        changed_path_entries = []
        for item in raw_entries:
            entry = {"status": item["status"], "path": item["path"]}
            if item.get("old_path"):
                entry["old_path"] = item["old_path"]
            changed_path_entries.append(entry)

    base_registry_doc: dict[str, Any] | None = None
    if args.trusted_base_registry_file and Path(args.trusted_base_registry_file).exists():
        try:
            text = Path(args.trusted_base_registry_file).read_text(encoding="utf-8")
            base_registry_doc = _yaml_safe_load_no_duplicate_keys(text, RegistryError) or {"surfaces": {}}
            validate_registry(base_registry_doc, args.schema)
        except (RegistryError, yaml.YAMLError) as exc:
            load_errors.append(f"trusted_base_registry_invalid:{exc}")
            base_registry_doc = None

    head_registry_doc: dict[str, Any] | None = None
    if args.trusted_head_registry_file and Path(args.trusted_head_registry_file).exists():
        try:
            text = Path(args.trusted_head_registry_file).read_text(encoding="utf-8")
            head_registry_doc = _yaml_safe_load_no_duplicate_keys(text, RegistryError) or {"surfaces": {}}
            validate_registry(head_registry_doc, args.schema)
        except (RegistryError, yaml.YAMLError) as exc:
            load_errors.append(f"trusted_head_registry_invalid:{exc}")
            head_registry_doc = None

    trusted_rederivation = TrustedRederivation(
        expected_base_sha=args.expected_base_sha,
        pr_body_raw=pr_body_raw,
        changed_path_entries=changed_path_entries,
        changed_paths_complete=not args.changed_paths_incomplete,
        expected_base_registry_blob_sha=args.expected_base_registry_blob_sha,
        expected_head_registry_blob_sha=args.expected_head_registry_blob_sha,
        base_registry_doc=base_registry_doc,
        head_registry_doc=head_registry_doc,
        # Issue #2099 AC1: optional -- only set when the caller has already
        # `git fetch --depth=1`'d this exact commit as a Git object (never a
        # working-tree checkout of it).
        candidate_head_ref=args.trusted_candidate_tree_ref,
        component_vrt_checkrun_provenance=component_vrt_provenance,
        # The #2100 CLI is the base-locked trusted-consumer authentication
        # path. No caller-controlled CLI switch may disable this requirement.
        require_component_vrt_checkrun_provenance=True,
    )
    return trusted_rederivation, load_errors


def _run_verify_trusted_artifact(args: argparse.Namespace) -> int:
    decision_raw: bytes | None = None
    if args.decision_file:
        decision_path = Path(args.decision_file)
        if decision_path.exists():
            decision_raw = decision_path.read_bytes()
    evidence_manifest_raw: bytes | None = None
    if args.evidence_manifest_file:
        manifest_path = Path(args.evidence_manifest_file)
        if manifest_path.exists():
            evidence_manifest_raw = manifest_path.read_bytes()

    trusted_rederivation, load_errors = _build_trusted_rederivation_from_args(args)

    verdict = verify_trusted_artifact(
        decision_raw=decision_raw,
        evidence_manifest_raw=evidence_manifest_raw,
        visual_impact_schema_path=args.visual_impact_schema,
        expected_head_sha=args.expected_head_sha or "",
        expected_repository=args.expected_repository or "",
        expected_pr_number=args.expected_pr_number or 0,
        trusted_rederivation=trusted_rederivation,
    )
    if load_errors:
        # A caller-claimed trusted-side input failed to load/validate --
        # never silently proceed as if it had simply not been supplied.
        verdict = TrustedArtifactVerdict(
            ok=False, reason_codes=[*verdict.reason_codes, *load_errors], decision=verdict.decision
        )
    output = verdict.to_dict()
    print(json.dumps(output, indent=2))
    if args.verdict_output:
        Path(args.verdict_output).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return 0 if verdict.ok else 1


def _run_policy_check(args: argparse.Namespace, changed_path_entries: list[dict[str, str]]) -> int:
    changed_paths = _flat_paths_for_resolve(changed_path_entries)
    result = resolve(
        changed_paths=changed_paths,
        registry_path=args.registry,
        schema_path=args.schema,
        mjs_path=args.mjs,
        repo_root=args.repo_root,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        node_bin=args.node_bin,
    )
    # PR #2045 OWNER fix_delta P0-2: reuse the single already-validated head
    # registry document `resolve()` produced instead of re-parsing the
    # working-tree file a second time (unvalidated, and potentially
    # divergent from whichever ref `--head-ref` pointed at).
    registry_doc: dict[str, Any] = result.head_doc if result.head_doc is not None else {}

    declaration_doc: dict[str, Any] | None = None
    declaration_error: str | None = None
    if result.affected_surfaces:
        if not args.pr_body_file:
            declaration_error = "no --pr-body-file supplied but surfaces are affected"
        else:
            pr_body = Path(args.pr_body_file).read_text(encoding="utf-8")
            try:
                declaration_doc = parse_declaration(pr_body, schema_path=args.visual_impact_schema)
            except DeclarationError as exc:
                declaration_error = str(exc)

    evidence_manifest: dict[str, Any] | None = None
    if args.evidence_manifest_file and Path(args.evidence_manifest_file).exists():
        evidence_manifest = json.loads(Path(args.evidence_manifest_file).read_text(encoding="utf-8"))

    authorized_owners: set[str] = set()
    codeowners_rules: list[tuple[str, list[str]]] | None = None
    if args.codeowners_file and Path(args.codeowners_file).exists():
        codeowners_rules = parse_codeowners(Path(args.codeowners_file).read_text(encoding="utf-8"))
        # Waiver authority is derived from ownership of the surface's VRT
        # CONTRACT (baseline + spec), not the production UI source files --
        # "who is authorized to accept a visual regression" is a review
        # ownership question, not a code ownership question. This flat union
        # is kept only as a legacy fallback; `evaluate_pr_policy` now scopes
        # per-surface via `codeowners_rules` (P1-3).
        contract_paths = sorted(
            {
                path
                for surface in registry_doc.get("surfaces", {}).values()
                for path in (surface.get("contracts", {}).get("baseline"), surface.get("contracts", {}).get("spec"))
                if path
            }
        )
        authorized_owners = authorized_owners_for_paths(codeowners_rules, contract_paths)

    def _tracking_issue_checker(tracking_issue: str) -> bool | None:
        return _check_tracking_issue_open(tracking_issue, args.repository)

    if declaration_error is not None:
        policy_result = {"ok": False, "surface_results": [], "failures": [declaration_error]}
    else:
        policy_result = evaluate_pr_policy(
            resolve_result=result,
            declaration_doc=declaration_doc,
            registry_doc=registry_doc,
            evidence_manifest=evidence_manifest,
            head_sha=args.head_sha or "",
            changed_paths=changed_paths,
            actor=args.actor or "",
            authorized_owners=authorized_owners,
            today=date.today(),
            tracking_issue_checker=_tracking_issue_checker,
            codeowners_rules=codeowners_rules,
            trusted_check_run_id=args.component_vrt_check_run_id,
            trusted_check_suite_id=args.component_vrt_check_suite_id,
            trusted_github_app_id=args.component_vrt_app_id,
            trusted_github_app_slug=args.component_vrt_app_slug,
            trusted_check_conclusion=args.component_vrt_check_conclusion,
        )

    decision = build_decision(
        repository=args.repository or "",
        pull_request_number=args.pr_number or 0,
        base_sha=args.base_sha or ("0" * 40),
        head_sha=args.head_sha or ("0" * 40),
        base_registry_blob_sha=args.base_registry_blob_sha or ("0" * 40),
        head_registry_blob_sha=args.head_registry_blob_sha or ("0" * 40),
        pr_body=(
            Path(args.pr_body_file).read_text(encoding="utf-8")
            if args.pr_body_file and Path(args.pr_body_file).exists()
            else ""
        ),
        changed_path_entries=changed_path_entries,
        affected_surfaces=[_build_decision_surface_entry(r, registry_doc) for r in policy_result["surface_results"]],
        component_vrt_report_check_run_id=args.component_vrt_check_run_id,
        github_actions_app_identity=args.github_actions_app_identity or "unknown",
        artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest,
        # Issue #2230 AC2: the producer's OWN `${{ github.run_id }}` /
        # `${{ github.run_attempt }}` context (`--run-id`/`--run-attempt`),
        # never re-derived by any other party.
        workflow_run_id=args.run_id,
        run_attempt=args.run_attempt,
    )

    schema_errors = _validate_decision_schema(decision, args.visual_impact_schema)
    ok = policy_result["ok"] and not schema_errors
    failures = list(policy_result["failures"]) + schema_errors

    output = {
        "schema": "VISUAL_IMPACT_POLICY_CHECK_RESULT_V1",
        "ok": ok,
        "failures": failures,
        "resolve_result": result.to_dict(),
        "decision": decision,
    }
    print(json.dumps(output, indent=2))
    # PR #2045 OWNER fix_delta P1-2: never write a schema-invalid decision
    # artifact -- upload-artifact's `if-no-files-found: error` (P0-1) then
    # correctly fails the job instead of silently shipping a broken one.
    if args.decision_output and not schema_errors:
        Path(args.decision_output).write_text(json.dumps(decision, indent=2), encoding="utf-8")
    return 0 if ok else 1


def _run_build_evidence_manifest(args: argparse.Namespace) -> int:
    """PR #2045 OWNER fix_delta P0-5 (V2 manifest): build one
    VISUAL_BASELINE_REVIEW_EVIDENCE_V2 record per registered surface from
    real per-surface inputs (`--surface-inputs-file`, produced by the CI job
    from actual VRT run outputs -- never fabricated). `baseline_sha256` is
    ALWAYS computed here from the on-disk baseline file (never trusted from
    the caller), matching this module's existing "compute, never trust a
    self-reported hash" pattern."""
    registry_doc = load_and_validate_registry(args.registry, args.schema, None, args.repo_root)
    surfaces = registry_doc.get("surfaces", {}) or {}
    inputs = json.loads(Path(args.surface_inputs_file).read_text(encoding="utf-8"))

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in inputs:
        surface_id = item.get("surface_id")
        surface_def = surfaces.get(surface_id)
        if surface_def is None:
            errors.append(f"unknown surface_id in --surface-inputs-file: {surface_id!r}")
            continue
        contracts = surface_def.get("contracts", {}) or {}
        baseline_path = contracts.get("baseline")
        baseline_sha256: str | None = None
        if baseline_path:
            baseline_abs = args.repo_root / baseline_path
            if baseline_abs.exists():
                baseline_sha256 = sha256_hex(baseline_abs.read_bytes())
            else:
                errors.append(f"{surface_id}: registered baseline path missing on disk: {baseline_path}")
        else:
            errors.append(f"{surface_id}: registry entry has no contracts.baseline")

        mismatched_raw = item.get("mismatched_pixels")
        mismatched_coerced = _coerce_mismatched_pixels(mismatched_raw)
        mismatched_pixels: Any = mismatched_coerced if mismatched_coerced is not None else mismatched_raw

        record = build_evidence_manifest_v2_record(
            surface_id=surface_id,
            contract_digest=compute_contract_digest(surface_def),
            head_sha=item.get("head_sha"),
            workflow_run_id=item.get("workflow_run_id"),
            # Issue #2230 AC2: the producer's OWN `${{ github.run_attempt }}`
            # context, never re-derived elsewhere -- paired with
            # `workflow_run_id` + `head_sha` this is the exact tuple
            # `verify_trusted_artifact()` cross-checks against the trusted
            # consumer's authenticated CheckRun provenance.
            run_attempt=item.get("run_attempt"),
            # CheckRun binding fields are always populated by the CONSUMER
            # (visual-impact-policy job's independently-fetched CheckRun API
            # lookup), never by this producer step -- see
            # build_evidence_from_manifest_v2()'s docstring.
            check_run_id=None,
            check_suite_id=None,
            github_app_id=None,
            github_app_slug=None,
            check_conclusion=None,
            baseline_path=baseline_path,
            baseline_sha256=baseline_sha256,
            actual_sha256=item.get("actual_sha256"),
            mismatched_pixels=mismatched_pixels,
            verify_command_id=contracts.get("verify_command_id"),
            verify_succeeded=bool(item.get("verify_succeeded")),
            update_command_id=contracts.get("update_command_id"),
            update_executed=bool(item.get("update_executed")),
            update_succeeded=bool(item.get("update_succeeded")),
            expected_artifact_id=item.get("expected_artifact_id"),
            actual_artifact_id=item.get("actual_artifact_id"),
            diff_artifact_id=item.get("diff_artifact_id"),
        )
        records.append(record)

    manifest = {"schema": EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": records}
    print(json.dumps(manifest, indent=2))
    if args.manifest_output:
        Path(args.manifest_output).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0 if not errors else 1


def _run_changed_paths(args: argparse.Namespace) -> int:
    raw = Path(args.name_status_file).read_bytes() if args.name_status_file else b""
    entries, flat = parse_git_diff_name_status_z(raw)
    if args.typed_output:
        Path(args.typed_output).write_text(json.dumps(entries), encoding="utf-8")
    if args.flat_output:
        Path(args.flat_output).write_text("\n".join(flat), encoding="utf-8")
    print(json.dumps(entries, indent=2))
    return 0


def _run_resolve_trusted_registry_blob(args: argparse.Namespace) -> int:
    """Issue #2099 AC7: `--mode resolve-trusted-registry-blob` -- read
    `--path` at `--ref` via `read_git_blob_at_ref()`'s three-way
    `GitReadOutcome` classification (never collapsing "file missing" and
    "git plumbing failure" into one branch, unlike the workflow's former
    inline `git show ... 2>/dev/null || rm -f ...`)."""
    if not args.ref or not args.path or not args.output_file:
        print("resolve-trusted-registry-blob requires --ref, --path, --output-file", file=sys.stderr)
        return 1
    outcome, content, message = read_git_blob_at_ref(REPO_ROOT, args.ref, args.path)
    if outcome == GitReadOutcome.PLUMBING_ERROR:
        print(f"git_plumbing_error: resolving {args.path} at {args.ref}: {message}", file=sys.stderr)
        return 1
    if outcome == GitReadOutcome.MISSING:
        # Normal case: the registry does not exist at this ref. Remove any
        # stale output from a previous invocation and succeed.
        Path(args.output_file).unlink(missing_ok=True)
        if args.blob_sha_output_file:
            Path(args.blob_sha_output_file).unlink(missing_ok=True)
        return 0
    assert content is not None
    Path(args.output_file).write_bytes(content)
    if args.blob_sha_output_file:
        rev_parse = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", f"{args.ref}:{args.path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if rev_parse.returncode != 0:
            print(f"git_plumbing_error: rev-parse {args.ref}:{args.path}: {rev_parse.stderr}", file=sys.stderr)
            return 1
        Path(args.blob_sha_output_file).write_text(rev_parse.stdout.strip() + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve VISUAL_IMPACT affected surfaces for a changed-path set.")
    parser.add_argument(
        "--mode",
        choices=[
            "resolve",
            "policy-check",
            "build-evidence-manifest",
            "changed-paths",
            "verify-trusted-artifact",
            "resolve-trusted-registry-blob",
            "acquire-component-vrt-checkrun",
            "acquire-trusted-artifact",
        ],
        default="resolve",
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--visual-impact-schema", type=Path, default=DEFAULT_VISUAL_IMPACT_SCHEMA_PATH)
    parser.add_argument("--mjs", type=Path, default=DEFAULT_MJS_PATH)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--changed-paths-file", type=str, default=None)
    parser.add_argument(
        "--changed-paths-typed-file",
        type=str,
        default=None,
        help=(
            "JSON array of {status,path[,old_path]} records (git diff "
            "--name-status -z --find-renames output, parsed). Preferred "
            "over --changed-paths-file for --mode policy-check: preserves "
            "rename/add/remove status for the decision digest (PR #2045 "
            "OWNER fix_delta P0-4)."
        ),
    )
    parser.add_argument("--base-ref", type=str, default=None)
    parser.add_argument("--head-ref", type=str, default=None)
    parser.add_argument("--expected-workflow-run-id", type=int, default=None)
    parser.add_argument("--expected-workflow-run-attempt", type=int, default=None)
    parser.add_argument("--component-vrt-jobs-file", type=str, default=None)
    parser.add_argument("--component-vrt-check-run-file", type=str, default=None)
    parser.add_argument("--component-vrt-jobs-complete", action="store_true", default=None)
    parser.add_argument("--node-bin", type=str, default="node")
    parser.add_argument("--pr-body-file", type=str, default=None)
    parser.add_argument("--evidence-manifest-file", type=str, default=None)
    parser.add_argument("--codeowners-file", type=str, default=None)
    parser.add_argument("--actor", type=str, default=None)
    parser.add_argument("--repository", type=str, default=None)
    parser.add_argument("--pr-number", type=int, default=None)
    parser.add_argument("--base-sha", type=str, default=None)
    parser.add_argument("--head-sha", type=str, default=None)
    parser.add_argument("--base-registry-blob-sha", type=str, default=None)
    parser.add_argument("--head-registry-blob-sha", type=str, default=None)
    parser.add_argument("--component-vrt-check-run-id", type=str, default=None)
    # PR #2045 OWNER fix_delta P0-5 (V2 manifest): trusted CheckRun binding
    # fields -- ALWAYS sourced from the caller's own independently-fetched
    # CheckRun API lookup (never from the evidence manifest's own
    # self-reported fields, which the producer job cannot even know yet).
    parser.add_argument("--component-vrt-check-suite-id", type=str, default=None)
    parser.add_argument("--component-vrt-app-id", type=str, default=None)
    parser.add_argument("--component-vrt-app-slug", type=str, default=None)
    parser.add_argument("--component-vrt-check-conclusion", type=str, default=None)
    parser.add_argument("--github-actions-app-identity", type=str, default=None)
    parser.add_argument("--artifact-id", type=str, default=None)
    parser.add_argument("--artifact-digest", type=str, default=None)
    parser.add_argument("--decision-output", type=str, default=None)
    # Issue #2019 AC30-AC37: `--mode verify-trusted-artifact` CLI surface,
    # invoked by the `workflow_run`-triggered trusted consumer workflow.
    parser.add_argument("--decision-file", type=str, default=None)
    parser.add_argument("--expected-head-sha", type=str, default=None)
    parser.add_argument("--expected-repository", type=str, default=None)
    parser.add_argument("--expected-pr-number", type=int, default=None)
    parser.add_argument("--verdict-output", type=str, default=None)
    # Issue #2091 AC1: trusted-side re-derivation inputs for
    # `--mode verify-trusted-artifact` -- every one of these MUST be
    # independently fetched/computed by the caller workflow (never copied
    # from the producer artifact). All are optional; omitting one skips
    # only that specific cross check (never an automatic pass).
    parser.add_argument("--expected-base-sha", type=str, default=None)
    parser.add_argument("--expected-base-registry-blob-sha", type=str, default=None)
    parser.add_argument("--expected-head-registry-blob-sha", type=str, default=None)
    parser.add_argument("--trusted-base-registry-file", type=str, default=None)
    parser.add_argument("--trusted-head-registry-file", type=str, default=None)
    parser.add_argument(
        "--changed-paths-incomplete",
        action="store_true",
        default=False,
        help=(
            "Set when the caller's --changed-paths-typed-file is KNOWN to be "
            "an incomplete changed-path set (e.g. a paginated API page-size "
            "cap was hit). Forces the verdict fail-closed."
        ),
    )
    # Issue #2099 AC1: the candidate PR head commit SHA, as a Git object the
    # caller has ALREADY fetched (`git fetch --depth=1 origin <sha>`) --
    # never a working-tree checkout. Enables `resolve_trusted_minimum()`'s
    # additional git-object-backed transitive import-graph step.
    parser.add_argument("--trusted-candidate-tree-ref", type=str, default=None)
    # Issue #2099 AC7: `--mode resolve-trusted-registry-blob` reads
    # `--path` as it exists at `--ref` via `read_git_blob_at_ref()`,
    # replacing the workflow's former inline
    # `git show ... 2>/dev/null || rm -f ...` pattern (which could not
    # distinguish "path missing" from "git plumbing failure").
    parser.add_argument("--ref", type=str, default=None)
    parser.add_argument("--path", type=str, default=None)
    parser.add_argument("--output-file", type=str, default=None)
    parser.add_argument("--blob-sha-output-file", type=str, default=None)
    # Issue #2100 PR #2229 review fix_delta P1-3: `--mode
    # acquire-component-vrt-checkrun` CLI surface.
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--run-attempt", type=int, default=None)
    parser.add_argument("--jobs-output-file", type=str, default=None)
    parser.add_argument("--check-run-output-file", type=str, default=None)
    # Issue #2230: `--mode acquire-trusted-artifact` CLI surface.
    parser.add_argument("--expected-artifact-name", type=str, default=None)
    parser.add_argument("--artifact-id-output-file", type=str, default=None)
    # PR #2045 OWNER fix_delta P0-5 (V2 manifest): `build-evidence-manifest`
    # mode is invoked by the `component-vrt-report` CI job to produce the
    # VISUAL_BASELINE_REVIEW_EVIDENCE_V2 artifact from real per-surface VRT
    # run inputs (never fabricated in raw shell/bash-embedded Python).
    parser.add_argument("--surface-inputs-file", type=str, default=None)
    parser.add_argument("--manifest-output", type=str, default=None)
    # PR #2045 OWNER fix_delta P1-4: `changed-paths` mode -- parses real
    # `git diff --name-status -z --find-renames` output (never re-implemented
    # inline in ci.yml).
    parser.add_argument("--name-status-file", type=str, default=None)
    parser.add_argument("--typed-output", type=str, default=None)
    parser.add_argument("--flat-output", type=str, default=None)
    args = parser.parse_args(argv)

    if args.mode == "policy-check":
        return _run_policy_check(args, _read_changed_path_entries(args))

    if args.mode == "build-evidence-manifest":
        return _run_build_evidence_manifest(args)

    if args.mode == "changed-paths":
        return _run_changed_paths(args)

    if args.mode == "verify-trusted-artifact":
        return _run_verify_trusted_artifact(args)

    if args.mode == "acquire-trusted-artifact":
        return _run_acquire_trusted_artifact(args)

    if args.mode == "resolve-trusted-registry-blob":
        return _run_resolve_trusted_registry_blob(args)

    if args.mode == "acquire-component-vrt-checkrun":
        return _run_acquire_component_vrt_checkrun(args)

    changed_paths = _read_changed_paths(args)

    result = resolve(
        changed_paths=changed_paths,
        registry_path=args.registry,
        schema_path=args.schema,
        mjs_path=args.mjs,
        repo_root=args.repo_root,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        node_bin=args.node_bin,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
