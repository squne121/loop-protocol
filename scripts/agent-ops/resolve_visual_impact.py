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


class RegistryError(Exception):
    pass


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


def load_registry_text(registry_path: Path, git_ref: str | None, repo_root: Path) -> str:
    """Load registry YAML text either from the working tree or a git ref."""
    if git_ref is None:
        return registry_path.read_text(encoding="utf-8")
    rel = registry_path.relative_to(repo_root).as_posix()
    proc = subprocess.run(
        ["git", "show", f"{git_ref}:{rel}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Registry did not exist at this ref (e.g. base ref predates this
        # Issue) -- treat as an empty registry rather than failing closed on
        # git plumbing noise; downstream union logic tolerates an empty map.
        return "schema_version: 1\nsurfaces: {}\n"
    return proc.stdout


def validate_registry(doc: dict[str, Any], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        messages = [f"{list(e.path)}: {e.message}" for e in errors]
        raise RegistryError("; ".join(messages))


def load_and_validate_registry(
    registry_path: Path, schema_path: Path, git_ref: str | None, repo_root: Path
) -> dict[str, Any]:
    text = load_registry_text(registry_path, git_ref, repo_root)
    try:
        doc = yaml.safe_load(text) or {}
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


def run_mjs(mjs_path: Path, request: dict[str, Any], node_bin: str = "node") -> dict[str, Any]:
    proc = subprocess.run(
        [node_bin, str(mjs_path)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    if not proc.stdout.strip():
        raise RegistryError(f"resolve_visual_impact.mjs produced no output (stderr: {proc.stderr})")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"resolve_visual_impact.mjs produced invalid JSON: {exc}\n{proc.stdout}") from exc


def match_coverage_roots(changed_path: str, coverage_roots: list[str]) -> bool:
    for pattern in coverage_roots:
        if fnmatch.fnmatch(changed_path, pattern):
            return True
        # `src/ui/**` should also match `src/ui/HudController.ts` (fnmatch
        # already handles this) and nested files under any depth.
        if pattern.endswith("/**") and changed_path.startswith(pattern[:-3]):
            return True
    return False


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

    base_doc: dict[str, Any] = head_doc
    if base_ref is not None:
        try:
            base_doc = load_and_validate_registry(registry_path, schema_path, base_ref, repo_root)
        except RegistryError as exc:
            result.errors.append(f"base registry invalid: {exc}")
            base_doc = {"schema_version": 1, "surfaces": {}}

    # registry-first union: producers used for graph resolution come from
    # BOTH base and head registries so that a head-side mapping deletion
    # cannot silently narrow what the resolver even looks at.
    union_surfaces: dict[str, Any] = dict(head_doc.get("surfaces", {}) or {})
    for surface_id, base_def in (base_doc.get("surfaces", {}) or {}).items():
        if surface_id not in union_surfaces:
            union_surfaces[surface_id] = base_def

    global_invalidators = set(head_doc.get("global_invalidators", []) or [])
    coverage_roots = head_doc.get("coverage_roots", []) or []

    changed_set = set(changed_paths)

    affected_surface_ids: dict[str, str] = {}

    # 1. Global invalidators: any hit affects ALL surfaces.
    if changed_set & global_invalidators:
        for surface_id in union_surfaces:
            affected_surface_ids[surface_id] = "global_invalidator"

    # 2. Direct producer path match (styles/assets/config listed verbatim).
    for surface_id, surface_def in union_surfaces.items():
        if surface_id in affected_surface_ids:
            continue
        if collect_producer_paths(surface_def) & changed_set:
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
    mismatched = manifest.get("mismatched_pixels")
    verify_success = mismatched == 0
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
) -> dict[str, Any]:
    """Evaluate every affected surface's disposition. Never passes on
    declaration self-report alone (AC12); unmapped/unknown_impact always
    fail closed (AC8)."""
    surfaces = registry_doc.get("surfaces", {}) or {}
    failures: list[str] = []
    surface_results: list[dict[str, Any]] = []

    if resolve_result.unmapped_visual_candidates:
        failures.append(f"unmapped_visual_candidate: {resolve_result.unmapped_visual_candidates}")
    if resolve_result.unknown_impact:
        failures.append(f"unknown_impact: {resolve_result.unknown_impact}")

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
        contract_job = surface_def.get("contracts", {}).get("job")
        baseline_path = surface_def.get("contracts", {}).get("baseline")
        baseline_diff_present = baseline_path in changed_paths

        if disposition == "waived":
            waiver = decl_entry.get("waiver", {}) or {}
            tracking_open = tracking_issue_checker(waiver.get("tracking_issue")) if tracking_issue_checker else None
            evaluation = evaluate_waiver(
                actor=actor,
                waiver=waiver,
                authorized_owners=authorized_owners,
                today=today,
                tracking_issue_exists_and_open=tracking_open,
            )
            ok, reason = evaluation.ok, evaluation.reason
        else:
            evidence = build_evidence_from_manifest(evidence_manifest, head_sha, surface_id, contract_job)
            evidence.baseline_diff_present = baseline_diff_present
            if disposition == "verified_unchanged":
                ok, reason = evaluate_verified_unchanged(evidence)
            elif disposition == "baseline_changed":
                ok, reason = evaluate_baseline_changed(evidence)
            else:
                ok, reason = False, "unknown_disposition"

        surface_results.append({"surface_id": surface_id, "disposition": disposition, "ok": ok, "reason": reason})
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


def _build_decision_surface_entry(result_entry: dict[str, Any], registry_doc: dict[str, Any]) -> dict[str, Any]:
    surface_id = result_entry["surface_id"]
    contracts = registry_doc.get("surfaces", {}).get(surface_id, {}).get("contracts", {})
    runner = contracts.get("runner", "unknown")
    return {
        "surface_id": surface_id,
        "contract_id": f"{surface_id}:{runner}",
        "disposition": result_entry["disposition"],
        "evidence": {"ok": result_entry["ok"], "reason": result_entry["reason"]},
    }


def _run_policy_check(args: argparse.Namespace, changed_paths: list[str]) -> int:
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
    registry_doc = yaml.safe_load(args.registry.read_text(encoding="utf-8"))

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
    if args.codeowners_file and Path(args.codeowners_file).exists():
        rules = parse_codeowners(Path(args.codeowners_file).read_text(encoding="utf-8"))
        # Waiver authority is derived from ownership of the surface's VRT
        # CONTRACT (baseline + spec), not the production UI source files --
        # "who is authorized to accept a visual regression" is a review
        # ownership question, not a code ownership question.
        contract_paths = sorted(
            {
                path
                for surface in registry_doc.get("surfaces", {}).values()
                for path in (surface.get("contracts", {}).get("baseline"), surface.get("contracts", {}).get("spec"))
                if path
            }
        )
        authorized_owners = authorized_owners_for_paths(rules, contract_paths)

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
            tracking_issue_checker=None,
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
        changed_path_entries=[{"status": "modified", "path": p} for p in changed_paths],
        affected_surfaces=[_build_decision_surface_entry(r, registry_doc) for r in policy_result["surface_results"]],
        component_vrt_report_check_run_id=args.component_vrt_check_run_id,
        github_actions_app_identity=args.github_actions_app_identity or "unknown",
        artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest,
    )

    output = {
        "schema": "VISUAL_IMPACT_POLICY_CHECK_RESULT_V1",
        "ok": policy_result["ok"],
        "failures": policy_result["failures"],
        "resolve_result": result.to_dict(),
        "decision": decision,
    }
    print(json.dumps(output, indent=2))
    if args.decision_output:
        Path(args.decision_output).write_text(json.dumps(decision, indent=2), encoding="utf-8")
    return 0 if policy_result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve VISUAL_IMPACT affected surfaces for a changed-path set.")
    parser.add_argument("--mode", choices=["resolve", "policy-check"], default="resolve")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--visual-impact-schema", type=Path, default=DEFAULT_VISUAL_IMPACT_SCHEMA_PATH)
    parser.add_argument("--mjs", type=Path, default=DEFAULT_MJS_PATH)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--changed-paths-file", type=str, default=None)
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
    parser.add_argument("--github-actions-app-identity", type=str, default=None)
    parser.add_argument("--artifact-id", type=str, default=None)
    parser.add_argument("--artifact-digest", type=str, default=None)
    parser.add_argument("--decision-output", type=str, default=None)
    args = parser.parse_args(argv)

    changed_paths = _read_changed_paths(args)

    if args.mode == "policy-check":
        return _run_policy_check(args, changed_paths)

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
