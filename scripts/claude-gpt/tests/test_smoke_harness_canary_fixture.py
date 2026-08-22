"""scripts/claude-gpt/tests/test_smoke_harness_canary_fixture.py

Issue #2274 AC14/AC15: `runtime_smoke_test.sh` の canary SubAgent fixture が
launcher-owned/session-owned として固定 spec で内部合成され、caller-supplied
`--agents` を経路として使用しないこと。

`scripts/claude-gpt/lib.sh` の以下 2 関数を対象にする（`runtime_smoke_test.sh`
本体もこれらの関数を呼び出す。caller-owned `--agents` を forbidden flag として
拒否する launch.sh 側の pre-filter ロジックには一切変更を加えていない -- この
suite が対象にするのは、`runtime_smoke_test.sh` 自身が launcher-owned fixture
として内部合成する側の経路であって、caller-supplied `--agents` を通す経路では
ない）:

- ``claude_gpt_smoke_canary_agents_json_fragment``: caller から
  name/prompt/model/tools を一切受け取らない固定 spec（smoke-mode 専用・
  高エントロピー名・``tools: []``・固定 prompt/marker）で canary fixture を
  JSON serializer により一括生成し、生成直後に自身で parse/readback して
  malformed input・予約済み agent name との衝突を fail-closed で拒否する
  （AC14/AC15）。
- ``claude_gpt_agents_json_merge_validate``: 複数 fragment を安全にマージし、
  malformed JSON・duplicate top-level key（agent name 衝突）を fail-closed で
  拒否する（AC15）。

固定文字列の shell 関数を `sh -c '. lib.sh; ...'` で直接呼び出す（launch.sh の
gate script 抽出と同じ精神 -- 実際に `runtime_smoke_test.sh` が呼ぶのと同一の
関数定義を対象にする）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LIB_SH = REPO_ROOT / "scripts" / "claude-gpt" / "lib.sh"
RUNTIME_SMOKE_SH = REPO_ROOT / "scripts" / "claude-gpt" / "runtime_smoke_test.sh"

SH_BIN = shutil.which("sh") or "/bin/sh"

RESERVED_SPARK_AGENT_NAME = "spark-codex"
MARKER = "CLAUDE_GPT_CANARY_SUBAGENT_OK"


def _sh(script: str, *, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SH_BIN, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _canary_fragment(marker: str, nonce: str, reserved_name: str = RESERVED_SPARK_AGENT_NAME):
    script = (
        f". '{LIB_SH}'\n"
        f"claude_gpt_smoke_canary_agents_json_fragment {_q(marker)} {_q(nonce)} {_q(reserved_name)}\n"
    )
    return _sh(script)


def _q(value: str) -> str:
    """Minimal POSIX single-quote shell-escape for test-controlled literals."""
    return "'" + value.replace("'", "'\\''") + "'"


def _merge_validate(*fragments: str):
    quoted = " ".join(_q(f) for f in fragments)
    script = f". '{LIB_SH}'\nclaude_gpt_agents_json_merge_validate {quoted}\n"
    return _sh(script)


# --- lib.sh source is present and referenced from runtime_smoke_test.sh ----


def test_lib_sh_defines_canary_fixture_function():
    text = LIB_SH.read_text(encoding="utf-8")
    assert "claude_gpt_smoke_canary_agents_json_fragment()" in text
    assert "claude_gpt_agents_json_merge_validate()" in text


def test_runtime_smoke_sh_uses_the_fixture_function_not_a_static_literal():
    text = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert "claude_gpt_smoke_canary_agents_json_fragment" in text
    # The old static single-word agent name "canary" fixture literal must be
    # gone -- the fixture is synthesized fresh per run now.
    assert '"canary":{"description"' not in text


# --- claude_gpt_smoke_canary_agents_json_fragment: positive spec compliance --


def test_canary_fixture_positive_control_valid_json_single_key():
    result = _canary_fragment(MARKER, "nonce-one")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert len(payload) == 1


def test_canary_fixture_tools_always_empty():
    result = _canary_fragment(MARKER, "nonce-two")
    payload = json.loads(result.stdout)
    entry = next(iter(payload.values()))
    assert entry["tools"] == []


def test_canary_fixture_never_carries_a_model_field():
    """Only the session-local spark-codex definition owns a `model` field
    (Issue #2274 AC1/AC2) -- the canary fixture must never carry one."""
    result = _canary_fragment(MARKER, "nonce-three")
    payload = json.loads(result.stdout)
    entry = next(iter(payload.values()))
    assert "model" not in entry


def test_canary_fixture_prompt_embeds_the_marker():
    result = _canary_fragment(MARKER, "nonce-four")
    payload = json.loads(result.stdout)
    entry = next(iter(payload.values()))
    assert MARKER in entry["prompt"]


def test_canary_fixture_agent_name_is_high_entropy_and_incorporates_nonce():
    """AC14: agent name must incorporate the launch/run nonce and be
    high-entropy (never a static literal like "canary")."""
    result_a = _canary_fragment(MARKER, "nonce-aaaa")
    result_b = _canary_fragment(MARKER, "nonce-bbbb")
    name_a = next(iter(json.loads(result_a.stdout)))
    name_b = next(iter(json.loads(result_b.stdout)))
    assert name_a != "canary"
    assert name_a != name_b
    assert name_a.startswith("canary-smoke-")
    # High-entropy: a 32-hex-char suffix derived from the nonce (sha256
    # prefix), not the raw nonce itself echoed verbatim.
    suffix = name_a[len("canary-smoke-"):]
    assert len(suffix) == 32
    int(suffix, 16)  # must be valid hex


def test_canary_fixture_deterministic_for_same_nonce():
    """Same nonce -> same derived agent name (needed so a single smoke run
    can compute the name once and reference it consistently in its own
    prompt text)."""
    result_a = _canary_fragment(MARKER, "same-nonce-value")
    result_b = _canary_fragment(MARKER, "same-nonce-value")
    name_a = next(iter(json.loads(result_a.stdout)))
    name_b = next(iter(json.loads(result_b.stdout)))
    assert name_a == name_b


# --- claude_gpt_smoke_canary_agents_json_fragment: fail-closed rejection ----


def test_canary_fixture_rejects_empty_marker():
    result = _canary_fragment("", "nonce-five")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_canary_fixture_rejects_empty_nonce():
    result = _canary_fragment(MARKER, "")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_canary_fixture_rejects_name_collision_with_reserved_spark_agent_name(monkeypatch):
    """Structural collision-rejection: if the derived canary name were ever
    to equal the reserved spark-codex session-local agent name, the
    function must refuse to emit a fixture (fail-closed), never silently
    letting the canary shadow/overwrite the spark definition."""
    # Force a collision by passing the canary's own about-to-be-derived
    # name as the "reserved" name argument -- this simulates the collision
    # condition deterministically without needing to brute-force a
    # sha256 preimage.
    probe = _canary_fragment(MARKER, "collision-probe-nonce", reserved_name="placeholder")
    assert probe.returncode == 0, probe.stderr
    derived_name = next(iter(json.loads(probe.stdout)))

    collided = _canary_fragment(MARKER, "collision-probe-nonce", reserved_name=derived_name)
    assert collided.returncode != 0
    assert collided.stdout.strip() == ""


# --- caller cannot override name/prompt/model/tools -------------------------


def test_canary_fixture_function_signature_accepts_no_name_prompt_model_tools_params():
    """AC14: "caller から name/prompt/model/tools を受け取らない" is enforced
    structurally -- the function's own positional parameters are marker,
    nonce, reserved_name only. Verify by grepping the function body in
    lib.sh: no `$4`/`$5`/`$6` positional parameter is ever read (which
    would indicate an additional caller-supplied override channel)."""
    text = LIB_SH.read_text(encoding="utf-8")
    start = text.index("claude_gpt_smoke_canary_agents_json_fragment() {")
    end = text.index("\n}\n", start)
    body = text[start:end]
    for forbidden_positional in ("$4", "$5", "$6", "$7", "$8", "$9"):
        assert forbidden_positional not in body, (
            f"canary fixture function body reads {forbidden_positional}, which "
            "would be an undocumented caller-supplied override channel"
        )


# --- claude_gpt_agents_json_merge_validate: AC15 fail-closed rejection -----


def test_merge_validate_accepts_disjoint_fragments():
    result = _merge_validate('{"a": {"x": 1}}', '{"b": {"y": 2}}')
    assert result.returncode == 0, result.stderr
    merged = json.loads(result.stdout)
    assert merged == {"a": {"x": 1}, "b": {"y": 2}}


def test_merge_validate_rejects_duplicate_agent_name():
    result = _merge_validate('{"a": {"x": 1}}', '{"a": {"x": 2}}')
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_merge_validate_rejects_malformed_json():
    result = _merge_validate('{"a": {"x": 1}}', "{not valid json")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_merge_validate_rejects_non_object_fragment():
    result = _merge_validate('{"a": {"x": 1}}', "[1, 2, 3]")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_merge_validate_rejects_empty_object_fragment():
    result = _merge_validate('{"a": {"x": 1}}', "{}")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_merge_validate_rejects_no_fragments_at_all():
    result = _sh(f". '{LIB_SH}'\nclaude_gpt_agents_json_merge_validate\n")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


# --- Positive control: canary invocation actually reaches the Task tool ----
#
# AC15's "smoke が実際に Agent invocation まで到達したことの positive control"
# is exercised end-to-end by `runtime_smoke_test.sh`'s own Phase B
# `run_convo_step "subagent" ...` step (`SUBAGENT_MARKER_OK`, unchanged by
# this fix-delta other than now naming the dynamically-generated
# `$CANARY_AGENT_NAME` in its prompt instead of a static "canary" literal --
# see `test_runtime_smoke_sh_uses_the_fixture_function_not_a_static_literal`
# above). That step requires a live claude-code-proxy + authenticated
# runtime and is out of scope for this hermetic pytest file (it is covered
# by `scripts/claude-gpt/runtime_smoke_test.sh` itself, Runtime Verification
# Applicability: immediate). This suite instead proves, hermetically, that
# `runtime_smoke_test.sh` wires the dynamically-generated agent name into
# its own Task-tool-invocation prompt (rather than a stale static literal),
# which is the structural precondition for that live positive control to
# ever be checking the right agent.


def test_runtime_smoke_sh_prompt_references_dynamic_agent_name_variable():
    text = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    assert "${CANARY_AGENT_NAME}" in text
    assert "subagent named canary " not in text
    assert "subagent named canary\n" not in text
