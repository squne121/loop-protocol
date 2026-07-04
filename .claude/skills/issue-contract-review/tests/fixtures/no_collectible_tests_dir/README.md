このディレクトリは Issue #1285 review Blocker (PR #1305) 対応のための専用 fixture です。
`pytest_exit5_no_tests.md` の VC が `docs/` のような将来変化しうる実ディレクトリに
依存しないよう、pytest discovery 対象（`test_*.py` / `*_test.py`）を絶対に含まない
決定論的な空ディレクトリとして用意しています。このファイル自体は pytest に収集され
ません（`.py` ではないため）。このディレクトリに `test_*.py` ファイルを追加しないで
ください（追加すると AC7 の exit 5 期待値が壊れます）。
