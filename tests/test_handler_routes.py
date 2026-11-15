"""The request handler's route tables (REVIEW-2026-11-11 §3.2): GET and POST
are dispatched from tables instead of a 55-branch if-chain, and every route
is a method of its own. What that must not have changed: which method
answers a URL, and the money-path routes' own key check — made in the
route itself, on top of the gate every POST passes first."""
import types

import pytest

from app.dash import handler
from app.dash.common import HTTPError, PAGES, SAXO_CALLBACK, STATIC
from app.dash.handler import GET_ROUTES, GET_VIEWS, POST_ROUTES, Handler

# every route that moves money, places or changes an order, arms live
# trading or starts paid work re-checks the key ITSELF
MONEY_PATH = ("/api/order-control", "/api/trade-retry", "/api/trade-cancel",
              "/api/maintenance-run", "/api/live-arm", "/api/live-limits",
              "/api/live-disarm", "/api/thesis-action", "/api/override",
              "/api/telegram-decision", "/api/price-triggers",
              "/api/position-floor", "/api/position-levels",
              "/api/manual-trade")


def unpaired(key=None):
    """A handler with no socket behind it: just enough to call one route
    method. The desk it belongs to has a key; the request has `key`. There
    is no database, market or claire-api on it — a route that got past its
    key check would fail on the first thing it touched, not with a 403."""
    h = object.__new__(Handler)
    h.ctx = types.SimpleNamespace(dash_key="the-desk-key")
    h.headers = {"X-Dash-Key": key} if key else {}
    h.client_address = ("192.168.1.50", 50000)
    return h


def test_every_route_names_a_method_that_exists():
    """A misspelt name in a table would only show when someone asked for
    that URL."""
    for table in (GET_VIEWS, GET_ROUTES, POST_ROUTES):
        for path, name in table.items():
            assert callable(getattr(Handler, name, None)), (path, name)


def test_no_url_is_answered_from_two_places():
    """do_GET looks in the pages, then the scripts, then the views, then
    the routes. A path in two of them would be answered by the first and
    the other would be dead code that looks alive."""
    places = [set(PAGES), set(STATIC), set(GET_VIEWS), set(GET_ROUTES)]
    for i, a in enumerate(places):
        for b in places[i + 1:]:
            assert not a & b
    # and a decorator cannot quietly replace a route that is already there
    with pytest.raises(RuntimeError):
        handler.post("/api/thesis-action")(lambda self, body: None)
    with pytest.raises(RuntimeError):
        handler.get("/api/overview")(lambda self, q: None)   # a view's URL
    assert POST_ROUTES["/api/thesis-action"] == "_post_thesis_action"


def test_the_tables_hold_the_routes_the_if_chain_had():
    assert len(GET_VIEWS) == 14 and len(GET_ROUTES) == 26
    assert len(POST_ROUTES) == 27
    assert SAXO_CALLBACK in GET_ROUTES and "/api/events" in GET_ROUTES
    # the two that shared one branch are two routes with one job each
    assert POST_ROUTES["/api/trade-retry"] == "_post_trade_retry"
    assert POST_ROUTES["/api/trade-cancel"] == "_post_trade_cancel"
    # no route was named into the override prefix by accident
    assert [p for p in POST_ROUTES if p.startswith("/api/override")] == [
        "/api/override"]


@pytest.mark.parametrize("path", MONEY_PATH)
def test_a_money_path_route_checks_the_key_itself(path):
    """Called directly — as if the gate at the top of do_POST had been moved
    or lost in a refactor — the route still refuses a request without the
    key, and one with the wrong key, before it does anything else."""
    for key in (None, "not-the-desk-key"):
        with pytest.raises(HTTPError) as e:
            getattr(unpaired(key), POST_ROUTES[path])({"work_item_id": "wi"})
        assert e.value.code == 403
        assert e.value.message == "dashboard not paired"


def test_with_the_key_a_money_path_route_gets_past_its_check():
    """The check above is the key and nothing else: the same call with the
    right key goes on to do its work (and fails here only because this
    handler has no desk behind it)."""
    for path in MONEY_PATH:
        with pytest.raises(Exception) as e:
            getattr(unpaired("the-desk-key"), POST_ROUTES[path])({})
        assert not (isinstance(e.value, HTTPError) and e.value.message
                    == "dashboard not paired"), path


def test_the_key_gated_reads_check_it_too():
    # what was said to Claire, and the real Saxo account
    with pytest.raises(HTTPError) as e:
        unpaired()._get_chat_history({})
    assert e.value.code == 403
    h = unpaired()
    h._send = lambda code, body: (code, body)
    assert h._get_saxo_account({"env": "live"}) == (
        403, {"error": "dashboard not paired"})
    assert GET_ROUTES["/api/chat-history"] == "_get_chat_history"
    assert GET_ROUTES["/api/saxo-account"] == "_get_saxo_account"


def test_arming_live_trading_still_needs_this_pc():
    """Paired but on the LAN: /api/live-arm refuses on the second check."""
    with pytest.raises(HTTPError) as e:
        unpaired("the-desk-key")._post_live_arm({"phrase": "x"})
    assert e.value.code == 403 and "desk's own PC" in e.value.message
