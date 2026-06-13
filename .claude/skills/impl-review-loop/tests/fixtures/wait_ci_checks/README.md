# Fixtures for wait_ci_checks helper tests

`test_wait_ci_checks.py` covers the mocked `gh` matrix directly in Python:

- `pass`
- `pending -> fail`
- `cancel`
- `skipping`
- `no checks`
- `head_sha_changed`
- `auth_error`

Add serialized fixture payloads here only when a case becomes too large to keep inline.
