"""Issue #2860 AC1 / AC3: issue-refinement-loop の run-scoped scratch 契約の focused regression.

issue-refinement-loop の SKILL.md は thin entrypoint (500 行上限) であり、ad hoc scratch
(本文 draft・anchor list・readback・guard result) の規約を Guardrails の 1 項目として持つ。
workspace は「canonical repo `tmp/` root を idempotent に materialize (`mkdir -p tmp`) →
その配下に `mktemp -d` で invocation-owned directory を atomic に実作成」の順で確立する。
旧固定 destination (`/tmp/issue_body.md` / `/tmp/readback.json` / `/tmp/guard_result.json` /
裸の `anchor_list.txt`、観測済み denial の `issue2785_followup_anchors` 系) の固定 write を
再導入すると FAIL し、説明文 (fence 外 prose) と read-only 引数は false positive にしない。

この file は意図的に自己完結している。bare module 名の import を使わず、他 test file と
`sys.modules` 上で衝突しない。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"

# 旧固定 / 共有 destination の 4 分類。値は「そのクラスの旧 write 例」を検出する正規表現。
# 観測済み denial (`issue2785_followup_anchors` 系) の anchor list を含む。
_LEGACY_DESTINATIONS: dict[str, re.Pattern[str]] = {
    "body": re.compile(r"/tmp/issue[\w.-]*body[\w.-]*\.md"),
    "anchor_list": re.compile(
        r"(?:/tmp/issue[\w.-]*anchors?[\w.-]*\.txt|(?<![\w/.$\"'-])anchor_list\.txt)"
    ),
    "readback": re.compile(r"/tmp/[\w.-]*readback[\w.-]*\.json"),
    "guard_result": re.compile(r"/tmp/[\w.-]*guard_result[\w.-]*\.json"),
}

# fixed absolute /tmp への write 形状 (リダイレクト / tee / write_text / open(..., "w"))。
_FIXED_TMP_WRITE_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\d&])>>?\s*[\"']?/tmp/"),
    re.compile(r"\btee\s+(?:-a\s+)?[\"']?/tmp/"),
    re.compile(r"write_text\(\s*[\"']/tmp/"),
    re.compile(r"open\(\s*[\"']/tmp/[^)]*[\"'](?:w|a|x)b?[\"']"),
)

# cwd 直下の裸のファイル名へのリダイレクト (`> readback.json` 等)。fd 複製 (`2>&1`) は除外する。
_BARE_FILE_REDIRECT = re.compile(
    r"(?<![\d&])>>?\s*(?![\"'$/&(\s])[\w.-]+\.(?:json|md|txt)\b"
)

_FENCE_RE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE)


def _fenced_blocks(text: str) -> list[str]:
    return [m.group(1) for m in _FENCE_RE.finditer(text)]


def _logical_lines(block: str) -> list[str]:
    """backslash 継続を 1 論理行へ結合し、コメント行を落とす。"""
    joined = re.sub(r"\\\n\s*", " ", block)
    return [ln for ln in joined.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def find_fixed_scratch_writes(text: str) -> list[str]:
    """fence 内 (実行例) の旧固定 destination / fixed-path write を `class:line` で返す。

    fence 外の prose (禁止例の説明文) は走査しない。"""
    violations: list[str] = []
    for block in _fenced_blocks(text):
        for line in _logical_lines(block):
            for name, pattern in _LEGACY_DESTINATIONS.items():
                if pattern.search(line):
                    violations.append(f"{name}: {line.strip()}")
            if any(shape.search(line) for shape in _FIXED_TMP_WRITE_SHAPES):
                violations.append(f"fixed_tmp_write: {line.strip()}")
            if _BARE_FILE_REDIRECT.search(line):
                violations.append(f"bare_file_write: {line.strip()}")
    return violations


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _bootstrap_commands(text: str) -> tuple[str, str]:
    """SKILL.md の scratch 規約 (inline code) から `mkdir -p tmp` と `WORKSPACE=$(mktemp -d ...)` を取り出す。

    `<N>` は実行用に具体的な Issue 番号へ置換する。"""
    spans = re.findall(r"`([^`\n]+)`", text)
    mkdir_line = next((s for s in spans if s == "mkdir -p tmp"), None)
    mktemp_line = next((s for s in spans if s.startswith("WORKSPACE=$(mktemp -d tmp/refinement-")), None)
    assert mkdir_line is not None, "`mkdir -p tmp` の inline code が SKILL.md に無い"
    assert mktemp_line is not None, "`WORKSPACE=$(mktemp -d tmp/refinement-...)` が SKILL.md に無い"
    return mkdir_line, mktemp_line.replace("<N>", "2860")


def _establish(cwd: Path, mkdir_line: str, mktemp_line: str) -> Path:
    script = f"set -eu\n{mkdir_line}\n{mktemp_line}\nprintf '%s' \"$WORKSPACE\"\n"
    result = subprocess.run(
        ["bash", "-c", script], cwd=cwd, capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return cwd / result.stdout.strip()


# ---------------------------------------------------------------------------
# AC2 / AC3: 現行 SKILL.md に旧固定 write が無い
# ---------------------------------------------------------------------------


def test_current_skill_md_has_no_fixed_scratch_write() -> None:
    assert find_fixed_scratch_writes(_skill_text()) == []


# ---------------------------------------------------------------------------
# AC3: 4 つの旧 fixed destination を各々復活させると FAIL する
# ---------------------------------------------------------------------------

_REINTRODUCTIONS = {
    "body": 'cat > /tmp/issue_body.md <<EOF\ndraft\nEOF',
    "anchor_list": "printf '%s\\n' anchor > anchor_list.txt",
    "readback": 'gh issue view 2860 --json title,body > /tmp/readback.json',
    "guard_result": "uv run python3 guard.py --format json > /tmp/guard_result.json",
}


@pytest.mark.parametrize("artifact", sorted(_REINTRODUCTIONS))
def test_reintroducing_each_legacy_destination_fails(artifact: str) -> None:
    """現行 SKILL.md に旧固定 write 例を 1 種ずつ再導入すると FAIL する。"""
    text = _skill_text()
    assert find_fixed_scratch_writes(text) == []
    mutated = text + "\n```bash\n" + _REINTRODUCTIONS[artifact] + "\n```\n"
    violations = find_fixed_scratch_writes(mutated)
    assert any(v.startswith(f"{artifact}:") for v in violations), violations


@pytest.mark.parametrize(
    "example",
    [
        "gh issue view 1 --json title,labels > /tmp/readback.json",
        "uv run python3 guard.py /tmp/issue_body.md --format json > /tmp/guard_result.json",
        "echo anchor > anchor_list.txt",
        "printf x > /tmp/issue2785_followup_anchors.txt",
        "python3 -c 'from pathlib import Path; Path.write_text(\"/tmp/issue2785_followup_anchors.txt\", \"x\")'",
        "cat <<EOF > /tmp/issue-2785-followup2-body.md",
        "gh issue view 1 --json title > readback.json",
    ],
)
def test_detector_flags_each_observed_fixed_destination_shape(example: str) -> None:
    text = f"手順:\n\n```bash\n{example}\n```\n"
    assert find_fixed_scratch_writes(text), example


# ---------------------------------------------------------------------------
# AC3: false positive にしない (説明文 / read-only 引数 / owned directory 内の通常名)
# ---------------------------------------------------------------------------


def test_prose_explanation_of_forbidden_path_is_not_a_false_positive() -> None:
    prose = (
        "旧 `/tmp/issue_body.md` / `/tmp/readback.json` / `/tmp/guard_result.json` と裸の "
        "`anchor_list.txt` への固定 write は禁止する。`gh ... > /tmp/readback.json` も使わない。\n"
    )
    assert find_fixed_scratch_writes(prose) == []


@pytest.mark.parametrize(
    "line",
    [
        'uv run python3 validate_issue_body.py --body-file "$WORKSPACE/body.md" --kind implementation',
        'guard-issue-body.py "$WORKSPACE/body.md" --readback-json "$WORKSPACE/readback.json"',
        'gh issue view 12 --json title,labels > "$WORKSPACE/readback.json"',
        'guard-issue-body.py "$WORKSPACE/body.md" --format json > "$WORKSPACE/guard_result.json"',
        'verify-anchors.sh "$WORKSPACE/anchors.txt"',
        'cat "$WORKSPACE/readback.json"',
        "cat /tmp/some-existing-readonly-input.json",
        "rg -n foo /tmp/some-existing-readonly-input.txt",
        "gh issue view 12 --json title,labels 2>&1",
        "gh issue view 12 --json title 2>/dev/null",
        "uv run python3 tool.py --body-file tmp/refinement-2860.AbC123/body.md",
        'WORKSPACE=$(mktemp -d tmp/refinement-2860.XXXXXX)',
    ],
)
def test_read_only_arguments_and_owned_directory_names_pass(line: str) -> None:
    assert find_fixed_scratch_writes(f"```bash\n{line}\n```\n") == [], line


# ---------------------------------------------------------------------------
# AC1: workspace 確立の順序・handoff・寿命・foreign cleanup 禁止が SKILL.md に明記されている
# ---------------------------------------------------------------------------


def _scratch_guardrail(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith("- ad hoc scratch")]
    assert len(lines) == 1, "scratch 規約の Guardrails 項目が 1 つだけ存在する"
    return lines[0]


def test_skill_md_documents_workspace_establishment_contract() -> None:
    text = _skill_text()
    rule = _scratch_guardrail(text)
    mkdir_pos = rule.index("`mkdir -p tmp`")
    mktemp_pos = rule.index("mktemp -d tmp/refinement-")
    # canonical tmp/ root の materialize (idempotent) が、その配下の実 directory 作成より先。
    assert mkdir_pos < mktemp_pos
    assert "idempotent" in rule and "atomic" in rule
    # name-only allocation は禁止として明記され、実行例 (fence) には現れない。
    assert re.search(r"`mktemp -u`[^。]*禁止", rule)
    assert all("mktemp -u" not in block for block in _fenced_blocks(text))
    # OS-temp 経路でも実 directory を作る。
    assert "OS-temp" in rule and "実作成" in rule
    # 具体 path の明示 handoff / terminal join までの寿命 / foreign cleanup 禁止 / Write・Edit 優先。
    assert "明示 handoff" in rule and "環境変数の暗黙持続に頼らない" in rule
    assert "terminal join" in rule
    assert "foreign cleanup 禁止" in rule and "残置理由" in rule
    assert "Write / Edit" in rule
    # canonical artifact は移動しない。
    assert "canonical artifact は対象外で移動しない" in rule


def test_thin_entrypoint_line_budget_is_preserved() -> None:
    assert len(_skill_text().splitlines()) <= 500


# ---------------------------------------------------------------------------
# AC3: tmp/ 不在 fixture から実際に workspace を確立できる / 二 invocation 分離 / 無関係 file 不変
# ---------------------------------------------------------------------------


def test_workspace_is_established_from_checkout_without_tmp_root(tmp_path: Path) -> None:
    mkdir_line, mktemp_line = _bootstrap_commands(_skill_text())
    assert not (tmp_path / "tmp").exists()
    workspace = _establish(tmp_path, mkdir_line, mktemp_line)
    assert workspace.is_dir()
    assert workspace.parent == tmp_path / "tmp"
    assert re.fullmatch(r"refinement-2860\.[A-Za-z0-9]{6}", workspace.name)
    assert not list(workspace.iterdir()), "確立直後の workspace は空である"
    # owned directory 内の通常名は書ける。
    (workspace / "body.md").write_text("draft", encoding="utf-8")
    assert (workspace / "body.md").read_text(encoding="utf-8") == "draft"


def test_two_invocations_get_separate_directories_and_unrelated_files_stay_unchanged(
    tmp_path: Path,
) -> None:
    mkdir_line, mktemp_line = _bootstrap_commands(_skill_text())
    (tmp_path / "tmp").mkdir()
    unrelated_file = tmp_path / "tmp" / "unrelated-existing.json"
    unrelated_file.write_text('{"keep": true}', encoding="utf-8")
    foreign_dir = tmp_path / "tmp" / "refinement-2860.FOREIGN"
    foreign_dir.mkdir()
    (foreign_dir / "body.md").write_text("foreign scratch", encoding="utf-8")
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (unrelated_file, foreign_dir / "body.md")
    }

    first = _establish(tmp_path, mkdir_line, mktemp_line)
    second = _establish(tmp_path, mkdir_line, mktemp_line)

    assert first != second
    assert first.parent == second.parent == tmp_path / "tmp"
    assert first.is_dir() and second.is_dir()
    (first / "body.md").write_text("first", encoding="utf-8")
    (second / "body.md").write_text("second", encoding="utf-8")
    assert (first / "body.md").read_text(encoding="utf-8") == "first"
    assert (second / "body.md").read_text(encoding="utf-8") == "second"
    # 既存の無関係 file / foreign directory は変更されない (foreign cleanup も上書きも無い)。
    for path, (content, mtime) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_mtime_ns == mtime
    assert foreign_dir.is_dir()


def test_bootstrap_order_matters_because_mktemp_does_not_create_the_tmp_root(tmp_path: Path) -> None:
    """順序固定の根拠: tmp/ 不在で mktemp -d を先に実行すると失敗する。"""
    _mkdir_line, mktemp_line = _bootstrap_commands(_skill_text())
    result = subprocess.run(
        ["bash", "-c", f"set -eu\n{mktemp_line}\n"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert not (tmp_path / "tmp").exists()


def test_bootstrap_is_idempotent_when_tmp_root_already_exists(tmp_path: Path) -> None:
    mkdir_line, mktemp_line = _bootstrap_commands(_skill_text())
    (tmp_path / "tmp").mkdir()
    workspace = _establish(tmp_path, mkdir_line, mktemp_line)
    assert workspace.is_dir()
    assert os.path.commonpath([workspace, tmp_path / "tmp"]) == str(tmp_path / "tmp")
    # shlex で解釈可能な単純な command だけであること (複合 shell operator に依存しない)。
    assert shlex.split(mkdir_line) == ["mkdir", "-p", "tmp"]
