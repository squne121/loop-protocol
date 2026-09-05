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

PR #2503 review fix_delta (issuecomment-5550095642) additions:

- The extractor now REJECTS two additional non-transparent-persistence
  shapes that were previously silently accepted as `direct_persist=True`:
  a named wrapper function (e.g. `gh_api`) whose OWN body captures `gh
  api`'s stdout via command substitution before re-emitting it (which
  silently strips trailing newlines), and a legitimate direct acquisition
  line followed by a LATER rewrite of the same output file (e.g. a
  command-substitution readback + `printf`), which the extractor
  previously never looked past the first matched line to see.
- The extractor now ACCEPTS three purely formatting/meaning-preserving
  variations of the direct acquisition line that the prior literal
  line-based regex rejected: a backslash line-continuation split across
  two physical lines, a trailing `# comment` after the redirect, and
  extra whitespace between `gh` and `api`. Two small "scope lock" tests
  confirm this tolerance does not turn into a general comment-stripping
  rule or a fuzzy caller-name match.
- `test_acquisition_parity_accepts_formatting_only_variants`
  (AC3, positive) exercises these formatting variants through the SAME
  `extract_acquisition_spec`/`check_acquisition_parity` functions AC1
  uses.
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
# [, optionally followed by a trailing `# comment`]. `gh\s+api` (rather
# than a literal single space) tolerates extra inter-token whitespace
# (PR #2503 review fix_delta P2) without opening up a fuzzy match on
# near-miss caller tokens (e.g. `gh_api_v2`) -- the `^`-anchored
# alternation only matches the two known exact caller spellings.
_DIRECT_ACQUISITION_RE = re.compile(
    r'^(?P<caller>gh_api|gh\s+api)\s+"(?P<endpoint>repos/\$\{[A-Za-z0-9_]+\}/pulls/\$\{[A-Za-z0-9_]+\})"'
    r"\s+--jq\s+'(?P<jq>[^']*)'\s*>\s*(?P<outfile>\S+)"
    r"(?:\s+#.*)?$"
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


# Matches a POSIX shell function DEFINITION line, e.g. `gh_api() { ... }`.
# Used to exclude a wrapper's own definition (which may itself mention
# `pulls/`/`--jq`/`.body` as part of a captured example call inside its
# body) from being mistaken for the actual acquisition call SITE.
_FUNCTION_DEF_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{")

# A POSIX shell backslash-newline line continuation: joins a purely
# formatting-only split of one logical command across two physical lines
# (PR #2503 review fix_delta P2) into a single line before any
# line-based search/matching is done.
_LINE_CONTINUATION_RE = re.compile(r"\\\n[ \t]*")


def _join_line_continuations(text: str) -> str:
    """Collapse `\\<newline>` shell line continuations into a single
    space so a purely formatting-only split of one logical command across
    two physical lines is treated identically to the single-line form."""
    return _LINE_CONTINUATION_RE.sub(" ", text)


def _find_body_acquisition_line(run_text: str) -> Optional[str]:
    """Return the single `run:` line performing a `pulls/{num}` fetch
    filtered on `.body`, ignoring unrelated `gh api`/`gh_api` calls that
    may appear in the same step (e.g. `.head.sha`, `.base.sha`,
    `.number`) and ignoring shell FUNCTION DEFINITION lines (e.g. a
    `gh_api() { ... }` wrapper) -- only the actual acquisition call SITE
    is a candidate line."""
    for raw_line in run_text.splitlines():
        line = raw_line.strip()
        if _FUNCTION_DEF_LINE_RE.match(line):
            continue
        if "pulls/" in line and "--jq" in line and ".body" in line:
            return line
    return None


def _find_function_definition(run_text: str, name: str) -> Optional[str]:
    """Return the body text of a POSIX shell function definition
    `name() { <body> }` within `run_text` (a single non-nested brace pair
    -- this is intentionally not a general Bash parser), or `None` if
    `name` is never defined as a function in this step's `run:` text."""
    pattern = re.compile(re.escape(name) + r"\s*\(\)\s*\{(.*?)\}", re.DOTALL)
    match = pattern.search(run_text)
    return match.group(1) if match else None


# A wrapper function body binds `gh api`'s stdout to a shell variable via
# command substitution (`VAR="$(...)"`) before it can re-emit it -- that
# round trip silently strips trailing newlines (PR #2503 review fix_delta
# P1, measured actual output bytes: producer direct save = `b"body\n\n"`,
# transparent wrapper = `b"body\n\n"`, capture+`printf` wrapper =
# `b"body"`).
_WRAPPER_CAPTURES_VIA_SUBSTITUTION_RE = re.compile(r'=\s*"?\$\(')


def _is_transparent_wrapper_body(body: str) -> bool:
    """A wrapper function forwards `gh api`'s stdout bytes UNCHANGED only
    if it never binds the call's output to a shell variable via command
    substitution before re-emitting it. Directly exec'ing `gh api ...
    "$@"` as the function's tail call is transparent; capturing then
    re-printing (even via `printf '%s'`) is not."""
    return _WRAPPER_CAPTURES_VIA_SUBSTITUTION_RE.search(body) is None


def _has_subsequent_rewrite_of_outfile(
    run_text: str, acquisition_line: str, outfile: str
) -> bool:
    """Return True if a LATER line in the same step's `run:` text writes
    to the same `outfile` again (e.g. a command-substitution readback of
    the just-acquired file followed by a `printf`/`echo` rewrite) -- such
    a round trip can silently alter the persisted bytes the same way a
    non-transparent wrapper can (PR #2503 review fix_delta P1), and the
    prior extractor never looked past the first matched acquisition
    line."""
    lines = run_text.splitlines()
    try:
        acquisition_index = next(
            i for i, raw in enumerate(lines) if raw.strip() == acquisition_line
        )
    except StopIteration:
        return False
    redirect_re = re.compile(r">{1,2}\s*" + re.escape(outfile) + r"(?:\s|$)")
    return any(
        redirect_re.search(raw.strip()) for raw in lines[acquisition_index + 1 :]
    )


def extract_acquisition_spec(step: dict) -> AcquisitionSpec:
    """Structurally extract the PR body acquisition semantics of a single
    GitHub Actions workflow step (already parsed via `yaml.safe_load()`).

    Raises `AcquisitionSpecError` if the step is not a PR body
    acquisition step at all (no live REST line and no event-snapshot
    marker).
    """
    run_text = _join_line_continuations(step.get("run") or "")
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
        caller = direct_match.group("caller")
        outfile = direct_match.group("outfile")
        if caller == "gh_api":
            wrapper_body = _find_function_definition(run_text, "gh_api")
            if wrapper_body is not None and not _is_transparent_wrapper_body(
                wrapper_body
            ):
                raise AcquisitionSpecError(
                    "gh_api wrapper function does not transparently forward "
                    "stdout untouched (captures the call's output via "
                    "command substitution before re-emitting it), which "
                    "can silently alter the persisted bytes"
                )
        if _has_subsequent_rewrite_of_outfile(run_text, acquisition_line, outfile):
            raise AcquisitionSpecError(
                f"output file {outfile!r} is rewritten by a later line in "
                "the same step after the direct acquisition line, which "
                "can silently alter the persisted bytes"
            )
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
    `pulls/{num}` fetch filtered on `.body`.

    PR #2503 review fix_delta P2 (non-blocking, left as-is): this scans
    ALL jobs in the workflow, so an unrelated audit-like job that also
    happens to save PR body content to a different file would trip the
    "exactly one" assertion even though the actual producer/consumer
    relationship is unaffected. Narrowing this to "the jobs already
    identified as producer/consumer" would require a job-classification
    abstraction (recognizing which job(s) are the relevant ones without
    hardcoding job names/ids, which would reintroduce the very
    wrapper/variable-naming coupling this module's docstring says to
    avoid) -- out of scope for this narrow fix_delta.
    """
    candidates = [
        step
        for step in _iter_run_steps(workflow_doc)
        if _find_body_acquisition_line(_join_line_continuations(step.get("run") or ""))
        is not None
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
    # PR #2503 review fix_delta P1(a): a named wrapper (`gh_api`) whose
    # OWN body captures `gh api`'s stdout via command substitution before
    # re-emitting it via `printf` is NOT a transparent passthrough --
    # `response="$(...)"` strips the trailing newline the same way a
    # bare `VAR="$(...)"` acquisition-line intermediate does, even though
    # the call SITE itself matches the literal direct-acquisition shape.
    "producer_wrapper_captures_and_reemits_stdout": {
        "run": (
            'gh_api() { local response; response="$(gh api '
            '"repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"\"')\"; printf '%s' \"$response\"; }\n"
            'gh_api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"\"' > pr_body.md"
        ),
    },
    # PR #2503 review fix_delta P1(b): a legitimate direct acquisition
    # line followed by a LATER command-substitution readback + `printf`
    # rewrite of the SAME output file also silently strips the trailing
    # newline. The prior extractor never looked past the first matched
    # line, so this rewrite was invisible to it.
    "producer_rewrites_output_file_after_acquisition": {
        "run": (
            'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"\"' > pr_body.md\n"
            'PR_BODY="$(cat pr_body.md)"\n'
            'printf "%s" "$PR_BODY" > pr_body.md'
        ),
    },
}


@pytest.mark.parametrize(
    "drift_case", sorted(_DRIFT_PRODUCER_STEPS), ids=sorted(_DRIFT_PRODUCER_STEPS)
)
def test_acquisition_parity_rejects_semantic_drift(drift_case):
    """GIVEN a synthetic producer step that drifts from the trusted
    consumer's baseline acquisition semantics in exactly one of several
    ways (event-snapshot regression, a canonicalization-changing
    intermediate inserted on only one side, a diverging jq filter, a
    named wrapper that captures-and-re-emits stdout non-transparently, or
    a later rewrite of the already-acquired output file), WHEN the AC1
    extractor/parity checker is applied, THEN it rejects the drift
    (raises `AcquisitionSpecError` or `AcquisitionParityError`) instead
    of silently accepting it. This test itself asserts the rejection
    happens and therefore PASSes (exit 0)."""
    drifted_producer_step = _DRIFT_PRODUCER_STEPS[drift_case]
    consumer_spec = extract_acquisition_spec(_BASELINE_CONSUMER_STEP)

    with pytest.raises((AcquisitionSpecError, AcquisitionParityError)):
        producer_spec = extract_acquisition_spec(drifted_producer_step)
        check_acquisition_parity(producer_spec, consumer_spec)


# ---------------------------------------------------------------------------
# AC3: formatting-only variants must still be ACCEPTED (no false positives)
# ---------------------------------------------------------------------------

_FORMATTING_VARIANT_PRODUCER_STEPS = {
    "line_continuation_split_across_two_physical_lines": {
        "run": (
            'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" \\\n'
            "  --jq '.body // \"\"' > pr_body.md"
        ),
    },
    "trailing_comment_after_redirect": {
        "run": (
            'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"\"' > pr_body.md  # persist body for later steps"
        ),
    },
    "extra_whitespace_between_gh_and_api": {
        "run": (
            'gh   api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"\"' > pr_body.md"
        ),
    },
}


@pytest.mark.parametrize(
    "variant_case",
    sorted(_FORMATTING_VARIANT_PRODUCER_STEPS),
    ids=sorted(_FORMATTING_VARIANT_PRODUCER_STEPS),
)
def test_acquisition_parity_accepts_formatting_only_variants(variant_case):
    """GIVEN a producer step whose acquisition command is a pure
    formatting variation of the baseline (a backslash line-continuation
    split across two physical lines, a trailing `# comment` after the
    redirect, or extra whitespace between `gh` and `api`), WHEN parsed
    through the SAME extractor/parity-checker functions AC1 uses, THEN it
    is accepted (no exception) and yields the exact same endpoint
    pattern, jq filter, and direct-persistence as the baseline -- a
    formatting-only difference must never be mistaken for semantic
    drift."""
    variant_producer_step = _FORMATTING_VARIANT_PRODUCER_STEPS[variant_case]
    consumer_spec = extract_acquisition_spec(_BASELINE_CONSUMER_STEP)
    producer_spec = extract_acquisition_spec(variant_producer_step)

    # Must not raise: formatting-only differences are not semantic drift.
    check_acquisition_parity(producer_spec, consumer_spec)

    assert producer_spec.endpoint_pattern == "repos/{var}/pulls/{var}"
    assert consumer_spec.endpoint_pattern == "repos/{var}/pulls/{var}"
    assert producer_spec.jq_filter == '.body // ""'
    assert producer_spec.direct_persist is True
    assert producer_spec.uses_event_snapshot is False


# ---------------------------------------------------------------------------
# Scope-lock tests: the P2 formatting tolerance must stay narrow and must
# never become a general comment-stripping rule or a fuzzy caller match.
# ---------------------------------------------------------------------------


def test_direct_acquisition_regex_preserves_hash_inside_jq_filter():
    """GIVEN a producer acquisition line with a legitimate `#` character
    INSIDE the single-quoted jq filter, followed by a genuine trailing
    shell comment after the redirect, WHEN parsed, THEN the jq filter is
    preserved verbatim (the `#` inside the quotes is never treated as a
    comment start) -- only the text after the outfile token is treated
    as a comment. This locks the P2 comment-tolerance fix to the narrow
    post-outfile position instead of an unconditional `#`-strips-
    everything rule that could silently alter a jq filter containing
    `#`."""
    step = {
        "run": (
            'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"#fallback\"' > pr_body.md  # trailing comment"
        ),
    }
    spec = extract_acquisition_spec(step)
    assert spec.jq_filter == '.body // "#fallback"'
    assert spec.direct_persist is True


def test_direct_acquisition_regex_rejects_near_miss_caller_tokens():
    """GIVEN a call-site token that merely CONTAINS `gh_api` as a
    substring (e.g. a differently-named wrapper `gh_api_v2`) rather than
    being an exact `gh_api`/`gh api` call, WHEN parsed, THEN it is NOT
    matched as a recognized direct acquisition line -- locking the P2
    whitespace-tolerance fix (`gh\\s+api`) to the two known caller
    spellings only, never an open-ended fuzzy match."""
    step = {
        "run": (
            'gh_api_v2 "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" '
            "--jq '.body // \"\"' > pr_body.md"
        ),
    }
    with pytest.raises(AcquisitionSpecError):
        extract_acquisition_spec(step)
