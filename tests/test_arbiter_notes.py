"""Arbiter's notes in plain English (app/arbiter_notes.py). Offline: a fake
LLM. Text only — the module must never be able to touch an order."""
import json

import pytest

from app import arbiter_notes as an
from app.accounting import db
from app.accounting.repo import Repo

NOTES = ["Signal bar volume 0.28x 20d avg = NOT volume-confirmed.",
         "Fundamental blackout: CET1, ROTE unretrieved."]


class Reply:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, []

    def invoke(self, msgs):
        self.calls.append(msgs)
        return Reply(self.text)


GOOD = json.dumps({"summary": "Low conviction: weak volume, no financials.",
                   "notes": ["Today's rise came on about a quarter of normal "
                             "trading, so few buyers backed it.",
                             "The bank's capital (CET1) and profitability "
                             "(ROTE) were not checked."]})


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    Repo(c).create_work_item("wi_1", "pipeline", "DBK", ts=1)
    c.execute("UPDATE work_items SET thesis_json=? WHERE id='wi_1'",
              (json.dumps({"direction": "buy", "conditions": NOTES}),))
    return c


def test_explains_each_note_and_caches(conn):
    llm = FakeLLM("```json\n" + GOOD + "\n```")
    out = an.explain(conn, "wi_1", llm_factory=lambda: llm)
    assert out["summary"].startswith("Low conviction")
    assert [n["original"] for n in out["notes"]] == NOTES
    assert "quarter of normal" in out["notes"][0]["plain"]
    assert "DBK" in llm.calls[0][1][1] and NOTES[1] in llm.calls[0][1][1]
    again = an.explain(conn, "wi_1", llm_factory=lambda: 1 / 0)
    assert again == out and len(llm.calls) == 1        # stored, not re-asked
    assert an.cached(conn, "wi_1", NOTES) == out


def test_a_bad_answer_is_not_stored(conn):
    with pytest.raises(ValueError, match="expected 2"):
        an.explain(conn, "wi_1", llm_factory=lambda: FakeLLM(
            json.dumps({"summary": "x", "notes": ["only one"]})))
    with pytest.raises(ValueError, match="no JSON"):
        an.explain(conn, "wi_1", llm_factory=lambda: FakeLLM("sorry"))
    assert an.cached(conn, "wi_1", NOTES) is None


def test_changed_notes_are_explained_afresh(conn):
    an.explain(conn, "wi_1", llm_factory=lambda: FakeLLM(GOOD))
    conn.execute("UPDATE work_items SET thesis_json=? WHERE id='wi_1'",
                 (json.dumps({"conditions": NOTES[:1]}),))
    llm = FakeLLM(json.dumps({"summary": "s", "notes": ["one"]}))
    out = an.explain(conn, "wi_1", llm_factory=lambda: llm)
    assert len(llm.calls) == 1 and out["notes"][0]["plain"] == "one"


def test_no_notes_needs_no_call(conn):
    Repo(conn).create_work_item("wi_2", "pipeline", "X", ts=1)
    assert an.explain(conn, "wi_2", llm_factory=lambda: 1 / 0) == \
        {"summary": None, "notes": []}


def test_written_ahead_for_waiting_cards_and_failures_back_off(conn):
    conn.execute("UPDATE work_items SET state='awaiting_approval'")
    t = [1000]
    failed = {}
    bad = an.write_ahead_once(conn, failed=failed, clock=lambda: t[0],
                              llm_factory=lambda: FakeLLM("nope"))
    assert bad == [] and "wi_1" in failed
    llm = FakeLLM(GOOD)
    assert an.write_ahead_once(conn, failed=failed, clock=lambda: t[0],
                               llm_factory=lambda: llm) == []
    assert llm.calls == []                             # backing off
    t[0] += an.RETRY_AFTER_S + 1
    assert an.write_ahead_once(conn, failed=failed, clock=lambda: t[0],
                               llm_factory=lambda: llm) == ["wi_1"]
    assert an.pending(conn) == []                      # nothing left to write
