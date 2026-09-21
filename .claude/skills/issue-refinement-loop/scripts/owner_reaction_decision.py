#!/usr/bin/env python3
"""
owner_reaction_decision.py

Deterministic, read-only owner-reaction reader / stable-user-id principal
resolver / drift checker / selection resolver (Issue #1975, parent #1950).

This CLI materializes steps 2-6 of the owner reaction procedure documented
in `.claude/skills/issue-refinement-loop/references/anchor-comment-handling.md`
("競合（material conflict）発生時の owner reaction 手順（#1950 AC3/AC4）") as a
working, deterministic program:

  2. Fetch ALL pages of GitHub reactions on the target comment.
  3. Resolve the reacting user's STABLE GitHub user ``id`` (not ``login``)
     as principal -- only reactions whose id matches the repository's
     AUTHORITATIVE owner stable id (resolved from ``GET repos/{repo}``,
     never a caller-supplied id trusted on its own -- PR #2683 fix_delta
     P2-1) count as owner reactions.
  4. Detect drift/staleness against a fixed preview binding (target
     comment body hash / anchor comment body hash / Issue body snapshot
     hash), via a SINGLE readback performed AFTER reaction retrieval
     completes (PR #2683 fix_delta P1-2: closes a TOCTOU window that an
     earlier readback-before-fetch ordering could not detect), and cross-
     checks that both the target comment and the anchor comment actually
     belong to the given Issue (PR #2683 fix_delta P1-3).
  5. Exclude untrusted (non-owner) and unmapped reactions from the
     decision.
  6. Determine selection semantics: unanswered / reject-all (``-1`` only) /
     selected / conflict / no-selection (unmapped-only) / stale /
     environment (fetch/pagination/identity) failure.

Out of Scope (see Issue #1975 "Out of Scope" for the authoritative list):
this CLI never performs any GitHub mutation, never promotes ``selected`` to
``approved_by_trusted_anchor``, and never itself wires into the existing
heavy mutation gate (``_classify_heavy_mutation_gate()`` /
``_is_approved_close_not_planned_decision()`` in
``run_refinement_preflight.py``). Its output is a structured decision only
-- the caller decides what, if anything, to do with it.

Issue #2689 (parent #1950) implements exactly that consumption on the
``run_refinement_preflight.py`` side, for the ``not_planned`` mutation
category only: immediately before evaluating its heavy mutation gate,
``run_refinement_preflight.py`` issues a FRESH subprocess execution of this
very CLI (same ``--repo`` / ``--issue-number`` / ``--owner-user-id`` /
``--preview-binding-file`` argument shape as the ``owner_reaction.decide``
registry entry) and feeds the parsed result into its own
``_is_approved_owner_reaction_not_planned_decision()`` predicate -- an
INDEPENDENT predicate from ``_is_approved_close_not_planned_decision()``
that never converts ``selected`` into ``approved_by_trusted_anchor``. This
module remains entirely unaware of, and unmodified for, that consumer: its
own selection semantics, reaction-reading logic, and Out of Scope boundary
above are unchanged by #2689.

Internal GitHub reads use ``gh api -X GET`` subprocess calls only (argv
arrays, never a shell string). Reaction pagination uses
``gh api -X GET --paginate --slurp ...`` -- this CLI does not reimplement
`gh`'s own ``Link: rel="next"`` pagination. A single `decide()` run issues
up to 5 SEQUENTIAL `gh` calls (repo owner resolution, reactions, target
comment readback, anchor comment readback, Issue readback) -- see
`DEFAULT_GH_TIMEOUT` for the per-call timeout budget this implies.

"Full retrieval success" (the only condition under which a selection may be
returned) requires ALL of:
  - the `gh` subprocess exits 0
  - the full ``--paginate --slurp`` response parses as JSON
  - every page has the expected shape (a JSON array)
  - every reaction record passes structural integrity validation
    (``id``/``user.id``/``content`` present and well-typed)
A nonzero subprocess exit is fail-closed REGARDLESS of whether stdout
happens to contain parseable partial JSON (Issue #1975 AC3): this module
never attempts to parse stdout unless the subprocess exited 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "OWNER_REACTION_DECISION_RESULT_V1"

# PR #2683 fix_delta (P2-3, OWNER adversarial review comment #5748651887):
# `decide()` now issues up to 5 SEQUENTIAL `gh` subprocess calls (repo
# owner resolution, reactions pagination, target comment readback, anchor
# comment readback, Issue readback -- see the "Orchestration" section
# below). At the previous 30s-per-call default, a worst-case chain could
# exceed the `command_registry.py` `owner_reaction.decide*` outer
# `timeout_seconds` budget before this module could ever produce a
# structured `environment_error`, causing an ungraceful outer kill instead.
# Lowered to a per-call budget that leaves comfortable margin under the
# widened outer budget (see `command_registry.py` for the corresponding
# `timeout_seconds` bump).
DEFAULT_GH_TIMEOUT = 12.0

# ---------------------------------------------------------------------------
# Known GitHub reaction content values (fixed enum, GitHub REST API).
# ---------------------------------------------------------------------------

KNOWN_REACTION_CONTENTS: frozenset[str] = frozenset(
    {"+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"}
)

# "-1" is a reserved sentinel meaning "reject all proposed options, a
# reproposal is needed" -- it is never a key a producer may remap in
# `reaction_option_map` (Issue #1975 In Scope: selection semantics).
REJECT_ALL_CONTENT = "-1"

# ---------------------------------------------------------------------------
# Top-level status / reason_code enums (Issue #1975 In Scope: selection
# semantics -- a small closed set of top-level statuses; finer-grained
# "why not selected" distinctions live under `reason_code`, never as new
# top-level status values).
# ---------------------------------------------------------------------------

STATUS_SELECTED = "selected"
STATUS_UNRESOLVED = "unresolved"
STATUS_STALE = "stale"
STATUS_ENVIRONMENT_ERROR = "environment_error"

REASON_UNANSWERED = "unanswered"
REASON_REJECT_ALL = "reject_all"
REASON_CONFLICT = "conflict"
REASON_NO_SELECTION = "no_selection"
REASON_PREVIEW_DRIFTED = "preview_drifted"

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_REQUIRED_PREVIEW_BINDING_KEYS = frozenset(
    {
        "comment_id",
        "comment_body_hash",
        "issue_snapshot_hash",
        "reaction_option_map",
        "options",
        # PR #2683 fix_delta P1-1 (OWNER adversarial review): the anchor
        # comment that carries the material-conflict options themselves
        # must ALSO be bound and read back -- without this, an edit to the
        # anchor comment (the options text the owner is reacting to) could
        # silently invalidate a selection that still looks internally
        # consistent.
        "anchor_comment_id",
        "anchor_comment_body_hash",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OwnerReactionDecisionError(Exception):
    """Base class for every fail-closed condition this module raises."""


class PreviewBindingInvalid(OwnerReactionDecisionError):
    """The preview binding input does not satisfy the fixed AC1 contract
    (missing/malformed keys, unknown reaction content key, dangling
    option_id reference, etc.)."""


class GhFetchFailed(OwnerReactionDecisionError):
    """A `gh api` subprocess call failed to produce usable JSON: nonzero
    exit (regardless of any partial stdout content -- Issue #1975 AC3(a)/
    (b)), a JSON decode failure, or an unexpected top-level shape."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)
        self.reason_code = reason_code
        self.detail = detail


class ReactionPageShapeInvalid(OwnerReactionDecisionError):
    """All pages were fetched (subprocess exit 0, valid JSON) but at least
    one page's response shape was not a JSON array (Issue #1975 AC3(c))."""


class ReactionRecordIntegrityFailure(OwnerReactionDecisionError):
    """A reaction record failed structural integrity validation: missing
    or malformed `id` / `user.id` / `content` (Issue #1975 AC3(d))."""


# ---------------------------------------------------------------------------
# gh subprocess transport (swappable: production vs. fixture-backed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GhInvocationResult:
    returncode: int
    stdout: str
    stderr: str


GhRunner = Callable[..., GhInvocationResult]


def default_gh_runner(argv: list[str], *, timeout: float) -> GhInvocationResult:
    """Real `gh` subprocess runner -- shell=False, argv array only."""
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return GhInvocationResult(returncode=127, stdout="", stderr="gh_not_found")
    except subprocess.TimeoutExpired:
        return GhInvocationResult(returncode=124, stdout="", stderr=f"gh_timeout_after_{timeout}s")
    return GhInvocationResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _classify_gh_call(argv: list[str], *, anchor_comment_id: int | None = None) -> str:
    """Classify which of the network calls `argv` represents, purely from
    the rendered endpoint token -- used by the fixture runner only
    (test/AC7 path). Production `default_gh_runner` never calls this.

    `anchor_comment_id` disambiguates the anchor comment readback from the
    target comment readback (PR #2683 fix_delta P1-1) -- both use the same
    `/issues/comments/<id>` endpoint shape, differing only in which id is
    rendered. When `anchor_comment_id` is not supplied (or does not match),
    any `/issues/comments/<id>` call classifies as `"comment"`.
    """
    endpoint = next((tok for tok in argv if tok.startswith("repos/")), "")
    if "/reactions" in endpoint:
        return "reactions"
    comment_match = re.search(r"/issues/comments/(\d+)(\?.*)?$", endpoint)
    if comment_match:
        if anchor_comment_id is not None and int(comment_match.group(1)) == anchor_comment_id:
            return "anchor"
        return "comment"
    if re.search(r"/issues/\d+(\?.*)?$", endpoint):
        return "issue"
    if re.fullmatch(r"repos/[^/]+/[^/]+", endpoint):
        # PR #2683 fix_delta P2-1: `GET repos/{repo}` -- resolves the
        # authoritative repository owner stable id.
        return "repo"
    return "unknown"


def make_fixture_gh_runner(fixture_path: Path) -> GhRunner:
    """Build a `GhRunner` backed entirely by a canned JSON fixture file
    (Issue #1975 AC7: fakes ONLY the GitHub network boundary -- CLI
    argument parsing, preview binding validation, hashing, pagination
    flattening, integrity validation, and selection logic all still run
    for real). Mirrors the established `run_refinement_preflight.py
    --fixture` precedent in this skill.

    Fixture shape::

        {
          "repo":              {"returncode": 0, "stdout": "<json text>", "stderr": ""},
          "comment":           {"returncode": 0, "stdout": "<json text>", "stderr": ""},
          "anchor":            {"returncode": 0, "stdout": "<json text>", "stderr": ""},
          "issue":             {"returncode": 0, "stdout": "<json text>", "stderr": ""},
          "reactions":         {"returncode": 0, "stdout": "<json text>", "stderr": ""},
          "anchor_comment_id": 556
        }

    `anchor_comment_id` (PR #2683 fix_delta P1-1) is routing metadata ONLY
    -- it is never itself part of any faked `gh` response -- used to tell
    the anchor comment readback call apart from the target comment
    readback call (see `_classify_gh_call`).
    """
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    anchor_comment_id = fixture.get("anchor_comment_id")

    def _runner(argv: list[str], *, timeout: float) -> GhInvocationResult:  # noqa: ARG001
        kind = _classify_gh_call(argv, anchor_comment_id=anchor_comment_id)
        entry = fixture.get(kind)
        if not isinstance(entry, dict):
            return GhInvocationResult(returncode=1, stdout="", stderr=f"no_fixture_for_kind:{kind}")
        return GhInvocationResult(
            returncode=int(entry.get("returncode", 0)),
            stdout=str(entry.get("stdout", "")),
            stderr=str(entry.get("stderr", "")),
        )

    return _runner


# ---------------------------------------------------------------------------
# argv builders (gh api -X GET, explicit; --paginate --slurp for reactions)
# ---------------------------------------------------------------------------


def build_gh_argv_repo(repo: str) -> list[str]:
    # PR #2683 fix_delta P2-1: resolves the repository's authoritative
    # owner stable id (`owner.id`) -- never trusts a caller-supplied
    # `--owner-user-id` on its own.
    return ["gh", "api", "-X", "GET", f"repos/{repo}"]


def build_gh_argv_comment(repo: str, comment_id: int) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{repo}/issues/comments/{comment_id}"]


def build_gh_argv_issue(repo: str, issue_number: int) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{repo}/issues/{issue_number}"]


def build_gh_argv_reactions(repo: str, comment_id: int) -> list[str]:
    # `gh api --paginate --slurp` (gh 2.88.1+): wraps EVERY page as its own
    # array element, even for a single page, so the top-level parsed JSON
    # is always list-of-lists -- see `_validate_reaction_page_shape()`.
    return [
        "gh", "api", "-X", "GET", "--paginate", "--slurp",
        f"repos/{repo}/issues/comments/{comment_id}/reactions?per_page=100",
    ]


# ---------------------------------------------------------------------------
# gh JSON fetch (fail-closed on nonzero exit BEFORE ever touching stdout)
# ---------------------------------------------------------------------------


def run_gh_json(argv: list[str], gh_runner: GhRunner, timeout: float) -> Any:
    result = gh_runner(argv, timeout=timeout)
    if result.returncode != 0:
        # Issue #1975 AC3(a)/(b): fail-closed on nonzero exit REGARDLESS of
        # whether stdout contains parseable partial JSON -- stdout is never
        # inspected below this branch.
        raise GhFetchFailed(
            "subprocess_nonzero_exit",
            f"exit={result.returncode} stderr={(result.stderr or '')[:300]}",
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhFetchFailed("gh_json_decode_error", str(exc)) from exc


def fetch_json_object(argv: list[str], gh_runner: GhRunner, timeout: float) -> dict:
    parsed = run_gh_json(argv, gh_runner, timeout)
    if not isinstance(parsed, dict):
        raise GhFetchFailed("unexpected_response_shape", f"expected_dict_got:{type(parsed).__name__}")
    return parsed


def _validate_reaction_page_shape(pages: Any) -> list[list[Any]]:
    if not isinstance(pages, list):
        raise ReactionPageShapeInvalid(f"top_level_not_list:{type(pages).__name__}")
    validated: list[list[Any]] = []
    for index, page in enumerate(pages):
        if not isinstance(page, list):
            raise ReactionPageShapeInvalid(f"page_{index}_not_list:{type(page).__name__}")
        validated.append(page)
    return validated


def _validate_reaction_record(record: Any) -> dict:
    if not isinstance(record, dict):
        raise ReactionRecordIntegrityFailure(f"record_not_dict:{type(record).__name__}")
    reaction_id = record.get("id")
    if not isinstance(reaction_id, int) or isinstance(reaction_id, bool):
        raise ReactionRecordIntegrityFailure("missing_or_invalid_id")
    content = record.get("content")
    if not isinstance(content, str) or content not in KNOWN_REACTION_CONTENTS:
        raise ReactionRecordIntegrityFailure(f"missing_or_invalid_content:{content!r}")
    user = record.get("user")
    if not isinstance(user, dict):
        raise ReactionRecordIntegrityFailure("missing_or_invalid_user")
    user_id = user.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ReactionRecordIntegrityFailure("missing_or_invalid_user_id")
    return record


def fetch_all_reactions(repo: str, comment_id: int, *, gh_runner: GhRunner, timeout: float) -> list[dict]:
    """Fetch + fully validate every reaction on `comment_id`. Raises on any
    of the 4 Issue #1975 AC3 fail-closed conditions; returns the flattened,
    per-record-validated reaction list only on full retrieval success."""
    argv = build_gh_argv_reactions(repo, comment_id)
    parsed = run_gh_json(argv, gh_runner, timeout)
    pages = _validate_reaction_page_shape(parsed)
    flattened: list[dict] = []
    for page in pages:
        for record in page:
            flattened.append(_validate_reaction_record(record))
    return flattened


# ---------------------------------------------------------------------------
# Principal resolver (stable user id, not login -- Issue #1975 AC2)
# ---------------------------------------------------------------------------


def resolve_owner_reaction_contents(reactions: list[dict], owner_user_id: int) -> list[str]:
    """Filter already-integrity-validated `reactions` down to the content
    values of reactions posted by the stable `owner_user_id` principal.
    Reactions from any other stable user id -- regardless of `login`, and
    regardless of volume -- are excluded unconditionally (Issue #1975
    AC2/AC5)."""
    contents: list[str] = []
    for record in reactions:
        if record["user"]["id"] == owner_user_id:
            contents.append(record["content"])
    return contents


# ---------------------------------------------------------------------------
# Selection semantics (Issue #1975 AC4)
# ---------------------------------------------------------------------------


def compute_selection(owner_reaction_contents: list[str], reaction_option_map: dict[str, str]) -> dict:
    """Disambiguate owner reaction content into exactly one of:
    unanswered / reject_all / selected / conflict / no_selection.

    `reaction_option_map` never contains the `-1` key (rejected up front by
    `validate_preview_binding()`), so `-1` is always treated as the
    reserved reject-all sentinel, never a mappable option.

    Design note (not an explicit AC4 sub-case, documented for clarity): if
    the owner reacts with BOTH `-1` and a mapped content (a genuinely mixed
    signal), this is classified as `conflict` rather than silently
    preferring either reading -- the same fail-closed "don't guess"
    posture AC4 requires for two mapped reaction types.
    """
    distinct = set(owner_reaction_contents)
    if not distinct:
        return {"status": STATUS_UNRESOLVED, "reason_code": REASON_UNANSWERED, "selected_option_id": None}

    mapped_hits = {content for content in distinct if content in reaction_option_map}
    valid_types = set(mapped_hits)
    if REJECT_ALL_CONTENT in distinct:
        valid_types.add(REJECT_ALL_CONTENT)

    if not valid_types:
        # Only unmapped, non-reject-all reactions -- never silently
        # converted into a selection (Issue #1975 In Scope / AC5).
        return {"status": STATUS_UNRESOLVED, "reason_code": REASON_NO_SELECTION, "selected_option_id": None}

    if valid_types == {REJECT_ALL_CONTENT}:
        return {"status": STATUS_UNRESOLVED, "reason_code": REASON_REJECT_ALL, "selected_option_id": None}

    if len(valid_types) > 1:
        return {"status": STATUS_UNRESOLVED, "reason_code": REASON_CONFLICT, "selected_option_id": None}

    (only_content,) = tuple(valid_types)
    return {
        "status": STATUS_SELECTED,
        "reason_code": None,
        "selected_option_id": reaction_option_map[only_content],
    }


# ---------------------------------------------------------------------------
# Preview binding validation (Issue #1975 AC1)
# ---------------------------------------------------------------------------


def validate_preview_binding(binding: Any) -> None:
    if not isinstance(binding, dict):
        raise PreviewBindingInvalid(f"not_a_dict:{type(binding).__name__}")

    missing = _REQUIRED_PREVIEW_BINDING_KEYS - set(binding.keys())
    if missing:
        raise PreviewBindingInvalid(f"missing_keys:{sorted(missing)}")

    comment_id = binding["comment_id"]
    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
        raise PreviewBindingInvalid("comment_id_invalid")

    anchor_comment_id = binding["anchor_comment_id"]
    if (
        not isinstance(anchor_comment_id, int)
        or isinstance(anchor_comment_id, bool)
        or anchor_comment_id <= 0
    ):
        raise PreviewBindingInvalid("anchor_comment_id_invalid")

    for hash_key in ("comment_body_hash", "issue_snapshot_hash", "anchor_comment_body_hash"):
        value = binding[hash_key]
        if not isinstance(value, str) or not _SHA256_HEX_RE.match(value):
            raise PreviewBindingInvalid(f"{hash_key}_invalid")

    reaction_option_map = binding["reaction_option_map"]
    if not isinstance(reaction_option_map, dict) or not reaction_option_map:
        raise PreviewBindingInvalid("reaction_option_map_invalid")
    for content, option_id in reaction_option_map.items():
        if content == REJECT_ALL_CONTENT or content not in KNOWN_REACTION_CONTENTS:
            raise PreviewBindingInvalid(f"reaction_option_map_key_invalid:{content!r}")
        if not isinstance(option_id, str) or not option_id:
            raise PreviewBindingInvalid(f"reaction_option_map_value_invalid_for:{content!r}")

    options = binding["options"]
    if not isinstance(options, dict) or not options:
        raise PreviewBindingInvalid("options_invalid")

    mapped_option_ids = set(reaction_option_map.values())
    missing_options = mapped_option_ids - set(options.keys())
    if missing_options:
        raise PreviewBindingInvalid(f"unmapped_option_ids_missing_from_options:{sorted(missing_options)}")

    for option_id, meta in options.items():
        if not isinstance(meta, dict):
            raise PreviewBindingInvalid(f"option_metadata_invalid:{option_id!r}")
        operation = meta.get("operation")
        target = meta.get("target")
        if not isinstance(operation, str) or not operation:
            raise PreviewBindingInvalid(f"option_operation_invalid:{option_id!r}")
        if not isinstance(target, str) or not target:
            raise PreviewBindingInvalid(f"option_target_invalid:{option_id!r}")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def _base_result(*, repo: str, issue_number: int, generated_at: str) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at,
        "repo": repo,
        "issue_number": issue_number,
        "preview_identity": {"comment_id": None, "comment_body_hash": None},
        "status": None,
        "reason_code": None,
        "selected_option_id": None,
        "selected_option_metadata": None,
        "owner_reaction_contents": [],
        "fetched_reaction_count": None,
        "drift": {
            "comment_body_drifted": None,
            "issue_body_drifted": None,
            "anchor_body_drifted": None,
        },
        "errors": [],
    }


def _environment_error_result(base: dict, *, reason_code: str, detail: str = "") -> dict:
    result = dict(base)
    result["status"] = STATUS_ENVIRONMENT_ERROR
    result["reason_code"] = reason_code
    if detail:
        result["errors"] = [detail]
    return result


def _stale_result(
    base: dict, *, comment_drifted: bool, issue_drifted: bool, anchor_drifted: bool
) -> dict:
    result = dict(base)
    result["status"] = STATUS_STALE
    result["reason_code"] = REASON_PREVIEW_DRIFTED
    result["drift"] = {
        "comment_body_drifted": comment_drifted,
        "issue_body_drifted": issue_drifted,
        "anchor_body_drifted": anchor_drifted,
    }
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def decide(
    *,
    repo: str,
    issue_number: int,
    owner_user_id: int,
    preview_binding: Any,
    gh_runner: GhRunner,
    timeout: float = DEFAULT_GH_TIMEOUT,
) -> dict:
    """Run the full owner-reaction decision pipeline. Always returns a
    well-formed `OWNER_REACTION_DECISION_RESULT_V1` dict -- never raises
    (every fail-closed condition is caught and mapped to
    `status: environment_error`)."""
    generated_at = _now_iso()
    base = _base_result(repo=repo, issue_number=issue_number, generated_at=generated_at)

    try:
        validate_preview_binding(preview_binding)
    except PreviewBindingInvalid as exc:
        return _environment_error_result(base, reason_code="preview_binding_invalid", detail=str(exc))

    comment_id = preview_binding["comment_id"]
    anchor_comment_id = preview_binding["anchor_comment_id"]
    base["preview_identity"] = {
        "comment_id": comment_id,
        "comment_body_hash": preview_binding["comment_body_hash"],
    }

    # PR #2683 fix_delta P2-1 (OWNER adversarial review): resolve the
    # AUTHORITATIVE repository owner stable id from `GET repos/{repo}` --
    # a caller-supplied `--owner-user-id` is never trusted on its own.
    try:
        repo_metadata = fetch_json_object(build_gh_argv_repo(repo), gh_runner, timeout)
    except GhFetchFailed as exc:
        return _environment_error_result(
            base, reason_code=f"repo_fetch_failed:{exc.reason_code}", detail=exc.detail
        )
    owner_metadata = repo_metadata.get("owner")
    resolved_owner_id = owner_metadata.get("id") if isinstance(owner_metadata, dict) else None
    if not isinstance(resolved_owner_id, int) or isinstance(resolved_owner_id, bool):
        return _environment_error_result(base, reason_code="repo_owner_shape_invalid")
    if resolved_owner_id != owner_user_id:
        return _environment_error_result(base, reason_code="owner_user_id_mismatch")

    # PR #2683 fix_delta P1-2 (TOCTOU, OWNER adversarial review): reactions
    # are fetched FIRST, and the drift/identity readback of the target
    # comment / anchor comment / Issue happens AFTER -- immediately before
    # a selection may be finalized below. This is the ONLY readback this
    # module performs; it deliberately supersedes the old
    # "readback-before-reactions" ordering, which could not detect an edit
    # that raced with (happened during, or immediately after) reaction
    # pagination. Minimizing to a single post-fetch readback also keeps
    # the total sequential `gh` call count bounded (see
    # `command_registry.py` timeout_seconds for the corresponding budget).
    try:
        reactions = fetch_all_reactions(repo, comment_id, gh_runner=gh_runner, timeout=timeout)
    except GhFetchFailed as exc:
        return _environment_error_result(
            base, reason_code=f"reactions_fetch_failed:{exc.reason_code}", detail=exc.detail
        )
    except ReactionPageShapeInvalid as exc:
        return _environment_error_result(base, reason_code="reaction_page_shape_invalid", detail=str(exc))
    except ReactionRecordIntegrityFailure as exc:
        return _environment_error_result(base, reason_code="reaction_record_integrity_failure", detail=str(exc))

    expected_issue_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"

    try:
        current_comment = fetch_json_object(build_gh_argv_comment(repo, comment_id), gh_runner, timeout)
    except GhFetchFailed as exc:
        return _environment_error_result(
            base, reason_code=f"comment_fetch_failed:{exc.reason_code}", detail=exc.detail
        )
    current_comment_body = current_comment.get("body")
    if not isinstance(current_comment_body, str):
        return _environment_error_result(base, reason_code="comment_body_shape_invalid")
    # PR #2683 fix_delta P1-3 (OWNER adversarial review): a `comment_id`
    # that resolves to a DIFFERENT Issue's comment (e.g. a stale/reused id,
    # or an id belonging to an unrelated Issue) must never be silently
    # accepted -- verify the comment actually belongs to `issue_number`.
    if current_comment.get("issue_url") != expected_issue_url:
        return _environment_error_result(base, reason_code="comment_issue_url_mismatch")

    try:
        current_anchor = fetch_json_object(build_gh_argv_comment(repo, anchor_comment_id), gh_runner, timeout)
    except GhFetchFailed as exc:
        return _environment_error_result(
            base, reason_code=f"anchor_fetch_failed:{exc.reason_code}", detail=exc.detail
        )
    current_anchor_body = current_anchor.get("body")
    if not isinstance(current_anchor_body, str):
        return _environment_error_result(base, reason_code="anchor_comment_body_shape_invalid")
    # Same identity-binding risk applies to the anchor comment (it carries
    # the options the owner is reacting to) -- verify it too belongs to
    # `issue_number`.
    if current_anchor.get("issue_url") != expected_issue_url:
        return _environment_error_result(base, reason_code="anchor_comment_issue_url_mismatch")

    try:
        current_issue = fetch_json_object(build_gh_argv_issue(repo, issue_number), gh_runner, timeout)
    except GhFetchFailed as exc:
        return _environment_error_result(
            base, reason_code=f"issue_fetch_failed:{exc.reason_code}", detail=exc.detail
        )
    current_issue_body = current_issue.get("body")
    if not isinstance(current_issue_body, str):
        return _environment_error_result(base, reason_code="issue_body_shape_invalid")

    comment_drifted = sha256_hex(current_comment_body) != preview_binding["comment_body_hash"]
    anchor_drifted = sha256_hex(current_anchor_body) != preview_binding["anchor_comment_body_hash"]
    issue_drifted = sha256_hex(current_issue_body) != preview_binding["issue_snapshot_hash"]
    if comment_drifted or anchor_drifted or issue_drifted:
        return _stale_result(
            base,
            comment_drifted=comment_drifted,
            issue_drifted=issue_drifted,
            anchor_drifted=anchor_drifted,
        )

    owner_contents = resolve_owner_reaction_contents(reactions, resolved_owner_id)
    selection = compute_selection(owner_contents, preview_binding["reaction_option_map"])

    result = dict(base)
    result["status"] = selection["status"]
    result["reason_code"] = selection["reason_code"]
    result["selected_option_id"] = selection["selected_option_id"]
    result["owner_reaction_contents"] = sorted(set(owner_contents))
    result["fetched_reaction_count"] = len(reactions)
    result["drift"] = {
        "comment_body_drifted": False,
        "issue_body_drifted": False,
        "anchor_body_drifted": False,
    }
    if selection["status"] == STATUS_SELECTED:
        result["selected_option_metadata"] = preview_binding["options"][selection["selected_option_id"]]
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--owner-user-id", required=True, type=int)
    parser.add_argument("--preview-binding-file", required=True)
    parser.add_argument(
        "--gh-fixture-file",
        required=False,
        default=None,
        help=(
            "TEST ONLY: bypass the real `gh` subprocess with a canned JSON "
            "fixture (see make_fixture_gh_runner docstring). Used only by "
            "the command_registry.py `owner_reaction.decide.fixture` "
            "test-only sibling entry -- the production `owner_reaction.decide` "
            "entry never sets this flag."
        ),
    )
    parser.add_argument("--timeout", required=False, type=float, default=DEFAULT_GH_TIMEOUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    generated_at = _now_iso()

    if args.issue_number <= 0 or args.owner_user_id <= 0:
        base = _base_result(repo=args.repo, issue_number=args.issue_number, generated_at=generated_at)
        result = _environment_error_result(base, reason_code="invalid_cli_argument")
        print(json.dumps(result, ensure_ascii=False))
        return 1

    try:
        preview_binding_text = Path(args.preview_binding_file).read_text(encoding="utf-8")
        preview_binding = json.loads(preview_binding_text)
    except (OSError, json.JSONDecodeError) as exc:
        base = _base_result(repo=args.repo, issue_number=args.issue_number, generated_at=generated_at)
        result = _environment_error_result(
            base, reason_code="preview_binding_file_unreadable", detail=str(exc)
        )
        print(json.dumps(result, ensure_ascii=False))
        return 1

    if args.gh_fixture_file:
        gh_runner: GhRunner = make_fixture_gh_runner(Path(args.gh_fixture_file))
    else:
        gh_runner = default_gh_runner

    result = decide(
        repo=args.repo,
        issue_number=args.issue_number,
        owner_user_id=args.owner_user_id,
        preview_binding=preview_binding,
        gh_runner=gh_runner,
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] != STATUS_ENVIRONMENT_ERROR else 1


if __name__ == "__main__":
    raise SystemExit(main())
