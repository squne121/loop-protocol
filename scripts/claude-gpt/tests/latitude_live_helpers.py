"""scripts/claude-gpt/tests/latitude_live_helpers.py

Issue #2426: bounded live-verification-only helpers shared by
`test_latitude_bun_options_live.py` (AC5), `test_latitude_trace_arrival_live.py`
(AC6) and `test_latitude_runtime_classifier_live.py` (AC7).

Design 7節の明記どおり、この runtime classifier は #2375（PR #2392）の
production retrospective collector（`collect_latitude_runtime_evidence`）とは
完全に分離した、本 Issue の live verification 専用の bounded helper である。
`#2392` の schema / Collection Budget は一切変更しない（この module は
`.claude/skills/agent-retrospective` 配下のどのファイルも import/変更しない）。

Not a pytest test module itself (no `test_` prefix in the filename's stem
segment that pytest scans -- this file is `latitude_live_helpers.py`, pytest
does not collect it as a test module).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"

# Design 7節: launcher-owned GPT model mapping（`[1m]` suffix除去後の base id）。
GPT_MODEL_BASE_IDS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
NATIVE_MODEL_REGEX = re.compile(r"^claude-")

_CONTEXT_HINT_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")

RuntimeClassification = Literal["claude_gpt", "claude_code_native", "unknown"]


def strip_context_hint(model: str) -> str:
    """`lib.sh` の `claude_gpt_strip_context_hint` と同じルールで `[1m]` 等の
    context-window hint suffix を除去する（single source of truth は lib.sh 側
    の shell 実装。ここは live verification 専用の Python 版で、bounded helper
    としてロジックだけ独立に保つ。Design 7節）。
    """
    return _CONTEXT_HINT_SUFFIX_RE.sub("", model)


def classify_runtime(models: list[str]) -> RuntimeClassification:
    """`latitude traces list` の trace item が持つ `models` 配列から、
    Design 7節の `latitude_runtime_classifier_v1` を適用して分類する。

    - launcher-owned GPT model set に一致する model が1つでもあれば `claude_gpt`
    - そうでなく `^claude-` に一致する model が1つでもあれば `claude_code_native`
    - どちらにも一致しなければ `unknown`（fail-closed。`unknown` を
      `claude_code_native` とみなさない）
    """
    stripped = [strip_context_hint(m) for m in models]
    if any(m in GPT_MODEL_BASE_IDS for m in stripped):
        return "claude_gpt"
    if any(NATIVE_MODEL_REGEX.match(m) for m in stripped):
        return "claude_code_native"
    return "unknown"


def is_environment_available() -> tuple[bool, str]:
    """claude-gpt launcher / latitude CLI / claude binary が live 検証に利用可能かを
    bounded に確認する。利用不能なら (False, reason) を返す（呼び出し側は
    pytest.skip する）。"""
    if shutil.which("claude") is None:
        return False, "claude_binary_not_found"
    if shutil.which("claude-code-proxy") is None:
        return False, "claude_code_proxy_not_found"
    if shutil.which("latitude") is None:
        return False, "latitude_cli_not_found"
    return True, ""


def run_claude_gpt_canary(prompt: str, *, nonce: str, timeout: float = 90.0) -> tuple[str, subprocess.CompletedProcess]:
    """non-sensitive canary prompt で実際に Claude-GPT session を1回起動し、
    Stop hook-sink（`CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=hook-sink-multi-turn`、
    既存 Issue #2219 observation-only harness）から実 session_id を読み取って
    返す。この起動は launch.sh の default Latitude Stop hook 配線
    （本 Issue AC1）も同時に additive に発火させる（sink はあくまで observation
    用の第二の Stop hook group であり、Latitude hook を置き換えない）。
    """
    import os

    env = dict(os.environ)
    env["CLAUDE_GPT_RUNTIME_SMOKE_HOOKS"] = "hook-sink-multi-turn"
    env["CLAUDE_GPT_HOOK_SINK_NONCE"] = nonce
    result = subprocess.run(
        [str(LAUNCH_SH), "--", "-p", prompt, "--output-format", "text"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    sink_path = _hook_sink_path(nonce)
    session_id = _read_stop_session_id(sink_path)
    return session_id, result


def _hook_sink_path(nonce: str) -> Path:
    home = _claude_gpt_home()
    return home / "state" / f"hook-sink-{nonce}.jsonl"


def _claude_gpt_home() -> Path:
    import os

    return Path(os.environ.get("CLAUDE_GPT_HOME", str(Path.home() / ".claude-gpt")))


def _read_stop_session_id(sink_path: Path) -> str:
    if not sink_path.exists():
        return ""
    session_id = ""
    for line in sink_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == "Stop" and record.get("session_id"):
            session_id = record["session_id"]
    return session_id


def find_recent_native_trace(project_slug: str, *, limit: int = 20) -> dict | None:
    """直近 `limit` 件の trace のうち、`models` attribute が Native Claude Code
    （`classify_runtime() == "claude_code_native"`）と分類される最初の trace を
    返す（AC7: Native 側の比較サンプルとして、新規セッションを起動せず既存の
    ambient native telemetry から拾う）。

    `serviceNames` は Native / Claude-GPT のどちらも固定で `claude-code` を
    報告する（Design 7節: 現行 Latitude telemetry は `service.name=claude-code`
    を固定生成し、model attribute だけが実際の判別要素であるため、
    `serviceNames` では両者を区別できない -- これはまさに本 classifier が
    model attribute を使う理由そのものである）。そのため選別自体も
    `classify_runtime()` を使う。見つからなければ None。
    """
    try:
        proc = subprocess.run(
            [
                "latitude",
                "traces",
                "list",
                "--project-slug",
                project_slug,
                "--limit",
                str(limit),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for item in payload.get("items", []):
        if classify_runtime(item.get("models") or []) == "claude_code_native":
            return item
    return None


def query_latitude_trace_by_session_id(
    session_id: str,
    project_slug: str,
    *,
    poll_interval_seconds: float = 5.0,
    max_wait_seconds: float = 90.0,
) -> dict | None:
    """`latitude traces list --project-slug <slug> --filters
    '{"sessionId":[{"op":"eq","value":"<session_id>"}]}' --limit 1 --format json`
    を bounded polling で呼び、exact session_id に一致する trace item を返す
    （見つからなければ None）。telemetry export は async のため、Latitude 側
    ingest が数秒遅延することを見込んで poll する。

    argv shape は #2375（PR #2392）`collect_snapshot.build_latitude_allowed_argv`
    が live-verified した shape と同一（`--filters` の `{"op": "eq", "value": ...}`
    形）を、本 Issue の独立 live verification helper として再実装したもの
    （production collector を import/変更はしない。Design 7節の分離明記）。
    """
    filters = json.dumps({"sessionId": [{"op": "eq", "value": session_id}]})
    deadline = time.monotonic() + max_wait_seconds
    while True:
        try:
            proc = subprocess.run(
                [
                    "latitude",
                    "traces",
                    "list",
                    "--project-slug",
                    project_slug,
                    "--filters",
                    filters,
                    "--limit",
                    "1",
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                items = payload.get("items")
                if isinstance(items, list) and items:
                    return items[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval_seconds)
