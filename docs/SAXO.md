# Claire × Saxo — operating guide

How Claire connects to your two Saxo accounts, how to sign in, how paper and
real-money trading work, and what to do when something goes wrong. The design
behind it is in [DESIGN.md §22](../DESIGN.md).

| | Saxo account | Money | On Claire's approval card | Saxo app (developer portal) |
|---|---|---|---|---|
| **SIM** | 11111111 | simulated (€1,000,000) | `Saxo SIM 11111111 — paper` | "Claire Trading" (SIM, PKCE) |
| **LIVE** | 99999999 | **real** | `⚠ Saxo LIVE 99999999 — real money` | your live app (PKCE) |

Both apps use grant type **PKCE** and the redirect URL
**`http://localhost/claire-saxo`** (no port, no trailing slash).

---

## 1. Signing in

Each account has its own sign-in. You sign in once; Claire keeps it alive
from then on (§2).

### From the dashboard (easiest)

1. On the Claire PC, open **http://localhost:7787/saxo** — it must be
   `localhost`, not the LAN address, because Saxo sends your browser back to
   `localhost` after you log in.
2. Pick the **Saxo LIVE** or **Saxo SIM** tab.
3. Click **Sign in to Saxo LIVE** (or SIM), log in on Saxo's page, approve.
   You land back on the Saxo accounts page with "Signed in ✓".

The dashboard key (🔑 in the menu bar) must be set in that browser.

### From a terminal (same result)

```
~/claire_trading/bin/saxo login live     # or: login sim
```

### The account check

After you log in, Claire asks Saxo which account the login holds. If it is
not the expected one (11111111 for SIM, 99999999 for live) the sign-in is
**refused** and your previous sign-in is kept — logging in with the wrong
Saxo login can never point the desk at a different account.

---

## 2. Staying signed in

- Saxo's access tokens last ~20 minutes; the refresh window is 40–60
  minutes. claire-api renews each signed-in account **every 10 minutes**, so
  a sign-in lasts as long as the desk keeps running. (This is a tiny web
  request to Saxo — it uses **no Claude/Max tokens**.)
- You need to sign in again only if the PC (or claire-api) is off or asleep
  for longer than the refresh window, if you change your Saxo password, or if
  Saxo ends the session.
- **Telegram alerts** (via Bob's bot) and desktop notifications tell you:
  - the renewal is failing and the sign-in lapses in < 15 min;
  - the sign-in has **expired** (repeated every 12 h until you sign in);
  - you are signed in again.

  An alert cannot be sent while the PC is off/asleep; you get it when the
  desk comes back. Test delivery any time: `bin/saxo test-alert`.
- Check status: `bin/saxo status` (both accounts), or the banner on the
  Saxo accounts page.

**Tip:** stop the PC sleeping if you want sign-ins to survive overnight.

---

## 3. Seeing your accounts

| Where | What |
|---|---|
| **Saxo accounts** page (`/saxo`) | Tabs for LIVE and SIM, straight from Saxo: total value, cash, unrealised P/L, available to trade; positions with P/L, P/L % and allocation; working orders (incl. stop-loss / take-profit legs held at Saxo). **Click a position to chart it** (daily prices, SMA 20/50, volume, your average cost as a dashed line; ranges 1 month – 5 years). Refreshes every 30 s. |
| **Portfolio** page (`/portfolio`) | A "Saxo LIVE — your real account" section with the same totals and positions; click a position to open its chart on the Saxo accounts page. The desk's own paper book is shown separately below it. |
| Claire / Bob / Telegram | Ask "what's in my Saxo account?" — Claire reads live or SIM on request and says which. |
| Terminal | `bin/saxo positions [live|sim]` |

None of these pages trades by itself. The only action on them is **Sell**
(§4a), which sends a sell to the Approvals page — it is shown only for shares
the desk bought on that account. Because they show your real account, they
need the dashboard key even to view.

Charts use the desk's market data (TwelveData/Yahoo), mapped from Saxo's
symbol (`D05:xses` → `D05.SI`). Markets the desk does not map show "No chart".

---

## 4. Paper trading on Saxo SIM

Already switched on (`SAXO_SIM_EXECUTION=enabled`). When you approve a trade
and choose **`Saxo SIM 11111111 — paper`**, Claire places a real order on
Saxo's simulator: limit entry plus stop-loss and take-profit legs, all held
at Saxo. Fills are picked up by the custodian within a few minutes.

Notes:
- The SIM account offers only **US and European** stocks as shares. SGX,
  HK, Tokyo and ASX names are listed there only as CFDs (checked
  2026-11-06), so:
  - **SGX** buys and sells on `saxo-paper` are **simulated by the desk
    itself**, like the Alpaca/moomoo paper books: they fill at your limit
    and never reach Saxo. Since 2026-11-11 the built-in sim **holds their
    stop-loss and take-profit** as resting orders (kept in desk.db, so they
    survive a restart); the custodian checks them against the live price
    every 5 minutes while the market is open and sells at the price it sees.
    They do not appear in Saxo's own positions or working orders. The approval card says so instead of
    asking Saxo. (`SIM_LOCAL_SUFFIXES` in `app/tools/saxo_broker.py`;
    never applied to LIVE, where your real account trades SGX shares.)
  - HK / Tokyo / ASX picks are still refused with a clear reason (no exact
    Saxo listing).
- The desk's book for this account (`saxo-paper`) has its own paper cash;
  Saxo SIM has €1M. The desk sizes against its book, Saxo against its own.
- **Currencies:** the desk's `saxo-paper` book is in **SGD**, to match your
  real Saxo account; Saxo's SIM account itself stays in **EUR** (Saxo sets
  that). The book was converted from USD on 2026-11-06 at 1.2773 with
  `python -m app.accounting.redenominate saxo-paper SGD --rate 1.2773`
  (desk stopped; backup and the conversion report in
  `var/backup-20261106-*-pre-sgd/`). Past trades are restated at that one
  rate.
- To go back to the built-in simulator: remove `SAXO_SIM_EXECUTION=enabled`
  from `etc/claire.env` and `bin/claire restart`.

---

## 4a. Selling, and what happens to the stop-loss and take-profit

Every desk buy on Saxo is a **bracket**: the limit entry plus a stop-loss and
a take-profit that rest at Saxo on their own (good-till-cancelled). Once the
entry fills they work as a pair — whichever fills first cancels the other.
The desk tracks both legs as orders of their own (they show on the position
card on **Portfolio** as "at the venue: stop … · target …").

**The trailing floor is a Saxo trailing stop** (since 2026-11-07). The floor %
you approve with a buy turns the bracket's stop into a `TrailingStopIfTraded`:
it starts at your stop, then follows the market at a fixed distance of
*floor % × entry price* (8% of a 150 entry = 12), moving only up, and **sells
automatically** when hit — at Saxo, whether or not the desk is running. The
distance is fixed at the entry, so as the price rises it is a smaller share
of it (12 is 4% at 300). The desk's own trailing alert stays on as a backup.
On Saxo-simulated SGX trades and the other paper books the trail % stays the
watcher's **trailing alert** (it asks you before selling); their fixed
stop-loss / take-profit are held by the built-in sim and do sell by
themselves. The screens say "trailing stop" or "trailing alert" accordingly.
Changing the % after the buy (Portfolio → Edit) moves only the alert: the
trailing stop at Saxo keeps its distance. If a partial sell re-protects the rest, the new
stop trails too, starting from where the old one had got to.

**Three ways a position is sold:**

| How | What happens |
|---|---|
| **Saxo fills a leg** (the price reaches your stop — fixed or trailing — or your target) | Saxo sells; the other leg cancels itself. Within ~5 minutes the custodian books the sale on the desk's book (realised P/L, cash) and marks the other leg cancelled. Nothing for you to do. |
| **Watcher exit review** (the trailing floor alert is breached — on Saxo the trailing stop has normally sold first) | The desk runs a short exit review and, if it says sell, puts a SELL card on **Approvals**. Nothing sells until you approve it. |
| **The Sell button** (Portfolio, or the Saxo accounts page for shares the desk bought there) | A small form: shares (default: all the desk holds), limit price (default: last price), optional note, **Check with Saxo**. *Send to approval* puts a SELL card on **Approvals** with the shares and account filled in; you approve it there. |

**When you approve a sell on Saxo**, the desk:

1. **cancels the position's stop-loss and take-profit at Saxo first.** Saxo
   will not accept a sell while they rest (it answers
   *SellOrdersAlreadyExistForOwnedContracts*), and otherwise the stop could
   sell the same shares again. If Saxo will not confirm the cancel, **nothing
   is sold** — the trade waits on Approvals under "Approved but not placed",
   with the reason;
2. places your limit sell (it fills at your price or better, for the day);
3. if the sell leaves shares behind (you sold part, or it expired unfilled),
   **re-protects the rest** with a new stop-loss + take-profit pair at the
   **same levels**. If that fails you get a Telegram alert and an
   "UNPROTECTED" flag on Mission Control — set the stop at Saxo yourself or
   sell the rest.

"Check with Saxo" on a sell may say Saxo *cannot fully check it while the
stop/target are working* — that is expected: those are exactly the orders
the desk cancels first.

**On Saxo LIVE (real money)** the same rules apply, plus the live locks
(§5). One difference: the desk **never re-protects a real-money position by
itself** — it alerts you and disarms live trading, and you decide.

The desk can only sell shares **it** bought (its own book). A position you
hold yourself at Saxo shows "not the desk's" on the Saxo accounts page.

**Legs placed before 2026-11-04** were not tracked; `bin/claire
legs-backfill --dry-run` shows what it would adopt from Saxo, and
`bin/claire legs-backfill --yes` adopts it (back up `var/desk.db` first). It
only reads from Saxo.

---

## 4b. Price triggers (the Watchlist page)

A price trigger waits for a price and then acts — once. Set them on
**Watchlist** (menu bar): click a stock, then **Add trigger**.

**Examples**

| You want | Set |
|---|---|
| "Buy 50 shares of X when it falls to 5.00" | Buy · 50 shares · when the price *falls to or below (≤)* 5.00 · stop e.g. 4.60 |
| "Buy about 1,000 dollars of X if it breaks out above 12" | Buy · amount 1000 · *rises to or above (≥)* 12.00 · limit a little higher, e.g. 12.10 · stop below |
| "Sell my 20 shares if it reaches 9.50" (take profit) | Sell · 20 shares · *rises to or above (≥)* 9.50 |
| "Sell my 20 shares if it drops to 7.00" (a mental stop) | Sell · 20 shares · *falls to or below (≤)* 7.00 |

The fields:

- **Order limit** — the price the order is sent at. It starts as the
  trigger price. A limit buy fills at that price or better, so for a
  breakout buy ("≥") set it a little above the trigger, or it may not fill.
- **Stop / target** — a buy needs a stop below the limit ("use suggestion"
  offers 2 × the typical daily move, and a target at twice the risk). A
  sell of shares the desk holds needs none. A sell of shares the desk does
  *not* hold is a short sale and needs a stop above the limit (paper only).
- **Account** — where the order goes. A sell must name the account that
  holds the shares.
- **Expires** — 30 days unless you change it.

**When it hits — you choose:**

- **Ask me when it hits** (the default) — the desk puts the trade on
  **Approvals** and you get a Telegram message. Nothing trades until you
  approve it, exactly like any other card.
- **Place it automatically when it hits** — you are approving the order
  *now, in advance*. When the price hits, the desk approves the card for you
  and sends the order. The usual checks still apply (cash, risk limits,
  price band, whole shares on Saxo); if one refuses, the card waits on
  Approvals and Telegram tells you why. **Only on paper accounts** (the
  built-in sim and Saxo SIM 11111111). On the real-money account a trigger
  can only ask you — the desk refuses "automatic" there, and even then the
  approval would still need you to type LIVE.

**Good to know**

- A trigger is checked every 2 minutes, and **only while that market is
  open** — an overnight price never fires it. If the price already meets
  the rule when you create it, it fires at the next check while open.
- It fires **once**, then shows as *fired* with a link to its approval.
  A sell trigger whose shares are gone by then (a stop filled, you sold)
  fails with the reason instead of selling anything.
- Cancel an active trigger on the Watchlist page (Triggers table →
  Cancel). Removing a stock from the watchlist cancels its triggers.
- The **price_triggers** job can be switched off on *Agents & Schedule*
  like any other job; while it is off, nothing fires.
- **Buy now** / **Sell now** on the same page do not wait for a price: they
  put the trade on Approvals straight away.

---

## 5. Real-money trading on Saxo LIVE — OFF until you turn it on

A live order reaches Saxo only when **all four locks** hold, re-checked at the
moment the order is sent:

1. **Enabled** — `CLAIRE_LIVE_TRADING=enabled` in `etc/claire.env`.
2. **Armed** — **Live trading** button on the Orders page → side panel →
   **Turn ON live trading**, from a browser **on the desk's own PC** (http://127.0.0.1:7787),
   typing `ARM LIVE TRADING`; or `bin/claire live arm` in a terminal. A
   browser on another device (phone, laptop) can turn it OFF but never ON.
   Claire's chat has no way to arm. Note: Bob runs on this PC with a shell,
   so he could technically reach the switch (as he could the terminal
   command); his instructions say trading approvals are yours, but nothing
   technical stops him arming.
3. **Your approval** — choose `⚠ Saxo LIVE 99999999 — real money` on the
   card (greyed out until armed) and type **LIVE** when asked.
4. **Spending caps** — money in the live account's currency, plus a count
   of buys (never shares). Defaults: max 1,000 per buy (shares × limit
   price), max 2 buys a day, max 2,000 of buys a day. They cap **buys only**: selling shares the
   desk bought is never capped and doesn't use up the day's allowance, so
   the desk can always get out of a position (a winner that grew past
   1,000, or an exit on a day with both orders used). Exits still need the
   switch armed, your approval with LIVE typed, and a limit price.

Always, whatever the limits: **limit orders only, no shorting, and the desk
can only sell shares it bought itself** — it can never sell what you already
hold at Saxo.

### Turning it on (first time)

1. Sign in to LIVE (§1) and check the LIVE tab shows your real account.
2. Edit `etc/claire.env`: set `CLAIRE_LIVE_TRADING=enabled`.
3. `bin/claire live setup` — creates the desk's `saxo-live` account for
   99999999 and copies its opening cash from Saxo.
4. `bin/claire restart`
5. When you are ready to trade: Orders page → **Live trading** → **Turn ON
   live trading** (browser on this PC), or `bin/claire live arm`
6. Approvals now offer the live account. Start small.

### Everyday controls

| Do this | Command / place |
|---|---|
| See the switch, spending caps, today's usage, live orders and history | **Orders** page → **Live trading** button (opens a side panel), or `bin/claire live status` |
| **Stop live trading now** | Orders → **Live trading** → **Turn OFF** (any paired device), **Disarm** on Approvals, or `bin/claire live disarm` |
| Change spending caps | Orders → **Live trading** → *Spending caps* → Save caps (applies at once, logged), or `bin/claire live limits max_order_base=1500 max_orders_per_day=3` |
| Re-arm | Orders → **Live trading** → **Turn ON** (browser on this PC, typed phrase), or `bin/claire live arm` |
| Remove live trading entirely | delete `CLAIRE_LIVE_TRADING=enabled`, `bin/claire restart` |

Disarming stops **new** live orders; orders already at Saxo keep their
stop-loss / take-profit legs. The desk also **disarms itself** if anything
about a live order is uncertain (an unrecorded fill, an order Saxo doesn't
know, an unknown placement result) — you then check, and re-arm.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "not signed in to Saxo live/sim" | Sign in (§1). |
| Sign-in button says to open localhost | You are on the LAN address or another device; use **http://localhost:7787/saxo** on the PC. |
| "Sign-in refused: that Saxo login holds account(s) …" | You logged in with the other Saxo login; sign in with the right one. |
| "this sign-in link has expired" | Took > 10 minutes, or the dashboard restarted mid-sign-in; click Sign in again. |
| Live account greyed out on the approval card | Not set up or not armed — the card says which (§5). |
| Order refused "no exact Saxo instrument" | That market isn't available on the account; nothing was sent. |
| Order refused "live trading: …" | One of the four locks or a limit; the reason is in the message. |
| "LIVE TRADING DISARMED by the custodian" | Something about a live order needs checking at Saxo; reconcile, then re-arm. |
| Sell refused "could not cancel the protective stop/target" | Saxo would not confirm the legs were cancelled, so nothing was sold. Check the working orders on the Saxo accounts page, then retry or cancel the trade on Approvals. |
| "UNPROTECTED position … no resting stop" / "RE-PROTECTION FAILED" | Some shares have no stop at Saxo (e.g. a partial sell could not be re-protected). Place a stop at Saxo or sell the rest. |
| "UNTRACKED venue order" | Saxo holds an order on the exit side of a desk position that the desk does not know (placed by hand, or before legs were tracked — try `bin/claire legs-backfill --dry-run`). |
| Page says the dashboard key is needed | Click 🔑 in the menu bar and enter it. |

---

## 7. Configuration reference (`etc/claire.env`, mode 600)

```
SAXO_SIM_ACCOUNT_ID=11111111
SAXO_SIM_APP_KEY=…            # SIM app "Claire Trading"
SAXO_SIM_APP_URL=http://localhost/claire-saxo
SAXO_SIM_ACCOUNT_KEY=…        # Saxo's AccountKey for 11111111
SAXO_SIM_EXECUTION=enabled    # approvals on Saxo SIM go to Saxo's simulator

SAXO_LIVE_ACCOUNT_ID=99999999
SAXO_LIVE_APP_KEY=…           # your live app
SAXO_LIVE_APP_URL=http://localhost/claire-saxo

CLAIRE_LIVE_TRADING=          # empty = real-money trading impossible
```

Sign-ins are stored in `etc/saxo_sim_token.json` and `etc/saxo_live_token.json`
(owner-only, never committed). No Saxo password is ever stored or needed.
