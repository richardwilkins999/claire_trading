"""The bin/claire control script's contract with the schema.

The script is bash, but the two things that make it dangerous are SQL: which
tables a reset wipes, and which rows count as "a trade in flight". Both are
parsed out of the script here so a schema rename cannot quietly turn a reset
into a config wipe, or make a graceful stop blind to a live order.
"""
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.accounting import db

BIN = Path(__file__).resolve().parent.parent / "bin"
SCRIPT = BIN / "claire"
RESET = BIN / "claire-reset"


def _tables():
    conn = db.connect(":memory:")
    db.init(conn)
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _reset_list():
    m = re.search(r"TABLES = \[(.*?)\]", RESET.read_text(), re.S)
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_reset_only_touches_tables_that_exist():
    missing = set(_reset_list()) - _tables()
    assert not missing, f"reset would silently skip: {missing}"


def test_reset_keeps_configuration():
    """Wiping trades must not wipe the desk's identity — its providers,
    agents and prompts, schedules, venues or instruments."""
    keep = {"providers", "agents", "agent_versions", "schedules",
            "mcp_servers", "instruments", "exchange_sessions", "watchlist",
            "provider_health", "provider_models"}
    assert not (set(_reset_list()) & keep)


def test_reset_clears_everything_that_records_a_trade():
    """The inverse guard: a table that holds money or run history must be in
    the list, or a 'reset' leaves a half-book behind."""
    must_clear = {"orders", "executions", "lots", "lot_closures",
                  "cash_transactions", "work_items", "events", "agent_runs",
                  "agent_reports", "broker_accounts"}
    assert must_clear <= set(_reset_list())


def test_inflight_query_matches_the_order_lifecycle():
    """A graceful stop waits on orders that are not terminal. If someone adds
    a new terminal status, this catches the script still waiting forever."""
    conn = db.connect(":memory:")
    db.init(conn)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='orders'"
                       ).fetchone()[0]
    statuses = set(re.findall(r"'(\w+)'",
                              sql[sql.find("status TEXT"):sql.find("),", sql.find("status TEXT"))]))
    excluded = set(re.search(
        r"status NOT IN\"\s*\n\s*\" \((.*?)\)", SCRIPT.read_text(), re.S
    ).group(1).replace("'", "").split(","))
    excluded = {s.strip() for s in excluded}
    live = statuses - excluded
    # these three mean the venue still owes us an answer
    assert live == {"pending_session", "placed", "partially_filled"}, live


def test_inflight_covers_authorised_but_unacked_approvals():
    """token_state='pending_resume' means the human approved and the resume
    was not acked — the one state where a trade can still appear after a
    stop, so a drain must wait for it."""
    assert "pending_resume" in SCRIPT.read_text()


def test_graceful_stop_pauses_and_restores_schedules():
    text = SCRIPT.read_text()
    assert "UPDATE schedules SET enabled=0" in text     # pause during drain
    assert "UPDATE schedules SET enabled=1" in text     # and put them back
    # restored on every exit path, or a Ctrl-C / hang-up leaves the desk
    # mute — and a signal must END the drain, not resume it
    assert "trap restore_schedules EXIT" in text
    for sig in ("HUP", "INT", "TERM"):
        assert f"trap 'on_drain_signal {sig} " in text


def test_reset_refuses_to_run_against_a_live_desk():
    text = RESET.read_text()
    assert "stop it first" in text
    assert 'backup="$ROOT/var/backup-$stamp"' in text   # and backs up first


def test_destroying_the_book_is_not_in_the_everyday_script():
    """start/stop is run daily; wiping the book is run once. They must not
    share an entry point — a typo should not be able to reach the DELETEs."""
    daily = SCRIPT.read_text()
    assert "DELETE FROM" not in daily
    assert "cmd_reset" not in daily
    assert RESET.exists() and RESET.stat().st_mode & 0o111   # and executable


def test_the_gate_is_documented_as_surviving_a_restart():
    """A stop does not drain for an approval, because it does not need to:
    the checkpoint and token persist. Verified live — keep it written down
    so nobody 'fixes' the drain by adding awaiting_approval to it."""
    text = SCRIPT.read_text()
    assert "checkpoint" in text and "persist" in text
    # the drain must NOT wait on a human decision
    inflight = re.search(r'if what == "inflight":(.*?)elif', text, re.S).group(1)
    assert "awaiting_approval" not in inflight


# ── network exposure ────────────────────────────────────────────────────────
def test_dashboards_bind_is_configurable_but_defaults_to_loopback():
    """Putting the desk on the LAN must be a deliberate setting, not the
    default a fresh clone inherits."""
    import inspect
    from app import dashboards
    sig = inspect.signature(dashboards.create_server)
    assert sig.parameters["bind"].default == "127.0.0.1"


def test_claire_api_is_never_exposed_off_loopback():
    """The dashboards may go on the LAN; claire-api may not. It owns
    /internal/resume — the one endpoint that turns a click into a trade — and
    its host guard is the last line of defence, so the bind must not become
    configurable alongside the dashboards'."""
    src = (Path(__file__).resolve().parent.parent / "app" / "api"
           / "server.py").read_text()
    assert 'host="127.0.0.1"' in src
    assert "CLAIRE_DASHBOARD_BIND" not in src


def test_the_money_path_is_gated_by_the_key_not_by_the_network():
    """Read routes are open to whoever can reach the port, so every action
    that moves money must check pairing itself rather than relying on the
    listener being loopback."""
    src = (Path(__file__).resolve().parent.parent / "app" / "dash"
           / "handler.py").read_text()       # where do_POST lives now
    for route in ("/api/thesis-action", "/api/override"):
        i = src.index(route)
        assert "_paired()" in src[i:i + 400], f"{route} is not key-gated"



# ── behaviour, in a throwaway checkout (never the live desk) ──────────────
REPO = BIN.parent


def _fake_root(tmp_path, name="desk"):
    """A scratch checkout: copies of the scripts, symlinks to the real app
    package and venv, its own var/ and etc/. Its ROOT is under tmp_path, so
    pids_for can only ever match processes whose cwd is that directory."""
    root = tmp_path / name
    (root / "bin").mkdir(parents=True)
    (root / "var").mkdir()
    (root / "etc").mkdir()
    for script in ("claire", "claire-reset"):
        dst = root / "bin" / script
        dst.write_text((BIN / script).read_text())
        dst.chmod(0o755)
    (root / "app").symlink_to(REPO / "app")
    (root / "venv").symlink_to(REPO / "venv")
    assert not str(root.resolve()).startswith(str(REPO.resolve()))
    return root


def _desk_db(root, enabled=("custodian", "watcher")):
    from app import scheduler
    conn = db.connect(str(root / "var" / "desk.db"))
    db.init(conn)
    scheduler.seed(conn)
    conn.execute("UPDATE schedules SET enabled=0")
    for job in enabled:
        conn.execute("UPDATE schedules SET enabled=1 WHERE job=?", (job,))
    return conn


def _enabled(root):
    c = sqlite3.connect(root / "var" / "desk.db")
    try:
        return {r[0] for r in c.execute(
            "SELECT job FROM schedules WHERE enabled=1")}
    finally:
        c.close()


def _fake_service(cwd, module="app.api.server"):
    # argv carries "-m app.api.server" like the real thing; it just sleeps
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "-m", module],
        cwd=cwd)


def _run(root, *args, timeout=60, **kw):
    env = {**os.environ, "CLAIRE_DRAIN_TIMEOUT": "20"}
    return subprocess.run(["bash", str(root / "bin" / "claire"), *args],
                          capture_output=True, text=True, timeout=timeout,
                          env=env, **kw)


@pytest.fixture
def procs():
    started = []
    yield started
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait()


def test_stop_only_touches_this_checkouts_processes(tmp_path, procs):
    root = _fake_root(tmp_path)
    other = tmp_path / "other-clone"
    other.mkdir()
    _desk_db(root)
    mine = _fake_service(root)
    decoy = _fake_service(other)                 # same argv, other checkout
    procs += [mine, decoy]
    time.sleep(0.3)
    r = _run(root, "stop", "--graceful")
    assert r.returncode == 0, r.stdout + r.stderr
    mine.wait(timeout=10)
    assert decoy.poll() is None, "stop killed another checkout's desk"
    assert _enabled(root) == {"custodian", "watcher"}      # restored
    assert not (root / "var" / ".drain-schedules.json").exists()


def test_stop_with_nothing_enabled_keeps_a_previous_drain_list(tmp_path,
                                                               procs):
    """M1: a drain killed before it could restore leaves every schedule off
    plus its state file. The next stop must not overwrite that file with an
    empty list — it restores the saved jobs instead."""
    root = _fake_root(tmp_path)
    _desk_db(root, enabled=())
    state = root / "var" / ".drain-schedules.json"
    state.write_text("reconcile analysis_asia")
    procs.append(_fake_service(root))
    time.sleep(0.3)
    r = _run(root, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _enabled(root) == {"reconcile", "analysis_asia"}
    assert not state.exists()


@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 143),
                                      (signal.SIGHUP, 129),
                                      (signal.SIGINT, 130)])
def test_signal_mid_drain_restores_schedules_and_exits(tmp_path, procs,
                                                       sig, code):
    root = _fake_root(tmp_path)
    conn = _desk_db(root)
    conn.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                 " created_at, updated_at) VALUES ('wi_1','pipeline','D05',"
                 " 'running','wi_1',0,0)")               # keeps the drain busy
    svc = _fake_service(root)
    procs.append(svc)
    time.sleep(0.3)
    env = {**os.environ, "CLAIRE_DRAIN_TIMEOUT": "60"}
    stop = subprocess.Popen(["bash", str(root / "bin" / "claire"), "stop"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env)
    procs.append(stop)
    state = root / "var" / ".drain-schedules.json"
    deadline = time.time() + 20
    while not (state.exists() and not _enabled(root)):
        assert time.time() < deadline, "drain never paused the schedules"
        time.sleep(0.1)
    time.sleep(0.5)
    stop.send_signal(sig)
    out, _ = stop.communicate(timeout=20)
    assert stop.returncode == code, out
    assert _enabled(root) == {"custodian", "watcher"}, out
    assert not state.exists()
    assert svc.poll() is None                     # interrupted ≠ stopped


def test_unit_stop_timeout_covers_the_drain():
    unit = (REPO / "systemd" / "claire.service").read_text()
    stop = int(re.search(r"^TimeoutStopSec=(\d+)", unit, re.M).group(1))
    drain = int(re.search(r"^Environment=CLAIRE_DRAIN_TIMEOUT=(\d+)", unit,
                          re.M).group(1))
    script_default = int(re.search(r"CLAIRE_DRAIN_TIMEOUT:-(\d+)",
                                   SCRIPT.read_text()).group(1))
    assert drain == script_default
    assert stop >= drain + 60          # kill wait (17 s) + restore + margin
    assert re.search(r"^KillMode=mixed", unit, re.M)


def test_pgrep_is_scoped_to_user():
    for script in (SCRIPT, RESET):
        text = script.read_text()
        assert 'pgrep -u "$(id -u)"' in text
        assert "ROOT_REAL" in text
        assert re.search(r"pgrep -f \"\$1\"", text) is None


def test_reset_backup_includes_wal_and_removes_checkpoint_sidecars(tmp_path):
    """M2: the newest commits live in desk.db-wal until a checkpoint; a cp of
    desk.db alone loses them. The online backup must contain them."""
    root = _fake_root(tmp_path)
    conn = _desk_db(root)
    raw = sqlite3.connect(root / "var" / "desk.db")
    raw.execute("PRAGMA wal_autocheckpoint=0")
    raw.execute("INSERT INTO work_items (id, kind, ticker, state, thread_id,"
                " created_at, updated_at) VALUES ('wi_wal','pipeline','D05',"
                " 'done','wi_wal',0,0)")
    raw.commit()
    assert (root / "var" / "desk.db-wal").stat().st_size > 0
    ck = sqlite3.connect(root / "var" / "checkpoints.db")
    ck.execute("PRAGMA journal_mode=WAL")
    ck.execute("PRAGMA wal_autocheckpoint=0")
    ck.execute("CREATE TABLE t (x)")
    ck.execute("INSERT INTO t VALUES (1)")
    ck.commit()
    assert (root / "var" / "checkpoints.db-wal").exists()
    assert (root / "var" / "checkpoints.db-shm").exists()

    r = subprocess.run(["bash", str(root / "bin" / "claire-reset"), "--yes"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    raw.close(); ck.close(); conn.close()

    (backup,) = list((root / "var").glob("backup-*"))
    b = sqlite3.connect(backup / "desk.db")
    assert b.execute("SELECT COUNT(*) FROM work_items WHERE id='wi_wal'"
                     ).fetchone()[0] == 1
    b.close()
    assert not list(backup.glob("*-wal")), "backup must be self-contained"
    bk = sqlite3.connect(backup / "checkpoints.db")
    assert bk.execute("SELECT x FROM t").fetchall() == [(1,)]
    bk.close()
    for suffix in ("", "-wal", "-shm"):
        assert not (root / "var" / f"checkpoints.db{suffix}").exists(), suffix
    # and the reset itself happened
    c = sqlite3.connect(root / "var" / "desk.db")
    assert c.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    c.close()


# ── watchdog: restart what crashed, never what was stopped on purpose ───────
# (REVIEW-2026-11-11 §3.6: the installed unit is a one-shot, so systemd does
# not see a later crash.) The scratch checkout has no etc/claire.env, so the
# start the watchdog attempts stops at "missing …/claire.env": the decision
# is tested, nothing is ever launched.
MARK = ".stopped-on-purpose"


def test_watchdog_is_silent_when_both_services_are_up(tmp_path, procs):
    root = _fake_root(tmp_path)
    procs += [_fake_service(root), _fake_service(root, "app.dashboards")]
    time.sleep(0.3)
    r = _run(root, "watchdog")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not (root / "var" / "log" / "watchdog.log").exists()


def test_watchdog_starts_what_has_died(tmp_path, procs):
    root = _fake_root(tmp_path)
    procs += [_fake_service(root)]              # the dashboards have crashed
    time.sleep(0.3)
    r = _run(root, "watchdog")
    out = r.stdout + r.stderr
    assert "claire-dashboards not running — starting" in out
    assert "claire-api not" not in out
    assert "missing" in out and "claire.env" in out     # it went on to start
    log = (root / "var" / "log" / "watchdog.log").read_text()
    assert "restarted claire-dashboards" in log and "WARNING" in log


def test_watchdog_leaves_a_desk_you_stopped_alone(tmp_path, procs):
    root = _fake_root(tmp_path)
    _desk_db(root)
    svc = _fake_service(root)
    procs += [svc]
    time.sleep(0.3)
    assert _run(root, "stop", "--kill").returncode == 0
    svc.wait(timeout=10)
    assert (root / "var" / MARK).exists()
    r = _run(root, "watchdog")                  # nothing running, on purpose
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    # stopping a desk that is already down still counts as "on purpose"
    (root / "var" / MARK).unlink()
    assert _run(root, "stop").returncode == 0
    assert (root / "var" / MARK).exists()


def test_start_clears_the_stopped_marker_and_the_units_exist():
    src = (BIN / "claire").read_text()
    start = src[src.index("cmd_start() {"):src.index("cmd_status() {")]
    assert 'rm -f "$STOPPED_MARK"' in start
    unit = (REPO / "systemd" / "claire-watchdog.service").read_text()
    timer = (REPO / "systemd" / "claire-watchdog.timer").read_text()
    assert "bin/claire watchdog" in unit and "Type=oneshot" in unit
    assert "OnUnitActiveSec=1min" in timer
