#!/usr/bin/env python3
"""Poll-bound immutability contract tests for update_branch.py (#1429 AC8).

Production invocation (main()) must always poll with PRODUCTION_POLL_MAX /
PRODUCTION_POLL_INTERVAL. A caller must not be able to relax these bounds
via CLI flags.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / '.claude' / 'skills' / 'implement-issue' / 'scripts' / 'update_branch.py'

spec = importlib.util.spec_from_file_location('update_branch_poll_bounds_module', SCRIPT_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

VALID_SHA = 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2'


def base_argv() -> list[str]:
    return [
        '--pr-number',
        '42',
        '--repo',
        module.CANONICAL_REPO,
        '--expected-head-sha',
        VALID_SHA,
        '--caller',
        'impl-review-loop.step-5',
    ]


class TestProductionPollBoundsAreFixedConstants:
    def test_given_module_defaults_when_read_then_match_documented_production_bounds(self):
        assert module.PRODUCTION_POLL_MAX == 12
        assert module.PRODUCTION_POLL_INTERVAL == 5.0

    def test_given_execute_update_branch_default_kwargs_when_inspected_then_use_production_constants(self):
        import inspect

        signature = inspect.signature(module.execute_update_branch)
        assert signature.parameters['poll_max'].default == module.PRODUCTION_POLL_MAX
        assert signature.parameters['poll_interval'].default == module.PRODUCTION_POLL_INTERVAL


class TestCliCannotRelaxPollBounds:
    def test_given_poll_max_flag_when_parse_args_then_reject_as_unrecognized(self):
        with pytest.raises(SystemExit):
            module.parse_args([*base_argv(), '--poll-max', '999'])

    def test_given_poll_interval_flag_when_parse_args_then_reject_as_unrecognized(self):
        with pytest.raises(SystemExit):
            module.parse_args([*base_argv(), '--poll-interval', '0'])

    def test_given_no_poll_flags_when_parse_args_then_namespace_has_no_poll_attributes(self):
        args = module.parse_args(base_argv())

        assert not hasattr(args, 'poll_max')
        assert not hasattr(args, 'poll_interval')


class TestMainAlwaysUsesProductionPollBounds:
    def test_given_main_invoked_when_execute_update_branch_called_then_bounds_are_production_constants(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        captured: dict[str, object] = {}

        def fake_execute(request, *, gh_runner=None, sleep_fn=None, poll_max=None, poll_interval=None):
            captured['poll_max'] = poll_max
            captured['poll_interval'] = poll_interval
            return {
                'status': 'ok',
                'reason_code': None,
                'errors': [],
            }

        monkeypatch.setattr(module, 'execute_update_branch', fake_execute)

        exit_code = module.main(base_argv())

        assert exit_code == 0
        assert captured['poll_max'] == module.PRODUCTION_POLL_MAX
        assert captured['poll_interval'] == module.PRODUCTION_POLL_INTERVAL
