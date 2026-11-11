"""The maintainer (DESIGN.md §23): keeps Claire healthy and says how she is.

Daily (and on demand from the Maintenance page) it:
  1. CHECKS — services, the custodian and broker sync, schedules, disk,
     backups, Saxo sign-ins, the
     arbiter's plain-English notes, the stop-losses Claire's built-in sim
     holds, and what is growing on disk;
  2. HOUSEKEEPS with fixed retention — rotates logs, keeps 7 daily backups,
     prunes finished runs' LangGraph checkpoints and old health rows, archives
     old narratives, deletes old Claude SDK transcripts, and drops the
     built-in sim's long-finished orders;
  3. asks the `maintainer` agent for a short plain-English status note.

Plain code does every check and every deletion. The AI only reads the
results and writes the note — it holds no tools and cannot remove anything.
Never touched, whatever their age: orders, executions, lots, cash, the
events audit trail, work items, agent_runs (cost metering), the live-trading
log. Each run is one row in maintenance_runs (the last 90 are kept).
"""
import gzip
import json
import os
import re
import shutil
import sqlite3
import tarfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400

# ── retention: deliberate, conservative, in one place ───────────────────
LOG_ROTATE_BYTES = 5 * 1024 * 1024      # rotate a log once it passes 5 MB
LOG_KEEP = 7                            # rotated copies kept per log
BACKUP_KEEP = 7                         # daily desk.db backups kept
CHECKPOINT_DAYS = 30                    # finished runs' graph state
HEALTH_DAYS = 30                        # service_health rows
NARRATIVE_DAYS = 30                     # archived runs' write-ups → tar.gz
SDK_TRANSCRIPT_DAYS = 30                # Claude SDK session transcripts
RUNS_KEEP = 90                          # maintenance_runs rows
SIM_ORDERS_DAYS = 30                    # the built-in sim's finished orders

TERMINAL = ("done", "rejected", "expired", "failed", "cancelled")
STATUS_RANK = {"ok": 0, "info": 0, "warn": 1, "bad": 2}

_lock = threading.Lock()


def sdk_transcript_dir(root=ROOT, home=None) -> Path:
    """Where the Claude CLI keeps the SDK agents' transcripts: the agents run
    with cwd=var/sdk, and the CLI names the folder after that path."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(root) / "var" / "sdk"))
    return Path(home or Path.home()) / ".claude" / "projects" / slug


def _size(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _mb(n) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _check(key, label, status, value, detail=""):
    return {"key": key, "label": label, "status": status, "value": value,
            "detail": detail}


class Maintainer:
    def __init__(self, conn, *, root=ROOT, clock=time.time, summarize=None,
                 alert=None, home=None, probe=None):
        self.conn, self.root, self.clock = conn, Path(root), clock
        self.var = self.root / "var"
        self.summarize = summarize      # (facts: dict) -> str, or None
        self.alert = alert              # (text, title=) -> None, or None
        self.sdk_dir = sdk_transcript_dir(self.root, home)
        # (url) -> bool: is the dashboards server answering?
        self.probe = probe

    # ── run ──────────────────────────────────────────────────────────────
    def run(self, trigger="schedule") -> dict:
        if not _lock.acquire(blocking=False):
            return {"skipped": "a maintenance run is already in progress"}
        try:
            return self._run(trigger)
        finally:
            _lock.release()

    def _run(self, trigger):
        started = int(self.clock())
        actions = []
        for step in (self.rotate_logs, self.backup, self.prune_checkpoints,
                     self.prune_health, self.archive_narratives,
                     self.prune_sdk_transcripts, self.prune_runs,
                     self.prune_sim_orders):
            try:
                actions.append(step())
            except Exception as e:              # noqa: BLE001 — one failing
                actions.append({"action": step.__name__, "ok": False,
                                "detail": f"error: {e}"[:300], "freed": 0})
        checks = self.checks()                  # after housekeeping: current
        status = max((c["status"] for c in checks),
                     key=lambda s: STATUS_RANK.get(s, 0), default="ok")
        if status == "info":
            status = "ok"
        if any(not a.get("ok", True) for a in actions) and status == "ok":
            status = "warn"
        headline = self.headline(status, checks, actions)
        summary, summary_error = None, None
        if self.summarize is not None:
            try:
                summary = self.summarize({
                    "status": status, "headline": headline,
                    "checks": [c for c in checks],
                    "housekeeping": actions})
            except Exception as e:              # noqa: BLE001 — the note is
                summary_error = str(e)[:300]    # a nicety, never a dependency
        finished = int(self.clock())
        cur = self.conn.execute(
            "INSERT INTO maintenance_runs (started_at, finished_at, trigger,"
            " status, headline, checks_json, actions_json, summary,"
            " summary_error) VALUES (?,?,?,?,?,?,?,?,?)",
            (started, finished, trigger, status, headline, json.dumps(checks),
             json.dumps(actions), summary, summary_error))
        if status == "bad" and self.alert is not None:
            prev = self.conn.execute(
                "SELECT status FROM maintenance_runs WHERE id < ?"
                " ORDER BY id DESC LIMIT 1", (cur.lastrowid,)).fetchone()
            if not prev or prev["status"] != "bad":     # on the change only
                try:
                    self.alert(f"{headline}\n\n{summary or ''}".strip(),
                               title="Claire maintenance")
                except Exception:               # noqa: BLE001
                    pass
        return {"id": cur.lastrowid, "status": status, "headline": headline,
                "checks": checks, "actions": actions, "summary": summary,
                "summary_error": summary_error}

    @staticmethod
    def headline(status, checks, actions) -> str:
        problems = [c for c in checks if c["status"] in ("warn", "bad")]
        freed = sum(a.get("freed", 0) for a in actions)
        head = {"ok": "All healthy", "warn": "Needs a look",
                "bad": "Needs attention now"}[status]
        if problems:
            head += ": " + "; ".join(c["label"] for c in problems[:4])
            if len(problems) > 4:
                head += f" (+{len(problems) - 4} more)"
        if freed:
            head += f". Housekeeping freed {_mb(freed)}"
        return head + "."

    # ── checks ───────────────────────────────────────────────────────────
    def checks(self) -> list:
        now = int(self.clock())
        out = []
        for fn in (self._check_services, self._check_custodian,
                   self._check_brokers, self._check_schedules,
                   self._check_disk, self._check_backups,
                   self._check_signins,
                   self._check_arbiter_notes, self._check_sim_stops,
                   self._check_growth):
            try:
                out.extend(fn(now))
            except Exception as e:              # noqa: BLE001
                out.append(_check(fn.__name__.removeprefix("_check_"),
                                  fn.__name__, "warn", "check failed",
                                  str(e)[:200]))
        return out

    def _check_services(self, now):
        out = []
        if self.probe is not None:
            up = self.probe("http://127.0.0.1:7787/")
            out.append(_check("dashboards", "Dashboards (:7787)",
                              "ok" if up else "bad",
                              "answering" if up else "NOT answering",
                              "" if up else "the pages and approvals are down"))
        # this code runs inside claire-api, so reaching here proves it is up
        out.append(_check("claire_api", "claire-api (:7788)", "ok", "running"))
        return out

    def _health(self, service):
        return self.conn.execute(
            "SELECT checked_at, ok, detail FROM service_health WHERE"
            " service=? ORDER BY checked_at DESC LIMIT 1",
            (service,)).fetchone()

    def _check_custodian(self, now):
        out = []
        for svc, label, every_min in (("custodian", "Custodian", 5),
                                      ("watcher", "Price watcher", 10)):
            h = self._health(svc)
            if h is None:
                out.append(_check(svc, label, "warn", "never ran"))
                continue
            age = now - h["checked_at"]
            flags = []
            try:
                flags = json.loads(h["detail"] or "{}").get("flags") or []
            except (TypeError, ValueError, AttributeError):
                pass
            if age > every_min * 60 * 3:
                st, val = "bad", f"last ran {age // 60} min ago"
            elif flags:
                st, val = "warn", f"{len(flags)} warning(s)"
            else:
                st, val = "ok", f"ran {age // 60} min ago, no warnings"
            out.append(_check(svc, label, st, val,
                              "; ".join(str(f) for f in flags[:3])[:400]))
        return out

    def _check_brokers(self, now):
        out = []
        try:
            rows = self.conn.execute(
                "SELECT a.id, s.venue, s.checked_at, s.contact_at, s.ok,"
                " s.error FROM broker_accounts a LEFT JOIN broker_sync s"
                " ON s.account_id=a.id").fetchall()
        except sqlite3.OperationalError:
            return [_check("brokers", "Broker sync", "warn",
                           "no sync table yet", "restart claire-api")]
        for r in rows:
            if r["checked_at"] is None:
                out.append(_check(f"broker:{r['id']}", f"Broker {r['id']}",
                                  "warn", "never synced"))
                continue
            stale = now - (r["contact_at"] or 0) > 15 * 60
            st = "bad" if stale and (r["venue"] or "").startswith("Saxo") \
                else "warn" if stale or not r["ok"] else "ok"
            val = (f"{r['venue']} · reached "
                   f"{(now - (r['contact_at'] or now)) // 60} min ago"
                   if r["contact_at"] else f"{r['venue']} · never reached")
            out.append(_check(f"broker:{r['id']}", f"Broker {r['id']}", st,
                              val, r["error"] or ""))
        return out

    def _check_schedules(self, now):
        out = []
        for r in self.conn.execute(
                "SELECT job, enabled, last_run_at, last_result FROM schedules"):
            res = {}
            try:
                res = json.loads(r["last_result"] or "null") or {}
            except (TypeError, ValueError):
                pass
            err = res.get("error") if isinstance(res, dict) else None
            if not r["enabled"]:
                st, val = "info", "disabled"
            elif err:
                st, val = "warn", "last run failed"
            else:
                st, val = "ok", "ok"
            out.append(_check(f"job:{r['job']}", f"Job {r['job']}", st, val,
                              (err or "")[:300]))
        return out

    def _check_disk(self, now):
        du = shutil.disk_usage(self.var if self.var.exists() else self.root)
        free = du.free / du.total * 100
        st = "bad" if free < 5 else "warn" if free < 15 else "ok"
        return [_check("disk", "Disk space", st,
                       f"{du.free / 1024 ** 3:.1f} GB free ({free:.0f}%)")]

    def _check_backups(self, now):
        bdir = self.var / "backups"
        files = sorted(bdir.glob("desk-*.db")) if bdir.is_dir() else []
        if not files:
            return [_check("backups", "Daily backups", "warn", "none yet",
                           f"kept in {bdir}")]
        age = now - int(files[-1].stat().st_mtime)
        st = "warn" if age > 2 * DAY else "ok"
        return [_check("backups", "Daily backups", st,
                       f"{len(files)} kept, newest {age // 3600} h old",
                       "same disk as the desk — copy off-machine for "
                       "real protection")]

    def _check_signins(self, now):
        out = []
        for env in ("sim", "live"):
            p = self.var / f"saxo_{env}_signin_alert.json"
            tok = self.root / "etc" / f"saxo_{env}_token.json"
            if not tok.exists():
                continue
            state = "ok"
            try:
                state = json.loads(p.read_text()).get("state", "ok")
            except (OSError, ValueError):
                pass
            out.append(_check(f"saxo_{env}", f"Saxo {env.upper()} sign-in",
                              "ok" if state == "ok" else "warn",
                              "signed in" if state == "ok" else state))
        return out

    def _check_arbiter_notes(self, now):
        """The plain-English 'Arbiter's notes' on approval cards are written
        ahead by a background thread (one LLM call per card). A card that
        has waited 15 minutes without them means that writer is failing."""
        from . import arbiter_notes
        try:
            waiting = arbiter_notes.pending(self.conn, limit=50)
        except sqlite3.OperationalError:
            return []
        old = [wi for wi in waiting if (self.conn.execute(
            "SELECT created_at FROM work_items WHERE id=?", (wi,)).fetchone()
            or {"created_at": now})["created_at"] < now - 15 * 60]
        if not old:
            return [_check("arbiter_notes", "Arbiter's notes", "ok",
                           "written for every waiting card")]
        return [_check("arbiter_notes", "Arbiter's notes", "warn",
                       f"{len(old)} card(s) without them",
                       "the cards show the arbiter's own shorthand instead; "
                       "check the recorder agent's provider on the Agents "
                       "page: " + ", ".join(old[:5]))]

    def _check_sim_stops(self, now):
        """Stop-losses / take-profits held by Claire's built-in sim (not
        at a broker): filled by the custodian against the live price. The
        custodian flags a lost one (RECONCILE) and a bare position
        (UNPROTECTED); this says how many there are and whether it checks."""
        try:
            (n,) = self.conn.execute(
                "SELECT COUNT(*) FROM orders o WHERE o.leg IS NOT NULL AND"
                " o.status IN ('placed','partially_filled') AND"
                " o.broker_order_id LIKE 'sim-%'").fetchone()
        except sqlite3.OperationalError:
            return []
        if not n:
            return [_check("sim_stops", "Stops held by Claire's sim", "info",
                           "none working")]
        h = self._health("custodian")
        age = now - h["checked_at"] if h else None
        if age is None or age > 15 * 60:
            return [_check("sim_stops", "Stops held by Claire's sim", "bad",
                           f"{n} working, NOT being checked",
                           "the custodian has not run for "
                           f"{'ever' if age is None else str(age // 60) + ' min'}"
                           " — nothing would sell at these stops; restart "
                           "claire-api")]
        return [_check("sim_stops", "Stops held by Claire's sim", "ok",
                       f"{n} working, checked {age // 60} min ago",
                       "checked against the live price every 5 minutes "
                       "while each market is open")]

    def _check_growth(self, now):
        out = []
        items = [
            ("desk_db", "desk.db (+WAL)", [self.var / "desk.db",
                                            self.var / "desk.db-wal"], 200),
            ("checkpoints_db", "checkpoints.db (+WAL)",
             [self.var / "checkpoints.db", self.var / "checkpoints.db-wal"],
             300),
            ("logs", "Logs (var/log)", [self.var / "log"], 100),
            ("narratives", "Narratives", [self.var / "narratives",
                                          self.var / "narratives-archive"],
             500),
            ("sdk", "AI agent transcripts", [self.sdk_dir], 1000),
            ("backups_size", "Backups", [self.var / "backups"], 500),
        ]
        for key, label, paths, warn_mb in items:
            n = sum(_size(p) for p in paths)
            st = "warn" if n > warn_mb * 1024 * 1024 else "info"
            out.append(_check(key, label, st, _mb(n),
                              f"warn above {warn_mb} MB"))
        dead = [p for p in (self.var / "api.log", self.var / "dash.log")
                if p.exists()]
        resets = sorted(self.var.glob("backup-*"))
        if dead or resets:
            out.append(_check(
                "leftovers", "Leftover files", "info",
                _mb(sum(_size(p) for p in dead + resets)),
                "Claire no longer writes or reads these (old logs, and copies "
                "made by bin/claire-reset); delete by hand if you don't need "
                "them: " +
                ", ".join(p.name for p in dead + resets)[:400]))
        return out

    # ── housekeeping ─────────────────────────────────────────────────────
    def rotate_logs(self) -> dict:
        """copytruncate: bin/claire opens the logs with >> (O_APPEND), so
        the running services keep writing to the same, now-empty file."""
        logdir, rotated, freed = self.var / "log", [], 0
        for log in sorted(logdir.glob("*.log")) if logdir.is_dir() else []:
            size = log.stat().st_size
            if size < LOG_ROTATE_BYTES:
                continue
            stamp = time.strftime("%Y%m%d-%H%M%S",
                                  time.localtime(self.clock()))
            dest = logdir / f"{log.name}.{stamp}.gz"
            with open(log, "rb") as src, gzip.open(dest, "wb") as gz:
                shutil.copyfileobj(src, gz)
            with open(log, "r+b") as f:
                f.truncate(0)
            freed += size - dest.stat().st_size
            rotated.append(log.name)
            olds = sorted(logdir.glob(f"{log.name}.*.gz"))
            for old in olds[:-LOG_KEEP]:
                freed += old.stat().st_size
                old.unlink()
        return {"action": "rotate_logs", "ok": True, "freed": max(0, freed),
                "detail": (f"rotated {', '.join(rotated)}" if rotated else
                           f"no log over {_mb(LOG_ROTATE_BYTES)}") +
                f"; keeping {LOG_KEEP} per log"}

    def backup(self) -> dict:
        bdir = self.var / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y%m%d", time.localtime(self.clock()))
        dest = bdir / f"desk-{day}.db"
        made = False
        if not dest.exists():
            tmp = bdir / f".desk-{day}.tmp"
            tmp.unlink(missing_ok=True)
            self.conn.execute("VACUUM INTO ?", (str(tmp),))
            tmp.replace(dest)
            made = True
        freed = 0
        for old in sorted(bdir.glob("desk-*.db"))[:-BACKUP_KEEP]:
            freed += old.stat().st_size
            old.unlink()
        return {"action": "backup", "ok": True, "freed": freed,
                "detail": (f"wrote {dest.name}" if made else
                           f"{dest.name} already exists") +
                f"; keeping the newest {BACKUP_KEEP}"}

    def prune_checkpoints(self) -> dict:
        """Graph state of runs that finished more than 30 days ago. A
        finished run can never resume, so its checkpoints are dead weight;
        anything still running or awaiting approval is never touched."""
        path = self.var / "checkpoints.db"
        if not path.exists():
            return {"action": "prune_checkpoints", "ok": True, "freed": 0,
                    "detail": "no checkpoints.db"}
        cutoff = int(self.clock()) - CHECKPOINT_DAYS * DAY
        threads = [r[0] for r in self.conn.execute(
            "SELECT thread_id FROM work_items WHERE thread_id IS NOT NULL"
            f" AND state IN ({','.join('?' * len(TERMINAL))})"
            " AND updated_at < ?", (*TERMINAL, cutoff))]
        before = _size(path)
        removed = 0
        if threads:
            ck = sqlite3.connect(str(path), timeout=30)
            try:
                for i in range(0, len(threads), 200):
                    chunk = threads[i:i + 200]
                    q = ",".join("?" * len(chunk))
                    with ck:
                        removed += ck.execute(
                            f"DELETE FROM checkpoints WHERE thread_id IN ({q})",
                            chunk).rowcount
                        ck.execute(f"DELETE FROM writes WHERE thread_id IN"
                                   f" ({q})", chunk)
                ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                ck.close()
        return {"action": "prune_checkpoints", "ok": True,
                "freed": max(0, before - _size(path)),
                "detail": f"{removed} checkpoints from {len(threads)} runs "
                          f"finished over {CHECKPOINT_DAYS} days ago "
                          "(space is reused inside the file)"}

    def prune_health(self) -> dict:
        cutoff = int(self.clock()) - HEALTH_DAYS * DAY
        n = self.conn.execute(
            "DELETE FROM service_health WHERE checked_at < ? AND rowid NOT IN"
            " (SELECT MAX(rowid) FROM service_health GROUP BY service)",
            (cutoff,)).rowcount
        return {"action": "prune_health", "ok": True, "freed": 0,
                "detail": f"{n} health rows older than {HEALTH_DAYS} days"}

    def prune_sim_orders(self) -> dict:
        """The built-in sim's memory (sim_orders): finished orders whose
        book row is finished too, after SIM_ORDERS_DAYS. The book keeps its
        own orders and executions for good."""
        cutoff = int(self.clock()) - SIM_ORDERS_DAYS * DAY
        try:
            rows = self.conn.execute(
                "SELECT id, book_json FROM sim_orders WHERE updated_at < ?",
                (cutoff,)).fetchall()
        except sqlite3.OperationalError:
            return {"action": "prune_sim_orders", "ok": True, "freed": 0,
                    "detail": "no built-in sim orders yet"}
        gone = []
        for r in rows:
            try:
                st = json.loads(r["book_json"]).get("status")
            except (TypeError, ValueError):
                continue
            if st not in ("filled", "cancelled"):
                continue                        # still resting: never
            live = self.conn.execute(
                "SELECT 1 FROM orders WHERE broker_order_id=? AND status IN"
                " ('pending_session','placed','partially_filled')",
                (r["id"],)).fetchone()
            if live is None:
                gone.append(r["id"])
        self.conn.executemany("DELETE FROM sim_orders WHERE id=?",
                              [(i,) for i in gone])
        return {"action": "prune_sim_orders", "ok": True, "freed": 0,
                "detail": f"{len(gone)} built-in sim orders finished over "
                          f"{SIM_ORDERS_DAYS} days ago"}

    def archive_narratives(self) -> dict:
        """Write-ups of runs archived over 30 days ago → one tar.gz each in
        var/narratives-archive; the Narratives reader still opens them."""
        base, arch = self.var / "narratives", self.var / "narratives-archive"
        cutoff = int(self.clock()) - NARRATIVE_DAYS * DAY
        rows = self.conn.execute(
            "SELECT id FROM work_items WHERE archived_at IS NOT NULL AND"
            " archived_at < ?", (cutoff,)).fetchall()
        done, freed = 0, 0
        for r in rows:
            d = base / r["id"]
            if not d.is_dir():
                continue
            arch.mkdir(parents=True, exist_ok=True)
            dest = arch / f"{r['id']}.tar.gz"
            tmp = arch / f".{r['id']}.tmp"
            size = _size(d)
            with tarfile.open(tmp, "w:gz") as tf:
                for f in sorted(d.glob("*.md")):
                    tf.add(f, arcname=f.name)
            with tarfile.open(tmp, "r:gz") as tf:       # verify before delete
                if sorted(tf.getnames()) != sorted(
                        f.name for f in d.glob("*.md")):
                    raise RuntimeError(f"archive check failed for {r['id']}")
            tmp.replace(dest)
            shutil.rmtree(d)
            freed += size - dest.stat().st_size
            done += 1
        return {"action": "archive_narratives", "ok": True,
                "freed": max(0, freed),
                "detail": f"archived {done} run write-ups (runs archived over "
                          f"{NARRATIVE_DAYS} days ago); still readable"}

    def prune_sdk_transcripts(self) -> dict:
        d = self.sdk_dir
        if not d.is_dir():
            return {"action": "prune_sdk_transcripts", "ok": True, "freed": 0,
                    "detail": "no transcript folder"}
        cutoff = self.clock() - SDK_TRANSCRIPT_DAYS * DAY
        n, freed = 0, 0
        for f in d.glob("*.jsonl"):
            st = f.stat()
            if st.st_mtime < cutoff:
                freed += st.st_size
                f.unlink()
                n += 1
        return {"action": "prune_sdk_transcripts", "ok": True, "freed": freed,
                "detail": f"{n} AI agent transcripts older than "
                          f"{SDK_TRANSCRIPT_DAYS} days"}

    def prune_runs(self) -> dict:
        n = self.conn.execute(
            "DELETE FROM maintenance_runs WHERE id NOT IN (SELECT id FROM"
            " maintenance_runs ORDER BY id DESC LIMIT ?)",
            (RUNS_KEEP - 1,)).rowcount
        return {"action": "prune_runs", "ok": True, "freed": 0,
                "detail": f"{n} old maintenance reports (keeping {RUNS_KEEP})"}


# ── the AI note ──────────────────────────────────────────────────────────
def make_summarize(conn, env=os.environ):
    """The `maintainer` agent writes the note from the facts. Read-only: it
    is given the results as text and returns text — no tools."""
    def summarize(facts: dict) -> str:
        from .providers import registry
        llm = registry.model_for(conn, "maintainer", env=env)
        brief = json.dumps(facts, default=str)[:12000]
        out = llm.invoke([
            ("system", registry.render_prompt(
                registry.agent_row(conn, "maintainer")["system_prompt"])),
            ("user", f"Today's maintenance results (JSON):\n{brief}")])
        text = out.content if isinstance(out.content, str) else str(out.content)
        return text.strip()[:1500]
    return summarize


def latest(conn, n=30) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM maintenance_runs ORDER BY id DESC LIMIT ?", (n,))]
    for r in rows:
        for k in ("checks_json", "actions_json"):
            try:
                r[k[:-5]] = json.loads(r.pop(k) or "[]")
            except (TypeError, ValueError):
                r[k[:-5]] = []
    return {"latest": rows[0] if rows else None,
            "history": [{k: r[k] for k in ("id", "started_at", "finished_at",
                                           "trigger", "status", "headline")}
                        | {"freed": sum(a.get("freed", 0)
                                        for a in r["actions"])}
                        for r in rows]}
