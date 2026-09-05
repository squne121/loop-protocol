"""test_pr_body_acquisition_parity.py (Issue #2499)

producer (`.github/workflows/ci.yml`) and trusted consumer
(`.github/workflows/visual-impact-trusted-consumer.yml`) must share the
same live PR body acquisition semantics: the same live Pull REST endpoint
pattern (`repos/{repo}/pulls/{pr_number}`), the same jq filter
(`.body // ""`), and a direct stdout-to-file persistence with no
canonicalization-changing intermediate (no shell variable binding +
`printf`, no command substitution round-trip). The producer must not use
the `github.event.pull_request.body` event-time snapshot (PR #2137 moved
it to a live `gh api pulls/{num}` fetch; see #2145 for the prior
event-snapshot vs. live-fetch divergence this parity guards against).

This module parses both workflow files with `yaml.safe_load()`,
structurally extracts the relevant `run:` step's acquisition semantics
(never coupling to full shell script text, wrapper naming, or unrelated
variable naming), and:

- AC1 (`test_live_pr_body_acquisition_parity`): asserts the live
  producer/consumer acquisition specs are in parity.
- AC2 (`test_acquisition_parity_rejects_semantic_drift`): asserts the
  same extractor/parity-checker REJECTS synthetic negative fixtures that
  each introduce exactly one kind of semantic drift on only one side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONSUMER_WORKFLOW_PATH = (
    REPO_ROOT / ".github" / "workflows" / "visual-impact-trusted-consumer.yml"
)

_VAR_TOKEN_RE = re.compile(r"\$\{[A-Za-z0-9_]+\}")

# Matches: (gh_api|gh api) "repos/${ANY}/pulls/${ANY}" --jq '<filter>' > <file>
_DIRECT_ACQUISITION_RE = re.compile(
    r'^(?:gh_api|gh api)\s+"(?P<endpoint>repos/\$\{[A-Za-z0-9_]+\}/pulls/\$\{[A-Za-z0-9_]+\})"'
    r"\s+--jq\s+'(?P<jq>[^']*)'\s*>\s*(?P<outfile>\S+)$"
)

# Matches: VAR="$( (gh_api|gh api) "repos/${ANY}/pulls/${ANY}" --jq '<filter>' )"
# i.e. the stdout is bound to a shell variable via command substitution
# instead of being persisted to a file directly -- a canonicalization-
# changing intermediate per the Issue #2499 contract.
_SUBSTITUTION_ACQUISITION_RE = re.compile(
    r'^(?P<var>[A-Za-z_][A-Za-z0-9_]*)="\$\('
    r'(?:gh_api|gh api)\s+"(?P<endpoint>repos/\$\{[A-Za-z0-9_]+\}/pulls/\$\{[A-Za-z0-9_]+\})"'
    r"\s+--jq\s+'(?P<jq>[^']*)'"
    r'\)"$'
)

_EVENT_SNAPSHOT_MARKER = "github.event.pull_request.body"


class AcquisitionSpecError(Exception):
    """Raised when a step's `run:` text contains no recognizable live PR
    body REST acquisition line and does not fall back to the event-time
    snapshot marker either (i.e. the step is not a PR body acquisition
    step at all)."""


class AcquisitionParityError(Exception):
    """Raised by `check_acquisition_parity` when the producer/consumer
    acquisition specs diverge on any tracked invariant."""


@dataclass(frozen=True)
class AcquisitionSpec:
    endpoint_pattern: Optional[str]
    jq_filter: Optional[str]
    direct_persist: Optional[bool]
    uses_event_snapshot: bool


def _normalize_endpoint(endpoint: str) -> str:
    """Collapse `${ANY_VAR_NAME}` interpolations to a generic `{var}`
    token so local variable naming (`${GITHUB_REPOSITORY}` vs `${REPO}`,
    `${PR_NUMBER}`, etc.) never affects parity -- only the REST path
    SHAPE (`repos/{var}/pulls/{var}`) does.
    """
    return _VAR_TOKEN_RE.sub("{var}", endpoint)


def _find_body_acquisition_line(run_text: str) -> Optional[str]:
    """Return the single `run:` line performing a `pulls/{num}` fetch
    filtered on `.body`, ignoring unrelated `gh api`/`gh_api` calls that
    may appear in the same step (e.g. `.head.sha`, `.base.sha`,
    `.number`)."""
    for raw_line in run_text.splitlines():
        line = raw_line.strip()
        if "pulls/" in line and "--jq" in line and ".body" in line:
            return line
    return None


def extract_acquisition_spec(step: dict) -> AcquisitionSpec:
    """Structurally extract the PR body acquisition semantics of a single
    GitHub Actions workflow step (already parsed via `yaml.safe_load()`).

    Raises `AcquisitionSpecError` if the step is not a PR body
    acquisition step at all (no live REST line and no event-snapshot
    marker).
    """
    run_text = step.get("run") or ""
    env = step.get("env") or {}
    combined_text = run_text + "\n" + "\n".join(str(v) for v in env.values())
    uses_event_snapshot = _EVENT_SNAPSHOT_MARKER in combined_text

    acquisition_line = _find_body_acquisition_line(run_text)
    if acquisition_line is None:
        if uses_event_snapshot:
            # Reverted to the event-time snapshot: there is no live REST
            # acquisition line to extract endpoint/jq/persistence from.
            return AcquisitionSpec(
                endpoint_pattern=None,
                jq_filter=None,
                direct_persist=None,
                uses_event_snapshot=True,
            )
        raise AcquisitionSpecError(
            "no live PR body REST acquisition line found in step run text"
        )

    direct_match = _DIRECT_ACQUISITION_RE.match(acquisition_line)
    if direct_match:
        return AcquisitionSpec(
            endpoint_pattern=_normalize_endpoint(direct_match.group("endpoint")),
            jq_filter=direct_match.group("jq"),
            direct_persist=True,
            uses_event_snapshot=uses_event_snapshot,
        )

    substitution_match = _SUBSTITUTION_ACQUISITION_RE.match(acquisition_line)
    if substitution_match:
        return AcquisitionSpec(
            endpoint_pattern=_normalize_endpoint(substitution_match.group("endpoint")),
            jq_filter=substitution_match.group("jq"),
            direct_persist=False,
            uses_event_snapshot=uses_event_snapshot,
        )

    raise AcquisitionSpecError(
        f"unrecognized PR body acquisition line shape: {acquisition_line!r}"
    )


def check_acquisition_parity(producer: AcquisitionSpec, consumer: AcquisitionSpec) -> None:
    """Raise `AcquisitionParityError` unless producer and consumer share
    the same live acquisition semantics (endpoint pattern, jq filter,
    direct-persistence) and the producer does not rely on the event-time
    snapshot."""
    errors: list[str] = []

    if producer.uses_event_snapshot:
        errors.append(
            "producer uses github.event.pull_request.body event-time "
            "snapshot instead of a live REST fetch"
        )
    if (
        producer.endpoint_pattern is None
        or producer.jq_filter is None
        or producer.direct_persist is None
    ):
        errors.append("producer has no complete live REST acquisition spec")
    if (
        consumer.endpoint_pattern is None
        or consumer.jq_filter is None
        or consumer.direct_persist is None
    ):
        errors.append("consumer has no complete live REST acquisition spec")

    if not errors:
        if producer.endpoint_pattern != consumer.endpoint_pattern:
            errors.append(
                "endpoint pattern mismatch: "
                f"producer={producer.endpoint_pattern!r} "
                f"consumer={consumer.endpoint_pattern!r}"
            )
        if producer.jq_filter != consumer.jq_filter:
            errors.append(
                "jq filter mismatch: "
                f"producer={producer.jq_filter!r} consumer={consumer.jq_filter!r}"
            )
        if producer.direct_persist != consumer.direct_persist:
            errors.append(
                "direct-persistence mismatch: "
                f"producer direct_persist={producer.direct_persist!r} "
                f"consumer direct_persist={consumer.direct_persist!r}"
            )

    if errors:
        raise AcquisitionParityError("; ".join(errors))


def _iter_run_steps(workflow_doc: dict):
    for job in (workflow_doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "run" in step:
                yield step


def _find_unique_pr_body_acquisition_step(workflow_doc: dict) -> dict:
    """Find the single step across all jobs whose `run:` performs a
    `pulls/{num}` fetch filtered on `.body`."""
    candidates = [
        step
        for step in _iter_run_steps(workflow_doc)
        if _find_body_acquisition_line(step.get("run") or "") is not None
    ]
    assert len(candidates) == 1, (
        "expected exactly one PR body acquisition step, found "
        f"{len(candidates)}"
    )
    return candidates[0]


def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC1: positive parity test against the live workflow files
# ---------------------------------------------------------------------------


def test_live_pr_body_acquisition_parity():
    """GIVEN the live `ci.yml` producer and
    `visual-impact-trusted-consumer.yml` trusted consumer workflows, WHEN
    their respective PR body acquisition `run:` steps are structurally
    parsed, THEN both share the same REST endpoint pattern, the same
    `.body // ""` jq filter, both persist stdout directly to a file with
    no canonicalization-changing intermediate, and the producer does not
    use the `github.event.pull_request.body` event-time snapshot."""
    ci_doc = _load_workflow(CI_WORKFLOW_PATH)
    consumer_doc = _load_workflow(CONSUMER_WORKFLOW_PATH)

    producer_step = _find_unique_pr_body_acquisition_step(ci_doc)
    consumer_step = _find_unique_pr_body_acquisition_step(consumer_doc)

    producer_spec = extract_acquisition_spec(producer_step)
    consumer_spec = extract_acquisition_spec(consumer_step)

    # Must not raise: the live workflows are expected to already be in parity.
    check_acquisition_parity(producer_spec, consumer_spec)

    assert producer_spec.endpoint_pattern == "repos/{var}/pulls/{var}"
    assert consumer_spec.endpoint_pattern == "repos/{var}/pulls/{var}"
    assert producer_spec.jq_filter == '.body // ""'
    assert consumer_spec.jq_filter == '.body // ""'
    assert producer_spec.direct_persist is True
    assert consumer_spec.direct_persist is True
    assert producer_spec.uses_event_snapshot is False
    assert consumer_spec.uses_event_snapshot is False


# ---------------------------------------------------------------------------
# AC2: negative drift-rejection test with synthetic fixtures
# ---------------------------------------------------------------------------

_BASELINE_CONSUMER_STEP = {
    "run": (
        'gh_api "repos/${REPO}/pulls/${PR_NUMBER}" --jq \'.body // ""\' '
        "> artifacts/trusted/pr_body.txt"
    ),
}

_DRIFT_PRODUCER_STEPS = {
    "producer_reverts_to_event_snapshot": {
        "env": {"PR_BODY": "${{ github.event.pull_request.body }}"},
        "run": 'printf "%s" "$PR_BODY" > pr_body.md',
    },
    "producer_inserts_canonicalization_changing_intermediate": {
        "run": (
            'PR_BODY="$(gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"\"')\"\n"
            'printf "%s" "$PR_BODY" > pr_body.md'
        ),
    },
    "producer_jq_filter_diverges": {
        "run": (
            'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body' > pr_body.md"
        ),
    },
}


@pytest.mark.parametrize(
    "drift_case", sorted(_DRIFT_PRODUCER_STEPS), ids=sorted(_DRIFT_PRODUCER_STEPS)
)
def test_acquisition_parity_rejects_semantic_drift(drift_case):
    """GIVEN a synthetic producer step that drifts from the trusted
    consumer's baseline acquisition semantics in exactly one of three
    ways (event-snapshot regression, a canonicalization-changing
    intermediate inserted on only one side, or a diverging jq filter),
    WHEN the AC1 extractor/parity checker is applied, THEN it rejects the
    drift (raises `AcquisitionSpecError` or `AcquisitionParityError`)
    instead of silently accepting it. This test itself asserts the
    rejection happens and therefore PASSes (exit 0)."""
    drifted_producer_step = _DRIFT_PRODUCER_STEPS[drift_case]
    consumer_spec = extract_acquisition_spec(_BASELINE_CONSUMER_STEP)

    with pytest.raises((AcquisitionSpecError, AcquisitionParityError)):
        producer_spec = extract_acquisition_spec(drifted_producer_step)
        check_acquisition_parity(producer_spec, consumer_spec)
