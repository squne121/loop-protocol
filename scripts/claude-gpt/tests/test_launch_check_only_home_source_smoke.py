"""scripts/claude-gpt/tests/test_launch_check_only_home_source_smoke.py

Issue #2803 AC6 (<!-- runtime-verification: true -->): hermetic regression test
that actually launches `launch.sh --check-only` as an external process (via the
existing shared hermetic test seam `_latitude_check_only_helper.py::
run_check_only()`) and observes, at the real launcher process-I/O boundary,
that the new `preflight.sh` provenance field `home_source` propagates into
`CLAUDE_GPT_LAUNCH_RESULT_V1.preflight.home_source` for both cases:

  1. `CLAUDE_GPT_HOME` unset/default -- canonical default `${HOME}/.claude-gpt`
     is used, and `.preflight.home_source == "default"`.
  2. explicit temp/stable override -- `CLAUDE_GPT_HOME=<isolated temp path>` is
     set explicitly, and `.preflight.home_source == "env_override"`. This case
     itself must not be treated as an error/failure (negative control: the
     `--check-only` invocation still exits 0 / `status: ok`).

Runtime Verification Applicability (per live Issue #2803):
  - decision: immediate, applicable_acs: [AC6]
  - profile: claude-gpt-process-io-smoke
  - assertion: external_process_exit_code_and_stdio_observed
  - This test observes only external process exit code / stdout of
    `launch.sh --check-only`; no live ChatGPT subscription network call is
    made or required (the fake proxy stands in for the real
    `claude-code-proxy` binary, matching the existing hermetic pattern used
    by the other `_latitude_check_only_helper.py` consumers).
  - No new harness/daemon/generic-telemetry/permanent-CI-required-gate is
    added; this reuses the existing shared hermetic seam.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2803_home_source", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_helper)

run_check_only = _helper.run_check_only


def test_home_source_default_propagates_via_real_launch_sh_check_only(tmp_path):
    """GIVEN CLAUDE_GPT_HOME が unset (canonical default codepath, isolated HOME)
    WHEN launch.sh --check-only を実 external process として起動する
    THEN CLAUDE_GPT_LAUNCH_RESULT_V1.preflight.home_source は "default" になる
    """
    result, _settings_path = run_check_only(tmp_path, use_default_claude_gpt_home=True)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    assert payload["status"] == "ok"
    assert payload["preflight"]["home_source"] == "default"


def test_home_source_env_override_propagates_and_is_not_an_error_negative_control(
    tmp_path,
):
    """GIVEN CLAUDE_GPT_HOME が明示的な isolated temp path に override されている
    WHEN launch.sh --check-only を実 external process として起動する
    THEN CLAUDE_GPT_LAUNCH_RESULT_V1.preflight.home_source は "env_override" になり、
         かつ override 自体が error/failure として扱われない (negative control:
         exit 0 / status: ok のまま)
    """
    result, _settings_path = run_check_only(tmp_path)  # default helper behavior: explicit override

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    # negative control: env_override alone must not turn this into a blocked/failed result
    assert payload["status"] == "ok"
    assert payload["preflight"]["home_source"] == "env_override"


def test_home_source_differs_between_default_and_override_same_boundary(tmp_path):
    """GIVEN 同一 launch.sh --check-only external process boundary
    WHEN default (unset) と explicit override の両方を実行する
    THEN 2 つの実行結果の home_source は異なり ("default" vs "env_override")、
         いずれも launch を error 扱いにしない (両方 status: ok)
    """
    default_run_dir = tmp_path / "default-run"
    override_run_dir = tmp_path / "override-run"
    default_run_dir.mkdir()
    override_run_dir.mkdir()

    default_result, _ = run_check_only(default_run_dir, use_default_claude_gpt_home=True)
    override_result, _ = run_check_only(override_run_dir)

    default_payload = json.loads(default_result.stdout)
    override_payload = json.loads(override_result.stdout)

    assert default_payload["status"] == "ok"
    assert override_payload["status"] == "ok"
    assert default_payload["preflight"]["home_source"] == "default"
    assert override_payload["preflight"]["home_source"] == "env_override"
    assert (
        default_payload["preflight"]["home_source"]
        != override_payload["preflight"]["home_source"]
    )
