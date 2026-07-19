"""AC1-AC5 (#1462): `check_implementation_overlap.py` の repository binding を
検証する。

online / dry-run 両方の呼び出し経路で `--repo owner/name` が必須になり、
canonicalize 済みの `repository` field が evidence payload のトップレベルと
`decision_inputs_sha256` の計算対象（`decision_payload`）の両方に含まれる
ことを、subprocess 経由で `tests/fixtures/overlap/` の固定 fixture を使い
検証する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
HELPER = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "implement-issue"
    / "scripts"
    / "check_implementation_overlap.py"
)
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "overlap"

DEFAULT_REPO = "squne121/loop-protocol"


def _run_cli(
    issue_number: int,
    current_file: Path,
    candidates_file: Path,
    *extra: str,
) -> Tuple[int, Dict[str, Any]]:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--issue-number",
            str(issue_number),
            "--dry-run",
            "--current-file",
            str(current_file),
            "--candidates-file",
            str(candidates_file),
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    return proc.returncode, payload


def _current_number(fixture_name: str) -> int:
    return json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))["number"]


# ------------------------------------------------------------
# AC1: --repo is required for dry-run
# ------------------------------------------------------------


def test_given_dry_run_without_repo_when_run_then_runtime_error_with_required_message() -> None:
    """AC1: dry-run 経路でも `--repo` が必須であり、欠落時は
    `OverlapRuntimeError`（"--repo is required for dry-run" 相当）で
    `runtime_error` route（exit 1）に倒す。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_self_only.json"
    current_number = _current_number("current_1451_analog.json")

    exit_code, payload = _run_cli(current_number, current_file, candidates_file)

    assert exit_code == 1, payload
    assert payload["route"] == "runtime_error"
    assert "--repo is required for dry-run" in payload["error"]


def test_given_online_without_repo_when_run_then_runtime_error() -> None:
    """既存の online 経路（`--repo` 欠落）も引き続き runtime_error になる。"""
    proc = subprocess.run(
        [sys.executable, str(HELPER), "--issue-number", "1", "--limit", "1"],
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["route"] == "runtime_error"
    assert "--repo is required for online fetch" in payload["error"]


# ------------------------------------------------------------
# AC2/AC3: repository field present + included in decision_inputs_sha256
# ------------------------------------------------------------


def test_given_valid_repo_when_run_then_evidence_top_level_repository_field_present() -> None:
    """AC2: evidence payload のトップレベルに canonicalize 済み `repository`
    field が含まれる。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_self_only.json"
    current_number = _current_number("current_1451_analog.json")

    exit_code, payload = _run_cli(
        current_number, current_file, candidates_file, "--repo", DEFAULT_REPO
    )

    assert exit_code == 0, payload
    assert payload["repository"] == DEFAULT_REPO


def test_given_different_repo_value_when_run_then_decision_inputs_sha256_changes() -> None:
    """AC3: `repository` field の値が `decision_payload`（`decision_inputs_sha256`
    の計算対象）にも含まれ、`repository` を変更すると `decision_inputs_sha256`
    が変化する。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_self_only.json"
    current_number = _current_number("current_1451_analog.json")

    _, payload_a = _run_cli(
        current_number, current_file, candidates_file, "--repo", "squne121/loop-protocol"
    )
    _, payload_b = _run_cli(
        current_number, current_file, candidates_file, "--repo", "someone-else/other-repo"
    )

    assert payload_a["repository"] != payload_b["repository"]
    assert payload_a["decision_inputs_sha256"] != payload_b["decision_inputs_sha256"]


# ------------------------------------------------------------
# AC4: repository field tamper -> evidence_sha256 mismatch
# ------------------------------------------------------------


def test_given_tampered_repository_field_when_recomputed_then_evidence_sha256_mismatches() -> None:
    """AC4: `repository` field を改ざんした fixture に対し、`evidence_sha256`
    の再計算結果が元の `evidence_sha256` と一致しない（tamper 検出）。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_self_only.json"
    current_number = _current_number("current_1451_analog.json")

    _, payload = _run_cli(
        current_number, current_file, candidates_file, "--repo", DEFAULT_REPO
    )
    original_evidence_sha256 = payload["evidence_sha256"]

    tampered = dict(payload)
    tampered["repository"] = "attacker/forged-repo"

    # evidence_sha256 は body 全体（evidence_sha256 自身を除く）から再計算される
    # canonical JSON の sha256 である。tamper 後の body から再計算した digest は
    # 元の evidence_sha256 と一致してはならない。
    recompute_source = dict(tampered)
    recompute_source.pop("evidence_sha256", None)
    canonical = json.dumps(recompute_source, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    import hashlib

    recomputed = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    assert recomputed != original_evidence_sha256


# ------------------------------------------------------------
# AC5: --repo case / notation variance canonicalization
# ------------------------------------------------------------


def test_given_mixed_case_repo_when_run_then_canonicalized_to_lowercase() -> None:
    """AC5: 大文字小文字や記法揺れのある `--repo` 入力（例:
    `squne121/LOOP-PROTOCOL`）が canonical value（小文字化された
    `owner/name`）へ正規化されて evidence に書き込まれる。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_self_only.json"
    current_number = _current_number("current_1451_analog.json")

    exit_code, payload = _run_cli(
        current_number, current_file, candidates_file, "--repo", "SQUNE121/LOOP-PROTOCOL"
    )

    assert exit_code == 0, payload
    assert payload["repository"] == "squne121/loop-protocol"


def test_given_two_notation_variants_when_run_then_same_canonical_repository_and_digest() -> None:
    """大文字小文字揺れの異なる 2 つの `--repo` 表記が同一 canonical value に
    正規化され、結果として `decision_inputs_sha256` も一致する。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_self_only.json"
    current_number = _current_number("current_1451_analog.json")

    _, payload_lower = _run_cli(
        current_number, current_file, candidates_file, "--repo", "squne121/loop-protocol"
    )
    _, payload_mixed = _run_cli(
        current_number, current_file, candidates_file, "--repo", "Squne121/Loop-Protocol"
    )

    assert payload_lower["repository"] == payload_mixed["repository"] == "squne121/loop-protocol"
    assert payload_lower["decision_inputs_sha256"] == payload_mixed["decision_inputs_sha256"]


def test_given_invalid_repo_format_when_run_then_runtime_error() -> None:
    """`owner/name` 形式でない `--repo` は fail-closed で `runtime_error` に倒す。"""
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_self_only.json"
    current_number = _current_number("current_1451_analog.json")

    exit_code, payload = _run_cli(
        current_number, current_file, candidates_file, "--repo", "not-a-valid-repo-format"
    )

    assert exit_code == 1, payload
    assert payload["route"] == "runtime_error"


# ------------------------------------------------------------
# Major 2 (PR #1474 レビュー): online 経路の repository canonicalization は
# GitHub API の `full_name`（rename/transfer 後の redirect を追跡）を正本に
# し、dry-run 経路は引き続きネットワークアクセスを避けた固定正規化規則
# （小文字化）へ fallback する。
# ------------------------------------------------------------


def test_given_dry_run_when_canonicalized_then_no_network_access_used_and_lowercase_applied() -> None:
    """dry-run 経路は `_canonicalize_repo(..., online=False)` を使い、
    GitHub API へアクセスせず静的な小文字化のみで canonicalize する。
    """
    import importlib.util
    import sys as _sys

    script_path = HELPER
    spec = importlib.util.spec_from_file_location(
        "check_implementation_overlap_repo_binding_major2", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    calls: list = []

    def _forbidden_fetch(repo: str):  # pragma: no cover - defensive
        calls.append(repo)
        raise AssertionError("dry-run canonicalization must not call GitHub API")

    module._fetch_repository_full_name = _forbidden_fetch  # type: ignore[assignment]

    result = module._canonicalize_repo("SQUNE121/LOOP-PROTOCOL", online=False)

    assert result == "squne121/loop-protocol"
    assert calls == []


def test_given_online_when_api_returns_renamed_full_name_then_canonical_repository_uses_it(monkeypatch) -> None:
    """online 経路（Major 2）: `GET /repos/{owner}/{repo}` が rename 後の
    現在の `full_name` を返す場合、evidence の `repository` field はその
    canonical value を使う（旧 alias の小文字化ではなく）。
    """
    import importlib.util
    import sys as _sys

    script_path = HELPER
    spec = importlib.util.spec_from_file_location(
        "check_implementation_overlap_repo_binding_major2_online", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(
        module, "_fetch_repository_full_name", lambda repo: "squne121/loop-protocol-renamed"
    )

    result = module._canonicalize_repo("squne121/old-alias", online=True)

    assert result == "squne121/loop-protocol-renamed"


def test_given_online_when_api_fetch_fails_then_falls_back_to_static_canonicalization(monkeypatch) -> None:
    """online 経路で `GET /repos/{owner}/{repo}` が失敗（None）した場合は
    固定の正規化規則（小文字化）へ fallback し、fail-closed にはしない
    （既存 online 経路の後方互換維持）。
    """
    import importlib.util
    import sys as _sys

    script_path = HELPER
    spec = importlib.util.spec_from_file_location(
        "check_implementation_overlap_repo_binding_major2_fallback", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "_fetch_repository_full_name", lambda repo: None)

    result = module._canonicalize_repo("SQUNE121/LOOP-PROTOCOL", online=True)

    assert result == "squne121/loop-protocol"


# ------------------------------------------------------------
# #1621 AC7: successor index compares (repository, issue_number) tuples --
# cross-repository issue numbers must not be treated as local successors.
# ------------------------------------------------------------


def test_given_cross_repository_native_blocking_same_number_then_successor_not_injected() -> None:
    """AC7: current の native blocking の repository が異なる場合、同一 issue
    number を持つ別 repository の候補と誤って successor 結合しない。
    """
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "check_implementation_overlap_repo_binding_ac7", HELPER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    current_raw = {
        "blocking": [{"repository": "other-owner/other-repo", "number": 9730, "state": "OPEN"}],
    }
    numbers = module._current_native_successor_numbers(current_raw, DEFAULT_REPO)
    assert numbers == frozenset()
    assert 9730 not in numbers


def test_given_same_repository_native_blocking_then_successor_injected() -> None:
    """AC7 の対照ケース: repository が current と一致する場合は index に含まれる。"""
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "check_implementation_overlap_repo_binding_ac7_positive", HELPER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    current_raw = {
        "blocking": [{"repository": DEFAULT_REPO, "number": 9731, "state": "OPEN"}],
    }
    numbers = module._current_native_successor_numbers(current_raw, DEFAULT_REPO)
    assert numbers == frozenset({9731})
