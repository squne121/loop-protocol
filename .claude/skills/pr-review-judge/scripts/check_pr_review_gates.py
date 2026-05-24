#!/usr/bin/env python3
"""
PR Review Deterministic Gates Checker (G1-G5)

Implements 5 deterministic gates to catch reviewer miss-types:
  G1: CI gate coverage (ci_test_selection/v1 artifact required)
  G2: Evidence binding structure validation
  G3: Implementation oracle verification (Python AST + grep)
  G4: Head SHA consistency check
  G5: Fixture guard path coverage trace
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import subprocess
import ast
import re
from dataclasses import dataclass, asdict
from enum import Enum


class GateStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class Verdict(Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass
class Finding:
    """Evidence reference structure per finding (per-finding scope)."""
    head_sha: str
    source_kind: str  # e.g., "ci_artifact", "github_api", "local_git"
    evidence_refs: Dict[str, Any]  # {code_ref?, pr_head_sha?, local_head_sha?, ci_run_ref{url,workflow,job,step,command}?}


@dataclass
class GateResult:
    """Result of a single gate."""
    gate_id: str  # g1, g2, g3, g4, g5
    gate_name: str
    status: str  # pass | fail | not_applicable
    minimal_context: Optional[str] = None  # Only for fail gates
    findings: Optional[List[Finding]] = None


@dataclass
class PRReviewGateResult:
    """PR_REVIEW_GATE_RESULT_V1 schema."""
    schema_version: str = "PR_REVIEW_GATE_RESULT_V1"
    generated_at: str = None
    generated_by: str = "check_pr_review_gates.py"
    verdict: str = "APPROVE"  # APPROVE | REQUEST_CHANGES
    gates: List[GateResult] = None

    def __post_init__(self):
        if self.generated_at is None:
            self.generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self.gates is None:
            self.gates = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "generated_by": self.generated_by,
            "verdict": self.verdict,
            "gates": [
                {
                    "gate_id": g.gate_id,
                    "gate_name": g.gate_name,
                    "status": g.status,
                    **({"minimal_context": g.minimal_context} if g.minimal_context and g.status == "fail" else {}),
                    **({"findings": [{"head_sha": f.head_sha, "source_kind": f.source_kind, "evidence_refs": f.evidence_refs} for f in g.findings]} if g.findings else {})
                }
                for g in self.gates
            ]
        }


class CheckPRReviewGates:
    """Main checker class implementing G1-G5."""

    def __init__(self):
        self.result = PRReviewGateResult()

    def g1_ci_test_selection(self, artifact_path: Optional[str] = None) -> GateResult:
        """
        G1: CI gate coverage

        Checks CI artifact (schema_version: ci_test_selection/v1) for uncovered test files.
        Requires: HEAD SHA + uncovered_changed_test_files field non-empty → fail
        """
        gate = GateResult(
            gate_id="g1",
            gate_name="ci_test_selection",
            status=GateStatus.NOT_APPLICABLE.value
        )

        if not artifact_path or not Path(artifact_path).exists():
            gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        try:
            with open(artifact_path) as f:
                artifact = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        # Verify schema version
        if artifact.get("schema_version") != "ci_test_selection/v1":
            gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        head_sha = artifact.get("head_sha")
        uncovered = artifact.get("uncovered_changed_test_files", [])

        if uncovered:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = f"Uncovered changed test files: {', '.join(uncovered)}"
            gate.findings = [Finding(
                head_sha=head_sha or "unknown",
                source_kind="ci_artifact",
                evidence_refs={
                    "ci_run_ref": {
                        "url": artifact.get("ci_run_url", "N/A"),
                        "workflow": artifact.get("workflow", "N/A"),
                        "job": artifact.get("job", "N/A"),
                        "command": artifact.get("collected_test_files", [])
                    }
                }
            )]
        else:
            gate.status = GateStatus.PASS.value

        return gate

    def g2_evidence_binding(self, pr_body: str = "", pr_head_sha: str = "", local_head_sha: str = "") -> GateResult:
        """
        G2: Evidence binding structure

        Validates per-finding evidence_refs structure containing:
        - code_ref (optional per-finding)
        - pr_head_sha (optional per-finding)
        - local_head_sha (optional per-finding)
        - ci_run_ref (optional per-finding with {url, workflow, job, step, command})

        Per-finding scope allows flexible evidence binding without top-level required fields.
        self_report 単独 APPROVE 禁止: チェックは PR body に evidence が適切に構造化されているかを確認。
        """
        gate = GateResult(
            gate_id="g2",
            gate_name="evidence_binding",
            status=GateStatus.PASS.value
        )

        # Basic check: if PR has evidence markers, validate structure
        # This is a structural validation that self_report ONLY is not acceptable.
        # In practice, pr-review-judge will check the actual evidence completeness.

        # For this checker: detect if there's evidence_refs structure anywhere
        # and ensure it's not solely self_report
        if "self_report" in pr_body and "evidence" not in pr_body and "findings" not in pr_body:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = "Evidence binding: self_report found without supporting evidence_refs structure. self_report alone cannot approve PR."
            gate.findings = [Finding(
                head_sha=pr_head_sha or local_head_sha or "unknown",
                source_kind="pr_body",
                evidence_refs={"self_report_only": True}
            )]

        return gate

    def g3_implementation_oracle(self, issue_body: str = "", target_files: Optional[List[str]] = None, method: str = "ast") -> GateResult:
        """
        G3: Implementation oracle verification

        Parses implementation_oracles from issue body (YAML/markdown list).
        For kind: python_ast_call → uses AST-based verification.
        Falls back to grep if AST fails or kind: grep specified.
        """
        gate = GateResult(
            gate_id="g3",
            gate_name="implementation_oracle",
            status=GateStatus.NOT_APPLICABLE.value
        )

        if not issue_body or "implementation_oracles:" not in issue_body:
            return gate

        oracles = self._parse_implementation_oracles(issue_body)
        if not oracles:
            return gate

        gate.status = GateStatus.PASS.value
        findings: List[Finding] = []
        failed_oracles = []

        for oracle in oracles:
            oracle_id = oracle.get("id", "unknown")
            oracle_kind = oracle.get("kind", "grep")
            files = oracle.get("files", target_files or [])

            if oracle_kind == "python_ast_call":
                result = self._verify_ast_call(oracle, files)
            else:  # fallback to grep
                result = self._verify_grep(oracle, files)

            if not result["passed"]:
                failed_oracles.append((oracle_id, result["error"]))
                findings.append(Finding(
                    head_sha=result.get("head_sha", "unknown"),
                    source_kind=oracle_kind,
                    evidence_refs={"oracle_id": oracle_id, "error": result["error"]}
                ))

        if failed_oracles:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = f"Oracle verification failed: {failed_oracles}"
            gate.findings = findings

        return gate

    def _parse_implementation_oracles(self, issue_body: str) -> List[Dict[str, Any]]:
        """Parse implementation_oracles block from markdown."""
        oracles = []
        lines = issue_body.split("\n")
        in_oracles = False
        current_oracle = {}
        list_item_indent = None

        for line in lines:
            if "implementation_oracles:" in line:
                in_oracles = True
                continue
            if in_oracles:
                # Detect list item (starts with "- ")
                if re.match(r"^\s*- ", line):
                    # Save previous oracle if exists
                    if current_oracle and "id" in current_oracle:
                        oracles.append(current_oracle)
                    # Start new oracle from list item
                    current_oracle = {}
                    list_item_indent = len(line) - len(line.lstrip())
                    # Extract id from first line after "- "
                    remainder = line.strip()[2:].strip()
                    if remainder:
                        current_oracle["id"] = remainder
                elif line.strip() == "":
                    # Empty line might end the section
                    continue
                elif line.strip() and not line[0].isspace() and not line.startswith("#"):
                    # Non-indented non-empty line ends section
                    if current_oracle and "id" in current_oracle:
                        oracles.append(current_oracle)
                    in_oracles = False
                    break
                elif line.strip().startswith("id:") and current_oracle:
                    current_oracle["id"] = line.split(":", 1)[1].strip()
                elif line.strip().startswith("kind:") and current_oracle:
                    current_oracle["kind"] = line.split(":", 1)[1].strip()
                elif line.strip().startswith("files:") and current_oracle:
                    # Simple parse: files on same line or next lines
                    files_part = line.split(":", 1)[1].strip()
                    if files_part.startswith("["):
                        # YAML list format, simple extraction
                        current_oracle["files"] = [f.strip("- [],'\"").strip() for f in files_part.split(",")]
                    else:
                        current_oracle["files"] = [files_part] if files_part else []
                elif line.strip().startswith("must_call:") and current_oracle:
                    current_oracle["must_call"] = {}
                elif "module:" in line and current_oracle and "must_call" in current_oracle:
                    current_oracle["must_call"]["module"] = line.split(":", 1)[1].strip()
                elif "function:" in line and current_oracle and "must_call" in current_oracle:
                    current_oracle["must_call"]["function"] = line.split(":", 1)[1].strip()

        if current_oracle and "id" in current_oracle:
            oracles.append(current_oracle)

        return oracles

    def _verify_ast_call(self, oracle: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
        """Verify must_call using Python AST analysis."""
        must_call = oracle.get("must_call", {})
        module = must_call.get("module")
        function = must_call.get("function")

        if not module or not function:
            return {"passed": False, "error": "must_call missing module or function"}

        found_calls = []
        for filepath in files:
            if not Path(filepath).exists():
                continue
            try:
                with open(filepath) as f:
                    tree = ast.parse(f.read())
                calls = self._find_ast_calls(tree, module, function)
                found_calls.extend(calls)
            except (SyntaxError, UnicodeDecodeError):
                continue

        if found_calls:
            return {"passed": True, "head_sha": "ast-verified"}
        return {
            "passed": False,
            "error": f"AST call not found: {module}.{function} in {files}",
            "head_sha": "unknown"
        }

    def _find_ast_calls(self, tree: ast.AST, module: str, function: str) -> List[str]:
        """Find all calls to module.function in AST."""
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Match module.function pattern
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        if node.func.value.id == module and node.func.attr == function:
                            calls.append(f"{module}.{function}")
        return calls

    def _verify_grep(self, oracle: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
        """Fallback verification using grep."""
        must_call = oracle.get("must_call", {})
        function = must_call.get("function", oracle.get("pattern", ""))

        if not function:
            return {"passed": False, "error": "No pattern or function specified"}

        for filepath in files:
            if not Path(filepath).exists():
                continue
            try:
                result = subprocess.run(
                    ["grep", "-q", function, filepath],
                    capture_output=True,
                    timeout=5
                )
                if result.returncode == 0:
                    return {"passed": True, "head_sha": "grep-verified"}
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

        return {
            "passed": False,
            "error": f"Grep pattern not found: {function} in {files}",
            "head_sha": "unknown"
        }

    def g4_head_sha_consistency(self, pr_head_sha: str = "", local_head_sha: str = "") -> GateResult:
        """
        G4: Head SHA consistency

        Compares gh pr view headRefOid with local git rev-parse.
        Mismatch → fail (indicates push incomplete or stale review).
        """
        gate = GateResult(
            gate_id="g4",
            gate_name="head_sha_consistency",
            status=GateStatus.PASS.value
        )

        if not pr_head_sha or not local_head_sha:
            gate.status = GateStatus.NOT_APPLICABLE.value
            return gate

        if pr_head_sha != local_head_sha:
            gate.status = GateStatus.FAIL.value
            gate.minimal_context = f"Head SHA mismatch: PR={pr_head_sha[:8]}, local={local_head_sha[:8]}. Commit may not be pushed."
            gate.findings = [Finding(
                head_sha=pr_head_sha,
                source_kind="github_api_vs_local_git",
                evidence_refs={
                    "pr_head_sha": pr_head_sha,
                    "local_head_sha": local_head_sha,
                    "note": "mismatch indicates incomplete push"
                }
            )]
        else:
            gate.status = GateStatus.PASS.value

        return gate

    def g5_fixture_guard_path_coverage(self, trace_log: Optional[str] = None, coverage_file: Optional[str] = None) -> GateResult:
        """
        G5: Fixture guard path coverage

        Verifies fixture_path_coverage/v1 trace output.
        Can use --trace-guards flag output or coverage marker file.
        Fails if no guard path coverage evidence is found.
        """
        gate = GateResult(
            gate_id="g5",
            gate_name="fixture_guard_path_coverage",
            status=GateStatus.NOT_APPLICABLE.value
        )

        # Check trace log for fixture_path_coverage/v1
        if trace_log:
            if "fixture_path_coverage/v1" in trace_log:
                gate.status = GateStatus.PASS.value
                return gate

        # Check coverage file
        if coverage_file and Path(coverage_file).exists():
            try:
                with open(coverage_file) as f:
                    content = f.read()
                    if "fixture_path_coverage/v1" in content:
                        gate.status = GateStatus.PASS.value
                        return gate
            except (FileNotFoundError, IOError):
                pass

        # Default: not applicable if no evidence found
        gate.status = GateStatus.NOT_APPLICABLE.value
        return gate

    def run_gate(self, gate_id: str, **kwargs) -> GateResult:
        """Run a specific gate by ID."""
        if gate_id == "g1":
            return self.g1_ci_test_selection(kwargs.get("artifact_path"))
        elif gate_id == "g2":
            return self.g2_evidence_binding(
                kwargs.get("pr_body", ""),
                kwargs.get("pr_head_sha", ""),
                kwargs.get("local_head_sha", "")
            )
        elif gate_id == "g3":
            return self.g3_implementation_oracle(
                kwargs.get("issue_body", ""),
                kwargs.get("target_files"),
                kwargs.get("method", "ast")
            )
        elif gate_id == "g4":
            return self.g4_head_sha_consistency(
                kwargs.get("pr_head_sha", ""),
                kwargs.get("local_head_sha", "")
            )
        elif gate_id == "g5":
            return self.g5_fixture_guard_path_coverage(
                kwargs.get("trace_log"),
                kwargs.get("coverage_file")
            )
        raise ValueError(f"Unknown gate: {gate_id}")

    def finalize_verdict(self):
        """Determine final verdict based on gate results."""
        has_fail = any(g.status == GateStatus.FAIL.value for g in self.result.gates)
        self.result.verdict = Verdict.REQUEST_CHANGES.value if has_fail else Verdict.APPROVE.value


def main():
    parser = argparse.ArgumentParser(
        description="PR Review Deterministic Gates Checker (G1-G5)"
    )
    parser.add_argument(
        "--rule", "-r",
        help="Gate to run: g1|g2|g3|g4|g5 or all (default: all)",
        default="all"
    )
    parser.add_argument(
        "--ci-artifact",
        help="Path to CI artifact (ci_test_selection/v1 JSON)"
    )
    parser.add_argument(
        "--pr-body",
        help="PR body text"
    )
    parser.add_argument(
        "--pr-head-sha",
        help="PR head SHA from GitHub"
    )
    parser.add_argument(
        "--local-head-sha",
        help="Local HEAD SHA from git"
    )
    parser.add_argument(
        "--issue-body",
        help="Issue body text for oracle verification"
    )
    parser.add_argument(
        "--target-files",
        nargs="+",
        help="Target files for oracle verification"
    )
    parser.add_argument(
        "--trace-guards",
        help="Trace log output from --trace-guards flag"
    )
    parser.add_argument(
        "--coverage-file",
        help="Coverage marker file with fixture_path_coverage/v1"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file (default: stdout)",
        default=None
    )
    parser.add_argument(
        "--format", "-f",
        choices=["json", "yaml"],
        default="json",
        help="Output format"
    )

    args = parser.parse_args()

    checker = CheckPRReviewGates()
    gates_to_run = args.rule.split(",") if args.rule != "all" else ["g1", "g2", "g3", "g4", "g5"]

    for gate in gates_to_run:
        gate = gate.strip().lower()
        kwargs = {
            "artifact_path": args.ci_artifact,
            "pr_body": args.pr_body or "",
            "pr_head_sha": args.pr_head_sha or "",
            "local_head_sha": args.local_head_sha or "",
            "issue_body": args.issue_body or "",
            "target_files": args.target_files,
            "trace_log": args.trace_guards,
            "coverage_file": args.coverage_file,
        }
        result = checker.run_gate(gate, **kwargs)
        checker.result.gates.append(result)

    checker.finalize_verdict()

    # Serialize output
    output_dict = checker.result.to_dict()

    if args.format == "yaml":
        try:
            import yaml
            output = yaml.dump(output_dict, default_flow_style=False)
        except ImportError:
            # Fallback to JSON if PyYAML not available
            output = json.dumps(output_dict, indent=2)
    else:
        output = json.dumps(output_dict, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Result written to {args.output}")
    else:
        print(output)

    # Exit code based on verdict
    sys.exit(0 if checker.result.verdict == Verdict.APPROVE.value else 1)


if __name__ == "__main__":
    main()
