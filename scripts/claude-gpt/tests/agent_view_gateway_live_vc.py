"""scripts/claude-gpt/tests/agent_view_gateway_live_vc.py

Issue #3055: Agent View の別 repo `@repo` dispatch で `--settings env` が必要かを判定する、
最小の read-only session receipt evaluator。

この script は session を dispatch・再開・停止しない。operator が正規の Agent View UI で
同一条件の 2 arm（control = `--settings` なし / candidate = `--settings env` あり）を dispatch
した後、その job id を環境変数で受け取り、Claude Code が保存した job state と transcript
を読むだけである。`claude --bg` による代用は UI provenance を欠くため、operator が
`CLAUDE_GPT_AV_UI_DISPATCHED=1` で宣言した場合のみ受理する（宣言自体は operator の証言であり
本 script は UI 操作を観測できない）。

環境変数（実 id・secret を Git に保存しない）:
  CLAUDE_GPT_AV_CONTROL_ID       control arm の job id（gateway-control-comparison で必須）
  CLAUDE_GPT_AV_CANDIDATE_ID     candidate arm の job id（必須）
  CLAUDE_GPT_AV_UI_DISPATCHED    "1" = 両 arm を Agent View UI の @repo で dispatch した
  CLAUDE_GPT_AV_TARGET_CWD       dispatch 先 repo の絶対 path（必須。未設定は SKIP。child cwd が配下であること）
  CLAUDE_GPT_AV_EXPECT_TEXT      child の assistant 応答に含まれるべき文字列
  CLAUDE_GPT_AV_JOBS_DIR         既定 ~/.claude/jobs
  CLAUDE_GPT_AV_PROJECTS_DIR     既定 ~/.claude/projects

Exit code:
  0   observed postcondition を満たした（`needed` / `unnecessary` の確定、または child 証跡の成立）
  1   evidence は読めたが postcondition を満たさない（`inconclusive` を含む）
  77  SKIP（runtime / job id / UI provenance が無い。PASS ではない）

出力は allowlist field のみの sanitized JSON。base URL・token・settings の raw 値は出さない。
proxy request との一対一相関は取得できないため常に `proxy_correlation: unverified`。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

SKIP_EXIT = 77
SCENARIOS = ("gateway-control-comparison", "dispatched-child-and-resume")
ROLE_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_LIB_SH = Path(__file__).resolve().parent.parent / "lib.sh"
_MODEL_ACCESS_RE = re.compile(r"issue with the selected model|model.{0,40}(not found|not available|access)", re.I)


def canonical_aliases() -> dict:
    """lib.sh の `CLAUDE_GPT_MODEL_*` 定義から role alias を導出する（別 model 定義をハードコードしない）。"""
    text = _LIB_SH.read_text(encoding="utf-8")
    found = dict(re.findall(r'^(CLAUDE_GPT_MODEL_[A-Z_]+)="([^"]+)"', text, re.M))
    mapping = {
        "ANTHROPIC_MODEL": "CLAUDE_GPT_MODEL_MAIN",
        "ANTHROPIC_SMALL_FAST_MODEL": "CLAUDE_GPT_MODEL_SMALL_FAST",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "CLAUDE_GPT_MODEL_OPUS",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "CLAUDE_GPT_MODEL_SONNET",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "CLAUDE_GPT_MODEL_HAIKU",
    }
    return {k: found[v] for k, v in mapping.items() if v in found}


def gateway_bases() -> set:
    return {re.sub(r"\[[^]]*\]$", "", v) for v in canonical_aliases().values()}


class Skip(Exception):
    pass


def _skip(reason: str) -> "Skip":
    return Skip(reason)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _flag_value(flags: list, name: str):
    for i, item in enumerate(flags):
        if item == name and i + 1 < len(flags):
            return flags[i + 1]
        if isinstance(item, str) and item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def _settings_env(flags: list) -> tuple[dict | None, str]:
    """`--settings` の (env object, source)。source = inline | file | unreadable | absent。

    file path の存在だけでは有効な設定の継承とは言えないため、file の中身を読む。"""
    raw = _flag_value(flags, "--settings")
    if not isinstance(raw, str):
        return None, "absent"
    source = "inline"
    try:
        doc = json.loads(raw)
    except ValueError:
        source = "file"
        doc = _read_json(Path(raw)) if raw.startswith("/") else None
        if doc is None:
            return None, "unreadable"
    env = doc.get("env") if isinstance(doc, dict) else None
    return (env if isinstance(env, dict) else None), source


def settings_env_is_gateway(env: dict | None) -> bool:
    """env が canonical の loopback gateway + 全 role alias を持つ（値が canonical と一致する）こと。"""
    if not env:
        return False
    url = env.get("ANTHROPIC_BASE_URL")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password:
        return False
    if "ANTHROPIC_API_KEY" in env or env.get("ANTHROPIC_AUTH_TOKEN", "unused") != "unused":
        return False  # 実 credential を持つ settings は AC4 の posture に反する（値は receipt に出さない）
    return all(env.get(k) == v for k, v in canonical_aliases().items()) and bool(canonical_aliases())


def _is_real_user_turn(row: dict) -> bool:
    """operator / UI が送った user turn。tool_result・meta・sidechain の `type: user` 行は数えない。"""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return False
    content = row.get("message", {}).get("content") if isinstance(row.get("message"), dict) else None
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return isinstance(content, str) and bool(content.strip())


def _permission_modes(rows: list[dict]) -> list[str]:
    """transcript に記録された実効 permission-mode event（CLI 引数の宣言とは別の観測）。"""
    return [str(r["permissionMode"]) for r in rows if r.get("type") == "permission-mode" and r.get("permissionMode")]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def _transcript(projects_dir: Path, cwd: str, session_id: str) -> list[dict]:
    enc = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    path = projects_dir / enc / f"{session_id}.jsonl"
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def assistant_responses(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(valid, failed)。valid = 実 model が生成し、prompt を echo していない assistant text。

    synthetic / API error message（`There's an issue with the selected model` 等）は failed。
    """
    prompts = [
        _text_of(r.get("message", {}).get("content")).strip()
        for r in rows
        if r.get("type") == "user" and isinstance(r.get("message"), dict)
    ]
    prompts = [p for p in prompts if p]
    valid, failed = [], []
    for i, r in enumerate(rows):
        if r.get("type") != "assistant" or not isinstance(r.get("message"), dict):
            continue
        msg = r["message"]
        text = _text_of(msg.get("content")).strip()
        if not text:
            continue
        model = str(msg.get("model", ""))
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
        if r.get("isApiErrorMessage") or model in ("", "<synthetic>") or not usage.get("output_tokens"):
            fc = "model_access_error" if _MODEL_ACCESS_RE.search(text) else "other_api_error"
            failed.append({"model": model or "unknown", "failure_class": fc})
            continue
        if any(text == p or (len(text) > 20 and text in p) for p in prompts):
            failed.append({"model": model, "failure_class": "prompt_echo"})
            continue
        if model not in gateway_bases():
            # Native / Claude Max 等の gateway 外 model の応答は gateway 経由の成功ではない。
            failed.append({"model": model, "failure_class": "non_gateway_model_response"})
            continue
        valid.append({"model": model, "text": text, "idx": i})
    return valid, failed


def load_arm(
    label: str, job_id: str, jobs_dir: Path, projects_dir: Path, target_cwd: str | None, expect: str | None
) -> dict:
    if not _JOB_ID_RE.match(job_id or ""):
        raise _skip(f"{label}: job id is not an 8-hex Agent View job id")
    state = _read_json(jobs_dir / job_id / "state.json")
    if not isinstance(state, dict):
        raise _skip(f"{label}: job state not found under jobs dir")
    cwd = str(state.get("cwd", ""))
    flags = state.get("respawnFlags") if isinstance(state.get("respawnFlags"), list) else []
    settings_env, settings_source = _settings_env(flags)
    rows_all = _transcript(projects_dir, cwd, str(state.get("sessionId", "")))
    perm_observed = _permission_modes(rows_all)
    has_settings = _flag_value(flags, "--settings") is not None
    valid, failed = assistant_responses(rows_all)
    if expect:
        valid_expected = [v for v in valid if expect in v["text"]]
    else:
        valid_expected = valid
    cwd_ok = bool(cwd) and (target_cwd is None or cwd == target_cwd or cwd.startswith(target_cwd.rstrip("/") + "/"))
    aliases = canonical_aliases()
    user_turns = [i for i, r in enumerate(rows_all) if _is_real_user_turn(r)]
    strip = lambda a: re.sub(r"\[[^]]*\]$", "", a or "")  # noqa: E731
    main_bases = {
        strip(aliases.get(k))
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL")
    }
    haiku_bases = {strip(aliases.get(k)) for k in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")}
    provider_env = state.get("providerEnv") if isinstance(state.get("providerEnv"), dict) else {}
    return {
        "label": label,
        "job_state": state.get("state"),
        "cli_version": state.get("cliVersion"),
        "child_cwd": cwd,
        "child_cwd_in_target": cwd_ok,
        "response_after_second_user_turn": len(user_turns) >= 2
        and any(v["idx"] > user_turns[1] for v in valid_expected),
        "child_cwd_is_git_root": (Path(cwd) / ".git").exists() if cwd else False,
        "settings_flag_present": has_settings,
        "settings_source": settings_source,
        "settings_content_valid": settings_env_is_gateway(settings_env),
        "settings_role_aliases_configured": (
            {k: settings_env.get(k) for k in ROLE_ENV_KEYS if k in settings_env} if settings_env else {}
        ),
        "provider_env_role_aliases": {k: provider_env.get(k) for k in ROLE_ENV_KEYS if k in provider_env},
        "provider_env_matches_canonical": all(provider_env.get(k) == v for k, v in aliases.items()) and bool(aliases),
        "permission_mode_declared": _flag_value(flags, "--permission-mode"),
        "permission_modes_observed": perm_observed,
        "permission_mode_consistent": bool(perm_observed)
        and (_flag_value(flags, "--permission-mode") in (None, perm_observed[-1])),
        "main_route_observed": bool(main_bases & {m for m in (v["model"] for v in valid_expected)}),
        "haiku_route_observed": bool(haiku_bases & {v["model"] for v in valid_expected}),
        "model_flag": _flag_value(flags, "--model"),
        "valid_assistant_responses": len(valid_expected),
        "valid_assistant_models": sorted({v["model"] for v in valid_expected}),
        "failed_responses": failed[:5],
        "resume_same_session": state.get("sessionId") == state.get("resumeSessionId") and bool(state.get("sessionId")),
        "resumed_after_first_terminal": bool(
            state.get("lastTerminalAt")
            and state.get("firstTerminalAt")
            and state["lastTerminalAt"] > state["firstTerminalAt"]
        ),
    }


def classify(control: dict, candidate: dict) -> str:
    """`needed | unnecessary | inconclusive`（欠落・無効 arm は成功/失敗として数えない）。"""
    if not candidate["settings_flag_present"] or control["settings_flag_present"]:
        return "inconclusive"  # arm の条件が逆/欠落
    if not (candidate["child_cwd_in_target"] and control["child_cwd_in_target"]):
        return "inconclusive"
    if control["cli_version"] != candidate["cli_version"] or not candidate["cli_version"]:
        return "inconclusive"  # 同一 Claude Code version 条件が成立しない
    # control は candidate と同じ supervisor env（launcher 由来の gateway を shell に持つ）で、差は --settings のみ。
    # gateway 環境が全く無い control は H1 を検証しない（native fallback が自明に出る）。
    if not (control["provider_env_matches_canonical"] and candidate["provider_env_matches_canonical"]):
        return "inconclusive"
    if control["provider_env_role_aliases"] != candidate["provider_env_role_aliases"]:
        return "inconclusive"
    if not candidate["settings_content_valid"]:
        return "inconclusive"
    if control["child_cwd"] != candidate["child_cwd"]:
        return "inconclusive"  # 同一 fixture repo への dispatch ではない
    if not (control["permission_modes_observed"] and candidate["permission_modes_observed"]):
        return "inconclusive"
    if control["permission_modes_observed"][-1] != candidate["permission_modes_observed"][-1]:
        return "inconclusive"
    cand_ok = candidate["valid_assistant_responses"] > 0
    ctrl_ok = control["valid_assistant_responses"] > 0
    if cand_ok and ctrl_ok:
        return "unnecessary"
    # control の失敗が gateway 未適用を示す class（model access error / gateway 外 model への fallback）の
    # ときだけ needed。prompt echo・rate limit 等の無関係な失敗は根拠にしない。
    gateway_failure = {"model_access_error", "non_gateway_model_response"}
    if cand_ok and any(f["failure_class"] in gateway_failure for f in control["failed_responses"]):
        return "needed"
    return "inconclusive"


def run(scenario: str, environ: dict) -> tuple[int, dict]:
    home = Path(environ.get("HOME", "~")).expanduser()
    jobs_dir = Path(environ.get("CLAUDE_GPT_AV_JOBS_DIR") or home / ".claude" / "jobs")
    projects_dir = Path(environ.get("CLAUDE_GPT_AV_PROJECTS_DIR") or home / ".claude" / "projects")
    target = environ.get("CLAUDE_GPT_AV_TARGET_CWD") or None
    expect = environ.get("CLAUDE_GPT_AV_EXPECT_TEXT") or None
    receipt: dict = {
        "schema": "AGENT_VIEW_GATEWAY_LIVE_VC_RECEIPT_V1",
        "scenario": scenario,
        "proxy_correlation": "unverified",
        "provider_route_claim": "none",
        # operator の自己申告のみ。本 script は UI 操作を独立に観測できない。
        "ui_dispatch_provenance": "operator_attested_only",
    }
    try:
        if environ.get("CLAUDE_GPT_AV_UI_DISPATCHED") != "1":
            raise _skip("operator did not attest an Agent View UI @repo dispatch (CLAUDE_GPT_AV_UI_DISPATCHED=1)")
        if not target:
            raise _skip("CLAUDE_GPT_AV_TARGET_CWD is not set (cross-repo target identity cannot be checked)")
        if not expect:
            raise _skip("CLAUDE_GPT_AV_EXPECT_TEXT is not set (a paraphrased echo cannot be told from a real answer)")
        cand_id = environ.get("CLAUDE_GPT_AV_CANDIDATE_ID", "")
        if not cand_id:
            raise _skip("CLAUDE_GPT_AV_CANDIDATE_ID is not set")
        candidate = load_arm("candidate", cand_id, jobs_dir, projects_dir, target, expect)
        if scenario == "gateway-control-comparison":
            ctrl_id = environ.get("CLAUDE_GPT_AV_CONTROL_ID", "")
            if not ctrl_id:
                raise _skip("CLAUDE_GPT_AV_CONTROL_ID is not set")
            control = load_arm("control", ctrl_id, jobs_dir, projects_dir, target, expect)
            verdict = classify(control, candidate)
            receipt.update(control=control, candidate=candidate, necessity=verdict)
            return (0 if verdict in ("needed", "unnecessary") else 1), receipt
        ok = (
            candidate["settings_flag_present"]
            and candidate["child_cwd_in_target"]
            and candidate["valid_assistant_responses"] >= 1
            and candidate["provider_env_matches_canonical"]
            and candidate["settings_content_valid"]
            and candidate["permission_mode_consistent"]
            and candidate["main_route_observed"]
            and candidate["resume_same_session"]
            and candidate["resumed_after_first_terminal"]
            and candidate["response_after_second_user_turn"]
            and candidate["valid_assistant_responses"] >= 2
        )
        receipt.update(
            candidate=candidate,
            observed_postcondition=ok,
            assertions={
                "settings_content_inherited": "observed" if candidate["settings_content_valid"] else "failed",
                "permission_mode_effective": "observed" if candidate["permission_mode_consistent"] else "failed",
                "main_route_called": "observed" if candidate["main_route_observed"] else "failed",
                "haiku_route_called": "observed" if candidate["haiku_route_observed"] else "unverified",
                "ui_dispatch_provenance": "unverified",
                "proxy_request_correlation": "unverified",
            },
        )
        return (0 if ok else 1), receipt
    except Skip as exc:
        receipt.update(status="skip", reason=str(exc))
        return SKIP_EXIT, receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--artifact-dir", default="artifacts/runtime-verification/agent-view-gateway")
    args = parser.parse_args(argv)
    code, receipt = run(args.scenario, dict(os.environ))
    receipt["exit_code"] = code
    if code == SKIP_EXIT:
        print(f"SKIP: {receipt['reason']}", file=sys.stderr)
    out = Path(args.artifact_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{args.scenario}.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    print(json.dumps(receipt, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
