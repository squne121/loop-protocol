#!/usr/bin/env python3
"""AGY_GROUNDING_EVIDENCE_VERDICT_V1 validator (Issue #1776).

Scans a grounded_research-related PR diff and/or PR body text for causal
claims (statements of the form "X が原因" / "X により解消" / "X によって修正"
etc.) and verifies each claim is backed by concrete evidence -- a hook log
reference, a citation, or a content-evidence file/URL reference -- rather
than resting solely on the model's self-report.

Claims without a nearby evidence reference are fail-closed: they are listed
in `unsupported_claims[]` and the overall `status` becomes `fail_closed`.
Claims with a nearby evidence reference are listed in `evidence_bindings[]`.
When no causal claims are present at all, `status` is `ok` (nothing to flag).

This is a clean-room review support tool (Issue #1776 In Scope item 1): it
does not itself decide merge-blocking policy -- that is pr-review-judge's
responsibility -- it only produces the AGY_GROUNDING_EVIDENCE_VERDICT_V1
verdict that a reviewer (human or review_subagent) consumes as evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_ID = "AGY_GROUNDING_EVIDENCE_VERDICT_V1"

# Causal-claim phrase patterns (Japanese). Matched per-sentence within a
# paragraph; a "paragraph" is a blank-line-delimited block of the input text.
CAUSAL_CLAIM_PATTERNS = [
    re.compile(r"[^。\n]{0,200}が原因[^。\n]{0,80}。?"),
    re.compile(r"[^。\n]{0,200}により(?:解消|修正|解決|改善)[^。\n]{0,80}。?"),
    re.compile(r"[^。\n]{0,200}によって(?:解消|修正|解決|改善)[^。\n]{0,80}。?"),
    re.compile(r"[^。\n]{0,200}(?:のせいで|が原因で)[^。\n]{0,80}。?"),
]

# Evidence reference patterns: a backtick-quoted file-like path/URL, or an
# explicit sha256 digest, co-located in the same paragraph as the claim.
EVIDENCE_REF_PATTERN = re.compile(
    r"`[^`\n]*\.(?:py|log|jsonl|json|md|yml|yaml)`"
    r"|https?://\S+"
    r"|\bsha256:[0-9a-f]{8,}\b"
    r"|`[^`\n]*\.claude/hooks/[^`\n]*`"
)

# Keywords that, combined with a concrete reference above, indicate the
# reference is being cited as hook/citation/content evidence (not just an
# incidental file mention).
EVIDENCE_KEYWORD_PATTERN = re.compile(
    r"hook|citation|content[_ ]?evidence|ログ|証跡|引用|出典",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceBinding:
    claim: str
    paragraph_index: int
    evidence_ref: str


@dataclass(frozen=True)
class UnsupportedClaim:
    claim: str
    paragraph_index: int
    reason: str


def _split_paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                paragraphs.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def _find_claims(paragraph: str) -> list[str]:
    claims: list[str] = []
    for pattern in CAUSAL_CLAIM_PATTERNS:
        for match in pattern.finditer(paragraph):
            claim_text = match.group(0).strip()
            if claim_text and claim_text not in claims:
                claims.append(claim_text)
    return claims


def _find_evidence_ref(paragraph: str) -> str | None:
    ref_match = EVIDENCE_REF_PATTERN.search(paragraph)
    if ref_match is None:
        return None
    if EVIDENCE_KEYWORD_PATTERN.search(paragraph) is None:
        return None
    return ref_match.group(0)


def evaluate_grounding_evidence(text: str) -> dict:
    """Evaluate causal claims in `text` and return an
    AGY_GROUNDING_EVIDENCE_VERDICT_V1-shaped dict (schema field omitted;
    callers/CLI attach schema + generated_at)."""
    paragraphs = _split_paragraphs(text)
    evidence_bindings: list[EvidenceBinding] = []
    unsupported_claims: list[UnsupportedClaim] = []

    for idx, paragraph in enumerate(paragraphs):
        claims = _find_claims(paragraph)
        if not claims:
            continue
        evidence_ref = _find_evidence_ref(paragraph)
        for claim in claims:
            if evidence_ref is not None:
                evidence_bindings.append(
                    EvidenceBinding(claim=claim, paragraph_index=idx, evidence_ref=evidence_ref)
                )
            else:
                unsupported_claims.append(
                    UnsupportedClaim(
                        claim=claim,
                        paragraph_index=idx,
                        reason=(
                            "causal claim has no co-located hook log / citation / "
                            "content-evidence reference; appears to rest on self-report only"
                        ),
                    )
                )

    status = "fail_closed" if unsupported_claims else "ok"
    return {
        "status": status,
        "evidence_bindings": [asdict(item) for item in evidence_bindings],
        "unsupported_claims": [asdict(item) for item in unsupported_claims],
    }


def _read_optional(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff-file", type=str, default=None, help="Path to PR diff text")
    parser.add_argument("--pr-body-file", type=str, default=None, help="Path to PR body/comment text")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output file (default: stdout)")
    args = parser.parse_args(argv)

    if not args.diff_file and not args.pr_body_file:
        print("ERROR: at least one of --diff-file / --pr-body-file is required", file=sys.stderr)
        return 2

    combined_text = "\n\n".join(
        part for part in (_read_optional(args.diff_file), _read_optional(args.pr_body_file)) if part
    )

    result = evaluate_grounding_evidence(combined_text)
    output_dict = {
        "schema": SCHEMA_ID,
        **result,
    }
    output = json.dumps(output_dict, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)

    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
