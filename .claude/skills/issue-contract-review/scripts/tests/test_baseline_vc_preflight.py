#!/usr/bin/env python3
"""
Unit tests for baseline_vc_preflight.py
"""

import json
import subprocess
import sys
from pathlib import Path


def run_preflight(fixture_file: str, issue_num: int = 999) -> dict:
    """fixture ファイルに対して preflight を実行"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--body-file",
            fixture_file,
            "--issue",
            str(issue_num),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    return json.loads(result.stdout)


def test_ac1_file_exists():
    """AC1: スクリプトが存在し py_compile が通る"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    assert script_path.exists(), f"Script not found: {script_path}"

    # py_compile check
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(script_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"py_compile failed: {result.stderr}"


def test_ac2_schema():
    """AC2: --issue --repo で JSON schema を返す"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    data = run_preflight(str(fixture))

    assert data["schema"] == "baseline_vc_preflight/v1"
    assert "results" in data
    assert data["issue"] == 999
    assert "repo" in data
    assert "generated_at" in data
    assert "source" in data
    assert data["source"]["kind"] == "body_file"


def test_ac3_body_file():
    """AC3: --body-file で fixture を入力でき GitHub 非依存に実行できる"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    data = run_preflight(str(fixture))

    assert data["schema"] == "baseline_vc_preflight/v1"
    assert len(data["results"]) > 0


def test_ac4_unexpected_pass():
    """AC4: exit_code=0 -> unexpected_pass / blocked"""
    fixture = Path(__file__).parent / "fixtures" / "command_passes.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0

    #少なくとも 1 つは unexpected_pass / blocked である
    found = any(
        r["classification"] == "unexpected_pass" and r["decision"] == "blocked"
        for r in results
    )
    assert found, "No unexpected_pass result found"


def test_ac5_expected_fail():
    """AC5: expected baseline fail だけ expected_fail / go"""
    fixture = Path(__file__).parent / "fixtures" / "expected_rg_no_match.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0

    # 少なくとも 1 つは expected_fail / go である
    found = any(
        r["classification"] == "expected_fail" and r["decision"] == "go"
        for r in results
    )
    assert found, "No expected_fail result found"


def test_ac6_env_missing_dep():
    """AC6: env_missing_dep は expected_fail にしない"""
    fixture = Path(__file__).parent / "fixtures" / "env_missing_dep.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0

    # env_missing_dep または file_not_found_unrunnable が blocked / human_judgment である
    found = any(
        r["category"] in ("env_missing_dep", "file_not_found_unrunnable")
        and r["decision"] in ("blocked", "human_judgment")
        for r in results
    )
    assert found, "No env_missing_dep / file_not_found_unrunnable result found"


def test_ac7_output_truncation():
    """AC7: stdout_head / stderr_head は配列で含まれる"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    for r in results:
        assert isinstance(r["stdout_head"], list), "stdout_head is not a list"
        assert isinstance(r["stderr_head"], list), "stderr_head is not a list"


def test_ac8_command_hash_stable():
    """AC8: command_hash は安定生成（同一 fixture 2 回実行で hash 一致）"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"

    run1 = run_preflight(str(fixture))
    run2 = run_preflight(str(fixture))

    hashes1 = [r["command_hash"] for r in run1["results"]]
    hashes2 = [r["command_hash"] for r in run2["results"]]

    assert hashes1 == hashes2, "command_hash unstable"


def test_ac9_compound_command():
    """AC9: compound command は compound_command_disallowed"""
    fixture = Path(__file__).parent / "fixtures" / "compound_command.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0

    # 少なくとも 1 つは compound_command_disallowed である
    found = any(
        r["category"] == "compound_command_disallowed" and r["decision"] == "blocked"
        for r in results
    )
    assert found, "No compound_command_disallowed result found"


if __name__ == "__main__":
    # Run tests
    test_ac1_file_exists()
    print("✓ AC1: file exists and py_compile passes")

    test_ac2_schema()
    print("✓ AC2: schema correct")

    test_ac3_body_file()
    print("✓ AC3: body-file works")

    test_ac4_unexpected_pass()
    print("✓ AC4: unexpected_pass / blocked")

    test_ac5_expected_fail()
    print("✓ AC5: expected_fail / go")

    test_ac6_env_missing_dep()
    print("✓ AC6: env_missing_dep blocked")

    test_ac7_output_truncation()
    print("✓ AC7: output truncation")

    test_ac8_command_hash_stable()
    print("✓ AC8: command_hash stable")

    test_ac9_compound_command()
    print("✓ AC9: compound_command_disallowed")

    print("\n✓ All tests passed!")
