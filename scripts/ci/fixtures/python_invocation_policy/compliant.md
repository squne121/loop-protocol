# Fixture: compliant invocations (positive cases)

Excluded from the governed-surface scan; consumed by the test suite to assert
that each canonical `uv run --locked` form is NOT flagged.

```bash
uv run --locked pytest scripts/ci/tests/test_python_invocation_policy.py
uv run --locked python3 scripts/ci/check_python_invocation_policy.py --strict
uv run --locked python scripts/ci/check_python_invocation_policy.py
uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py
```
