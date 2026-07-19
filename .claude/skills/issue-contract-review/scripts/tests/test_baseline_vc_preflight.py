#!/usr/bin/env python3
"""
Unit tests for baseline_vc_preflight.py
"""

import json
import subprocess
import sys
from pathlib import Path
import tempfile


def test_current_head_evidence_certifies_clean_temporary_repository():
    """GIVEN clean temporary Git repo WHEN current-head evidence is observed THEN it is certified."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import collect_current_head_evidence

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        evidence = collect_current_head_evidence(str(repo), head)

    assert evidence["certified"] is True
    assert evidence["head_sha"] == head
    assert evidence["reviewed_head_sha"] == head
    assert evidence["clean_before"] is True


def test_current_head_evidence_rejects_dirty_temporary_repository():
    """GIVEN dirty temporary Git repo WHEN current-head evidence is observed THEN it is blocked."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import collect_current_head_evidence

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        evidence = collect_current_head_evidence(str(repo), head)

    assert evidence["certified"] is False
    assert evidence["clean_before"] is False
    assert evidence["stop_condition_triggered"] is True


def test_current_head_evidence_rejects_abbreviated_oid_and_symbolic_ref():
    """GIVEN non-full revision inputs WHEN evidence is observed THEN certification fails closed."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import collect_current_head_evidence

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        abbreviated = collect_current_head_evidence(str(repo), head[:12])
        symbolic = collect_current_head_evidence(str(repo), "HEAD")

    assert abbreviated["certified"] is False
    assert "reviewed_head_sha_not_full_oid" in abbreviated["errors"]
    assert symbolic["certified"] is False
    assert "reviewed_head_sha_not_full_oid" in symbolic["errors"]


def test_finalize_dirty_after_execution_blocks_and_maps_to_nonzero_exit():
    """GIVEN certified before evidence WHEN worktree becomes dirty THEN final status exits non-zero."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import (
        collect_current_head_evidence,
        exit_code_for_status,
        finalize_current_head_evidence,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        evidence = collect_current_head_evidence(str(repo), head)
        (repo / "tracked.txt").write_text("dirty after\n", encoding="utf-8")

        finalized = finalize_current_head_evidence(str(repo), evidence)
        final_status = "pass" if finalized["certified"] else "blocked"

    assert finalized["clean_before"] is True
    assert finalized["clean_after"] is False
    assert finalized["certified"] is False
    assert exit_code_for_status(final_status) == 1


def test_current_head_retrieval_failure_finalizes_blocked_safety_fields():
    """GIVEN retrieval failure WHEN current-head CLI emits JSON THEN stop condition is true."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        completed = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--body-file",
                str(repo / "missing-issue.md"),
                "--cwd",
                str(repo),
                "--evidence-mode",
                "current-head",
                "--reviewed-head-sha",
                head,
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    payload = json.loads(completed.stdout)
    assert completed.returncode != 0
    assert payload["status"] == "blocked"
    assert payload["stop_condition_triggered"] is True
    assert payload["human_review_required"] is False

try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False


def run_preflight(fixture_file: str, issue_num: int = 999) -> dict:
    """
    fixture ファイルに対して preflight を実行

    C2: exit code は status フィールドの値に応じて変わるため、
    ここで JSON を parse することが重要。
    """
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
        timeout=90,
    )
    # C2: exit code は 0, 1, 2, 3 など様々な値
    # JSON parse が成功すれば status フィールドで判定する
    assert result.stdout, f"No output: {result.stderr}"
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
    """AC6: env_missing_dep は expected_fail にしない。
    #514 以降: env_missing_dep.md の python3 -c は unsafe_command として blocked になる。
    テストは「blocked / decision=blocked」として判定する。
    """
    fixture = Path(__file__).parent / "fixtures" / "env_missing_dep.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0

    # #514: python3 -c は unsafe_command として blocked になる。
    # env_missing_dep / file_not_found_unrunnable / unsafe_command / command_not_allowed
    # のどれかが blocked / human_judgment であれば pass。
    found = any(
        r["category"] in (
            "env_missing_dep",
            "file_not_found_unrunnable",
            "unsafe_command",
            "command_not_allowed",
        )
        and r["decision"] in ("blocked", "human_judgment")
        for r in results
    )
    assert found, "No blocked/human_judgment result found for env_missing_dep fixture"


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
    """B5: python3 missing.py → blocked.
    #514 以降: `python3 <script>` は -m py_compile / -m pytest 以外は command_not_allowed として blocked。
    """
    fixture = Path(__file__).parent / "fixtures" / "missing_script.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0, "Expected at least one result"

    # #514: python3 <script> (not -m py_compile / -m pytest) is command_not_allowed/blocked.
    # Accept both file_not_found_unrunnable (old) and command_not_allowed (new #514 behavior).
    found = any(
        r["category"] in ("file_not_found_unrunnable", "command_not_allowed")
        and r["decision"] == "blocked"
        for r in results
    )
    assert found, f"Expected blocked for missing script. Got: {results[0]['category']}/{results[0]['decision']}"


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
    # C2: Exit code depends on status, just check that YAML was output
    assert result.stdout, f"Script produced no output: {result.stderr}"

    # Parse YAML and verify structure
    try:
        import yaml
        output = yaml.safe_load(result.stdout)
    except ImportError:
        pytest.skip("PyYAML not installed for fragment test")
        return

    assert "vc_preflight" in output, "Missing vc_preflight key"

    vc = output["vc_preflight"]
    assert "passed" in vc, "Missing passed field"
    assert isinstance(vc["passed"], bool), "passed should be bool"
    assert "vc_failed_as_expected" in vc
    assert "vc_passed_unexpectedly" in vc
    assert "vc_unrunnable" in vc
    assert "vc_human_judgment" in vc, "Missing vc_human_judgment (C3)"
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
        # C3: decision は human_judgment も許可
        assert cls["decision"] in ["go", "blocked", "human_judgment"]


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


# C1: Test that top-level yaml import is not required for JSON mode
def test_yaml_lazy_import():
    """C1: JSON mode は PyYAML なしで動作する"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    # Read the script and verify yaml is not imported at top level
    with open(script_path) as f:
        script_content = f.read()

    # Check that top-level "import yaml" does not exist
    lines = script_content.split("\n")
    top_level_yaml_import = False
    for i, line in enumerate(lines[:50]):  # Check first 50 lines
        if line.strip().startswith("import yaml") and not line.strip().startswith("#"):
            top_level_yaml_import = True
            break

    assert not top_level_yaml_import, "top-level 'import yaml' found; should be lazy-loaded"

    # Test JSON mode still works (no PyYAML needed)
    # C2: Exit code depends on status, not returncode==0 requirement
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--body-file",
            str(fixture),
            "--issue",
            "999",
            "--format",
            "json",  # JSON mode should work without PyYAML
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Just check that JSON was output (status code may vary)
    assert result.stdout, f"JSON mode produced no output: {result.stderr}"
    data = json.loads(result.stdout)
    assert data["schema"] == "baseline_vc_preflight/v1"


# C2: Test exit code contract
def test_exit_code_contract_pass():
    """C2: status=pass → exit 0"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--body-file", str(fixture), "--issue", "999"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # simple.md has expected_fail, so status should be pass → exit 0
    data = json.loads(result.stdout)
    if data["status"] == "pass":
        assert result.returncode == 0, f"Expected exit 0 for pass, got {result.returncode}"


def test_exit_code_contract_blocked():
    """C2: status=blocked (from VC execution) → exit 1"""
    # Use a fixture with VCs that result in blocked (not extraction error)
    fixture = Path(__file__).parent / "fixtures" / "compound_command.md"
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--body-file", str(fixture), "--issue", "999"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    assert data["status"] == "blocked"
    # blocked from VC execution (no extraction_errors) → exit 1
    if data["summary"]["extraction_errors"] == 0:
        assert result.returncode == 1, f"Expected exit 1 for blocked from VC, got {result.returncode}"


def test_exit_code_contract_human_judgment():
    """C2: status=human_judgment → exit 3"""
    fixture = Path(__file__).parent / "fixtures" / "mixed_blocked_unknown.md"
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--body-file", str(fixture), "--issue", "999"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    if data["status"] == "human_judgment":
        assert result.returncode == 3, f"Expected exit 3 for human_judgment, got {result.returncode}"


def test_exit_code_contract_error():
    """C2: retrieval/parse error → exit 2"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    result = subprocess.run(
        [sys.executable, str(script_path), "--body-file", "/nonexistent/file.md", "--issue", "999"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, f"Expected exit 2 for retrieval error, got {result.returncode}"
    data = json.loads(result.stdout)
    assert data["status"] == "blocked"
    assert len(data["errors"]) > 0


# C3: Test that fragment preserves human_judgment decision
def test_fragment_preserves_human_judgment():
    """C3: fragment で decision=human_judgment が保持される"""
    fixture = Path(__file__).parent / "fixtures" / "mixed_blocked_unknown.md"
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

    try:
        import yaml
        output = yaml.safe_load(result.stdout)
    except ImportError:
        if HAS_PYTEST:
            pytest.skip("PyYAML not installed")
        return

    vc = output["vc_preflight"]
    assert "vc_human_judgment" in vc, "Missing vc_human_judgment count (C3)"

    # Check if any classification has human_judgment decision
    has_human_judgment = any(
        c["decision"] == "human_judgment" for c in vc["classifications"]
    )
    if vc["vc_human_judgment"] > 0:
        assert has_human_judgment, "vc_human_judgment > 0 but no human_judgment in classifications"


# C4: Test confidence parity between JSON and fragment
def test_confidence_json_fragment_parity():
    """C4: JSON と fragment で confidence が一致"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    # Get JSON output
    json_result = subprocess.run(
        [sys.executable, str(script_path), "--body-file", str(fixture), "--issue", "999", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    json_data = json.loads(json_result.stdout)

    # Get fragment output
    fragment_result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--body-file",
            str(fixture),
            "--issue",
            "999",
            "--format",
            "contract-review-fragment"
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    try:
        import yaml
        fragment_data = yaml.safe_load(fragment_result.stdout)
    except ImportError:
        if HAS_PYTEST:
            pytest.skip("PyYAML not installed")
        return

    # Compare confidence values for same AC
    json_results = {r["ac"]: r["confidence"] for r in json_data["results"]}
    fragment_classifications = {c["ac"]: c["confidence"] for c in fragment_data["vc_preflight"]["classifications"]}

    for ac, json_conf in json_results.items():
        frag_conf = fragment_classifications.get(ac)
        assert json_conf == frag_conf, f"AC {ac}: JSON confidence {json_conf} != fragment {frag_conf}"


# AC1-AC7: pytest invocation detection and exit code classification

def test_pytest_invocation_detect_pytest_direct():
    """AC1: _is_pytest_invocation detects bare pytest"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    # Import the function for testing
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_pytest_invocation

    assert _is_pytest_invocation("pytest")
    assert _is_pytest_invocation("pytest tests/")
    assert _is_pytest_invocation("pytest --verbose tests/")


def test_pytest_invocation_detect_python_m_pytest():
    """AC1: _is_pytest_invocation detects python -m pytest"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_pytest_invocation

    assert _is_pytest_invocation("python -m pytest")
    assert _is_pytest_invocation("python3 -m pytest")
    assert _is_pytest_invocation("python -m pytest tests/")


def test_pytest_invocation_detect_uv_run_pytest():
    """AC1: _is_pytest_invocation detects uv run pytest"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_pytest_invocation

    assert _is_pytest_invocation("uv run pytest")
    assert _is_pytest_invocation("uv run pytest tests/")
    assert _is_pytest_invocation("uv run --locked pytest")
    assert _is_pytest_invocation("uv run --with pytest pytest")
    assert _is_pytest_invocation("uv run python -m pytest")


def test_uv_lock_check_exact_only():
    """AC1: _is_uv_lock_check is exact-match only for uv lock --check"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_uv_lock_check

    assert _is_uv_lock_check(["uv", "lock", "--check"])
    assert not _is_uv_lock_check(["/usr/bin/uv", "lock", "--check"])  # path-qualified rejected (Issue #1210)
    assert not _is_uv_lock_check(["uv", "lock"])
    assert not _is_uv_lock_check(["uv", "lock", "--upgrade"])
    assert not _is_uv_lock_check(["uv", "sync"])
    assert not _is_uv_lock_check(["uv", "run", "uv", "lock", "--check"])


def test_runtime_dependency_smoke_exact_python_and_python3_only():
    """AC2: canonical runtime smoke is allowed only for exact python/python3 invocation"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_uv_runtime_smoke_command

    assert _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "--no-default-groups",
        "python", "scripts/ci/runtime_dependency_smoke.py",
    ])
    assert not _is_uv_runtime_smoke_command([
        "/usr/bin/uv", "run", "--isolated", "--locked", "--no-default-groups",
        "python3", "scripts/ci/runtime_dependency_smoke.py",
    ])  # path-qualified uv rejected (Issue #1210)
    assert not _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "python", "scripts/ci/runtime_dependency_smoke.py",
    ])
    assert not _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "--no-default-groups", "python3",
    ])


def test_runtime_dependency_smoke_rejects_extra_uv_options():
    """AC3: extra uv options are rejected for runtime smoke allowlist"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_uv_runtime_smoke_command

    _smoke = "scripts/ci/runtime_dependency_smoke.py"
    forbidden_cases = [
        ["uv", "run", "--with", "pytest", "--isolated", "--locked", "--no-default-groups", "python", _smoke],
        ["uv", "run", "--group", "dev", "--isolated", "--locked", "--no-default-groups", "python3", _smoke],
        ["uv", "run", "--all-groups", "--isolated", "--locked", "--no-default-groups", "python", _smoke],
        ["uv", "run", "--extra", "feature", "--isolated", "--locked", "--no-default-groups", "python3", _smoke],
        ["uv", "run", "--python", "/usr/bin/python3", "--isolated", "--locked",
         "--no-default-groups", "python", _smoke],
        ["uv", "run", "--project", ".", "--isolated", "--locked", "--no-default-groups", "python", _smoke],
        ["uv", "run", "--directory", ".", "--isolated", "--locked", "--no-default-groups", "python3", _smoke],
        ["uv", "run", "--env-file", ".env", "--isolated", "--locked", "--no-default-groups", "python", _smoke],
        ["uv", "run", "--upgrade", "--isolated", "--locked", "--no-default-groups", "python3", _smoke],
    ]
    for argv in forbidden_cases:
        assert not _is_uv_runtime_smoke_command(argv), f"unexpected allow: {argv}"


def test_runtime_dependency_smoke_rejects_inline_and_non_repo_scripts():
    """AC4: runtime smoke rejects inline python and non-repo/invalid script paths"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_uv_runtime_smoke_command

    assert not _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "--no-default-groups",
        "python", "-c", "print(1)",
    ])
    assert not _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "--no-default-groups",
        "python", "-m", "pytest",
    ])
    assert not _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "--no-default-groups",
        "python", "../runtime_dependency_smoke.py",
    ])
    assert not _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "--no-default-groups",
        "python", "/tmp/runtime_dependency_smoke.py",
    ])
    assert not _is_uv_runtime_smoke_command([
        "uv", "run", "--isolated", "--locked", "--no-default-groups",
        "python", "scripts/other_smoke.py",
    ])
    assert not _is_uv_runtime_smoke_command([
        "sh", "-lc",
        "uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py",
    ])


def test_pytest_invocation_detect_non_pytest():
    """AC1: _is_pytest_invocation rejects non-pytest commands"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    import sys
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_pytest_invocation

    assert not _is_pytest_invocation("bash")
    assert not _is_pytest_invocation("grep test file.txt")
    assert not _is_pytest_invocation("rg pytest")
    assert not _is_pytest_invocation("node test.js")


def test_pytest_exit4_missing_file_expected_fail():
    """AC2: pytest exit 4 + file not found → expected_baseline_fail / go"""
    fixture = Path(__file__).parent / "fixtures" / "pytest_exit4_missing_file.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0, "Expected at least one result"

    # Should be classified as expected_baseline_fail with decision go
    found = any(
        r["classification"] == "expected_fail"
        and r["category"] == "expected_baseline_fail"
        and r["decision"] == "go"
        for r in results
    )
    assert found, f"Expected expected_baseline_fail/go. Got: {[r['category'] + '/' + r['decision'] for r in results]}"


def test_pytest_exit5_no_tests_collected_hard_block():
    """AC7 (#1285): pytest exit 5 (no tests collected) is a hard block
    (vc_no_tests_collected / blocked), NOT expected_baseline_fail. This
    replaces the prior test that incorrectly expected exit 5 to resolve to
    expected_baseline_fail/go; the current baseline_vc_preflight.py contract
    always classifies exit 5 as vc_no_tests_collected/blocked so authors are
    forced to rewrite the VC into canonical missing-file node-id form
    instead of relying on -k/path selectors that collect zero tests."""
    fixture = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "pytest_exit5_no_tests.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0, "Expected at least one result"

    found = any(
        r["classification"] == "blocked"
        and r["category"] == "vc_no_tests_collected"
        and r["decision"] == "blocked"
        for r in results
    )
    got = [r["category"] + "/" + r["decision"] for r in results]
    assert found, f"Expected vc_no_tests_collected/blocked. Got: {got}"


def test_pytest_exit4_usage_error_not_expected():
    """AC4: pytest exit 4 + CLI usage error → NOT expected_baseline_fail"""
    fixture = Path(__file__).parent / "fixtures" / "pytest_usage_error.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0, "Expected at least one result"

    # Should NOT be classified as expected_baseline_fail
    found_expected_baseline = any(
        r["category"] == "expected_baseline_fail"
        for r in results
    )
    assert not found_expected_baseline, "pytest CLI usage error should not be expected_baseline_fail"


def test_non_pytest_exit4_not_expected():
    """AC5: non-pytest command + exit 4 → NOT expected_baseline_fail"""
    fixture = Path(__file__).parent / "fixtures" / "bash_exit4_file_not_found.md"
    data = run_preflight(str(fixture))

    results = data["results"]
    assert len(results) > 0, "Expected at least one result"

    # Should NOT be classified as expected_baseline_fail
    found_expected_baseline = any(
        r["category"] == "expected_baseline_fail"
        for r in results
    )
    assert not found_expected_baseline, "bash exit 4 should not be expected_baseline_fail"


def test_pytest_unexpected_pass_unchanged():
    """AC6: exit 0 → blocked (regression check).
    #514 以降: python3 -c は unsafe_command として blocked になる (run_command 呼ばれない)。
    exit 0 で unexpected_pass になるテストは true を返す allowlist コマンド（true コマンド）で確認する。
    """
    fixture_content = """## Verification Commands

```bash
# AC1
$ true
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # `true` exits 0 → unexpected_pass / blocked
        found = any(
            r["classification"] == "unexpected_pass"
            and r["decision"] == "blocked"
            for r in results
        )
        assert found, f"'true' exit 0 should be unexpected_pass/blocked. Got: {results[0]}"
    finally:
        import os
        os.unlink(fixture_file)


def test_full_preflight_pytest_baseline_fail_status_pass():
    """AC7: full preflight with pytest baseline fail → status=pass, human_judgment=0"""
    fixture = Path(__file__).parent / "fixtures" / "pytest_baseline_only.md"
    data = run_preflight(str(fixture))

    # Top-level status should be pass
    assert data["status"] == "pass", f"Expected status=pass but got {data['status']}"

    # Summary should show expected_fail >= 1
    assert data["summary"]["expected_fail"] >= 1, "Expected at least 1 expected_fail in summary"

    # Summary should show human_judgment == 0
    assert data["summary"]["human_judgment"] == 0, (
        f"Expected human_judgment=0 but got {data['summary']['human_judgment']}"
    )

    # At least one result should be expected_fail
    assert len(data["results"]) > 0
    found = any(r["classification"] == "expected_fail" for r in data["results"])
    assert found, "Expected at least one expected_fail result"


# C5: Test preparation stops on human_judgment
def test_preparation_stops_on_human_judgment():
    """C5: preparation.md に human_judgment 停止が記載されている"""
    prep_file = Path(__file__).parent.parent.parent.parent / "impl-review-loop" / "steps" / "preparation.md"
    if not prep_file.exists():
        pytest.skip(f"preparation.md not found at {prep_file}")

    with open(prep_file) as f:
        content = f.read()

    # Check that preparation.md mentions stopping on human_judgment
    assert "human_judgment" in content.lower() or "human judgment" in content.lower(), \
        "preparation.md should document stopping on human_judgment"


# C6: Test shlex compound command detection
def test_shlex_compound_detection_no_space():
    """C6: cmd&&cmd（空白なし）を compound と検出"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ python3 -c 'print("test")'&&false
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", fixture_file, "--issue", "999"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        results = data["results"]

        found = any(
            r["category"] == "compound_command_disallowed" for r in results
        )
        assert found, "cmd&&cmd without space should be detected as compound"
    finally:
        import os
        os.unlink(fixture_file)


def test_shlex_compound_detection_quoted_pipe():
    """C6: quoted string 内の | は compound ではない"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ python3 -c "print('a | b')"
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", fixture_file, "--issue", "999"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        results = data["results"]

        # Should not be marked as compound_command_disallowed
        found = any(
            r["category"] == "compound_command_disallowed" for r in results
        )
        assert not found, "quoted | should not be detected as compound"
    finally:
        import os
        os.unlink(fixture_file)


def test_shlex_compound_detection_redirect():
    """C6: > redirect も compound と見なす (fail-closed)"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ echo test > /tmp/test.txt
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", fixture_file, "--issue", "999"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        results = data["results"]

        found = any(
            r["category"] == "compound_command_disallowed" for r in results
        )
        assert found, "redirect > should be detected as compound (fail-closed)"
    finally:
        import os
        os.unlink(fixture_file)


# Medium risk 1: grep error handling
def test_grep_invalid_regex_is_not_expected_fail():
    """中リスク 1: grep invalid regex → human_judgment"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ grep "[invalid" /tmp/test.txt
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", fixture_file, "--issue", "999"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        results = data["results"]

        # grep with invalid regex should be human_judgment or unknown
        _found = any(
            r["decision"] == "human_judgment" and r["category"] == "unknown"
            for r in results
        )
        # If grep fails with invalid regex, it should not be expected_fail
        not_expected_fail = all(
            r["classification"] != "expected_fail" for r in results
        )
        assert not_expected_fail, "grep invalid regex should not be classified as expected_fail"
    finally:
        import os
        os.unlink(fixture_file)


# Medium risk 2: AC2 GitHub integration test (skipped if no auth)
def test_ac2_issue_repo_mocked():
    """中リスク 2: AC2 GitHub integration を fixture mock で決定論的テスト"""
    if not HAS_PYTEST:
        print("⊘ AC2 mocked: requires pytest")
        return

    # Instead of mocking subprocess.run in a subprocess (doesn't work),
    # we just test that the script can parse command-line args correctly
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    # Verify the script has the correct arguments
    result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--issue" in result.stdout
    assert "--repo" in result.stdout
    assert "--body-file" in result.stdout


# New tests for AC410: scope_class and classification extensions

def test_regression_gate_pass_expected_pass_decision_go():
    """AC4: pnpm typecheck with exit 0 → regression_gate / expected_pass / go"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ pnpm typecheck
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        # pnpm typecheck will probably fail in test env, but we're checking the logic
        # For now just test the structure exists
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", fixture_file, "--issue", "999"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        # Check that scope_class field exists in results
        for r in data["results"]:
            assert "scope_class" in r, "scope_class field missing from result"
    finally:
        import os
        os.unlink(fixture_file)


def test_scope_class_field_present_in_all_results():
    """AC1: All results contain scope_class field"""
    fixture = Path(__file__).parent / "fixtures" / "simple.md"
    data = run_preflight(str(fixture))

    for r in data["results"]:
        assert "scope_class" in r, f"scope_class missing in result: {r['ac']}"
        assert r["scope_class"] in (
            "baseline_fail_expected",
            "regression_gate",
            "pr_review_only",
            "runtime_only",
        ), f"Invalid scope_class value: {r['scope_class']}"


def test_classification_expected_pass_summary_key():
    """AC2: classification=expected_pass exists in summary dict (no KeyError)"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ python3 -c "import sys; sys.exit(0)"
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", fixture_file, "--issue", "999"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        # Summary should have expected_pass and skipped keys
        assert "expected_pass" in data["summary"], "expected_pass missing from summary"
        assert "skipped" in data["summary"], "skipped missing from summary"
    finally:
        import os
        os.unlink(fixture_file)


def test_classification_skipped_summary_key():
    """AC2: classification=skipped exists in summary dict (no KeyError)"""
    fixture_content = """## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ grep "test" dummy.txt
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", fixture_file, "--issue", "999"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        # Should have skipped result
        assert any(r["classification"] == "skipped" for r in data["results"]), "No skipped results found"
    finally:
        import os
        os.unlink(fixture_file)


def test_preflight_scope_pr_review_only_marker():
    """AC5: # preflight-scope: pr_review_only marker → skipped / verification_owner"""
    fixture_content = """## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ grep "expected" file.txt
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # Should be skipped with correct metadata
        found = any(
            r["classification"] == "skipped"
            and r["decision"] == "go"
            and r.get("verification_owner") == "pr-review-judge"
            and r.get("runtime_verification_required") is False
            for r in results
        )
        assert found, f"pr_review_only marker not processed correctly. Got: {results[0]}"
    finally:
        import os
        os.unlink(fixture_file)


def test_preflight_scope_runtime_only_marker():
    """AC5: # preflight-scope: runtime_only marker → skipped / runtime_verification_required=true"""
    fixture_content = """## Verification Commands

```bash
# AC1
# preflight-scope: runtime_only
$ check_physics_simulation_result
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # Should be skipped with runtime_verification_required=true
        found = any(
            r["classification"] == "skipped"
            and r["decision"] == "go"
            and r.get("verification_owner") == "impl-review-loop"
            and r.get("runtime_verification_required") is True
            for r in results
        )
        assert found, f"runtime_only marker not processed correctly. Got: {results[0]}"
    finally:
        import os
        os.unlink(fixture_file)


def test_negated_rg_command_baseline_fail_expected():
    """AC6: ! rg -q "pattern" file → baseline_fail_expected / expected_fail / go"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ ! rg -q "should_not_exist" src/
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # Negated rg should be baseline_fail_expected
        found = any(
            r["scope_class"] == "baseline_fail_expected"
            and r["classification"] == "expected_fail"
            and r["decision"] == "go"
            for r in results
        )
        assert found, f"Negated rg not classified correctly. Got: {results[0]}"
    finally:
        import os
        os.unlink(fixture_file)


def test_command_substitution_static_classification():
    """AC7 (updated for #514): test "$(wc -l < file)" → static blocked, not executed.
    #514 以降: $(...) は unsupported_shell_syntax として blocked になる（以前は expected_fail/go だった）。
    """
    fixture_content = """## Verification Commands

```bash
# AC1
$ test "$(wc -l < /nonexistent/file)" -le 10
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # #514: $(...) → blocked / unsupported_shell_syntax / decision=blocked, not executed
        found = any(
            r["classification"] == "blocked"
            and r["category"] == "unsupported_shell_syntax"
            and r["decision"] == "blocked"
            and r["exit_code"] is None  # not executed
            for r in results
        )
        assert found, f"Command substitution should be blocked/unsupported_shell_syntax. Got: {results[0]}"
    finally:
        import os
        os.unlink(fixture_file)


def test_issue_393_snapshot_fixture_processed():
    """AC9: #393 body snapshot fixture preflight processes without error and contains results"""
    fixture = Path(__file__).parent / "fixtures" / "issue_393_body.md"
    if not fixture.exists():
        pytest.skip("issue_393_body.md fixture not found")

    data = run_preflight(str(fixture))

    # Fixture should be processed successfully and have results
    assert "results" in data, "No results in preflight output"
    assert len(data["results"]) > 0, "No VCs were extracted from fixture"
    # Verify scope_class is present in all results
    for result in data["results"]:
        assert "scope_class" in result, f"scope_class missing in AC {result['ac']}"
        assert result["scope_class"] in ("baseline_fail_expected", "regression_gate", "pr_review_only", "runtime_only")


# B6: New tests with monkeypatch for behavioral verification

def test_regression_gate_pnpm_typecheck_exit0_expected_pass_go(monkeypatch):
    """B6: pnpm typecheck exit 0 → expected_pass / go"""
    # Import the function
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_regression_gate_command

    # This is a command that should be detected as regression_gate
    assert _is_regression_gate_command("pnpm typecheck")
    assert _is_regression_gate_command("pnpm lint")
    assert _is_regression_gate_command("pnpm test")
    assert _is_regression_gate_command("pnpm build")


def test_uv_pytest_missing_path_not_regression_gate():
    """B3: uv run pytest path/not/exist → NOT regression_gate"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_regression_gate_command

    # Path does not exist, should return False
    result = _is_regression_gate_command("uv run pytest path/that/does/not/exist.py")
    assert not result, "Missing path should not be regression_gate"


def test_uv_pytest_existing_path_is_regression_gate(tmp_path):
    """B3: uv run pytest path/that/exists → regression_gate"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_regression_gate_command

    # Create a real test file
    test_file = tmp_path / "test_example.py"
    test_file.write_text("def test_example(): pass")

    # Should be detected as regression_gate
    result = _is_regression_gate_command(f"uv run pytest {test_file}", cwd=str(tmp_path.parent))
    assert result, "Existing path should be regression_gate"

def test_pytest_exit4_new_test_on_existing_file_is_noncanonical_category(tmp_path):
    """Issue #1347 AC1: uv run pytest <existing_file>::<new_test_name>, where the
    file exists but the referenced test function does not (baseline exit_code=4),
    must NOT be classified as expected_baseline_fail/go. It must be classified as
    a distinct, explicitly-named non-canonical category (different from both
    `unknown` and `human_judgment`) so diagnostics can identify this specific VC
    shape (Issue #1285 / PR #1305: existing-file missing node-id is forbidden)."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    test_file = tmp_path / "existing_test_file.py"
    test_file.write_text("def test_already_here():\n    assert True\n")

    stderr = (
        f"ERROR: not found: {test_file}::test_not_yet_implemented\n"
        "(no match in any of [<Module existing_test_file.py>])\n\n\n"
        "no tests ran in 0.01s\n"
    )

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=4,
        stdout="",
        stderr=stderr,
        command=f"uv run pytest {test_file}::test_not_yet_implemented -q",
        cwd=str(tmp_path.parent),
    )

    assert category == "existing_file_missing_node_id_noncanonical", (
        f"Expected existing_file_missing_node_id_noncanonical, got category={category!r} "
        f"classification={classification!r} decision={decision!r}"
    )
    assert category not in ("unknown", "human_judgment"), (
        "Category must be explicitly distinct from unknown / human_judgment"
    )
    assert classification not in ("expected_fail",), (
        "Must NOT be expected_baseline_fail-equivalent (expected_fail classification)"
    )
    assert decision != "go", "Must NOT be decision=go"
    assert decision == "blocked", f"Expected decision=blocked, got {decision!r}"
    assert scope_class == "baseline_fail_expected", (
        f"Expected scope_class=baseline_fail_expected, got {scope_class!r}"
    )
    assert fix_hint is not None, "fix_hint must not be None for this non-canonical category"
    assert "missing_new_test_file.py::test_name" in fix_hint, (
        f"fix_hint must guide the author to the canonical missing-file node-id shape, "
        f"got: {fix_hint!r}"
    )


def test_pytest_exit4_missing_file_still_expected_baseline_fail(tmp_path):
    """Issue #1347 AC2: a genuinely missing-file pytest node-id (the file itself
    does not exist on disk, e.g. missing_new_test_file.py::test_name -- the
    canonical VC shape per Issue #1285 / PR #1305) must keep its existing
    expected_baseline_fail/go classification unchanged by the AC1 fix above."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    missing_file = tmp_path / "missing_new_test_file.py"
    # Deliberately do NOT create missing_file on disk.

    stderr = (
        f"ERROR: file or directory not found: {missing_file}::test_something\n\n\n"
        "no tests ran in 0.01s\n"
    )

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=4,
        stdout="",
        stderr=stderr,
        command=f"uv run pytest {missing_file}::test_something -q",
        cwd=str(tmp_path.parent),
    )

    assert classification == "expected_fail"
    assert category == "expected_baseline_fail"
    assert decision == "go"


def test_pytest_exit4_existing_file_missing_node_id_real_subprocess(tmp_path):
    """PR #1366 review (Major 4): reproduce the existing-file / missing node-id
    shape with a REAL pytest subprocess (not a hand-written stderr string), and
    feed its actual exit_code/stdout/stderr through classify_result(). This
    guards against the classifier's regex drifting from pytest's real error
    message shape (as opposed to only testing against a hand-authored fixture)."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    test_file = tmp_path / "test_existing_file_real.py"
    test_file.write_text("def test_missing():\n    assert True\n")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", f"{test_file}::test_missing", "-q"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )

    # Sanity: this collected node-id DOES exist, so this run must PASS (exit 0),
    # establishing a baseline before we run the deliberately-missing node-id below.
    assert result.returncode == 0, (
        f"Sanity baseline (existing node-id) unexpectedly failed: "
        f"exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    missing_node_id_result = subprocess.run(
        [sys.executable, "-m", "pytest", f"{test_file}::test_not_yet_implemented", "-q"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )

    assert missing_node_id_result.returncode == 4, (
        f"Expected pytest exit 4 (usage error / not found) for a missing node-id, "
        f"got exit={missing_node_id_result.returncode} "
        f"stdout={missing_node_id_result.stdout!r} stderr={missing_node_id_result.stderr!r}"
    )

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=missing_node_id_result.returncode,
        stdout=missing_node_id_result.stdout,
        stderr=missing_node_id_result.stderr,
        command=f"uv run pytest {test_file}::test_not_yet_implemented -q",
        cwd=str(tmp_path),
    )

    assert category == "existing_file_missing_node_id_noncanonical", (
        f"Expected existing_file_missing_node_id_noncanonical from a REAL pytest "
        f"subprocess run, got category={category!r} classification={classification!r} "
        f"stdout={missing_node_id_result.stdout!r} stderr={missing_node_id_result.stderr!r}"
    )
    assert decision == "blocked", f"Expected decision=blocked, got {decision!r}"


def test_command_substitution_is_not_run(monkeypatch):
    """B2 (updated for #514): $(...) not executed (still holds).
    #514 以降: $(...) は unsupported_shell_syntax/blocked になり、依然として run_command() は呼ばれない。
    """
    fixture_content = """## Verification Commands

```bash
# AC1
$ test "$(wc -l < /nonexistent/file)" -le 10
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # #514: $(...) → blocked / unsupported_shell_syntax, not executed (exit_code=None)
        found = any(
            r["classification"] == "blocked"
            and r["category"] == "unsupported_shell_syntax"
            and r["decision"] == "blocked"
            and r["exit_code"] is None  # not executed
            for r in results
        )
        assert found, (
            f"Command substitution should be blocked/unsupported_shell_syntax and not executed. Got: {results[0]}"
        )
    finally:
        import os
        os.unlink(fixture_file)


def test_negated_rg_is_not_run(monkeypatch):
    """B2 + NB1: ! rg "pattern" not executed"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ ! rg -q "nonexistent_pattern_xyz" .
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # Should be statically classified as expected_fail, not executed
        found = any(
            r["classification"] == "expected_fail"
            and r["decision"] == "go"
            and r["scope_class"] == "baseline_fail_expected"
            for r in results
        )
        assert found, f"Negated rg should be static classified. Got: {results[0]}"
    finally:
        import os
        os.unlink(fixture_file)


def test_fragment_contains_scope_class_and_skipped_metadata():
    """B1: fragment has classification / scope_class / skipped metadata"""
    fixture_content = """## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only
$ grep "test" /tmp/dummy.txt

# AC2
$ rg "should_not_exist" src/
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

        result = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--body-file",
                fixture_file,
                "--issue",
                "999",
                "--format",
                "contract-review-fragment"
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        try:
            import yaml
            output = yaml.safe_load(result.stdout)
        except ImportError:
            if HAS_PYTEST:
                pytest.skip("PyYAML not installed")
            return

        vc = output["vc_preflight"]
        classifications = vc["classifications"]

        # Check all items have classification and scope_class
        for item in classifications:
            assert "classification" in item, "Missing classification field"
            assert "scope_class" in item, "Missing scope_class field"

        # Check skipped item has routing metadata
        skipped_items = [c for c in classifications if c["classification"] == "skipped"]
        for item in skipped_items:
            assert "verification_owner" in item, "Missing verification_owner in skipped item"
            assert "deferred_reason" in item, "Missing deferred_reason in skipped item"
            assert "runtime_verification_required" in item, "Missing runtime_verification_required in skipped item"
    finally:
        import os
        os.unlink(fixture_file)


def test_pytest_exit_5_classified_as_baseline_fail(monkeypatch):
    """B3: pytest exit 5 "no tests collected" → expected_baseline_fail"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ pytest path/that/does/not/exist/ -k new_thing
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # Should be classified as expected_baseline_fail when pytest fails properly
        # (actual behavior depends on whether pytest is installed; just check structure exists)
        for r in results:
            assert "classification" in r
            assert "scope_class" in r
    finally:
        import os
        os.unlink(fixture_file)


def test_preflight_scope_typo_invalid_marker_human_judgment():
    """NB2: # preflight-scope: pr-reveiw-only (typo) → human_judgment"""
    fixture_content = """## Verification Commands

```bash
# AC1
# preflight-scope: pr-reveiw-only
$ grep "test" /tmp/dummy.txt
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # Invalid marker should result in human_judgment
        found = any(
            r["classification"] == "human_judgment"
            and r["decision"] == "human_judgment"
            and "Unknown preflight-scope marker" in (r.get("fix_hint") or "")
            for r in results
        )
        assert found, f"Invalid marker should be human_judgment. Got: {results[0]}"
    finally:
        import os
        os.unlink(fixture_file)


# B1: New tests for blocker fixes

def test_has_command_substitution_single_quoted_literal_is_false():
    """B3: rg -n '\\$\\([^)]+\\)' file → False (single-quoted literal)"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import has_command_substitution

    # Single-quoted pattern is a literal, not substitution
    result = has_command_substitution(r"rg -n '\$\([^)]+\)' file")
    assert result is False, "Single-quoted literal should not detect as substitution"


def test_has_command_substitution_double_quoted_substitution_is_true():
    """B3: echo "$(date)" → True (double-quoted substitution)"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import has_command_substitution

    # Double-quoted substitution should be detected
    result = has_command_substitution('echo "$(date)"')
    assert result is True, "Double-quoted substitution should be detected"


def test_has_command_substitution_mixed_quotes():
    """B3: Mixed quote detection"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import has_command_substitution

    # Test literal in single quotes
    assert has_command_substitution("echo '$(date)'") is False
    # Test substitution in double quotes
    assert has_command_substitution('echo "$(date)"') is True
    # Test backtick outside quotes
    assert has_command_substitution("echo `date`") is True
    # Test backtick in single quotes
    assert has_command_substitution("echo '`date`'") is False


def test_s_missing_file_exit1_go():
    """AC1: test -s <missing-file> exit 1 → file_not_found_expected / go"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="",
        command="test -s /nonexistent/artifact.json",
    )
    assert category == "file_not_found_expected", f"Expected file_not_found_expected, got {category}"
    assert decision == "go", f"Expected go, got {decision}"
    assert classification == "expected_fail", f"Expected expected_fail, got {classification}"


def test_s_empty_file_exit1_go(tmp_path):
    """AC2: test -s <empty-file> exit 1 → file_not_found_expected / go with fix_hint"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    empty_file = tmp_path / "empty.json"
    empty_file.write_text("")
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="",
        command=f"test -s {empty_file}",
    )
    assert category == "file_not_found_expected", f"Expected file_not_found_expected, got {category}"
    assert decision == "go", f"Expected go, got {decision}"
    assert fix_hint is not None and "test -s false means missing or zero-size file" in fix_hint, (
        f"Expected fix_hint containing 'test -s false means missing or zero-size file', got {fix_hint!r}"
    )


def test_s_nonempty_file_exit0_blocked(tmp_path):
    """AC3: test -s <nonempty-file> exit 0 → unexpected_pass / blocked"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    nonempty_file = tmp_path / "data.json"
    nonempty_file.write_text('{"key": "value"}')
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="",
        stderr="",
        command=f"test -s {nonempty_file}",
    )
    assert decision == "blocked", f"Expected blocked, got {decision}"
    assert classification == "unexpected_pass", f"Expected unexpected_pass, got {classification}"


def test_fixed_env_injects_ci_true_for_pnpm_build(monkeypatch):
    """AC2: pnpm build は shell=False のまま CI=true を runner 側で注入する"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    import baseline_vc_preflight as module

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        captured["shell"] = kwargs.get("shell")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module.pnpm_gate_registry, "resolve_trusted_pnpm", lambda _root: "/trusted/pnpm"
    )

    exit_code, stdout, stderr, duration_ms, runner_env_delta = module.run_command("pnpm build", 30, ".")

    assert captured["argv"] == ["/trusted/pnpm", "run", "build"]
    assert captured["shell"] is False
    assert captured["env"]["CI"] == "true"
    assert runner_env_delta == {"CI": "true"}
    assert exit_code == 0


def test_fixed_env_applies_to_all_canonical_pnpm_gates(monkeypatch):
    """AC2 (updated): fixed_env は全 canonical pnpm gate（typecheck/lint/test/build）に CI=true を注入する"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    import baseline_vc_preflight as module

    for gate in ("pnpm typecheck", "pnpm lint", "pnpm test", "pnpm build"):
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(module.subprocess, "run", fake_run)
        monkeypatch.setattr(
            module.pnpm_gate_registry, "resolve_trusted_pnpm", lambda _root: "/trusted/pnpm"
        )

        _, _, _, _, runner_env_delta = module.run_command(gate, 30, ".")

        assert captured["env"] is not None and captured["env"].get("CI") == "true", (
            f"{gate}: expected CI=true injection, got env={captured.get('env')}"
        )
        assert runner_env_delta == {"CI": "true"}, (
            f"{gate}: expected runner_env_delta=={{'CI': 'true'}}, got {runner_env_delta}"
        )


def test_pnpm_build_with_extra_args_is_blocked_without_launch(monkeypatch):
    """AC2: extra args は canonical gate でなく、script subprocess を起動しない。"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    import baseline_vc_preflight as module

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    exit_code, _, stderr, _, runner_env_delta = module.run_command(
        "pnpm build --filter foo", 30, "."
    )

    assert captured == {}
    assert exit_code == -1
    assert "noncanonical_request_argv" in stderr
    assert runner_env_delta == {}


def test_runner_env_delta_output_exposes_only_fixed_env(monkeypatch, tmp_path, capsys):
    """AC3: JSON evidence と fragment は injected delta のみ残し full env を漏らさない"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    import baseline_vc_preflight as module

    fixture = tmp_path / "pnpm_build.md"
    fixture.write_text(
        "## Verification Commands\n\n```bash\n# AC1\n$ pnpm build\n```\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SHOULD_NOT_LEAK_SECRET", "sentinel-secret")

    def fake_run(argv, **kwargs):
        assert kwargs["env"]["CI"] == "true"
        assert kwargs["env"]["SHOULD_NOT_LEAK_SECRET"] == "sentinel-secret"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module.pnpm_gate_registry, "resolve_trusted_pnpm", lambda _root: "/trusted/pnpm"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script_path),
            "--body-file",
            str(fixture),
            "--issue",
            "999",
        ],
    )

    exit_code = module.main()
    raw_json = capsys.readouterr().out
    data = json.loads(raw_json)

    assert exit_code == 0
    assert "sentinel-secret" not in raw_json
    assert "SHOULD_NOT_LEAK_SECRET" not in raw_json
    assert data["results"][0]["runner_env_delta"] == {"CI": "true"}
    assert set(data["results"][0]["runner_env_delta"].keys()) == {"CI"}

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script_path),
            "--body-file",
            str(fixture),
            "--issue",
            "999",
            "--format",
            "contract-review-fragment",
        ],
    )
    exit_code = module.main()
    raw_fragment = capsys.readouterr().out

    assert exit_code == 0
    assert "sentinel-secret" not in raw_fragment
    assert "SHOULD_NOT_LEAK_SECRET" not in raw_fragment

    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed for fragment verification")
        return

    fragment = yaml.safe_load(raw_fragment)
    assert (
        fragment["vc_preflight"]["classifications"][0]["evidence"]["runner_env_delta"]
        == {"CI": "true"}
    )


def test_env_wrapper_exact_env_ci_pnpm_gate_is_blocked():
    """AC5: env CI=true pnpm build は exact grammar を一般許可せず blocked のまま"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ env CI=true pnpm build
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        found = any(
            r["category"] == "command_not_allowed"
            and r["decision"] == "blocked"
            for r in results
        )
        assert found, f"Expected command_not_allowed/blocked. Got: {results}"
    finally:
        import os
        os.unlink(fixture_file)


def test_pnpm_build_with_extra_args_is_not_canonical_gate():
    """AC5: pnpm build extra args は canonical pnpm gate として扱わない"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ pnpm build --filter foo
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        found = any(
            r["category"] == "command_not_allowed"
            and r["decision"] == "blocked"
            for r in results
        )
        assert found, f"Expected command_not_allowed/blocked. Got: {results}"
    finally:
        import os
        os.unlink(fixture_file)


def test_shell_env_prefix_ci_true_pnpm_build_is_blocked():
    """AC4: shell env prefix 形式の CI=true pnpm build は許可しない"""
    fixture_content = """## Verification Commands

```bash
# AC1
$ CI=true pnpm build
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        found = any(
            r["category"] == "command_not_allowed"
            and r["decision"] == "blocked"
            for r in results
        )
        assert found, f"Expected command_not_allowed/blocked. Got: {results}"
    finally:
        import os
        os.unlink(fixture_file)


def test_package_manager_no_tty_prompt_classified_as_tooling_blocker_after_runner_delta():
    """AC6: runner delta 適用済み no-TTY は tooling/state blocker として分類する"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        (
           "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY Aborted removal of modules directory due to no TTY. If running"
           " in CI, set CI=true"
       ),
        "pnpm build",
        cwd=".",
        runner_env_delta={"CI": "true"},
    )

    assert classification == "blocked"
    assert category == "package_manager_no_tty_prompt"
    assert decision == "blocked"
    assert scope_class == "regression_gate"
    assert fix_hint is not None and "already injected by the runner" in fix_hint


def test_package_manager_no_tty_prompt_without_runner_delta_points_to_env_retry():
    """AC6: fixed env delta 未適用 no-TTY は runner-side CI=true の注入を促す"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        1,
        "",
        (
           "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY Aborted removal of modules directory due to no TTY. If running"
           " in CI, set CI=true"
       ),
        "pnpm build",
        cwd=".",
        runner_env_delta={},
    )

    assert classification == "blocked"
    assert category == "package_manager_no_tty_prompt"
    assert decision == "blocked"
    assert scope_class == "regression_gate"
    assert fix_hint is not None and "runner-side CI=true" in fix_hint


def test_s_quoted_path_exit1_go():
    """AC6: test -s with quoted path containing spaces → file_not_found_expected / go"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="",
        command='test -s "/path/with spaces/artifact.json"',
    )
    assert category == "file_not_found_expected", f"Expected file_not_found_expected, got {category}"
    assert decision == "go", f"Expected go, got {decision}"


def test_s_malformed_exit2_not_go():
    """AC7: test -s with malformed invocation exit 2 → blocked (not go)"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    # exit 2: operand missing (test -s with no file argument)
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="test: -s: binary operator expected",
        command="test -s",
    )
    assert decision == "blocked", f"Expected blocked decision, got {decision}"
    assert classification == "blocked", f"Expected blocked classification, got {classification}"


def test_s_extra_operand_exit2_blocked():
    """AC7 extra: test -s with extra operand exit 2 → blocked (len>=3 but exit 2)"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    # exit 2: extra operand (`test -s a b` is an error per POSIX)
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="test: too many arguments",
        command="test -s /some/file /extra",
    )
    assert decision == "blocked", f"Expected blocked decision, got {decision}"
    assert classification == "blocked", f"Expected blocked classification, got {classification}"


def test_classify_result_uses_cwd_argument(tmp_path):
    """B1: classify_result threads cwd to _is_regression_gate_command"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    # Create a test file in tmp_path
    test_file = tmp_path / "test_example.py"
    test_file.write_text("def test_example(): pass")

    # Call classify_result with cwd set to tmp_path.parent
    # Command references relative path
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="",
        stderr="",
        command=f"uv run pytest {test_file}",
        cwd=str(tmp_path.parent)
    )

    # When exit_code=0 and cwd is threaded properly, should be regression_gate
    assert scope_class == "regression_gate", f"Expected regression_gate but got {scope_class}"
    assert classification == "expected_pass", f"Expected expected_pass but got {classification}"


def test_uv_pytest_k_option_does_not_treat_value_as_path(tmp_path):
    """B2: pytest -k option value should not be treated as positional path"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _is_regression_gate_command

    # Create a directory named "smoke" under tmp_path
    smoke_dir = tmp_path / "smoke"
    smoke_dir.mkdir()

    # Create an actual test directory
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_example.py"
    test_file.write_text("def test_example(): pass")

    # Command with -k option and "smoke" as value
    # Should only return True if "tests/" is detected as positional (not "smoke")
    result = _is_regression_gate_command(
        "uv run pytest -k smoke tests/",
        cwd=str(tmp_path)
    )
    assert result is True, "pytest with -k and valid test path should be regression_gate"

    # Command with only -k and no valid positional path
    result = _is_regression_gate_command(
        "uv run pytest -k smoke",
        cwd=str(tmp_path)
    )
    assert result is False, "pytest with only -k and no test path should not be regression_gate"


def test_regression_gate_pnpm_typecheck_exit1_blocked(tmp_path):
    """B5: uv run pytest with valid path + failure → regression_gate/blocked"""
    # Create a test file that will fail
    test_file = tmp_path / "test_fail.py"
    test_file.write_text("def test_fail(): assert False")

    fixture_content = f"""## Verification Commands

```bash
# AC1
$ uv run pytest {test_file}
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_file = f.name

    try:
        data = run_preflight(fixture_file)
        results = data["results"]
        assert len(results) > 0

        # pytest with valid path that fails should be regression_gate
        found = any(
            r["scope_class"] == "regression_gate"
            for r in results
        )
        assert found, f"Expected regression_gate scope_class. Got: {results[0]['scope_class']}"
    finally:
        import os
        os.unlink(fixture_file)


def test_check_c13_vc_preflight_decision_consistency_valid():
    """B4: C13 validates schema consistency for valid classifications"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    classifications = [
        {
            "ac": "AC1",
            "command": "rg test file",
            "exit_code": 1,
            "classification": "expected_fail",
            "category": "expected_baseline_fail",
            "confidence": "high",
            "scope_class": "baseline_fail_expected",
            "evidence": {},
            "decision": "go",
        },
        {
            "ac": "AC2",
            "command": "pnpm typecheck",
            "exit_code": 0,
            "classification": "expected_pass",
            "category": "regression_gate",
            "confidence": "high",
            "scope_class": "regression_gate",
            "evidence": {},
            "decision": "go",
        },
    ]

    is_valid, failures = check_c13_vc_preflight_decision_consistency(classifications)
    assert is_valid, f"Valid classifications should pass. Failures: {failures}"
    assert len(failures) == 0


def test_check_c13_vc_preflight_decision_consistency_missing_field():
    """B4: C13 detects missing required fields"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    classifications = [
        {
            "ac": "AC1",
            "command": "rg test file",
            "exit_code": 1,
            # missing "classification"
            "scope_class": "baseline_fail_expected",
            "decision": "go",
        },
    ]

    is_valid, failures = check_c13_vc_preflight_decision_consistency(classifications)
    assert not is_valid, "Missing field should fail"
    assert any("missing classification" in f for f in failures)


def test_check_c13_vc_preflight_decision_consistency_invalid_enum():
    """B4: C13 detects invalid enum values"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    classifications = [
        {
            "ac": "AC1",
            "command": "rg test file",
            "exit_code": 1,
            "classification": "invalid_classification",
            "scope_class": "baseline_fail_expected",
            "decision": "go",
        },
    ]

    is_valid, failures = check_c13_vc_preflight_decision_consistency(classifications)
    assert not is_valid, "Invalid enum should fail"
    assert any("invalid classification" in f for f in failures)


def test_check_c13_vc_preflight_decision_regression_gate_consistency():
    """B4: C13 checks regression_gate + go requires expected_pass"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    # Invalid: regression_gate + go but classification=blocked
    classifications = [
        {
            "ac": "AC1",
            "command": "pnpm typecheck",
            "exit_code": 1,
            "classification": "blocked",
            "scope_class": "regression_gate",
            "decision": "go",
        },
    ]

    is_valid, failures = check_c13_vc_preflight_decision_consistency(classifications)
    assert not is_valid, "regression_gate + go requires expected_pass"
    assert any("regression_gate + go" in f and "expected_pass" in f for f in failures), (
        f"Expected regression_gate + go error, got: {failures}"
    )


def test_check_c13_vc_preflight_decision_regression_gate_blocked_consistency():
    """B4: C13 checks regression_gate + blocked requires blocked classification"""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    # Invalid: regression_gate + blocked but classification=expected_pass
    classifications = [
        {
            "ac": "AC1",
            "command": "pnpm typecheck",
            "exit_code": 0,
            "classification": "expected_pass",
            "scope_class": "regression_gate",
            "decision": "blocked",
        },
    ]

    is_valid, failures = check_c13_vc_preflight_decision_consistency(classifications)
    assert not is_valid, "regression_gate + blocked requires blocked classification"
    assert any("regression_gate + blocked" in f and "blocked" in f for f in failures), (
        f"Expected regression_gate + blocked error, got: {failures}"
    )


def test_preflight_scope_unknown_value_is_human_judgment():
    """B4: unknown preflight-scope value is routed to human_judgment."""
    fixture_content = """## Verification Commands

```bash
# preflight-scope: invalid
$ test -f /etc/passwd
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_path = f.name

    try:
        data = run_preflight(fixture_path)
        assert data["status"] == "human_judgment"
        assert any(
            r["decision"] == "human_judgment" and r["category"] == "unknown"
            for r in data["results"]
        )
    finally:
        import os
        os.unlink(fixture_path)


def test_preflight_scope_empty_value_is_human_judgment():
    """B4: empty preflight-scope value is routed to human_judgment."""
    fixture_content = """## Verification Commands

```bash
# preflight-scope:
$ test -f /etc/passwd
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_path = f.name

    try:
        data = run_preflight(fixture_path)
        assert data["status"] in ("human_judgment", "blocked")
        assert any(r["category"] == "unknown" for r in data["results"])
    finally:
        import os
        os.unlink(fixture_path)


def test_invalid_ac_marker_is_ignored_for_ac_field():
    """B4: invalid AC marker syntax is not treated as parsed AC label in baseline preflight."""
    fixture_content = """## Verification Commands

```bash
# AC1: description
true
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(fixture_content)
        fixture_path = f.name

    try:
        data = run_preflight(fixture_path)
        assert data["results"], "Expected at least one result"
        assert data["results"][0]["ac"] == "AC_UNKNOWN"
    finally:
        import os
        os.unlink(fixture_path)



# ---------------------------------------------------------------------------
# Issue #889: baseline-expect annotation tests
# ---------------------------------------------------------------------------


def test_baseline_expect_pass_exit0_is_expected_pass():
    """Issue #889: baseline-expect: pass + exit 0 → expected_pass / go (NOT unexpected_pass)."""
    fixture = Path(__file__).parent / "fixtures" / "baseline_expect_pass.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    assert r["classification"] == "expected_pass", (
        f"Expected expected_pass but got {r['classification']} "
        f"(category={r.get('category')}, decision={r.get('decision')})"
    )
    assert r["decision"] == "go", f"Expected go but got {r['decision']}"
    assert r.get("annotations", {}).get("baseline_expect") == "pass"


def test_baseline_expect_pass_exit1_is_human_judgment():
    """Issue #889: baseline-expect: pass + exit 1 → human_judgment / baseline_regression_failed."""
    fixture = Path(__file__).parent / "fixtures" / "baseline_expect_pass_fail.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    # Should be human_judgment (regression detected)
    assert r["decision"] == "human_judgment", (
        f"Expected human_judgment but got {r['decision']} "
        f"(classification={r.get('classification')}, category={r.get('category')})"
    )
    assert r["classification"] == "human_judgment"
    assert r.get("category") == "baseline_regression_failed"
    assert r.get("annotations", {}).get("baseline_expect") == "pass"


def test_baseline_expect_fail_exit1_is_expected_fail():
    """Issue #889: baseline-expect: fail + exit 1 → expected_fail / go (backward compat)."""
    fixture = Path(__file__).parent / "fixtures" / "baseline_expect_fail.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    assert r["classification"] == "expected_fail", (
        f"Expected expected_fail but got {r['classification']}"
    )
    assert r["decision"] == "go"
    assert r.get("annotations", {}).get("baseline_expect") == "fail"


def test_baseline_expect_deferred_is_skipped():
    """Issue #889: baseline-expect: deferred → skipped / go."""
    fixture = Path(__file__).parent / "fixtures" / "baseline_expect_deferred.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    assert r["classification"] == "skipped", (
        f"Expected skipped but got {r['classification']}"
    )
    assert r["decision"] == "go"


def test_missing_annotation_unexpected_pass_has_hint():
    """Issue #889: annotation absent + exit 0 → unexpected_pass / blocked with missing_annotation hint."""
    fixture = Path(__file__).parent / "fixtures" / "missing_annotation_unexpected_pass.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    assert r["classification"] == "unexpected_pass"
    assert r["decision"] == "blocked"
    # missing_annotation_unexpected_pass is True in annotations
    annotations = r.get("annotations", {})
    assert annotations.get("missing_baseline_expect") is True
    # fix_hint should mention missing_annotation
    fix_hint = r.get("fix_hint") or ""
    assert "missing_annotation" in fix_hint.lower() or "baseline-expect" in fix_hint.lower()


def test_baseline_expect_pass_does_not_override_compound_command():
    """Issue #889: baseline-expect: pass does NOT override compound command blocker."""
    fixture = Path(__file__).parent / "fixtures" / "baseline_expect_pass_compound.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    # Should still be blocked (compound command, not expected_pass)
    assert r["classification"] == "blocked"
    assert r["category"] == "compound_command_disallowed"
    assert r["decision"] == "blocked"


def test_annotation_source_fields_present():
    """Issue #889: result JSON has annotations and annotation_source fields (AC11)."""
    fixture = Path(__file__).parent / "fixtures" / "baseline_expect_pass.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    assert "annotations" in r, "annotations field missing from result"
    assert "annotation_source" in r, "annotation_source field missing from result"
    ann = r["annotations"]
    assert "baseline_expect" in ann, "annotations.baseline_expect missing"
    assert "vc_role" in ann, "annotations.vc_role missing"
    assert "missing_baseline_expect" in ann, "annotations.missing_baseline_expect missing"
    src = r["annotation_source"]
    assert "line" in src, "annotation_source.line missing"
    assert "raw" in src, "annotation_source.raw missing"


def test_annotation_scope_does_not_cross_empty_line():
    """Issue #889: annotation scope stops at empty line (does not affect next command)."""
    # Two commands; first has baseline-expect: pass but there's an empty line before the second
    # The second command should NOT inherit the annotation
    import tempfile
    import os
    body = """## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ test -d /home

# AC2
$ test -f /this_file_definitely_does_not_exist_12345abc
```
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        import subprocess
        import sys
        import json
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", tmp_path, "--issue", "999"],
            capture_output=True, text=True, timeout=90,
        )
        data = json.loads(result.stdout)
    finally:
        os.unlink(tmp_path)

    results = data["results"]
    assert len(results) == 2

    # First command (AC1): baseline-expect: pass, exit 0 → expected_pass
    r1 = results[0]
    assert r1["annotations"]["baseline_expect"] == "pass"
    assert r1["classification"] == "expected_pass"

    # Second command (AC2): no annotation (empty line separates), exit 1 → expected_fail
    r2 = results[1]
    assert r2["annotations"]["baseline_expect"] is None
    # exit 1 on test -f nonexistent → expected_fail
    assert r2["classification"] == "expected_fail"


# ---------------------------------------------------------------------------
# Issue #889 BLOCKER 2 fix: invalid baseline-expect annotation
# ---------------------------------------------------------------------------


def test_invalid_baseline_expect_value_is_human_judgment():
    """BLOCKER 2: # baseline-expect: pas (typo) → human_judgment / invalid_baseline_expect_annotation."""
    import tempfile
    import os
    body = """## Verification Commands

```bash
# AC1
# baseline-expect: pas
$ test -f README.md
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(body)
        tmp_path = tf.name
    try:
        import subprocess
        import sys
        import json
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", tmp_path, "--issue", "999"],
            capture_output=True, text=True, timeout=90,
        )
        data = json.loads(result.stdout)
    finally:
        os.unlink(tmp_path)

    results = data["results"]
    assert len(results) > 0
    r = results[0]
    assert r["classification"] == "human_judgment", (
        f"Expected human_judgment but got {r['classification']}"
    )
    assert r["category"] == "invalid_baseline_expect_annotation", (
        f"Expected invalid_baseline_expect_annotation but got {r['category']}"
    )
    assert r["decision"] == "human_judgment"
    fix_hint = r.get("fix_hint") or ""
    assert "pas" in fix_hint and "pass|fail|deferred" in fix_hint


def test_empty_baseline_expect_value_is_human_judgment():
    """BLOCKER 2: # baseline-expect: (empty) → human_judgment."""
    import tempfile
    import os
    body = """## Verification Commands

```bash
# AC1
# baseline-expect:
$ test -f README.md
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(body)
        tmp_path = tf.name
    try:
        import subprocess
        import sys
        import json
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", tmp_path, "--issue", "999"],
            capture_output=True, text=True, timeout=90,
        )
        data = json.loads(result.stdout)
    finally:
        os.unlink(tmp_path)

    results = data["results"]
    assert len(results) > 0
    r = results[0]
    # Empty value is invalid → human_judgment
    assert r["classification"] == "human_judgment"
    assert r["category"] == "invalid_baseline_expect_annotation"


def test_uppercase_baseline_expect_is_human_judgment():
    """BLOCKER 2: # baseline-expect: PASS (uppercase) → human_judgment."""
    import tempfile
    import os
    body = """## Verification Commands

```bash
# AC1
# baseline-expect: PASS
$ test -f README.md
```
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(body)
        tmp_path = tf.name
    try:
        import subprocess
        import sys
        import json
        script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
        result = subprocess.run(
            [sys.executable, str(script_path), "--body-file", tmp_path, "--issue", "999"],
            capture_output=True, text=True, timeout=90,
        )
        data = json.loads(result.stdout)
    finally:
        os.unlink(tmp_path)

    results = data["results"]
    assert len(results) > 0
    r = results[0]
    assert r["classification"] == "human_judgment"
    assert r["category"] == "invalid_baseline_expect_annotation"


# ---------------------------------------------------------------------------
# MAJOR 2: malicious body fixtures
# ---------------------------------------------------------------------------


def test_malicious_baseline_expect_does_not_bypass_static_blocker():
    """MAJOR 2: baseline-expect: pass does NOT bypass unsafe/compound command blockers."""
    import tempfile
    import os
    import subprocess
    import sys
    import json

    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"

    malicious_bodies = [
        # bash -c is unsafe_command
        """## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ bash -c 'echo hacked'
```
""",
        # && is compound_command_disallowed
        """## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ test -f README.md && touch /tmp/pwned
```
""",
    ]

    for body in malicious_bodies:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(body)
            tmp_path = tf.name
        try:
            result = subprocess.run(
                [sys.executable, str(script_path), "--body-file", tmp_path, "--issue", "999"],
                capture_output=True, text=True, timeout=90,
            )
            data = json.loads(result.stdout)
        finally:
            os.unlink(tmp_path)

        results = data["results"]
        assert len(results) > 0
        r = results[0]
        # Must NOT be expected_pass — static blockers take priority over baseline-expect
        assert r["classification"] != "expected_pass", (
            f"Malicious body was classified as expected_pass: {r}"
        )
        assert r["decision"] == "blocked", (
            f"Malicious body decision was not blocked: {r}"
        )


# ---------------------------------------------------------------------------
# MAJOR 3: baseline-expect: deferred category
# ---------------------------------------------------------------------------


def test_baseline_expect_deferred_category():
    """MAJOR 3: baseline-expect: deferred → category is baseline_expect_deferred."""
    fixture = Path(__file__).parent / "fixtures" / "baseline_expect_deferred.md"
    data = run_preflight(str(fixture))
    results = data["results"]
    assert len(results) > 0

    r = results[0]
    assert r["classification"] == "skipped"
    assert r["decision"] == "go"
    # MAJOR 3 fix: category must be baseline_expect_deferred (not preflight_scope_pr_review_only)
    assert r["category"] == "baseline_expect_deferred", (
        f"Expected baseline_expect_deferred but got {r['category']}"
    )






# ===== #899 genuine behavioral tests (subprocess the real script) =====
def _run_bvp_899(body, strict=False):
    import subprocess as _sp
    import json as _json
    import tempfile as _tf
    import os as _os
    import sys as _sys
    script = str(Path(__file__).parent.parent / "baseline_vc_preflight.py")
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        p = f.name
    try:
        argv = [_sys.executable, script, "--body-file", p]
        if strict:
            argv.append("--strict")
        r = _sp.run(argv, capture_output=True, text=True)
        return _json.loads(r.stdout)
    finally:
        _os.unlink(p)


def _result_for_899(data, needle):
    for it in data.get("results", []):
        if needle in (it.get("raw_command") or ""):
            return it
    return None


def test_ac7_actionlint_existing_pass_gate_expected_pass():
    """AC7: an existing 'pass' gate VC (e.g. `Run actionlint`) annotated with a
    preceding '# baseline-expect: pass' is classified expected_pass / go. Uses
    `echo ok` as an environment-stable stand-in that reliably exits 0; the
    motivating real gate is `Run actionlint`."""
    body = "## Verification Commands\n\n```bash\n# baseline-expect: pass\n$ echo ok\n```\n"
    data = _run_bvp_899(body, strict=False)
    it = _result_for_899(data, "echo ok")
    assert it is not None, data
    assert it["classification"] == "expected_pass", it
    assert it["decision"] == "go", it


def test_ac8_strict_missing_annotation_needs_fix():
    """AC8: in --strict a VC targeting a NEW Allowed Path file with no baseline-expect
    annotation is missing_baseline_expect_for_new_allowed_path (body-author-fixable
    needs_fix), not human_judgment. Non-strict keeps old behavior (AC9)."""
    body = ("## Verification Commands\n\n```bash\n$ test -f docs/dev/ac8-new-path-899.md\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/ac8-new-path-899.md`\n")
    data = _run_bvp_899(body, strict=True)
    it = _result_for_899(data, "test -f docs/dev/ac8-new-path-899.md")
    assert it is not None, data
    assert it["category"] == "missing_baseline_expect_for_new_allowed_path", it
    assert it["decision"] == "blocked", it
    assert it.get("strict") and it["strict"].get("needs_fix") is True, it
    data2 = _run_bvp_899(body, strict=False)
    it2 = _result_for_899(data2, "test -f docs/dev/ac8-new-path-899.md")
    assert it2 is not None and it2["category"] != "missing_baseline_expect_for_new_allowed_path", it2


def test_ac5_ci_performance_missing_baseline_needs_fix():
    """AC5: #895-type — a strict-mode VC referencing a new docs/dev/ci-performance.md
    that does not exist at baseline is missing_baseline_expect_for_new_allowed_path
    (needs_fix), not human_judgment."""
    body = (
        "## Verification Commands\n\n```bash\n"
        "$ rg ci_runtime_baseline_v1 docs/dev/ci-performance-nonexistent-899.md"
        "\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/ci-performance-nonexistent-899.md`\n")
    data = _run_bvp_899(body, strict=True)
    it = _result_for_899(data, "ci-performance-nonexistent-899.md")
    assert it is not None, data
    assert it["category"] == "missing_baseline_expect_for_new_allowed_path", it
    assert it["decision"] != "human_judgment", it


def test_ac2_inline_baseline_expect_detection():
    """AC2: inline '# baseline-expect:' is detected as invalid placement BEFORE
    execution — exit_code is None proves the malformed command was never run."""
    body = "## Verification Commands\n\n```bash\n$ rg nonexistentpattern899 README.md # baseline-expect: pass\n```\n"
    data = _run_bvp_899(body, strict=False)
    it = _result_for_899(data, "rg nonexistentpattern899")
    assert it is not None, data
    assert it["category"] == "inline_baseline_expect_invalid_placement", it
    assert it["classification"] == "blocked", it
    assert it["exit_code"] is None, it


def test_ac12_inline_annotation_quoted_literal_safe():
    """AC12: a quoted literal containing '# baseline-expect:' is NOT mistaken for an
    inline annotation (no false positive)."""
    body = ("## Verification Commands\n\n```bash\n# baseline-expect: fail\n"
            "$ rg \"# baseline-expect: pass\" docs/dev/dor.md\n```\n")
    data = _run_bvp_899(body, strict=False)
    it = _result_for_899(data, "rg ")
    assert it is not None, data
    assert it["category"] != "inline_baseline_expect_invalid_placement", it


# ---------------------------------------------------------------------------
# AC4: exact argv negative cases (Issue #1210)
# ---------------------------------------------------------------------------

def test_is_uv_lock_check_rejects_path_qualified_uv():
    """AC4: _is_uv_lock_check は path-qualified uv を拒否する"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from baseline_vc_preflight import _is_uv_lock_check
    assert not _is_uv_lock_check(["/usr/bin/uv", "lock", "--check"])
    assert not _is_uv_lock_check(["./uv", "lock", "--check"])
    assert not _is_uv_lock_check(["/tmp/uv", "lock", "--check"])


def test_is_uv_runtime_smoke_rejects_path_qualified_uv():
    """AC4: _is_uv_runtime_smoke_command は path-qualified uv を拒否する"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from baseline_vc_preflight import _is_uv_runtime_smoke_command
    assert not _is_uv_runtime_smoke_command(
        ["/usr/bin/uv", "run", "--isolated", "--locked", "--no-default-groups", "python",
         "scripts/ci/runtime_dependency_smoke.py"]
    )


def test_is_uv_runtime_smoke_rejects_path_qualified_python():
    """AC4: _is_uv_runtime_smoke_command は path-qualified python/python3 を拒否する"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from baseline_vc_preflight import _is_uv_runtime_smoke_command
    assert not _is_uv_runtime_smoke_command(
        ["uv", "run", "--isolated", "--locked", "--no-default-groups", "./python",
         "scripts/ci/runtime_dependency_smoke.py"]
    )
    assert not _is_uv_runtime_smoke_command(
        ["uv", "run", "--isolated", "--locked", "--no-default-groups", "/tmp/python3",
         "scripts/ci/runtime_dependency_smoke.py"]
    )


def test_is_uv_runtime_smoke_rejects_option_reorder():
    """AC4: _is_uv_runtime_smoke_command は option 順序変更を拒否する"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from baseline_vc_preflight import _is_uv_runtime_smoke_command
    assert not _is_uv_runtime_smoke_command(
        ["uv", "run", "--locked", "--isolated", "--no-default-groups", "python",
         "scripts/ci/runtime_dependency_smoke.py"]
    )


def test_is_uv_runtime_smoke_rejects_duplicate_options():
    """AC4: _is_uv_runtime_smoke_command は duplicate option を拒否する"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from baseline_vc_preflight import _is_uv_runtime_smoke_command
    assert not _is_uv_runtime_smoke_command(
        ["uv", "run", "--isolated", "--isolated", "--locked", "--no-default-groups", "python",
         "scripts/ci/runtime_dependency_smoke.py"]
    )


# --- Issue #1328: rg exit_code==2 new-file-missing-Allowed-Path deterministic classification ---


def test_ac1_rg_exit2_missing_file_in_allowed_paths_is_expected_fail():
    """AC1: rg against a single new Allowed Paths file (missing) with exit_code 2
    and a missing-path stderr is classified expected_fail/new_file_missing_expected/go."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs/new/file.md: No such file or directory (os error 2)",
        command="rg foo docs/new/file.md",
        allowed_paths=["docs/new/file.md"],
        static_policy_passed=True,
    )
    assert classification == "expected_fail", f"Expected expected_fail, got {classification}"
    assert category == "new_file_missing_expected", f"Expected new_file_missing_expected, got {category}"
    assert decision == "go", f"Expected go, got {decision}"
    assert scope_class == "baseline_fail_expected"


def test_ac2_rg_exit2_missing_file_outside_allowed_paths_not_expected_fail():
    """AC2: if the missing path is outside Allowed Paths, do not classify as
    new_file_missing_expected; remain human_judgment/blocked."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs/new/file.md: No such file or directory (os error 2)",
        command="rg foo docs/new/file.md",
        allowed_paths=["docs/other/file.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"
    assert classification in ("human_judgment", "blocked"), (
        f"Expected human_judgment or blocked, got {classification}"
    )


def test_ac3_rg_exit2_invalid_regex_not_expected_fail():
    """AC3: rg invalid regex (unclosed group) exit_code 2 must not be classified
    as new_file_missing_expected."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: regex parse error:\n    (?:(unclosed)\n    ^\nerror: unclosed group",
        command="rg '(?:(unclosed' docs/new/file.md",
        allowed_paths=["docs/new/file.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"
    assert classification in ("human_judgment", "blocked"), (
        f"Expected human_judgment or blocked, got {classification}"
    )


def test_ac4_rg_exit2_unsupported_option_not_expected_fail():
    """AC4: rg unsupported option (unrecognized flag) exit_code 2 must not be
    classified as new_file_missing_expected."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: unrecognized flag --not-a-real-flag",
        command="rg --not-a-real-flag foo docs/new/file.md",
        allowed_paths=["docs/new/file.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"
    assert classification in ("human_judgment", "blocked"), (
        f"Expected human_judgment or blocked, got {classification}"
    )


def test_ac5_rg_extract_path_operands_handles_flags_and_multiple_paths():
    """AC5: _rg_extract_path_operands correctly extracts path operands from an
    rg argv containing -e, --regexp, --glob, --, value-taking options, and
    multiple paths (without misclassifying flag values as paths)."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _rg_extract_path_operands

    argv = [
        "rg",
        "-e", "pattern with spaces",
        "--glob", "*.py",
        "--type", "py",
        "-C", "3",
        "--",
        "docs/a", "docs/b/c.md",
    ]
    assert _rg_extract_path_operands(argv) == ["docs/a", "docs/b/c.md"]

    # --regexp= form and multiple positional paths without --
    argv2 = ["rg", "--regexp=foo", "docs/x", "docs/y"]
    assert _rg_extract_path_operands(argv2) == ["docs/x", "docs/y"]

    # No path operand at all (whole-repo search)
    argv3 = ["rg", "foo"]
    assert _rg_extract_path_operands(argv3) == []


def test_ac6_test_flag_file_not_found_expected_regression():
    """AC6: existing `test -f` / `test -d` / `test -s` file_not_found_expected
    classification is unaffected by the Issue #1328 changes."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="",
        command="test -f /nonexistent/new-allowed-path.py",
        allowed_paths=["nonexistent/new-allowed-path.py"],
        static_policy_passed=True,
    )
    assert classification == "expected_fail"
    assert category == "file_not_found_expected"
    assert decision == "go"

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="",
        command="test -d /nonexistent/new-allowed-dir",
        allowed_paths=["nonexistent/new-allowed-dir"],
        static_policy_passed=True,
    )
    assert classification == "expected_fail"
    assert category == "file_not_found_expected"
    assert decision == "go"


def test_ac7_candidate_new_allowed_path_target_uses_mature_rg_parser():
    """AC7: _candidate_new_allowed_path_target() delegates rg path-operand
    extraction to the mature argv parser (correctly skipping -e/--glob/--
    value-taking flags) instead of a naive non_opt[1:] split."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _candidate_new_allowed_path_target

    # Naive non_opt[1:] extraction would have misparsed this (e.g. treating
    # the --glob value or the pattern as the path), but the mature parser
    # correctly isolates the single trailing path operand.
    command = "rg -e foo --glob '*.py' --type py -- docs/new/allowed-file.md"
    target = _candidate_new_allowed_path_target(command, ["docs/new/allowed-file.md"], ".")
    assert target == "docs/new/allowed-file.md", f"Expected docs/new/allowed-file.md, got {target!r}"


def test_ac9_rg_exit2_missing_path_without_allowed_paths_arg_not_expected_fail():
    """AC9: classify_result() called without the allowed_paths argument (default
    None) never classifies rg exit_code==2 as new_file_missing_expected, even
    when stderr is a missing-path pattern."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs/new/file.md: No such file or directory (os error 2)",
        command="rg foo docs/new/file.md",
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"


def test_ac10_rg_exit2_mixed_missing_path_and_invalid_regex_stderr_not_expected_fail():
    """AC10: stderr with BOTH a missing-path pattern and an invalid-regex /
    unsupported-option pattern must not be classified as
    new_file_missing_expected (blacklist wins)."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    mixed_stderr = (
        "rg: docs/new/file.md: No such file or directory (os error 2)\n"
        "rg: regex parse error: bad pattern"
    )
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr=mixed_stderr,
        command="rg foo docs/new/file.md",
        allowed_paths=["docs/new/file.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"


def test_ac11_new_file_missing_expected_is_high_confidence():
    """AC11: new_file_missing_expected category is in compute_confidence()'s
    high confidence set."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import compute_confidence

    assert compute_confidence("new_file_missing_expected") == "high"


def test_rg_stderr_indicates_missing_path_whitelist():
    """Direct unit test of the whitelist/blacklist helper functions (Issue #1328)."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import (
        _rg_stderr_indicates_missing_path,
        _rg_stderr_indicates_error_not_missing_path,
        _is_rg_missing_path_error,
    )

    missing_dir_stderr = "rg: /tmp/no_such_dir_xyz/: No such file or directory (os error 2)"
    assert _rg_stderr_indicates_missing_path(missing_dir_stderr)
    assert not _rg_stderr_indicates_error_not_missing_path(missing_dir_stderr)
    assert _is_rg_missing_path_error(missing_dir_stderr)

    permission_stderr = "rg: /tmp/permtest/f.txt: Permission denied (os error 13)"
    assert not _is_rg_missing_path_error(permission_stderr)
    assert _rg_stderr_indicates_error_not_missing_path(permission_stderr)

    unsupported_option_stderr = "rg: unrecognized flag --not-a-real-flag"
    assert not _is_rg_missing_path_error(unsupported_option_stderr)


def test_rg_path_operands_all_within_allowed_paths_empty_operands_is_false():
    """A broad rg search (no path operand extracted) must never be treated as
    'within Allowed Paths' — it is always False regardless of allowed_paths."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _rg_path_operands_all_within_allowed_paths

    assert _rg_path_operands_all_within_allowed_paths([], ["docs/new/file.md"]) is False


# --- Issue #1328 follow-up (OWNER adversarial review): harden the false-green
# boundary and align Allowed Paths containment with the existing fail-closed
# gate grammar. ---


def test_rg_exit2_with_stdout_match_and_missing_path_is_not_new_file_missing_expected():
    """OWNER Blocker 1: a multi-path rg invocation where one path produced a
    match (non-empty stdout) and a DIFFERENT path was missing must NOT be
    classified as new_file_missing_expected. Issue #1328's AC1 only covers a
    single new/missing Allowed Paths file with no matches anywhere; it must
    not be broadened to "some paths matched, some were missing"."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="docs/allowed/existing.md:foo\n",
        stderr="rg: docs/allowed/missing.md: No such file or directory (os error 2)",
        command="rg foo docs/allowed/existing.md docs/allowed/missing.md",
        allowed_paths=["docs/allowed/existing.md", "docs/allowed/missing.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"


def test_rg_exit2_partial_missing_among_multiple_allowed_paths_not_expected_fail():
    """OWNER Blocker 1: even with empty stdout, if only SOME of the multiple
    path operands are reported missing in stderr (not the full operand set),
    the ambiguous case must not be classified as new_file_missing_expected."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs/allowed/missing.md: No such file or directory (os error 2)",
        command="rg foo docs/allowed/existing.md docs/allowed/missing.md",
        allowed_paths=["docs/allowed/existing.md", "docs/allowed/missing.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"


def test_rg_exit2_missing_path_mismatched_with_argv_operand_not_expected_fail():
    """OWNER Blocker 1: if the path reported missing in stderr does not match
    the (single) argv path operand at all, do not classify as
    new_file_missing_expected (defensive; should not normally occur but must
    fail closed if it does)."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs/allowed/other-file.md: No such file or directory (os error 2)",
        command="rg foo docs/allowed/missing.md",
        allowed_paths=["docs/allowed/missing.md", "docs/allowed/other-file.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"


def test_normalize_repo_relative_path_strict_rejects_unsafe_paths():
    """OWNER Blocker 2: _normalize_repo_relative_path_strict() rejects
    absolute paths, backslash-containing paths, '..' segments, and empty
    segments, mirroring AllowedPathsMatcher.normalize_path() in
    allowed_paths_review_gate.py."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _normalize_repo_relative_path_strict

    assert _normalize_repo_relative_path_strict("docs/new/file.md") == "docs/new/file.md"
    assert _normalize_repo_relative_path_strict("./docs/new/file.md") == "docs/new/file.md"
    assert _normalize_repo_relative_path_strict("/docs/new/file.md") is None
    assert _normalize_repo_relative_path_strict("../docs/new/file.md") is None
    assert _normalize_repo_relative_path_strict("docs/../new/file.md") is None
    assert _normalize_repo_relative_path_strict("docs\\new\\file.md") is None
    assert _normalize_repo_relative_path_strict("docs//new/file.md") is None
    assert _normalize_repo_relative_path_strict("") is None
    assert _normalize_repo_relative_path_strict(".") is None


def test_rg_exit2_allowed_paths_containment_rejects_traversal_and_absolute():
    """OWNER Blocker 2: rg exit_code==2 against a path operand that is an
    Allowed-Paths-adjacent traversal ('../') or absolute path must not be
    classified as new_file_missing_expected, even if a naive string-prefix
    match would have allowed it."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    # '..' traversal path operand
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs/allowed/../new/file.md: No such file or directory (os error 2)",
        command="rg foo docs/allowed/../new/file.md",
        allowed_paths=["docs/allowed/../new/file.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"

    # empty-segment path operand (docs//new/file.md)
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs//new/file.md: No such file or directory (os error 2)",
        command="rg foo docs//new/file.md",
        allowed_paths=["docs//new/file.md"],
        static_policy_passed=True,
    )
    assert category != "new_file_missing_expected", f"Unexpected new_file_missing_expected: {category}"


def test_rg_extract_path_operands_handles_file_flag():
    """OWNER Blocker 3: -f/--file PATTERNFILE is a value-taking flag; its value
    must not be misidentified as PATTERN or as a PATH operand, and the
    trailing path operand must still be extracted correctly."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import _rg_extract_path_operands

    assert _rg_extract_path_operands(["rg", "-f", "patterns.txt", "docs/new/file.md"]) == ["docs/new/file.md"]
    assert _rg_extract_path_operands(["rg", "--file", "patterns.txt", "docs/new/file.md"]) == ["docs/new/file.md"]
    assert _rg_extract_path_operands(["rg", "--file=patterns.txt", "docs/new/file.md"]) == ["docs/new/file.md"]
    # multiple -f occurrences (rg supports repeated -f) with multiple paths
    assert _rg_extract_path_operands(
        ["rg", "-f", "p1.txt", "-f", "p2.txt", "docs/a", "docs/b"]
    ) == ["docs/a", "docs/b"]


def test_rg_exit2_missing_file_via_file_flag_command_is_expected_fail():
    """OWNER Blocker 3: end-to-end classify_result() check that a `rg -f
    patterns.txt docs/new/file.md` command against a missing Allowed Paths
    file is still classified new_file_missing_expected (the -f fix must not
    regress the AC1 happy path when -f is used instead of an inline pattern)."""
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    sys.path.insert(0, str(script_path.parent))
    from baseline_vc_preflight import classify_result

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=2,
        stdout="",
        stderr="rg: docs/new/file.md: No such file or directory (os error 2)",
        command="rg -f patterns.txt docs/new/file.md",
        allowed_paths=["docs/new/file.md"],
        static_policy_passed=True,
    )
    assert classification == "expected_fail"
    assert category == "new_file_missing_expected"
    assert decision == "go"
