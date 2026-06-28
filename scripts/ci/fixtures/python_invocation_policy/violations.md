# Fixture: non-compliant invocations (negative cases)

This fixture lives under `scripts/ci/fixtures/python_invocation_policy/**` and is
excluded from the governed-surface scan. It is consumed directly by the policy
checker test suite to assert that each non-compliant form is detected.

```bash
uv run pytest scripts/ci/tests/test_python_invocation_policy.py
uv run python3 scripts/ci/check_python_invocation_policy.py --strict
python3 -m pytest scripts/ci/tests/test_python_invocation_policy.py
uv run --locked python3 -m pytest scripts/ci/tests/test_python_invocation_policy.py
```
