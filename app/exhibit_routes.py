import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from flask_login import login_required
from werkzeug.utils import secure_filename
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import Criterion, Response, EvidenceDocument
# Shared access rules: owner OR invited ApplicationMember may view; edits respect subsection assignments.
from app.wizard_routes import verify_application_ownership, check_edit_permission

logger = logging.getLogger(__name__)
exhibit_bp = Blueprint("exhibit", __name__)

ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}
MAX_FILE_SIZE = 25 * 1024 * 1024


class StorageError(Exception):
    """A classified failure talking to exhibit storage. `message` is safe to show users; details go to logs."""

    def __init__(self, code, status, message):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def error_response(status, code, message):
    return jsonify({"error": message, "code": code}), status


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


def _gcs_bucket_name():
    """Bucket from env. On Cloud Run a missing bucket must not silently fall back to ephemeral local disk."""
    name = os.environ.get("GCS_BUCKET_NAME")
    if not name and os.environ.get("K_SERVICE"):
        logger.error("GCS_BUCKET_NAME is not set on Cloud Run; refusing to store exhibits on ephemeral disk.")
        raise StorageError("storage_not_configured", 500,
                           "File storage is not configured on the server. Your file was not saved. "
                           "Please contact support.")
    return name


def upload_file_storage(file_obj, filename: str, application_id: int) -> str:
    gcs_bucket_name = _gcs_bucket_name()

    if gcs_bucket_name:
        blob_path = f"applications/{application_id}/exhibits/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{filename}"
        logger.info(f"Uploading exhibit to gs://{gcs_bucket_name}/{blob_path}")
        try:
            from google.cloud import storage

            client = storage.Client()
            blob = client.bucket(gcs_bucket_name).blob(blob_path)
            file_obj.seek(0)
            blob.upload_from_file(file_obj, timeout=60)
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
    saved_filename = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{secure_filename(filename)}"
    absolute_file_path = os.path.join(absolute_upload_dir, saved_filename)
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
                storage.Client().bucket(bucket_name).blob(blob_path).delete(timeout=30)
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


def _serialize(doc):
    return {
        "id": doc.id,
        "response_id": doc.response_id,
        "filename": doc.filename,
        "file_size_bytes": doc.file_size_bytes,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
    }


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit", methods=["POST"])
@login_required
def upload_exhibit(application_id, criterion_id):
    storage_path = None
    try:
        criterion, err = _load_exhibit_criterion(application_id, criterion_id, require_edit=True)
        if err:
            return err

        if criterion.answer_type != "exhibit":
            return error_response(400, "not_an_exhibit_criterion",
                                  f"Criterion {criterion_id} does not accept file uploads.")

        file = request.files.get("file")
        if not file or not file.filename:
            return error_response(400, "no_file", "No file was selected. Choose a file and try again.")

        secure_name = secure_filename(file.filename)
        if not secure_name or not allowed_file(secure_name):
            return error_response(400, "invalid_file_type",
                                  f"That file type is not allowed. Allowed formats: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")

        file.seek(0, os.SEEK_END)
        file_size_bytes = file.tell()
        file.seek(0)
        if file_size_bytes == 0:
            return error_response(400, "empty_file", "The selected file is empty.")
        if file_size_bytes > MAX_FILE_SIZE:
            return error_response(413, "file_too_large",
                                  f"Files must be 25 MB or smaller. This file is {round(file_size_bytes / (1024 * 1024), 2)} MB.")

        now = datetime.utcnow()
        response_record = Response.query.filter_by(application_id=application_id, criterion_id=criterion_id).first()
        if not response_record:
            response_record = Response(application_id=application_id, criterion_id=criterion_id, narrative_text="",
                                       not_applicable=False, created_at=now, updated_at=now)
            db.session.add(response_record)
            db.session.flush()

        storage_path = upload_file_storage(file, secure_name, application_id)

        evidence_doc = EvidenceDocument(response_id=response_record.id, filename=secure_name, gcs_path=storage_path,
                                        uploaded_at=now, file_size_bytes=file_size_bytes)
        db.session.add(evidence_doc)
        db.session.commit()

        logger.info(f"Uploaded exhibit '{secure_name}' ({file_size_bytes} bytes) for app {application_id}, criterion {criterion_id}")
        return jsonify(_serialize(evidence_doc)), 201

    except StorageError as e:
        db.session.rollback()
        return error_response(e.status, e.code, e.message)
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during exhibit upload (app {application_id}, criterion {criterion_id}): {e}", exc_info=True)
        if storage_path:
            # The file reached storage but its database record did not: remove it so storage and DB stay consistent.
            try:
                delete_file_storage(storage_path)
            except StorageError:
                logger.error(f"ORPHANED exhibit file needs manual cleanup: {storage_path}")
        return error_response(500, "database_error", "Your file could not be recorded. Please try again.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during exhibit upload (app {application_id}, criterion {criterion_id}): {e}", exc_info=True)
        return error_response(500, "internal_error", "An unexpected error occurred during upload. Please try again.")


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit", methods=["GET"])
@login_required
def get_exhibits(application_id, criterion_id):
    try:
        _criterion, err = _load_exhibit_criterion(application_id, criterion_id, require_edit=False)
        if err:
            return err

        response_record = Response.query.filter_by(application_id=application_id, criterion_id=criterion_id).first()
        if not response_record:
            return jsonify([]), 200

        documents = (EvidenceDocument.query.filter_by(response_id=response_record.id)
                     .order_by(EvidenceDocument.uploaded_at, EvidenceDocument.id).all())
        return jsonify([_serialize(doc) for doc in documents]), 200

    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving exhibits for app {application_id}, criterion {criterion_id}: {e}", exc_info=True)
        return error_response(500, "database_error", "Attachments could not be loaded. Please refresh and try again.")
    except Exception as e:
        logger.error(f"Unexpected error retrieving exhibits for app {application_id}, criterion {criterion_id}: {e}", exc_info=True)
        return error_response(500, "internal_error", "Attachments could not be loaded. Please refresh and try again.")


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit/<int:evidence_id>", methods=["DELETE"])
@login_required
def delete_exhibit(application_id, criterion_id, evidence_id):
    try:
        _criterion, err = _load_exhibit_criterion(application_id, criterion_id, require_edit=True)
        if err:
            return err

        response_record = Response.query.filter_by(application_id=application_id, criterion_id=criterion_id).first()
        doc = (EvidenceDocument.query.filter_by(id=evidence_id, response_id=response_record.id).first()
               if response_record else None)
        if not doc:
            return error_response(404, "exhibit_not_found", "That attachment no longer exists.")

        delete_file_storage(doc.gcs_path)
        db.session.delete(doc)
        db.session.commit()
        logger.info(f"Deleted exhibit {evidence_id} for app {application_id}, criterion {criterion_id}")
        return jsonify({"message": "Exhibit deleted successfully", "evidence_id": evidence_id}), 200

    except StorageError as e:
        db.session.rollback()
        return error_response(e.status, e.code, e.message)
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error deleting exhibit {evidence_id} (app {application_id}, criterion {criterion_id}): {e}", exc_info=True)
        return error_response(500, "database_error", "The attachment could not be deleted. Please try again.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error deleting exhibit {evidence_id}: {e}", exc_info=True)
        return error_response(500, "internal_error", "The attachment could not be deleted. Please try again.")
