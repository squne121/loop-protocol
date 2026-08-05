#!/usr/bin/env python3
"""Input-boundary contract tests for update_branch.py (#1429).

execute_update_branch() must self-validate pr_number / repo / caller /
expected_head_sha format / update_method *before* calling the GitHub API.
Any violation must be reported as a deterministic reason_code without
issuing a `gh` command.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / '.claude' / 'skills' / 'implement-issue' / 'scripts' / 'update_branch.py'

spec = importlib.util.spec_from_file_location('update_branch_input_validation_module', SCRIPT_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

UpdateBranchRequest = module.UpdateBranchRequest
execute_update_branch = module.execute_update_branch
CANONICAL_REPO = module.CANONICAL_REPO

VALID_SHA = 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2'


class RefusingGhRunner:
    """gh_runner stub that fails the test if the API is ever called.

    Used to prove input validation short-circuits before any subprocess
    invocation (fail-closed boundary check, #1429 AC4).
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(args)
        raise AssertionError(f'GitHub API must not be called for invalid input: {args}')


def base_request(**overrides: object) -> UpdateBranchRequest:
    fields: dict[str, object] = {
        'pr_number': 42,
        'repo': CANONICAL_REPO,
        'expected_head_sha': VALID_SHA,
        'caller': 'impl-review-loop.step-5',
    }
    fields.update(overrides)
    return UpdateBranchRequest(**fields)


class TestPrNumberValidation:
    @pytest.mark.parametrize('pr_number', [0, -1, -42])
    def test_given_non_positive_pr_number_when_execute_then_validation_failed_without_api_call(
        self, pr_number: int
    ):
        runner = RefusingGhRunner()

        result = execute_update_branch(base_request(pr_number=pr_number), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'
        assert runner.calls == []


class TestRepoValidation:
    @pytest.mark.parametrize(
        'repo',
        ['not-canonical/loop-protocol', 'squne121/other-repo', '', 'squne121/loop-protocol.git'],
    )
    def test_given_non_canonical_repo_when_execute_then_validation_failed_without_api_call(self, repo: str):
        runner = RefusingGhRunner()

        result = execute_update_branch(base_request(repo=repo), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'
        assert runner.calls == []

    def test_given_canonical_repo_when_validated_then_no_repo_violation(self):
        runner = RefusingGhRunner()

        # canonical repo passes the repo check; the RefusingGhRunner still
        # raises because a real head-sha preflight call would follow, so we
        # only assert the failure is NOT a repo-shaped validation_failed at
        # the boundary — i.e. execution proceeds past _validate_request.
        with pytest.raises(AssertionError):
            execute_update_branch(base_request(), gh_runner=runner)


class TestExpectedHeadShaFormatValidation:
    @pytest.mark.parametrize(
        'expected_head_sha',
        [
            'abc123',
            'zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz',
            'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b',
            'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c',
        ],
    )
    def test_given_non_full_length_hex_sha_when_execute_then_validation_failed_without_api_call(
        self, expected_head_sha: str
    ):
        runner = RefusingGhRunner()

        result = execute_update_branch(base_request(expected_head_sha=expected_head_sha), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'
        assert runner.calls == []

    def test_given_empty_expected_head_sha_when_execute_then_missing_not_validation_failed(self):
        runner = RefusingGhRunner()

        result = execute_update_branch(base_request(expected_head_sha='   '), gh_runner=runner)

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'expected_head_sha_missing'
        assert runner.calls == []


class TestCallerAllowlistValidation:
    @pytest.mark.parametrize('caller', ['unknown-caller', '', 'evil-actor.step-99'])
    def test_given_caller_not_in_allowlist_when_execute_then_validation_failed_without_api_call(
        self, caller: str
    ):
        runner = RefusingGhRunner()

        result = execute_update_branch(base_request(caller=caller), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'
        assert runner.calls == []

    @pytest.mark.parametrize('caller', ['impl-review-loop.step-5', 'manual'])
    def test_given_known_caller_when_validated_then_no_caller_violation(self, caller: str):
        runner = RefusingGhRunner()

        # A known caller passes the caller-allowlist check; execution then
        # proceeds to the head-sha preflight call, which RefusingGhRunner
        # rejects — proving the caller check itself did not block.
        with pytest.raises(AssertionError):
            execute_update_branch(base_request(caller=caller), gh_runner=runner)


class TestUpdateMethodValidation:
    def test_given_non_merge_only_update_method_when_execute_then_validation_failed_without_api_call(self):
        runner = RefusingGhRunner()

        result = execute_update_branch(base_request(update_method='rebase'), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'
        assert runner.calls == []


class TestValidationOrdering:
    def test_given_multiple_violations_when_execute_then_fail_closed_before_any_api_call(self):
        runner = RefusingGhRunner()

        result = execute_update_branch(
            base_request(pr_number=-1, repo='wrong/repo', caller='nope', expected_head_sha='bad'),
            gh_runner=runner,
        )

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'
        assert runner.calls == []
