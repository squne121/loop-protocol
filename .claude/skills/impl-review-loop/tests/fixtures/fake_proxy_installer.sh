#!/bin/sh
# fake_proxy_installer.sh — Issue #2810 AC9/AC10 hermetic fixture.
#
# Referenced via `CLAUDE_GPT_REPAIR_INSTALLER_URL=file://<this file>` in the
# AC9/AC10 Verification Commands. `scripts/claude-gpt/repair_proxy.sh`
# fetches its installer script text via `curl` (which supports the `file://`
# scheme without any network access) and executes it with `bash`, passing
# through `CLAUDE_CODE_PROXY_INSTALL_DIR` / `CLAUDE_CODE_PROXY_VERSION`
# unmodified -- exactly the same hermetic strategy already established by
# `scripts/claude-gpt/tests/test_repair_proxy.py`'s `FAKE_INSTALLER_SOURCE`
# fixture (reused here, not reinvented), with the required model set
# hardcoded to the same `gpt-6-sol` / `gpt-6-luna` pair
# `scripts/claude-gpt/lib.sh::claude_gpt_required_model_set()` derives from
# the repository's default `CLAUDE_GPT_MODEL_*` aliases -- so
# `repair_proxy.sh`'s own live-catalog re-verification (AC5 of #2801) passes
# without any additional env var beyond what the AC9/AC10 VC lines already
# set.
#
# This fixture never performs real network access, never touches the
# operator's real ~/.claude-gpt, and only writes inside
# CLAUDE_CODE_PROXY_INSTALL_DIR (which repair_proxy.sh derives from the
# fixture-scoped CLAUDE_GPT_HOME the AC9/AC10 VC lines set).

set -e
: "${CLAUDE_CODE_PROXY_INSTALL_DIR:?}"
: "${CLAUDE_CODE_PROXY_VERSION:?}"

mkdir -p "$CLAUDE_CODE_PROXY_INSTALL_DIR"
cat > "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy" <<PYEOF
#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ["gpt-6-sol", "gpt-6-luna"]
VERSION = "claude-code-proxy ${CLAUDE_CODE_PROXY_VERSION}"


def _serve(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            return

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print(VERSION)
        return 0
    if args[0] == "serve":
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if port is None:
            return 1
        return _serve(port)
    return 1


sys.exit(main())
PYEOF
chmod +x "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy"
