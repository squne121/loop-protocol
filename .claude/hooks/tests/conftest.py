import sys
from pathlib import Path

# importlib モードでは sys.path への自動追加が行われないため、
# 同ディレクトリ内の test_guard_api_input 等をインポート可能にする。
sys.path.insert(0, str(Path(__file__).parent))
