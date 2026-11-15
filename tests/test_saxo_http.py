"""The Saxo request layer — offline, mocked HTTP.

Part 1 records, wrapper by wrapper, exactly what each of the desk's Saxo
wrappers puts on the wire and exactly what it raises: the broker adapter
(orders), the read-only account view and the chart reader. These were
written against the code BEFORE the wrappers were moved onto one shared
core (app/tools/saxo_http.py) and must keep passing unchanged: nobody can
place an order at Saxo to check a refactor, so these are the check.

Part 2 tests the shared core itself; part 3 the lock around the order
throttle (one SaxoBroker is shared by the custodian's thread and the API's
request threads).
"""
import json
import threading
import time
import types

import httpx
import pytest

from app.tools import saxo_chart, saxo_http, saxo_live
from app.tools.brokers import BrokerError
from app.tools.saxo_broker import SaxoBroker

SIM = "https://gateway.saxobank.com/sim/openapi"
LIVE = "https://gateway.saxobank.com/openapi"
TOKEN = "s3cr3t-bearer-token"      # must never show up in a message or a log
T0 = 1_800_000_000


def wire(req):
    """Everything about a request that Saxo would see. The user-agent and
    accept-encoding headers are httpx's own (they change with its version
    and with which compression libraries are installed), so they are left
    out; every other header is compared exactly."""
    headers = {k: v for k, v in req.headers.items()
               if k not in ("user-agent", "accept-encoding")}
    return {"method": req.method, "url": str(req.url), "headers": headers,
            "body": req.content}


def recorder(respond=None):
    """(handler, seen): a mock Saxo that records every request."""
    seen = []

    def handler(req):
        seen.append(wire(req))
        out = respond(req) if respond else None
        return out if out is not None else httpx.Response(200, json={})
    return handler, seen


def bare(extra=None):
    """The headers of a request with no body."""
    return {"host": "gateway.saxobank.com", "accept": "*/*",
            "connection": "keep-alive", "authorization": f"Bearer {TOKEN}",
            **(extra or {})}


def with_json(body, extra=None):
    """The headers of a request that carries a JSON body."""
    return {"host": "gateway.saxobank.com", "accept": "*/*",
            "connection": "keep-alive", "authorization": f"Bearer {TOKEN}",
            **(extra or {}),
            "content-length": str(len(body)),
            "content-type": "application/json"}


# ═════════════════════════════════════════════════════════════════════════
# Part 1a — the broker adapter (SaxoBroker._req): orders
# ═════════════════════════════════════════════════════════════════════════
AAPL_DETAILS = {"TickSize": 0.01, "Format": {"OrderDecimals": 2},
                "LotSize": 1, "IsTradable": True}


def broker(handler, env="sim", **kw):
    kw.setdefault("order_gap_s", 0)
    return SaxoBroker(env, token=lambda: TOKEN, account_key="KEY==",
                      client=httpx.Client(transport=httpx.MockTransport(
                          handler)), **kw)


def saxo_ref(req):
    """The two instrument look-ups every placement starts with."""
    p = req.url.path
    if p.endswith("/ref/v1/instruments/"):
        return httpx.Response(200, json={"Data": [
            {"Symbol": "AAPL:xnas", "Identifier": 211}]})
    if p.endswith("/details/211/Stock"):
        return httpx.Response(200, json=AAPL_DETAILS)
    return None


@pytest.mark.parametrize("env,base", [("sim", SIM), ("live", LIVE)])
def test_broker_read_is_a_bare_get_with_the_bearer_header_only(env, base):
    handler, seen = recorder(lambda r: httpx.Response(200, json={
        "CashBalance": 5, "Currency": "USD", "TotalValue": 9}))
    snap = broker(handler, env).snapshot()
    assert snap == {"environment": env, "cash": 5, "currency": "USD",
                    "total_value": 9}
    assert seen == [{"method": "GET", "url": base + "/port/v1/balances/me",
                     "headers": bare(), "body": b""}]


def test_broker_reads_keep_the_query_string_written_into_the_path():
    handler, seen = recorder(lambda r: saxo_ref(r) or httpx.Response(
        200, json={"Data": []}))
    b = broker(handler)
    b.resolve("AAPL")
    b.positions()
    b.working_orders()
    b.related_order_ids("77")
    assert [s["url"] for s in seen] == [
        SIM + "/ref/v1/instruments/?Keywords=AAPL&AssetTypes=Stock&$top=50",
        SIM + "/ref/v1/instruments/details/211/Stock",
        SIM + "/port/v1/positions/me?FieldGroups="
              "PositionBase,DisplayAndFormat&$top=500",
        SIM + "/port/v1/orders/me?FieldGroups=DisplayAndFormat&$top=200",
        SIM + "/cs/v1/audit/orderactivities?OrderId=77&EntryType=All"]
    assert all(s["method"] == "GET" and s["headers"] == bare()
               and s["body"] == b"" for s in seen)


def test_broker_placement_is_a_json_post_with_the_request_id():
    def respond(req):
        if req.method == "POST":
            return httpx.Response(200, json={"OrderId": "5001"})
        return saxo_ref(req) or httpx.Response(200, json={"Data": []})
    handler, seen = recorder(respond)
    b = broker(handler, "live")
    p = b.place_bracket(symbol="AAPL", side="buy", qty=3, limit=201.239,
                        client_order_id="ord_x")
    assert p.status == "accepted" and p.broker_order_id == "5001"
    (post,) = [s for s in seen if s["method"] == "POST"]
    assert post["url"] == LIVE + "/trade/v2/orders"
    assert post["headers"] == with_json(post["body"],
                                        {"x-request-id": "ord_x"})
    assert json.loads(post["body"]) == {
        "AccountKey": "KEY==", "Uic": 211, "AssetType": "Stock",
        "BuySell": "Buy", "Amount": 3, "OrderType": "Limit",
        "OrderPrice": 201.23, "OrderDuration": {"DurationType": "DayOrder"},
        "ManualOrder": False}


def test_broker_placement_without_an_order_id_sends_a_fresh_request_id():
    def respond(req):
        if req.method == "POST":
            return httpx.Response(200, json={"OrderId": "5001"})
        return saxo_ref(req) or httpx.Response(200, json={"Data": []})
    handler, seen = recorder(respond)
    b = broker(handler)
    b.place_bracket(symbol="AAPL", side="buy", qty=1, limit=10)
    b.place_oco(symbol="AAPL", side="sell", qty=1, stop_loss=9)
    rids = [s["headers"]["x-request-id"] for s in seen
            if s["method"] == "POST"]
    assert len(rids) == 2 and rids[0] != rids[1]
    assert all(len(r) == 32 and int(r, 16) >= 0 for r in rids)


def test_broker_precheck_cancel_and_change_carry_no_request_id():
    orders = [{"OrderId": "78", "AccountKey": "KEY==", "Uic": 211,
               "OpenOrderType": "StopIfTraded", "Price": 140.0, "Amount": 10,
               "BuySell": "Sell", "Status": "Working",
               "DisplayAndFormat": {"Symbol": "AAPL:xnas"}}]

    def respond(req):
        if req.url.path.endswith("/precheck"):
            return httpx.Response(200, json={"PreCheckResult": "Ok"})
        if req.url.path.endswith("/orders/me"):
            return httpx.Response(200, json={"Data": orders})
        if req.method == "DELETE":
            return httpx.Response(200, json={"Orders": [{"OrderId": "78"}]})
        return saxo_ref(req) or httpx.Response(200, json={"OrderId": "78"})
    handler, seen = recorder(respond)
    b = broker(handler)
    assert b.precheck(symbol="AAPL", side="buy", qty=1, limit=10)["ok"]
    b.change_leg("78", 143.337, symbol="AAPL")
    b.cancel("78")
    pre, patch, delete = [s for s in seen if s["method"] != "GET"]
    assert (pre["method"], pre["url"]) == \
        ("POST", SIM + "/trade/v2/orders/precheck")
    assert pre["headers"] == with_json(pre["body"])
    assert (patch["method"], patch["url"]) == \
        ("PATCH", SIM + "/trade/v2/orders")
    assert patch["headers"] == with_json(patch["body"])
    assert json.loads(patch["body"]) == {
        "AccountKey": "KEY==", "OrderId": "78", "AssetType": "Stock",
        "Uic": 211, "Amount": 10, "BuySell": "Sell",
        "OrderType": "StopIfTraded", "OrderPrice": 143.33,
        "OrderDuration": {"DurationType": "GoodTillCancel"}}
    # a cancel has no body at all: no content-type, no content-length
    assert delete == {"method": "DELETE", "headers": bare(), "body": b"",
                      "url": SIM + "/trade/v2/orders/78/?AccountKey=KEY=="}


def test_broker_error_text_carries_saxos_error_info():
    b = broker(lambda r: httpx.Response(400, json={
        "ErrorInfo": {"ErrorCode": "TooFarFromMarket", "Message": "no"},
        "Other": 1}))
    with pytest.raises(BrokerError) as e:
        b._req("POST", "/trade/v2/orders", body={"a": 1})
    assert str(e.value) == ('Saxo POST /trade/v2/orders: HTTP 400 '
                            '{"ErrorCode": "TooFarFromMarket", '
                            '"Message": "no"}')
    assert type(e.value) is BrokerError


def test_broker_error_text_is_the_whole_json_when_there_is_no_error_info():
    b = broker(lambda r: httpx.Response(404, json={
        "Message": "bad", "ModelState": {"Uic": ["?"]}}))
    with pytest.raises(BrokerError) as e:
        b._req("DELETE", "/trade/v2/orders/9/?AccountKey=KEY==")
    # the query string (it holds the account key) is not in the message
    assert str(e.value) == ('Saxo DELETE /trade/v2/orders/9/: HTTP 404 '
                            '{"Message": "bad", "ModelState": {"Uic": ["?"]}}')


def test_broker_error_text_when_the_body_is_empty_or_not_json():
    b = broker(lambda r: httpx.Response(503))
    with pytest.raises(BrokerError) as e:
        b._req("GET", "/port/v1/balances/me")
    assert str(e.value) == 'Saxo GET /port/v1/balances/me: HTTP 503 ""'
    page = "<html>" + "x" * 500
    b = broker(lambda r: httpx.Response(502, text=page))
    with pytest.raises(BrokerError) as e:
        b._req("GET", "/port/v1/orders/me?$top=200")
    # the body is cut to 300 characters, and the quoted result to 300 again
    assert str(e.value) == ("Saxo GET /port/v1/orders/me: HTTP 502 "
                            + json.dumps(page[:300])[:300])
    assert len(str(e.value)) == len("Saxo GET /port/v1/orders/me: HTTP 502 ") \
        + 300


def test_broker_long_error_info_is_cut_to_300_characters():
    info = {"ErrorCode": "X", "Message": "m" * 400}
    b = broker(lambda r: httpx.Response(400, json={"ErrorInfo": info}))
    with pytest.raises(BrokerError) as e:
        b._req("POST", "/trade/v2/orders", body={})
    assert str(e.value) == ("Saxo POST /trade/v2/orders: HTTP 400 "
                            + json.dumps(info)[:300])


def test_broker_json_error_body_that_is_not_an_object_is_not_a_broker_error():
    """Recorded, not wished for: an error body that is JSON but not an
    object (Saxo has never been seen to send one) has no .get, so the
    adapter lets an AttributeError out instead of a BrokerError. A
    placement then counts as "unknown" rather than refused — the cautious
    side. Left as it was when the wrappers were unified."""
    b = broker(lambda r: httpx.Response(400, json=["no"]))
    with pytest.raises(AttributeError):
        b._req("POST", "/trade/v2/orders", body={})


def test_broker_never_puts_the_token_in_an_error():
    for resp in (httpx.Response(401, json={"ErrorInfo": {"ErrorCode": "x"}}),
                 httpx.Response(500, text="boom"), httpx.Response(429)):
        b = broker(lambda r, resp=resp: resp)
        with pytest.raises(BrokerError) as e:
            b._req("POST", "/trade/v2/orders", body={})
        assert TOKEN not in str(e.value) and TOKEN not in repr(e.value)
        assert "Bearer" not in str(e.value)


def test_broker_empty_answer_reads_as_an_empty_object():
    assert broker(lambda r: httpx.Response(204))._req(
        "DELETE", "/trade/v2/orders/1/?AccountKey=K") == {}
    assert broker(lambda r: httpx.Response(200))._req(
        "GET", "/port/v1/balances/me") == {}
    assert broker(lambda r: httpx.Response(204)).cancel("1") is None
    with pytest.raises(ValueError):             # a 200 that is not JSON
        broker(lambda r: httpx.Response(200, text="<html>"))._req(
            "GET", "/port/v1/balances/me")


def test_broker_network_failure_comes_out_as_it_is():
    """Not a BrokerError: the caller must be able to tell "Saxo refused"
    (certain) from "nobody knows whether the order arrived"."""
    def offline(req):
        raise httpx.ConnectError("offline")
    b = broker(lambda r: saxo_ref(r) or offline(r))
    with pytest.raises(httpx.ConnectError, match="offline"):
        b._req("GET", "/port/v1/balances/me")
    with pytest.raises(httpx.ConnectError):
        b.place_bracket(symbol="AAPL", side="buy", qty=1, limit=10)


def test_broker_placement_4xx_is_refused_429_and_5xx_are_unknown():
    def answering(status):
        return lambda r: saxo_ref(r) or httpx.Response(
            status, json={"ErrorInfo": {"ErrorCode": "E"}})
    p = broker(answering(404)).place_bracket(symbol="AAPL", side="buy",
                                             qty=1, limit=10)
    assert p.status == "rejected" and p.detail == \
        'Saxo POST /trade/v2/orders: HTTP 404 {"ErrorCode": "E"}'
    p = broker(answering(400)).place_oco(symbol="AAPL", side="sell", qty=1,
                                         stop_loss=9)
    assert p.status == "rejected" and p.detail == \
        'Saxo POST /trade/v2/orders: HTTP 400 {"ErrorCode": "E"}'
    for status in (429, 500, 503):
        with pytest.raises(BrokerError, match=f"HTTP {status}"):
            broker(answering(status)).place_bracket(
                symbol="AAPL", side="buy", qty=1, limit=10)
        with pytest.raises(BrokerError, match=f"HTTP {status}"):
            broker(answering(status)).place_oco(
                symbol="AAPL", side="sell", qty=1, stop_loss=9)
    out = broker(answering(400)).precheck(symbol="AAPL", side="buy", qty=1,
                                          limit=10)
    assert out == {"ok": False, "error": 'Saxo POST /trade/v2/orders/precheck'
                                         ': HTTP 400 {"ErrorCode": "E"}'}


def test_broker_spaces_every_changing_order_call_and_no_read():
    naps, now = [], {"t": 100.0}
    handler, seen = recorder()
    b = broker(handler, order_gap_s=1.1,
               sleep=lambda s: naps.append(round(s, 2)),
               monotonic=lambda: now["t"])
    b._req("POST", "/trade/v2/orders", body={})         # first: no wait
    b._req("POST", "/trade/v2/orders/precheck", body={})
    b._req("PATCH", "/trade/v2/orders", body={})
    b._req("DELETE", "/trade/v2/orders/1/?AccountKey=K")
    assert naps == [1.1, 1.1, 1.1]
    b._req("GET", "/trade/v2/orders/1")                 # reads never wait
    b._req("GET", "/port/v1/orders/me")
    b._req("POST", "/port/v1/anything", body={})        # nor other paths
    assert naps == [1.1, 1.1, 1.1] and len(seen) == 7


def test_broker_waits_first_and_asks_for_the_token_after_the_wait():
    """The token is fetched after the pause, so a pause can never make the
    request go out with a token that was read before it."""
    events, now = [], {"t": 100.0}

    def token():
        events.append("token")
        return TOKEN

    def handler(req):
        events.append("send")
        return httpx.Response(200, json={})
    b = SaxoBroker("sim", token=token, account_key="K", order_gap_s=1.1,
                   client=httpx.Client(transport=httpx.MockTransport(handler)),
                   sleep=lambda s: events.append("sleep"),
                   monotonic=lambda: now["t"])
    b.cancel("1")
    b.cancel("2")
    assert events == ["token", "send", "sleep", "token", "send"]


def test_broker_refuses_an_unknown_environment():
    with pytest.raises(BrokerError, match="unknown Saxo environment 'prod'"):
        SaxoBroker("prod", token=lambda: TOKEN, account_key="K")


# ═════════════════════════════════════════════════════════════════════════
# Part 1b — the read-only account view (saxo_live._get)
# ═════════════════════════════════════════════════════════════════════════
ENV = {"SAXO_SIM_APP_KEY": "s", "SAXO_LIVE_APP_KEY": "l"}


@pytest.fixture
def tok(tmp_path):
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({
        "access_token": TOKEN, "refresh_token": "R1",
        "access_expires_at": T0 + 1200, "refresh_expires_at": T0 + 2400,
        "code_verifier": "v", "redirect_uri": "http://localhost/claire-saxo"}))
    return path


def live_get(what, which, handler, tok):
    return saxo_live._get(
        what, which, env=ENV, clock=lambda: T0, path=tok,
        client=httpx.Client(transport=httpx.MockTransport(handler)))


READS = {
    "me": "/port/v1/users/me",
    "accounts": "/port/v1/accounts/me",
    "balances": "/port/v1/balances/me",
    "positions": "/port/v1/netpositions/me?FieldGroups="
                 "NetPositionBase,NetPositionView,DisplayAndFormat&$top=500",
    "orders": "/port/v1/orders/me?FieldGroups=DisplayAndFormat&$top=200",
    "openings": "/port/v1/positions/me?FieldGroups=PositionBase&$top=500",
}


@pytest.mark.parametrize("which,base", [("sim", SIM), ("live", LIVE)])
def test_live_view_reads_are_bare_gets_on_the_right_gateway(which, base, tok):
    assert set(saxo_live.READ_PATHS) == set(READS)
    for what, path in READS.items():
        handler, seen = recorder(lambda r: httpx.Response(
            200, json={"Data": [1]}))
        assert live_get(what, which, handler, tok) == {"Data": [1]}
        assert seen == [{"method": "GET", "url": base + path,
                         "headers": bare(), "body": b""}]


def test_live_view_401_says_sign_in_again(tok):
    with pytest.raises(saxo_live.NotSignedIn) as e:
        live_get("balances", "sim", lambda r: httpx.Response(
            401, json={"ErrorInfo": {"ErrorCode": "InvalidToken"}}), tok)
    assert str(e.value) == \
        "Saxo rejected the sim token — run bin/saxo login sim"
    with pytest.raises(saxo_live.NotSignedIn) as e:
        live_get("positions", "live", lambda r: httpx.Response(401), tok)
    assert str(e.value) == \
        "Saxo rejected the live token — run bin/saxo login live"


def test_live_view_other_errors_quote_the_body_as_saxo_sent_it(tok):
    raw = '{"ErrorInfo": {"ErrorCode": "Nope"}}'
    with pytest.raises(saxo_live.SaxoError) as e:
        live_get("balances", "sim", lambda r: httpx.Response(
            403, text=raw), tok)
    assert str(e.value) == f"Saxo balances: HTTP 403 {raw}"
    assert type(e.value) is saxo_live.SaxoError     # not "sign in again"
    page = "<html>" + "y" * 400
    with pytest.raises(saxo_live.SaxoError) as e:
        live_get("positions", "live", lambda r: httpx.Response(
            500, text=page), tok)
    assert str(e.value) == f"Saxo positions: HTTP 500 {page[:200]}"
    with pytest.raises(saxo_live.SaxoError) as e:
        live_get("orders", "sim", lambda r: httpx.Response(429), tok)
    assert str(e.value) == "Saxo orders: HTTP 429 "
    assert TOKEN not in str(e.value)


def test_live_view_summary_turns_a_saxo_error_into_an_honest_reason(tok):
    out = saxo_live.summary(
        "sim", env=ENV, clock=lambda: T0, path=tok,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(503, text="down"))))
    assert out["error"] == "Saxo balances: HTTP 503 down"
    out = saxo_live.summary(
        "sim", env=ENV, clock=lambda: T0, path=tok,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(401))))
    assert out["error"] == \
        "Saxo rejected the sim token — run bin/saxo login sim"


def test_live_view_empty_answer_and_network_failure_are_not_saxo_errors(tok):
    """Recorded as it is today: a 200 with nothing in it is a decoding
    error, and a network failure is httpx's own error — neither is a
    SaxoError, so neither is reported as "Saxo said no"."""
    with pytest.raises(ValueError) as e:
        live_get("balances", "sim", lambda r: httpx.Response(200), tok)
    assert not isinstance(e.value, saxo_live.SaxoError)

    def offline(req):
        raise httpx.ConnectError("offline")
    with pytest.raises(httpx.ConnectError, match="offline"):
        live_get("balances", "sim", offline, tok)


def test_live_view_only_reads_what_is_on_its_list(tok):
    handler, seen = recorder()
    for what in ("orders/../../trade/v2/orders", "/trade/v2/orders", ""):
        with pytest.raises(saxo_live.SaxoError,
                           match="is not a read-only Saxo endpoint"):
            live_get(what, "sim", handler, tok)
    with pytest.raises(saxo_live.SaxoError, match="unknown Saxo environment"):
        live_get("balances", "prod", handler, tok)
    assert seen == []                           # nothing was sent


# ═════════════════════════════════════════════════════════════════════════
# Part 1c — the chart reader (saxo_chart._get)
# ═════════════════════════════════════════════════════════════════════════
@pytest.fixture
def chart_env(monkeypatch):
    """The chart reader with its sign-in and its pacing clock replaced:
    `events` lists, in order, each pause, each token read (with the
    environment asked for) and nothing else."""
    events, now = [], {"t": 1000.0}

    def access_token(which):
        events.append(("token", which))
        return TOKEN

    def sleep(s):
        events.append(("sleep", round(s, 2)))
        now["t"] += s
    monkeypatch.setattr(saxo_live, "access_token", access_token)
    monkeypatch.setattr(saxo_chart, "time", types.SimpleNamespace(
        monotonic=lambda: now["t"], sleep=sleep))
    monkeypatch.setattr(saxo_chart, "_last", [0.0])
    monkeypatch.setattr(saxo_chart, "_uic", {})
    monkeypatch.delenv("CLAIRE_SAXO_CHART_ENV", raising=False)
    return events


def chart_saxo(req):
    if req.url.path.endswith("/ref/v1/instruments/"):
        return httpx.Response(200, json={"Data": [
            {"Symbol": "D05:xses", "Identifier": 44, "AssetType": "CfdOnStock",
             "CurrencyCode": "SGD", "Description": "DBS"},
            {"Symbol": "IBM:xnys", "Identifier": 7, "AssetType": "Stock",
             "CurrencyCode": "USD", "Description": "IBM Corp."}]})
    return httpx.Response(200, json={"Data": [
        {"Time": "2026-11-12T00:00:00Z", "Open": 1, "High": 2, "Low": 0.5,
         "Close": 1.5, "Volume": 10}]})


def test_chart_reader_sends_its_search_and_its_bars_request(chart_env):
    handler, seen = recorder(chart_saxo)
    out = saxo_chart.chart("D05.SI", "1mo", "1d", client=httpx.Client(
        transport=httpx.MockTransport(handler)))
    assert out["close"] == [1.5] and out["src"] == "saxo-cfd"
    assert seen == [
        {"method": "GET", "headers": bare(), "body": b"",
         "url": SIM + "/ref/v1/instruments/?Keywords=D05&AssetTypes="
                      "Stock%2CEtf%2CCfdOnStock%2CCfdOnEtf&%24top=50"},
        {"method": "GET", "headers": bare(), "body": b"",
         "url": SIM + "/chart/v3/charts?Uic=44&AssetType=CfdOnStock"
                      "&Horizon=1440&Count=22"}]
    # paced: the second request waited out the half second, and each
    # request read its token after its pause
    assert chart_env == [("token", "sim"), ("sleep", 0.5), ("token", "sim")]


def test_chart_reader_search_request_and_the_live_setting(chart_env,
                                                          monkeypatch):
    monkeypatch.setenv("CLAIRE_SAXO_CHART_ENV", "live")
    handler, seen = recorder(chart_saxo)
    hits = saxo_chart.search("ibm corp", client=httpx.Client(
        transport=httpx.MockTransport(handler)))
    assert [h["symbol"] for h in hits] == ["D05.SI", "IBM"]
    assert seen == [{"method": "GET", "headers": bare(), "body": b"",
                     "url": LIVE + "/ref/v1/instruments/?Keywords=ibm+corp"
                                   "&AssetTypes=Stock%2CEtf&%24top=50"}]
    assert chart_env == [("token", "live")]


def test_chart_reader_error_names_the_path_and_the_status_only(chart_env):
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(500, json={"ErrorInfo": {"ErrorCode": "x"}})))
    with pytest.raises(saxo_chart.SaxoChartError) as e:
        saxo_chart._get("/chart/v3/charts", {"Uic": 1}, which="sim", client=c)
    assert str(e.value) == "Saxo /chart/v3/charts: HTTP 500"
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, text="who?")))
    with pytest.raises(saxo_chart.SaxoChartError) as e:
        saxo_chart.search("x", client=c)
    assert str(e.value) == "Saxo /ref/v1/instruments/: HTTP 401"
    assert type(e.value) is saxo_chart.SaxoChartError
    assert TOKEN not in str(e.value)


def test_chart_reader_empty_answer_and_network_failure_come_out_as_they_are(
        chart_env):
    c = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200)))
    with pytest.raises(ValueError) as e:
        saxo_chart._get("/chart/v3/charts", {"Uic": 1}, which="sim", client=c)
    assert not isinstance(e.value, saxo_chart.SaxoChartError)

    def offline(req):
        raise httpx.ConnectError("offline")
    with pytest.raises(httpx.ConnectError):
        saxo_chart._get("/chart/v3/charts", {"Uic": 1}, which="sim",
                        client=httpx.Client(
                            transport=httpx.MockTransport(offline)))


# ═════════════════════════════════════════════════════════════════════════
# Part 2 — the shared core (saxo_http.send)
# ═════════════════════════════════════════════════════════════════════════
def http_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("env,base", [("sim", SIM), ("live", LIVE)])
def test_core_sends_to_the_named_gateway_and_decodes_the_answer(env, base):
    handler, seen = recorder(lambda r: httpx.Response(200, json={"a": [1]}))
    out = saxo_http.send(http_client(handler), env, "GET",
                         "/port/v1/balances/me?x=1&$top=2", TOKEN)
    assert out == {"a": [1]}
    assert seen == [{"method": "GET", "headers": bare(), "body": b"",
                     "url": base + "/port/v1/balances/me?x=1&$top=2"}]


def test_core_sends_a_json_body_params_and_extra_headers():
    handler, seen = recorder()
    saxo_http.send(http_client(handler), "sim", "POST", "/trade/v2/orders",
                   TOKEN, json={"Uic": 211}, headers={"x-request-id": "r1"})
    saxo_http.send(http_client(handler), "sim", "GET", "/ref/v1/instruments/",
                   TOKEN, params={"Keywords": "a b", "$top": 50})
    post, get = seen
    assert post["method"] == "POST" and post["url"] == SIM + "/trade/v2/orders"
    assert json.loads(post["body"]) == {"Uic": 211}
    assert post["headers"] == with_json(post["body"], {"x-request-id": "r1"})
    assert get == {"method": "GET", "headers": bare(), "body": b"",
                   "url": SIM + "/ref/v1/instruments/?Keywords=a+b&%24top=50"}


@pytest.mark.parametrize("env", ["prod", "LIVE", "", None])
def test_core_refuses_an_unknown_environment_before_sending(env):
    """Anything that is not exactly "sim" or "live" is an error — never a
    request that quietly goes to the real-money gateway."""
    handler, seen = recorder()
    with pytest.raises(ValueError, match="unknown Saxo environment"):
        saxo_http.send(http_client(handler), env, "GET", "/port/v1/users/me",
                       TOKEN)
    assert seen == []


def test_core_error_carries_the_status_and_what_saxo_said():
    c = http_client(lambda r: httpx.Response(400, json={
        "ErrorInfo": {"ErrorCode": "TooFarFromMarket"}}))
    with pytest.raises(saxo_http.SaxoHTTPError) as e:
        saxo_http.send(c, "sim", "POST", "/trade/v2/orders?AccountKey=K==",
                       TOKEN, json={})
    err = e.value
    assert (err.status, err.method, err.path) == \
        (400, "POST", "/trade/v2/orders")       # no query string
    assert err.is_json and err.body == {
        "ErrorInfo": {"ErrorCode": "TooFarFromMarket"}}
    assert json.loads(err.text) == err.body
    assert str(err) == "Saxo POST /trade/v2/orders: HTTP 400"

    c = http_client(lambda r: httpx.Response(503, text="<html>down"))
    with pytest.raises(saxo_http.SaxoHTTPError) as e:
        saxo_http.send(c, "live", "GET", "/port/v1/balances/me", TOKEN)
    err = e.value
    assert (err.status, err.is_json, err.body, err.text) == \
        (503, False, None, "<html>down")
    assert str(err) == "Saxo GET /port/v1/balances/me: HTTP 503"

    c = http_client(lambda r: httpx.Response(429))
    with pytest.raises(saxo_http.SaxoHTTPError) as e:
        saxo_http.send(c, "sim", "DELETE", "/trade/v2/orders/1/", TOKEN)
    assert (e.value.status, e.value.is_json, e.value.text) == (429, False, "")


def test_core_json_null_error_body_is_json_not_missing():
    c = http_client(lambda r: httpx.Response(500, text="null"))
    with pytest.raises(saxo_http.SaxoHTTPError) as e:
        saxo_http.send(c, "sim", "GET", "/port/v1/users/me", TOKEN)
    assert e.value.is_json and e.value.body is None


def test_core_empty_answer_is_an_error_unless_the_caller_says_it_is_fine():
    c = http_client(lambda r: httpx.Response(204))
    with pytest.raises(ValueError):
        saxo_http.send(c, "sim", "GET", "/port/v1/users/me", TOKEN)
    assert saxo_http.send(c, "sim", "DELETE", "/trade/v2/orders/1/", TOKEN,
                          empty_ok=True) == {}
    # empty_ok is about an EMPTY answer only: junk is still an error
    c = http_client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ValueError):
        saxo_http.send(c, "sim", "GET", "/port/v1/users/me", TOKEN,
                       empty_ok=True)


def test_core_leaves_a_network_failure_as_httpx_raised_it():
    def offline(req):
        raise httpx.ReadTimeout("slow")
    with pytest.raises(httpx.ReadTimeout, match="slow"):
        saxo_http.send(http_client(offline), "sim", "GET",
                       "/port/v1/users/me", TOKEN)


def test_the_token_is_never_in_an_error_or_a_log_line(caplog, tok, chart_env):
    """Through the core and through each wrapper, on success and on
    failure, with every logger listening at its most talkative."""
    caplog.set_level(0)
    errors = []

    def attempt(call):
        try:
            call()
        except Exception as e:                  # noqa: BLE001 — collected
            errors.append(e)
    for respond in (lambda r: httpx.Response(200, json={"Data": []}),
                    lambda r: httpx.Response(401, text="bad token"),
                    lambda r: httpx.Response(500, json={"ErrorInfo": {}}),
                    lambda r: httpx.Response(200, text="not json")):
        c = http_client(respond)
        attempt(lambda: saxo_http.send(c, "sim", "POST", "/trade/v2/orders",
                                       TOKEN, json={}))
        attempt(lambda: broker(respond).cancel("1"))
        attempt(lambda: broker(respond).snapshot())
        attempt(lambda: live_get("balances", "sim", respond, tok))
        attempt(lambda: saxo_chart.search("x", client=c))
    assert len(errors) >= 12
    for e in errors:
        # the error itself and the one it was raised in place of, if any
        for x in (e, e.__context__):
            shown = f"{x} {x!r} {getattr(x, '__dict__', {})!r}"
            assert TOKEN not in shown and "Bearer" not in shown
    assert TOKEN not in caplog.text and "Bearer" not in caplog.text
    assert not [r for r in caplog.records
                if r.name == "app.tools.saxo_http"]     # the core is silent


# ═════════════════════════════════════════════════════════════════════════
# Part 3 — the lock around the order throttle
# ═════════════════════════════════════════════════════════════════════════
class FakeClock:
    """A clock for the broker's injectable `sleep` / `monotonic` that
    behaves like real time when several threads use it: a pause ends `s`
    after the moment that thread last read the clock, so two threads that
    pause side by side wake at the SAME time (the back-to-back sends the
    lock is there to prevent). `read` is the time each thread last read —
    for an order call, the moment the throttle let it go. A thread named in
    `hold` stays in its pause until `wake` is set, so a test can look at
    what the other threads do meanwhile."""

    def __init__(self, t=100.0, hold=()):
        self.t, self.hold = t, set(hold)
        self.naps, self.read = [], {}
        self._mutex = threading.Lock()
        self.pausing = threading.Event()
        self.wake = threading.Event()

    def monotonic(self):
        with self._mutex:
            now = self.read[threading.current_thread().name] = self.t
        # step aside between reading the time and acting on it: exactly
        # the gap another thread slips into when nothing stops it
        time.sleep(0.001)
        return now

    def sleep(self, s):
        me = threading.current_thread().name
        with self._mutex:
            self.naps.append(round(s, 2))
        self.pausing.set()
        if me in self.hold:
            assert self.wake.wait(5)
        with self._mutex:
            self.t = max(self.t, self.read[me] + s)


def test_two_threads_cannot_both_read_the_old_time_and_send_back_to_back():
    clock = FakeClock(hold={"A"})
    handler, seen = recorder()
    b = broker(handler, order_gap_s=1.1, sleep=clock.sleep,
               monotonic=clock.monotonic)
    b.cancel("0")                       # the last order call: t = 100
    ta = threading.Thread(target=b.cancel, args=("1",), name="A")
    tb = threading.Thread(target=b.cancel, args=("2",), name="B")
    ta.start()
    assert clock.pausing.wait(5)        # A is pausing, inside the throttle
    tb.start()
    tb.join(0.2)
    # B has to be waiting for A's turn to end. Had it been able to read the
    # old time (100) it would have paused its own 1.1 s side by side with A
    # and already be finished.
    waiting = tb.is_alive()
    clock.wake.set()
    ta.join(5)
    tb.join(5)
    assert not ta.is_alive() and not tb.is_alive()
    assert waiting
    # A went at 101.1; B then took its OWN pause after A: 102.2
    assert clock.read["A"] == pytest.approx(101.1)
    assert clock.read["B"] == pytest.approx(102.2)
    assert clock.naps == [1.1, 1.1]
    assert [s["method"] for s in seen] == ["DELETE"] * 3


def test_many_threads_at_once_each_get_their_own_gap():
    clock = FakeClock()
    handler, seen = recorder()
    b = broker(handler, order_gap_s=1.1, sleep=clock.sleep,
               monotonic=clock.monotonic)
    start = threading.Barrier(8)

    def go(i):
        start.wait(5)
        b._req("POST", "/trade/v2/orders", body={"n": i})
    threads = [threading.Thread(target=go, args=(i,), name=f"t{i}")
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads) and len(seen) == 8
    went = sorted(clock.read.values())
    assert len(went) == 8
    assert went == pytest.approx([100 + 1.1 * k for k in range(8)])
    assert clock.naps == [1.1] * 7


def test_a_pause_on_one_broker_does_not_hold_another_broker_back():
    """SIM and live are separate SaxoBroker objects with separate sessions
    at Saxo: each paces itself, and a paused SIM order must not delay a
    live one."""
    clock = FakeClock(hold={"A"})
    handler, seen = recorder()
    sim = broker(handler, "sim", order_gap_s=1.1, sleep=clock.sleep,
                 monotonic=clock.monotonic)
    live = broker(handler, "live", order_gap_s=1.1, sleep=clock.sleep,
                  monotonic=clock.monotonic)
    sim.cancel("0")
    ta = threading.Thread(target=sim.cancel, args=("1",), name="A")
    ta.start()
    assert clock.pausing.wait(5)        # the SIM broker is mid-pause
    live.cancel("9")                    # returns at once, on this thread
    assert seen[-1]["url"].startswith(LIVE + "/trade/v2/orders/9/")
    assert ta.is_alive()
    clock.wake.set()
    ta.join(5)
    assert not ta.is_alive() and len(seen) == 3


def test_reads_are_not_held_up_by_a_paused_order_call():
    clock = FakeClock(hold={"A"})
    handler, seen = recorder()
    b = broker(handler, order_gap_s=1.1, sleep=clock.sleep,
               monotonic=clock.monotonic)
    b.cancel("0")
    ta = threading.Thread(target=b.cancel, args=("1",), name="A")
    ta.start()
    assert clock.pausing.wait(5)
    b.snapshot()                        # a read: no throttle, no lock
    assert seen[-1]["method"] == "GET" and ta.is_alive()
    clock.wake.set()
    ta.join(5)
    assert not ta.is_alive()


def test_the_throttle_lets_go_of_its_lock_when_the_pause_fails():
    """If the pause itself raises, the next order call must not hang."""
    naps = []

    def sleep(s):
        naps.append(s)
        if len(naps) == 1:
            raise KeyboardInterrupt
    b = broker(recorder()[0], order_gap_s=1.1, sleep=sleep,
               monotonic=lambda: 100.0)
    b.cancel("0")
    with pytest.raises(KeyboardInterrupt):
        b.cancel("1")
    done = threading.Thread(target=b.cancel, args=("2",))
    done.start()
    done.join(5)
    assert not done.is_alive() and len(naps) == 2
