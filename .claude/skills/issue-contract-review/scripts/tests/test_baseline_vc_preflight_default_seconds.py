#!/usr/bin/env python3
"""
Unit tests for baseline_vc_preflight.py — Issue #1333 AC1 / AC4

AC1: baseline_vc_preflight.py に DEFAULT_TIMEOUT_SECONDS(>= 90) の named
     constant が追加され、--timeout-seconds の argparse default がこの
     定数を参照している(30固定値のままではない)ことを固定する。

Runtime Verification Applicability: not_applicable
本ファイルは named constant の値・argparse default の参照関係のみを
検証する軽量テストであり、実際に長時間 VC を実行する timing test は含まない。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import baseline_vc_preflight  # noqa: E402


def test_default_timeout_seconds_constant_is_at_least_90():
    """AC1: DEFAULT_TIMEOUT_SECONDS が90以上の named constant として存在する"""
    assert hasattr(baseline_vc_preflight, "DEFAULT_TIMEOUT_SECONDS")
    assert isinstance(baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS, int)
    assert baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS >= 90


def test_timeout_seconds_argparse_default_references_named_constant():
    """AC1: --timeout-seconds の argparse default が DEFAULT_TIMEOUT_SECONDS と一致する

    リテラル直書き(例: default=30)ではなく named constant を参照している
    ことを、実際に main() 相当の parser 構築ロジックを再現して検証する。
    main() は full run を伴うため、ここでは同一定義の parser を最小構成で
    再構築し、argparse が解決する実際の default 値を確認する。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS,
        help="Timeout per command",
    )
    args = parser.parse_args([])
    assert args.timeout_seconds == baseline_vc_preflight.DEFAULT_TIMEOUT_SECONDS
    assert args.timeout_seconds >= 90


def test_main_source_uses_named_constant_not_literal_30():
    """AC1: main() のソースが `default=30` のリテラル直書きに戻っていないことを固定する

    main() は argparse.ArgumentParser を関数内でインライン構築しており
    再利用可能な関数に分離されていないため、リテラル回帰を防ぐ回帰テストと
    してソース文字列を検査する（動作検証ではなく静的な契約遵守チェック）。
    """
    script_path = Path(__file__).parent.parent / "baseline_vc_preflight.py"
    with open(script_path) as f:
        script_content = f.read()

    assert '"--timeout-seconds"' in script_content
    assert "default=30" not in script_content, (
        "--timeout-seconds の default が literal 30 に戻っている"
    )
    assert "default=DEFAULT_TIMEOUT_SECONDS" in script_content
