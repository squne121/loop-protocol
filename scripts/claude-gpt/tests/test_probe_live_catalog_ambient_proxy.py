"""scripts/claude-gpt/tests/test_probe_live_catalog_ambient_proxy.py

Issue #2948: `claude_gpt_probe_live_catalog`（repair_proxy.sh の再検証 probe）の 2 箇所の
curl（readiness loop と catalog 取得）が、ambient な curl routing（`http_proxy` /
`ALL_PROXY` / `all_proxy` / default `.curlrc` の `proxy` / `connect-to`）を継承せず、使い捨て
loopback proxy（fake-bin）へ直接到達することを固定する focused regression test。

観測方法（mock / 静的検査では代替しない）:
  - 実 `bash` が実 `lib.sh` を source して `claude_gpt_probe_live_catalog <fake-bin> <port>` を
    実 process として実行する。curl も実 curl。
  - fake-bin は `serve --port <port> --no-monitor` を受けて 127.0.0.1 で `/v1/models` を返す
    別 process で、受信した request path を hit log へ追記する（target の受信件数の証拠）。
  - fake ambient proxy / decoy は in-process の loopback server で、受信 path を記録する。
  - 各 test は、同じ hermetic env で素の curl が ambient 設定に従って fake proxy / decoy へ流れる
    ことを control assertion として先に示す。control が失敗した場合は false PASS を避けるため
    test を FAIL にする。

`.curlrc` の `proxy` 指令は `--noproxy '*'` 単独でも無効化されるため `-q` を固定できない。
`connect-to` は `--noproxy` では無効化されず `-q`（default config を読まない）でのみ無効化される
ので、AC6 の test が readiness curl / catalog curl それぞれの `-q` を固定する。

実行環境: curl が無ければ SKIP（exit 77。SKIP は PASS ではない）。
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
LIB_SH = SCRIPT_DIR / "lib.sh"

TARGET_MODELS = ("gpt-6-sol", "gpt-6-luna", "target-only-marker")
CATALOG_PATH = "/v1/models"

# 大文字 HTTP_PROXY は curl 自身が（CGI 対策で）無視するため対象外。curl が実際に参照する変数のみ。
PROXY_VARS = ("http_proxy", "ALL_PROXY", "all_proxy")

# 親 env に host から紛れ込むと偶然 green / red になる変数。hermetic env からは必ず除去する。
_SCRUBBED_VARS = (
    "NO_PROXY",
    "no_proxy",
    "CURL_HOME",
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
    "CURL_CA_BUNDLE",
)


@pytest.fixture(autouse=True)
def _require_curl() -> None:
    if shutil.which("curl") is None:
        # SKIP は PASS ではない。environment-unavailable として exit 77 で宣言する。
        pytest.exit("SKIP: curl unavailable (environment-unavailable, exit 77); never a PASS", returncode=77)


def _hermetic_env(tmp_path: Path, **overrides: str) -> dict:
    """PATH / HOME / XDG_CONFIG_HOME のみから作り直した親 env。host の proxy / curl 設定は継承しない。"""
    home = tmp_path / "ambient-home"
    xdg = tmp_path / "ambient-xdg-config"
    home.mkdir(parents=True, exist_ok=True)
    xdg.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
    }
    env.update(overrides)
    return env


class _RecordingServer:
    """In-process loopback HTTP server。受信した request path をすべて記録する。

    fake proxy / decoy は不完全な catalog を返し、誤って経由した probe が本物の target と
    取り違えられないようにする。"""

    def __init__(self, models, port: int = 0):
        self.hits: list[str] = []
        hits = self.hits
        body = json.dumps({"data": [{"id": m} for m in models]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


_FAKE_BIN_SOURCE = textwrap.dedent(
    """\
    #!{python}
    import json
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    argv = sys.argv[1:]
    if argv[:1] != ["serve"] or "--port" not in argv or "--no-monitor" not in argv:
        sys.exit(2)
    port = int(argv[argv.index("--port") + 1])
    body = json.dumps({{"data": [{{"id": m}} for m in {models!r}]}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            with open({hit_log!r}, "a", encoding="utf-8") as fh:
                fh.write(self.path + "\\n")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    """
)


def _write_fake_bin(tmp_path: Path) -> tuple[Path, Path]:
    """`serve --port <port> --no-monitor` を受けて 127.0.0.1 で /v1/models を返す実行ファイル。

    返り値: (binary path, 受信 path を追記する hit log)。"""
    hit_log = tmp_path / "target-hits.log"
    hit_log.write_text("", encoding="utf-8")
    binary = tmp_path / "fake-claude-code-proxy"
    binary.write_text(
        _FAKE_BIN_SOURCE.format(python=sys.executable, models=list(TARGET_MODELS), hit_log=str(hit_log)),
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary, hit_log


def _target_hits(hit_log: Path) -> list[str]:
    return [line for line in hit_log.read_text(encoding="utf-8").splitlines() if line]


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_probe(env: dict, binary: Path, port: int) -> subprocess.CompletedProcess:
    """実 bash が実 lib.sh を source して probe 関数を実 process として呼ぶ。"""
    return subprocess.run(
        ["bash", "-c", '. "$1"; claude_gpt_probe_live_catalog "$2" "$3"', "probe", str(LIB_SH), str(binary), str(port)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _plain_curl(env: dict, url: str, *extra: str) -> None:
    """Control: 同じ hermetic env で動かす素の curl（`extra` で option を足せる）。"""
    subprocess.run(
        ["curl", *extra, "-s", "--connect-timeout", "2", "-m", "3", "-o", "/dev/null", url],
        env=env,
        capture_output=True,
        timeout=10,
    )


def _write_curlrc(env: dict, text: str) -> None:
    # curl 8.5 は $CURL_HOME, $XDG_CONFIG_HOME/.curlrc, $HOME/.curlrc の順に探す。
    # どれが優先されても読まれるよう、候補すべてへ同じ内容を書く。
    for directory in (Path(env["HOME"]), Path(env["XDG_CONFIG_HOME"])):
        (directory / ".curlrc").write_text(text, encoding="utf-8")


def _assert_probe_reached_target(proc, hit_log: Path, *diverted: tuple[str, list[str]]) -> None:
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    catalog = json.loads(proc.stdout)
    assert [m["id"] for m in catalog["data"]] == list(TARGET_MODELS), (
        f"returned catalog is not the target's: {proc.stdout}"
    )
    hits = _target_hits(hit_log)
    assert hits.count(CATALOG_PATH) >= 2, f"target must receive readiness + catalog requests, got {hits}"
    for name, got in diverted:
        assert got == [], f"probe was diverted to {name}: {got}"


# ---------------------------------------------------------------------------
# AC1: ambient env proxy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("proxy_var", PROXY_VARS)
def test_ambient_env_proxy_not_used(tmp_path, proxy_var):
    binary, hit_log = _write_fake_bin(tmp_path)
    port = _free_port()
    with _RecordingServer(("not-the-target",)) as proxy:
        env = _hermetic_env(tmp_path, **{proxy_var: proxy.url})
        assert env[proxy_var] == proxy.url
        # control: ambient proxy 設定が実際に素の curl を fake proxy へ流す（target が稼働していても）。
        with _RecordingServer(TARGET_MODELS, port=port) as control_target:
            _plain_curl(env, f"http://127.0.0.1:{port}{CATALOG_PATH}")
            assert proxy.hits, (
                "control failed: ambient proxy setting did not divert plain curl; test would be a false PASS"
            )
            assert control_target.hits == [], "control failed: plain curl reached the target directly"
        proxy.hits.clear()

        proc = _run_probe(env, binary, port)

    _assert_probe_reached_target(proc, hit_log, ("the ambient proxy", proxy.hits))


# ---------------------------------------------------------------------------
# AC2: default .curlrc proxy (HOME / XDG_CONFIG_HOME)
# ---------------------------------------------------------------------------


def test_default_curlrc_proxy_not_used(tmp_path):
    binary, hit_log = _write_fake_bin(tmp_path)
    port = _free_port()
    with _RecordingServer(("not-the-target",)) as proxy:
        env = _hermetic_env(tmp_path)
        _write_curlrc(env, f'proxy = "{proxy.url}"\n')
        with _RecordingServer(TARGET_MODELS, port=port) as control_target:
            _plain_curl(env, f"http://127.0.0.1:{port}{CATALOG_PATH}")
            assert proxy.hits, (
                "control failed: .curlrc proxy was not honoured by plain curl; test would be a false PASS"
            )
            assert control_target.hits == [], "control failed: plain curl reached the target directly"
        proxy.hits.clear()

        proc = _run_probe(env, binary, port)

    _assert_probe_reached_target(proc, hit_log, ("the .curlrc proxy", proxy.hits))


# ---------------------------------------------------------------------------
# AC3: no ambient proxy -> normal environment still returns the target's catalog
# ---------------------------------------------------------------------------


def test_no_ambient_proxy_returns_catalog(tmp_path):
    binary, hit_log = _write_fake_bin(tmp_path)
    port = _free_port()
    env = _hermetic_env(tmp_path)
    for key in _SCRUBBED_VARS:
        assert key not in env

    proc = _run_probe(env, binary, port)

    _assert_probe_reached_target(proc, hit_log)


# ---------------------------------------------------------------------------
# AC6: .curlrc connect-to (neutralised only by `-q`, not by `--noproxy '*'`)
# ---------------------------------------------------------------------------


def test_default_curlrc_connect_to_not_used(tmp_path):
    binary, hit_log = _write_fake_bin(tmp_path)
    port = _free_port()
    with _RecordingServer(("decoy-only",)) as decoy, _RecordingServer(("not-the-target",)) as ambient_proxy:
        env = _hermetic_env(tmp_path, http_proxy=ambient_proxy.url)
        _write_curlrc(env, f'connect-to = "127.0.0.1:{port}:127.0.0.1:{decoy.port}"\n')
        target_url = f"http://127.0.0.1:{port}{CATALOG_PATH}"
        # ambient proxy は `--noproxy '*'` / `-q` 無しの curl だけを巻き込む。ここでは connect-to を
        # 単独で観測したいので、control では ambient proxy を env から外した env を使う。
        control_env = {k: v for k, v in env.items() if k != "http_proxy"}
        with _RecordingServer(TARGET_MODELS, port=port) as control_target:
            # control 1: 素の curl は .curlrc の connect-to で decoy に流れ、target には届かない。
            _plain_curl(control_env, target_url)
            assert decoy.hits and not control_target.hits, (
                "control failed: plain curl did not follow .curlrc connect-to"
            )
            decoy.hits.clear()
            # control 2: `--noproxy '*'` 単独でも connect-to は無効化されない（-q 固定が必要な根拠）。
            _plain_curl(control_env, target_url, "--noproxy", "*")
            assert decoy.hits and not control_target.hits, "control failed: --noproxy '*' alone neutralised connect-to"
        decoy.hits.clear()
        ambient_proxy.hits.clear()

        proc = _run_probe(env, binary, port)

    _assert_probe_reached_target(
        proc,
        hit_log,
        ("the .curlrc connect-to decoy", decoy.hits),
        ("the ambient proxy", ambient_proxy.hits),
    )
