# Claire — LangGraph Multi-Agent Paper-Trading Desk

An AI trading desk: specialist LLM agents research a stock, a bull and a bear
debate it, an arbiter issues a typed verdict, and **nothing executes without
explicit human approval verified by a single-use token**. Execution and
accounting are deterministic Python — no LLM ever touches money. Paper/SIM
only, enforced in code and schema.

Full specification: [DESIGN.md](DESIGN.md). Build status: **all §20 steps
implemented**. On this machine, both `ANTHROPIC_API_KEY` and
`CLAIRE_INTERNAL_SECRET` are already set in `etc/claire.env` and Claire runs
live — this section is for setting up a *fresh* checkout elsewhere.

Physically lives at `/mnt/projects/claire_trading`; `~/claire_trading` is a
symlink to it.

## Quick start (development)

```sh
python3.14 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp etc/claire.env.example etc/claire.env && chmod 600 etc/claire.env
#   → set CLAIRE_INTERNAL_SECRET (openssl rand -hex 24) and ANTHROPIC_API_KEY
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
tests/run.sh          # 74 offline tests, <7s: accounting (exact micro-unit
                      # money), graph structure + interrupt/resume,
                      # authorization (403/409/410), providers, sessions,
                      # executor/custodian/watcher, dashboards HTTP
```

Live smoke tests activate automatically when the services are running.

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

This machine runs Claire via a systemd **wrapper** unit, not the
`systemd/claire-api.service` / `claire-dashboards.service` files in this
repo — those hardcode a `/opt/Claire` path that was never actually used
here (an earlier design assumed a dedicated deploy path; the repo just
lives at `/mnt/projects/claire_trading` instead), and running them directly
would fight `bin/claire stop`'s graceful-drain logic: their `Restart=always`
would just relaunch whatever the script had just told to stop.

Installed unit (`systemd/claire.service` — a thin wrapper, not the two
files above):
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
start. `claire-custodian.timer` (DB-side safety net if claire-api is down)
is a real systemd timer and can still be installed/enabled normally if
recurring jobs need it — the schedules themselves live in desk.db, run by
the scheduler inside claire-api, per the **/schedule page**. Market scans
only run against exchanges that are open at fire time.

## Layout

```
app/graph/        pipeline StateGraph, typed state, LLM + execution nodes
app/accounting/   desk.db schema + THE ONLY WRITER (integer micro-unit money)
app/providers/    DB-backed multi-LLM registry, metering, health, seeds
app/tools/        Yahoo market data, search, narrative jail, run_python
                  sandbox, broker adapters (paper sim + MCP configs)
app/api/          claire-api :7788 (FastAPI): resume seam, runs, /ask
app/dashboards.py :7787 stdlib server + approval authorization + watcher
app/custodian.py  fills, TTLs, expiry, orphans (in-process + timer)
app/sessions.py   exchange calendars: is_open / next_open / close_of
web/              dashboard pages (no build step)
systemd/          claire-api/claire-dashboards/claire-custodian unit
                  templates (not what's installed — see "Deploy"), plus
                  the custodian timer and per-region pre-open analysis
voice/            Claire's Piper TTS voice + custom-trained openWakeWord
                  detection model (gitignored — real binaries). Shared
                  with Bob's voice daemon via ~/voice symlinks; see
                  claire_trading's sibling `bob` repo for the full map.
tests/            six layers per DESIGN.md §18
```

## Current limitations

- Broker adapters run against the in-process paper sim; the MCP configs for
  alpaca/saxo/moomoo are in `app/tools/brokers.py` and activate when
  credentials exist.
- Yahoo endpoints are unofficial: occasional 429s are retried once, then
  surfaced honestly.
