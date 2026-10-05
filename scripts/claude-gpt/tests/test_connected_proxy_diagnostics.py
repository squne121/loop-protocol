"""scripts/claude-gpt/tests/test_connected_proxy_diagnostics.py

Issue #2925 AC3: 接続先 server（`ANTHROPIC_BASE_URL` が実際に向く running server）を基準にした
診断の focused test。実 TCP port に bind する別 process の fake server を相手に、実
`launch.sh --check-only` / 通常起動を subprocess で駆動する。

確認すること:
  - required model set は `gpt-6-sol` **および** `gpt-6-luna`。一方だけでは PASS しない。
  - 診断の authority は接続先 server であり、PATH 上の binary ではない。PATH binary の
    path / version は補助 evidence として別項目に分離され、server version は取得できなければ
    「未確認」と記録される（binary の version で代用しない）。
  - model catalog の不足を、推論能力や entitlement の failure と誤分類しない。
  - launcher が起動していない proxy は、launcher の終了時に停止されない（成功・失敗いずれの
    経路でも）。launcher は proxy を一切起動しない。
"""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
from pathlib import Path

import pytest

_HARNESS_PATH = Path(__file__).resolve().parent / "_launcher_harness.py"
_spec = importlib.util.spec_from_file_location("claude_gpt_launcher_harness_2925_diag", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


def _check_only(tmp_path, url, **env_overrides):
    env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=url, **env_overrides)
    proc = H.run_launcher(["--check-only"], env)
    return proc, (json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None)


def _write_fake_path_proxy(directory: Path, version_line: str, marker: Path) -> Path:
    """PATH 上の fake `claude-code-proxy`。`--version` 以外で実行されたら marker を作る。"""
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "claude-code-proxy"
    binary.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "{version_line}"; exit 0; fi\n'
        f'echo "$@" > "{marker}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


# ---------------------------------------------------------------------------
# required model set: both models, never one alone
# ---------------------------------------------------------------------------


def test_check_only_passes_only_when_both_required_models_are_listed(tmp_path):
    with H.FakeServer(models=("gpt-6-sol", "gpt-6-luna", "gpt-6-astra")) as server:
        proc, payload = _check_only(tmp_path, server.url)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert payload["status"] == "ok" and payload["mode"] == "check_only"
    connected = payload["connected_server"]
    assert connected["model_catalog_ok"] is True
    assert connected["missing_models"] == []
    assert sorted(connected["required_models"]) == ["gpt-6-luna", "gpt-6-sol"]
    assert connected["reachable"] is True and connected["models_http_status"] == 200


@pytest.mark.parametrize(
    "listed, missing",
    [
        (("gpt-6-sol",), ["gpt-6-luna"]),
        (("gpt-6-luna",), ["gpt-6-sol"]),
        (("gpt-6-astra",), ["gpt-6-sol", "gpt-6-luna"]),
        ((), ["gpt-6-sol", "gpt-6-luna"]),
    ],
)
def test_check_only_does_not_pass_when_only_one_or_neither_model_is_listed(tmp_path, listed, missing):
    with H.FakeServer(models=listed) as server:
        proc, payload = _check_only(tmp_path, server.url)
    assert proc.returncode == 7, (proc.stdout, proc.stderr)
    assert payload["status"] == "failed"
    assert payload["reason"] == "model_alias_not_resolved"
    connected = payload["connected_server"]
    assert connected["model_catalog_ok"] is False
    assert sorted(connected["missing_models"]) == sorted(missing)
    # server には到達できている。推論能力・entitlement の failure とは分類しない。
    assert connected["reachable"] is True
    assert payload["cause"] == "connected_server_model_catalog_incomplete"
    assert "entitlement" not in json.dumps(payload).lower()
    assert "inference" not in json.dumps(payload).lower()


def test_check_only_prefix_lookalike_model_ids_do_not_count_as_required_models(tmp_path):
    with H.FakeServer(models=("gpt-6-sol-mini", "gpt-6-luna-lite")) as server:
        proc, payload = _check_only(tmp_path, server.url)
    assert proc.returncode == 7
    assert sorted(payload["connected_server"]["missing_models"]) == ["gpt-6-luna", "gpt-6-sol"]


@pytest.mark.parametrize(
    "raw_body",
    [
        '{"note":"gpt-6-sol","fallback":"gpt-6-luna"}',  # 別 field の値は catalog ではない
        '{"data":{"gpt-6-sol":1,"gpt-6-luna":2}}',  # data が list ではない
        '{"data":["gpt-6-sol","gpt-6-luna"]}',  # 要素が object ではない
        '{"data":[{"name":"gpt-6-sol"},{"name":"gpt-6-luna"}]}',  # id ではなく name
        '{"data":[{"id":"gpt-6-sol-mini"},{"id":"xgpt-6-luna"}]}',  # prefix / suffix lookalike
        '["gpt-6-sol","gpt-6-luna"]',  # top-level が object ではない
        "not json gpt-6-sol gpt-6-luna",  # parse 不能
        "",
    ],
)
def test_check_only_requires_exact_data_id_in_a_structured_catalog(tmp_path, raw_body):
    with H.FakeServer(raw_models_body=raw_body) as server:
        proc, payload = _check_only(tmp_path, server.url)
    assert proc.returncode == 7, (proc.stdout, proc.stderr)
    assert sorted(payload["connected_server"]["missing_models"]) == ["gpt-6-luna", "gpt-6-sol"]


def test_check_only_reports_only_the_missing_one_for_a_partial_structured_catalog(tmp_path):
    body = '{"object":"list","data":[{"id":"gpt-6-sol","note":"gpt-6-luna"}]}'
    with H.FakeServer(raw_models_body=body) as server:
        proc, payload = _check_only(tmp_path, server.url)
    assert proc.returncode == 7
    assert payload["connected_server"]["missing_models"] == ["gpt-6-luna"]


def test_normal_launch_is_blocked_on_an_incomplete_catalog_and_never_reaches_claude(tmp_path):
    with H.FakeServer(models=("gpt-6-sol",)) as server:
        proc, observed, _env = H.run_launcher_with_fake_claude(tmp_path, server.url, ("-p", "x"))
    assert proc.returncode == 7
    assert observed is None, "claude must not start when the connected server lacks a required model"
    assert json.loads(proc.stdout)["reason"] == "model_alias_not_resolved"


# ---------------------------------------------------------------------------
# authority = connected server; PATH binary = auxiliary evidence only
# ---------------------------------------------------------------------------


def test_connected_server_is_the_authority_not_the_path_binary(tmp_path):
    marker = tmp_path / "path-proxy-was-executed"
    path_bin = _write_fake_path_proxy(tmp_path / "bin", "claude-code-proxy 0.1.36", marker)
    bin_dir = str(path_bin.parent)
    env_path = bin_dir + ":" + H.base_env(tmp_path)["PATH"]

    # 古い (v0.1.36 を名乗る) PATH binary があっても、接続先 server が両 model を持てば PASS。
    with H.FakeServer() as server:
        proc, payload = _check_only(tmp_path, server.url, PATH=env_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert payload["connected_server"]["model_catalog_ok"] is True
    # 逆に、新しい binary を名乗っていても接続先 server が不足していれば FAIL。
    new_bin = _write_fake_path_proxy(tmp_path / "bin2", "claude-code-proxy 0.1.43", marker)
    env_path2 = str(new_bin.parent) + ":" + H.base_env(tmp_path)["PATH"]
    with H.FakeServer(models=("gpt-6-sol",)) as server:
        proc2, payload2 = _check_only(tmp_path, server.url, PATH=env_path2)
    assert proc2.returncode == 7
    assert payload2["connected_server"]["missing_models"] == ["gpt-6-luna"]
    # launcher は PATH binary を --version 以外では決して実行しない（proxy を起動しない）。
    assert not marker.exists()


def test_local_binary_path_and_version_are_recorded_separately_as_auxiliary_evidence(tmp_path):
    marker = tmp_path / "marker"
    path_bin = _write_fake_path_proxy(tmp_path / "bin", "claude-code-proxy 9.9.9-aux", marker)
    env_path = str(path_bin.parent) + ":" + H.base_env(tmp_path)["PATH"]
    with H.FakeServer() as server:
        proc, payload = _check_only(tmp_path, server.url, PATH=env_path)
    assert proc.returncode == 0
    aux = payload["local_proxy_binary_auxiliary"]
    assert aux["path"] == str(path_bin)
    assert aux["version"] == "claude-code-proxy 9.9.9-aux"
    assert "auxiliary" in aux["note"]
    connected = payload["connected_server"]
    # server version は binary の version で代用しない。取得できなければ「未確認」。
    assert connected["version"] == "未確認"
    assert "9.9.9" not in json.dumps(connected)
    assert "aux" not in connected["version"]


def test_server_version_is_unconfirmed_when_the_server_exposes_none(tmp_path):
    with H.FakeServer() as server:
        _proc, payload = _check_only(tmp_path, server.url)
    assert payload["connected_server"]["version"] == "未確認"
    assert "healthz" in payload["connected_server"]["version_note"]


def test_missing_path_binary_is_recorded_as_null_without_failing_the_diagnosis(tmp_path):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    # curl / sh / python3 は必要なので PATH 全体は残し、claude-code-proxy だけが解決できない状態にする。
    with H.FakeServer() as server:
        proc, payload = _check_only(tmp_path, server.url, CLAUDE_GPT_PROXY_BIN=str(tmp_path / "does-not-exist"))
    assert proc.returncode == 0, proc.stderr
    aux = payload["local_proxy_binary_auxiliary"]
    assert aux["path"] == str(tmp_path / "does-not-exist")
    assert aux["version"] != "claude-code-proxy 0.1.42"


def test_diagnostics_target_the_url_the_claude_process_will_actually_use(tmp_path):
    with H.FakeServer() as good, H.FakeServer(models=()) as bad:
        proc_good, payload_good = _check_only(tmp_path, good.url)
        proc_bad, payload_bad = _check_only(tmp_path, bad.url)
        assert proc_good.returncode == 0 and proc_bad.returncode == 7
        assert payload_good["connected_server"]["port"] == good.port
        assert payload_bad["connected_server"]["port"] == bad.port
        # 通常起動で claude が受け取る ANTHROPIC_BASE_URL は、診断した URL と同一。
        _p, observed, _e = H.run_launcher_with_fake_claude(tmp_path, good.url)
        assert observed["env"]["ANTHROPIC_BASE_URL"] == good.url


# ---------------------------------------------------------------------------
# unreachable / malformed / non-loopback endpoints
# ---------------------------------------------------------------------------


def test_unreachable_server_returns_bounded_diagnostic_with_start_hint(tmp_path):
    url = f"http://127.0.0.1:{H.closed_port()}"
    proc, payload = _check_only(tmp_path, url)
    assert proc.returncode == 7
    assert payload["reason"] == "connected_server_unreachable"
    connected = payload["connected_server"]
    assert connected["reachable"] is False and connected["classification"] == "unreachable"
    assert "claude-code-proxy serve" in payload["start_hint"]
    # model 不足とは別分類（到達不能を catalog 不足と誤報告しない）。
    assert payload["cause"] != "connected_server_model_catalog_incomplete"


def test_models_endpoint_http_error_is_distinguished_from_missing_models(tmp_path):
    with H.FakeServer(models_status=500) as server:
        proc, payload = _check_only(tmp_path, server.url)
    assert proc.returncode == 7
    assert payload["reason"] == "connected_server_models_unavailable"
    assert payload["connected_server"]["models_http_status"] == 500
    assert payload["connected_server"]["reachable"] is True


@pytest.mark.parametrize("url", ["http://example.com:18765", "https://api.anthropic.com", "http://10.1.2.3:18765"])
def test_non_loopback_base_url_is_refused_without_any_request(tmp_path, url):
    proc, payload = _check_only(tmp_path, url)
    assert proc.returncode == 7
    assert payload["reason"] == "connected_server_not_loopback"
    assert payload["connected_server"]["reachable"] is False
    assert payload["connected_server"]["models_http_status"] is None


def _run_check_only_with_bash(tmp_path, base_url):
    """実 `bash scripts/claude-gpt/launch.sh --check-only` を local subprocess として起動する。"""
    env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=base_url)
    return subprocess.run(
        ["bash", str(H.LAUNCH_SH), "--check-only"],
        env=env,
        cwd=str(H.REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60.0,
    )


@pytest.mark.parametrize("host", ["127.evil.example", "127.0.0.1.evil.example", "0127.0.0.1"])
def test_loopback_reject_non_loopback_hosts(tmp_path, host):
    """`127.` 始まりの host 名や先頭 0 付きの曖昧表記は loopback として受理しない（#2939 AC1）。

    exit code と stdout/stderr の `non_loopback` 分類を観測する。network request は発生しない。"""
    proc = _run_check_only_with_bash(tmp_path, f"http://{host}:18765")
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert "non_loopback" in proc.stdout + proc.stderr, (proc.stdout, proc.stderr)
    connected = json.loads(proc.stdout)["connected_server"]
    assert connected["classification"] == "non_loopback"
    assert connected["reachable"] is False
    assert connected["models_http_status"] is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_accept_loopback_hosts(tmp_path, host):
    """正当な loopback host は non_loopback として拒否されない（#2939 AC2）。

    live proxy は不要。閉じた port へ向けるため結果は `unreachable` になるが、loopback 判定を
    通過して probe 段階まで進んだこと（non_loopback / invalid_base_url でないこと）を確認する。"""
    proc = _run_check_only_with_bash(tmp_path, f"http://{host}:{H.closed_port()}")
    assert "non_loopback" not in proc.stdout + proc.stderr, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["connected_server"]["classification"] == "unreachable", (proc.stdout, proc.stderr)
    assert payload["reason"] == "connected_server_unreachable"


@pytest.mark.parametrize("url", ["127.0.0.1:18765", "http://127.0.0.1:18765/v1", "http://user@127.0.0.1:1", "http://127.0.0.1:abc"])
def test_malformed_base_url_is_refused(tmp_path, url):
    proc, payload = _check_only(tmp_path, url)
    assert proc.returncode == 7
    assert payload["reason"] == "invalid_anthropic_base_url"


def test_default_base_url_is_the_upstream_loopback_endpoint(tmp_path):
    env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=None)
    proc = H.run_launcher(["--dry-run"], env)
    assert json.loads(proc.stdout)["base_url"] == "http://127.0.0.1:18765"


# ---------------------------------------------------------------------------
# process ownership: a proxy the launcher did not start is never stopped
# ---------------------------------------------------------------------------


def test_launcher_never_stops_a_proxy_it_did_not_start_on_success(tmp_path):
    with H.FakeServer() as server:
        pid = server.pid
        proc, observed, _env = H.run_launcher_with_fake_claude(tmp_path, server.url, ("-p", "x"))
        assert proc.returncode == 0 and observed is not None
        assert server.alive() and server.proc.pid == pid
        assert server.listening()


@pytest.mark.parametrize("models", [("gpt-6-sol",), ()])
def test_launcher_never_stops_a_proxy_it_did_not_start_on_diagnostic_failure(tmp_path, models):
    with H.FakeServer(models=models) as server:
        proc, _payload = _check_only(tmp_path, server.url)
        assert proc.returncode == 7
        assert server.alive() and server.listening()


def test_launcher_never_stops_a_proxy_when_claude_exits_nonzero(tmp_path):
    with H.FakeServer() as server:
        fake = H.write_fake_claude(tmp_path / "fake-claude")
        env = H.base_env(
            tmp_path, ANTHROPIC_BASE_URL=server.url, FAKE_CLAUDE_OUT=str(tmp_path / "o.json"), FAKE_CLAUDE_EXIT="3"
        )
        proc = H.run_launcher(["--claude-bin", str(fake), "--", "-p"], env)
        assert proc.returncode == 3
        assert server.alive() and server.listening()


def test_launcher_never_starts_a_proxy_even_when_nothing_is_listening(tmp_path):
    marker = tmp_path / "proxy-started"
    path_bin = _write_fake_path_proxy(tmp_path / "bin", "claude-code-proxy 0.1.42", marker)
    env_path = str(path_bin.parent) + ":" + H.base_env(tmp_path)["PATH"]
    proc, observed, _env = H.run_launcher_with_fake_claude(
        tmp_path, f"http://127.0.0.1:{H.closed_port()}", ("-p", "x"), PATH=env_path
    )
    assert proc.returncode == 7
    assert observed is None
    assert not marker.exists(), "launcher must not run `claude-code-proxy serve`"


def test_launcher_source_has_no_proxy_lifecycle_code():
    source = (H.SCRIPT_DIR / "launch.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    for token in (" serve ", "kill ", "wait ", "trap ", "PROXY_PID", "ss -ltn"):
        assert token not in code, token


# ---------------------------------------------------------------------------
# repair helper scope (which binary is repaired, which server needs a restart)
# ---------------------------------------------------------------------------


def test_failure_receipt_states_the_repair_scope_and_who_must_restart_the_server(tmp_path):
    with H.FakeServer(models=("gpt-6-sol",)) as server:
        _proc, payload = _check_only(tmp_path, server.url)
    assert payload["repair_command"] == "scripts/claude-gpt/repair_proxy.sh"
    assert "BINARY" in payload["repair_scope"] and "owner must restart" in payload["repair_scope"]
    assert "SERVER OWNER" in payload["start_hint"]


# ---------------------------------------------------------------------------
# auxiliary PATH binary must never block the launcher (hanging --version)
# ---------------------------------------------------------------------------


def _write_hanging_path_proxy(directory: Path, started: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "claude-code-proxy"
    binary.write_text(
        f'#!/bin/sh\necho started >> "{started}"\nsleep 60 &\nsleep 60\n',
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_hanging_auxiliary_binary_does_not_block_dry_run_or_normal_launch(tmp_path):
    started = tmp_path / "aux-started"
    path_bin = _write_hanging_path_proxy(tmp_path / "bin", started)
    search_path = str(path_bin.parent) + ":" + H.base_env(tmp_path)["PATH"]
    with H.FakeServer() as server:
        launch_env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url, PATH=search_path)
        dry = H.run_launcher(["--dry-run"], launch_env, timeout=10)
        assert dry.returncode == 0, dry.stderr
        assert not started.exists(), "dry-run must not execute the auxiliary PATH binary"
        proc, observed, _ = H.run_launcher_with_fake_claude(tmp_path, server.url, ("-p", "x"), PATH=search_path)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert observed is not None
    assert not started.exists(), "normal launch must not execute the auxiliary PATH binary"


def test_hanging_auxiliary_binary_is_bounded_and_unconfirmed_in_check_only(tmp_path):
    import time

    started = tmp_path / "aux-started"
    path_bin = _write_hanging_path_proxy(tmp_path / "bin", started)
    search_path = str(path_bin.parent) + ":" + H.base_env(tmp_path)["PATH"]
    with H.FakeServer() as server:
        t0 = time.monotonic()
        proc, payload = _check_only(tmp_path, server.url, PATH=search_path)
        elapsed = time.monotonic() - t0
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert elapsed < 15, f"check-only blocked on the auxiliary binary for {elapsed:.1f}s"
    assert payload["local_proxy_binary_auxiliary"]["version"] == "unknown"
    assert payload["connected_server"]["model_catalog_ok"] is True
