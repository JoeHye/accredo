"""Exhibit list/upload/delete on /api/applications/<id>/criteria/<criterion_id>/exhibits.

Covers the {"status": ...} JSON envelope, content-type sniffing, the 25 MB limit, the 15 s GCS
timeout, Response auto-creation (including a concurrent-insert race), and rollback / orphan cleanup.
Run from the repo root:  python -m pytest tests/test_exhibits_upload.py
SQLite + local disk by default (set TEST_DATABASE_URL for Postgres); GCS is a fake client where a
test needs it. No network.
"""
import io
import os
import zipfile
from datetime import date

import pytest

from app import create_app, db
from app import exhibit_routes
from app.models import (Application, ApplicationMember, Criterion, EvidenceDocument, Portfolio, Response,
                        StandardPack, Users)

PW = "password123"
LEAK_MARKERS = ["Traceback", "gs://", "accredo-test-exhibits", "SECRET-DETAIL", "postgresql"]
CRIT = "0-B-1"


def docx_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


def plain_zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes.txt", "not a word file")
    return buf.getvalue()


PDF = b"%PDF-1.7\n%test\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


@pytest.fixture
def env(tmp_path, monkeypatch):
    # TEST_DATABASE_URL=postgresql+psycopg2://... runs the same tests on Postgres (schema is recreated).
    pg_url = os.environ.get("TEST_DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", pg_url or f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("EXHIBIT_UPLOAD_DIR", str(tmp_path / "exhibits"))
    for var in ("SECRET_KEY", "GCS_BUCKET_NAME", "K_SERVICE"):
        monkeypatch.delenv(var, raising=False)
    if pg_url:
        monkeypatch.setenv("SECRET_KEY", "test-only-secret-key")
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        if pg_url:
            db.drop_all()
            db.create_all()
        users = {}
        for name in ("owner", "member", "outsider"):
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
        for cid, item in ((CRIT, "1"), ("0-B-2", "2"), ("0-C-3", "3")):
            db.session.add(Criterion(id=cid, standard_pack_id="pk", section_code="0", section_name="S",
                                     subsection_code="B", item_number=item, question_text="Upload evidence.",
                                     answer_type="exhibit", required=True))
        db.session.add(Criterion(id="nar", standard_pack_id="pk", section_code="0", section_name="S",
                                 item_number="9", question_text="Describe.", answer_type="narrative"))
        db.session.add(ApplicationMember(application_id=application.id, user_id=users["member"].id))
        db.session.commit()
        app_id = application.id
    return app, app_id, tmp_path


def login(app, who="member"):
    c = app.test_client()
    resp = c.post("/api/auth/login", json={"email": f"{who}@example.edu", "password": PW})
    assert resp.status_code == 200, resp.get_json()
    return c


def url(app_id, crit=CRIT, evidence_id=None):
    base = f"/api/applications/{app_id}/criteria/{crit}/exhibits"
    return base if evidence_id is None else f"{base}/{evidence_id}"


def post(c, app_id, data=PDF, name="catalog.pdf", crit=CRIT):
    return c.post(url(app_id, crit), data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def assert_error(resp, status, code):
    body = resp.get_json()
    assert resp.status_code == status, (resp.status_code, body)
    assert body["status"] == "error" and body["code"] == code and body["error"], body
    raw = resp.get_data(as_text=True)
    assert not any(m in raw for m in LEAK_MARKERS), raw


def counts(app):
    with app.app_context():
        return Response.query.count(), EvidenceDocument.query.count()


def install_fake_gcs(monkeypatch, upload_exc=None, delete_exc=None):
    log = []

    class Blob:
        def __init__(self, path):
            self.path = path
            self.content_type = None

        def upload_from_file(self, f, timeout=None):
            log.append(("upload", self.path, timeout, self.content_type))
            if upload_exc:
                raise upload_exc

        def delete(self, timeout=None):
            log.append(("delete", self.path, timeout, None))
            if delete_exc:
                raise delete_exc

    class Bucket:
        def blob(self, path):
            return Blob(path)

    class Client:
        def bucket(self, name):
            return Bucket()

    import google.cloud.storage as real_storage
    monkeypatch.setattr(real_storage, "Client", Client)
    monkeypatch.setenv("GCS_BUCKET_NAME", "accredo-test-exhibits")
    return log


# ---------- GET ----------

@pytest.mark.parametrize("crit", ["0-B-1", "0-B-2", "0-C-3"])
def test_get_without_response_is_200_empty_and_creates_nothing(env, crit):
    app, app_id, _ = env
    resp = login(app).get(url(app_id, crit))
    assert resp.status_code == 200 and resp.get_json() == {"status": "success", "exhibits": []}
    assert counts(app) == (0, 0)


def test_get_lists_uploaded_files_in_order(env):
    app, app_id, _ = env
    c = login(app)
    assert post(c, app_id, PDF, "a.pdf").status_code == 201
    assert post(c, app_id, PNG, "b.png").status_code == 201
    body = c.get(url(app_id)).get_json()
    assert body["status"] == "success"
    assert [(e["filename"], e["size"]) for e in body["exhibits"]] == [("a.pdf", len(PDF)), ("b.png", len(PNG))]
    assert set(body["exhibits"][0]) == {"id", "filename", "size", "uploaded_at"}


def test_get_unknown_criterion_and_outsider(env):
    app, app_id, _ = env
    assert_error(login(app).get(url(app_id, "nope")), 404, "criterion_not_found")
    assert login(app, "outsider").get(url(app_id)).status_code in (403, 404)


# ---------- POST: success ----------

@pytest.mark.parametrize("data,name", [(PDF, "catalog.pdf"), (PNG, "chart.png"), (JPG, "photo.jpg"),
                                       (JPG, "photo.JPEG"), (docx_bytes(), "policy.docx")])
def test_upload_allowed_types_creates_response_and_record(env, data, name):
    app, app_id, tmp_path = env
    resp = post(login(app), app_id, data, name)
    body = resp.get_json()
    assert resp.status_code == 201, body
    assert body["status"] == "success"
    assert set(body["file"]) == {"id", "filename", "size", "uploaded_at"}
    assert body["file"]["filename"] == name and body["file"]["size"] == len(data)
    with app.app_context():
        doc = EvidenceDocument.query.one()
        assert Response.query.one().id == doc.response_id
        assert os.path.isabs(doc.gcs_path) and doc.gcs_path.startswith(str(tmp_path / "exhibits"))
        assert open(doc.gcs_path, "rb").read() == data


def test_second_upload_reuses_the_response(env):
    app, app_id, _ = env
    c = login(app)
    post(c, app_id, PDF, "a.pdf")
    post(c, app_id, PDF, "a.pdf")
    assert counts(app) == (1, 2)
    with app.app_context():
        paths = [d.gcs_path for d in EvidenceDocument.query.all()]
    assert len(set(paths)) == 2, "same-named uploads in the same second must not share a file"


def test_concurrent_first_upload_reuses_the_winning_response(env, monkeypatch):
    """Simulates losing the race: the row already exists, but this request's first lookup missed it."""
    app, app_id, _ = env
    with app.app_context():
        db.session.add(Response(application_id=app_id, criterion_id=CRIT, narrative_text="", not_applicable=False))
        db.session.commit()
        winner_id = Response.query.one().id
    real_find = exhibit_routes._find_response
    calls = []

    def find_missing_first(application_id, criterion_id):
        calls.append(1)
        return None if len(calls) == 1 else real_find(application_id, criterion_id)
    monkeypatch.setattr(exhibit_routes, "_find_response", find_missing_first)
    resp = post(login(app), app_id)
    assert resp.status_code == 201, resp.get_json()
    with app.app_context():
        assert Response.query.count() == 1
        assert EvidenceDocument.query.one().response_id == winner_id


# ---------- POST: validation ----------

def test_missing_file_field(env):
    app, app_id, _ = env
    resp = login(app).post(url(app_id), data={}, content_type="multipart/form-data")
    assert_error(resp, 400, "no_file")


def test_non_exhibit_criterion(env):
    app, app_id, _ = env
    assert_error(post(login(app), app_id, crit="nar"), 400, "not_an_exhibit_criterion")


@pytest.mark.parametrize("data,name,code", [
    (b"MZ\x90\x00", "tool.exe", "invalid_file_type"),            # extension not allowed
    (b"hello", "notes.pdf", "invalid_file_type"),                # contents not a known format
    (PNG, "fake.pdf", "file_type_mismatch"),                     # PNG renamed to .pdf
    (PDF, "fake.png", "file_type_mismatch"),
    (plain_zip_bytes(), "archive.docx", "invalid_file_type"),    # a ZIP that is not a Word file
    (b"", "empty.pdf", "empty_file"),
])
def test_rejected_files_store_nothing(env, data, name, code):
    app, app_id, tmp_path = env
    assert_error(post(login(app), app_id, data, name), 400, code)
    assert counts(app) == (0, 0)
    stored = tmp_path / "exhibits"
    assert not stored.exists() or not os.listdir(stored)


def test_size_limit_is_strictly_below_the_maximum(env, monkeypatch):
    app, app_id, _ = env
    monkeypatch.setattr(exhibit_routes, "MAX_FILE_SIZE", len(PDF) + 1)
    c = login(app)
    assert_error(post(c, app_id, PDF + b"x"), 413, "file_too_large")     # exactly the maximum
    assert post(c, app_id, PDF).status_code == 201                       # one byte under


def test_default_limit_is_25_mib():
    assert exhibit_routes.MAX_FILE_SIZE == 25 * 1024 * 1024


# ---------- POST: GCS ----------

def test_gcs_upload_uses_15s_timeout_and_sniffed_content_type(env, monkeypatch):
    app, app_id, _ = env
    log = install_fake_gcs(monkeypatch)
    resp = post(login(app), app_id, docx_bytes(), "policy.docx")
    assert resp.status_code == 201, resp.get_json()
    (op, path, timeout, ctype), = log
    assert op == "upload" and timeout == 15
    assert ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert path.startswith(f"applications/{app_id}/exhibits/") and path.endswith("_policy.docx")
    assert "gs://" not in resp.get_data(as_text=True)


def test_gcs_timeout_rolls_back_the_new_response(env, monkeypatch):
    import requests
    app, app_id, _ = env
    install_fake_gcs(monkeypatch, upload_exc=requests.exceptions.ReadTimeout("SECRET-DETAIL read timed out"))
    assert_error(post(login(app), app_id), 503, "storage_unavailable")
    assert counts(app) == (0, 0), "a failed upload must not leave a Response or EvidenceDocument behind"


def test_db_commit_failure_deletes_the_uploaded_blob(env, monkeypatch):
    from sqlalchemy.exc import OperationalError
    app, app_id, _ = env
    log = install_fake_gcs(monkeypatch)
    c = login(app)

    def failing_commit():
        raise OperationalError("INSERT", {}, Exception("SECRET-DETAIL db down"))
    monkeypatch.setattr(db.session, "commit", failing_commit)
    resp = post(c, app_id)
    monkeypatch.undo()
    assert_error(resp, 500, "database_error")
    uploads = [p for op, p, *_ in log if op == "upload"]
    deletes = [(p, t) for op, p, t, _ in log if op == "delete"]
    assert uploads and deletes == [(uploads[0], 15)], log


def test_unexpected_error_after_upload_also_deletes_the_blob(env, monkeypatch):
    app, app_id, _ = env
    log = install_fake_gcs(monkeypatch)
    c = login(app)

    class Boom(exhibit_routes.EvidenceDocument):
        def __init__(self, **kw):
            raise RuntimeError("SECRET-DETAIL boom")
    monkeypatch.setattr(exhibit_routes, "EvidenceDocument", Boom)
    assert_error(post(c, app_id), 500, "internal_error")
    assert [op for op, *_ in log] == ["upload", "delete"]


def test_orphan_cleanup_failure_is_logged_for_manual_cleanup(env, monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError
    app, app_id, _ = env
    install_fake_gcs(monkeypatch, delete_exc=RuntimeError("SECRET-DETAIL delete failed"))
    c = login(app)

    def failing_commit():
        raise OperationalError("INSERT", {}, Exception("db down"))
    monkeypatch.setattr(db.session, "commit", failing_commit)
    with caplog.at_level("ERROR"):
        assert_error(post(c, app_id), 500, "database_error")
    assert any("ORPHANED exhibit file needs manual cleanup" in r.getMessage() for r in caplog.records)


# ---------- DELETE ----------

def test_delete_removes_record_and_file(env):
    app, app_id, _ = env
    c = login(app)
    file_id = post(c, app_id).get_json()["file"]["id"]
    with app.app_context():
        path = db.session.get(EvidenceDocument, file_id).gcs_path
    resp = c.delete(url(app_id, evidence_id=file_id))
    assert resp.status_code == 200 and resp.get_json() == {"status": "success", "evidence_id": file_id}
    assert not os.path.exists(path)
    assert c.get(url(app_id)).get_json()["exhibits"] == []


def test_delete_unknown_exhibit_is_404(env):
    app, app_id, _ = env
    assert_error(login(app).delete(url(app_id, evidence_id=999)), 404, "exhibit_not_found")


def test_delete_storage_failure_keeps_the_record(env, monkeypatch):
    app, app_id, _ = env
    install_fake_gcs(monkeypatch)
    c = login(app)
    file_id = post(c, app_id).get_json()["file"]["id"]
    install_fake_gcs(monkeypatch, delete_exc=__import__("google.api_core.exceptions", fromlist=["x"]).Forbidden("SECRET-DETAIL"))
    assert_error(c.delete(url(app_id, evidence_id=file_id)), 500, "storage_permission_denied")
    assert [e["id"] for e in c.get(url(app_id)).get_json()["exhibits"]] == [file_id]


def test_delete_db_failure_leaves_the_file_untouched(env, monkeypatch):
    from sqlalchemy.exc import OperationalError
    app, app_id, _ = env
    log = install_fake_gcs(monkeypatch)
    c = login(app)
    file_id = post(c, app_id).get_json()["file"]["id"]

    def failing_flush(*a, **kw):
        raise OperationalError("DELETE", {}, Exception("SECRET-DETAIL db down"))
    monkeypatch.setattr(db.session, "flush", failing_flush)
    resp = c.delete(url(app_id, evidence_id=file_id))
    monkeypatch.undo()
    assert_error(resp, 500, "database_error")
    assert not [op for op, *_ in log if op == "delete"], "file must not be deleted when the record delete fails"


# ---------- legacy route the wizard uses keeps its shape ----------

def test_legacy_routes_share_the_same_data(env):
    app, app_id, _ = env
    c = login(app)
    file_id = post(c, app_id).get_json()["file"]["id"]
    legacy = c.get(f"/api/applications/{app_id}/responses/{CRIT}/exhibit").get_json()
    assert isinstance(legacy, list) and legacy[0]["id"] == file_id and legacy[0]["file_size_bytes"] == len(PDF)
    err = c.post(f"/api/applications/{app_id}/responses/{CRIT}/exhibit", data={},
                 content_type="multipart/form-data").get_json()
    assert err["status"] == "error" and err["code"] == "no_file"


# ---------- XLSX ----------

def xlsx_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
    return buf.getvalue()


def test_xlsx_upload_is_accepted_and_typed(env, monkeypatch):
    app, app_id, _ = env
    log = install_fake_gcs(monkeypatch)
    resp = post(login(app), app_id, xlsx_bytes(), "enrollment.xlsx")
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["file"]["filename"] == "enrollment.xlsx"
    assert log[0][3] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.parametrize("data,name,code", [
    (xlsx_bytes(), "renamed.docx", "file_type_mismatch"),      # spreadsheet named as Word
    (docx_bytes(), "renamed.xlsx", "file_type_mismatch"),      # Word named as spreadsheet
    (plain_zip_bytes(), "archive.xlsx", "invalid_file_type"),  # a ZIP that is neither
])
def test_xlsx_and_docx_are_not_interchangeable(env, data, name, code):
    app, app_id, _ = env
    assert_error(post(login(app), app_id, data, name), 400, code)
    assert counts(app) == (0, 0)


# ---------- directive checklist: explicit DB fields, .sh, real >25 MB payload ----------

@pytest.mark.parametrize("data,name", [(PDF, "catalog.pdf"), (docx_bytes(), "policy.docx"), (xlsx_bytes(), "enrollment.xlsx")])
def test_upload_writes_evidence_row_with_every_field(env, data, name):
    app, app_id, tmp_path = env
    assert post(login(app), app_id, data, name, crit="0-B-2").status_code == 201
    with app.app_context():
        doc, criterion_id = (db.session.query(EvidenceDocument, Response.criterion_id)
                             .join(Response, Response.id == EvidenceDocument.response_id).one())
        assert (doc.filename, doc.file_size_bytes, criterion_id) == (name, len(data), "0-B-2")
        assert doc.uploaded_at is not None
        assert os.path.isabs(doc.gcs_path) and os.path.getsize(doc.gcs_path) == len(data)   # storage path


@pytest.mark.parametrize("name", ["install.sh", "tool.exe", "macro.bat", "page.html"])
def test_disallowed_extensions_are_rejected(env, name):
    app, app_id, _ = env
    assert_error(post(login(app), app_id, b"#!/bin/sh\necho hi\n", name), 400, "invalid_file_type")
    assert counts(app) == (0, 0)


def test_real_26_mb_upload_is_rejected_by_the_25_mb_request_cap(env):
    """Full stack, real size: rejected with 413 and nothing stored (Flask's 25 MB request cap, backed by the route's own size check)."""
    app, app_id, tmp_path = env
    big = PDF + b"0" * (26 * 1024 * 1024)
    assert_error(post(login(app), app_id, big, "big.pdf"), 413, "file_too_large")
    assert counts(app) == (0, 0)
    stored = tmp_path / "exhibits"
    assert not stored.exists() or not os.listdir(stored)
