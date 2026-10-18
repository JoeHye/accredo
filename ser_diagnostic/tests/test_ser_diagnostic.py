import copy
import json
import os
import sys
from unittest import mock

import pytest
from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ser_app.content import load_content, validate_content, score_answers, ContentError, AnswerError, DEFAULT_CONTENT_PATH  # noqa: E402

QIDS = [f"q{i}" for i in range(1, 11)]


def answers(default="yes", **overrides):
    a = {q: default for q in QIDS}
    a.update(overrides)
    return a


@pytest.fixture
def content():
    return load_content(DEFAULT_CONTENT_PATH)


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    def make(allow_draft=True, content_override=None):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ser.db'}")
        monkeypatch.delenv("K_SERVICE", raising=False)
        if allow_draft:
            monkeypatch.setenv("SER_ALLOW_DRAFT", "1")
        else:
            monkeypatch.delenv("SER_ALLOW_DRAFT", raising=False)
        path = None
        if content_override is not None:
            path = tmp_path / "content.json"
            path.write_text(json.dumps(content_override))
        from ser_app import create_app
        app = create_app(str(path) if path else None)
        app.config["TESTING"] = True
        return app
    return make


def valid_payload(**overrides):
    p = {"name": "Pat Doe", "email": "Pat@Example.edu", "school_name": "Example Tech", "answers": answers()}
    p.update(overrides)
    return p


# ---------------------------------------------------------------- scoring
def test_shipped_content_is_valid_and_still_draft(content):
    assert content["status"] == "DRAFT", "content must stay DRAFT until the owner approves the wording"
    assert len(content["questions"]) == 10


@pytest.mark.parametrize("ans,score,key", [
    (answers("yes"), 20, "ready"),
    (answers("yes", q3="partly", q4="partly", q5="partly", q6="partly"), 16, "ready"),   # band boundary
    (answers("yes", q3="partly", q4="partly", q5="partly", q6="partly", q7="partly"), 15, "close"),
    (answers("partly", q1="yes", q2="yes", q3="no", q4="no", q5="no"), 9, "close"),       # boundary: 4+0+5
    (answers("partly", q1="yes", q2="yes", q3="no", q4="no", q5="no", q6="no"), 8, "foundation"),
    (answers("partly"), 10, "close"),  # "Partly" on gate questions does NOT trigger the gate
])
def test_bands(content, ans, score, key):
    r = score_answers(content, ans)
    assert (r["score"], r["result_key"], r["gate_failed"]) == (score, key, False)
    assert r["max_score"] == 20


@pytest.mark.parametrize("gate_q", ["q1", "q2"])
def test_gate_no_caps_result_even_with_high_score(content, gate_q):
    r = score_answers(content, answers("yes", **{gate_q: "no"}))
    assert r["score"] == 18
    assert r["result_key"] == "not_eligible" and r["gate_failed"] is True


def test_gaps_worst_first_then_question_order(content):
    r = score_answers(content, answers("yes", q9="partly", q4="no", q2="partly", q7="no"))
    assert [g["id"] for g in r["gaps"]] == ["q4", "q7", "q2", "q9"]
    assert r["gaps"][0]["answer"] == "No" and r["gaps"][0]["why"]


def test_all_yes_has_no_gaps(content):
    assert score_answers(content, answers("yes"))["gaps"] == []


@pytest.mark.parametrize("bad,needle", [
    ({k: v for k, v in answers().items() if k != "q5"}, "unanswered: q5"),
    (answers(q11="yes"), "unknown questions: q11"),
    (answers(q3="maybe"), "invalid answers for: q3"),
    (["yes"] * 10, "must be an object"),
    (None, "must be an object"),
])
def test_invalid_answers_rejected(content, bad, needle):
    with pytest.raises(AnswerError, match=needle):
        score_answers(content, bad)


# ---------------------------------------------------------------- content validation
def test_content_validation_failures(content):
    for mutate, needle in [
        (lambda c: c.pop("bands"), "missing key 'bands'"),
        (lambda c: c.update(status="LIVE"), "status must be one of"),
        (lambda c: c["sections"][0]["question_ids"].append("q3"), "every question exactly once"),
        (lambda c: c["bands"].pop(), "min_score 0"),
        (lambda c: c["questions"][0].update(why=""), "missing 'why'"),
        (lambda c: c.update(status="APPROVED"), "branding decided"),
    ]:
        c = copy.deepcopy(content)
        mutate(c)
        with pytest.raises(ContentError, match=needle):
            validate_content(c)


def test_missing_and_malformed_content_file(tmp_path):
    with pytest.raises(ContentError, match="not found"):
        load_content(str(tmp_path / "nope.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ContentError, match="JSONDecodeError"):
        load_content(str(bad))


# ---------------------------------------------------------------- app / routes
def test_draft_content_is_blocked_without_opt_in(app_factory):
    client = app_factory(allow_draft=False).test_client()
    assert client.get("/").status_code == 503
    r = client.post("/api/submit", json=valid_payload())
    assert r.status_code == 503 and "not published" in r.get_json()["error"]
    assert client.get("/health").get_json()["content_status"] == "DRAFT"


def test_page_renders_with_draft_banner(app_factory):
    r = app_factory().test_client().get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "DRAFT v0.1" in html and "Self-Evaluation Report" in html
    assert html.count('type="radio"') == 30


def test_submit_scores_and_stores_lead(app_factory):
    app = app_factory()
    r = app.test_client().post("/api/submit", json=valid_payload(answers=answers("yes", q6="no")))
    assert r.status_code == 200
    body = r.get_json()
    assert body["score"] == 18 and body["result_key"] == "ready"
    from ser_app.models import Lead
    with app.app_context():
        leads = Lead.query.all()
        assert len(leads) == 1
        lead = leads[0]
        assert (lead.email, lead.school_name, lead.score, lead.result_key, lead.content_version) == \
               ("pat@example.edu", "Example Tech", 18, "ready", "0.1")
        assert lead.answers["q6"] == "no"


@pytest.mark.parametrize("payload,field", [
    (valid_payload(email="not-an-email"), "email"),
    (valid_payload(name=""), "name"),
    (valid_payload(name="x" * 201), "name"),
    (valid_payload(school_name="A\x00B"), "school_name"),
    (valid_payload(answers={"q1": "yes"}), "answers"),
])
def test_validation_errors_store_nothing(app_factory, payload, field):
    app = app_factory()
    r = app.test_client().post("/api/submit", json=payload)
    assert r.status_code == 400 and field in r.get_json()["fields"]
    from ser_app.models import Lead
    with app.app_context():
        assert Lead.query.count() == 0


def test_non_json_and_oversized_bodies(app_factory):
    client = app_factory().test_client()
    assert client.post("/api/submit", data="hello", content_type="text/plain").status_code == 400
    big = valid_payload(name="x" * 20000)
    assert client.post("/api/submit", json=big).status_code == 413


def test_honeypot_returns_result_but_stores_nothing(app_factory):
    app = app_factory()
    r = app.test_client().post("/api/submit", json=valid_payload(website="http://spam.example"))
    assert r.status_code == 200 and r.get_json()["score"] == 20
    from ser_app.models import Lead
    with app.app_context():
        assert Lead.query.count() == 0


def test_db_failure_on_submit_is_explicit_and_leaks_nothing(app_factory):
    app = app_factory()
    from ser_app import db
    err = OperationalError("INSERT", {}, Exception("postgresql://ser_app:FAKE-PW@/cloudsql/x"))
    with mock.patch.object(db.session, "commit", side_effect=err):
        r = app.test_client().post("/api/submit", json=valid_payload())
    assert r.status_code == 503
    text = r.get_data(as_text=True)
    for leak in ("FAKE-PW", "cloudsql", "postgresql", "Traceback", "details"):
        assert leak not in text


def test_health_db_failure_leaks_nothing(app_factory):
    app = app_factory()
    from ser_app import db
    with mock.patch.object(db.session, "execute", side_effect=OperationalError("SELECT 1", {}, Exception("host=10.1.2.3 password=FAKE-PW"))):
        r = app.test_client().get("/health")
    assert r.status_code == 503 and "FAKE-PW" not in r.get_data(as_text=True)


def test_refuses_to_start_on_cloud_run_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("K_SERVICE", "ser-diagnostic")
    from ser_app import create_app
    with pytest.raises(RuntimeError, match="DATABASE_URL must be set"):
        create_app()


def test_invalid_content_fails_startup(app_factory, content):
    broken = copy.deepcopy(content)
    broken["bands"] = []
    with pytest.raises(RuntimeError, match="content is invalid"):
        app_factory(content_override=broken)
