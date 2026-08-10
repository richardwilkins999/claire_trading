"""`run_python` sandbox (DESIGN.md §9): numpy/pandas over data the TOOL
fetches, in a subprocess with a scrubbed environment (no claire.env secrets),
no network, and CPU/time/memory caps. An injected prompt cannot reach the
internal endpoints or any credential.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_PRELUDE = r"""
import resource, socket, builtins, json, sys
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)

def _no_net(*a, **k):
    raise RuntimeError("network is disabled in run_python")
socket.socket = _no_net
socket.create_connection = _no_net
socket.getaddrinfo = _no_net

_data = json.load(open(sys.argv[1]))
DATA = _data          # the OHLCV/context dict the tool fetched for the agent
RESULT = None
_code = open(sys.argv[2]).read()
exec(compile(_code, "<agent-code>", "exec"))
json.dump({"result": RESULT}, open(sys.argv[3], "w"), default=str)
"""


def run_python(code: str, data: dict, *, timeout=15,
               python=sys.executable) -> dict:
    """Execute agent-authored analysis code. The code sees `DATA` (dict) and
    must set `RESULT`. Returns {result} or {error}."""
    with tempfile.TemporaryDirectory(prefix="claire-sbx-") as td:
        td = Path(td)
        (td / "data.json").write_text(json.dumps(data, default=str))
        (td / "code.py").write_text(code)
        (td / "prelude.py").write_text(_PRELUDE)
        try:
            proc = subprocess.run(
                [python, "-I", str(td / "prelude.py"), str(td / "data.json"),
                 str(td / "code.py"), str(td / "out.json")],
                capture_output=True, text=True, timeout=timeout,
                cwd=td,
                env={"PATH": "/usr/bin:/bin",            # SCRUBBED — no claire.env
                     "HOME": str(td)},
            )
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {timeout}s"}
        if proc.returncode != 0:
            return {"error": (proc.stderr or "failed")[-2000:]}
        try:
            return json.loads((td / "out.json").read_text())
        except (OSError, json.JSONDecodeError):
            return {"error": "code did not produce RESULT"}
