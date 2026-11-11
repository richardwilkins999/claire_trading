"""Dashboard-side approval authorization (DESIGN.md §10 steps 2-3).

The dashboards server is the ONLY component that converts a human click into
a resume — and only when the caller PRESENTS the token that was delivered in
the approval card. Localhost access alone approves nothing.
"""
import hmac
import json
import time

import httpx
import logging
from .logsetup import swallowed

log = logging.getLogger(__name__)


# A resume claims the token ('resuming') for as long as the graph runs. A
# claim older than this belongs to a process that died mid-resume and may be
# taken over; execution is seconds, so ten minutes is generous.
RESUMING_STALE_S = 600


class AuthError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def default_resume_post(api_base, secret):
    def post(body):
        r = httpx.post(f"{api_base}/internal/resume", json=body,
                       headers={"x-claire-secret": secret}, timeout=30)
        return r.status_code, (r.json() if r.headers.get("content-type", "")
                               .startswith("application/json") else {})
    return post


def thesis_action(conn, payload: dict, resume_post, *, clock=time.time):
    """Authorize (token presented ∧ unexpired ∧ unused ∧ awaiting_approval),
    mark pending_resume, POST the resume, burn on ack. Raises AuthError with
    the HTTP-ish code otherwise."""
    wi = payload.get("work_item_id")
    token = payload.get("token") or ""
    action = payload.get("action")
    if action not in ("approve", "reject"):
        raise AuthError(400, "action must be approve|reject")
    if not token:
        raise AuthError(403, "token required — it is on the approval card")
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
    if row is None:
        raise AuthError(404, "unknown work item")
    if not row["approval_token"] or not hmac.compare_digest(
            token.encode(), row["approval_token"].encode()):   # constant-time
        raise AuthError(403, "token mismatch")
    if row["token_state"] == "burned":
        raise AuthError(409, "token already used")
    if row["state"] != "awaiting_approval":
        raise AuthError(409, f"item is {row['state']}")
    now = int(clock())
    if row["expires_at"] and now >= row["expires_at"]:
        raise AuthError(410, "approval expired")
    if action == "approve" and not payload.get("broker"):
        raise AuthError(400, "approve needs a broker")
    if action == "approve" and payload.get("broker") == "saxo_live":
        # REAL MONEY (DESIGN.md §22): refuse unless the desk is armed, and
        # make the human say so explicitly on this very approval — choosing
        # the account from a dropdown is not enough
        import os

        from . import live_trading
        st = live_trading.status(conn, os.environ)
        if not st["armed"]:
            raise AuthError(409, f"Saxo LIVE is not available: {st['summary']}")
        if payload.get("confirm_live") != "LIVE":
            raise AuthError(400, "a live-money approval must be confirmed by "
                                 "typing LIVE on the approval card")
    if action == "approve" and not (payload.get("size_base") or
                                    payload.get("qty")):
        raise AuthError(400, "approve needs size_base (buy) or qty (sell) —"
                             " a positive quantity is mandatory")
    if action == "approve" and payload.get("size_base"):
        acct = conn.execute("SELECT id, base_currency FROM broker_accounts"
                            " WHERE broker=? LIMIT 1",
                            (payload["broker"],)).fetchone()
        if acct is None:
            raise AuthError(400, f"no account for broker "
                                 f"{payload['broker']!r}")
        # what is FREE to spend: cash already promised to open buy orders is
        # not, and approving against it only fails later at the fill
        from .accounting.repo import Repo
        bal = Repo(conn).available_cash(acct["id"])
        if float(payload["size_base"]) > bal / 1e6:
            raise AuthError(400, f"size {payload['size_base']:.2f} exceeds "
                                 f"{acct['id']} cash "
                                 f"{bal / 1e6:.2f} {acct['base_currency']} — "
                                 "top up on the Portfolio page or reduce size")

    levels = _card_levels(row, payload) if action == "approve" else None
    order_type = payload.get("order_type") or "limit"
    if order_type not in ("limit", "market"):
        raise AuthError(400, "order_type must be limit or market")

    # authorize: mark pending_resume (NOT burned) + audit row. Conditional on
    # the state the checks above read, so an item that was resumed, burned or
    # expired in between is refused rather than re-authorized. A re-click on
    # a pending_resume token (claire-api was down) is allowed; the single-use
    # claim that actually matters is taken atomically by /internal/resume.
    cur = conn.execute(
        "UPDATE work_items SET token_state='pending_resume', updated_at=?"
        " WHERE id=? AND approval_token=? AND state='awaiting_approval'"
        " AND token_state IN ('minted','pending_resume')",
        (now, wi, row["approval_token"]))
    if cur.rowcount != 1:
        raise AuthError(409, "token already used or being used")
    conn.execute(
        "INSERT INTO events (item_id, ts, actor, from_state, to_state, payload)"
        " VALUES (?,?,?,?,?,?)",
        (wi, now, "human", "awaiting_approval", f"authorize:{action}",
         json.dumps({**{k: payload.get(k) for k in
                       ("size_base", "qty", "trail_pct", "broker",
                        "confirm_live")}, "levels": levels,
                     "order_type": order_type})))
    if levels:
        th = {**json.loads(row["thesis_json"] or "{}"), **levels}
        conn.execute("UPDATE work_items SET thesis_json=? WHERE id=?",
                     (json.dumps(th), wi))

    body = {"work_item_id": wi,
            "status": "approved" if action == "approve" else "rejected",
            "token": token, "actor": "human",
            "size_base": payload.get("size_base"), "qty": payload.get("qty"),
            "trail_pct": payload.get("trail_pct"),
            "broker": payload.get("broker"), "levels": levels,
            "order_type": order_type}
    try:
        code, resp = resume_post(body)      # idempotent server-side; retryable
    except Exception as e:                  # noqa: BLE001 — claire-api down
        raise AuthError(502, f"claire-api unreachable ({e}); approval saved, "
                             "will retry") from e
    if code == 200:
        return {"ok": True, **resp}
    # resume refused or claire-api down: token stays pending_resume so a retry
    # (human or custodian) can complete it; surface the reason
    raise AuthError(code if code >= 400 else 502,
                    resp.get("detail", f"resume failed ({code})"))


def retry_pending(conn, resume_post, *, clock=time.time):
    """Crash recovery: re-POST resumes that were authorized but never acked.
    Called by the custodian; idempotent because /internal/resume is."""
    done = []
    now = int(clock())
    # 'resuming' past the stale window = the resuming process died mid-run;
    # without picking those up the approval would be stuck for good
    for row in conn.execute(
            "SELECT id, approval_token FROM work_items"
            " WHERE token_state='pending_resume'"
            " OR (token_state='resuming' AND updated_at < ?)",
            (now - RESUMING_STALE_S,)).fetchall():
        if last_event(conn, row["id"]) == "resume_failed" and \
                not is_transient(last_error(conn, row["id"])):
            # the trade WAS attempted and REFUSED (exchange rejection,
            # sizing, price band, cash, a live limit). Re-sending the same
            # thing is the human's decision, made on the Approvals page
            # (retry_failed) — never an automatic loop. A transient failure
            # (venue/network down) is still retried here until it expires.
            continue
        events = conn.execute(
            "SELECT payload, to_state FROM events WHERE item_id=?"
            " AND to_state LIKE 'authorize:%' ORDER BY id DESC LIMIT 1",
            (row["id"],)).fetchone()
        if not events:
            continue
        detail = json.loads(events["payload"] or "{}")
        action = events["to_state"].split(":", 1)[1]
        try:
            code, _ = resume_post({
                "work_item_id": row["id"],
                "status": "approved" if action == "approve" else "rejected",
                "token": row["approval_token"], "actor": "human", **detail})
        except Exception as e:              # noqa: BLE001 — api down: next run
            swallowed(log, "retry of an authorised approval could not reach claire-api",
                      e)
            continue
        if code == 200:
            done.append(row["id"])
    return done


# ── approved, attempted, refused: the human decides (Approvals page) ─────
# Only failures where the order demonstrably never reached a decision are
# transient. Anything else — including an error we do not recognise — is
# treated as a refusal and waits for the human: guessing "transient" wrong
# would mean re-sending an order the venue already turned down.
TRANSIENT_MARKERS = ("venue down", "unreachable", "timed out", "timeout",
                     "connection", "temporarily", "http 502", "http 503",
                     "http 504", "service unavailable")


def is_transient(error) -> bool:
    e = (error or "").lower()
    return any(m in e for m in TRANSIENT_MARKERS)


def last_error(conn, wi):
    r = conn.execute("SELECT payload FROM events WHERE item_id=? AND"
                     " to_state='resume_failed' ORDER BY id DESC LIMIT 1",
                     (wi,)).fetchone()
    return json.loads(r["payload"] or "{}").get("error") if r else None


def last_event(conn, wi):
    r = conn.execute("SELECT to_state FROM events WHERE item_id=?"
                     " ORDER BY id DESC LIMIT 1", (wi,)).fetchone()
    return r["to_state"] if r else None


def failed_approvals(conn, *, ttl_s=3600, clock=time.time):
    """Approved trades whose execution was attempted and refused, still
    retryable: what failed, what was approved, and how long until the
    custodian's 60-minute rule expires the approval."""
    now = int(clock())
    out = []
    for w in conn.execute(
            "SELECT * FROM work_items WHERE state='approved'"
            " AND token_state='pending_resume'").fetchall():
        if last_event(conn, w["id"]) != "resume_failed":
            continue
        err = conn.execute(
            "SELECT ts, payload FROM events WHERE item_id=? AND"
            " to_state='resume_failed' ORDER BY id DESC LIMIT 1",
            (w["id"],)).fetchone()
        auth = conn.execute(
            "SELECT payload FROM events WHERE item_id=? AND"
            " to_state='authorize:approve' ORDER BY id DESC LIMIT 1",
            (w["id"],)).fetchone()
        approved_at = conn.execute(
            "SELECT MIN(ts) FROM events WHERE item_id=? AND"
            " to_state='approved'", (w["id"],)).fetchone()[0] or w["updated_at"]
        attempts = conn.execute(
            "SELECT COUNT(*) FROM events WHERE item_id=? AND"
            " to_state='resume_failed'", (w["id"],)).fetchone()[0]
        out.append({
            "id": w["id"], "ticker": w["ticker"], "kind": w["kind"],
            "thesis": json.loads(w["thesis_json"] or "{}"),
            "approval": json.loads((auth and auth["payload"]) or "{}"),
            "error": json.loads(err["payload"] or "{}").get("error"),
            # transient: the custodian keeps retrying it; refused: waits
            "auto_retry": is_transient(
                json.loads(err["payload"] or "{}").get("error")),
            "failed_at": err["ts"], "attempts": attempts,
            "expires_at": approved_at + ttl_s,
            "expires_in_s": max(approved_at + ttl_s - now, 0)})
    return out


def _failed_row(conn, wi):
    row = conn.execute("SELECT * FROM work_items WHERE id=?", (wi,)).fetchone()
    if row is None:
        raise AuthError(404, "unknown work item")
    if row["state"] != "approved" or row["token_state"] != "pending_resume" \
            or last_event(conn, wi) != "resume_failed":
        raise AuthError(409, f"{wi} is not a failed approval (it is "
                             f"{row['state']}/{row['token_state']})")
    return row


SAXO_MAX_PCT = 25          # Saxo refuses a stop/target further from entry


def _card_levels(row, payload):
    """The limit price / stop / target you set on the approval card,
    checked BEFORE the trade resumes: the same rules as any plan (stop below
    a buy's entry, target above it), a buy must keep a stop, and Saxo's 25%
    limit. `entry` (a manual limit price) replaces the plan's entry range
    with that one price. Returns the changed keys of {entry_low, entry_high,
    stop_loss, take_profit}, or None when nothing differs from the plan."""
    lv = payload.get("levels")
    if lv is None:
        return None
    if not isinstance(lv, dict):
        raise AuthError(400, "levels must be an object")
    th = json.loads(row["thesis_json"] or "{}")
    direction = th.get("direction")
    if direction not in ("buy", "sell"):
        return None
    new = {}
    if lv.get("entry") not in (None, ""):
        try:
            e = float(lv["entry"])
        except (TypeError, ValueError):
            raise AuthError(400, "the limit price must be a number") from None
        if not e > 0:
            raise AuthError(400, "the limit price must be more than zero")
        new["entry_low"] = new["entry_high"] = e
    for k in ("stop_loss", "take_profit"):
        if k not in lv:
            continue
        v = lv[k]
        if v in (None, ""):
            new[k] = None
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise AuthError(400, f"{k} must be a number") from None
        if not v > 0:
            raise AuthError(400, f"{k} must be more than zero")
        new[k] = v
    if all(th.get(k) == v for k, v in new.items()):
        return None                                  # unchanged: the plan
    data = {**th, **new}
    entry = data.get("entry_high") or data.get("entry_low")
    stop, target = data.get("stop_loss"), data.get("take_profit")
    if direction == "buy" and not stop:
        raise AuthError(400, "a buy needs a stop below the entry")
    if entry:
        lo = direction == "buy"
        if stop and (stop >= entry if lo else stop <= entry):
            raise AuthError(400, f"the stop must be {'below' if lo else 'above'}"
                                 f" the entry ({entry:g})")
        if target and (target <= entry if lo else target >= entry):
            raise AuthError(400, f"the target must be "
                                 f"{'above' if lo else 'below'} the entry "
                                 f"({entry:g})")
        if (payload.get("broker") or "").startswith("saxo"):
            for name, v in (("stop", stop), ("target", target)):
                if v and abs(v - entry) / entry * 100 > SAXO_MAX_PCT:
                    raise AuthError(400, f"Saxo refuses a {name} more than "
                                         f"{SAXO_MAX_PCT}% from the entry "
                                         f"({name} {v:g}, entry {entry:g})")
    from .graph.state import Thesis
    try:
        Thesis.model_validate(data)
    except Exception as e:                           # noqa: BLE001 — shown
        raise AuthError(400, f"those levels are not valid: {str(e)[:200]}") \
            from None
    return new


def retry_failed(conn, wi, resume_post, *, confirm_live=None, changes=None):
    """Re-send a refused trade. By default with the SAME approval the human
    gave (size, account, trailing floor); `changes` may replace any of those
    — the human editing the retry on the Approvals page. (Price levels are
    edited separately, via desk.amend, before this is called.) Execution
    re-checks everything (live price band, cash, limits, the live switch)
    as if new."""
    row = _failed_row(conn, wi)
    auth = conn.execute(
        "SELECT payload FROM events WHERE item_id=? AND"
        " to_state='authorize:approve' ORDER BY id DESC LIMIT 1",
        (wi,)).fetchone()
    detail = json.loads((auth and auth["payload"]) or "{}")
    for k, v in (changes or {}).items():
        if k in ("size_base", "qty", "trail_pct", "broker",
                 "order_type") and v is not None:
            detail[k] = v
    if detail.get("broker") == "saxo_live":
        import os

        from . import live_trading
        if not live_trading.status(conn, os.environ)["armed"]:
            raise AuthError(409, "Saxo LIVE is not armed")
        if confirm_live != "LIVE":
            raise AuthError(400, "retrying a live-money trade must be "
                                 "confirmed by typing LIVE")
    if detail.get("broker") and not conn.execute(
            "SELECT 1 FROM broker_accounts WHERE broker=?",
            (detail["broker"],)).fetchone():
        raise AuthError(400, f"no account for broker {detail['broker']!r}")
    body = {"work_item_id": wi, "status": "approved",
            "token": row["approval_token"], "actor": "human",
            **{k: detail.get(k) for k in ("size_base", "qty", "trail_pct",
                                          "broker")},
            **({"order_type": detail["order_type"]}
               if detail.get("order_type") else {})}
    try:
        code, resp = resume_post(body)
    except Exception as e:                  # noqa: BLE001
        raise AuthError(502, f"claire-api unreachable ({e})") from e
    if code == 200:
        return {"ok": True, **resp}
    raise AuthError(code if code >= 400 else 502,
                    resp.get("detail", f"retry failed ({code})"))


def cancel_failed(conn, wi, *, clock=time.time):
    """Give up on a refused trade: rejected, token burned. The PASS it came
    from (for an override) can then be overridden again with new levels."""
    _failed_row(conn, wi)
    now = int(clock())
    cur = conn.execute(
        "UPDATE work_items SET state='rejected', token_state='burned',"
        " updated_at=? WHERE id=? AND state='approved'"
        " AND token_state='pending_resume'", (now, wi))
    if cur.rowcount != 1:
        raise AuthError(409, "the trade changed state — refresh")
    conn.execute(
        "INSERT INTO events (item_id, ts, actor, from_state, to_state,"
        " payload) VALUES (?,?,?,?,?,?)",
        (wi, now, "human", "approved", "rejected",
         json.dumps({"reason": "cancelled after a failed placement"})))
    return {"ok": True, "state": "rejected"}
