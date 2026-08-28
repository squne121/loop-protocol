"""Issue #2368: verify the root-level AGY post-runtime override dead code
(PR #2357 origin, superseded by PR #2365's SubAgent-local AGY-first + native
fallback design) has been fully removed from ``root_entry_router.py`` and
its now-orphaned dedicated test/compat scaffolding.
"""

import ast
from pathlib import Path


def test_agy_post_runtime_override_fully_removed():
    router_path = Path(".claude/skills/issue-refinement-loop/scripts/root_entry_router.py")
    router = router_path.read_text(encoding="utf-8")
    tree = ast.parse(router)

    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "capability_preflight_result"
    )
    arguments = {
        argument.arg
        for argument in [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
    }
    for parameter in (
        "agy_observed_failure_class",
        "agy_required",
        "agy_fallback_allowed",
    ):
        assert parameter not in arguments, parameter

    for marker in (
        "_apply_agy_observed_failure_override",
        "resolve_agy_advisory_route",
        "_AGY_ADVISORY_FALLBACK_FAILURE_CLASSES",
        "AGY_ROUTE_AGY",
        "AGY_ROUTE_NON_AGY_FALLBACK",
        "AGY_ROUTE_BLOCKED",
    ):
        assert marker not in router, marker

    assert not Path(
        ".claude/skills/issue-refinement-loop/tests/test_agy_advisory_fallback.py"
    ).exists()

    compat = Path(
        "scripts/claude-gpt/tests/test_timeout_taxonomy_compat.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "resolve_agy_advisory_route",
        "root_entry_router",
        "_ISSUE_REFINEMENT_LOOP_SCRIPTS",
        "agy_timeout",
    ):
        assert marker not in compat, marker
