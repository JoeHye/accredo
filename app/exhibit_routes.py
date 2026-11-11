import os
import uuid
import zipfile
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from flask_login import login_required
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from app import db
from app.models import Criterion, Response, EvidenceDocument
# Shared access rules: owner OR invited ApplicationMember may view; edits respect subsection assignments.
from app.wizard_routes import verify_application_ownership, check_edit_permission

logger = logging.getLogger(__name__)
exhibit_bp = Blueprint("exhibit", __name__)

# Extension -> detected content type. ".doc" (legacy Word) is kept because the upload form already offers it.
ALLOWED_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
ALLOWED_EXTENSIONS = set(ALLOWED_TYPES)
MAX_FILE_SIZE = 25 * 1024 * 1024  # files must be strictly smaller than this
GCS_TIMEOUT_SECONDS = 15  # per HTTP request to GCS


class StorageError(Exception):
    """A classified failure talking to exhibit storage. `message` is safe to show users; details go to logs."""

    def __init__(self, code, status, message):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


class UploadRejected(Exception):
    """A request problem the user can fix (no file, wrong type, too large). Nothing was stored."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status, code, message):
    return jsonify({"status": "error", "error": message, "code": code}), status


def classify_storage_exception(e):
    """Map GCS / auth / network exceptions to a StorageError with an actionable, non-leaking message."""
    from google.api_core import exceptions as gexc
    from google.auth import exceptions as auth_exc
    import requests

    if isinstance(e, (gexc.Forbidden, gexc.Unauthorized, auth_exc.DefaultCredentialsError, auth_exc.RefreshError)):
        return StorageError("storage_permission_denied", 500,
                            "File storage rejected the request because of a server permission problem. "
                            "Your file was not saved. Please contact support.")
    if isinstance(e, gexc.NotFound):
        return StorageError("storage_bucket_not_found", 500,
                            "The file storage location is misconfigured on the server. "
                            "Your file was not saved. Please contact support.")
    if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError, auth_exc.TransportError,
                      gexc.ServiceUnavailable, gexc.GatewayTimeout, gexc.DeadlineExceeded, gexc.RetryError,
                      TimeoutError)):
        return StorageError("storage_unavailable", 503,
                            "File storage is temporarily unavailable. Please try again in a minute.")
    return StorageError("storage_error", 500,
                        "An unexpected file storage error occurred. Your file was not saved. Please try again.")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def detect_content_type(file_obj):
    """Content type from the file's leading bytes (the browser-sent type is not trusted). None if unrecognised."""
    file_obj.seek(0)
    head = file_obj.read(8)
    file_obj.seek(0)
    if head.startswith(b"%PDF-"):
        return ALLOWED_TYPES["pdf"]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ALLOWED_TYPES["png"]
    if head.startswith(b"\xff\xd8\xff"):
        return ALLOWED_TYPES["jpg"]
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ALLOWED_TYPES["doc"]
    if head.startswith(b"PK\x03\x04"):
        # Any ZIP starts like this; Word needs word/document.xml, Excel needs xl/workbook.xml.
        try:
            with zipfile.ZipFile(file_obj) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, OSError):
            names = set()
        finally:
            file_obj.seek(0)
        if "word/document.xml" in names:
            return ALLOWED_TYPES["docx"]
        if "xl/workbook.xml" in names:
            return ALLOWED_TYPES["xlsx"]
        return None
    return None


def _gcs_bucket_name():
    """Bucket from env. On Cloud Run a missing bucket must not silently fall back to ephemeral local disk."""
    name = os.environ.get("GCS_BUCKET_NAME")
    if not name and os.environ.get("K_SERVICE"):
        logger.error("GCS_BUCKET_NAME is not set on Cloud Run; refusing to store exhibits on ephemeral disk.")
        raise StorageError("storage_not_configured", 500,
                           "File storage is not configured on the server. Your file was not saved. "
                           "Please contact support.")
    return name


def _unique_name(filename):
    # Timestamp for humans, random suffix so two same-named uploads in one second never overwrite each other.
    return f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}_{filename}"


def upload_file_storage(file_obj, filename: str, application_id: int, content_type=None) -> str:
    gcs_bucket_name = _gcs_bucket_name()

    if gcs_bucket_name:
        blob_path = f"applications/{application_id}/exhibits/{_unique_name(filename)}"
        logger.info(f"Uploading exhibit to gs://{gcs_bucket_name}/{blob_path}")
        try:
            from google.cloud import storage

            client = storage.Client()
            blob = client.bucket(gcs_bucket_name).blob(blob_path)
            if content_type:
                blob.content_type = content_type
            file_obj.seek(0)
            blob.upload_from_file(file_obj, timeout=GCS_TIMEOUT_SECONDS)
            return f"gs://{gcs_bucket_name}/{blob_path}"
        except StorageError:
            raise
        except Exception as e:
            classified = classify_storage_exception(e)
            logger.error(f"GCS upload failed [{classified.code}] bucket={gcs_bucket_name} path={blob_path} "
                         f"app={application_id} file={filename}: {type(e).__name__}: {e}", exc_info=True)
            raise classified from e

    upload_dir = os.environ.get("EXHIBIT_UPLOAD_DIR") or os.path.join(
        os.path.abspath(os.path.dirname(__file__)), "..", "instance", "exhibits")
    absolute_upload_dir = os.path.abspath(upload_dir)
    absolute_file_path = os.path.join(absolute_upload_dir, _unique_name(secure_filename(filename)))
    logger.info(f"Local exhibit storage (development) active: {absolute_upload_dir}")
    try:
        os.makedirs(absolute_upload_dir, exist_ok=True)
        file_obj.seek(0)
        file_obj.save(absolute_file_path)
        return absolute_file_path
    except OSError as e:
        logger.error(f"Local file save failed for app {application_id}, file {filename}: {e}", exc_info=True)
        if os.path.exists(absolute_file_path):
            try:
                os.remove(absolute_file_path)
            except OSError as cleanup_err:
                logger.error(f"Could not remove partial file {absolute_file_path}: {cleanup_err}")
        raise StorageError("storage_error", 500, "The server could not save your file. Please try again.") from e


def delete_file_storage(storage_path: str):
    """Delete a stored exhibit. A file that is already gone is treated as deleted (logged, not an error)."""
    if storage_path.startswith("gs://"):
        bucket_name, _, blob_path = storage_path[len("gs://"):].partition("/")
        try:
            from google.cloud import storage
            from google.api_core import exceptions as gexc

            try:
                storage.Client().bucket(bucket_name).blob(blob_path).delete(timeout=GCS_TIMEOUT_SECONDS)
                logger.info(f"Deleted GCS blob {storage_path}")
            except gexc.NotFound:
                logger.warning(f"GCS blob already missing, treating as deleted: {storage_path}")
        except Exception as e:
            classified = classify_storage_exception(e)
            logger.error(f"GCS delete failed [{classified.code}] {storage_path}: {type(e).__name__}: {e}", exc_info=True)
            raise classified from e
    else:
        if os.path.exists(storage_path):
            try:
                os.remove(storage_path)
                logger.info(f"Deleted local file {storage_path}")
            except OSError as e:
                logger.error(f"Failed to delete local file {storage_path}: {e}", exc_info=True)
                raise StorageError("storage_error", 500, "The server could not delete the file. Please try again.") from e
        else:
            logger.warning(f"Local exhibit file already missing, treating as deleted: {storage_path}")


def _cleanup_orphan(storage_path, context):
    """The file reached storage but its database record did not: remove it so storage and DB stay consistent."""
    try:
        delete_file_storage(storage_path)
        logger.info(f"Removed orphaned exhibit file after failed upload ({context}): {storage_path}")
    except StorageError:
        logger.error(f"ORPHANED exhibit file needs manual cleanup ({context}): {storage_path}")


def _load_exhibit_criterion(application_id, criterion_id, require_edit):
    """Shared access + criterion checks. Returns (criterion, None) or (None, error_response_tuple)."""
    app_record, err_resp, err_code = verify_application_ownership(application_id)
    if err_resp:
        return None, (err_resp, err_code)
    criterion = Criterion.query.get(criterion_id)
    if not criterion:
        return None, error_response(404, "criterion_not_found", f"Criterion {criterion_id} was not found.")
    if require_edit:
        ok, edit_resp, edit_code = check_edit_permission(application_id, criterion.section_code, criterion.subsection_code)
        if not ok:
            return None, (edit_resp, edit_code)
    return criterion, None


def _find_response(application_id, criterion_id):
    return Response.query.filter_by(application_id=application_id, criterion_id=criterion_id).first()


def _get_or_create_response(application_id, criterion_id):
    """The criterion's Response, inserting it (flushed, not committed) if missing.

    Two first uploads can race on the unique (application_id, criterion_id) constraint. The loser's
    flush raises IntegrityError; nothing else is pending in the session at this point, so it rolls
    back and re-reads the winner's row. Same behaviour on Postgres and SQLite.
    """
    existing = _find_response(application_id, criterion_id)
    if existing:
        return existing
    now = datetime.utcnow()
    created = Response(application_id=application_id, criterion_id=criterion_id, narrative_text="",
                       not_applicable=False, created_at=now, updated_at=now)
    db.session.add(created)
    try:
        db.session.flush()
        return created
    except IntegrityError:
        db.session.rollback()
        winner = _find_response(application_id, criterion_id)
        if winner is None:
            raise
        logger.info(f"Response {winner.id} for app {application_id}, criterion {criterion_id} "
                    "was created concurrently; reusing it")
        return winner


def _read_upload():
    """Validate the multipart file. Returns (file, secure_name, size, content_type) or raises UploadRejected."""
    try:
        file = request.files.get("file")
    except RequestEntityTooLarge:
        raise UploadRejected(413, "file_too_large", "Files must be smaller than 25 MB.")
    if not file or not file.filename:
        raise UploadRejected(400, "no_file", "No file was selected. Choose a file and try again.")

    secure_name = secure_filename(file.filename)
    allowed_list = ", ".join(sorted(ALLOWED_EXTENSIONS))
    if not secure_name or not allowed_file(secure_name):
        raise UploadRejected(400, "invalid_file_type", f"That file type is not allowed. Allowed formats: {allowed_list}.")

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size == 0:
        raise UploadRejected(400, "empty_file", "The selected file is empty.")
    if size >= MAX_FILE_SIZE:
        raise UploadRejected(413, "file_too_large",
                             f"Files must be smaller than 25 MB. This file is {round(size / (1024 * 1024), 2)} MB.")

    ext = secure_name.rsplit(".", 1)[1].lower()
    detected = detect_content_type(file)
    if detected is None:
        raise UploadRejected(400, "invalid_file_type",
                             f"The file's contents are not a supported format. Allowed formats: {allowed_list}.")
    if detected != ALLOWED_TYPES[ext]:
        raise UploadRejected(400, "file_type_mismatch",
                             f"The file is named .{ext} but its contents are a different format. "
                             "Rename it with the correct extension or export it again.")
    return file, secure_name, size, detected


def _perform_upload(application_id, criterion_id):
    """Validate, store, and record one exhibit. Returns the EvidenceDocument, or a (json, status) error tuple."""
    storage_path = None
    try:
        criterion, err = _load_exhibit_criterion(application_id, criterion_id, require_edit=True)
        if err:
            return err
        if criterion.answer_type != "exhibit":
            return error_response(400, "not_an_exhibit_criterion",
                                  f"Criterion {criterion_id} does not accept file uploads.")

        file, secure_name, file_size_bytes, content_type = _read_upload()

        response_record = _get_or_create_response(application_id, criterion_id)
        storage_path = upload_file_storage(file, secure_name, application_id, content_type=content_type)

        evidence_doc = EvidenceDocument(response_id=response_record.id, filename=secure_name, gcs_path=storage_path,
                                        uploaded_at=datetime.utcnow(), file_size_bytes=file_size_bytes)
        db.session.add(evidence_doc)
        db.session.commit()

        logger.info(f"Uploaded exhibit {evidence_doc.id} '{secure_name}' ({file_size_bytes} bytes) "
                    f"for app {application_id}, criterion {criterion_id}, response {response_record.id}")
        return evidence_doc

    except UploadRejected as e:
        db.session.rollback()
        return error_response(e.status, e.code, e.message)
    except StorageError as e:
        # Nothing reached storage, so the uncommitted Response insert is simply rolled back.
        db.session.rollback()
        return error_response(e.status, e.code, e.message)
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during exhibit upload (operation=insert evidence_document, app {application_id}, "
                     f"criterion {criterion_id}, storage_path={storage_path}): {e}", exc_info=True)
        if storage_path:
            _cleanup_orphan(storage_path, f"app {application_id}, criterion {criterion_id}")
        return error_response(500, "database_error", "Your file could not be recorded. Please try again.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during exhibit upload (app {application_id}, criterion {criterion_id}, "
                     f"storage_path={storage_path}): {e}", exc_info=True)
        if storage_path:
            _cleanup_orphan(storage_path, f"app {application_id}, criterion {criterion_id}")
        return error_response(500, "internal_error", "An unexpected error occurred during upload. Please try again.")


def _list_documents(application_id, criterion_id):
    """The criterion's EvidenceDocuments (empty when no Response exists yet), or a (json, status) error tuple."""
    try:
        _criterion, err = _load_exhibit_criterion(application_id, criterion_id, require_edit=False)
        if err:
            return err
        response_record = _find_response(application_id, criterion_id)
        if not response_record:
            return []
        return (EvidenceDocument.query.filter_by(response_id=response_record.id)
                .order_by(EvidenceDocument.uploaded_at, EvidenceDocument.id).all())
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving exhibits for app {application_id}, criterion {criterion_id}: {e}", exc_info=True)
        return error_response(500, "database_error", "Attachments could not be loaded. Please refresh and try again.")
    except Exception as e:
        logger.error(f"Unexpected error retrieving exhibits for app {application_id}, criterion {criterion_id}: {e}", exc_info=True)
        return error_response(500, "internal_error", "Attachments could not be loaded. Please refresh and try again.")


def _perform_delete(application_id, criterion_id, evidence_id):
    """Delete one exhibit (record first, then file). Returns None on success, or a (json, status) error tuple."""
    try:
        _criterion, err = _load_exhibit_criterion(application_id, criterion_id, require_edit=True)
        if err:
            return err

        response_record = _find_response(application_id, criterion_id)
        doc = (EvidenceDocument.query.filter_by(id=evidence_id, response_id=response_record.id).first()
               if response_record else None)
        if not doc:
            return error_response(404, "exhibit_not_found", "That attachment no longer exists.")

        storage_path = doc.gcs_path
        db.session.delete(doc)
        db.session.flush()                  # a DB problem surfaces here, before the file is touched
        delete_file_storage(storage_path)   # a storage failure rolls the delete back below
        db.session.commit()
        logger.info(f"Deleted exhibit {evidence_id} for app {application_id}, criterion {criterion_id}")
        return None

    except StorageError as e:
        db.session.rollback()
        return error_response(e.status, e.code, e.message)
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error deleting exhibit {evidence_id} (operation=delete evidence_document, "
                     f"app {application_id}, criterion {criterion_id}): {e}", exc_info=True)
        return error_response(500, "database_error", "The attachment could not be deleted. Please try again.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error deleting exhibit {evidence_id}: {e}", exc_info=True)
        return error_response(500, "internal_error", "The attachment could not be deleted. Please try again.")


def _serialize(doc):
    return {
        "id": doc.id,
        "response_id": doc.response_id,
        "filename": doc.filename,
        "file_size_bytes": doc.file_size_bytes,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
    }


def _serialize_file(doc):
    return {
        "id": doc.id,
        "filename": doc.filename,
        "size": doc.file_size_bytes,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
    }


# ---------- /criteria/<id>/exhibits: {"status": ...} envelope ----------

@exhibit_bp.route("/api/applications/<int:application_id>/criteria/<criterion_id>/exhibits", methods=["GET"])
@login_required
def list_criterion_exhibits(application_id, criterion_id):
    result = _list_documents(application_id, criterion_id)
    if isinstance(result, tuple):
        return result
    return jsonify({"status": "success", "exhibits": [_serialize_file(d) for d in result]}), 200


@exhibit_bp.route("/api/applications/<int:application_id>/criteria/<criterion_id>/exhibits", methods=["POST"])
@login_required
def upload_criterion_exhibit(application_id, criterion_id):
    result = _perform_upload(application_id, criterion_id)
    if isinstance(result, tuple):
        return result
    return jsonify({"status": "success", "file": _serialize_file(result)}), 201


@exhibit_bp.route("/api/applications/<int:application_id>/criteria/<criterion_id>/exhibits/<int:evidence_id>",
                  methods=["DELETE"])
@login_required
def delete_criterion_exhibit(application_id, criterion_id, evidence_id):
    err = _perform_delete(application_id, criterion_id, evidence_id)
    if err:
        return err
    return jsonify({"status": "success", "evidence_id": evidence_id}), 200


# ---------- /responses/<id>/exhibit: the shape app/static/wizard.js reads ----------

@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit", methods=["POST"])
@login_required
def upload_exhibit(application_id, criterion_id):
    result = _perform_upload(application_id, criterion_id)
    if isinstance(result, tuple):
        return result
    return jsonify(_serialize(result)), 201


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit", methods=["GET"])
@login_required
def get_exhibits(application_id, criterion_id):
    result = _list_documents(application_id, criterion_id)
    if isinstance(result, tuple):
        return result
    return jsonify([_serialize(doc) for doc in result]), 200


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit/<int:evidence_id>", methods=["DELETE"])
@login_required
def delete_exhibit(application_id, criterion_id, evidence_id):
    err = _perform_delete(application_id, criterion_id, evidence_id)
    if err:
        return err
    return jsonify({"message": "Exhibit deleted successfully", "evidence_id": evidence_id}), 200
