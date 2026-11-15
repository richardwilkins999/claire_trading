"""One way to send a request to Saxo's OpenAPI gateway.

The desk talks to Saxo from three places, each with its own rules: the
broker adapter (saxo_broker: orders, paced, behind the live-trading
switch), the read-only account view (saxo_live) and the chart reader
(saxo_chart). They used to build the address, the sign-in header and the
error handling three times over (REVIEW-2026-11-11 §3.1). This is the part
they share, and nothing more:

    send(client, environment, method, path, token, ...)

puts one request on the wire and returns Saxo's decoded answer, or raises
SaxoHTTPError when Saxo answers with an error status.

What is deliberately NOT here, because each caller's rule differs and each
keeps its own: pacing, the live-trading guard, which paths may be called,
where the token comes from, retries (nobody retries), timeouts (they belong
to the httpx client the caller hands in) and the words of the error the
caller's own callers see. A network failure is not turned into anything:
httpx's own exception comes out as it is, so an adapter can still tell
"Saxo refused" (certain) from "nobody knows whether it arrived".

The bearer token goes into the request header and nowhere else: it is not
kept on the error, and nothing here logs.
"""

# The two gateways. A SIM token only works on the first and a live token
# only on the second; which one a request goes to is always the caller's
# explicit choice, never a default.
API = {"sim": "https://gateway.saxobank.com/sim/openapi",
       "live": "https://gateway.saxobank.com/openapi"}

_NOT_JSON = object()


class SaxoHTTPError(Exception):
    """Saxo answered with an HTTP error status (400 or above).

    status   the HTTP status code
    method   the verb that was sent
    path     the path that was asked for, WITHOUT its query string (a query
             can hold an account key; it has no business in a message)
    text     the body of the answer as text ('' when there was none)
    is_json  whether that body was JSON
    body     the decoded JSON body (None when it was not JSON)

    Between them `text` and `body` are everything Saxo said about what went
    wrong (its ErrorInfo object, when it sends one, is body["ErrorInfo"]).
    The message is "Saxo <method> <path>: HTTP <status>" and never holds
    the token. Each wrapper turns this into its own exception, quoting as
    much of Saxo's answer as it always has."""

    def __init__(self, method, path, status, text, body=_NOT_JSON):
        self.method = method
        self.path = path.split("?")[0]
        self.status = status
        self.text = text
        self.is_json = body is not _NOT_JSON
        self.body = body if self.is_json else None
        super().__init__(f"Saxo {method} {self.path}: HTTP {status}")


def send(client, environment, method, path, token, *, json=None, params=None,
         headers=None, empty_ok=False):
    """Send one request to Saxo and return the decoded JSON answer.

    client       an httpx.Client — its timeout and connection pool apply
    environment  "sim" or "live": which gateway (anything else is refused
                 before a byte is sent, never quietly treated as live)
    path         the OpenAPI path, e.g. "/port/v1/balances/me"; a query
                 string written into it is sent exactly as written
    token        the bearer token, sent as "Authorization: Bearer <token>"
    json         a body to send as JSON (None: no body, no content-type)
    params       query parameters for httpx to encode onto the address
    headers      extra headers (e.g. x-request-id), after Authorization
    empty_ok     an answer with no body reads as {} — Saxo answers some
                 order calls with nothing; without this an empty answer is
                 a decoding error (ValueError), as any non-JSON answer is

    Raises SaxoHTTPError for a status of 400 or above. Network failures
    and timeouts come out as httpx raised them."""
    if environment not in API:
        raise ValueError(f"unknown Saxo environment {environment!r}")
    r = client.request(method, API[environment] + path, json=json,
                       params=params,
                       headers={"Authorization": f"Bearer {token}",
                                **(headers or {})})
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = _NOT_JSON
        raise SaxoHTTPError(method, path, r.status_code, r.text, body)
    if empty_ok and not r.content:
        return {}
    return r.json()
