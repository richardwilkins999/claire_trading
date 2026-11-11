"""What started a run — decided once.

Three places each parsed the run's free-text `trigger` to answer this (the
run story on the Runs page, the Usage page's attribution, and the Telegram
card), with three orderings of the rules and three vocabularies, and they
had begun to disagree: a "human override …" trigger on a run whose kind was
not `override` was an override to Usage and Telegram and "other" to the
story (REVIEW-2026-11-11 §3.1). classify() is the one decision; each of the
three keeps only its own wording.
"""

# every code classify() can return
OVERRIDE = "override"               # you overrode a PASS
EXIT_REVIEW = "exit_review"         # the price watcher started a sell review
VOLUME_SCAN = "volume_scan"         # the hourly unusual-volume sweep
DAILY_SCREEN = "daily_screen"       # picked by the screener agent
DAILY_FALLBACK = "daily_fallback"   # ...or by the top-of-book fallback
PRICE_TRIGGER = "price_trigger"     # one of your price triggers fired
MANUAL_TRADE = "manual_trade"       # a buy/sell you started yourself
LAUNCH_FORM = "launch_form"         # the Launch form on the Runs page
MARKETS_PAGE = "markets_page"       # "Send to pipeline" on Markets
CHAT = "chat"                       # asked for in Claire's chat
UNRECORDED = "unrecorded"           # no trigger text at all
OTHER = "other"


def classify(kind, trigger, *, override_of=None) -> tuple:
    """(code, detail). `detail` is the part of the trigger after its
    "label: " prefix, when there is one, else the whole trigger text (or
    None when the code says it all)."""
    t = (trigger or "").strip()
    tl = t.lower()
    after = t.split(": ", 1)[1] if ": " in t else None
    if tl.startswith("manual ") and " from trigger" in tl:
        return PRICE_TRIGGER, after
    if kind == "override" or override_of or tl.startswith("human override"):
        return OVERRIDE, (t[len("human override of "):]
                          if tl.startswith("human override of ") else None)
    if kind == "sell_review" or tl.startswith("watcher:"):
        return EXIT_REVIEW, after or (t or None)
    if tl.startswith("event scan"):
        return VOLUME_SCAN, after
    if tl.startswith("deterministic fallback"):
        return DAILY_FALLBACK, None
    if kind == "screen" or tl.startswith("screener"):
        return DAILY_SCREEN, after
    if tl.startswith("launched from the trading page"):
        return LAUNCH_FORM, None
    if tl.startswith("launched from the markets page"):
        return MARKETS_PAGE, None
    if kind == "manual" or tl.startswith("manual "):
        return MANUAL_TRADE, t or None
    if tl.startswith("chat: "):
        return CHAT, t[len("chat: "):]
    if not t:
        return UNRECORDED, None
    return OTHER, t
