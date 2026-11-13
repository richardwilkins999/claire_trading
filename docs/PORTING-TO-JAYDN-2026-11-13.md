# Applying the 2026-11-13 page changes to Jaydn_trading

One commit made to `claire_trading` on 2026-11-13, packaged so it can be
applied to `Jaydn_trading`. It changes two pages (Home and Portfolio) and
nothing else: no Python, no database, no settings, no broker calls.

Like the earlier guide, it was written without sight of `Jaydn_trading`.

| | |
|---|---|
| Needs first | The 2026-11-11 port ([`PORTING-TO-JAYDN.md`](PORTING-TO-JAYDN.md)), up to `2b4449e` |
| Starts from | `2b4449e` — "Review: status after the second pass" |
| Ends at | `095ecbb` — "Home and Portfolio: profit and loss first, compact Today rows, System map first" |
| Size | 1 commit, 6 files, +399 / −127 lines |
| Patch for `git am` | [`porting/claire-2026-11-13.mbox`](porting/claire-2026-11-13.mbox) |
| Patch for `git apply` | [`porting/claire-2026-11-13.diff`](porting/claire-2026-11-13.diff) |

Both patch files were applied to a clean checkout of `2b4449e` and
reproduced `095ecbb` exactly; the page tests passed there (67 passed).

---

## 1. What it changes

**Mission Control (Home)**

- **System map** is the first tab and the one the page opens on. **Today**
  is second, and opens directly at `/#today`.
- Each holding on Today is one compact line:
  - left: what the stop-loss would cost you, with its price small beneath;
  - middle: a slim bar, red from the stop to what you paid and green from
    there to the target, with marks for what you paid, the trailing alert
    and the price now;
  - right: what the take-profit would make, with its price beneath.
- "no stop-loss" and "plan only" are written out in that line. Who acts at
  each level (the broker, the desk's simulation, the trailing alert) is the
  bar's tooltip.
- Resting the pointer on a ticker pops up its one-week chart, with your
  cost and levels drawn in.

**Portfolio**

- Each "What protects it" tile shows the money first, large and coloured by
  sign, with the price it happens at beneath ("if hit at 154.45 USD").
- The distance chip, the list of separate orders and "At the broker — sells
  by itself" moved to the tile's tooltip. Three tiles now fit across a card.
- The price line under the tiles is unchanged.

**Shared code** (so the two pages cannot disagree)

- `web/orderform.js`: `protectionFacts`, `protectionHolder`,
  `protectionMoney`, `protectionLine`.
- `web/charts.js`: `drawPositionChart`, used by the Portfolio chart and the
  popup.

Files touched: `web/index.html`, `web/portfolio.html`, `web/orderform.js`,
`web/charts.js`, `tests/test_ui_review_1111.py`, `tests/test_dashboards.py`.

---

## 2. Before you start

On the Jaydn machine, in the `Jaydn_trading` checkout.

1. **Check the 2026-11-11 port is in.** This patch edits the Today tab,
   which that port added:

   ```bash
   grep -c 'id="view-desk"' web/index.html
   ```

   It should print `1`. If it prints `0`, apply the earlier guide first.

2. **Record the test baseline:**

   ```bash
   ./venv/bin/python -m pytest tests -q
   ```

3. **Work on a branch and mark the way back:**

   ```bash
   git switch -c port-claire-2026-11-13
   ```

   ```bash
   git tag before-port-2026-11-13
   ```

The desk does not need stopping, and the database needs no backup: the
pages are read from disk on each request and nothing else changes.

---

## 3. Apply

Use the first method that fits.

### A. Jaydn can fetch Claire's repository

This needs `095ecbb` to be on the remote you fetch from. If the remote from
the earlier port is still set up:

```bash
git fetch claire
```

```bash
git cherry-pick 095ecbb
```

### B. No access to Claire's repository

Copy this guide and `docs/porting/claire-2026-11-13.mbox` across, then:

```bash
git am --3way docs/porting/claire-2026-11-13.mbox
```

### C. A and B conflict too much

One uncommitted change, with rejected hunks left as conflict markers:

```bash
git apply --3way docs/porting/claire-2026-11-13.diff
```

Commit it yourself once the tests pass.

### If it conflicts

The likely place is `web/index.html` or `web/portfolio.html`, if Jaydn's
copies have their own edits. `git status` lists the conflicted files.

- Keep Jaydn's own wording and anything Jaydn-specific.
- Take the incoming change around it. Section 1 says what each part is for.
- Continue with `git cherry-pick --continue` (method A) or
  `git am --continue` (method B).
- To give up: `git cherry-pick --abort` or `git am --abort`, then
  `git reset --hard before-port-2026-11-13`.

---

## 4. What to adapt in a desk that is not Claire

The patch carries Claire's name in three pieces of on-screen text. Change
them to the desk's own name after applying:

| File | Text | Where it shows |
|---|---|---|
| `web/orderform.js` | `Claire's sim` and `Claire's sim — sells at the live price` (in `protectionHolder`) | Tooltips on Today and Portfolio |
| `web/index.html` | `Claire asks you before selling` (twice, in `protectionOf`) | Tooltips on the Today bar |

Find them with:

```bash
grep -n "Claire's sim\|Claire asks you" web/orderform.js web/index.html
```

It should list four lines: two in each file (one of the two in
`web/orderform.js` is a comment).

Other things to check:

- **Static files.** The Home page now loads `/orderform.js` and
  `/charts.js`. The 2026-11-11 port already serves both (`STATIC` in
  `app/dash/common.py`); if Jaydn's file map differs, make sure both are in
  it or the Today tab will be empty.
- **Currencies.** The amounts at each level are in the share's own currency
  and labelled with it; the "Profit / loss" column stays in the account's
  currency. Nothing to change, but expect to see both on a desk that holds
  foreign shares.
- **Old links.** `/#system` no longer does anything (the System map is the
  default). `/#today` is the new one. The `homeTab` browser setting is no
  longer written or read.

---

## 5. Verify

### Tests

```bash
./venv/bin/python -m pytest tests -q
```

On Claire this ends `992 passed, 4 skipped`. What matters on Jaydn is no
failures beyond the baseline from step 2.2.

Two tests changed with the pages:

- `tests/test_ui_review_1111.py` describes the new layout (five tests
  added, and `test_home_page_answers_the_daily_question` adjusted for the
  tab swap).
- `tests/test_dashboards.py::test_mission_control_map_is_one_svg` used to
  forbid any absolutely-positioned element on the Home page. It now allows
  the Today bar (`.pbar`) only.

If you renamed "Claire" in section 4, no test pins those strings.

### Look at the pages without touching anything real

```bash
./venv/bin/python tests/preview_server.py
```

Open `http://127.0.0.1:7799`, enter `preview-key` when asked for the
dashboard key, and check:

- Home opens on the **System map**; the **Today** tab is second;
- on Today, each holding is one line with a red amount, a bar and a green
  amount, and a key under the table;
- resting on a ticker shows a chart, and moving away hides it;
- on Portfolio, each tile leads with a signed amount and the price sits
  beneath it.

The preview's made-up holdings have few levels set, so expect "no
stop-loss" and "plan only" on some rows. Stop it with Ctrl-C.

### On the running desk

No restart is needed. Refresh the Home and Portfolio pages in the browser.
If a page looks unchanged, do a hard refresh (Ctrl-Shift-R).

---

## 6. Going back

```bash
git reset --hard before-port-2026-11-13
```

Then refresh the pages. There is nothing to undo in the database or the
settings.

---

## 7. Not included

- Clicking a ticker on Today still opens the top of Portfolio, not that
  stock's card.
- On a phone, the Today table scrolls sideways to reach the bar, and the
  chart popup needs a mouse or keyboard.
