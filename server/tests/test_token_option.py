"""The API token can be fixed with --token, --token-file or $GCSENDER_TOKEN."""

import http.client
import os
import socket
import subprocess
import sys
import time

import pytest

from gcsender.__main__ import parse_args, resolve_token

GOOD = "my-fixed-token_123.ABC~"


def test_sources_and_precedence(tmp_path):
    assert resolve_token(parse_args([]), {}) == (None, "random")
    assert resolve_token(parse_args(["--token", GOOD]), {"GCSENDER_TOKEN": "x" * 20}) == (GOOD, "--token")
    f = tmp_path / "tok"
    f.write_text(GOOD + "\n")
    assert resolve_token(parse_args(["--token-file", str(f)]), {}) == (GOOD, "--token-file")
    assert resolve_token(parse_args([]), {"GCSENDER_TOKEN": GOOD}) == (GOOD, "$GCSENDER_TOKEN")


@pytest.mark.parametrize("bad", ["short", "has space in it here", 'quote"injection-attempt', "<script>alert(1)</script>", "x" * 300])
def test_unsafe_or_short_tokens_are_refused(bad):
    with pytest.raises(ValueError, match="16-256"):
        resolve_token(parse_args(["--token", bad]), {})


def test_conflicts_and_missing_file(tmp_path):
    with pytest.raises(ValueError, match="either"):
        resolve_token(parse_args(["--token", GOOD, "--token-file", str(tmp_path / "t")]), {})
    with pytest.raises(ValueError, match="cannot read"):
        resolve_token(parse_args(["--token-file", str(tmp_path / "missing")]), {})


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, path, token):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}", "X-GCS-Token": token})
    status = conn.getresponse().status
    conn.close()
    return status


def test_server_uses_the_fixed_token(tmp_path):
    port = _free_port()
    env = {**os.environ, "GCSENDER_TOKEN": GOOD}
    proc = subprocess.Popen(
        [sys.executable, "-m", "gcsender", "--simulate", "0.9j", "--http", f"127.0.0.1:{port}", "--state-dir", str(tmp_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                status = _get(port, "/api/state", GOOD)
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.1)
        assert status == 200
        assert _get(port, "/api/state", "some-other-token-1234") == 401
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    assert "API token: fixed, from $GCSENDER_TOKEN" in out


def test_server_refuses_a_bad_token(tmp_path):
    r = subprocess.run([sys.executable, "-m", "gcsender", "--token", "short", "--state-dir", str(tmp_path)],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "16-256" in r.stderr
