"""The maintainer (DESIGN.md §23): plain-code checks and housekeeping with
fixed retention, an AI note that can fail without consequence, and things
it must never delete."""
import gzip
import json
import os
import sqlite3
import threading
import time

import httpx
import pytest

from app import maintenance
from app.accounting import db
from app.accounting.money import to_micro
from app.accounting.repo import Repo
from app.dashboards import create_server
from app.tools.files import Narratives

NOW = 1_793_900_000
DAY = 86400
DKEY = "dash-key-123"


@pytest.fixture
def world(tmp_path):
    (tmp_path / "var" / "log").mkdir(parents=True)
    (tmp_path / "etc").mkdir()
    conn = db.connect(tmp_path / "var" / "desk.db")
    db.init(conn)
    home = tmp_path / "home"
    m = maintenance.Maintainer(conn, root=tmp_path, clock=lambda: NOW,
                               home=home)
    return tmp_path, conn, m, home


def item(conn, wi, state, updated, archived=None):
    conn.execute(
        "INSERT INTO work_items (id, kind, ticker, state, thread_id,"
        " created_at, updated_at, archived_at) VALUES (?,?,?,?,?,?,?,?)",
        (wi, "pipeline", "PG", state, wi, updated, updated, archived))


def test_logs_rotate_by_copytruncate_and_keep_seven(world, monkeypatch):
    root, conn, m, _ = world
    monkeypatch.setattr(maintenance, "LOG_ROTATE_BYTES", 100)
    log = root / "var" / "log" / "api.log"
    for i in range(9):                              # older rotations
        (root / "var" / "log" / f"api.log.2026010{i}-000000.gz").write_bytes(b"x")
    log.write_text("GET /api/orders\n" * 50)
    with open(log, "ab") as writer:                 # a service holding it open
        a = m.rotate_logs()
        writer.write(b"after rotation\n")
    assert log.read_text() == "after rotation\n"    # same file, still written
    gz = sorted((root / "var" / "log").glob("api.log.*.gz"))
    assert len(gz) == maintenance.LOG_KEEP
    newest = gzip.decompress(gz[-1].read_bytes()).decode()
    assert newest.startswith("GET /api/orders")
    assert "api.log" in a["detail"] and a["ok"]


def test_small_logs_are_left_alone(world):
    root, _, m, _ = world
    (root / "var" / "log" / "api.log").write_text("hi\n")
    m.rotate_logs()
    assert (root / "var" / "log" / "api.log").read_text() == "hi\n"
    assert not list((root / "var" / "log").glob("*.gz"))


def test_backups_one_a_day_keep_seven(world):
    root, conn, m, _ = world
    b = root / "var" / "backups"
    b.mkdir()
    for d in range(1, 9):
        (b / f"desk-2026010{d}.db").write_bytes(b"old")
    m.backup()
    files = sorted(b.glob("desk-*.db"))
    assert len(files) == maintenance.BACKUP_KEEP
    today = time.strftime("%Y%m%d", time.localtime(NOW))
    assert files[-1].name == f"desk-{today}.db"
    # a real, openable copy of desk.db
    c = sqlite3.connect(files[-1])
    assert c.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 10
    assert "already exists" in m.backup()["detail"]     # once a day


def _checkpoints(root, threads):
    c = sqlite3.connect(root / "var" / "checkpoints.db")
    c.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT"
              " DEFAULT '', checkpoint_id TEXT, parent_checkpoint_id TEXT,"
              " type TEXT, checkpoint BLOB, metadata BLOB)")
    c.execute("CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT"
              " DEFAULT '', checkpoint_id TEXT, task_id TEXT, idx INT,"
              " channel TEXT, type TEXT, value BLOB)")
    for t in threads:
        for i in range(3):
            c.execute("INSERT INTO checkpoints (thread_id, checkpoint_id)"
                      " VALUES (?,?)", (t, str(i)))
            c.execute("INSERT INTO writes (thread_id, checkpoint_id, task_id,"
                      " idx, channel) VALUES (?,?,?,?,?)", (t, str(i), "t", 0,
                                                            "c"))
    c.commit()
    c.close()


def test_only_long_finished_runs_lose_their_graph_state(world):
    root, conn, m, _ = world
    item(conn, "old_done", "done", NOW - 40 * DAY)
    item(conn, "old_rejected", "rejected", NOW - 31 * DAY)
    item(conn, "new_done", "done", NOW - 5 * DAY)
    item(conn, "old_waiting", "awaiting_approval", NOW - 60 * DAY)
    item(conn, "old_executing", "executing", NOW - 60 * DAY)
    _checkpoints(root, ["old_done", "old_rejected", "new_done", "old_waiting",
                        "old_executing"])
    a = m.prune_checkpoints()
    c = sqlite3.connect(root / "var" / "checkpoints.db")
    left = {r[0] for r in c.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    assert left == {"new_done", "old_waiting", "old_executing"}
    assert {r[0] for r in c.execute("SELECT DISTINCT thread_id FROM writes")} \
        == left
    assert "6 checkpoints from 2 runs" in a["detail"]


def test_health_rows_pruned_but_latest_per_service_kept(world):
    _, conn, m, _ = world
    for svc, ts in (("custodian", NOW - 40 * DAY), ("custodian", NOW - 1),
                    ("watcher", NOW - 50 * DAY)):
        conn.execute("INSERT INTO service_health (service, checked_at, ok,"
                     " detail) VALUES (?,?,1,'{}')", (svc, ts))
    m.prune_health()
    rows = conn.execute("SELECT service, checked_at FROM service_health"
                        " ORDER BY service").fetchall()
    assert [tuple(r) for r in rows] == [("custodian", NOW - 1),
                                        ("watcher", NOW - 50 * DAY)]


def test_old_write_ups_are_archived_and_stay_readable(world):
    root, conn, m, _ = world
    n = Narratives(root / "var" / "narratives")
    item(conn, "wi_old", "done", NOW - 90 * DAY, archived=NOW - 40 * DAY)
    item(conn, "wi_new", "done", NOW - 90 * DAY, archived=NOW - 2 * DAY)
    for wi in ("wi_old", "wi_new"):
        n.write(wi, "summary", f"# {wi} summary")
        n.write(wi, "news.report", "news")
    m.archive_narratives()
    assert not (root / "var" / "narratives" / "wi_old").exists()
    assert (root / "var" / "narratives-archive" / "wi_old.tar.gz").exists()
    assert (root / "var" / "narratives" / "wi_new").is_dir()
    assert n.list("wi_old") == ["news.report.md", "summary.md"]
    assert n.read("wi_old", "summary") == "# wi_old summary"
    with pytest.raises(FileNotFoundError):
        n.read("wi_old", "bull.report")


def test_old_ai_transcripts_deleted_new_kept(world):
    root, conn, m, home = world
    d = maintenance.sdk_transcript_dir(root, home)
    d.mkdir(parents=True)
    old, new, other = d / "old.jsonl", d / "new.jsonl", d / "notes.txt"
    for f in (old, new, other):
        f.write_text("{}")
    os.utime(old, (NOW - 40 * DAY,) * 2)
    os.utime(other, (NOW - 40 * DAY,) * 2)
    os.utime(new, (NOW - DAY,) * 2)
    m.prune_sdk_transcripts()
    assert not old.exists() and new.exists() and other.exists()


def test_sdk_folder_name_matches_the_claude_cli():
    p = maintenance.sdk_transcript_dir("/mnt/projects/claire_trading", "/h")
    assert str(p) == ("/h/.claude/projects/"
                      "-mnt-projects-claire-trading-var-sdk")


def test_a_full_run_never_touches_money_or_the_audit_trail(world):
    root, conn, m, _ = world
    repo = Repo(conn)
    repo.create_account("a", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"},
                        ts=NOW - 400 * DAY)
    repo.deposit("a", to_micro("100"), ts=NOW - 400 * DAY)
    item(conn, "ancient", "done", NOW - 400 * DAY, archived=NOW - 300 * DAY)
    repo.set_state("ancient", "done", actor="t", ts=NOW - 400 * DAY)
    conn.execute("INSERT INTO agent_runs (agent_id, started_at, status)"
                 " VALUES ('news', ?, 'ok')", (NOW - 400 * DAY,))
    tables = ("orders", "executions", "lots", "cash_transactions", "events",
              "work_items", "agent_runs", "broker_accounts")
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in tables}
    m.run(trigger="test")
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in tables}
    assert before == after


def test_run_records_a_report_with_the_ai_note(world):
    _, conn, m, _ = world
    seen = {}

    def note(facts):
        seen.update(facts)
        return "All quiet. Backups written."
    m.summarize = note
    r = m.run(trigger="test")
    assert {"status", "headline", "checks", "housekeeping"} <= set(seen)
    row = conn.execute("SELECT * FROM maintenance_runs").fetchone()
    assert row["summary"] == "All quiet. Backups written."
    assert row["status"] == r["status"] and row["trigger"] == "test"
    keys = {c["key"] for c in json.loads(row["checks_json"])}
    assert {"disk", "backups", "custodian", "logs", "sdk"} <= keys


def test_the_ai_failing_does_not_fail_maintenance(world):
    _, conn, m, _ = world

    def broken(facts):
        raise RuntimeError("model unavailable")
    m.summarize = broken
    r = m.run(trigger="test")
    assert r["summary"] is None and "model unavailable" in r["summary_error"]
    assert any(a["action"] == "backup" and a["ok"] for a in r["actions"])


def test_bad_status_alerts_once_not_every_run(world):
    _, conn, m, _ = world
    sent = []
    m.alert = lambda text, title: sent.append(text)
    m.probe = lambda url: False                     # dashboards down → bad
    assert m.run(trigger="test")["status"] == "bad"
    m.run(trigger="test")
    assert len(sent) == 1 and "Dashboards" in sent[0]


def test_one_run_at_a_time(world):
    _, _, m, _ = world
    with maintenance._lock:
        assert "already in progress" in m.run(trigger="test")["skipped"]


def test_page_and_run_button(world):
    root, conn, m, _ = world
    m.summarize = lambda f: "Fine."
    m.run(trigger="test")
    srv = create_server(conn, Repo(conn), object(), secret="s", dash_key=DKEY,
                        api_base="http://127.0.0.1:9", clock=lambda: NOW,
                        port=0, sse_interval=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert httpx.get(base + "/maintenance").status_code == 200
        d = httpx.get(base + "/api/maintenance").json()
        assert d["latest"]["summary"] == "Fine."
        assert d["retention"]["backup_keep"] == 7
        assert len(d["history"]) == 1
        # unpaired: refused; paired: forwarded (claire-api is down here)
        assert httpx.post(base + "/api/maintenance-run",
                          json={}).status_code == 403
        assert httpx.post(base + "/api/maintenance-run", json={},
                          headers={"X-Dash-Key": DKEY}).status_code == 502
    finally:
        srv.shutdown()


def test_internal_run_endpoint_needs_the_secret(world):
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    _, conn, _, _ = world
    ran = threading.Event()
    client = TestClient(create_app(object(), conn, "sec",
                                   maintenance_runner=lambda trigger:
                                   ran.set()))
    assert client.post("/internal/maintenance").status_code == 403
    r = client.post("/internal/maintenance", headers={"x-claire-secret": "sec"})
    assert r.status_code == 200 and ran.wait(2)
    with maintenance._lock:
        assert client.post("/internal/maintenance", headers={
            "x-claire-secret": "sec"}).status_code == 409


# ── the parts added 2026-11-11: arbiter notes, sim stops ─────────────────
def by_key(checks):
    return {c["key"]: c for c in checks}


def test_arbiter_notes_check_flags_cards_left_without_them(world):
    root, conn, m, _ = world
    item(conn, "wi_a", "awaiting_approval", NOW - 3600)
    conn.execute("UPDATE work_items SET thesis_json=? WHERE id='wi_a'",
                 (json.dumps({"conditions": ["SMA20 reclaim"]}),))
    c = by_key(m.checks())["arbiter_notes"]
    assert c["status"] == "warn" and "wi_a" in c["detail"]
    from app import arbiter_notes
    conn.execute("INSERT INTO arbiter_notes VALUES (?,?,?,?,?)",
                 ("wi_a", arbiter_notes.notes_hash(["SMA20 reclaim"]), "s",
                  json.dumps(["plain"]), NOW))
    assert by_key(m.checks())["arbiter_notes"]["status"] == "ok"


def test_sim_stops_check_says_whether_anything_is_checking_them(world):
    root, conn, m, _ = world
    assert by_key(m.checks())["sim_stops"]["value"] == "none working"
    repo = Repo(conn)
    repo.create_account("alpaca-paper", "alpaca", "paper", "USD",
                        fee_model={"type": "flat", "per_trade": "0"},
                        risk_limits={}, ts=NOW)
    repo.add_instrument("NASDAQ:CSCO", "CSCO", "NASDAQ", "USD")
    item(conn, "wi_s", "done", NOW)
    conn.execute(
        "INSERT INTO orders (id, work_item_id, account_id, instrument_id,"
        " side, qty, status, broker_order_id, leg, stop_loss, created_at,"
        " updated_at, expires_at) VALUES ('leg_1','wi_s','alpaca-paper',"
        " 'NASDAQ:CSCO','sell',9000000,'placed','sim-abc','stop',97000000,"
        " ?,?,?)", (NOW, NOW, NOW + DAY))
    c = by_key(m.checks())["sim_stops"]
    assert c["status"] == "bad" and "NOT being checked" in c["value"]
    conn.execute("INSERT INTO service_health (service, checked_at, ok,"
                 " detail) VALUES ('custodian', ?, 'ok', '{}')", (NOW - 120,))
    c = by_key(m.checks())["sim_stops"]
    assert c["status"] == "ok" and c["value"].startswith("1 working")


def test_housekeeping_drops_only_finished_sim_orders(world):
    root, conn, m, _ = world
    from app.tools.brokers import SIM_TABLE
    conn.execute(SIM_TABLE)
    old = NOW - 40 * DAY
    for oid, st, t in (("sim-done", "filled", old), ("sim-rest", "placed", old),
                       ("sim-new", "cancelled", NOW)):
        conn.execute("INSERT INTO sim_orders VALUES (?,?,?,?,?)",
                     (oid, "alpaca", None, json.dumps({"status": st}), t))
    a = m.prune_sim_orders()
    left = {r[0] for r in conn.execute("SELECT id FROM sim_orders")}
    assert left == {"sim-rest", "sim-new"} and a["ok"]      # a resting stop stays
