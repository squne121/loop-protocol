"""Issue #3055: Agent View cross-repo gateway の opt-in settings profile の focused regression。

新しい launcher 経路は追加しない。operator が `launch.sh --dry-run` の canonical env から
repo 外の絶対 path settings を生成して `claude agents --settings` へ渡す手順について、
(1) canonical 値との drift が無いこと、(2) default launcher が settings / permission mode を
注入しないこと、(3) secret が settings に入らないこと、(4) read-only evaluator が false-green
しないことを fake claude / 合成 job state で確認する。実 Agent View dispatch は検証しない
（runtime AC は agent_view_gateway_live_vc.py が担当し、実機が無ければ SKIP 77）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _launcher_harness as h  # noqa: E402
import agent_view_gateway_live_vc as ev  # noqa: E402

DOC = h.REPO_ROOT / "docs" / "dev" / "claude-gpt-runtime-prerequisites.md"
ROLE_KEYS = ev.ROLE_ENV_KEYS


def _lib_value(name: str) -> str:
    out = subprocess.run(
        ["sh", "-c", f'. "{h.LIB_SH}"; printf %s "${name}"'], capture_output=True, text=True, check=True
    )
    return out.stdout


def _dry_run_env(tmp_path: Path, **overrides) -> dict:
    proc = h.run_launcher(["--dry-run"], h.base_env(tmp_path, **overrides))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["launch_env"]


def _derive_settings(tmp_path: Path, **overrides) -> dict:
    """docs の手順 1 と同じ変換（dry-run の launch_env を {"env": ...} へ包む）。"""
    return {"env": _dry_run_env(tmp_path, **overrides)}


def test_argv_or_model_settings_env_reuses_lib_role_aliases(tmp_path):
    env = _derive_settings(tmp_path)["env"]
    expected = {
        "ANTHROPIC_MODEL": "CLAUDE_GPT_MODEL_MAIN",
        "ANTHROPIC_SMALL_FAST_MODEL": "CLAUDE_GPT_MODEL_SMALL_FAST",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "CLAUDE_GPT_MODEL_OPUS",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "CLAUDE_GPT_MODEL_SONNET",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "CLAUDE_GPT_MODEL_HAIKU",
    }
    assert set(expected) == set(ROLE_KEYS)
    for key, lib_name in expected.items():
        assert env[key] == _lib_value(lib_name)


def test_argv_or_model_settings_base_url_follows_connected_server(tmp_path):
    with h.FakeServer() as server:
        env = _derive_settings(tmp_path, ANTHROPIC_BASE_URL=server.url)["env"]
    assert env["ANTHROPIC_BASE_URL"] == server.url


def test_argv_or_model_default_launch_injects_no_settings_or_permission_mode(tmp_path):
    with h.FakeServer() as server:
        proc, observed, _ = h.run_launcher_with_fake_claude(tmp_path, server.url, ["agents"])
    assert proc.returncode == 0, proc.stderr
    assert observed["argv"] == ["agents"]


def test_argv_or_model_explicit_settings_path_passes_through_verbatim(tmp_path):
    settings = str(tmp_path / "agent-view-settings.json")
    with h.FakeServer() as server:
        proc, observed, _ = h.run_launcher_with_fake_claude(tmp_path, server.url, ["agents", "--settings", settings])
    assert proc.returncode == 0, proc.stderr
    assert observed["argv"] == ["agents", "--settings", settings]


def test_secret_or_settings_ambient_credentials_never_reach_derived_settings(tmp_path):
    sentinel = "sk-ant-SENTINEL-must-not-leak"
    settings = _derive_settings(
        tmp_path,
        ANTHROPIC_API_KEY=sentinel,
        ANTHROPIC_AUTH_TOKEN=sentinel,
        ANTHROPIC_CUSTOM_HEADERS=f"x-key: {sentinel}",
    )
    blob = json.dumps(settings)
    assert sentinel not in blob
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "unused"
    assert "ANTHROPIC_API_KEY" not in settings["env"]
    assert "ANTHROPIC_CUSTOM_HEADERS" not in settings["env"]


def test_secret_or_settings_base_url_must_stay_loopback(tmp_path):
    proc = h.run_launcher(
        ["--dry-run"], h.base_env(tmp_path, ANTHROPIC_BASE_URL="http://user:pw@gateway.example.com:443")
    )
    # dry-run は診断しないが、通常診断は non-loopback を exit 7 で拒否する（settings 化前の gate）。
    check = h.run_launcher(["--check-only"], h.base_env(tmp_path, ANTHROPIC_BASE_URL="http://gateway.example.com:443"))
    assert proc.returncode == 0
    assert check.returncode == 7


def test_secret_or_settings_doc_uses_absolute_path_not_inline_json():
    text = DOC.read_text(encoding="utf-8")
    start = text.index("Agent View cross-repo gateway")
    section = text[start : text.index("### 接続先 server の診断")]
    assert re.search(r'--settings "\$HOME/\.claude-gpt/[^"]+\.json"', section)
    assert not re.search(r"claude agents[^\n]*--settings\s+'?\{", section)
    assert "ANTHROPIC_API_KEY" not in section


def test_secret_or_settings_default_launcher_does_not_write_settings(tmp_path):
    with h.FakeServer() as server:
        proc, observed, env = h.run_launcher_with_fake_claude(tmp_path, server.url, ["agents"])
    assert proc.returncode == 0
    assert "--settings" not in observed["argv"]
    assert not list(Path(env["HOME"]).rglob("agent-view-settings.json"))


# --- read-only evaluator: false-green 防止 ---------------------------------------------------------


def _write_job(root: Path, job_id: str, cwd: Path, *, settings: bool, replies: list[dict], resumed: bool = False):
    session = f"{job_id}-0000-4000-8000-000000000000"
    flags = ["--model", "gpt-6-sol[1m]", "--permission-mode", "bypassPermissions"]
    if settings:
        flags = ["--settings", json.dumps({"env": {"ANTHROPIC_MODEL": "gpt-6-sol[1m]"}}), *flags]
    state = {
        "state": "done",
        "sessionId": session,
        "resumeSessionId": session,
        "cwd": str(cwd),
        "cliVersion": "test",
        "respawnFlags": flags,
        "providerEnv": {"ANTHROPIC_MODEL": "gpt-6-sol[1m]"},
        "firstTerminalAt": "2026-10-14T03:30:00Z",
        "lastTerminalAt": "2026-10-14T04:30:00Z" if resumed else "2026-10-14T03:30:00Z",
    }
    jobs = root / "jobs" / job_id
    jobs.mkdir(parents=True)
    (jobs / "state.json").write_text(json.dumps(state), encoding="utf-8")
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    proj = root / "projects" / enc
    proj.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "message": {"content": "Say MARKER_OK"}}]
    for r in replies:
        rows.append(
            {
                "type": "assistant",
                "isApiErrorMessage": r.get("err", False),
                "message": {
                    "model": r["model"],
                    "content": [{"type": "text", "text": r["text"]}],
                    "usage": {"output_tokens": r.get("tokens", 5)},
                },
            }
        )
    (proj / f"{session}.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")


def _env(root: Path, target: Path, **extra) -> dict:
    env = {
        "HOME": str(root),
        "CLAUDE_GPT_AV_JOBS_DIR": str(root / "jobs"),
        "CLAUDE_GPT_AV_PROJECTS_DIR": str(root / "projects"),
        "CLAUDE_GPT_AV_TARGET_CWD": str(target),
        "CLAUDE_GPT_AV_UI_DISPATCHED": "1",
        "CLAUDE_GPT_AV_EXPECT_TEXT": "MARKER_OK",
    }
    env.update(extra)
    return env


GOOD = {"model": "gpt-6-sol", "text": "MARKER_OK done"}
ERR = {"model": "<synthetic>", "text": "There's an issue with the selected model", "err": True, "tokens": 0}
ECHO = {"model": "gpt-6-sol", "text": "Say MARKER_OK"}


def test_live_vc_skips_77_without_ids_or_ui_attestation(tmp_path):
    code, receipt = ev.run("gateway-control-comparison", {"HOME": str(tmp_path)})
    assert code == 77 and receipt["status"] == "skip"
    code, _ = ev.run(
        "dispatched-child-and-resume",
        _env(tmp_path, tmp_path, CLAUDE_GPT_AV_CANDIDATE_ID="", CLAUDE_GPT_AV_UI_DISPATCHED="0"),
    )
    assert code == 77


def test_live_vc_skips_77_for_unknown_job_id(tmp_path):
    env = _env(tmp_path, tmp_path, CLAUDE_GPT_AV_CANDIDATE_ID="deadbeef", CLAUDE_GPT_AV_CONTROL_ID="cafebabe")
    code, _ = ev.run("gateway-control-comparison", env)
    assert code == 77


def test_live_vc_classifies_needed_when_only_candidate_responds(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[ERR])
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("gateway-control-comparison", env)
    assert (code, receipt["necessity"]) == (0, "needed")
    assert receipt["proxy_correlation"] == "unverified" and receipt["provider_route_claim"] == "none"


def test_live_vc_classifies_unnecessary_when_control_also_responds(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[GOOD])
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("gateway-control-comparison", env)
    assert (code, receipt["necessity"]) == (0, "unnecessary")


def test_live_vc_prompt_echo_and_swapped_arms_are_not_green(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[ERR])
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[ECHO])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("gateway-control-comparison", env)
    assert (code, receipt["necessity"]) == (1, "inconclusive")
    swapped = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="bbbbbbbb", CLAUDE_GPT_AV_CANDIDATE_ID="aaaaaaaa")
    assert ev.run("gateway-control-comparison", swapped)[0] == 1


def test_live_vc_wrong_cwd_is_inconclusive(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _write_job(tmp_path, "aaaaaaaa", other, settings=False, replies=[GOOD])
    _write_job(tmp_path, "bbbbbbbb", other, settings=True, replies=[GOOD])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("gateway-control-comparison", env)[0] == 1


def test_live_vc_resume_requires_two_valid_responses_after_terminal(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("dispatched-child-and-resume", env)[0] == 0
    _write_job(tmp_path, "cccccccc", target, settings=True, replies=[GOOD], resumed=False)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="cccccccc")
    assert ev.run("dispatched-child-and-resume", env)[0] == 1


def test_live_vc_receipt_has_no_raw_settings_or_urls(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[GOOD])
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    blob = json.dumps(ev.run("gateway-control-comparison", env)[1])
    assert "http://" not in blob and "AUTH_TOKEN" not in blob and "respawnFlags" not in blob


@pytest.mark.parametrize("scenario", ev.SCENARIOS)
def test_live_vc_cli_exits_77_without_runtime(tmp_path, scenario):
    proc = subprocess.run(
        [sys.executable, str(Path(ev.__file__)), "--scenario", scenario, "--artifact-dir", str(tmp_path / "art")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 77 and proc.stderr.startswith("SKIP: ")
