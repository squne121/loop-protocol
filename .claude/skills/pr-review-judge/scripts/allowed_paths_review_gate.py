#!/usr/bin/env python3
"""
Allowed Paths Review Gate (ALLOWED_PATHS_GATE_RESULT_V1)

Deterministically recalculates PR's actual changed files (from git diff or GitHub API)
against linked issue's Allowed Paths snapshot. Producer role: review_subagent.

Key principles:
- Worker transcript / report is NOT an input
- changed_files_source is git_diff_base_head (triple-dot merge-base..head)
- head_sha != reviewed_head_sha → indeterminate (merge-blocking)
- contract fingerprint and execution context are separated for freshness detection
- Allowed Paths matcher uses POSIX path normalization with specific rules
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import hashlib


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
    tool_version: str = "1.0.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContractFingerprint:
    """Contract fingerprint for freshness judgment."""
    issue_number: int
    contract_source_kind: str  # e.g., "github_issue"
    contract_source_id: str  # e.g., "758"
    contract_body_sha256: str
    allowed_paths_normalized_sha256: str
    base_ref: str  # e.g., "main"
    base_sha_at_snapshot: str

    def to_normalized_json(self) -> str:
        """Return normalized JSON string for hashing."""
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


@dataclass
class AllowedPathsGateResult:
    """ALLOWED_PATHS_GATE_RESULT_V1 schema."""
    produced_at: str
    status: str = ""  # ok | fail_closed | stale_snapshot | indeterminate
    produced_by: str = "allowed_paths_review_gate.py"
    producer_role: str = "review_subagent"
    worker_report_used_as_canonical: bool = False

    # Input binding
    pr_number: int = None
    base_ref: str = None
    base_sha: str = None
    head_sha: str = None
    reviewed_head_sha: str = None
    changed_files_source: str = "git_diff_base_head"

    # Result details
    allowed_paths_source: str = None
    changed_files_count: int = 0
    changed_files: List[str] = field(default_factory=list)
    allowed_paths_list: List[str] = field(default_factory=list)
    violations: List[Dict[str, Any]] = field(default_factory=list)

    # Snapshot freshness
    contract_fingerprint: Dict[str, Any] = field(default_factory=dict)
    execution_context: Dict[str, Any] = field(default_factory=dict)

    # Diagnostics
    reason: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON/YAML serialization."""
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
    """
    Matches repo-relative POSIX paths against allowed patterns.

    Rules:
    - exact file path → complete match
    - trailing /** → recursive subdirectory match
    - * → single segment only (does NOT cross /)
    - .. / absolute / backslash → fail_closed
    """

    @staticmethod
    def normalize_path(path: str) -> Optional[str]:
        """
        Normalize path to repo-relative POSIX format.
        Return None if path is invalid (absolute, contains .., backslash, etc).
        """
        # Reject absolute paths
        if path.startswith('/'):
            return None

        # Convert backslashes to forward slashes
        normalized = path.replace('\\', '/')

        # Reject paths containing .. (parent directory traversal)
        if '..' in normalized.split('/'):
            return None

        # Remove leading ./
        if normalized.startswith('./'):
            normalized = normalized[2:]

        return normalized

    @staticmethod
    def matches_pattern(file_path: str, pattern: str) -> bool:
        """
        Check if file_path matches the allowed pattern.
        Both path and pattern are assumed normalized.
        """
        # Exact match
        if pattern == file_path:
            return True

        # Trailing /** → recursive directory match
        if pattern.endswith('/**'):
            dir_pattern = pattern[:-3]  # Remove /**
            # Match exact directory or anything under it
            if file_path == dir_pattern or file_path.startswith(dir_pattern + '/'):
                return True

        # Single segment wildcard (*)
        if '*' in pattern and '/**' not in pattern:
            # Convert * to regex: match anything except /
            regex_pattern = pattern.replace('.', r'\.').replace('*', '[^/]*')
            # Anchor pattern
            regex_pattern = f'^{regex_pattern}$'
            if re.match(regex_pattern, file_path):
                return True

        return False

    @staticmethod
    def is_file_allowed(file_path: str, allowed_paths: List[str]) -> bool:
        """
        Check if file_path is allowed by the allowed_paths list.
        """
        normalized = AllowedPathsMatcher.normalize_path(file_path)
        if normalized is None:
            # Path contains invalid elements (.. / absolute / backslash)
            return False

        for pattern in allowed_paths:
            norm_pattern = AllowedPathsMatcher.normalize_path(pattern)
            if norm_pattern is None:
                # Invalid pattern in allowed_paths
                continue
            if AllowedPathsMatcher.matches_pattern(normalized, norm_pattern):
                return True

        return False


class AllowedPathsGateEvaluator:
    """
    Main evaluator for PR allowed paths gate.
    """

    def __init__(
        self,
        pr_number: int,
        base_ref: str,
        base_sha: str,
        head_sha: str,
        reviewed_head_sha: str,
        allowed_paths: List[str],
        contract_body_sha256: str,
        issue_number: int = None,
        contract_source_kind: str = "github_issue",
        contract_source_id: str = None,
        expected_contract_fingerprint: Optional[Dict[str, Any]] = None,
    ):
        self.pr_number = pr_number
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.reviewed_head_sha = reviewed_head_sha
        self.allowed_paths = allowed_paths
        self.contract_body_sha256 = contract_body_sha256
        self.issue_number = issue_number or 0
        self.contract_source_kind = contract_source_kind
        self.contract_source_id = contract_source_id or ""
        # Fingerprint captured at contract-snapshot (go) time. Freshness is judged by
        # comparing this against the fingerprint recomputed from the CURRENT contract /
        # base at review time. When None, freshness cannot be judged and the gate
        # treats the snapshot as fresh (no expected baseline to diverge from).
        self.expected_contract_fingerprint = expected_contract_fingerprint

    def compute_allowed_paths_hash(self) -> str:
        """Compute SHA256 hash of normalized allowed_paths."""
        normalized = json.dumps(
            sorted(self.allowed_paths),
            separators=(',', ':'),
            ensure_ascii=True
        )
        return hashlib.sha256(normalized.encode()).hexdigest()

    def compute_contract_fingerprint(self) -> Dict[str, Any]:
        """
        Compute contract fingerprint for freshness judgment.
        Includes: issue_number, contract_source_*, allowed_paths_hash, base_ref, base_sha.
        """
        fp = ContractFingerprint(
            issue_number=self.issue_number,
            contract_source_kind=self.contract_source_kind,
            contract_source_id=self.contract_source_id,
            contract_body_sha256=self.contract_body_sha256,
            allowed_paths_normalized_sha256=self.compute_allowed_paths_hash(),
            base_ref=self.base_ref,
            base_sha_at_snapshot=self.base_sha,
        )
        return json.loads(fp.to_normalized_json())

    def get_changed_files_from_git(self) -> List[str]:
        """
        Get changed files using git diff triple-dot (merge-base..head).
        Format: git diff --name-only <base_sha>...<head_sha>
        """
        try:
            result = subprocess.run(
                [
                    'git',
                    'diff',
                    '--name-only',
                    f'{self.base_sha}...{self.head_sha}',
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            lines = [line.strip() for line in result.stdout.split('\n') if line.strip()]
            return lines
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git diff failed: {e.stderr}")

    def evaluate(self) -> AllowedPathsGateResult:
        """
        Evaluate the allowed paths gate.
        Returns ALLOWED_PATHS_GATE_RESULT_V1.
        """
        now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        result = AllowedPathsGateResult(
            produced_at=now,
            pr_number=self.pr_number,
            base_ref=self.base_ref,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            reviewed_head_sha=self.reviewed_head_sha,
        )

        # Step 1: Check if head_sha matches reviewed_head_sha
        if self.head_sha != self.reviewed_head_sha:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"head_sha mismatch: {self.head_sha} != {self.reviewed_head_sha} (merge-blocking)"
            result.errors.append(result.reason)
            return result

        # Step 1.5: Missing Allowed Paths → indeterminate (cannot judge compliance)
        if not self.allowed_paths:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = "Allowed Paths snapshot is missing or empty (merge-blocking)"
            result.errors.append(result.reason)
            return result

        # Step 2: Compute contract fingerprint (current, from the contract/base observed now)
        try:
            result.contract_fingerprint = self.compute_contract_fingerprint()
        except Exception as e:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"Failed to compute contract fingerprint: {str(e)}"
            result.errors.append(result.reason)
            return result

        # Step 2.5: Freshness — compare the snapshot-time (expected) fingerprint against the
        # fingerprint recomputed now. Any divergence (contract_body_sha256 / base_sha /
        # allowed_paths / base_ref / issue) means the snapshot is stale (merge-blocking).
        # execution_context (generated_at / worktree_root) is intentionally NOT part of the
        # fingerprint, so it can never trigger stale_snapshot.
        if self.expected_contract_fingerprint is not None and \
                self.expected_contract_fingerprint != result.contract_fingerprint:
            result.status = GateStatus.STALE_SNAPSHOT.value
            result.reason = "contract fingerprint diverged from snapshot (stale_snapshot, merge-blocking)"
            result.errors.append(result.reason)
            return result

        # Step 3: Get changed files from git diff
        try:
            changed_files = self.get_changed_files_from_git()
        except Exception as e:
            result.status = GateStatus.INDETERMINATE.value
            result.reason = f"Failed to get changed files: {str(e)}"
            result.errors.append(result.reason)
            return result

        result.changed_files = changed_files
        result.changed_files_count = len(changed_files)
        result.allowed_paths_list = self.allowed_paths
        result.allowed_paths_source = "linked_issue_contract_snapshot"

        # Step 4: Set execution context (audit log only)
        try:
            cwd = Path.cwd()
            result.execution_context = ExecutionContext(
                worktree_root=str(cwd),
                generated_at=now,
            ).to_dict()
        except Exception as e:
            result.execution_context = {
                "worktree_root": "unknown",
                "generated_at": now,
                "error": str(e),
            }

        # Step 5: Check if changed files are allowed
        violations = []
        for file in changed_files:
            if not AllowedPathsMatcher.is_file_allowed(file, self.allowed_paths):
                violations.append({
                    "file": file,
                    "reason": "not in allowed_paths",
                })

        result.violations = violations

        # Step 6: Determine status
        if violations:
            result.status = GateStatus.FAIL_CLOSED.value
            result.reason = f"{len(violations)} file(s) outside allowed paths"
        else:
            result.status = GateStatus.OK.value
            result.reason = "All changed files are within allowed paths"

        return result


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate PR allowed paths gate deterministically"
    )
    parser.add_argument('--pr-number', type=int, required=True, help='PR number')
    parser.add_argument('--base-ref', required=True, help='Base branch name (e.g., main)')
    parser.add_argument('--base-sha', required=True, help='Base SHA (at snapshot time)')
    parser.add_argument('--head-sha', required=True, help='Current head SHA')
    parser.add_argument('--reviewed-head-sha', required=True, help='Head SHA at review time')
    parser.add_argument('--allowed-paths', type=json.loads, required=True,
                        help='JSON array of allowed paths from issue contract')
    parser.add_argument('--contract-body-sha256', required=True,
                        help='SHA256 of issue contract body')
    parser.add_argument('--issue-number', type=int, help='Linked issue number')
    parser.add_argument('--expected-contract-fingerprint', type=json.loads, default=None,
                        help='JSON object: contract_fingerprint captured at snapshot (go) '
                             'time. When provided, a divergence from the freshly recomputed '
                             'fingerprint yields stale_snapshot.')
    parser.add_argument('--format', choices=['json', 'yaml'], default='json',
                        help='Output format')

    args = parser.parse_args()

    # Evaluate
    evaluator = AllowedPathsGateEvaluator(
        pr_number=args.pr_number,
        base_ref=args.base_ref,
        base_sha=args.base_sha,
        head_sha=args.head_sha,
        reviewed_head_sha=args.reviewed_head_sha,
        allowed_paths=args.allowed_paths,
        contract_body_sha256=args.contract_body_sha256,
        issue_number=args.issue_number,
        expected_contract_fingerprint=args.expected_contract_fingerprint,
    )

    result = evaluator.evaluate()
    result_dict = result.to_dict()

    # Output
    if args.format == 'yaml':
        try:
            import yaml
            print(yaml.dump(result_dict, default_flow_style=False, sort_keys=False))
        except ImportError:
            print("PyYAML not available, using JSON format instead", file=sys.stderr)
            print(json.dumps(result_dict, indent=2))
    else:
        print(json.dumps(result_dict, indent=2))

    # Exit code based on status
    if result.status == GateStatus.OK.value:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
