#!/usr/bin/env python3
"""
guard-issue-body.py

Issue 本文ファイルに対して以下のガードを適用し、YAML/JSON で結果を出力する。

Guards:
  1. Template Guard         — 必須セクションが存在するか（ISSUE_TEMPLATE 動的取得）
  2. Outcome Quality Guard  — Outcome が成果物形式・完了条件を含むか
  3. Diff Threshold         — 削減率が 50% 以下か（--orig-file 指定時）
  4. AC-VC Alignment        — AC 番号と VC の # AC<N> コメントの件数が一致するか
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

# Machine-Readable Contract block の issue_kind を抽出する正規表現
_MRC_BLOCK_RE = re.compile(
    r'```yaml\s*(.*?)```',
    re.DOTALL
)


def validate_path(value: str) -> Path:
    if not _PATH_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"Path must match ^[A-Za-z0-9._/-]+$, got: {value!r}"
        )
    p = Path(value)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"File not found: {value!r}")
    return p


def load_required_labels(template_dir: Path, issue_kind: str) -> list:
    """
    .github/ISSUE_TEMPLATE/{issue_kind}.yml をパースし、
    validations.required: true の要素の attributes.label を返す。
    type: markdown 要素は除外する。

    Args:
        template_dir: ISSUE_TEMPLATE ディレクトリのパス
        issue_kind: 種別（implementation / research / parent 等）

    Returns:
        必須ラベルのリスト（`## <label>` 形式で本文内の見出しと照合する）

    Raises:
        FileNotFoundError: テンプレートファイルが存在しない場合
        ValueError: attributes.label が無い/文字列でない場合
    """
    template_path = template_dir / f"{issue_kind}.yml"
    if not template_path.exists():
        raise FileNotFoundError(
            f"ISSUE_TEMPLATE not found for kind '{issue_kind}': {template_path}"
        )

    with template_path.open(encoding="utf-8") as f:
        template = yaml.safe_load(f)

    body_items = template.get("body", [])
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


def extract_issue_kind_from_body(body: str):
    """
    本文の Machine-Readable Contract fenced yaml 内の issue_kind フィールドを抽出する。
    複数の ```yaml ブロックがある場合は最初に contract_schema_version を含むものを使う。

    Returns:
        str | None: issue_kind 文字列、または見つからなかった場合 None
    """
    for match in _MRC_BLOCK_RE.finditer(body):
        block_text = match.group(1)
        try:
            data = yaml.safe_load(block_text)
            if isinstance(data, dict) and "issue_kind" in data:
                issue_kind = data["issue_kind"]
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


def guard_template(body: str, issue_kind: str, template_dir=None) -> dict:
    """
    ISSUE_TEMPLATE の validations.required: true ラベルを動的に取得して
    本文に `## <label>` 形式で存在するか確認する。

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

    # `## <label>` 形式で本文内に存在するか確認
    missing = []
    for label in required_labels:
        section_header = f"## {label}"
        if section_header not in body:
            missing.append(section_header)

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


def guard_ac_vc_alignment(body: str, issue_kind: str, template_dir=None) -> dict:
    """
    AC 番号と VC の # AC<N> コメントの件数が一致するか確認する。
    issue_kind が VC セクションを必須に持たない種別（parent 等）では skipped: true を返す。

    「VC セクションを必須に持つ種別か」は ISSUE_TEMPLATE の required label に
    'Verification Commands' が含まれるかで動的に判定する（ハードコードしない）。
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

    ac_count = len(re.findall(r'^- \[.\] AC\d+', body, re.MULTILINE))
    vc_ac_count = len(re.findall(r'# AC\d+', body))
    passed = (ac_count == 0) or (ac_count == vc_ac_count)
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
        ac_count = len(re.findall(r'^- \[.\] AC\d+', body, re.MULTILINE))
        vc_ac_count = len(re.findall(r'# AC\d+', body))
        passed = (ac_count == 0) or (ac_count == vc_ac_count)
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
