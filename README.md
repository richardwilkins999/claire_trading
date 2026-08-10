# Claire — LangGraph Multi-Agent Paper-Trading Desk

An AI trading desk: specialist LLM agents research a stock, a bull and a bear
debate it, an arbiter issues a typed verdict, and **nothing executes without
explicit human approval verified by a single-use token**. Execution and
accounting are deterministic Python — no LLM ever touches money. Paper/SIM
only, enforced in code and schema.

Full specification: [DESIGN.md](DESIGN.md). Build status: **all §20 steps
implemented**; live LLM runs need an `ANTHROPIC_API_KEY` in `etc/claire.env`.

## Quick start (development)

```sh
python3.14 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp etc/claire.env.example etc/claire.env && chmod 600 etc/claire.env
#   → set CLAIRE_INTERNAL_SECRET (openssl rand -hex 24) and ANTHROPIC_API_KEY
./venv/bin/python -m app.api.server        # :7788 graph runtime + chat
./venv/bin/python -m app.dashboards        # :7787 pages + watcher
```

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

## Deploy (/opt/Claire, systemd user units)

```sh
sudo mkdir /opt/Claire && sudo chown $USER:$USER /opt/Claire
git clone git@github.com:richardwilkins999/claire_trading /opt/Claire
cd /opt/Claire && python3.14 -m venv venv && venv/bin/pip install -r requirements.txt
cp etc/claire.env.example etc/claire.env && chmod 600 etc/claire.env  # add keys
cp systemd/* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now claire-api claire-dashboards \
  claire-custodian.timer claire-reconcile.timer \
  claire-analysis-asia.timer claire-analysis-eu.timer claire-analysis-us.timer
```

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
systemd/          services + timers (per-region pre-open analysis)
tests/            six layers per DESIGN.md §18
```

## Current limitations

- No `ANTHROPIC_API_KEY` on this machine yet → pipeline runs fail loudly at
  the analyst stage (by design); add the key and restart to go live.
- Broker adapters run against the in-process paper sim; the MCP configs for
  alpaca/saxo/moomoo are in `app/tools/brokers.py` and activate when
  credentials exist.
- Yahoo endpoints are unofficial: occasional 429s are retried once, then
  surfaced honestly.
