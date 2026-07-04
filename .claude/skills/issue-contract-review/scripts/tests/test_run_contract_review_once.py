#!/usr/bin/env python3
"""
Unit tests for run_contract_review_once.py

Issue #1333: baseline_vc_preflight.py の per-command timeout デフォルト値引き上げ
（30 -> 90秒）に伴い、run_contract_review_once.py 側の
_VC_PREFLIGHT_TIMEOUT（複数 VC 直列実行を許容する上位タイムアウト）も
安全マージンを持って引き上げる必要がある。本テストはその回帰を防ぐ。
"""

import sys
from pathlib import Path


def test_vc_preflight_timeout_is_600():
    """Issue #1333 AC2: _VC_PREFLIGHT_TIMEOUT が600秒であることを確認する"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import run_contract_review_once

    assert run_contract_review_once._VC_PREFLIGHT_TIMEOUT == 600
