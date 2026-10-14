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


def test_secret_or_settings_generation_gate_rejects_userinfo_and_non_loopback_urls(tmp_path):
    # docs 手順 1 は dry-run の前に `--check-only` を必須にする。dry-run 単体は診断しないため
    # credential 入り URL をそのまま出力する。gate（--check-only）が生成前に拒否することを固定する。
    for url in ("http://u:x@127.0.0.1:18765", "http://gateway.example.com:443", "http://127.0.0.1:1/v1"):
        dry = h.run_launcher(["--dry-run"], h.base_env(tmp_path, ANTHROPIC_BASE_URL=url))
        assert dry.returncode == 0  # dry-run は gate ではない
        check = h.run_launcher(["--check-only"], h.base_env(tmp_path, ANTHROPIC_BASE_URL=url))
        assert check.returncode == 7, url


def test_secret_or_settings_doc_requires_check_only_before_generation():
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("Agent View cross-repo gateway") : text.index("### 接続先 server の診断")]
    block = re.search(r"```bash\n(.*?)```", section, re.S).group(1)
    assert "--check-only" in block and "--dry-run" in block
    assert re.search(r"--check-only[^\n]*\n\s*&&\s*sh [^\n]*--dry-run", block)


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


def _gateway_env() -> dict:
    return {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18765", **ev.canonical_aliases()}


_UNSET = object()
HUMAN = {"kind": "human"}


def _write_job(
    root: Path,
    job_id: str,
    cwd: Path,
    *,
    settings: bool,
    replies: list[dict],
    resumed: bool = False,
    cli: str = "test",
    settings_env=_UNSET,
    settings_file: Path | None = None,
    perm_declared: str | None = "bypassPermissions",
    perm_events: tuple = ("bypassPermissions",),
    provider_env=_UNSET,
):
    session = f"{job_id}-0000-4000-8000-000000000000"
    flags = ["--model", "gpt-6-sol[1m]"]
    if perm_declared:
        flags += ["--permission-mode", perm_declared]
    if settings_file is not None:
        flags = ["--settings", str(settings_file), *flags]
    elif settings:
        env = _gateway_env() if settings_env is _UNSET else settings_env
        flags = ["--settings", json.dumps({"env": env}), *flags]
    state = {
        "state": "done",
        "sessionId": session,
        "resumeSessionId": session,
        "cwd": str(cwd),
        "cliVersion": cli,
        "respawnFlags": flags,
        "providerEnv": dict(ev.canonical_aliases()) if provider_env is _UNSET else provider_env,
        "firstTerminalAt": "2026-10-14T03:30:00Z",
        "lastTerminalAt": "2026-10-14T04:30:00Z" if resumed else "2026-10-14T03:30:00Z",
    }
    jobs = root / "jobs" / job_id
    jobs.mkdir(parents=True)
    (jobs / "state.json").write_text(json.dumps(state), encoding="utf-8")
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    proj = root / "projects" / enc
    proj.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "permission-mode", "permissionMode": m, "sessionId": session} for m in perm_events]
    rows.append({"type": "user", "origin": HUMAN, "message": {"content": "Say MARKER_OK"}})
    for n, r in enumerate(replies):
        if resumed and n == len(replies) - 1 and n > 0:
            rows.append({"type": "user", "origin": HUMAN, "message": {"content": "Say MARKER_OK again"}})
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


def _pair(tmp_path, control_replies, cand_replies, *, control_cli="test", cand_cli="test", **extra):
    target = tmp_path / "repo"
    target.mkdir(exist_ok=True)
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=control_replies, cli=control_cli)
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=cand_replies, cli=cand_cli)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb", **extra)
    return ev.run("gateway-control-comparison", env)


NATIVE = {"model": "claude-sonnet-4-5", "text": "MARKER_OK from Claude Max"}
RATE = {"model": "<synthetic>", "text": "API Error: 429 rate limit exceeded", "err": True, "tokens": 0}


def test_live_vc_native_model_responses_are_not_gateway_success(tmp_path):
    # 両 arm が Claude Max で応答しても `unnecessary` にしない。
    assert _pair(tmp_path, [NATIVE], [NATIVE])[1]["necessity"] == "inconclusive"


def test_live_vc_candidate_native_fallback_is_not_needed(tmp_path):
    assert _pair(tmp_path, [ERR], [NATIVE])[1]["necessity"] == "inconclusive"


def test_live_vc_control_native_fallback_with_gateway_candidate_is_needed(tmp_path):
    assert _pair(tmp_path, [NATIVE], [GOOD])[1]["necessity"] == "needed"


@pytest.mark.parametrize("bad_control", [[ECHO], [RATE]])
def test_live_vc_unrelated_control_failures_are_not_needed(tmp_path, bad_control):
    assert _pair(tmp_path, bad_control, [GOOD])[1]["necessity"] == "inconclusive"


def test_live_vc_cli_version_mismatch_is_inconclusive(tmp_path):
    assert _pair(tmp_path, [ERR], [GOOD], control_cli="1.0.0", cand_cli="2.0.0")[1]["necessity"] == "inconclusive"


def test_live_vc_missing_target_cwd_skips_77(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    env.pop("CLAUDE_GPT_AV_TARGET_CWD")
    assert ev.run("dispatched-child-and-resume", env)[0] == 77


def test_live_vc_resume_with_native_model_is_not_green(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[NATIVE, NATIVE], resumed=True)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("dispatched-child-and-resume", env)[0] == 1


def test_live_vc_missing_provider_env_is_not_canonical_match(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    state_path = tmp_path / "jobs" / "bbbbbbbb" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["providerEnv"] = {}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("dispatched-child-and-resume", env)[0] == 1


def _resume_env(tmp_path, **job_kwargs):
    target = tmp_path / "repo"
    target.mkdir(exist_ok=True)
    kwargs = {"settings": True, "replies": [GOOD, GOOD], "resumed": True, **job_kwargs}
    _write_job(tmp_path, "bbbbbbbb", target, **kwargs)
    return _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")


def test_live_vc_ac3_green_reports_unverified_assertions_explicitly(tmp_path):
    code, receipt = ev.run("dispatched-child-and-resume", _resume_env(tmp_path))
    assert code == 0
    a = receipt["assertions"]
    assert a["settings_content_inherited"] == a["permission_mode_effective"] == a["main_route_called"] == "observed"
    # 未実行の Haiku route・operator 自己申告の UI provenance・proxy 相関は PASS 扱いしない。
    assert a["haiku_route_called"] == a["ui_dispatch_provenance"] == a["proxy_request_correlation"] == "unverified"
    assert receipt["ui_dispatch_provenance"] == "operator_attested_only"
    assert receipt["candidate"]["settings_role_aliases_configured"]  # 設定値は設定値として別項目


def test_live_vc_haiku_route_observed_only_with_actual_luna_response(tmp_path):
    luna = {"model": "gpt-6-luna", "text": "MARKER_OK haiku"}
    _, receipt = ev.run("dispatched-child-and-resume", _resume_env(tmp_path, replies=[GOOD, luna]))
    assert receipt["assertions"]["haiku_route_called"] == "observed"


def test_live_vc_main_route_not_observed_when_only_haiku_responds(tmp_path):
    luna = {"model": "gpt-6-luna", "text": "MARKER_OK haiku"}
    assert ev.run("dispatched-child-and-resume", _resume_env(tmp_path, replies=[luna, luna]))[0] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"perm_events": ()},  # 宣言だけで実効 permission-mode event が無い
        {"perm_events": ("default",)},  # 宣言 bypassPermissions と実効が不一致
        {"perm_declared": None, "perm_events": ()},  # 宣言も観測も無い
    ],
)
def test_live_vc_declared_permission_mode_is_not_effective_mode(tmp_path, kwargs):
    assert ev.run("dispatched-child-and-resume", _resume_env(tmp_path, **kwargs))[0] == 1


def test_live_vc_settings_file_path_content_is_read_not_just_flag_presence(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"env": _gateway_env()}), encoding="utf-8")
    code, receipt = ev.run("dispatched-child-and-resume", _resume_env(tmp_path, settings_file=good))
    assert code == 0 and receipt["candidate"]["settings_source"] == "file"


@pytest.mark.parametrize(
    "content",
    [
        {"env": {}},  # gateway env を持たない file
        {"env": {**_gateway_env(), "ANTHROPIC_BASE_URL": "https://api.example.com"}},  # non-loopback
        {"env": {**_gateway_env(), "ANTHROPIC_MODEL": "claude-sonnet-4-5"}},  # alias が canonical と不一致
        None,  # file が存在しない
    ],
)
def test_live_vc_settings_file_without_valid_gateway_content_is_not_inherited(tmp_path, content):
    path = tmp_path / "bad.json"
    if content is not None:
        path.write_text(json.dumps(content), encoding="utf-8")
    code, receipt = ev.run("dispatched-child-and-resume", _resume_env(tmp_path, settings_file=path))
    assert code == 1 and receipt["assertions"]["settings_content_inherited"] == "failed"


def test_live_vc_candidate_inline_settings_with_native_aliases_is_inconclusive(tmp_path):
    bad_env = {**_gateway_env(), "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"}
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[ERR])
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD], settings_env=bad_env)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("gateway-control-comparison", env)[1]["necessity"] == "inconclusive"


def test_live_vc_control_without_gateway_env_is_not_a_valid_h1_control(tmp_path):
    # supervisor に gateway が全く無い control は native fallback が自明に出る。needed の根拠にしない。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[NATIVE], provider_env={})
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("gateway-control-comparison", env)[1]["necessity"] == "inconclusive"


def test_live_vc_arms_must_share_permission_mode_and_child_cwd(tmp_path):
    target = tmp_path / "repo"
    other = target / "sub-b"
    other.mkdir(parents=True)
    (target / "sub-a").mkdir()
    base = {"replies": [GOOD]}
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[ERR], perm_events=("default",))
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, **base)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("gateway-control-comparison", env)[1]["necessity"] == "inconclusive"
    _write_job(tmp_path, "cccccccc", target / "sub-a", settings=False, replies=[ERR])
    _write_job(tmp_path, "dddddddd", other, settings=True, replies=[GOOD])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="cccccccc", CLAUDE_GPT_AV_CANDIDATE_ID="dddddddd")
    assert ev.run("gateway-control-comparison", env)[1]["necessity"] == "inconclusive"


def test_live_vc_expect_text_is_required(tmp_path):
    env = _resume_env(tmp_path)
    env.pop("CLAUDE_GPT_AV_EXPECT_TEXT")
    assert ev.run("dispatched-child-and-resume", env)[0] == 77


def test_live_vc_resume_needs_a_response_after_a_second_user_turn(tmp_path):
    # terminal 時刻だけが進み、再開後の user turn / 応答が無い transcript は resume の証拠にならない。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=False)
    state_path = tmp_path / "jobs" / "bbbbbbbb" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["lastTerminalAt"] = "2026-10-14T04:30:00Z"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("dispatched-child-and-resume", env)[0] == 1


@pytest.mark.parametrize("extra", [{"ANTHROPIC_API_KEY": "k"}, {"ANTHROPIC_AUTH_TOKEN": "real-looking-value"}])
def test_live_vc_settings_carrying_real_credentials_are_rejected(tmp_path, extra):
    env = _resume_env(tmp_path, settings_env={**_gateway_env(), **extra})
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1 and "real-looking-value" not in json.dumps(receipt)


def _append_rows(root: Path, job_id: str, cwd: Path, rows: list[dict]):
    session = f"{job_id}-0000-4000-8000-000000000000"
    path = root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{session}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write("\n" + json.dumps(r))


def _assistant(text="MARKER_OK done", model="gpt-6-sol"):
    return {
        "type": "assistant",
        "message": {"model": model, "content": [{"type": "text", "text": text}], "usage": {"output_tokens": 5}},
    }


TOOL_RESULT = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "d87d667"}]},
}


def test_live_vc_tool_result_rows_are_not_a_second_user_turn(tmp_path):
    # 1 回の user prompt の中で tool_use -> tool_result -> 応答しても、resume 後の応答ではない。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD], resumed=False)
    _append_rows(tmp_path, "bbbbbbbb", target, [TOOL_RESULT, _assistant()])
    state_path = tmp_path / "jobs" / "bbbbbbbb" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["lastTerminalAt"] = "2026-10-14T04:30:00Z"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1 and receipt["candidate"]["response_after_second_user_turn"] is False


@pytest.mark.parametrize("flag", ["isMeta", "isSidechain"])
def test_live_vc_meta_and_sidechain_user_rows_are_not_user_turns(tmp_path, flag):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD], resumed=False)
    _append_rows(
        tmp_path,
        "bbbbbbbb",
        target,
        [{"type": "user", flag: True, "origin": HUMAN, "message": {"content": "injected"}}, _assistant()],
    )
    state_path = tmp_path / "jobs" / "bbbbbbbb" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["lastTerminalAt"] = "2026-10-14T04:30:00Z"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("dispatched-child-and-resume", env)[0] == 1


def test_live_vc_real_second_user_prompt_after_tool_use_is_resume_evidence(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD], resumed=True)
    _append_rows(
        tmp_path,
        "bbbbbbbb",
        target,
        [TOOL_RESULT, {"type": "user", "origin": HUMAN, "message": {"content": "Say MARKER_OK again"}}, _assistant()],
    )
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("dispatched-child-and-resume", env)[0] == 0


@pytest.mark.parametrize(
    "row",
    [
        {
            "type": "user",
            "origin": {"kind": "task-notification"},
            "message": {"content": "<task-notification>x</task-notification>"},
        },
        {"type": "user", "message": {"content": "<command-name>/exit</command-name>"}},
        {"type": "user", "message": {"content": "<local-command-stdout>ok</local-command-stdout>"}},
    ],
)
def test_live_vc_non_human_user_rows_are_not_user_turns(tmp_path, row):
    # background task 通知や slash command 出力が 2 回目の user turn に数えられて resume 証拠にならない。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD], resumed=True)
    _append_rows(tmp_path, "bbbbbbbb", target, [row, _assistant()])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1 and receipt["candidate"]["response_after_second_user_turn"] is False


def test_live_vc_cli_version_falls_back_to_transcript_version(tmp_path):
    # 実 job state の control は cliVersion を持たないことがある。transcript の version を使う。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[ERR], cli=None)
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD], cli=None)
    for jid in ("aaaaaaaa", "bbbbbbbb"):
        _append_rows(tmp_path, jid, target, [{"type": "system", "version": "2.1.299"}])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("gateway-control-comparison", env)[1]["necessity"] == "needed"
