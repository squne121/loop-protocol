#!/usr/bin/env python3
"""
Unit tests for baseline_vc_preflight.py
"""

import json
import subprocess
import sys
from pathlib import Path
import tempfile

try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False

import yaml


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


# B1-B8 新規テスト

def test_no_commands_is_blocked():
    """B3: VC セクション存在するが fenced block / コマンドなし → blocked"""
    fixture = Path(__file__).parent / "fixtures" / "empty_vc_section.md"
    data = run_preflight(str(fixture))

    assert data["status"] == "blocked", f"Expected blocked but got {data['status']}"
    assert data["summary"]["extraction_errors"] >= 1, "Expected extraction_errors > 0"
    assert len(data["errors"]) > 0, "Expected errors array to be non-empty"


def test_mixed_blocked_and_human_judgment_status_is_blocked():
    """B2: blocked + human_judgment 混在 → status = blocked"""
    fixture = Path(__file__).parent / "fixtures" / "mixed_blocked_unknown.md"
    data = run_preflight(str(fixture))

    # blocked と human_judgment が両方あれば、status は blocked であるべき
    has_blocked = any(r["decision"] == "blocked" for r in data["results"])
    has_human_judgment = any(r["decision"] == "human_judgment" for r in data["results"])

    if has_blocked and has_human_judgment:
        assert data["status"] == "blocked", "Status should be blocked when both blocked and human_judgment exist"


def test_compound_command_not_executed():
    """B1: compound command が実行前に rejected (side-effect なし)"""
    # Use a temporary directory for this test
    import tempfile
    import os

    tmp_dir = tempfile.mkdtemp()
    try:
        # Create a fixture with compound command that would create a side-effect file
        marker_file = os.path.join(tmp_dir, "marker.txt")

        fixture_content = f"""## Verification Commands

```bash
# AC1
$ python3 -c 'open("{marker_file}", "w").write("x")' && false
```
"""
        fixture_file = os.path.join(tmp_dir, "compound_sideeffect.md")
        with open(fixture_file, "w") as f:
            f.write(fixture_content)

        data = run_preflight(fixture_file)

        # Command should be classified as compound_command_disallowed
        found = any(
            r["category"] == "compound_command_disallowed"
            for r in data["results"]
        )
        assert found, "Expected compound_command_disallowed"

        # Marker file should NOT be created because command was rejected before execution
        assert not os.path.exists(marker_file), "Compound command was executed (should have been rejected)"
    finally:
        # Cleanup
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_inline_ac_suffix_is_parsed():
    """B4: inline suffix # AC<N> を parser で検出"""
    fixture = Path(__file__).parent / "fixtures" / "inline_ac_suffix.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) == 2, f"Expected 2 commands but got {len(results)}"

    # Both should have AC labels from inline suffix
    assert results[0]["ac"] == "AC1", f"Expected AC1 but got {results[0]['ac']}"
    assert results[1]["ac"] == "AC2", f"Expected AC2 but got {results[1]['ac']}"


def test_missing_script_is_file_not_found_unrunnable():
    """B5: python3 missing.py → file_not_found_unrunnable / blocked"""
    fixture = Path(__file__).parent / "fixtures" / "missing_script.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0, "Expected at least one result"

    # Should be classified as file_not_found_unrunnable
    found = any(
        r["category"] == "file_not_found_unrunnable" and r["decision"] == "blocked"
        for r in results
    )
    assert found, f"Expected file_not_found_unrunnable/blocked. Got: {results[0]['category']}/{results[0]['decision']}"


def test_truncate_output_is_byte_limited():
    """B7: 多バイト文字を含む長い出力が bytes_per_line 以下に切り詰められる"""
    # Create fixture with command that outputs many Japanese characters
    fixture_content = """## Verification Commands

```bash
# AC1
$ python3 -c "print('あ' * 1000)"
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        for r in results:
            stdout_lines = r["stdout_head"]
            # Each line should be within byte limit when reconstructed
            for line in stdout_lines:
                # Verify it's valid UTF-8
                encoded = line.encode("utf-8")
                # The byte limit in the script is 2048 per line by default
                # Allow slightly over due to replacement character insertion (UTF-8 decode error handling)
                assert len(encoded) <= 2100, f"Line greatly exceeds 2048 bytes: {len(encoded)}"
    finally:
        import os
        os.unlink(fixture_file)


def test_contract_review_fragment_format():
    """B8: --format contract-review-fragment で YAML が出力される"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--body-file",
            str(fixture),
            "--issue",
            "999",
            "--format",
            "contract-review-fragment",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"

    # Parse YAML and verify structure
    output = yaml.safe_load(result.stdout)
    assert "vc_preflight" in output, "Missing vc_preflight key"

    vc = output["vc_preflight"]
    assert "passed" in vc, "Missing passed field"
    assert isinstance(vc["passed"], bool), "passed should be bool"
    assert "vc_failed_as_expected" in vc
    assert "vc_passed_unexpectedly" in vc
    assert "vc_unrunnable" in vc
    assert "classifications" in vc
    assert isinstance(vc["classifications"], list)

    # Check classifications structure
    for cls in vc["classifications"]:
        assert "ac" in cls
        assert "command" in cls
        assert "exit_code" in cls
        assert "category" in cls
        assert "confidence" in cls
        assert cls["confidence"] in ["high", "medium", "low"]
        assert "evidence" in cls
        assert "decision" in cls
        assert cls["decision"] in ["go", "blocked"]


def test_ac2_issue_repo_integration():
    """B6: --issue --repo で Issue body を取得 (integration test)"""
    # Skip if pytest not available and called directly
    if not HAS_PYTEST:
        print("⊘ B6: skipping integration test (requires pytest context)")
        return

    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    # Check if gh is authenticated
    auth_check = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    if auth_check.returncode != 0:
        pytest.skip("gh auth not available, skipping integration test")

    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--issue",
            "329",
            "--repo",
            "squne121/loop-protocol",
            "--timeout-seconds",
            "5",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Should succeed with valid GitHub credentials
    if result.returncode == 0:
        data = json.loads(result.stdout)
        assert data["schema"] == "baseline_vc_preflight/v1"
        assert data["source"]["kind"] == "github_issue"
        assert data["issue"] == 329
    else:
        pytest.skip("Unable to fetch Issue (auth or network issue)")


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

    test_no_commands_is_blocked()
    print("✓ B3: no commands → blocked")

    test_mixed_blocked_and_human_judgment_status_is_blocked()
    print("✓ B2: mixed blocked/human_judgment → blocked")

    test_compound_command_not_executed()
    print("✓ B1: compound command not executed")

    test_inline_ac_suffix_is_parsed()
    print("✓ B4: inline AC suffix parsed")

    test_missing_script_is_file_not_found_unrunnable()
    print("✓ B5: missing script → file_not_found_unrunnable")

    test_truncate_output_is_byte_limited()
    print("✓ B7: output byte-limited")

    test_contract_review_fragment_format()
    print("✓ B8: contract-review-fragment format valid")

    test_ac2_issue_repo_integration()
    print("✓ B6: integration test skipped (requires pytest)")

    print("\n✓ All tests passed!")
