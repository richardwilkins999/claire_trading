"""READ-ONLY views of Richard's two Saxo accounts, and the OAuth sign-ins
that every Saxo integration borrows its login from.

    sim   paper account SAXO_SIM_ACCOUNT_ID  (Saxo's simulation environment)
    live  real account  SAXO_LIVE_ACCOUNT_ID  (real money)

Each environment is fully separate: its own OAuth app (SAXO_SIM_APP_KEY /
SAXO_LIVE_APP_KEY), its own sign-in file (etc/saxo_<env>_token.json), its own
keepalive and alerts. A SIM token can never be used against the live account
or the reverse. `bin/saxo login sim|live` signs in.

What this module can do is read, and that is enforced in code:
- `_get` is the only verb, and only to the fixed READ_PATHS (portfolio reads;
  nothing under /trade/). Placing orders lives in saxo_broker.py, behind the
  live-trading switch (DESIGN.md §22); it only borrows `access_token` here.

Saxo access tokens live ~20 min and refresh windows 40-60 min, and every
refresh ROTATES the refresh token. So the token file is guarded by an
exclusive lock (two processes refreshing at once would invalidate each
other), and claire-api runs a keepalive that refreshes well inside the
window and alerts Richard (Telegram) if a sign-in is about to lapse.
Token files live in etc/ (0600), out of reach of the sandbox and agents.
"""
import base64
import fcntl
import functools
import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
import logging
from ..logsetup import swallowed

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
ENVS = ("sim", "live")
DEFAULT_REDIRECT = "http://localhost/claire-saxo"
# The environment used when a caller does not name one. Kept for the older
# single-sign-in config (SAXO_APP_ENV + SAXO_APP_KEY); new config names each.
DEFAULT_ENV = (os.environ.get("SAXO_APP_ENV") or "live").strip().lower()
if DEFAULT_ENV not in ENVS:
    raise ValueError(f"SAXO_APP_ENV must be sim or live, not {DEFAULT_ENV!r}")
APP_ENV = DEFAULT_ENV                           # backward-compatible name
LABELS = {"sim": "Saxo SIM (paper)", "live": "Saxo LIVE (real money)"}
REFRESH_MARGIN = 300            # refresh when < 5 min of access token remain
KEEPALIVE_EVERY = 600           # well inside a 40-60 min refresh window


def token_file(which):
    return ROOT / "etc" / f"saxo_{which}_token.json"


def auth_base(which):
    return f"https://{which}.logonvalidation.net"


def api_base(which):
    return "https://gateway.saxobank.com/" + (
        "sim/openapi" if which == "sim" else "openapi")


TOKEN_FILE = token_file(DEFAULT_ENV)
AUTH_BASE = auth_base(DEFAULT_ENV)
API_BASE = api_base(DEFAULT_ENV)

# The ONLY endpoints this module will call. Reads of the client's own
# portfolio; nothing under /trade/ — there is no way to place, amend or
# cancel an order from here.
READ_PATHS = {
    "me": "/port/v1/users/me",
    "accounts": "/port/v1/accounts/me",
    "balances": "/port/v1/balances/me",
    "positions": "/port/v1/netpositions/me?FieldGroups="
                 "NetPositionBase,NetPositionView,DisplayAndFormat&$top=500",
    "orders": "/port/v1/orders/me?FieldGroups=DisplayAndFormat&$top=200",
    # one row per opening fill — the first purchase price and date
    "openings": "/port/v1/positions/me?FieldGroups=PositionBase&$top=500",
}


class SaxoError(Exception):
    pass


class NotSignedIn(SaxoError):
    pass


def _which(which):
    w = (which or DEFAULT_ENV).strip().lower()
    if w not in ENVS:
        raise SaxoError(f"unknown Saxo environment {which!r} (sim or live)")
    return w


def account_id(which, env=os.environ):
    """The Saxo AccountId this environment is for (labels, setup)."""
    return (env.get(f"SAXO_{_which(which).upper()}_ACCOUNT_ID") or "").strip()


# ── PKCE / sign-in ───────────────────────────────────────────────────────
def app_config(env=os.environ, which=None):
    """The OAuth app for one environment, from etc/claire.env:
    SAXO_SIM_APP_KEY / SAXO_LIVE_APP_KEY (+ _URL, _SECRET). The older
    single-app names (SAXO_APP_* for the env named by SAXO_APP_ENV) are
    still read."""
    w = _which(which)
    legacy = (env.get("SAXO_APP_ENV") or "live").strip().lower() == w

    def pick(name):
        v = env.get(f"SAXO_{w.upper()}_APP_{name}")
        if not v and legacy:
            v = env.get(f"SAXO_APP_{name}")
        return (v or "").strip()
    key = pick("KEY")
    if not key:
        raise NotSignedIn(f"SAXO_{w.upper()}_APP_KEY is not set in "
                          "etc/claire.env — register the app on "
                          "developer.saxo first")
    return {"which": w, "app_key": key,
            "redirect_uri": pick("URL") or DEFAULT_REDIRECT,
            "secret": pick("SECRET") or None, "auth_base": auth_base(w)}


def new_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(
        b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cfg, redirect_uri, challenge, state):
    return (cfg.get("auth_base") or AUTH_BASE) + "/authorize?" + urlencode({
        "response_type": "code", "client_id": cfg["app_key"],
        "redirect_uri": redirect_uri, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state})


@functools.cache
def _shared_client() -> httpx.Client:
    """One pooled client for the process; a Client per call was never
    closed and leaked its socket (see tradingview._shared_client)."""
    return httpx.Client(timeout=20)


def _token_request(cfg, data, client=None):
    data = {**data, "client_id": cfg["app_key"]}
    auth = (cfg["app_key"], cfg["secret"]) if cfg.get("secret") else None
    c = client or _shared_client()
    r = c.post((cfg.get("auth_base") or AUTH_BASE) + "/token", data=data,
               auth=auth)
    if r.status_code != 200 and r.status_code != 201:
        raise NotSignedIn(f"Saxo token endpoint said HTTP {r.status_code}: "
                          f"{r.text[:200]} — run bin/saxo login "
                          f"{cfg.get('which', '')} again")
    return r.json()


def _stamp(tok, verifier, redirect_uri, now):
    return {"access_token": tok["access_token"],
            "refresh_token": tok["refresh_token"],
            "access_expires_at": now + int(tok.get("expires_in", 1200)),
            "refresh_expires_at": now + int(
                tok.get("refresh_token_expires_in", 2400)),
            "code_verifier": verifier, "redirect_uri": redirect_uri}


def exchange_code(cfg, code, verifier, redirect_uri, *, client=None,
                  clock=time.time, path=None):
    path = path or token_file(cfg.get("which") or DEFAULT_ENV)
    tok = _token_request(cfg, {"grant_type": "authorization_code",
                               "code": code, "redirect_uri": redirect_uri,
                               "code_verifier": verifier}, client)
    with _locked(path) as f:
        _write(f, path, _stamp(tok, verifier, redirect_uri, int(clock())))


# ── sign-in flow, shared by bin/saxo and the dashboard's Sign-in button ──
LOGIN_TTL = 600                 # a started sign-in must finish within 10 min
_PENDING = {}                   # state -> sign-in in progress (this process)


def begin_login(which, redirect_uri, *, env=os.environ, clock=time.time):
    """Start a PKCE sign-in; returns the Saxo URL to send the browser to.
    The verifier never leaves this process, and the random `state` is the
    only key that can complete it — a callback this process did not start
    (login CSRF) is refused."""
    cfg = app_config(env, which)
    now = int(clock())
    for s in [s for s, p in _PENDING.items() if p["expires"] < now]:
        _PENDING.pop(s, None)
    verifier, challenge = new_pkce()
    state = secrets.token_urlsafe(24)
    _PENDING[state] = {"which": cfg["which"], "cfg": cfg, "verifier": verifier,
                       "redirect": redirect_uri, "expires": now + LOGIN_TTL}
    return authorize_url(cfg, redirect_uri, challenge, state)


def complete_login(state, code, *, env=os.environ, client=None,
                   clock=time.time):
    """Finish a sign-in started by begin_login. The new tokens are tried on
    a scratch file first and installed only if Saxo says they belong to the
    expected account (SAXO_<ENV>_ACCOUNT_ID) — signing in with the wrong
    Saxo login can never silently repoint the desk at another account, and
    a failed sign-in never destroys a working one. Returns the env."""
    p = _PENDING.pop(state or "", None)
    if p is None or p["expires"] < int(clock()):
        raise SaxoError("this sign-in link has expired or was not started "
                        "here — start the sign-in again")
    w = p["which"]
    real = token_file(w)
    tmp = real.with_name(real.stem + ".pending.json")
    try:
        exchange_code(p["cfg"], code, p["verifier"], p["redirect"],
                      client=client, clock=clock, path=tmp)
        want = account_id(w, env)
        if want:
            accts = _get("accounts", w, env=env, client=client, clock=clock,
                         path=tmp).get("Data", [])
            got = {str(a.get("AccountId")) for a in accts}
            if want not in got:
                raise SaxoError(
                    f"that Saxo login holds account(s) "
                    f"{', '.join(sorted(got)) or 'none'}, but the {w} account "
                    f"here is {want} — sign in with the {w} login instead")
        with _locked(real):
            os.replace(tmp, real)
    finally:
        tmp.unlink(missing_ok=True)
        tmp.with_suffix(".lock").unlink(missing_ok=True)
    return w


# ── token store (locked; rotation-safe) ──────────────────────────────────
@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path=None):
    path = path or TOKEN_FILE
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        which = path.stem.split("_")[1] if path.stem.count("_") >= 2 else ""
        raise NotSignedIn(f"not signed in to Saxo {which} — run "
                          f"bin/saxo login {which}".rstrip())


def _write(_, path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def access_token(which=None, *, env=os.environ, client=None, clock=time.time,
                 path=None, force=False):
    """A valid access token for one environment, refreshing (under the lock)
    when close to expiry. The re-read inside the lock matters: another
    process may have just rotated the refresh token."""
    w = _which(which)
    path = path or token_file(w)
    tok = _read(path)
    now = int(clock())
    if not force and tok["access_expires_at"] - now > REFRESH_MARGIN:
        return tok["access_token"]
    with _locked(path):
        tok = _read(path)
        now = int(clock())
        if not force and tok["access_expires_at"] - now > REFRESH_MARGIN:
            return tok["access_token"]
        if tok["refresh_expires_at"] <= now:
            raise NotSignedIn(f"Saxo {w} sign-in expired (the desk was down "
                              "longer than Saxo's refresh window) — run "
                              f"bin/saxo login {w}")
        cfg = app_config(env, w)
        new = _token_request(cfg, {"grant_type": "refresh_token",
                                   "refresh_token": tok["refresh_token"],
                                   "redirect_uri": tok["redirect_uri"],
                                   "code_verifier": tok["code_verifier"]},
                             client)
        _write(None, path, _stamp(new, tok["code_verifier"],
                                  tok["redirect_uri"], now))
        return new["access_token"]


def token_source(which):
    """A () -> token callable bound to ONE environment, for adapters."""
    w = _which(which)

    def token():
        return access_token(w)
    token.which = w
    return token


def status(which=None, *, clock=time.time, path=None):
    w = _which(which)
    try:
        tok = _read(path or token_file(w))
    except NotSignedIn as e:
        return {"env": w, "signed_in": False, "detail": str(e)}
    now = int(clock())
    return {"env": w, "signed_in": tok["refresh_expires_at"] > now,
            "access_valid_s": max(tok["access_expires_at"] - now, 0),
            "refresh_valid_s": max(tok["refresh_expires_at"] - now, 0)}


# ── reads ────────────────────────────────────────────────────────────────
def _get(what, which=None, *, env=os.environ, client=None, clock=time.time,
         path=None):
    if what not in READ_PATHS:                 # nothing else is reachable
        raise SaxoError(f"{what!r} is not a read-only Saxo endpoint")
    w = _which(which)
    token = access_token(w, env=env, client=client, clock=clock, path=path)
    c = client or _shared_client()
    r = c.get(api_base(w) + READ_PATHS[what],
              headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise NotSignedIn(f"Saxo rejected the {w} token — run "
                          f"bin/saxo login {w}")
    if r.status_code >= 400:
        raise SaxoError(f"Saxo {what}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def _price_from_pl(view, amount):
    """Saxo sends CurrentPrice 0 (CurrentPriceType "None") where the account
    has no market data for that exchange — every position on the SIM
    account, 2026-11-10. A zero price is NO price: it made every position
    read "-100%". Saxo still values the position (ProfitLossOnTrade), so the
    price it used is recovered from that: avg + P/L ÷ amount (a short's
    negative amount gives the right sign too). Market value and exposure
    follow; the base-currency rate is Saxo's own (P/L base ÷ P/L)."""
    if view.get("CurrentPrice") or not amount:
        return view
    avg, pl = view.get("AverageOpenPrice"), view.get("ProfitLossOnTrade")
    if not avg or pl is None:
        return view
    cur = avg + pl / amount
    if cur <= 0:
        return view
    v = dict(view, CurrentPrice=round(cur, 6), _price_src="Saxo's P/L")
    if not v.get("MarketValue"):
        v["MarketValue"] = round(amount * cur, 2)
    plb = view.get("ProfitLossOnTradeInBaseCurrency")
    if not view.get("ExposureInBaseCurrency") and pl and plb is not None:
        v["ExposureInBaseCurrency"] = round(abs(amount * cur * plb / pl), 2)
    return v


def _pct(cur, avg, amount):
    try:
        if not avg or not cur:              # no price is not a -100% loss
            return None
        sign = 1 if (amount or 0) >= 0 else -1
        return round((cur - avg) / avg * 100 * sign, 2)
    except TypeError:
        return None


def positions(which=None, **kw):
    """Open net positions, flattened to what a person reads."""
    out = []
    for p in _get("positions", which, **kw).get("Data", []):
        base, view = p.get("NetPositionBase", {}), p.get("NetPositionView", {})
        disp = p.get("DisplayAndFormat", {})
        view = _price_from_pl(view, base.get("Amount"))
        out.append({
            "symbol": disp.get("Symbol"), "name": disp.get("Description"),
            "asset_type": base.get("AssetType"),
            "currency": disp.get("Currency"),
            "amount": base.get("Amount"),
            "avg_open_price": view.get("AverageOpenPrice"),
            "current_price": view.get("CurrentPrice"),
            "market_value": view.get("MarketValue"),
            "pnl": view.get("ProfitLossOnTrade"),
            "pnl_base": view.get("ProfitLossOnTradeInBaseCurrency"),
            "pnl_pct": _pct(view.get("CurrentPrice"),
                            view.get("AverageOpenPrice"), base.get("Amount")),
            "exposure_base": view.get("ExposureInBaseCurrency"),
            # set when Saxo sent no price and it was worked out from P/L
            "price_src": view.get("_price_src"),
            "_net_id": p.get("NetPositionId"),
        })
    try:
        _add_openings(out, _get("openings", which, **kw).get("Data", []))
    except Exception as e:                  # noqa: BLE001 — extra detail only
        swallowed(log, "Saxo position-opening detail unavailable",
                  e, every=600)
        pass
    for r in out:
        r.pop("_net_id", None)
    return sorted(out, key=lambda r: -abs(r.get("exposure_base") or 0))


def _add_openings(rows, openings):
    """Per net position: the FIRST purchase (price, time), how many fills
    opened it, and what was paid in total (sum of amount x open price)."""
    by_net = {}
    for o in openings:
        b = o.get("PositionBase", {})
        if b.get("OpenPrice") is None:
            continue
        by_net.setdefault(o.get("NetPositionId"), []).append(b)
    for r in rows:
        opens = sorted(by_net.get(r.get("_net_id"), []),
                       key=lambda b: b.get("ExecutionTimeOpen") or "")
        if not opens:
            continue
        r["first_open_price"] = opens[0]["OpenPrice"]
        r["first_open_time"] = opens[0].get("ExecutionTimeOpen")
        r["open_fills"] = len(opens)
        r["cost_basis"] = round(sum(abs(b.get("Amount") or 0) * b["OpenPrice"]
                                    for b in opens), 2)


def orders(which=None, **kw):
    """Working orders at Saxo (incl. the desk's stop-loss/take-profit legs)."""
    out = []
    for o in _get("orders", which, **kw).get("Data", []):
        disp = o.get("DisplayAndFormat", {})
        out.append({
            "order_id": o.get("OrderId"), "symbol": disp.get("Symbol"),
            "name": disp.get("Description"), "currency": disp.get("Currency"),
            "side": o.get("BuySell"), "amount": o.get("Amount"),
            "type": o.get("OpenOrderType") or o.get("OrderType"),
            "price": o.get("Price"), "status": o.get("Status"),
            "duration": (o.get("Duration") or {}).get("DurationType"),
            "placed": o.get("OrderTime"),
            "distance_to_market": o.get("DistanceToMarket"),
        })
    return out


def balance(which=None, **kw):
    b = _get("balances", which, **kw)
    keep = ("Currency", "CashBalance", "TotalValue", "MarginAvailableForTrading",
            "UnrealizedPositionsValue", "NonMarginPositionsValue",
            "UnrealizedMarginProfitLoss", "OpenPositionsCount")
    return {k: b.get(k) for k in keep if k in b}


def summary(which=None, *, with_orders=False, **kw):
    """One call for Claire / the CLI / the dashboard: cash + positions (and
    optionally working orders) for one environment, or an honest reason."""
    w = _which(which)
    env = kw.get("env", os.environ)
    head = {"env": w, "account": LABELS[w] + " — read-only view",
            "account_id": account_id(w, env) or None}
    try:
        out = {**head, "balance": balance(w, **kw),
               "positions": positions(w, **kw)}
        if with_orders:
            out["orders"] = orders(w, **kw)
        return out
    except SaxoError as e:
        return {**head, "error": str(e)}


# ── keepalive + sign-in alerts (claire-api) ──────────────────────────────
WARN_BELOW = 15 * 60            # refresh failing with < 15 min of window left
RETRY_EVERY = 60                # after a failed refresh, try again soon
REMIND_EVERY = 12 * 3600        # while signed out, remind twice a day


def alert_state_file(which):
    return ROOT / "var" / f"saxo_{which}_signin_alert.json"


ALERT_STATE = alert_state_file(DEFAULT_ENV)


class SignInWatch:
    """Turns one environment's keepalive outcome into Telegram alerts, once
    per change:

      ok ──(refresh failing, window closing)──▶ warning ──▶ lapsed
       ▲                                                      │
       └──────────(bin/saxo login)── "signed in again" ◀──────┘

    Lapsed re-reminds every 12 h until fixed. Silent for an environment that
    was never signed in (no token file). State is persisted so a restart
    neither forgets a lapse nor repeats the alert. If the PC itself is off
    or asleep no alert can be sent; the lapse is reported when it is back."""

    def __init__(self, which=None, *, notify=None, clock=time.time,
                 state_path=None, token_path=None):
        if notify is None:
            from .. import alerts
            notify = alerts.send
        self.which = _which(which)
        self.notify, self.clock = notify, clock
        self.state_path = state_path or alert_state_file(self.which)
        self.token_path = token_path or token_file(self.which)

    def _load(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"state": "ok", "alerted_at": 0}

    def _save(self, st):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st))

    def _say(self, st, new_state, text):
        self.notify(text, title=f"Claire · {LABELS[self.which]}")
        st.update(state=new_state, alerted_at=int(self.clock()))

    def observe(self, *, refreshed, error=None):
        """Call after each keepalive attempt. Returns the state."""
        st = self._load()
        if not self.token_path.exists():
            return "not_set_up"
        now = int(self.clock())
        s = status(self.which, clock=self.clock, path=self.token_path)
        rejected = isinstance(error, NotSignedIn)
        w = self.which
        if refreshed:
            if st["state"] in ("warning", "lapsed"):
                self._say(st, "ok", f"signed in to Saxo {w} again — it "
                          "works normally.")
            st["state"] = "ok"
        elif not s["signed_in"] or rejected:
            if st["state"] != "lapsed" or \
                    now - st.get("alerted_at", 0) >= REMIND_EVERY:
                what = ("Live orders are refused and the live view is blank"
                        if w == "live" else "Saxo SIM orders are refused "
                        "and the SIM view is blank")
                self._say(st, "lapsed", f"your Saxo {w} sign-in has EXPIRED. "
                          f"{what} until you run on the PC:  "
                          f"~/claire_trading/bin/saxo login {w}"
                          + (_armed_note() if w == "live" else ""))
        elif s["refresh_valid_s"] < WARN_BELOW and st["state"] == "ok":
            self._say(st, "warning", f"Saxo {w} sign-in refresh is failing "
                      f"({str(error)[:120]}); it lapses in about "
                      f"{s['refresh_valid_s'] // 60} min unless the "
                      "connection recovers.")
        self._save(st)
        return st["state"]


def _armed_note():
    try:
        from .. import live_trading
        from ..accounting import db
        conn = db.connect(ROOT / "var" / "desk.db")
        if live_trading.status(conn, os.environ)["armed"]:
            return (" Live trading is ARMED: open live orders keep their "
                    "Saxo stop-loss/take-profit, but the desk cannot see "
                    "their fills until you sign in.")
    except Exception:                           # noqa: BLE001 — note only
        pass
    return ""


def keepalive_loop(stop_event=None, *, every=KEEPALIVE_EVERY, watches=None):
    """Keep every signed-in environment alive (sim and live independently)
    and alert on trouble. An environment never signed in is skipped."""
    watches = watches or {w: SignInWatch(w) for w in ENVS}
    while not (stop_event and stop_event.is_set()):
        soonest = every
        for w, watch in watches.items():
            refreshed, error = False, None
            try:
                if token_file(w).exists() and status(w)["signed_in"]:
                    access_token(w, force=True)
                    refreshed = True
            except Exception as e:              # noqa: BLE001 — judged below
                error = e
            try:
                state = watch.observe(refreshed=refreshed, error=error)
            except Exception:                   # noqa: BLE001 — never kill
                state = "ok"                    # the keepalive over an alert
            if error is not None and state != "lapsed":
                soonest = min(soonest, RETRY_EVERY)
        if stop_event:
            stop_event.wait(soonest)
        else:
            time.sleep(soonest)
