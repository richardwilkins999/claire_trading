"""Suite-wide guards. The default provider is the Claude Agent SDK on the
Max login, so without this any test that reaches the real registry would
spend subscription quota and depend on this machine being signed in."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_web_search(monkeypatch):
    """web_search with no injected client would ask Tavily or the news
    feeds for real. A test that wants search passes `_client` / `search_fn`."""
    from app.tools import search

    def blocked():
        raise search.SearchError("offline test: real web search is blocked")
    monkeypatch.setattr(search, "_search_client", blocked)


@pytest.fixture(autouse=True)
def _no_real_claude_sdk(monkeypatch):
    import claude_agent_sdk

    from app.providers import claude_sdk

    def blocked(*a, **k):
        raise claude_sdk.SdkError("offline test: real Claude SDK calls are "
                                  "blocked")
    monkeypatch.setattr(claude_agent_sdk, "query", blocked)
    # deterministic regardless of whether this machine is signed in
    monkeypatch.setattr(claude_sdk, "not_ready",
                        lambda home=None: "not signed in to Claude (test)")


@pytest.fixture(autouse=True)
def _no_real_saxo_files(monkeypatch, tmp_path):
    """Tests must never read or rotate the real Saxo sign-ins in etc/ (or
    write the real alert state in var/): every token and alert file path is
    redirected into the test's own temp directory."""
    from app.tools import saxo_live
    monkeypatch.setattr(saxo_live, "token_file",
                        lambda which: tmp_path / f"saxo_{which}_token.json")
    monkeypatch.setattr(saxo_live, "alert_state_file",
                        lambda which: tmp_path / f"saxo_{which}_alert.json")
    monkeypatch.setattr(saxo_live, "TOKEN_FILE",
                        tmp_path / "saxo_default_token.json")


@pytest.fixture(autouse=True)
def _no_real_telegram_file(monkeypatch, tmp_path):
    """The system map reads whether Telegram is set up: never from the real
    ~/bob/telegram.env in a test."""
    from app import alerts
    monkeypatch.setattr(alerts, "TELEGRAM_ENV", tmp_path / "telegram.env")


@pytest.fixture(autouse=True)
def _no_real_desktop_notifications(monkeypatch):
    """alerts.send also runs notify-send. A test that called it popped a real
    "Claire: hello" (then "x") onto Richard's desktop on EVERY test run
    (2026-11-06). Record instead."""
    from app import alerts

    class _Done:
        returncode = 0
    sent = []
    real_run = alerts.subprocess.run    # the shared module: pass others on

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "notify-send":
            sent.append(list(cmd))
            return _Done()
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(alerts.subprocess, "run", fake_run)
    return sent
