# Claire — LangGraph Multi-Agent Paper-Trading Desk

An AI trading desk: specialist LLM agents research a stock, a bull and a bear
debate it, an arbiter issues a typed verdict, and **nothing executes without
explicit human approval verified by a single-use token**. Execution and
accounting are deterministic Python — no LLM ever touches money. Paper/SIM
by default; real-money trading on Saxo exists but is OFF behind a four-lock
switch (DESIGN.md §22, `bin/claire live`). Saxo how-to — signing in, the
Saxo accounts page, SIM and live trading: [docs/SAXO.md](docs/SAXO.md).

Full specification: [DESIGN.md](DESIGN.md). Build status: **all §20 steps
implemented**. On this machine `CLAIRE_INTERNAL_SECRET` is already set in
`etc/claire.env` and Claire runs live — this section is for setting up a
*fresh* checkout elsewhere.

**Model billing.** Every agent defaults to the `claude_max` provider: the
Claude Agent SDK signed in with the machine's claude.ai login, so model use
counts against the Claude Max subscription, not API credit
(`app/providers/claude_sdk.py`). It needs no API key; it does need `claude`
to have been logged in once as this user (`claude` → `/login`). LangGraph
still owns orchestration and the approval gate. The `anthropic` (API-key)
provider remains available per agent on the Agents page.

Physically lives at `/mnt/projects/claire_trading`; `~/claire_trading` is a
symlink to it.

## Quick start (development)

```sh
python3.14 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp etc/claire.env.example etc/claire.env && chmod 600 etc/claire.env
#   → set CLAIRE_INTERNAL_SECRET (openssl rand -hex 24); ANTHROPIC_API_KEY
#     only if an agent is moved to the API-billed `anthropic` provider
claude   # once, then /login — the default provider uses this sign-in
./venv/bin/python -m app.api.server        # :7788 graph runtime + chat
./venv/bin/python -m app.dashboards        # :7787 pages + watcher
```

Once `etc/claire.env` exists, day-to-day control is `bin/claire`, not the
raw commands above — see "Day-to-day control" below.

Open http://127.0.0.1:7787 — Mission Control, Trading (launch runs),
Portfolio, Markets, Agents (per-agent provider/model panel), Providers.

Chat seam (voice/assistants integrate here):
`POST :7788/ask {"text": …}` → NDJSON stream · `GET /status` · `GET /feed`.

## Tests

```sh
tests/run.sh          # about 800 offline tests, ~90 s: accounting (exact
                      # micro-unit money), graph structure + interrupt/
                      # resume, authorization (403/409/410), providers,
                      # sessions, executor/custodian/watcher, dashboards HTTP
```

`tests/test_smoke.py` runs against the LIVE services whenever they are up
(it sends Claire one real chat turn). Leave it out with
`pytest tests --deselect tests/test_smoke.py` to stay offline.

## Day-to-day control

`bin/claire` is the actual interface used on this machine — not the raw
`python -m app.api.server` commands above, and not the `systemd/*.service`
files in this repo directly (see "Deploy" below for why).

```sh
bin/claire start                 # claire-api (:7788) + dashboards (:7787)
bin/claire stop [--graceful]     # pause schedules, drain work in flight,
                                 # then stop (default; see bin/claire for the
                                 # --kill variant and CLAIRE_DRAIN_TIMEOUT)
bin/claire restart [--kill]
bin/claire status                # what's running, what's in flight
```

Resetting the book (`bin/claire-reset`) is a separate, deliberately harder
to reach command — not part of the daily-use script.

## Deploy

This machine runs Claire via one systemd **wrapper** unit that calls
`bin/claire`. (Per-service units with `Restart=always` would fight
`bin/claire stop`'s graceful drain by relaunching what the script had just
stopped; the templates for them, which pointed at a `/opt/Claire` path that
never existed, were removed on 2026-11-11.)

Installed unit (`systemd/claire.service`):
```ini
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/mnt/projects/claire_trading
ExecStart=/mnt/projects/claire_trading/bin/claire start
ExecStop=/mnt/projects/claire_trading/bin/claire stop --graceful
TimeoutStopSec=320
```
```sh
cp systemd/claire.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claire.service
```
Gives reboot persistence, not crash recovery — if `claire-api` or
`claire-dashboards` dies mid-session (not via a reboot), systemd won't
notice or restart it, since the wrapper already exited after the one-shot
start. Recurring jobs (custodian, price triggers, market scans, reconcile,
maintenance) are not systemd timers: the schedules live in desk.db and are
run by the scheduler inside claire-api, per the **Schedule page**. Market
scans only run against exchanges that are open at fire time.

Logs: `var/log/api.log` and `var/log/dashboards.log`, one timestamped line
per event (`app/logsetup.py`); `CLAIRE_LOG_LEVEL` in `etc/claire.env` sets
the level (default INFO). The maintainer rotates them.

## Layout

```
app/graph/        pipeline StateGraph, typed state, LLM + execution nodes
app/accounting/   desk.db schema + THE ONLY WRITER (integer micro-unit money)
app/providers/    DB-backed multi-LLM registry, metering, health, seeds
app/tools/        market data (Twelve Data > TradingView > Yahoo), news
                  search, narrative jail, run_python sandbox, broker
                  adapters (in-process paper sim, Saxo)
app/api/          claire-api :7788 (FastAPI): resume seam, runs, /ask
app/dashboards.py :7787 stdlib server + approval authorization + watcher
app/custodian.py  fills, TTLs, expiry, orphans, ledger check, broker sync
app/logsetup.py   logging for both services
app/maintenance.py daily checks, log rotation, backups, retention + AI note (§23)
app/sessions.py   exchange calendars: is_open / next_open / close_of
web/              dashboard pages (no build step)
systemd/          claire.service — the wrapper unit (see "Deploy")
docs/             SAXO.md; past reviews
voice/            Claire's Piper TTS voice + custom-trained openWakeWord
                  detection model (gitignored — real binaries). Shared
                  with Bob's voice daemon via ~/voice symlinks; see
                  claire_trading's sibling `bob` repo for the full map.
tests/            six layers per DESIGN.md §18
```

## Current limitations

- alpaca-paper and moomoo-paper are the in-process paper sim, not those
  brokers; saxo-paper trades at Saxo SIM (SGX names are simulated locally,
  as Saxo SIM lists them only as CFDs). The `mcp_servers` table and
  `mcp/saxo_mcp.py` are not used by the desk itself.
- Without a `TAVILY_API_KEY` the agents' web search is a news-headline
  search (Bing News / Google News RSS), not a general web search.
- Yahoo is the last-resort data source and rate-limits this machine; a
  refusal is surfaced, never papered over.
- Holiday calendars in `app/sessions.py` are hand-maintained per year.
