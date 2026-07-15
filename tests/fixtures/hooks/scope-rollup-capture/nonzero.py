import sys

sys.stdout.write("scope-rollup-fixture-stdout-must-not-leak\n")
sys.stderr.write("scope-rollup-fixture-stderr-must-not-leak\n")
raise SystemExit(9)
