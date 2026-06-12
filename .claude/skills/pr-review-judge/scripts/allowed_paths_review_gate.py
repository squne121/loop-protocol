#!/usr/bin/env python3
"""
Allowed Paths Review Gate (ALLOWED_PATHS_GATE_RESULT_V1)

Deterministically recalculates PR's actual changed files (from git diff)
against linked issue's Allowed Paths snapshot. Producer role: review_subagent.

Key principles:
- Worker transcript / report is NOT an input
- changed_files_source is git_diff_base_head (triple-dot merge-base..head)
- head_sha != reviewed_head_sha -> indeterminate (merge-blocking)
- contract fingerprint and execution context are separated for freshness detection
- review mode requires explicit snapshot bindings and expected fingerprint
- Allowed Paths matcher uses repo-relative POSIX normalization with fail-closed invalid input handling
"""

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class GateStatus(Enum):
    """Status values for allowed paths gate."""

    OK = "ok"
    FAIL_CLOSED = "fail_closed"
    STALE_SNAPSHOT = "stale_snapshot"
    INDETERMINATE = "indeterminate"


@dataclass
class ExecutionContext:
    """Execution context (audit log only, NOT used for freshness judgment)."""

    worktree_root: str
    generated_at: str
    tool_version: str = "1.2.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContractFingerprint:
    """Contract fingerprint for freshness judgment."""

    issue_number: int
    contract_source_kind: str
    contract_source_id: str
    contract_body_sha256: str
    allowed_paths_normalized_sha256: str
    base_ref: str
    base_sha_at_snapshot: str

    def to_normalized_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass
class AllowedPathsGateResult:
    """ALLOWED_PATHS_GATE_RESULT_V1 schema."""

    produced_at: str
    status: str = ""
    produced_by: str = "allowed_paths_review_gate.py"
    producer_role: str = "review_subagent"
    worker_report_used_as_canonical: bool = False
    pr_number: int = 0
    base_ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    reviewed_head_sha: str = ""
    changed_files_source: str = "git_diff_base_head"
    allowed_paths_source: str = ""
    changed_files_count: int = 0
    changed_files: List[str] = field(default_factory=list)
    allowed_paths_list: List[str] = field(default_factory=list)
    violations: List[Dict[str, Any]] = field(default_factory=list)
    contract_fingerprint: Dict[str, Any] = field(default_factory=dict)
    execution_context: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "produced_at": self.produced_at,
            "produced_by": self.produced_by,
            "producer_role": self.producer_role,
            "worker_report_used_as_canonical": self.worker_report_used_as_canonical,
            "pr_number": self.pr_number,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "reviewed_head_sha": self.reviewed_head_sha,
            "changed_files_source": self.changed_files_source,
            "allowed_paths_source": self.allowed_paths_source,
            "changed_files_count": self.changed_files_count,
            "changed_files": self.changed_files,
            "allowed_paths_list": self.allowed_paths_list,
            "violations": self.violations,
            "contract_fingerprint": self.contract_fingerprint,
            "execution_context": self.execution_context,
            **({} if self.reason is None else {"reason": self.reason}),
            **({} if not self.errors else {"errors": self.errors}),
        }


class AllowedPathsMatcher:
    """Matches repo-relative POSIX paths against the restricted Allowed Paths grammar."""

    @staticmethod
    def normalize_path(path: str) -> Optional[str]:
        """Normalize repo-relative paths and reject invalid input."""
        if not path:
            return None
        if "\\" in path:
            return None
        if path.startswith("/"):
            return None

        normalized = path[2:] if path.startswith("./") else path
        if normalized in {"", "."}:
            return None
        if ".." in normalized.split("/"):
            return None
        return normalized

    @staticmethod
    def normalize_allowed_pattern(pattern: str) -> Optional[str]:
        # Trailing-slash patterns like "src/ui/" are treated as directory prefixes
        # and normalized to "src/ui/**". Wildcard + trailing-slash is invalid.
        if pattern.endswith("/"):
            # Reject wildcard + trailing-slash (e.g. "src/*/")
            bare = pattern.rstrip("/")
            if "*" in bare:
                return None
            normalized_bare = AllowedPathsMatcher.normalize_path(bare)
            if normalized_bare is None:
                return None
            return normalized_bare + "/**"
        normalized = AllowedPathsMatcher.normalize_path(pattern)
        if normalized is None:
            return None
        if "**" in normalized and not normalized.endswith("/**"):
            return None
        return normalized

    @staticmethod
    def matches_pattern(file_path: str, pattern: str) -> bool:
        if pattern == file_path:
            return True

        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            return file_path == prefix or file_path.startswith(prefix + "/")

        if "*" not in pattern:
            return False

        file_parts = file_path.split("/")
        pattern_parts = pattern.split("/")
        if len(file_parts) != len(pattern_parts):
            return False

        for file_part, pattern_part in zip(file_parts, pattern_parts):
            if pattern_part == "*":
                continue
            if "*" in pattern_part:
                return False
            if file_part != pattern_part:
                return False
        return True

    @staticmethod
    def is_file_allowed(file_path: str, allowed_paths: List[str]) -> bool:
        normalized_file = AllowedPathsMatcher.normalize_path(file_path)
        if normalized_file is None:
            return False

        for pattern in allowed_paths:
            normalized_pattern = AllowedPathsMatcher.normalize_allowed_pattern(pattern)
            if normalized_pattern is None:
                continue
            if AllowedPathsMatcher.matches_pattern(normalized_file, normalized_pattern):
                return True
        return False


class AllowedPathsGateEvaluator:
    """Main evaluator for PR allowed paths gate."""

    def __init__(
        self,
        *,
        pr_number: int,
        base_ref: str,
        base_sha: str,
        head_sha: str,
        reviewed_head_sha: str,
        allowed_paths: List[str],
        contract_body_sha256: str,
        contract_source_kind: str,
        contract_source_id: str,
        expected_contract_fingerprint: Optional[Dict[str, Any]],
        issue_number: int = 0,
    ):
        self.pr_number = pr_number
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.reviewed_head_sha = reviewed_head_sha
        self.allowed_paths = allowed_paths
        self.contract_body_sha256 = contract_body_sha256
        self.contract_source_kind = contract_source_kind
        self.contract_source_id = contract_source_id
        self.expected_contract_fingerprint = expected_contract_fingerprint
        self.issue_number = issue_number

    def canonicalize_allowed_paths(self) -> List[str]:
        canonicalized: List[str] = []
        for pattern in self.allowed_paths:
            normalized = AllowedPathsMatcher.normalize_allowed_pattern(pattern)
            if normalized is None:
                raise ValueError(f"invalid allowed path pattern: {pattern}")
            canonicalized.append(normalized)
        return sorted(set(canonicalized))

    def compute_allowed_paths_hash(self) -> str:
        normalized_json = json.dumps(
            self.canonicalize_allowed_paths(),
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(normalized_json.encode()).hexdigest()

    def compute_contract_fingerprint(self) -> Dict[str, Any]:
        fingerprint = ContractFingerprint(
            issue_number=self.issue_number,
            contract_source_kind=self.contract_source_kind,
            contract_source_id=self.contract_source_id,
            contract_body_sha256=self.contract_body_sha256,
            allowed_paths_normalized_sha256=self.compute_allowed_paths_hash(),
            base_ref=self.base_ref,
            base_sha_at_snapshot=self.base_sha,
        )
        return json.loads(fingerprint.to_normalized_json())

    def get_changed_files_from_git(self) -> List[str]:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{self.base_sha}...{self.head_sha}"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git diff failed: {exc.stderr}") from exc
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def evaluate(self) -> AllowedPathsGateResult:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        result = AllowedPathsGateResult(
            produced_at=now,
            pr_number=self.pr_number,
            base_ref=self.base_ref,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            reviewed_head_sha=self.reviewed_head_sha,
        )

        if self.head_sha != self.reviewed_head_sha:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = (
                f"head_sha mismatch: {self.head_sha} != {self.reviewed_head_sha} (merge-blocking)"
            )
            result.errors.append(result.reason)
            return result

        if not self.allowed_paths:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = "Allowed Paths snapshot is missing or empty (merge-blocking)"
            result.errors.append(result.reason)
            return result

        if not self.contract_source_kind or not self.contract_source_id:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = "contract_source_kind/source_id missing (merge-blocking)"
            result.errors.append(result.reason)
            return result

        if self.expected_contract_fingerprint is None:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = "expected_contract_fingerprint_missing (merge-blocking)"
            result.errors.append(result.reason)
            return result

        try:
            result.allowed_paths_list = self.canonicalize_allowed_paths()
            result.contract_fingerprint = self.compute_contract_fingerprint()
        except Exception as exc:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"Failed to compute contract fingerprint: {exc}"
            result.errors.append(result.reason)
            return result

        if self.expected_contract_fingerprint != result.contract_fingerprint:
            result.status = GateStatus.STALE_SNAPSHOT.value
            result.reason = "contract fingerprint diverged from snapshot (stale_snapshot, merge-blocking)"
            result.errors.append(result.reason)
            return result

        try:
            changed_files = self.get_changed_files_from_git()
        except Exception as exc:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"Failed to get changed files: {exc}"
            result.errors.append(result.reason)
            return result

        result.changed_files = changed_files
        result.changed_files_count = len(changed_files)
        result.allowed_paths_source = "linked_issue_contract_snapshot"

        try:
            result.execution_context = ExecutionContext(
                worktree_root=str(Path.cwd()),
                generated_at=now,
            ).to_dict()
        except Exception as exc:
            result.execution_context = {
                "worktree_root": "unknown",
                "generated_at": now,
                "error": str(exc),
            }

        violations = []
        for file_path in changed_files:
            if not AllowedPathsMatcher.is_file_allowed(file_path, result.allowed_paths_list):
                violations.append({"file": file_path, "reason": "not in allowed_paths"})

        result.violations = violations
        if violations:
            result.status = GateStatus.FAIL_CLOSED.value
            result.reason = f"{len(violations)} file(s) outside allowed paths"
        else:
            result.status = GateStatus.OK.value
            result.reason = "All changed files are within allowed paths"
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate PR allowed paths gate deterministically")
    parser.add_argument("--pr-number", type=int, required=True, help="PR number")
    parser.add_argument("--base-ref", required=True, help="Base branch name (e.g., main)")
    parser.add_argument("--base-sha", required=True, help="Base SHA (at snapshot time)")
    parser.add_argument("--head-sha", required=True, help="Current head SHA")
    parser.add_argument("--reviewed-head-sha", required=True, help="Head SHA at review time")
    parser.add_argument(
        "--allowed-paths",
        type=json.loads,
        required=True,
        help="JSON array of allowed paths from issue contract",
    )
    parser.add_argument("--contract-body-sha256", required=True, help="SHA256 of issue contract body")
    parser.add_argument("--contract-source-kind", required=True, help="Contract source kind")
    parser.add_argument("--contract-source-id", required=True, help="Contract source identifier")
    parser.add_argument("--expected-contract-fingerprint", type=json.loads, required=True, help="JSON object captured at snapshot time")
    parser.add_argument("--issue-number", type=int, default=0, help="Linked issue number")
    parser.add_argument("--format", choices=["json", "yaml"], default="json", help="Output format")
    args = parser.parse_args()

    evaluator = AllowedPathsGateEvaluator(
        pr_number=args.pr_number,
        base_ref=args.base_ref,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
        reviewed_head_sha=args.reviewed_head_sha,
        allowed_paths=args.allowed_paths,
        contract_body_sha256=args.contract_body_sha256,
        contract_source_kind=args.contract_source_kind,
        contract_source_id=args.contract_source_id,
        expected_contract_fingerprint=args.expected_contract_fingerprint,
        issue_number=args.issue_number,
    )

    result = evaluator.evaluate()
    result_dict = result.to_dict()

    if args.format == "yaml":
        try:
            import yaml
            print(yaml.dump(result_dict, default_flow_style=False, sort_keys=False))
        except ImportError:
            print("PyYAML not available, using JSON format instead", file=sys.stderr)
            print(json.dumps(result_dict, indent=2))
    else:
        print(json.dumps(result_dict, indent=2))

    sys.exit(0 if result.status == GateStatus.OK.value else 1)


if __name__ == "__main__":
    main()
