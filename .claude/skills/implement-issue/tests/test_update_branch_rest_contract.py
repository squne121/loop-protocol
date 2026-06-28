#!/usr/bin/env python3
"""Contract tests for update_branch REST wrapper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / '.claude' / 'skills' / 'implement-issue' / 'scripts' / 'update_branch.py'

spec = importlib.util.spec_from_file_location('update_branch_module', SCRIPT_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

CommandResult = module.CommandResult
UpdateBranchRequest = module.UpdateBranchRequest
execute_update_branch = module.execute_update_branch
run_gh = module.run_gh


class FakeGhRunner:
    def __init__(self, responses: dict[str, list[CommandResult]]) -> None:
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> CommandResult:
        self.calls.append(args)
        key = self._key(args)
        queue = self.responses.get(key)
        if not queue:
            raise AssertionError(f'No fake response queued for {key}: {args}')
        return queue.pop(0)

    @staticmethod
    def _key(args: list[str]) -> str:
        if args[:4] == ['api', '-i', '-X', 'PUT']:
            return 'update'
        if args[:2] == ['api', 'user']:
            return 'user'
        if 'headRepository,baseRepository,maintainerCanModify,isCrossRepository' in args:
            return 'permission_view'
        if 'headRefOid' in args:
            return 'head'
        raise AssertionError(f'Unexpected gh args: {args}')


def request(expected_head_sha: str = 'abc123') -> UpdateBranchRequest:
    return UpdateBranchRequest(
        pr_number=42,
        repo='squne121/loop-protocol',
        expected_head_sha=expected_head_sha,
        caller='impl-review-loop.step-5',
    )


def http_response(
    status: int,
    body: dict[str, object] | str,
    *,
    headers: dict[str, str] | None = None,
) -> str:
    payload = body if isinstance(body, str) else json.dumps(body)
    header_lines = ['content-type: application/json']
    for key, value in (headers or {}).items():
        header_lines.append(f'{key}: {value}')
    rendered_headers = '\n'.join(header_lines)
    return f'HTTP/1.1 {status} test\n{rendered_headers}\n\n{payload}'


class TestUpdateBranchRestContract:
    def test_given_missing_expected_head_sha_when_execute_then_block_without_api_call(self):
        runner = FakeGhRunner({})

        result = execute_update_branch(request(expected_head_sha='   '), gh_runner=runner)

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'expected_head_sha_missing'
        assert runner.calls == []

    def test_given_preflight_mismatch_when_execute_then_block_before_rest_call(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, 'def456\n')],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'expected_head_sha_mismatch'
        assert result['before_head_sha'] == 'def456'
        assert all(call[:4] != ['api', '-i', '-X', 'PUT'] for call in runner.calls)

    def test_given_202_and_head_changes_when_execute_then_return_ok_and_rerun_both(self):
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, 'abc123\n'),
                CommandResult(0, 'abc123\n'),
                CommandResult(0, 'def456\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
        })

        result = execute_update_branch(
            request(),
            gh_runner=runner,
            sleep_fn=sleeps.append,
            poll_max=3,
            poll_interval=0.25,
        )

        assert result['status'] == 'ok'
        assert result['before_head_sha'] == 'abc123'
        assert result['after_head_sha'] == 'def456'
        assert result['new_head_sha'] == 'def456'
        assert result['rerun_required'] == {
            'verification': True,
            'pr_review': True,
            'reason': 'pr_head_changed_by_update_branch',
        }
        assert sleeps == [0.25]

    def test_given_202_and_head_unchanged_when_execute_then_fail_deterministically(self):
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, 'abc123\n'),
                CommandResult(0, 'abc123\n'),
                CommandResult(0, 'abc123\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
        })

        result = execute_update_branch(
            request(),
            gh_runner=runner,
            sleep_fn=sleeps.append,
            poll_max=2,
            poll_interval=0.5,
        )

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'head_unchanged_after_accepted'
        assert result['after_head_sha'] == 'abc123'
        assert result['poll_attempts'] == 2
        assert sleeps == [0.5]

    def test_given_403_permission_denied_when_execute_then_collect_permission_diagnostics(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, 'abc123\n')],
            'update': [CommandResult(1, http_response(403, {'message': 'Forbidden'}))],
            'user': [CommandResult(0, 'squne121\n')],
            'permission_view': [
                CommandResult(
                    0,
                    json.dumps(
                        {
                            'headRepository': {'nameWithOwner': 'squne121/loop-protocol'},
                            'baseRepository': {'nameWithOwner': 'squne121/loop-protocol'},
                            'maintainerCanModify': False,
                            'isCrossRepository': False,
                        }
                    ),
                )
            ],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'permission_blocked'
        assert result['reason_code'] == 'permission_denied'
        assert result['permission_diagnostics'] == {
            'auth_actor': 'squne121',
            'head_repo': 'squne121/loop-protocol',
            'base_repo': 'squne121/loop-protocol',
            'fork_pr': False,
            'maintainer_can_modify': False,
            'required_permissions': 'pull_requests:write, contents:write_on_head_repository_when_github_app',
        }

    @pytest.mark.parametrize('status', [403, 422, 429])
    def test_given_secondary_rate_limit_when_execute_then_fail_closed_with_header_diagnostics(self, status: int):
        runner = FakeGhRunner({
            'head': [CommandResult(0, 'abc123\n')],
            'update': [
                CommandResult(
                    1,
                    http_response(
                        status,
                        {'message': 'You have exceeded a secondary rate limit. Please retry later.'},
                        headers={
                            'retry-after': '30',
                            'x-ratelimit-remaining': '0',
                            'x-ratelimit-reset': '1718181818',
                        },
                    ),
                )
            ],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'secondary_rate_limit'
        assert result['rate_limit_diagnostics'] == {
            'retry_after_seconds': 30,
            'x_ratelimit_remaining': 0,
            'x_ratelimit_reset': 1718181818,
        }

    def test_given_422_expected_head_sha_mismatch_when_execute_then_block_and_refetch_current_head(self):
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, 'abc123\n'),
                CommandResult(0, 'def456\n'),
            ],
            'update': [
                CommandResult(
                    1,
                    http_response(422, {'message': 'expected_head_sha does not match current head sha'}),
                )
            ],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'expected_head_sha_mismatch'
        assert result['after_head_sha'] == 'def456'

    def test_given_422_expected_head_sha_mismatch_and_refetch_fails_when_execute_then_keep_after_head_null(self):
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, 'abc123\n'),
                CommandResult(1, '', 'gh pr view failed'),
            ],
            'update': [
                CommandResult(
                    1,
                    http_response(422, {'message': 'expected_head_sha does not match current head sha'}),
                )
            ],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'expected_head_sha_mismatch'
        assert result['after_head_sha'] is None
        assert 'gh pr view failed' in result['errors']

    def test_given_422_validation_failure_when_execute_then_fail_closed(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, 'abc123\n')],
            'update': [CommandResult(1, http_response(422, {'message': 'Validation failed'}))],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'

    def test_given_transport_error_when_execute_then_return_transport_error(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, 'abc123\n')],
            'update': [CommandResult(1, '', 'dial tcp timeout')],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'transport_error'

    def test_given_unknown_http_status_when_execute_then_fail_closed(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, 'abc123\n')],
            'update': [CommandResult(1, http_response(500, {'message': 'server error'}))],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'unknown_http_status'


class TestRunGhTransportGuard:
    def test_given_timeout_expired_when_run_gh_then_return_command_result(self, monkeypatch: pytest.MonkeyPatch):
        def fake_run(*_args, **_kwargs):
            raise module.subprocess.TimeoutExpired(cmd=['gh'], timeout=60, stderr='timed out')

        monkeypatch.setattr(module.subprocess, 'run', fake_run)

        result = run_gh(['api', 'user'])

        assert result.returncode == 124
        assert result.stderr == 'timed out'

    def test_given_file_not_found_when_run_gh_then_return_transport_command_result(
        self,
        monkeypatch: pytest.MonkeyPatch
    ):
        def fake_run(*_args, **_kwargs):
            raise FileNotFoundError('gh not found')

        monkeypatch.setattr(module.subprocess, 'run', fake_run)

        result = run_gh(['api', 'user'])

        assert result.returncode == 127
        assert 'gh not found' in result.stderr
