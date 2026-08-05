#!/usr/bin/env python3
"""Contract tests for update_branch REST wrapper."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

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

VALID_HEAD_A = 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2'
VALID_HEAD_B = 'b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3'
VALID_BASE = 'c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4'


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
        if 'compare' in (args[1] if len(args) > 1 else ''):
            return 'compare'
        if 'headRefOid' in args:
            return 'head'
        if 'baseRefOid' in args:
            return 'base'
        raise AssertionError(f'Unexpected gh args: {args}')


def request(expected_head_sha: str = VALID_HEAD_A) -> UpdateBranchRequest:
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


def compare_result(status: str) -> CommandResult:
    return CommandResult(0, f'{status}\n')


class TestUpdateBranchRestContract:
    def test_given_missing_expected_head_sha_when_execute_then_block_without_api_call(self):
        runner = FakeGhRunner({})

        result = execute_update_branch(request(expected_head_sha='   '), gh_runner=runner)

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'expected_head_sha_missing'
        assert runner.calls == []

    def test_given_preflight_mismatch_when_execute_then_block_before_rest_call(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, f'{VALID_HEAD_B}\n')],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'expected_head_sha_mismatch'
        assert result['before_head_sha'] == VALID_HEAD_B
        assert all(call[:4] != ['api', '-i', '-X', 'PUT'] for call in runner.calls)

    def test_given_202_and_head_changes_and_ancestry_verified_when_execute_then_return_ok_and_rerun_both(self):
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_B}\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
            'compare': [
                compare_result('ahead'),  # expected_head_sha is ancestor of new head
                compare_result('ahead'),  # before_base_sha is ancestor of new head
            ],
        })

        result = execute_update_branch(
            request(),
            gh_runner=runner,
            sleep_fn=sleeps.append,
            poll_max=3,
            poll_interval=0.25,
        )

        assert result['status'] == 'ok'
        assert result['before_head_sha'] == VALID_HEAD_A
        assert result['before_base_sha'] == VALID_BASE
        assert result['after_head_sha'] == VALID_HEAD_B
        assert result['new_head_sha'] == VALID_HEAD_B
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
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_A}\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
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
        assert result['after_head_sha'] == VALID_HEAD_A
        assert result['poll_attempts'] == 2
        assert sleeps == [0.5]

    def test_given_403_permission_denied_when_execute_then_collect_permission_diagnostics(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, f'{VALID_HEAD_A}\n')],
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
            'head': [CommandResult(0, f'{VALID_HEAD_A}\n')],
            'update': [
                CommandResult(
                    1,
                    http_response(
                        status,
                        {'message': 'You have exceeded a secondary rate limit. Please retry later.'},
                        headers={
                            'retry-after': '30',
                            'x-ratelimit-remaining': '5',
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
            'x_ratelimit_remaining': 5,
            'x_ratelimit_reset': 1718181818,
        }

    @pytest.mark.parametrize('status', [403, 429])
    def test_given_primary_rate_limit_headers_when_execute_then_classify_as_primary_not_secondary(
        self, status: int
    ):
        # #1429 iteration-1 P2: a primary rate-limit exhaustion response
        # (x-ratelimit-remaining: 0, no secondary/abuse-detection wording)
        # must not be misclassified as secondary_rate_limit.
        runner = FakeGhRunner({
            'head': [CommandResult(0, f'{VALID_HEAD_A}\n')],
            'update': [
                CommandResult(
                    1,
                    http_response(
                        status,
                        {'message': 'API rate limit exceeded for user.'},
                        headers={
                            'retry-after': '60',
                            'x-ratelimit-remaining': '0',
                            'x-ratelimit-reset': '1718181818',
                        },
                    ),
                )
            ],
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
            'user': [CommandResult(0, 'squne121\n')],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['reason_code'] == 'primary_rate_limit'
        assert result['reason_code'] != 'secondary_rate_limit'
        assert result['rate_limit_diagnostics']['x_ratelimit_remaining'] == 0

    def test_given_422_expected_head_sha_mismatch_when_execute_then_block_and_refetch_current_head(self):
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_B}\n'),
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
        assert result['after_head_sha'] == VALID_HEAD_B

    def test_given_422_expected_head_sha_mismatch_and_refetch_fails_when_execute_then_keep_after_head_null(self):
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
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
            'head': [CommandResult(0, f'{VALID_HEAD_A}\n')],
            'update': [CommandResult(1, http_response(422, {'message': 'Validation failed'}))],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'validation_failed'

    def test_given_transport_error_when_execute_then_return_transport_error(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, f'{VALID_HEAD_A}\n')],
            'update': [CommandResult(1, '', 'dial tcp timeout')],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'transport_error'

    def test_given_unknown_http_status_when_execute_then_fail_closed(self):
        runner = FakeGhRunner({
            'head': [CommandResult(0, f'{VALID_HEAD_A}\n')],
            'update': [CommandResult(1, http_response(500, {'message': 'server error'}))],
        })

        result = execute_update_branch(request(), gh_runner=runner)

        assert result['status'] == 'failed'
        assert result['reason_code'] == 'unknown_http_status'


class TestPostUpdatePostconditionVerification:
    """Adversarial postcondition tests (#1429 iteration-1 P1-2).

    A bare "headRefOid differs from expected_head_sha" is not sufficient
    evidence that update-branch actually succeeded. These tests prove that
    format/ancestry verification is required before status: ok is reported.
    """

    def test_given_poll_returns_null_head_when_execute_then_blocked_unexpected_head_change(self):
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, 'null\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
        })

        result = execute_update_branch(
            request(), gh_runner=runner, sleep_fn=sleeps.append, poll_max=2, poll_interval=0.1,
        )

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'unexpected_head_change'
        assert result['rerun_required'] == {
            'verification': True,
            'pr_review': True,
            'reason': 'unexpected_head_change_after_update_branch',
        }

    def test_given_poll_returns_short_abbreviated_sha_when_execute_then_blocked_unexpected_head_change(self):
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, 'b2c3d4e\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
        })

        result = execute_update_branch(
            request(), gh_runner=runner, sleep_fn=sleeps.append, poll_max=2, poll_interval=0.1,
        )

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'unexpected_head_change'

    def test_given_poll_returns_unrelated_full_length_sha_when_execute_then_blocked_unexpected_head_change(self):
        # New head is a real full-length SHA, but the compare API says it
        # is NOT an ancestor of expected_head_sha (diverged history) —
        # this must not be reported as ok.
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_B}\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
            'compare': [compare_result('diverged')],
        })

        result = execute_update_branch(
            request(), gh_runner=runner, sleep_fn=sleeps.append, poll_max=2, poll_interval=0.1,
        )

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'unexpected_head_change'
        assert result['after_head_sha'] == VALID_HEAD_B

    def test_given_concurrent_push_not_containing_old_head_when_execute_then_blocked_unexpected_head_change(self):
        # expected_head_sha ancestry check fails ("behind"): new head does
        # not contain expected_head_sha's commits (concurrent force-push).
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_B}\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
            'compare': [compare_result('behind')],
        })

        result = execute_update_branch(
            request(), gh_runner=runner, sleep_fn=sleeps.append, poll_max=2, poll_interval=0.1,
        )

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'unexpected_head_change'

    def test_given_new_head_does_not_contain_base_when_execute_then_blocked_unexpected_head_change(self):
        # expected_head_sha ancestry passes, but the base-ancestor check
        # fails: the new head does not contain the pre-update base commit.
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_B}\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
            'compare': [
                compare_result('ahead'),  # expected_head_sha IS an ancestor
                compare_result('diverged'),  # base SHA is NOT an ancestor
            ],
        })

        result = execute_update_branch(
            request(), gh_runner=runner, sleep_fn=sleeps.append, poll_max=2, poll_interval=0.1,
        )

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'unexpected_head_change'

    def test_given_compare_api_transport_error_when_execute_then_fail_closed_not_ok(self):
        # Ancestry could not be determined at all (compare API failure) —
        # must not default to ok.
        sleeps: list[float] = []
        runner = FakeGhRunner({
            'head': [
                CommandResult(0, f'{VALID_HEAD_A}\n'),
                CommandResult(0, f'{VALID_HEAD_B}\n'),
            ],
            'update': [CommandResult(0, http_response(202, {'message': 'accepted'}))],
            'base': [CommandResult(0, f'{VALID_BASE}\n')],
            'compare': [CommandResult(1, '', 'network error')],
        })

        result = execute_update_branch(
            request(), gh_runner=runner, sleep_fn=sleeps.append, poll_max=2, poll_interval=0.1,
        )

        assert result['status'] == 'blocked'
        assert result['reason_code'] == 'unexpected_head_change'


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


FAKE_GH_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys

CALL_LOG = os.environ['FAKE_GH_CALL_LOG']
SCRIPT_SEQUENCE = os.environ['FAKE_GH_RESPONSES']

with open(CALL_LOG, 'a', encoding='utf-8') as fh:
    fh.write(json.dumps(sys.argv[1:]) + chr(10))

with open(SCRIPT_SEQUENCE, encoding='utf-8') as fh:
    sequence = json.load(fh)

count_path = SCRIPT_SEQUENCE + '.count'
call_index = 0
if os.path.exists(count_path):
    with open(count_path, encoding='utf-8') as fh:
        call_index = int(fh.read().strip() or '0')
with open(count_path, 'w', encoding='utf-8') as fh:
    fh.write(str(call_index + 1))

if call_index >= len(sequence):
    sys.stderr.write('fake gh: no scripted response left\\n')
    sys.exit(99)

entry = sequence[call_index]
sys.stdout.write(entry.get('stdout', ''))
sys.stderr.write(entry.get('stderr', ''))
sys.exit(entry.get('returncode', 0))
"""


def _write_fake_gh(tmp_path: Path, responses: list[dict[str, object]]) -> Path:
    bin_dir = tmp_path / 'fakebin'
    bin_dir.mkdir(exist_ok=True)
    script_path = bin_dir / 'gh'
    script_path.write_text(FAKE_GH_SCRIPT, encoding='utf-8')
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    responses_path = tmp_path / 'responses.json'
    responses_path.write_text(json.dumps(responses), encoding='utf-8')

    return bin_dir


def _run_canonical_subprocess(
    tmp_path: Path,
    bin_dir: Path,
    responses_path: Path,
    call_log_path: Path,
    *,
    extra_argv: list[str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env['PATH'] = f'{bin_dir}{os.pathsep}{env.get("PATH", "")}'
    env['FAKE_GH_CALL_LOG'] = str(call_log_path)
    env['FAKE_GH_RESPONSES'] = str(responses_path)

    argv = [
        sys.executable,
        str(SCRIPT_PATH),
        '--pr-number',
        '42',
        '--repo',
        'squne121/loop-protocol',
        '--expected-head-sha',
        VALID_HEAD_A,
        '--caller',
        'impl-review-loop.step-5',
        '--update-method',
        'merge_only',
    ]
    if extra_argv:
        argv.extend(extra_argv)

    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


class TestCanonicalSubprocessInvocation:
    """Real-subprocess CLI-entrypoint tests (#1429 iteration-1 P1-3).

    Launches `update_branch.py` as a genuine subprocess (not importlib
    in-process, not a monkeypatched run_gh) with a fake `gh` executable
    placed first on PATH, so the actual argparse exit codes, stdout
    single-JSON-only contract, gh resolution via PATH, and the exact argv
    constructed for each HTTP branch are exercised end-to-end.
    """

    def test_given_success_sequence_when_run_as_subprocess_then_exit_0_and_single_json_stdout(
        self, tmp_path: Path
    ):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        # Scripted sequence matching the real poll loop: preflight head,
        # update PUT, base fetch, one poll head (changed), then two
        # compare calls (expected_head_sha ancestry, base-sha ancestry).
        responses = [
            {'stdout': f'{VALID_HEAD_A}\n'},
            {'stdout': 'HTTP/1.1 202 Accepted\ncontent-type: application/json\n\n{"message":"accepted"}'},
            {'stdout': f'{VALID_BASE}\n'},
            {'stdout': f'{VALID_HEAD_B}\n'},
            {'stdout': 'ahead\n'},
            {'stdout': 'ahead\n'},
        ]
        bin_dir = _write_fake_gh(tmp_path, responses)

        completed = _run_canonical_subprocess(tmp_path, bin_dir, responses_path, call_log_path)

        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload['status'] == 'ok'
        assert payload['new_head_sha'] == VALID_HEAD_B
        assert completed.stderr == '' or 'fake gh' not in completed.stderr
        # stdout must be exactly one JSON document (no contamination).
        json.loads(completed.stdout)

    def test_given_invalid_repo_when_run_as_subprocess_then_exit_1_without_invoking_fake_gh(
        self, tmp_path: Path
    ):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses_path.write_text('[]', encoding='utf-8')

        env = dict(os.environ)
        env['PATH'] = f'{bin_dir}{os.pathsep}{env.get("PATH", "")}'
        env['FAKE_GH_CALL_LOG'] = str(call_log_path)
        env['FAKE_GH_RESPONSES'] = str(responses_path)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                '--pr-number',
                '42',
                '--repo',
                'not-canonical/repo',
                '--expected-head-sha',
                VALID_HEAD_A,
                '--caller',
                'impl-review-loop.step-5',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['reason_code'] == 'validation_failed'
        assert not call_log_path.exists()

    def test_given_invalid_caller_when_run_as_subprocess_then_exit_1_without_invoking_fake_gh(
        self, tmp_path: Path
    ):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses_path.write_text('[]', encoding='utf-8')

        env = dict(os.environ)
        env['PATH'] = f'{bin_dir}{os.pathsep}{env.get("PATH", "")}'
        env['FAKE_GH_CALL_LOG'] = str(call_log_path)
        env['FAKE_GH_RESPONSES'] = str(responses_path)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                '--pr-number',
                '42',
                '--repo',
                'squne121/loop-protocol',
                '--expected-head-sha',
                VALID_HEAD_A,
                '--caller',
                'not-a-known-caller',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['reason_code'] == 'validation_failed'
        assert not call_log_path.exists()

    def test_given_sha_mismatch_when_run_as_subprocess_then_exit_1_and_skip_update_call(self, tmp_path: Path):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses = [{'stdout': f'{VALID_HEAD_B}\n'}]
        responses_path.write_text(json.dumps(responses), encoding='utf-8')

        completed = _run_canonical_subprocess(tmp_path, bin_dir, responses_path, call_log_path)

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['status'] == 'blocked'
        assert payload['reason_code'] == 'expected_head_sha_mismatch'
        calls = [json.loads(line) for line in call_log_path.read_text(encoding='utf-8').splitlines()]
        assert all(call[:4] != ['api', '-i', '-X', 'PUT'] for call in calls)

    def test_given_202_then_unchanged_poll_when_run_as_subprocess_then_exit_1_head_unchanged(
        self, tmp_path: Path
    ):
        # Production invocation always uses PRODUCTION_POLL_MAX=12 /
        # PRODUCTION_POLL_INTERVAL=5.0s real time.sleep() (#1429 AC8 — a
        # caller cannot relax these), so this specific subprocess exercise
        # genuinely takes ~60s wall-clock; give it a generous timeout
        # rather than asserting on a shortened bound the CLI does not
        # expose.
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        responses = [
            {'stdout': f'{VALID_HEAD_A}\n'},
            {'stdout': 'HTTP/1.1 202 Accepted\ncontent-type: application/json\n\n{"message":"accepted"}'},
            {'stdout': f'{VALID_BASE}\n'},
        ] + [{'stdout': f'{VALID_HEAD_A}\n'}] * 12
        bin_dir = _write_fake_gh(tmp_path, responses)

        completed = _run_canonical_subprocess(
            tmp_path, bin_dir, responses_path, call_log_path, timeout=90,
        )

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['reason_code'] == 'head_unchanged_after_accepted'

    def test_given_403_when_run_as_subprocess_then_exit_1_permission_blocked(self, tmp_path: Path):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses = [
            {'stdout': f'{VALID_HEAD_A}\n'},
            {'stdout': 'HTTP/1.1 403 Forbidden\ncontent-type: application/json\n\n{"message":"Forbidden"}'},
            {'stdout': 'squne121\n'},
            {
                'stdout': json.dumps(
                    {
                        'headRepository': {'nameWithOwner': 'squne121/loop-protocol'},
                        'baseRepository': {'nameWithOwner': 'squne121/loop-protocol'},
                        'maintainerCanModify': False,
                        'isCrossRepository': False,
                    }
                )
            },
        ]
        responses_path.write_text(json.dumps(responses), encoding='utf-8')

        completed = _run_canonical_subprocess(tmp_path, bin_dir, responses_path, call_log_path)

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['status'] == 'permission_blocked'
        assert payload['reason_code'] == 'permission_denied'

    def test_given_422_when_run_as_subprocess_then_exit_1_validation_failed(self, tmp_path: Path):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses = [
            {'stdout': f'{VALID_HEAD_A}\n'},
            {'stdout': 'HTTP/1.1 422 Unprocessable\ncontent-type: application/json\n\n{"message":"Validation failed"}'},
        ]
        responses_path.write_text(json.dumps(responses), encoding='utf-8')

        completed = _run_canonical_subprocess(tmp_path, bin_dir, responses_path, call_log_path)

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['reason_code'] == 'validation_failed'

    def test_given_429_when_run_as_subprocess_then_exit_1_secondary_rate_limit(self, tmp_path: Path):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses = [
            {'stdout': f'{VALID_HEAD_A}\n'},
            {
                'stdout': (
                    'HTTP/1.1 429 Too Many Requests\ncontent-type: application/json\n\n'
                    '{"message":"You have exceeded a secondary rate limit. Please retry later."}'
                )
            },
        ]
        responses_path.write_text(json.dumps(responses), encoding='utf-8')

        completed = _run_canonical_subprocess(tmp_path, bin_dir, responses_path, call_log_path)

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['reason_code'] == 'secondary_rate_limit'

    def test_given_gh_command_times_out_when_run_as_subprocess_then_exit_1_transport_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Simulate a hanging/timed-out gh by making the fake gh sleep past
        # the wrapper's own internal 60s subprocess.run timeout would be
        # too slow for a unit test; instead exercise the transport-error
        # path via a fake gh that exits non-zero with no parseable HTTP
        # status, which is the same fail-closed contract surface reached
        # by a real timeout (run_gh() converts TimeoutExpired to
        # returncode=124, http.status=None -> REASON_TRANSPORT_ERROR).
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses = [
            {'stdout': f'{VALID_HEAD_A}\n'},
            {'stdout': '', 'stderr': 'dial tcp: i/o timeout', 'returncode': 1},
        ]
        responses_path.write_text(json.dumps(responses), encoding='utf-8')

        completed = _run_canonical_subprocess(tmp_path, bin_dir, responses_path, call_log_path)

        assert completed.returncode == 1
        payload = json.loads(completed.stdout)
        assert payload['reason_code'] == 'transport_error'

    def test_given_argparse_missing_required_flag_when_run_as_subprocess_then_exit_2_and_no_gh_call(
        self, tmp_path: Path
    ):
        responses_path = tmp_path / 'responses.json'
        call_log_path = tmp_path / 'calls.log'
        bin_dir = _write_fake_gh(tmp_path, [])
        responses_path.write_text('[]', encoding='utf-8')

        env = dict(os.environ)
        env['PATH'] = f'{bin_dir}{os.pathsep}{env.get("PATH", "")}'
        env['FAKE_GH_CALL_LOG'] = str(call_log_path)
        env['FAKE_GH_RESPONSES'] = str(responses_path)

        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), '--pr-number', '42'],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert completed.returncode == 2
        assert not call_log_path.exists()
