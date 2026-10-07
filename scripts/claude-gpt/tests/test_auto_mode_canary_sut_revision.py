"""scripts/claude-gpt/tests/test_auto_mode_canary_sut_revision.py

Issue #2960: `auto_mode_canary` の evidence `sut_revision` は、実際に評価した checkout の
HEAD SHA を `checkout_head_sha` として出力する。旧 field 名は live main を主張する誤読を招くため
出力しない。live main SHA は観測しない（別 Issue の範囲）。

Runtime Verification Applicability: immediate (AC4)。canary executable を実 subprocess として
`--mode agy --no-evidence` で起動し、exit code と stdout evidence を観測する
（live ChatGPT / proxy / GitHub I/O は使わない。exit 77 は SKIP であり PASS ではない）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

CHECKOUT_HEAD_FIELD = "checkout_head_sha"
LEGACY_FIELD = "main_sha"  # 旧 field 名（deprecated-key negative assertion 用。runtime consumer ではない）

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent
REPO_ROOT = SCRIPT_DIR.parent.parent
CANARY_PY = SCRIPT_DIR / "auto_mode_canary.py"


def _load_canary_module():
    # 同名 module の sys.modules 衝突を避けるため、この file 固有の一意名で load する。
    spec = importlib.util.spec_from_file_location("auto_mode_canary_sut_revision_under_test", CANARY_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass の postponed annotation 解決に必要
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=30,
    )
    return result.stdout.strip()


def test_sut_revision_reports_checkout_head_not_main(tmp_path, monkeypatch):
    """GIVEN main と異なる HEAD を持つ一時 git repo
    WHEN REPO_ROOT をその repo に差し替えて `_sut_revision()` を呼ぶ
    THEN `checkout_head_sha` は実 checkout HEAD と一致し、旧 field は存在しない。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "main commit", "--no-gpg-sign")
    main_head = _git(repo, "rev-parse", "main")

    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "candidate commit", "--no-gpg-sign")
    candidate_head = _git(repo, "rev-parse", "HEAD")
    assert candidate_head != main_head

    canary = _load_canary_module()
    monkeypatch.setattr(canary, "REPO_ROOT", repo)
    revision = canary._sut_revision()

    assert revision[CHECKOUT_HEAD_FIELD] == candidate_head
    assert revision[CHECKOUT_HEAD_FIELD] != main_head
    assert LEGACY_FIELD not in revision


def test_sut_revision_unknown_fallback_when_not_a_git_repo(tmp_path, monkeypatch):
    """GIVEN git repo ではない directory
    WHEN `_sut_revision()` を呼ぶ
    THEN returncode != 0 の既存 fallback として `checkout_head_sha` は "unknown" のまま維持される。"""
    not_repo = tmp_path / "not-a-repo"
    not_repo.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    canary = _load_canary_module()
    monkeypatch.setattr(canary, "REPO_ROOT", not_repo)
    revision = canary._sut_revision()

    assert revision[CHECKOUT_HEAD_FIELD] == "unknown"
    assert LEGACY_FIELD not in revision


def test_canary_subprocess_sut_revision_matches_checkout_head():
    """GIVEN canary executable を実 subprocess として `--mode agy --no-evidence` で起動する
    WHEN stdout の evidence JSON を観測する
    THEN `sut_revision.checkout_head_sha` は subprocess が属する checkout の HEAD と一致し、
    旧 field は無く、exit 77 / exit_classification skip（SKIP は PASS へ昇格しない）。"""
    result = subprocess.run(
        ["uv", "run", "--locked", "python3", str(CANARY_PY), "--mode", "agy", "--no-evidence"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 77, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["schema"] == "AUTO_MODE_CANARY_EVIDENCE_V2"
    assert evidence["exit_classification"] == "skip"

    sut_revision = evidence["sut_revision"]
    assert LEGACY_FIELD not in sut_revision
    assert LEGACY_FIELD not in result.stdout

    # subprocess が属する checkout（canary の REPO_ROOT）の実 HEAD
    expected_head = _git(REPO_ROOT, "rev-parse", "HEAD")
    assert sut_revision[CHECKOUT_HEAD_FIELD] == expected_head
