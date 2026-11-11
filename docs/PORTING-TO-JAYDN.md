# Applying the 2026-11-11 changes to Jaydn_trading

Everything done to `claire_trading` on 2026-11-11, packaged so it can be
applied to `Jaydn_trading`. It was written without sight of
`Jaydn_trading` (it is not on the machine this was done on), so it assumes
that desk is a copy of Claire at or near the starting commit below, and
says where a copy that has drifted will need a hand.

| | |
|---|---|
| Starts from | `61229a2` — "Claire's tools: label both currencies; recover Saxo prices from its P/L" |
| Ends at | `91f3717` — "Review: status at the end of 2026-11-11" |
| Size | 15 commits, 87 files, +4,617 / −1,868 lines |
| Per-commit patches | [`porting/claire-2026-11-11.mbox`](porting/claire-2026-11-11.mbox) (for `git am`) |
| One combined patch | [`porting/claire-2026-11-11.diff`](porting/claire-2026-11-11.diff) (for `git apply`) |
| Why each change was made | [`../REVIEW-2026-11-11.md`](../REVIEW-2026-11-11.md) — start with "Status at the end of 2026-11-11" |

Nothing here places, changes or cancels an order by itself, and nothing
touches broker settings or the live-trading switch.

---

## 1. Before you start

Do these on the Jaydn machine, in the `Jaydn_trading` checkout. The control
script is called `bin/claire` in Claire; use whatever Jaydn's is called.

1. **Know your starting point.** Check that Jaydn has the commit this
   starts from, or an equivalent port of it:

   ```bash
   git log --oneline -8
   ```

   If the last Claire change ported to Jaydn is older than `61229a2`, port
   the commits in between first; these patches build on them.

2. **Stop the desk and back up the database.**

   ```bash
   bin/claire stop
   ```

   ```bash
   cp var/desk.db var/desk.db.before-2026-11-11
   ```

3. **Record the test baseline**, so you can tell a failure you caused from
   one that was already there:

   ```bash
   ./venv/bin/python -m pytest tests -q --deselect tests/test_smoke.py
   ```

   (`--deselect` matters before the patches: until commit 8, the smoke test
   sends a real chat turn to a running desk.)

4. **Work on a branch and mark the way back.**

   ```bash
   git switch -c port-2026-11-11
   ```

   ```bash
   git tag before-port-2026-11-11
   ```

---

## 2. Apply

Use the first method that fits. Copy this `docs/` folder (the guide and the
two patch files) to the Jaydn machine first if it cannot reach Claire's
repository.

### A. Jaydn can fetch Claire's repository — cherry-pick

Cleanest, because git knows the history.

```bash
git remote add claire https://github.com/richardwilkins999/claire_trading.git
```

```bash
git fetch claire
```

```bash
git cherry-pick 61229a2..91f3717
```

### B. No access to Claire's repository — apply the per-commit patches

```bash
git am --3way docs/porting/claire-2026-11-11.mbox
```

`--3way` lets git merge a hunk whose surroundings have changed. The 15
commits arrive one by one with their original messages.

### C. A and B both conflict too much — apply the combined patch

One uncommitted change, with rejected hunks left as conflict markers:

```bash
git apply --3way docs/porting/claire-2026-11-11.diff
```

Then commit it yourself once the tests pass.

### When a commit conflicts (methods A and B)

Git stops at the commit it cannot apply.

1. `git status` lists the conflicted files. Open each, keep Jaydn's version
   of anything Jaydn-specific, and take the incoming change around it.
2. Section 4 below says what each commit is for; that is usually enough to
   resolve a hunk by hand.
3. Continue with `git cherry-pick --continue` (method A) or
   `git am --continue` (method B).
4. To leave one commit out entirely: `git cherry-pick --skip` or
   `git am --skip`. Section 4 says which commits others depend on.
5. To give up and return to where you started: `git cherry-pick --abort` or
   `git am --abort`, then `git reset --hard before-port-2026-11-11`.

Run the tests after resolving each conflicted commit, not only at the end.

---

## 3. What will need adapting in a desk that is not Claire

Check these before or while applying. Each is a place where Claire's name,
layout or choices are written into the change.

| Where | What to check | Why it matters |
|---|---|---|
| `app/providers/seeds.py` — `_P_V1` | It must be **word for word** the prompt template Jaydn's agents were first seeded with (Claire's begins "You are the {role} on Claire's trading desk, analysing {ticker}."). | On start, a stored prompt that still equals `_P_V1` is upgraded to the new per-role prompt. If Jaydn's template says "Jaydn's trading desk", nothing matches and no prompt is upgraded — silently. Edit `_P_V1` to Jaydn's old text, and the desk's name in the four new templates (`_ANALYST`, `_DEBATER`, `_DESK`, `_CHAT`) and in the `claire_chat` brief. |
| `app/tools/search.py` — `NEWS_UA` | `claire-desk/1.0 (news search)` | The name the desk gives the news feeds. Use Jaydn's. |
| `web/nav.js` — `NAV` | Menu labels, including "Ask Claire"; the page list. | If Jaydn has more or fewer pages, add or remove them here. `tests/test_ui_review_1111.py` asserts 14 pages and the exact set of links — update both numbers to Jaydn's. |
| `app/exchanges.py` and `DESK` in `web/nav.js` | The ten exchanges, their currency, suffix, board lot and time zone. | If Jaydn trades a different set, change **both**; `tests/test_exchanges.py` fails if they differ, and also checks them against `app/sessions.py`. |
| `app/sessions.py` | The 2027 holiday tables are for Claire's ten exchanges. | Add tables for any exchange Jaydn has that Claire does not. |
| Telegram | Three commits touch `app/telegram_approvals.py` (logging, board lot, table definition, one unused function). | If Jaydn has no Telegram module (the Ronathan port did not), drop those hunks: delete the file from the conflict and continue. Also remove `telegram_notices` from the block appended to `app/accounting/schema.sql` and from `TABLES` in `bin/claire-reset`. |
| Deleted files | `bin/bob-dashboard`, `bin/bob-dashboard-server`, `systemd/claire-api.service`, `claire-dashboards.service`, `claire-custodian.service`, `claire-custodian.timer`, and the `python -m app.custodian` / `python -m app.autonomous` command-line entry points. | They were unused **on Claire's machine**. If Jaydn runs the custodian from a systemd timer, or uses the per-service units, skip commit 7 (`9781a8e`) or restore those files afterwards. Check with `systemctl --user list-units 'claire*' 'jaydn*'` and `systemctl --user list-timers`. |
| `README.md`, `DESIGN.md` | Paths (`/mnt/projects/claire_trading`), the name, the test count. | Documentation only; take Jaydn's wording where they conflict. |
| `tests/preview_server.py` | Made-up accounts named `alpaca-paper` and `saxo-paper`. | Only matters if you use the preview; rename to taste. |
| Account and broker names | The code keys on broker names (`saxo`, `saxo_live`, `alpaca`) and on `environment='live'`, not on account ids. | Nothing to change unless Jaydn renamed the brokers. |

---

## 4. The fifteen commits

In the order they apply. "Needs" lists earlier commits a commit builds on;
skip a commit and you must also skip, or hand-fix, the ones that need it.

| # | Commit | What it does | Needs | Check afterwards |
|---|---|---|---|---|
| 1 | `1959891` Money-path fixes (B1, B2, B3, B6) | A sell review that fails to start is not counted as a fired alert and is retried. After a TTL cancel the custodian books any last fill before expiring the order. FX asks every source for a fresh rate and reuses an old one for at most 4 hours (`FX_MAX_AGE`). The custodian checks the ledger's consistency every run. | — | `tests/test_money_path_review_1111.py` |
| 2 | `3b683c8` Add the review | Adds `REVIEW-2026-11-11.md`. Documentation only. | — | — |
| 3 | `595d5fb` Keyless search | Without a `TAVILY_API_KEY`, `web_search` reads the Bing News and Google News RSS feeds; DuckDuckGo was answering with its bot check. An outage raises instead of returning an empty list. The screener now sees a Tavily key. | — | `tests/test_search.py`; then one live search (section 5) |
| 4 | `0f806ce` Agent prompts | One prompt template per kind of role; unedited stored prompts are upgraded to version 2 on start; no literal `{ticker}` can reach a model. | — | `tests/test_agent_prompts.py`. **Read the `_P_V1` row in section 3 first.** |
| 5 | `27d0ad1` Logging | `app/logsetup.py`: timestamps on every line, uncaught thread deaths logged, 22 silent handlers and the market-data fallbacks leave a throttled line, tracebacks for failed jobs, custodian duties and dashboard 500s. | 1, 3 | `tests/test_logging.py`. **Apply commit 14 as well** — see the warning below. |
| 6 | `fd3c803` Display correctness (B5, B7, B12, B13) | Totals per account currency on Home, Portfolio and Trades; the Trading feed shows the latest events and polls incrementally; Markets never guesses an exchange; two pages no longer replace shared script functions. | — | `tests/test_display_review_1111.py` |
| 7 | `9781a8e` Remove dead code and stale docs | Deletes the files listed in section 3 and seven unused functions; corrects README and DESIGN. | 5 | Full suite. Optional — see section 3. |
| 8 | `b86de86` Tests | Test servers shut down fast (suite roughly halves in time); `tests/test_smoke.py` needs `CLAIRE_SMOKE=1`. | — | Full suite runs with no `--deselect` from here on |
| 9 | `dcc2fd7` Calendars, HK tickers, card cash, live caps, small fixes | 2027 holidays and a daily maintainer check that warns before they run out; Hong Kong tickers in one four-digit form; the approval card shows cash free to spend; live caps kept in one place; average cost of shares still held; break-even is not a loss; Thesis checks no longer `assert`; `claire-reset` clears six more tables; three side tables defined in `schema.sql`; ten indexes. | 5 | `tests/test_review_1111_rest.py` |
| 10 | `72baa75` Exchange registry | `app/exchanges.py` and `DESK` in `web/nav.js` replace eleven Python tables and five page tables. | 6, 9 | `tests/test_exchanges.py` |
| 11 | `4a4c6b1` dashboards.py split, first steps | Moves the Usage report to `app/dash/usage.py` and helper functions to `app/levels.py` and `app/accounts.py`; one error tail instead of three. No behaviour change. | 5, 10 | Full suite |
| 12 | `9d260b9` UI | Grouped menu with a PAPER / LIVE ARMED indicator and an approvals badge; a "Today" home view; in-page confirmations replacing every browser `confirm`/`prompt`/`alert`; Reject asks first; doctype and viewport on every page; `tests/preview_server.py`. | 6, 10 | `tests/test_ui_review_1111.py`, then look at the pages (section 5) |
| 13 | `2f55bbd` UI, small | "Plan only" as a warning badge; deep links to a run and to an approval card work; unused page code removed. | 12 | Look at Portfolio and `/trading#<run id>` |
| 14 | `5b8a9f0` Logging: scrub credentials | Blanks API keys, tokens and bot tokens in every log line. | 5 | `tests/test_logging.py` |
| 15 | `91f3717` Review status | Documentation only. | 2 | — |

> **Do not run a desk with commit 5 but without commit 14.** An error's own
> text quotes the URL that failed, API key included. On Claire, the first
> hour of logging wrote the Twelve Data key into `var/log/dashboards.log`
> fifteen times before commit 14 existed. If that happens on Jaydn, see
> "If a key reached the log" in section 6.

---

## 5. Verify

### Tests

```bash
./venv/bin/python -m pytest tests -q
```

On Claire this ends `842 passed, 4 skipped` in about 45 seconds. The four
skips are the opt-in smoke tests. Jaydn's count will differ if its test
suite does; what matters is no failures beyond your baseline from step 1.3.

### Look at the pages without touching anything real

```bash
./venv/bin/python tests/preview_server.py
```

Open `http://127.0.0.1:7799` and, when asked for the dashboard key, enter
`preview-key`. It serves made-up data from a throwaway database and cannot
reach Saxo, Telegram, the model provider or the web. Check:

- the menu fits one line and shows **PAPER** on the right;
- Home opens on "Today" with holdings per currency and a protection badge
  on each;
- on Approvals, **Reject** opens a confirmation instead of rejecting.

Stop it with Ctrl-C.

### Start the desk and check it

```bash
bin/claire start
```

What happens to the database on this first start — all of it additive:

- ten indexes are created, and three tables that already exist are
  declared in `schema.sql`;
- each agent prompt that was never edited becomes version 2 (the old text
  stays in `agent_versions`);
- the 2027 holidays are merged into `exchange_sessions`.

Then check, in this order:

1. **Prompts were upgraded.** Every agent except `maintainer` should be at
   version 2. If they are all still at 1, `_P_V1` does not match Jaydn's
   old prompts (section 3).

   ```bash
   ./venv/bin/python -c "import sqlite3; print(dict(sqlite3.connect('file:var/desk.db?mode=ro', uri=True).execute('SELECT id, version FROM agents').fetchall()))"
   ```

2. **The custodian and watcher have run clean** on the new process (wait up
   to five minutes for the custodian):

   ```bash
   ./venv/bin/python -c "import sqlite3; [print(r) for r in sqlite3.connect('file:var/desk.db?mode=ro', uri=True).execute(\"SELECT service, datetime(checked_at,'unixepoch','localtime'), ok FROM service_health WHERE checked_at > strftime('%s','now')-600 ORDER BY checked_at DESC LIMIT 6\")]"
   ```

   A `LEDGER INCONSISTENT` flag here is the new consistency check finding a
   real problem in Jaydn's book. Do not silence it; look at the account it
   names.

3. **The log has timestamps and no credentials.**

   ```bash
   tail -n 20 var/log/dashboards.log
   ```

   ```bash
   grep -cE "apikey=[^<]" var/log/*.log
   ```

   The second command must print `0` for every file.

4. **Search works** (one real request to the public news feeds):

   ```bash
   ./venv/bin/python -c "from app.tools.search import web_search; [print(h['title'][:80]) for h in web_search('Nvidia earnings', env={}, max_results=3)]"
   ```

5. **The pages.** Home shows your real holdings per currency; Trades shows
   its three money totals; the menu shows PAPER (or LIVE ARMED).

---

## 6. What changes for the person using the desk

- **Home** opens on holdings, protection and what is waiting. The system
  map is on its own tab.
- **Menu:** Trades, Runs and Usage are under *Review*; Agents, Providers,
  Maintenance and Saxo accounts under *System*. "Trading" is now "Runs".
- **Money** is shown per currency. There is no single "equity" for a desk
  with accounts in two currencies.
- **Confirmations** are in the page. Real-money actions show a red REAL
  MONEY banner and need the word typed before the button works.
- **Reject** asks first.
- **Approval card cash** is what is free to spend; cash held for open buy
  orders is shown separately.
- **Web search without a Tavily key is a news search.** It will not find
  company filings.
- **A failed sell-review launch** now sends one alert a day per position
  and is retried every check, instead of passing silently.
- **Maintenance** has a new line, "Exchange holiday calendars". It warns
  from mid-November 2027, and from 15 December 2026 about NSE's incomplete
  2027 list.
- **Logs** in `var/log/` have timestamps. `CLAIRE_LOG_LEVEL` in the env
  file sets the level (default `INFO`).

### If a key reached the log

Only possible if the desk ran with commit 5 and without commit 14.

```bash
grep -lE "apikey=[^<]" var/log/*
```

If that lists a file, stop the desk, then blank the values in place:

```bash
./venv/bin/python -c "
import pathlib
from app.logsetup import redact
for p in pathlib.Path('var/log').glob('*'):
    if p.is_file():
        t = p.read_text(errors='replace'); p.write_text(redact(t))"
```

The log directory is not in git, so the key has not left the machine
unless the log was copied somewhere. If it was, replace the key at the
provider.

---

## 7. Going back

Code:

```bash
bin/claire stop
```

```bash
git reset --hard before-port-2026-11-11
```

Database: the new indexes and the merged holidays are harmless to the old
code and can stay. Only the prompts change what the agents are told. To put
every agent back on its version-1 prompt:

```bash
./venv/bin/python -c "
import sqlite3
c = sqlite3.connect('var/desk.db')
c.execute('''UPDATE agents SET version=1, system_prompt=(SELECT system_prompt
  FROM agent_versions v WHERE v.agent_id=agents.id AND v.version=1)
  WHERE version=2''')
c.commit()"
```

Or restore the whole database from the copy made in step 1.2 — but that
also discards every trade and order recorded since.

---

## 8. Not included

These were found by the review and are **not** fixed by these patches, on
Claire or anywhere. The full list is in `REVIEW-2026-11-11.md` under
"Status at the end of 2026-11-11". The ones that matter most:

- Nothing compares the book's positions with what the broker holds.
- NSE's 2027 holidays are incomplete until its circular is published.
- The chat agent's refusal rate has not been re-measured on the new prompt.
- `dashboards.py` is still one 3,170-line file; the positions and pricing
  logic is still implemented in several places.
- The single trade ticket, editable protection and the rest of the UI list.
