import copy
import json
import os
import re

import pytest
from markupsafe import escape as _escape
from sqlalchemy.exc import OperationalError

from ser_app import SerLead, create_app, db
from ser_app.scoring import DEFAULT_CONTENT_PATH, ContentError, load_content, score

CONTENT = load_content(DEFAULT_CONTENT_PATH)
QIDS = [q["id"] for q in CONTENT["questions"]]


def html_escape(text):
    return str(_escape(text))


def answers_for(n_yes, n_partly=0, gate_no=False):
    """Build answers: first gates (yes unless gate_no), then fill yes/partly/no in order."""
    ans = {}
    remaining = list(QIDS)
    plan = ["yes"] * n_yes + ["partly"] * n_partly
    plan += ["no"] * (len(QIDS) - len(plan))
    for qid, a in zip(remaining, plan):
        ans[qid] = a
    if gate_no:
        ans[QIDS[0]] = "no"
    return ans


# ---------- content and scoring (no database) ----------

def test_content_loads_with_ten_questions_and_approved_cta():
    assert len(CONTENT["questions"]) == 10
    assert CONTENT["cta"]["button"] == "Request 1 of 5 ACCSC Fall 2026 Pilot Review Spots"


@pytest.mark.parametrize("points,expected", [
    (20, "green"), (16, "green"),     # 100%, 80% -> Green
    (15, "yellow"), (10, "yellow"),   # 75%, 50% -> Yellow
    (9, "red"), (0, "red"),           # 45%, 0%  -> Red
])
def test_band_boundaries(points, expected):
    # yes=2, partly=1: build exactly `points` without failing a gate (gates are Q1, Q2 and answered yes).
    n_yes = points // 2
    n_partly = points % 2
    ans = answers_for(n_yes, n_partly)
    if n_yes < 2:  # keep gates passing for this test: use partly on gates instead of no
        ans = {qid: "no" for qid in QIDS}
        for qid in QIDS[:points]:
            ans[qid] = "partly"
    r = score(CONTENT, ans)
    assert r["points"] == points
    assert r["band"]["key"] == expected, r


def test_gate_no_forces_not_yet_eligible_even_with_high_score():
    ans = {qid: "yes" for qid in QIDS}
    ans[QIDS[1]] = "no"  # Q2 operating history
    r = score(CONTENT, ans)
    assert r["percent"] == 90.0
    assert r["gated"] is True
    assert r["band"]["key"] == "red"
    assert r["band"]["label"] == "Not yet eligible"


def test_gaps_listed_worst_first():
    ans = {qid: "yes" for qid in QIDS}
    ans[QIDS[5]] = "partly"
    ans[QIDS[3]] = "no"
    r = score(CONTENT, ans)
    assert [g["question"]["id"] for g in r["gaps"]] == [QIDS[3], QIDS[5]]


def test_missing_answer_rejected():
    ans = {qid: "yes" for qid in QIDS[:-1]}
    with pytest.raises(ValueError):
        score(CONTENT, ans)


def test_text_edit_needs_no_code_change(tmp_path):
    edited = copy.deepcopy(CONTENT)
    edited["questions"][0]["text"] = "EDITED QUESTION TEXT"
    edited["cta"]["button"] = "EDITED CTA"
    p = tmp_path / "content.json"
    p.write_text(json.dumps(edited))
    assert load_content(str(p))["questions"][0]["text"] == "EDITED QUESTION TEXT"


@pytest.mark.parametrize("mutate", [
    lambda c: c.pop("questions"),
    lambda c: c["questions"].append(dict(c["questions"][0])),          # duplicate id
    lambda c: c.__setitem__("bands", [b for b in c["bands"] if b["min_percent"] != 0]),
    lambda c: c["gate_failed"].__setitem__("band", "purple"),
])
def test_invalid_content_fails_fast(tmp_path, mutate):
    bad = copy.deepcopy(CONTENT)
    mutate(bad)
    p = tmp_path / "content.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ContentError):
        load_content(str(p))


# ---------- web app (SQLite) ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ser_test.db'}")
    monkeypatch.delenv("K_SERVICE", raising=False)
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        c.app = app
        yield c


def form_data(answers, **overrides):
    data = {"name": "Pat Example", "email": "Pat@Example.edu", "school": "Example Tech", "consent": "yes", "website": ""}
    data.update(answers)
    data.update(overrides)
    return data


def test_form_renders_all_questions_and_honeypot(client):
    html = client.get("/").get_data(as_text=True)
    for q in CONTENT["questions"]:
        assert html_escape(q["text"]) in html
    assert 'name="website"' in html


def test_submit_stores_lead_and_shows_result(client):
    ans = {qid: "yes" for qid in QIDS}
    ans[QIDS[4]] = "no"
    resp = client.post("/submit", data=form_data(ans))
    assert resp.status_code == 303
    lead_id = resp.headers["Location"].rsplit("/", 1)[-1]
    with client.app.app_context():
        lead = db.session.get(SerLead, lead_id)
        assert lead.email == "pat@example.edu"
        assert lead.points == 18 and lead.band == "green" and lead.consent is True
        assert lead.answers == ans and lead.pilot_requested is False
    page = client.get(f"/result/{lead_id}").get_data(as_text=True)
    assert "90%" in page and "Ready to start your SER" in page
    assert CONTENT["questions"][4]["why"] in page
    assert CONTENT["cta"]["button"] in page


def test_pilot_request_recorded_once(client):
    resp = client.post("/submit", data=form_data({qid: "partly" for qid in QIDS}))
    lead_id = resp.headers["Location"].rsplit("/", 1)[-1]
    assert client.post(f"/result/{lead_id}/pilot").status_code == 303
    with client.app.app_context():
        first = db.session.get(SerLead, lead_id).pilot_requested_at
        assert first is not None
    client.post(f"/result/{lead_id}/pilot")
    with client.app.app_context():
        assert db.session.get(SerLead, lead_id).pilot_requested_at == first
    page = client.get(f"/result/{lead_id}").get_data(as_text=True)
    assert html_escape(CONTENT["cta"]["thanks"]) in page and CONTENT["cta"]["button"] not in page


@pytest.mark.parametrize("overrides,field", [
    ({"email": "not-an-email"}, "Enter a valid email address."),
    ({"name": ""}, "Required."),
    ({"consent": ""}, "Please confirm"),
    ({"school": "x" * 300}, "255 characters or fewer"),
])
def test_validation_errors_return_400_and_store_nothing(client, overrides, field):
    resp = client.post("/submit", data=form_data({qid: "yes" for qid in QIDS}, **overrides))
    assert resp.status_code == 400 and field in resp.get_data(as_text=True)
    with client.app.app_context():
        assert SerLead.query.count() == 0


def test_unanswered_question_returns_400(client):
    ans = {qid: "yes" for qid in QIDS[:-1]}
    resp = client.post("/submit", data=form_data(ans))
    assert resp.status_code == 400 and "Please choose an answer." in resp.get_data(as_text=True)


def test_honeypot_rejects_and_stores_nothing(client):
    resp = client.post("/submit", data=form_data({qid: "yes" for qid in QIDS}, website="http://spam"))
    assert resp.status_code == 400
    with client.app.app_context():
        assert SerLead.query.count() == 0


def test_db_failure_returns_generic_500_without_leaking(client, monkeypatch):
    secret = "postgresql+psycopg2://ser_app:FAKE-PW@/ser?host=/cloudsql/x"
    def boom():
        raise OperationalError("INSERT", {}, Exception(secret))
    monkeypatch.setattr(db.session, "commit", boom)
    resp = client.post("/submit", data=form_data({qid: "yes" for qid in QIDS}))
    body = resp.get_data(as_text=True)
    assert resp.status_code == 500
    assert "FAKE-PW" not in body and "postgresql" not in body and "Traceback" not in body
    assert "try again" in body


def test_unknown_or_malformed_result_ids_404(client):
    assert client.get("/result/not-a-uuid").status_code == 404
    assert client.get("/result/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.post("/result/00000000-0000-0000-0000-000000000000/pilot").status_code == 404


def test_html_injection_is_escaped(client):
    resp = client.post("/submit", data=form_data({qid: "yes" for qid in QIDS}, school="<script>alert(1)</script>"))
    lead_id = resp.headers["Location"].rsplit("/", 1)[-1]
    page = client.get(f"/result/{lead_id}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_health_ok_and_failure_leaks_nothing(client, monkeypatch):
    assert client.get("/health").get_json() == {"status": "healthy"}
    def boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("FAKE-PW"))
    monkeypatch.setattr(db.session, "execute", boom)
    resp = client.get("/health")
    assert "FAKE-PW" not in resp.get_data(as_text=True)
    assert resp.status_code == 503 and resp.get_json() == {"status": "unhealthy"}


def test_security_headers_present(client):
    h = client.get("/").headers
    assert h["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in h["Content-Security-Policy"]


def test_refuses_to_start_on_cloud_run_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("K_SERVICE", "ser-diagnostic")
    with pytest.raises(RuntimeError, match="DATABASE_URL must be set"):
        create_app()
