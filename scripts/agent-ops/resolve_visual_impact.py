#!/usr/bin/env python3
"""resolve_visual_impact.py (Issue #2019)

Orchestration + policy layer:

1. Registry (docs/dev/visual-surfaces.yml) load/validate/diff and
   TypeScript-compiler-API-backed affected-surface resolution (delegates the
   actual static import-graph walk to resolve_visual_impact.mjs -- this
   module never re-implements TypeScript/Vite import semantics with regex).
2. VISUAL_IMPACT_DECLARATION_V1 parsing (untrusted PR body input).
3. VISUAL_IMPACT_DECISION_V1 generation (trusted CI observation) and
   disposition evaluation (verified_unchanged / baseline_changed / waived),
   including trusted-base-branch CODEOWNERS-derived waiver authority.

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
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

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


def run_mjs(mjs_path: Path, request: dict[str, Any], node_bin: str = "node") -> dict[str, Any]:
    """PR #2045 OWNER fix_delta P0-2: a non-zero mjs exit code must always be
    surfaced as a resolver error, even when stdout happens to parse as valid
    JSON (the mjs script sets `process.exitCode = 1` on per-surface
    exceptions/invalid input while still emitting a best-effort JSON body).
    Silently trusting the JSON body regardless of exit code let a crashed
    resolver invocation degrade to an empty/partial `surfaces` map that was
    then treated as "fully resolved, no impact"."""
    proc = subprocess.run(
        [node_bin, str(mjs_path)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
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
) -> dict[str, Any]:
    """Build VISUAL_IMPACT_DECISION_V1 from TRUSTED observation inputs only.
    Never copies a declaration's self-reported disposition verbatim -- the
    caller supplies `affected_surfaces` (each entry already carrying an
    independently-evaluated `disposition` + `evidence`), never the raw
    declaration dict itself (Issue #2019 AC11/AC12)."""
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


def resolve_trusted_minimum(
    changed_paths: list[str],
    base_doc: dict[str, Any],
    head_doc: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """Issue #2091 AC1/AC2: a COARSE, independently-computable subset of
    `resolve()`'s affected-surface logic that the trusted-consumer workflow
    can run WITHOUT executing any candidate-PR-head code (no TS-compiler
    transitive import-graph walk via `resolve_visual_impact.mjs`/Node --
    that step alone would require reading/parsing PR-head source files,
    which this trust boundary intentionally never does).

    Reuses steps 0/1/2/3/5 of `resolve()` verbatim (meta policy paths /
    global invalidators / direct producer+contract paths / registry-union
    mapping deletion / coverage-root boundary) against TRUSTED-side
    (base_sha/head_sha) registries and a TRUSTED-side changed-path set.
    Deliberately omits step 4 (mjs transitive reachability) -- a path only
    reachable via transitive import (never a direct producer/contract path,
    never under any `coverage_roots` pattern) is NOT caught by this coarse
    check; this is a scoped, documented limitation (see Issue #2091 Notes
    for Reviewer / PR follow-up), not a silent gap.

    Returns `(affected_surface_ids, unmapped_visual_candidates)` -- same
    shapes as the corresponding parts of `ResolveResult`."""
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

    unmapped_visual_candidates: list[str] = []
    for changed_path in changed_paths:
        if changed_path in all_producer_paths:
            continue
        if match_coverage_roots(changed_path, coverage_roots):
            unmapped_visual_candidates.append(changed_path)

    return affected_surface_ids, unmapped_visual_candidates


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
                trusted_affected, trusted_unmapped = resolve_trusted_minimum(
                    trusted_changed_paths, tr.base_registry_doc, tr.head_registry_doc
                )
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
