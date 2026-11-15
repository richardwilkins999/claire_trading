"""The two ports are settings (app/ports.py), so two desks can run on one
machine. Every place that used to say 7787 / 7788 asks there."""
import re
import subprocess
from pathlib import Path

import pytest

from app import ports

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_are_the_ports_the_desk_has_always_used():
    assert ports.check({}) == (7788, 7787)
    assert ports.api_base({}) == "http://127.0.0.1:7788"
    assert ports.dashboard_base({}) == "http://127.0.0.1:7787"


def test_a_second_desk_gets_its_own_pair():
    env = {"CLAIRE_API_PORT": "7798", "CLAIRE_DASHBOARD_PORT": " 7797 "}
    assert ports.check(env) == (7798, 7797)
    assert ports.api_base(env) == "http://127.0.0.1:7798"


@pytest.mark.parametrize("env, words", [
    ({"CLAIRE_API_PORT": "abc"}, "not a port number"),
    ({"CLAIRE_DASHBOARD_PORT": "80"}, "between 1024 and 65535"),
    ({"CLAIRE_API_PORT": "70000"}, "between 1024 and 65535"),
    ({"CLAIRE_API_PORT": "7787"}, "must be different"),       # = dashboards
    ({"CLAIRE_API_PORT": "9000", "CLAIRE_DASHBOARD_PORT": "9000"},
     "must be different"),
])
def test_a_bad_value_is_refused_not_defaulted(env, words):
    """Falling back to the default would collide with the desk already
    running there."""
    with pytest.raises(ports.PortError, match=words):
        ports.check(env)


def test_no_port_number_is_written_into_the_code_that_runs():
    """A literal 7787 / 7788 in a URL or a listen call would ignore the
    setting. (Comments, docstrings and the defaults themselves are fine.)"""
    bad = []
    for f in [*ROOT.glob("app/**/*.py"), ROOT / "bin" / "claire"]:
        if f.name == "ports.py":
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(:|port\s*=\s*)778[78]\b", code) and \
                    '"""' not in line and "e.g." not in line and \
                    "port_setting" not in line and \
                    not line.lstrip().startswith(('"', "'", "(", "→")):
                bad.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
    # create_server's own defaults are for tests; the service passes both
    bad = [b for b in bad if "def create_server" not in b
           and "api_base=\"http://127.0.0.1:7788\"," not in b
           and "port=7787, env=None" not in b]
    assert not bad, bad


def test_the_services_and_the_script_read_the_settings():
    api = (ROOT / "app/api/server.py").read_text()
    dash = (ROOT / "app/dashboards.py").read_text()
    assert "port=api_port" in api and "ports.check()" in api
    assert "port=dash_port" in dash and "api_base=api_base" in dash
    assert "ports.dashboard_port(env)" in (
        ROOT / "app/telegram_approvals.py").read_text()
    example = (ROOT / "etc/claire.env.example").read_text()
    assert "CLAIRE_API_PORT=" in example and "CLAIRE_DASHBOARD_PORT=" in example


def _script_ports(tmp_path, env_text, extra_env=None):
    """What bin/claire works out, from a scratch settings file."""
    script = (ROOT / "bin/claire").read_text()
    fn = script[script.index("port_setting() {"):script.index('DASH_URL="')]
    env_file = tmp_path / "claire.env"
    env_file.write_text(env_text)
    out = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nENV_FILE="{env_file}"\n{fn}\n'
                       'echo "$API_PORT $DASH_PORT $API_URL"'],
        capture_output=True, text=True, timeout=10,
        env={"PATH": "/usr/bin:/bin", **(extra_env or {})})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_control_script_reads_the_same_two_settings(tmp_path):
    assert _script_ports(tmp_path, "") == "7788 7787 http://127.0.0.1:7788"
    assert _script_ports(
        tmp_path, "X=1\nCLAIRE_API_PORT=7798\nCLAIRE_DASHBOARD_PORT=7797\n"
    ) == "7798 7797 http://127.0.0.1:7798"
    # empty or not a number: the default, as the services would refuse it
    assert _script_ports(tmp_path, "CLAIRE_API_PORT=\n").startswith("7788 ")
    assert _script_ports(tmp_path, "CLAIRE_API_PORT=abc\n").startswith("7788 ")
    # the environment wins over the file, as it does for the services
    assert _script_ports(tmp_path, "CLAIRE_API_PORT=7798\n",
                         {"CLAIRE_API_PORT": "7808"}).startswith("7808 ")
