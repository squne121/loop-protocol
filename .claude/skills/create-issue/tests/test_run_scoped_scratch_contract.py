"""Issue #2860 AC2 / AC3: create-issue の run-scoped scratch 契約の focused regression.

create-issue の SKILL.md は、本文 draft・anchor list・post-create readback・guard result の
4 scratch artifact を、起票ごとに実際に作成した invocation-owned workspace
(`tmp/` root を idempotent に materialize した上で `mktemp -d` した一意 directory) の具体 path へ
揃える。旧固定 destination (`/tmp/issue_body.md` / `/tmp/readback.json` /
`/tmp/guard_result.json` / 裸の `anchor_list.txt`) の固定 write を再導入すると FAIL する。
説明文 (fence 外 prose) と read-only 引数は false positive にしない。

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
    """SKILL.md の workspace 確立 fence から `mkdir -p tmp` と `WORKSPACE=$(mktemp -d ...)` を取り出す。"""
    for block in _fenced_blocks(text):
        if "mkdir -p tmp" in block and "mktemp -d tmp/" in block:
            lines = [ln for ln in block.splitlines() if ln.strip()]
            mkdir_line = next(ln for ln in lines if ln.strip() == "mkdir -p tmp")
            mktemp_line = next(ln for ln in lines if ln.startswith("WORKSPACE=$(mktemp -d tmp/"))
            return mkdir_line, mktemp_line
    raise AssertionError("workspace 確立 fence (mkdir -p tmp → mktemp -d tmp/...) が SKILL.md に無い")


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
    "body": ('"$WORKSPACE/body.md"', "/tmp/issue_body.md"),
    "anchor_list": ('"$WORKSPACE/anchors.txt"', "anchor_list.txt"),
    "readback": ('"$WORKSPACE/readback.json"', "/tmp/readback.json"),
    "guard_result": ('"$WORKSPACE/guard_result.json"', "/tmp/guard_result.json"),
}


@pytest.mark.parametrize("artifact", sorted(_REINTRODUCTIONS))
def test_reintroducing_each_legacy_destination_fails(artifact: str) -> None:
    current, legacy = _REINTRODUCTIONS[artifact]
    text = _skill_text()
    assert current in text, f"現行 SKILL.md に {current} の実行例が無い"
    mutated = text.replace(current, legacy)
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
        "uv run python3 tool.py --body-file tmp/create-issue.AbC123/body.md",
        'WORKSPACE=$(mktemp -d tmp/create-issue.XXXXXX)',
    ],
)
def test_read_only_arguments_and_owned_directory_names_pass(line: str) -> None:
    assert find_fixed_scratch_writes(f"```bash\n{line}\n```\n") == [], line


# ---------------------------------------------------------------------------
# AC2: 4 scratch artifact の producer と consumer が同じ workspace の具体 path に整合する
# ---------------------------------------------------------------------------


def test_four_scratch_artifacts_share_one_workspace_with_consistent_producers_and_consumers() -> None:
    text = _skill_text()
    code = "\n".join(_logical_lines("\n".join(_fenced_blocks(text))))
    referenced = set(re.findall(r'"\$WORKSPACE/([\w.-]+)"', code))
    assert referenced == {"body.md", "anchors.txt", "readback.json", "guard_result.json"}

    # readback: producer (stdout capture) と consumer (--readback-json) が同一 path。
    assert re.search(r'gh issue view .*> "\$WORKSPACE/readback\.json"', code)
    assert len(re.findall(r'--readback-json "\$WORKSPACE/readback\.json"', code)) >= 2
    # body: validator (--body-file) と guard (positional) が同一 path。
    assert '--body-file "$WORKSPACE/body.md"' in code
    assert 'guard-issue-body.py "$WORKSPACE/body.md"' in code
    # anchor list: verify-anchors.sh の consumer が workspace の anchors.txt。
    assert 'verify-anchors.sh "$WORKSPACE/anchors.txt"' in code
    # guard result の producer は workspace 内で、直後に exit status を保持する。
    assert re.search(r'--format json > "\$WORKSPACE/guard_result\.json"\nGUARD_EXIT=\$\?', code)


def test_skill_md_documents_workspace_establishment_contract() -> None:
    text = _skill_text()
    mkdir_pos = text.index("mkdir -p tmp")
    mktemp_pos = text.index("mktemp -d tmp/create-issue.")
    # canonical tmp/ root の materialize が、その配下の実 directory 作成より先。
    assert mkdir_pos < mktemp_pos
    # 実行例 (fence) に name-only allocation は無い。説明文では禁止として明記される。
    assert all("mktemp -u" not in block for block in _fenced_blocks(text))
    assert re.search(r"`mktemp -u`[^\n]*禁止", text)
    # 明示 handoff / terminal join までの寿命 / foreign cleanup 禁止 / Write・Edit 優先。
    assert "明示的に引き渡す" in text
    assert "terminal join" in text
    assert "foreign cleanup 禁止" in text
    assert "Write / Edit" in text
    assert "idempotent" in text


# ---------------------------------------------------------------------------
# AC3: tmp/ 不在 fixture から実際に workspace を確立できる / 二 invocation 分離 / 無関係 file 不変
# ---------------------------------------------------------------------------


def test_workspace_is_established_from_checkout_without_tmp_root(tmp_path: Path) -> None:
    mkdir_line, mktemp_line = _bootstrap_commands(_skill_text())
    assert not (tmp_path / "tmp").exists()
    workspace = _establish(tmp_path, mkdir_line, mktemp_line)
    assert workspace.is_dir()
    assert workspace.parent == tmp_path / "tmp"
    assert re.fullmatch(r"create-issue\.[A-Za-z0-9]{6}", workspace.name)
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
    foreign_dir = tmp_path / "tmp" / "create-issue.FOREIGN"
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


# ---------------------------------------------------------------------------
# AC2 (OWNER scope extension): 共通参照 references/body-authoring.md の実 consumer が
# SKILL.md の producer と同じ concrete path (`$WORKSPACE/body.md`) を使う。
# ---------------------------------------------------------------------------

BODY_AUTHORING_MD = Path(__file__).resolve().parent.parent / "references" / "body-authoring.md"

# 直前が `/` `$` `"` `'` `-` 英数字・`.` でない裸の `issue_body.md` (= cwd 直下の predictable な consumer path)。
_BARE_ISSUE_BODY = re.compile(r"(?<![\w/.$\"'-])issue_body\.md\b")


def find_bare_issue_body_consumers(text: str) -> list[str]:
    """fence 内の実行例 (コメント行を除く) にある裸の `issue_body.md` consumer を返す。

    fence 外の説明文 (禁止例を述べる prose) は走査しない。"""
    found: list[str] = []
    for block in _fenced_blocks(text):
        for line in _logical_lines(block):
            if _BARE_ISSUE_BODY.search(line):
                found.append(line.strip())
    return found


def test_body_authoring_ac_vc_consumers_use_the_concrete_workspace_body_path() -> None:
    text = BODY_AUTHORING_MD.read_text(encoding="utf-8")
    assert find_bare_issue_body_consumers(text) == []
    code = "\n".join(_logical_lines("\n".join(_fenced_blocks(text))))
    # AC 件数 (awk) と VC の # AC<n> 件数 (rg) の 4 つの実 consumer が全て workspace body path を読む。
    assert len(re.findall(r'awk .*"\$WORKSPACE/body\.md"', code)) == 2
    assert len(re.findall(r'rg -c "# AC\[0-9\]" "\$WORKSPACE/body\.md"', code)) == 2
    assert "AC_COUNT=$(awk" in code and "VC_AC_COUNT=$(rg -c" in code


def test_reintroducing_bare_issue_body_consumer_in_body_authoring_fails() -> None:
    text = BODY_AUTHORING_MD.read_text(encoding="utf-8")
    assert '"$WORKSPACE/body.md"' in text
    mutated = text.replace('"$WORKSPACE/body.md"', "issue_body.md")
    assert find_bare_issue_body_consumers(mutated), "裸の issue_body.md consumer を検出できない"


def test_bare_issue_body_detector_has_no_false_positive_on_prose_or_concrete_paths() -> None:
    prose = "cwd 直下の裸の `issue_body.md` を読まず、`$WORKSPACE/body.md` を使う。\n"
    assert find_bare_issue_body_consumers(prose) == []
    concrete = '```bash\nrg -c "# AC[0-9]" "$WORKSPACE/body.md"\n# issue_body.md という旧名の説明コメント\n```\n'
    assert find_bare_issue_body_consumers(concrete) == []
