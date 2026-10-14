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
import os
import re
import shutil
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
# resume 境界の合成時刻: 初回 probe -> 初回応答 -> first terminal(T1) -> 2 回目 probe(P2) -> 応答 -> last terminal。
T_FIRST_PROBE = "2026-10-14T03:00:00.000Z"
T_PRE_REPLY = "2026-10-14T03:10:00.000Z"
T_FIRST_TERMINAL = "2026-10-14T03:30:00.000Z"
T_SECOND_PROBE = "2026-10-14T04:00:00.000Z"
T_POST_REPLY = "2026-10-14T04:10:00.000Z"
T_LAST_TERMINAL = "2026-10-14T04:30:00.000Z"


def _assistant_row(r: dict, ts: str | None = None) -> dict:
    row = {
        "type": "assistant",
        "isApiErrorMessage": r.get("err", False),
        "message": {
            "model": r["model"],
            "content": [{"type": "text", "text": r["text"]}],
            "usage": {"output_tokens": r.get("tokens", 5)},
        },
    }
    if ts:
        row["timestamp"] = ts
    return row


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
    model_flag: str = "gpt-6-sol[1m]",
    prompt: str = "Say MARKER_OK",
    job_state: str = "done",
    timeline=_UNSET,
    row_cwd: Path | None = None,
    pre_ts: str | None = T_PRE_REPLY,
):
    session = f"{job_id}-0000-4000-8000-000000000000"
    flags = ["--model", model_flag]
    if perm_declared:
        flags += ["--permission-mode", perm_declared]
    if settings_file is not None:
        flags = ["--settings", str(settings_file), *flags]
    elif settings:
        env = _gateway_env() if settings_env is _UNSET else settings_env
        flags = ["--settings", json.dumps({"env": env}), *flags]
    state = {
        "state": job_state,
        "sessionId": session,
        "resumeSessionId": session,
        "cwd": str(cwd),
        "cliVersion": cli,
        "respawnFlags": flags,
        "providerEnv": dict(ev.canonical_aliases()) if provider_env is _UNSET else provider_env,
        "firstTerminalAt": T_FIRST_TERMINAL,
        "lastTerminalAt": T_LAST_TERMINAL if resumed else T_FIRST_TERMINAL,
    }
    jobs = root / "jobs" / job_id
    jobs.mkdir(parents=True)
    (jobs / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if timeline is _UNSET:
        # 実 job と同じ `{at, state}` 行。resumed の場合は first terminal の後に再度 working -> done になる。
        timeline = [(T_FIRST_PROBE, "working"), (T_FIRST_TERMINAL, "done")]
        if resumed:
            timeline += [(T_SECOND_PROBE, "working"), (T_LAST_TERMINAL, "done")]
    if timeline is not None:
        (jobs / "timeline.jsonl").write_text(
            "\n".join(json.dumps({"at": at, "state": st, "detail": "", "text": ""}) for at, st in timeline),
            encoding="utf-8",
        )
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    proj = root / "projects" / enc
    proj.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "permission-mode", "permissionMode": m, "sessionId": session} for m in perm_events]
    rows.append({"type": "user", "origin": HUMAN, "timestamp": T_FIRST_PROBE, "message": {"content": prompt}})
    for n, r in enumerate(replies):
        post = resumed and n == len(replies) - 1 and n > 0
        if post:
            rows.append(
                {
                    "type": "user",
                    "origin": HUMAN,
                    "timestamp": T_SECOND_PROBE,
                    "message": {"content": f"{prompt} again"},
                }
            )
        rows.append(_assistant_row(r, T_POST_REPLY if post else pre_ts))
    if row_cwd is not None:
        rows = [{**x, "cwd": str(row_cwd)} for x in rows]
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
    return _assistant_row({"model": model, "text": text})


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
        [
            TOOL_RESULT,
            {
                "type": "user",
                "origin": HUMAN,
                "timestamp": T_SECOND_PROBE,
                "message": {"content": "Say MARKER_OK again"},
            },
            _assistant(),
        ],
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


_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t.invalid", *args],
        check=True,
        capture_output=True,
        env=env,
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "--allow-empty", "-q", "-m", "init")
    return path


def _relocate(tmp_path: Path, cwd: Path, name: str = "-relocated-worktree") -> None:
    src = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    src.rename(tmp_path / "projects" / name)


def test_live_vc_transcript_relocated_to_linked_worktree_of_same_repository_is_accepted(tmp_path):
    target = _git_repo(tmp_path / "repo")
    worktree = tmp_path / "linked-wt"
    _git(target, "worktree", "add", "-q", "-b", "wt-branch", str(worktree))
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True, row_cwd=worktree)
    _relocate(tmp_path, target)
    code, receipt = ev.run("dispatched-child-and-resume", _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb"))
    assert code == 0 and receipt["candidate"]["transcript_identity"] == "same_repository"


def test_live_vc_transcript_relocated_to_primary_checkout_of_same_repository_is_accepted(tmp_path):
    target = _git_repo(tmp_path / "repo")
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True, row_cwd=target)
    _relocate(tmp_path, target)
    code, _ = ev.run("dispatched-child-and-resume", _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb"))
    assert code == 0


def test_live_vc_transcript_relocated_to_foreign_repository_is_rejected(tmp_path):
    # job state の cwd は target 配下だが、同一 session id の transcript の実 cwd は別 Git repository。
    target = _git_repo(tmp_path / "repo")
    foreign = _git_repo(tmp_path / "foreign-repo")
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True, row_cwd=foreign)
    _relocate(tmp_path, target, "-foreign-repo")
    code, receipt = ev.run("dispatched-child-and-resume", _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb"))
    cand = receipt["candidate"]
    assert code == 1 and cand["child_cwd_in_target"] is False
    assert cand["transcript_identity"] == "foreign_repository"


def test_live_vc_transcript_relocated_without_cwd_field_is_not_guessed(tmp_path):
    target = _git_repo(tmp_path / "repo")
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    _relocate(tmp_path, target)
    code, receipt = ev.run("dispatched-child-and-resume", _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb"))
    assert code == 1 and receipt["candidate"]["transcript_identity"] == "cwd_missing"
    assert receipt["candidate"]["child_cwd_in_target"] is False


@pytest.mark.parametrize("which", ["missing_dir", "not_a_git_repo"])
def test_live_vc_transcript_relocated_with_unresolvable_git_identity_is_not_guessed(tmp_path, which):
    target = _git_repo(tmp_path / "repo")
    row_cwd = tmp_path / "gone" if which == "missing_dir" else tmp_path / "plain-dir"
    if which == "not_a_git_repo":
        row_cwd.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True, row_cwd=row_cwd)
    _relocate(tmp_path, target)
    code, receipt = ev.run("dispatched-child-and-resume", _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb"))
    assert code == 1 and receipt["candidate"]["transcript_identity"] == "git_identity_unavailable"


def test_live_vc_transcript_relocated_with_non_git_target_is_not_guessed(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True, row_cwd=target)
    _relocate(tmp_path, target)
    code, receipt = ev.run("dispatched-child-and-resume", _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb"))
    assert code == 1 and receipt["candidate"]["transcript_identity"] == "git_identity_unavailable"


def test_live_vc_ambiguous_relocated_transcript_is_not_guessed(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    src = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(target))
    for name in ("-copy-a", "-copy-b"):
        (tmp_path / "projects" / name).mkdir()
        for f in src.iterdir():
            (tmp_path / "projects" / name / f.name).write_bytes(f.read_bytes())

    for f in list(src.iterdir()):
        f.unlink()
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    assert ev.run("dispatched-child-and-resume", env)[0] == 1


# --- F1: resume 境界（terminal -> その後の human probe）の観測 ------------------------------------------


def _resume_receipt(tmp_path, **job_kwargs):
    code, receipt = ev.run("dispatched-child-and-resume", _resume_env(tmp_path, **job_kwargs))
    return code, receipt, receipt["candidate"]["resume"]


def test_live_vc_resume_boundary_observed_with_pre_and_post_probe_and_latest_success(tmp_path):
    code, receipt, resume = _resume_receipt(tmp_path)
    assert code == 0 and resume == {"status": "observed", "reason": "boundary_and_probes_observed"}
    a = receipt["assertions"]
    assert a["resume_boundary"] == a["two_turn_response"] == "observed"
    # process の再起動・settings 再読込そのものは観測できない。PASS でも常に unverified。
    assert a["process_restart_observed"] == "unverified"


def test_live_vc_resume_two_human_prompts_in_one_run_without_terminal_is_not_resume(tmp_path):
    # (a) timeline に T1 terminal が無い: 同一実行中 session で human prompt を 2 回送っただけ。
    timeline = [(T_FIRST_PROBE, "working"), (T_SECOND_PROBE, "working")]
    code, receipt, resume = _resume_receipt(tmp_path, timeline=timeline, job_state="working")
    assert code == 1 and resume["status"] == "unverified" and resume["reason"] == "no_terminal_in_timeline"
    assert receipt["observed_postcondition"] is False


def test_live_vc_resume_terminal_after_both_probes_is_not_a_boundary(tmp_path):
    # terminal 行が両 probe より後にしか無い（途中に終端していない）。
    timeline = [(T_FIRST_PROBE, "working"), (T_SECOND_PROBE, "working"), (T_LAST_TERMINAL, "done")]
    code, _, resume = _resume_receipt(tmp_path, timeline=timeline)
    assert code == 1 and resume["reason"] == "no_human_turn_after_terminal"


def test_live_vc_resume_first_api_error_then_later_successes_is_not_resume(tmp_path):
    # (b) 初回 API error、2 回目以降に成功応答 2 行。旧判定（有効応答 2 件 + 2 回目 turn 後の応答）は通ってしまう。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[ERR, GOOD], resumed=True)
    _append_rows(tmp_path, "bbbbbbbb", target, [_assistant()])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    cand = receipt["candidate"]
    assert cand["valid_assistant_responses"] == 2 and cand["response_after_second_user_turn"] is True
    assert code == 1 and cand["resume"] == {"status": "failed", "reason": "no_valid_response_pre_boundary"}


@pytest.mark.parametrize("job_state", ["done", "failed"])
def test_live_vc_resume_latest_probe_failure_after_earlier_success_is_not_resume(tmp_path, job_state):
    # (c) 過去成功済みだが最新 resume probe が失敗（最新 assistant 応答が API error / job failed）。
    target = tmp_path / "repo"
    target.mkdir()
    timeline = [
        (T_FIRST_PROBE, "working"),
        (T_FIRST_TERMINAL, "done"),
        (T_SECOND_PROBE, "working"),
        (T_LAST_TERMINAL, job_state),
    ]
    _write_job(
        tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True, job_state=job_state,
        timeline=timeline,
    )  # fmt: skip
    _append_rows(tmp_path, "bbbbbbbb", target, [_assistant_row(ERR, "2026-10-14T04:20:00.000Z")])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1 and receipt["candidate"]["resume"] == {"status": "failed", "reason": "latest_response_not_valid"}
    assert receipt["assertions"]["resume_boundary"] == "failed"


def test_live_vc_resume_job_still_working_with_only_past_responses_is_not_resume(tmp_path):
    # (d) job がまだ working で過去の応答だけ。最新 probe の完了を観測していない。
    timeline = [(T_FIRST_PROBE, "working"), (T_FIRST_TERMINAL, "done"), (T_SECOND_PROBE, "working")]
    code, _, resume = _resume_receipt(tmp_path, job_state="working", timeline=timeline)
    assert code == 1 and resume == {"status": "failed", "reason": "job_final_state_not_done"}


def test_live_vc_resume_timeline_state_working_even_if_state_json_done_is_not_resume(tmp_path):
    timeline = [(T_FIRST_PROBE, "working"), (T_FIRST_TERMINAL, "done"), (T_SECOND_PROBE, "working")]
    code, _, resume = _resume_receipt(tmp_path, job_state="done", timeline=timeline)
    assert code == 1 and resume["reason"] == "job_final_state_not_done"


def test_live_vc_resume_last_terminal_not_after_probe_is_not_resume(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    state_path = tmp_path / "jobs" / "bbbbbbbb" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["lastTerminalAt"] = T_FIRST_TERMINAL
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1 and receipt["candidate"]["resume"]["reason"] == "last_terminal_not_after_probe"


@pytest.mark.parametrize("timeline", [None, []])
def test_live_vc_resume_boundary_unobservable_is_unverified_not_success(tmp_path, timeline):
    # (e) timeline 欠落（または空）で resume 境界を観測できない。応答が揃っていても成功にしない。
    code, receipt, resume = _resume_receipt(tmp_path, timeline=timeline)
    reason = "timeline_missing" if timeline is None else "no_terminal_in_timeline"
    assert code == 1 and resume == {"status": "unverified", "reason": reason}
    a = receipt["assertions"]
    assert a["resume_boundary"] == "unverified" and receipt["observed_postcondition"] is False
    assert a["two_turn_response"] == "observed"  # 2 turn 応答と resume 境界は別 assertion
    assert a["process_restart_observed"] == "unverified"


def test_live_vc_resume_does_not_require_same_session_id(tmp_path):
    # Claude Code の resume では session id が変わる場合がある。resume_same_session を AND に戻さない。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    state_path = tmp_path / "jobs" / "bbbbbbbb" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["resumeSessionId"] = "ffffffff-0000-4000-8000-000000000000"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    code, receipt = ev.run("dispatched-child-and-resume", _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb"))
    assert code == 0 and receipt["candidate"]["resume_same_session"] is False


# --- F2: 2 arm 因果比較の対応条件 -----------------------------------------------------------------------


def _pair_job_kwargs(tmp_path, control: dict, candidate: dict):
    target = tmp_path / "repo"
    target.mkdir(exist_ok=True)
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, **control)
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, **candidate)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    return ev.run("gateway-control-comparison", env)


def test_live_vc_control_only_nonexistent_model_flag_is_not_needed(tmp_path):
    # (1) control だけ `--model gpt-NONEXISTENT`（model access error）。2 arm の実 request model が異なる。
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": [ERR], "model_flag": "gpt-NONEXISTENT"}, {"replies": [GOOD]})
    assert (code, receipt["necessity"]) == (1, "inconclusive")


def test_live_vc_different_first_probe_prompt_is_inconclusive(tmp_path):
    # (2) 最初の human probe の prompt が異なる。
    code, receipt = _pair_job_kwargs(
        tmp_path, {"replies": [ERR]}, {"replies": [GOOD], "prompt": "Say MARKER_OK differently"}
    )
    assert (code, receipt["necessity"]) == (1, "inconclusive")


def test_live_vc_prompt_whitespace_only_difference_is_still_the_same_probe(tmp_path):
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": [ERR]}, {"replies": [GOOD], "prompt": "Say   MARKER_OK "})
    assert (code, receipt["necessity"]) == (0, "needed")


def test_live_vc_candidate_first_run_failure_with_later_turn_success_is_not_needed(tmp_path):
    # (3) candidate の初回が失敗し、追加 turn だけ成功。初回区間の応答だけを比較する。
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": [ERR]}, {"replies": [ERR, GOOD], "resumed": True})
    assert receipt["candidate"]["valid_assistant_responses"] == 1  # 全体では有効応答が 1 件ある
    assert receipt["candidate"]["first_run_valid_responses"] == 0
    assert (code, receipt["necessity"]) == (1, "inconclusive")


UNRELATED_API_ERROR = {"model": "<synthetic>", "text": "API Error: 500 upstream failure", "err": True, "tokens": 0}


@pytest.mark.parametrize("bad_control", [[ECHO], [RATE], [UNRELATED_API_ERROR]])
def test_live_vc_unrelated_control_first_run_failures_are_not_needed_causal(tmp_path, bad_control):
    # (4) prompt echo / rate limit / 無関係 API error は gateway 継承の failure と誤診しない。
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": bad_control}, {"replies": [GOOD]})
    assert (code, receipt["necessity"]) == (1, "inconclusive")


def test_live_vc_control_gateway_failure_only_after_first_run_is_not_needed(tmp_path):
    # control の gateway failure が追加 turn にしか無く、初回区間は prompt echo。
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": [ECHO, ERR], "resumed": True}, {"replies": [GOOD]})
    assert (code, receipt["necessity"]) == (1, "inconclusive")


def test_live_vc_same_conditions_with_control_response_is_unnecessary(tmp_path):
    # (5) 同一条件で control も応答。
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": [GOOD]}, {"replies": [GOOD]})
    assert (code, receipt["necessity"]) == (0, "unnecessary")


@pytest.mark.parametrize("failure", [ERR, NATIVE])
def test_live_vc_valid_control_gateway_failure_with_candidate_first_run_success_is_needed(tmp_path, failure):
    # (6) 同一 model / prompt で control の初回が gateway 未適用の failure、candidate の初回が成功。
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": [failure]}, {"replies": [GOOD]})
    assert (code, receipt["necessity"]) == (0, "needed")
    assert receipt["proxy_correlation"] == "unverified" and receipt["provider_route_claim"] == "none"


def test_live_vc_arm_without_first_run_evidence_is_inconclusive(tmp_path):
    # 初回区間に有効応答も failure class も無い（assistant 応答が無い）arm は比較できない。
    code, receipt = _pair_job_kwargs(tmp_path, {"replies": []}, {"replies": [GOOD]})
    assert (code, receipt["necessity"]) == (1, "inconclusive")


# --- F4: operator 文書の evaluator 実行 cwd --------------------------------------------------------------


def _doc_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    return text[text.index("実機 2 arm 対照の operator 手順") : text.index("### 接続先 server の診断")]


def _doc_evaluator_commands() -> list[str]:
    return [
        line.strip()
        for line in _doc_section().splitlines()
        if "agent_view_gateway_live_vc.py" in line and line.strip().startswith("uv run")
    ]


def test_doc_evaluator_commands_run_from_absolute_repo_directory():
    commands = _doc_evaluator_commands()
    assert len(commands) == 2
    assert {re.search(r"--scenario (\S+)", c).group(1) for c in commands} == set(ev.SCENARIOS)
    for command in commands:
        assert re.search(r'uv run --directory "\$REPO" --locked python3 scripts/claude-gpt/tests/', command), command
    section = _doc_section()
    assert re.search(r"^REPO=\S", section, re.M)  # REPO を絶対 path の変数として保持する
    assert "<repo>" not in section  # 未定義 placeholder で launcher を指さない
    assert 'sh "$REPO/scripts/claude-gpt/launch.sh"' in section


def _run_doc_command(command: str, cwd: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_GPT_AV_")}
    env["REPO"] = str(h.REPO_ROOT)
    return subprocess.run(
        ["sh", "-c", f'{command} --artifact-dir "{tmp_path / "art"}"'],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to run the documented command")
@pytest.mark.parametrize("scenario", ev.SCENARIOS)
def test_doc_evaluator_command_from_parent_directory_skips_77_not_exit_2(tmp_path, scenario):
    # operator が `cd ~/projects`（本 repository の親）へ移動した後でも evaluator が起動し、env 未設定で SKIP 77。
    parent = tmp_path / "projects"
    parent.mkdir()
    command = next(c for c in _doc_evaluator_commands() if f"--scenario {scenario}" in c)
    proc = _run_doc_command(command, parent, tmp_path)
    assert proc.returncode == 77, proc.stderr
    assert proc.stderr.startswith("SKIP: ")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to run the documented command")
def test_doc_evaluator_command_without_directory_fails_from_parent_directory(tmp_path):
    # 負例: `--directory "$REPO"` が無いと相対 path を親から探して exit 2 になる（誤った文書の再発を固定する）。
    parent = tmp_path / "projects"
    parent.mkdir()
    command = _doc_evaluator_commands()[0].replace('--directory "$REPO" ', "")
    assert "--directory" not in command
    proc = _run_doc_command(command, parent, tmp_path)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)


# --- iteration 1: blocked は resume 境界でない / P2 束縛 / 初回区間 / 非 dict 行 / cwd 正規化 ---------------


def test_live_vc_resume_blocked_state_is_not_a_terminal_boundary(tmp_path):
    # B1: `blocked` は入力待ちで process が生存している。人間の返信が resume 成功になってはならない。
    timeline = [
        (T_FIRST_PROBE, "working"),
        (T_FIRST_TERMINAL, "blocked"),
        (T_SECOND_PROBE, "working"),
        (T_LAST_TERMINAL, "done"),
    ]
    code, receipt, resume = _resume_receipt(tmp_path, timeline=timeline)
    assert code == 1 and resume["status"] == "unverified"
    assert receipt["assertions"]["resume_boundary"] == "unverified"
    assert receipt["observed_postcondition"] is False


@pytest.mark.parametrize("terminal", ["done", "failed", "stopped"])
def test_live_vc_resume_done_failed_stopped_are_boundary_terminals(tmp_path, terminal):
    timeline = [
        (T_FIRST_PROBE, "working"),
        (T_FIRST_TERMINAL, terminal),
        (T_SECOND_PROBE, "working"),
        (T_LAST_TERMINAL, "done"),
    ]
    code, _, resume = _resume_receipt(tmp_path, timeline=timeline)
    assert code == 0 and resume["status"] == "observed"


def test_live_vc_resume_failed_second_probe_then_successful_third_probe_is_not_resume(tmp_path):
    # W1: P2 に対する応答が失敗し、後続の P3 で成功した場合は post-boundary の成功にしない。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, ERR], resumed=True)
    third_probe = {
        "type": "user",
        "origin": HUMAN,
        "timestamp": "2026-10-14T04:20:00.000Z",
        "message": {"content": "retry"},
    }
    _append_rows(tmp_path, "bbbbbbbb", target, [third_probe, _assistant_row(GOOD, "2026-10-14T04:25:00.000Z")])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1
    assert receipt["candidate"]["resume"] == {"status": "failed", "reason": "no_valid_response_post_boundary"}


def test_live_vc_first_run_is_cut_at_the_next_human_turn(tmp_path):
    # W2: candidate の初回が失敗し追加 turn だけ成功。terminal の時刻情報が無くても、次の human turn で初回区間を切る。
    code, receipt = _pair_job_kwargs(
        tmp_path, {"replies": [ERR]}, {"replies": [ERR, GOOD], "resumed": True, "timeline": None}
    )
    assert receipt["candidate"]["first_run_valid_responses"] == 0
    assert (code, receipt["necessity"]) == (1, "inconclusive")


def test_live_vc_first_run_is_cut_at_the_first_terminal(tmp_path):
    # W2: 次の human turn が無くても、最初の terminal より後の応答は初回区間に混入させない。
    late_success = _assistant_row(GOOD, "2026-10-14T03:40:00.000Z")
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[ERR])
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[ERR])
    _append_rows(tmp_path, "bbbbbbbb", target, [late_success])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("gateway-control-comparison", env)
    assert receipt["candidate"]["first_run_valid_responses"] == 0
    assert (code, receipt["necessity"]) == (1, "inconclusive")


def test_live_vc_first_run_continues_across_blocked_state_without_a_human_turn(tmp_path):
    # `blocked` は承認待ち等で human turn 無しに継続し得るため、初回区間の終端にしない。
    target = tmp_path / "repo"
    target.mkdir()
    timeline = [
        (T_FIRST_PROBE, "working"),
        ("2026-10-14T03:20:00.000Z", "blocked"),
        ("2026-10-14T03:21:00.000Z", "working"),
        (T_FIRST_TERMINAL, "done"),
    ]
    _write_job(tmp_path, "aaaaaaaa", target, settings=False, replies=[ERR])
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[ERR], timeline=timeline)
    _append_rows(tmp_path, "bbbbbbbb", target, [_assistant_row(GOOD, "2026-10-14T03:25:00.000Z")])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CONTROL_ID="aaaaaaaa", CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("gateway-control-comparison", env)
    assert (code, receipt["necessity"]) == (0, "needed")


def test_live_vc_non_dict_json_transcript_lines_are_ignored(tmp_path):
    # W3: `123` や `[1]` のような dict 以外の JSON 行で AttributeError にならず、無視される。
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    _append_rows(tmp_path, "bbbbbbbb", target, [123, [1], "text", None])
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 0 and receipt["candidate"]["resume"]["status"] == "observed"


@pytest.mark.parametrize("kind", ["dotdot", "symlink"])
def test_live_vc_job_cwd_escaping_target_via_dotdot_or_symlink_is_not_in_target(tmp_path, kind):
    # W4: 文字列 prefix が target 配下でも、`..` / symlink を正規化すると別 directory を指す cwd は受理しない。
    target = tmp_path / "repo"
    foreign = tmp_path / "foreign"
    target.mkdir()
    foreign.mkdir()
    if kind == "dotdot":
        cwd = target / ".." / "foreign"
    else:
        cwd = target / "link"
        cwd.symlink_to(foreign, target_is_directory=True)
    assert str(cwd).startswith(str(target) + "/")
    _write_job(tmp_path, "bbbbbbbb", cwd, settings=True, replies=[GOOD, GOOD], resumed=True)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1 and receipt["candidate"]["child_cwd_in_target"] is False


def test_live_vc_job_cwd_in_real_subdirectory_of_target_is_in_target(tmp_path):
    target = tmp_path / "repo"
    sub = target / "sub"
    sub.mkdir(parents=True)
    _write_job(tmp_path, "bbbbbbbb", sub, settings=True, replies=[GOOD, GOOD], resumed=True)
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 0 and receipt["candidate"]["child_cwd_in_target"] is True


# --- iteration 2: pre-boundary の成功を時刻で T1 に束縛する・等号側の境界値 --------------------------------


def test_live_vc_resume_terminal_before_first_valid_response_is_not_resume(tmp_path):
    # W-a: terminal(03:05) が最初の有効応答(03:10)より前。T1 より後の応答は pre-boundary の成功ではない。
    timeline = [
        (T_FIRST_PROBE, "working"),
        ("2026-10-14T03:05:00.000Z", "failed"),
        (T_SECOND_PROBE, "working"),
        (T_LAST_TERMINAL, "done"),
    ]
    code, _, resume = _resume_receipt(tmp_path, timeline=timeline)
    assert code == 1 and resume == {"status": "failed", "reason": "no_valid_response_pre_boundary"}


def test_live_vc_resume_pre_boundary_response_without_timestamp_is_unverified(tmp_path):
    code, receipt, resume = _resume_receipt(tmp_path, pre_ts=None)
    assert code == 1 and resume == {"status": "unverified", "reason": "pre_boundary_response_timestamp_missing"}
    assert receipt["assertions"]["resume_boundary"] == "unverified"


def test_live_vc_resume_pre_boundary_response_before_first_probe_time_is_not_resume(tmp_path):
    # 応答の時刻が P1 より前（時計の不整合）。P1 以後でなければ束縛を証明できない。
    code, _, resume = _resume_receipt(tmp_path, pre_ts="2026-10-14T02:59:59.000Z")
    assert code == 1 and resume["reason"] == "no_valid_response_pre_boundary"


def test_live_vc_resume_pre_boundary_response_exactly_at_terminal_is_accepted(tmp_path):
    # 等号側: 応答時刻 == T1 は pre-boundary（T1 以下）。
    code, _, resume = _resume_receipt(tmp_path, pre_ts=T_FIRST_TERMINAL)
    assert code == 0 and resume["status"] == "observed"


def test_live_vc_resume_pre_boundary_response_just_after_terminal_is_not_resume(tmp_path):
    code, _, resume = _resume_receipt(tmp_path, pre_ts="2026-10-14T03:30:00.001Z")
    assert code == 1 and resume["reason"] == "no_valid_response_pre_boundary"


def test_live_vc_resume_pre_boundary_response_exactly_at_first_probe_is_accepted(tmp_path):
    code, _, resume = _resume_receipt(tmp_path, pre_ts=T_FIRST_PROBE)
    assert code == 0 and resume["status"] == "observed"


def test_live_vc_resume_terminal_at_same_instant_as_first_probe_is_the_boundary(tmp_path):
    # 等号側: terminal が P1 と同時刻でも T1 である（`at >= p1_ts`）。その後の応答 (03:10) は pre-boundary でない。
    timeline = [
        (T_FIRST_PROBE, "working"),
        (T_FIRST_PROBE, "failed"),
        (T_SECOND_PROBE, "working"),
        (T_LAST_TERMINAL, "done"),
    ]
    code, _, resume = _resume_receipt(tmp_path, timeline=timeline)
    assert code == 1 and resume == {"status": "failed", "reason": "no_valid_response_pre_boundary"}


def test_live_vc_resume_terminal_at_same_instant_as_second_probe_is_not_a_boundary_before_it(tmp_path):
    # 等号側: P2 が T1 と同時刻なら T1 より後ではない（`ts > t1`）。境界後の human turn を観測できていない。
    timeline = [(T_FIRST_PROBE, "working"), (T_SECOND_PROBE, "done"), (T_LAST_TERMINAL, "done")]
    code, _, resume = _resume_receipt(tmp_path, timeline=timeline)
    assert code == 1 and resume == {"status": "unverified", "reason": "no_human_turn_after_terminal"}


def test_live_vc_resume_last_terminal_at_same_instant_as_second_probe_is_not_after_it(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    _write_job(tmp_path, "bbbbbbbb", target, settings=True, replies=[GOOD, GOOD], resumed=True)
    state_path = tmp_path / "jobs" / "bbbbbbbb" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["lastTerminalAt"] = T_SECOND_PROBE
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = _env(tmp_path, target, CLAUDE_GPT_AV_CANDIDATE_ID="bbbbbbbb")
    code, receipt = ev.run("dispatched-child-and-resume", env)
    assert code == 1 and receipt["candidate"]["resume"]["reason"] == "last_terminal_not_after_probe"
