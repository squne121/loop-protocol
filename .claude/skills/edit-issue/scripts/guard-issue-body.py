#!/usr/bin/env python3
"""
guard-issue-body.py

Issue 本文ファイルに対して以下のガードを適用し、YAML/JSON で結果を出力する。

Guards:
  1. Template Guard         — 必須セクションが存在するか（ISSUE_TEMPLATE 動的取得）
  2. Outcome Quality Guard  — Outcome が成果物形式・完了条件を含むか
  3. Diff Threshold         — 削減率が 50% 以下か（--orig-file 指定時）
  4. AC-VC Alignment        — AC 番号集合と VC の # AC<N> 番号集合が一致するか
                              （issue_kind 別 skip: VC セクションが不要な種別ではスキップ）

Usage:
    python3 guard-issue-body.py <body_file> [--orig-file <original_file>]
                                [--format yaml|json] [--issue-kind implementation|research|parent]

Exit codes:
    0 — all guards pass
    2 — at least one guard failed
    1 — unexpected error
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# allowlist: ファイルパスは安全な文字のみ許可
_PATH_RE = re.compile(r'^[A-Za-z0-9._/\-]+$')

# Outcome 不適合パターン（動作状態のみ・成果物形式欠落）
_OUTCOME_NG_RE = re.compile(
    r'(決定される|整理される|完了する|検討する|改善する)\s*$',
    re.MULTILINE
)

# issue_kind の安全な文字のみ許可（パストラバーサル対策）
_ISSUE_KIND_RE = re.compile(r'^[A-Za-z0-9_-]+$')

# fenced code block を除去するための正規表現
_FENCED_CODE_BLOCK_RE = re.compile(r'```.*?```', re.DOTALL)


def validate_path(value: str) -> Path:
    if not _PATH_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"Path must match ^[A-Za-z0-9._/-]+$, got: {value!r}"
        )
    p = Path(value)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"File not found: {value!r}")
    return p


def validate_issue_kind(issue_kind: str) -> None:
    """
    issue_kind がパストラバーサルに利用できない安全な文字列であることを検証する。

    Raises:
        ValueError: 不正な文字が含まれる場合
    """
    if not _ISSUE_KIND_RE.fullmatch(issue_kind):
        raise ValueError(
            f"Invalid issue_kind: {issue_kind!r}. "
            "Must match ^[A-Za-z0-9_-]+$ (no path traversal characters allowed)"
        )


def load_required_labels(template_dir: Path, issue_kind: str) -> list:
    """
    .github/ISSUE_TEMPLATE/{issue_kind}.yml をパースし、
    validations.required: true の要素の attributes.label を返す。
    type: markdown 要素は除外する。

    スコープ: 本リポジトリの implementation / research / parent テンプレートの
    validations.required: true の attributes.label に限定した照合を行う。
    GitHub Forms schema 全体（checkboxes の option 単位 required 等）への
    一般対応ではない。

    Args:
        template_dir: ISSUE_TEMPLATE ディレクトリのパス
        issue_kind: 種別（implementation / research / parent 等）

    Returns:
        必須ラベルのリスト（`## <label>` 形式で本文内の見出しと照合する）

    Raises:
        ValueError: issue_kind に不正な文字が含まれる場合、または
                    テンプレートの構造が不正な場合（dict でない、body が配列でない等）
        FileNotFoundError: テンプレートファイルが存在しない場合
    """
    # パストラバーサル対策: issue_kind の文字列を検証
    validate_issue_kind(issue_kind)

    template_path = template_dir / f"{issue_kind}.yml"

    # パス解決後にテンプレートディレクトリ外を参照していないか確認
    if template_path.resolve().parent != template_dir.resolve():
        raise ValueError(
            f"Resolved template path escapes template_dir: {template_path.resolve()}"
        )

    if not template_path.exists():
        raise FileNotFoundError(
            f"ISSUE_TEMPLATE not found for kind '{issue_kind}': {template_path}"
        )

    with template_path.open(encoding="utf-8") as f:
        template = yaml.safe_load(f)

    # テンプレート構造の検証
    if not isinstance(template, dict):
        raise ValueError(
            f"Template file '{issue_kind}.yml' must be a YAML mapping (dict), "
            f"got {type(template).__name__!r}"
        )

    body_items = template.get("body", [])
    if not isinstance(body_items, list):
        raise ValueError(
            f"'body' in '{issue_kind}.yml' must be a list, "
            f"got {type(body_items).__name__!r}"
        )

    required_labels = []

    for item in body_items:
        # type: markdown 要素は除外
        if item.get("type") == "markdown":
            continue

        validations = item.get("validations", {})
        if not validations.get("required"):
            continue

        attrs = item.get("attributes", {})
        label = attrs.get("label")

        if label is None:
            raise ValueError(
                f"Required item in '{issue_kind}.yml' is missing 'attributes.label': {item!r}"
            )
        if not isinstance(label, str):
            raise ValueError(
                f"'attributes.label' must be a string, got {type(label).__name__!r}: {label!r}"
            )

        required_labels.append(label)

    return required_labels


def _extract_mrc_section(body: str) -> str:
    """
    本文から `## Machine-Readable Contract` セクションのテキストを返す。
    次の `## ` 見出しまでを切り出す。

    Returns:
        str: MRC セクションのテキスト（セクションが存在しない場合は空文字列）
    """
    lines = body.splitlines()
    in_section = False
    section_lines = []
    for line in lines:
        if re.match(r'^##[ \t]+Machine-Readable Contract[ \t]*$', line):
            in_section = True
            continue
        if in_section:
            if re.match(r'^##[ \t]+', line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def extract_issue_kind_from_body(body: str):
    """
    本文の `## Machine-Readable Contract` セクション配下にある fenced yaml ブロックから
    issue_kind フィールドを抽出する。

    抽出条件（すべてを満たす場合のみ issue_kind を返す）:
    - ブロックが `## Machine-Readable Contract` セクション配下にある
    - yaml.safe_load() が dict を返す
    - `contract_schema_version` が "v1" である
    - `issue_kind` が str である

    Returns:
        str | None: issue_kind 文字列、または見つからなかった場合 None
    """
    mrc_section = _extract_mrc_section(body)
    if not mrc_section:
        return None

    # MRC セクション内の fenced yaml ブロックを検索
    for match in re.finditer(r'```yaml\s*(.*?)```', mrc_section, re.DOTALL):
        block_text = match.group(1)
        try:
            data = yaml.safe_load(block_text)
            if not isinstance(data, dict):
                continue
            if data.get("contract_schema_version") != "v1":
                continue
            issue_kind = data.get("issue_kind")
            if isinstance(issue_kind, str) and issue_kind.strip():
                return issue_kind.strip()
        except yaml.YAMLError:
            continue
    return None


def resolve_template_dir() -> Path:
    """
    リポジトリルートを起点に .github/ISSUE_TEMPLATE ディレクトリを返す。
    スクリプトが .claude/skills/... にあることを考慮して複数候補を探す。
    """
    # スクリプト自体のパスを起点に探す
    script_path = Path(__file__).resolve()

    # git リポジトリルートを探す（.git ディレクトリを上向きに検索）
    current = script_path.parent
    for _ in range(10):
        if (current / ".git").exists():
            candidate = current / ".github" / "ISSUE_TEMPLATE"
            if candidate.is_dir():
                return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent

    raise FileNotFoundError(
        "Could not locate .github/ISSUE_TEMPLATE directory from script path"
    )


def _strip_fenced_code_blocks(text: str) -> str:
    """
    テキストから fenced code block（``` で囲まれた範囲）を除去して返す。
    """
    return _FENCED_CODE_BLOCK_RE.sub('', text)


def guard_template(body: str, issue_kind: str, template_dir=None) -> dict:
    """
    ISSUE_TEMPLATE の validations.required: true ラベルを動的に取得して
    本文に `## <label>` 形式で存在するか確認する。

    コードブロック・引用内の偽陽性を防ぐため、fenced code block を除去した
    本文に対して行頭 Markdown 見出しとして正規表現でマッチする。

    Args:
        body: Issue 本文
        issue_kind: 種別（implementation / research / parent 等）
        template_dir: ISSUE_TEMPLATE ディレクトリ（省略時は自動検出）
    """
    if template_dir is None:
        template_dir = resolve_template_dir()

    try:
        required_labels = load_required_labels(template_dir, issue_kind)
    except FileNotFoundError as e:
        return {
            "name": "template_guard",
            "passed": False,
            "error": str(e),
            "missing_sections": [],
        }
    except ValueError as e:
        return {
            "name": "template_guard",
            "passed": False,
            "error": str(e),
            "missing_sections": [],
        }

    # fenced code block を除去した本文で行頭見出しを確認
    stripped_body = _strip_fenced_code_blocks(body)
    missing = []
    for label in required_labels:
        pattern = re.compile(
            rf'^##[ \t]+{re.escape(label)}[ \t]*$',
            re.MULTILINE
        )
        if not pattern.search(stripped_body):
            missing.append(f"## {label}")

    return {
        "name": "template_guard",
        "passed": len(missing) == 0,
        "missing_sections": missing,
    }


def extract_outcome_block(text: str) -> str:
    lines = text.splitlines()
    in_block = False
    block_lines = []
    for line in lines:
        if line.strip() == "## Outcome":
            in_block = True
            continue
        if in_block:
            if line.startswith("## "):
                break
            block_lines.append(line)
    return "\n".join(block_lines)


def guard_outcome_quality(body: str) -> dict:
    outcome_block = extract_outcome_block(body)
    ng_match = _OUTCOME_NG_RE.search(outcome_block)
    return {
        "name": "outcome_quality_guard",
        "passed": ng_match is None,
        "detail": f"NG pattern found: {ng_match.group(0)!r}" if ng_match else None,
    }


def guard_diff_threshold(orig_text: str, new_text: str) -> dict:
    orig_lines = len(orig_text.splitlines())
    new_lines = len(new_text.splitlines())
    diff_lines = orig_lines - new_lines
    threshold = orig_lines // 2
    passed = diff_lines <= threshold
    return {
        "name": "diff_threshold",
        "passed": passed,
        "orig_lines": orig_lines,
        "new_lines": new_lines,
        "diff_lines": diff_lines,
        "threshold": threshold,
    }


def _extract_vc_section(body: str) -> str:
    """
    本文から `## Verification Commands` セクションのテキストを返す。
    次の `## ` 見出しまでを切り出す。

    Returns:
        str: VC セクションのテキスト（セクションが存在しない場合は空文字列）
    """
    lines = body.splitlines()
    in_section = False
    section_lines = []
    for line in lines:
        if re.match(r'^##[ \t]+Verification Commands[ \t]*$', line):
            in_section = True
            continue
        if in_section:
            if re.match(r'^##[ \t]+', line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def guard_ac_vc_alignment(body: str, issue_kind: str, template_dir=None) -> dict:
    """
    AC 番号集合と VC の # AC<N> 番号集合が一致するか確認する。
    issue_kind が VC セクションを必須に持たない種別（parent 等）では skipped: true を返す。

    「VC セクションを必須に持つ種別か」は ISSUE_TEMPLATE の required label に
    'Verification Commands' が含まれるかで動的に判定する（ハードコードしない）。

    AC 番号は `- [x] AC<N>` 形式から抽出し、VC 番号は `## Verification Commands`
    セクション配下の `# AC<N>` コメントから抽出する。
    重複番号がある場合も集合一致で判定するため偽陽性を防ぐ。
    """
    if template_dir is None:
        try:
            template_dir = resolve_template_dir()
        except FileNotFoundError:
            # テンプレートディレクトリが見つからない場合はスキップしない（安全側）
            template_dir = None

    # VC セクションが required かどうかを ISSUE_TEMPLATE から判定
    has_vc_required = False
    if template_dir is not None:
        try:
            required_labels = load_required_labels(template_dir, issue_kind)
            has_vc_required = "Verification Commands" in required_labels
        except (FileNotFoundError, ValueError):
            # テンプレートが見つからない場合はスキップしない（安全側）
            has_vc_required = True

    if not has_vc_required:
        return {
            "name": "ac_vc_alignment",
            "passed": True,
            "skipped": True,
            "reason": f"issue_kind '{issue_kind}' does not require Verification Commands section",
        }

    # AC 番号を Acceptance Criteria から抽出（集合で管理）
    ac_numbers = re.findall(r'^- \[.\] AC(\d+)\b', body, re.MULTILINE)
    # VC 番号を Verification Commands セクション配下からのみ抽出（集合で管理）
    vc_section = _extract_vc_section(body)
    vc_numbers = re.findall(r'# AC(\d+)\b', vc_section)

    ac_count = len(ac_numbers)
    vc_ac_count = len(vc_numbers)

    if ac_count == 0:
        passed = True
    else:
        passed = sorted(ac_numbers) == sorted(vc_numbers)

    return {
        "name": "ac_vc_alignment",
        "passed": passed,
        "skipped": False,
        "ac_count": ac_count,
        "vc_ac_count": vc_ac_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Guard checks for GitHub Issue body files"
    )
    parser.add_argument(
        "body_file",
        type=validate_path,
        help="Path to new body file (safe chars only)"
    )
    parser.add_argument(
        "--orig-file",
        dest="orig_file",
        type=validate_path,
        default=None,
        help="Path to original body file for diff threshold check"
    )
    parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Output format (default: yaml)"
    )
    parser.add_argument(
        "--issue-kind",
        dest="issue_kind",
        choices=["implementation", "research", "parent"],
        default=None,
        help="Issue kind for template-based guard (implementation/research/parent)"
    )
    args = parser.parse_args()

    body = args.body_file.read_text(encoding="utf-8")

    # issue_kind 解決: (1) --issue-kind 引数 → (2) 本文 MRC の issue_kind → (3) fail
    issue_kind = args.issue_kind
    if issue_kind is None:
        issue_kind = extract_issue_kind_from_body(body)

    if issue_kind is None:
        # issue_kind を解決できない場合は template_guard を fail
        results = [
            {
                "name": "template_guard",
                "passed": False,
                "error": (
                    "Cannot determine issue_kind. "
                    "Provide --issue-kind argument or include issue_kind in Machine-Readable Contract block."
                ),
                "missing_sections": [],
            }
        ]
        results.append(guard_outcome_quality(body))

        if args.orig_file is not None:
            orig_body = args.orig_file.read_text(encoding="utf-8")
            results.append(guard_diff_threshold(orig_body, body))

        # ac_vc_alignment は issue_kind 不明のためスキップしない（安全側）
        ac_numbers = re.findall(r'^- \[.\] AC(\d+)\b', body, re.MULTILINE)
        vc_section = _extract_vc_section(body)
        vc_numbers = re.findall(r'# AC(\d+)\b', vc_section)
        ac_count = len(ac_numbers)
        vc_ac_count = len(vc_numbers)
        if ac_count == 0:
            passed = True
        else:
            passed = sorted(ac_numbers) == sorted(vc_numbers)
        results.append({
            "name": "ac_vc_alignment",
            "passed": passed,
            "skipped": False,
            "ac_count": ac_count,
            "vc_ac_count": vc_ac_count,
        })
    else:
        results = []
        results.append(guard_template(body, issue_kind))
        results.append(guard_outcome_quality(body))

        if args.orig_file is not None:
            orig_body = args.orig_file.read_text(encoding="utf-8")
            results.append(guard_diff_threshold(orig_body, body))

        results.append(guard_ac_vc_alignment(body, issue_kind))

    all_passed = all(r["passed"] for r in results)
    output = {
        "all_passed": all_passed,
        "guards": results,
    }

    if args.format == "json":
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        # Simple YAML-like output (no external dependency beyond PyYAML)
        print(f"all_passed: {str(all_passed).lower()}")
        print("guards:")
        for r in results:
            print(f"  - name: {r['name']}")
            print(f"    passed: {str(r['passed']).lower()}")
            for k, v in r.items():
                if k in ("name", "passed"):
                    continue
                if v is None:
                    print(f"    {k}: null")
                elif isinstance(v, bool):
                    print(f"    {k}: {str(v).lower()}")
                elif isinstance(v, list):
                    if v:
                        print(f"    {k}:")
                        for item in v:
                            print(f"      - {item!r}")
                    else:
                        print(f"    {k}: []")
                else:
                    print(f"    {k}: {v}")

    sys.exit(0 if all_passed else 2)


if __name__ == "__main__":
    main()
