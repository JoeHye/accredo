"""Exhibit upload/list/delete: access rules, storage failure paths, and JSON error contract.

Run from the repo root:  python -m pytest tests
Uses SQLite and local-disk storage; GCS is replaced by a fake client per test.
"""
import io
import sys
import types
from datetime import date

import pytest

from app import create_app, db
from app.models import (Application, ApplicationMember, Criterion, EvidenceDocument, Portfolio,
                        StandardPack, SubsectionAssignment, Users)

PW = "password123"
LEAK_MARKERS = ["Traceback", "gs://", "accredo-test-exhibits", "SECRET-DETAIL", "postgresql"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("EXHIBIT_UPLOAD_DIR", str(tmp_path / "exhibits"))
    for var in ("SECRET_KEY", "GCS_BUCKET_NAME", "K_SERVICE"):
        monkeypatch.delenv(var, raising=False)
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        users = {}
        for name in ("owner", "member", "outsider", "assignee"):
            u = Users(email=f"{name}@example.edu")
            u.set_password(PW)
            db.session.add(u)
            users[name] = u
        db.session.add(StandardPack(id="pk", accrediting_body="ACCSC", application_type="initial",
                                    version_effective_date=date(2026, 1, 1)))
        db.session.flush()
        portfolio = Portfolio(owner_user_id=users["owner"].id, name="P")
        db.session.add(portfolio)
        db.session.flush()
        application = Application(portfolio_id=portfolio.id, standard_pack_id="pk")
        db.session.add(application)
        db.session.flush()
        db.session.add(Criterion(id="ex1", standard_pack_id="pk", section_code="I", section_name="S",
                                 subsection_code="A", item_number="1", question_text="Upload your catalog.",
                                 answer_type="exhibit"))
        db.session.add(Criterion(id="nar1", standard_pack_id="pk", section_code="I", section_name="S",
                                 item_number="2", question_text="Describe.", answer_type="narrative"))
        db.session.add(ApplicationMember(application_id=application.id, user_id=users["member"].id))
        db.session.add(ApplicationMember(application_id=application.id, user_id=users["assignee"].id))
        db.session.commit()
        app_id = application.id
        ids = {k: v.id for k, v in users.items()}
    return app, app_id, ids


def login(app, who):
    c = app.test_client()
    resp = c.post("/api/auth/login", json={"email": f"{who}@example.edu", "password": PW})
    assert resp.status_code == 200, resp.get_json()
    return c


def upload(c, app_id, data=b"%PDF-1.4 test", name="catalog.pdf", crit="ex1"):
    return c.post(f"/api/applications/{app_id}/responses/{crit}/exhibit",
                  data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def assert_structured_error(resp, status, code):
    body = resp.get_json()
    assert resp.status_code == status, (resp.status_code, body)
    assert body["code"] == code and body["error"], body
    raw = resp.get_data(as_text=True)
    assert not any(m in raw for m in LEAK_MARKERS), raw
    assert "details" not in body


# ---------- the reported bug: invited members could not load or upload ----------

def test_member_can_list_upload_and_delete(env):
    app, app_id, _ = env
    c = login(app, "member")
    assert c.get(f"/api/applications/{app_id}/responses/ex1/exhibit").get_json() == []
    up = upload(c, app_id)
    assert up.status_code == 201, up.get_json()
    listed = c.get(f"/api/applications/{app_id}/responses/ex1/exhibit").get_json()
    assert [f["filename"] for f in listed] == ["catalog.pdf"] and "gcs_path" not in listed[0]
    assert c.delete(f"/api/applications/{app_id}/responses/ex1/exhibit/{listed[0]['id']}").status_code == 200
    assert c.get(f"/api/applications/{app_id}/responses/ex1/exhibit").get_json() == []


def test_owner_still_works(env):
    app, app_id, _ = env
    c = login(app, "owner")
    assert upload(c, app_id).status_code == 201


def test_outsider_gets_403_json(env):
    app, app_id, _ = env
    c = login(app, "outsider")
    resp = c.get(f"/api/applications/{app_id}/responses/ex1/exhibit")
    assert resp.status_code == 403 and resp.get_json()["error"] == "Forbidden"
    assert upload(c, app_id).status_code == 403


def test_assignment_blocks_other_member_uploads_but_not_viewing(env):
    app, app_id, ids = env
    with app.app_context():
        db.session.get(Application, app_id).enforce_assignments = True
        db.session.add(SubsectionAssignment(application_id=app_id, section_code="I", subsection_code="A",
                                            assigned_user_id=ids["assignee"]))
        db.session.commit()
    member = login(app, "member")
    assert member.get(f"/api/applications/{app_id}/responses/ex1/exhibit").status_code == 200
    assert upload(member, app_id).status_code == 403
    assert upload(login(app, "assignee"), app_id).status_code == 201
    assert upload(login(app, "owner"), app_id).status_code == 201


def test_unauthenticated_api_returns_401_json(env):
    app, app_id, _ = env
    resp = app.test_client().get(f"/api/applications/{app_id}/responses/ex1/exhibit")
    assert resp.status_code == 401 and resp.is_json


# ---------- validation errors: 400 / 413 with structured JSON ----------

@pytest.mark.parametrize("kwargs,status,code", [
    ({"name": "malware.exe"}, 400, "invalid_file_type"),
    ({"data": b""}, 400, "empty_file"),
    ({"crit": "nar1"}, 400, "not_an_exhibit_criterion"),
    ({"crit": "nope"}, 404, "criterion_not_found"),
])
def test_validation_errors(env, kwargs, status, code):
    app, app_id, _ = env
    assert_structured_error(upload(login(app, "member"), app_id, **kwargs), status, code)


def test_missing_file_part(env):
    app, app_id, _ = env
    resp = login(app, "member").post(f"/api/applications/{app_id}/responses/ex1/exhibit", data={},
                                     content_type="multipart/form-data")
    assert_structured_error(resp, 400, "no_file")


def test_file_too_large(env, monkeypatch):
    import app.exhibit_routes as er
    monkeypatch.setattr(er, "MAX_FILE_SIZE", 10)
    app, app_id, _ = env
    assert_structured_error(upload(login(app, "member"), app_id, data=b"x" * 11), 413, "file_too_large")


# ---------- storage failure paths (fake GCS) ----------

def install_fake_gcs(monkeypatch, upload_exc=None, delete_exc=None, log=None):
    log = log if log is not None else []

    class Blob:
        def __init__(self, path):
            self.path = path
        def upload_from_file(self, f, timeout=None):
            log.append(("upload", self.path))
            if upload_exc:
                raise upload_exc
        def delete(self, timeout=None):
            log.append(("delete", self.path))
            if delete_exc:
                raise delete_exc

    class Bucket:
        def blob(self, path):
            return Blob(path)

    class Client:
        def bucket(self, name):
            log.append(("bucket", name))
            return Bucket()

    import google.cloud.storage as real_storage
    monkeypatch.setattr(real_storage, "Client", Client)
    monkeypatch.setenv("GCS_BUCKET_NAME", "accredo-test-exhibits")
    return log


def gexc():
    from google.api_core import exceptions
    return exceptions


@pytest.mark.parametrize("exc_factory,status,code", [
    (lambda: gexc().Forbidden("SECRET-DETAIL permission denied on accredo-test-exhibits"), 500, "storage_permission_denied"),
    (lambda: gexc().NotFound("SECRET-DETAIL bucket accredo-test-exhibits"), 500, "storage_bucket_not_found"),
    (lambda: gexc().ServiceUnavailable("SECRET-DETAIL"), 503, "storage_unavailable"),
    (lambda: __import__("requests").exceptions.ConnectTimeout("SECRET-DETAIL timeout"), 503, "storage_unavailable"),
    (lambda: RuntimeError("SECRET-DETAIL weird"), 500, "storage_error"),
])
def test_gcs_upload_failures_are_classified_logged_and_not_recorded(env, monkeypatch, caplog, exc_factory, status, code):
    app, app_id, _ = env
    install_fake_gcs(monkeypatch, upload_exc=exc_factory())
    with caplog.at_level("ERROR"):
        resp = upload(login(app, "member"), app_id)
    assert_structured_error(resp, status, code)
    assert any(code in r.getMessage() and "SECRET-DETAIL" in r.getMessage() for r in caplog.records), \
        "full error detail must be logged server-side"
    with app.app_context():
        assert EvidenceDocument.query.count() == 0


def test_gcs_success_path_records_gs_uri(env, monkeypatch):
    app, app_id, _ = env
    log = install_fake_gcs(monkeypatch)
    assert upload(login(app, "member"), app_id).status_code == 201
    with app.app_context():
        doc = EvidenceDocument.query.one()
        assert doc.gcs_path.startswith("gs://accredo-test-exhibits/applications/")
    assert ("bucket", "accredo-test-exhibits") in log


def test_cloud_run_without_bucket_refuses_instead_of_ephemeral_disk(env, monkeypatch):
    app, app_id, _ = env
    monkeypatch.setenv("K_SERVICE", "accredo")
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    assert_structured_error(upload(login(app, "member"), app_id), 500, "storage_not_configured")


def test_db_failure_after_gcs_upload_removes_orphan_blob(env, monkeypatch):
    from sqlalchemy.exc import OperationalError
    app, app_id, _ = env
    log = install_fake_gcs(monkeypatch)
    c = login(app, "member")
    real_commit = db.session.commit

    def failing_commit():
        raise OperationalError("INSERT", {}, Exception("SECRET-DETAIL db down"))
    monkeypatch.setattr(db.session, "commit", failing_commit)
    resp = upload(c, app_id)
    monkeypatch.setattr(db.session, "commit", real_commit)
    assert_structured_error(resp, 500, "database_error")
    uploaded = [p for op, p in log if op == "upload"]
    deleted = [p for op, p in log if op == "delete"]
    assert uploaded and deleted == uploaded, log


def test_delete_with_gcs_permission_error_keeps_record(env, monkeypatch):
    app, app_id, _ = env
    install_fake_gcs(monkeypatch)
    c = login(app, "member")
    assert upload(c, app_id).status_code == 201
    install_fake_gcs(monkeypatch, delete_exc=gexc().Forbidden("SECRET-DETAIL"))
    doc_id = c.get(f"/api/applications/{app_id}/responses/ex1/exhibit").get_json()[0]["id"]
    assert_structured_error(c.delete(f"/api/applications/{app_id}/responses/ex1/exhibit/{doc_id}"),
                            500, "storage_permission_denied")
    assert len(c.get(f"/api/applications/{app_id}/responses/ex1/exhibit").get_json()) == 1


def test_delete_when_blob_already_gone_still_removes_record(env, monkeypatch):
    app, app_id, _ = env
    install_fake_gcs(monkeypatch)
    c = login(app, "member")
    assert upload(c, app_id).status_code == 201
    install_fake_gcs(monkeypatch, delete_exc=gexc().NotFound("gone"))
    doc_id = c.get(f"/api/applications/{app_id}/responses/ex1/exhibit").get_json()[0]["id"]
    assert c.delete(f"/api/applications/{app_id}/responses/ex1/exhibit/{doc_id}").status_code == 200
    assert c.get(f"/api/applications/{app_id}/responses/ex1/exhibit").get_json() == []


def test_list_db_error_returns_structured_500(env, monkeypatch):
    from sqlalchemy.exc import OperationalError
    import app.exhibit_routes as er
    app, app_id, _ = env
    c = login(app, "member")

    class BoomQuery:
        def filter_by(self, **kw):
            raise OperationalError("SELECT", {}, Exception("SECRET-DETAIL"))
    monkeypatch.setattr(er, "Response", type("BoomResponse", (), {"query": BoomQuery()}))
    assert_structured_error(c.get(f"/api/applications/{app_id}/responses/ex1/exhibit"), 500, "database_error")
