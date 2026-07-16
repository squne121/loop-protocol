import os
from pathlib import Path


capture_dir = os.environ.get("SCOPE_ROLLUP_CAPTURE_DIR")
if capture_dir:
    env_probe_payload = []
    if "SECRET_TEST" in os.environ:
        env_probe_payload.append("secret-leaked")
    if "CODEX_SCOPE_ROLLUP_CAPTURE_SCRIPT" in os.environ:
        env_probe_payload.append("script-override-env-leaked")

    Path(capture_dir).joinpath("env_probe.txt").write_text(
        ",".join(env_probe_payload),
        encoding="utf-8",
    )
