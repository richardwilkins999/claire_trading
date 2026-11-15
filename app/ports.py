"""The two ports this desk listens on — settings, so a second desk can run
on the same machine (a clone in another folder with its own etc/claire.env
and var/).

  CLAIRE_API_PORT        claire-api, loopback only            (default 7788)
  CLAIRE_DASHBOARD_PORT  the dashboards (pages + their API)   (default 7787)

Everything that used to say 7787 / 7788 asks here: the two servers, the
links one process uses to reach the other, the maintainer's probe, the link
in a Telegram card, and `bin/claire` (which reads the same two lines from
etc/claire.env). A bad value stops the start with a plain message — a desk
that silently fell back to the default would collide with the one already
running there.
"""
import os

API_DEFAULT, DASHBOARD_DEFAULT = 7788, 7787


class PortError(ValueError):
    pass


def _port(env, name, default) -> int:
    raw = ((env or {}).get(name) or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        raise PortError(f"{name}={raw!r} is not a port number") from None
    if not 1024 <= port <= 65535:
        raise PortError(f"{name}={port} must be between 1024 and 65535")
    return port


def api_port(env=None) -> int:
    return _port(os.environ if env is None else env,
                 "CLAIRE_API_PORT", API_DEFAULT)


def dashboard_port(env=None) -> int:
    return _port(os.environ if env is None else env,
                 "CLAIRE_DASHBOARD_PORT", DASHBOARD_DEFAULT)


def check(env=None):
    """Both ports, validated together: (api, dashboard). They must differ."""
    a, d = api_port(env), dashboard_port(env)
    if a == d:
        raise PortError(f"CLAIRE_API_PORT and CLAIRE_DASHBOARD_PORT are both "
                        f"{a} — they must be different")
    return a, d


def api_base(env=None) -> str:
    """Where claire-api answers. Always loopback: it is never on the LAN."""
    return f"http://127.0.0.1:{api_port(env)}"


def dashboard_base(env=None) -> str:
    """The dashboards as this machine reaches them."""
    return f"http://127.0.0.1:{dashboard_port(env)}"
