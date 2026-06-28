# Fixture: policy-example marked block (AC6b)

The fenced block below is immediately preceded by a `<!-- policy-example -->`
HTML comment, so the checker must treat it as illustrative and exclude it from
scanning even though it contains a non-compliant form.

<!-- policy-example -->
```bash
uv run pytest scripts/ci/tests/test_python_invocation_policy.py
```

The fenced block below is NOT marked and would be scanned in a governed file
(here it is fixture-excluded, but the test scans this file directly):

```bash
uv run --locked pytest scripts/ci/tests/test_python_invocation_policy.py
```
