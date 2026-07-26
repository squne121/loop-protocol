#!/usr/bin/env python3
"""check_agy_causal_claim_drift.py - fail-closed AGY causal-claim drift check.

Issue #1778 (parent: #1265, source: adversarial re-audit of CLOSED #1494).

`agy_permission_policy.py` / `run_gemini_headless.py` carry code comments
that reference `Issue #N` as the origin/justification for a given design
decision. Some of those referenced Issues have since been investigated to a
conclusion recorded in a `references/*.md` document under
`.claude/skills/gemini-cli-headless-delegation/references/` -- and that
document's YAML frontmatter `status:` can be `resolved` or `refuted`,
meaning the referenced Issue's original claim is no longer the live
understanding.

When that happens, this script requires the code comment to carry an
explicit back-reference marker (`# SUPERSEDED (Issue #M): ...`, anywhere in
the same contiguous comment block as the `Issue #N` reference) acknowledging
that the investigation moved on. A comment that references an Issue whose
findings doc says `status: resolved` / `status: refuted`, with no such
marker nearby, is causal-claim drift: the comment is presenting a claim as
current when a later investigation already resolved or refuted it.

This is a **read-only, static** check: `agy_permission_policy.py` /
`run_gemini_headless.py` are only ever read as text, never imported or
edited (Issue #1778 Out of Scope / Stop Conditions -- no behavior change).

Exit codes:
  0 = no drift detected (or, with --apply-baseline, no NEW drift beyond the
      pre-existing baseline)
  1 = drift detected (fail-closed)
  2 = usage / input error (a target file is missing)

Design references:
- Issue #1778 Source item 5, AC3
- Issue #1788 (CI gate integration): `--apply-baseline` added below
- `.claude/skills/gemini-cli-headless-delegation/references/agy-headless-tool-use-investigation.md`
- `.claude/skills/gemini-cli-headless-delegation/schemas/agy_causal_claim_manifest_v1.schema.json`

## Issue #1788: CI gate baseline/allowlist mechanism

`build_manifest()` / the underlying drift detection rule (an `Issue #N`
comment reference with no `SUPERSEDED` marker, where the referenced Issue's
findings doc is `resolved`/`refuted`) is **unchanged** by this addition
(Issue #1788 Out of Scope). What is added is a purely additive,
opt-in *exit-code* filter: `--apply-baseline` compares each detected
finding's stable identity (`source_file` + `issue_number` + `doc_path` +
`doc_status` -- deliberately NOT `line`, so unrelated line-number churn in
the same file does not spuriously "un-baseline" an already-known finding)
against `_CI_GATE_BASELINE_KEYS` below. Findings whose stable key is already
in the baseline still appear in the printed manifest (`baseline_count`),
but do not contribute to the fail-closed exit code; only a finding whose
stable key is NOT in the baseline (a genuinely NEW drift) causes exit 1.

Without `--apply-baseline`, behavior is 100% unchanged: any p0 finding
fails closed (exit 1), same as before Issue #1788.

The baseline below was captured against the repo state as of Issue #1788
(54 raw findings / 5 stable keys, see PR for the Issue #1788 implementation
for the exact capture command and count). Burning down this baseline (fixing
the pre-existing drift and removing entries here) is intentionally left as
follow-up work -- Issue #1788 Out of Scope forbids changing the detection
logic itself, and bulk-fixing 24+ existing code comments is a separate,
larger effort.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_ID = "AGY_CAUSAL_CLAIM_MANIFEST_V1"
PRODUCER = "check_agy_causal_claim_drift"

_REPO_ROOT = Path(__file__).resolve().parents[1]

_DEFAULT_CODE_TARGETS: tuple[str, ...] = (
    ".claude/skills/gemini-cli-headless-delegation/scripts/agy_permission_policy.py",
    ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py",
)
_DEFAULT_REFERENCES_DIR = (
    ".claude/skills/gemini-cli-headless-delegation/references"
)

# Issue #1788: baseline of causal-claim drift findings known to exist at CI
# gate activation time. Each entry is the STABLE identity of a finding
# (source_file, issue_number, doc_path, doc_status) -- intentionally
# excluding `line`, so that unrelated line-number shifts within an already
# -baselined (source_file, issue_number, doc_path, doc_status) combination
# do not spuriously register as new drift. A genuinely new combination
# (e.g. a fresh `Issue #N` reference to a different resolved/refuted Issue,
# or a reference in a file not covered here) is NOT in this set and will
# still fail the gate. See module docstring "Issue #1788: CI gate
# baseline/allowlist mechanism" above.
_CI_GATE_BASELINE_KEYS: frozenset[tuple[str, int, str, str]] = frozenset(
    {
        (
            ".claude/skills/gemini-cli-headless-delegation/scripts/"
            "agy_permission_policy.py",
            1758,
            ".claude/skills/gemini-cli-headless-delegation/references/"
            "agy-headless-tool-use-investigation.md",
            "resolved",
        ),
        (
            ".claude/skills/gemini-cli-headless-delegation/scripts/"
            "run_gemini_headless.py",
            1708,
            ".claude/skills/gemini-cli-headless-delegation/references/"
            "agy-headless-tool-use-investigation.md",
            "resolved",
        ),
        (
            ".claude/skills/gemini-cli-headless-delegation/scripts/"
            "run_gemini_headless.py",
            1752,
            ".claude/skills/gemini-cli-headless-delegation/references/"
            "agy-headless-tool-use-investigation.md",
            "resolved",
        ),
        (
            ".claude/skills/gemini-cli-headless-delegation/scripts/"
            "run_gemini_headless.py",
            1771,
            ".claude/skills/gemini-cli-headless-delegation/references/"
            "agy-headless-tool-use-investigation.md",
            "resolved",
        ),
        (
            ".claude/skills/gemini-cli-headless-delegation/scripts/"
            "run_gemini_headless.py",
            1777,
            ".claude/skills/gemini-cli-headless-delegation/references/"
            "agy-headless-tool-use-investigation.md",
            "resolved",
        ),
    }
)


def _finding_baseline_key(finding: dict[str, Any]) -> tuple[str, int, str, str]:
    return (
        finding["source_file"],
        finding["issue_number"],
        finding["doc_path"],
        finding["doc_status"],
    )


def partition_baseline_findings(
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into (baselined, new) using `_CI_GATE_BASELINE_KEYS`."""
    baselined: list[dict[str, Any]] = []
    new: list[dict[str, Any]] = []
    for finding in findings:
        if _finding_baseline_key(finding) in _CI_GATE_BASELINE_KEYS:
            baselined.append(finding)
        else:
            new.append(finding)
    return baselined, new

_ISSUE_REF_RE = re.compile(r"Issue\s*#(\d+)")
_SUPERSEDED_RE = re.compile(r"SUPERSEDED\s*\(Issue\s*#\d+\)", re.IGNORECASE)
_RESOLVING_STATUSES = frozenset({"resolved", "refuted"})
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_STATUS_FIELD_RE = re.compile(r"^status:\s*(\S+)\s*$", re.MULTILINE)
_BARE_ISSUE_NUM_RE_TEMPLATE = r"(?<!\d)#{n}(?!\d)"


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _comment_block_bounds(lines: list[str], line_idx: int) -> tuple[int, int]:
    """Return (start, end) 0-based inclusive bounds of the contiguous
    `#`-prefixed comment block containing line_idx."""
    start = line_idx
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    end = line_idx
    while end + 1 < len(lines) and lines[end + 1].strip().startswith("#"):
        end += 1
    return start, end


def _extract_issue_references(
    source: str, source_path: str
) -> list[dict[str, Any]]:
    """Return every `Issue #N` reference found in comment lines, along with
    whether a SUPERSEDED marker is present in the same comment block."""
    lines = source.splitlines()
    refs: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        for match in _ISSUE_REF_RE.finditer(line):
            issue_number = int(match.group(1))
            block_start, block_end = _comment_block_bounds(lines, idx)
            block_text = "\n".join(lines[block_start : block_end + 1])
            superseded = bool(_SUPERSEDED_RE.search(block_text))
            refs.append(
                {
                    "issue_number": issue_number,
                    "source_file": source_path,
                    "line": idx + 1,
                    "superseded_marker_present": superseded,
                }
            )
    return refs


def _parse_frontmatter_status(doc_text: str) -> "str | None":
    match = _FRONTMATTER_RE.match(doc_text)
    if not match:
        return None
    frontmatter = match.group(1)
    status_match = _STATUS_FIELD_RE.search(frontmatter)
    if not status_match:
        return None
    return status_match.group(1)


def _doc_mentions_issue(doc_body: str, issue_number: int) -> bool:
    pattern = re.compile(_BARE_ISSUE_NUM_RE_TEMPLATE.format(n=issue_number))
    return bool(pattern.search(doc_body))


def _build_resolved_issue_index(
    references_dir: Path,
) -> dict[int, list[tuple[str, str]]]:
    """Return {issue_number: [(doc_repo_relative_path, status), ...]} for
    every references/*.md doc whose frontmatter status is resolved/refuted
    and which mentions that issue number (bare `#N`) anywhere in its body."""
    index: dict[int, list[tuple[str, str]]] = {}
    if not references_dir.is_dir():
        return index
    for doc_path in sorted(references_dir.glob("*.md")):
        doc_text = doc_path.read_text(encoding="utf-8")
        status = _parse_frontmatter_status(doc_text)
        if status not in _RESOLVING_STATUSES:
            continue
        # Body only (after frontmatter), so a mention inside the frontmatter
        # block itself (e.g. `related_issue: "#1494"`) does not count.
        body = _FRONTMATTER_RE.sub("", doc_text, count=1)
        # Collect every bare issue number mentioned in the body once, then
        # test membership per code-side reference below.
        for match in re.finditer(r"(?<!\d)#(\d+)(?!\d)", body):
            mentioned = int(match.group(1))
            index.setdefault(mentioned, []).append(
                (_repo_relative(doc_path), status)
            )
    return index


def build_manifest(
    code_targets: list[Path], references_dir: Path
) -> dict[str, Any]:
    resolved_index = _build_resolved_issue_index(references_dir)

    findings: list[dict[str, Any]] = []
    target_repo_relative: list[str] = []
    for target in code_targets:
        repo_relative = _repo_relative(target)
        target_repo_relative.append(repo_relative)
        source = target.read_text(encoding="utf-8")
        for ref in _extract_issue_references(source, repo_relative):
            if ref["superseded_marker_present"]:
                continue
            docs = resolved_index.get(ref["issue_number"])
            if not docs:
                continue
            for doc_path, status in docs:
                findings.append(
                    {
                        "finding_id": (
                            f"causal_claim_drift:{repo_relative}:"
                            f"{ref['line']}:Issue{ref['issue_number']}"
                        ),
                        "kind": "causal_claim_drift",
                        "identifier": f"Issue #{ref['issue_number']}",
                        "detail": (
                            f"{repo_relative}:{ref['line']} references "
                            f"Issue #{ref['issue_number']} without a "
                            f"'# SUPERSEDED (Issue #M): ...' back-reference "
                            f"marker, but {doc_path} (status: {status}) "
                            f"already mentions Issue #{ref['issue_number']} "
                            f"as resolved/refuted."
                        ),
                        "severity": "p0",
                        "source_file": repo_relative,
                        "line": ref["line"],
                        "issue_number": ref["issue_number"],
                        "doc_path": doc_path,
                        "doc_status": status,
                    }
                )

    summary: dict[str, int] = {}
    for finding in findings:
        summary[finding["kind"]] = summary.get(finding["kind"], 0) + 1

    status = "findings_detected" if findings else "ok"
    return {
        "schema": SCHEMA_ID,
        "producer": PRODUCER,
        "target_files": target_repo_relative,
        "findings": findings,
        "summary": summary,
        "status": status,
    }


def _resolve_repo_relative(raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate
    return _REPO_ROOT / raw


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--code-target",
        action="append",
        dest="code_targets",
        default=None,
        help=(
            "Repo-relative path to a source file to scan for 'Issue #N' "
            "comments. May be given multiple times. Defaults to "
            "agy_permission_policy.py and run_gemini_headless.py."
        ),
    )
    parser.add_argument(
        "--references-dir",
        default=_DEFAULT_REFERENCES_DIR,
        help="Repo-relative path to the references/*.md directory to check.",
    )
    parser.add_argument(
        "--apply-baseline",
        action="store_true",
        default=False,
        help=(
            "Issue #1788: only fail closed on findings NOT already present "
            "in the built-in _CI_GATE_BASELINE_KEYS baseline. Findings "
            "matching the baseline are still reported in the manifest "
            "(baseline_count) but do not affect the exit code. Without "
            "this flag, behavior is unchanged from pre-#1788: any p0 "
            "finding fails closed."
        ),
    )
    args = parser.parse_args(argv)

    code_target_strs = args.code_targets or list(_DEFAULT_CODE_TARGETS)
    code_targets: list[Path] = []
    for raw in code_target_strs:
        resolved = _resolve_repo_relative(raw)
        if not resolved.exists():
            print(
                json.dumps(
                    {
                        "schema": SCHEMA_ID,
                        "producer": PRODUCER,
                        "error": f"code target file not found: {raw}",
                    }
                )
            )
            return 2
        code_targets.append(resolved)

    references_dir = _resolve_repo_relative(args.references_dir)

    manifest = build_manifest(code_targets, references_dir)

    if args.apply_baseline:
        baselined, new_findings = partition_baseline_findings(
            manifest["findings"]
        )
        manifest["baseline_applied"] = True
        manifest["baseline_count"] = len(baselined)
        manifest["new_finding_count"] = len(new_findings)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        has_new_p0 = any(f["severity"] == "p0" for f in new_findings)
        return 1 if has_new_p0 else 0

    manifest["baseline_applied"] = False
    print(json.dumps(manifest, indent=2, sort_keys=True))
    has_p0 = any(f["severity"] == "p0" for f in manifest["findings"])
    return 1 if has_p0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
