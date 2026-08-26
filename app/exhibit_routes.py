# app/exhibit_routes.py
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import Application, Criterion, Response, EvidenceDocument, ApplicationMember

logger = logging.getLogger(__name__)
exhibit_bp = Blueprint("exhibit", __name__)

ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}
MAX_FILE_SIZE = 25 * 1024 * 1024

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def verify_application_ownership(application_id):
    try:
        app_record = Application.query.get(application_id)
        if not app_record:
            return None, jsonify({"error": "Application not found"}), 404
        is_owner = app_record.portfolio and app_record.portfolio.owner_user_id == current_user.id
        is_member = ApplicationMember.query.filter_by(
            application_id=application_id, user_id=current_user.id
        ).first() is not None
        if not is_owner and not is_member:
            return None, jsonify({"error": "Forbidden", "message": "You do not have access to this application"}), 403
        return app_record, None, None
    except Exception as e:
        logger.error(f"Error verifying application ownership for app {application_id}: {str(e)}", exc_info=True)
        return None, jsonify({"error": "Internal server error"}), 500

def upload_file_storage(file_obj, filename: str, application_id: int) -> str:
    gcs_bucket_name = os.environ.get("GCS_BUCKET_NAME")

    if gcs_bucket_name:
        logger.info(f"GCS storage mode active. Uploading to bucket: {gcs_bucket_name}")
        try:
            from google.cloud import storage

            client = storage.Client()
            bucket = client.bucket(gcs_bucket_name)
            blob_path = f"applications/{application_id}/exhibits/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{filename}"
            blob = bucket.blob(blob_path)

            blob.upload_from_file(file_obj, timeout=60)
            return f"gs://{gcs_bucket_name}/{blob_path}"
        except Exception as e:
            logger.error(f"GCS upload failed for application {application_id}, file {filename}: {str(e)}", exc_info=True)
            raise
    else:
        upload_dir = os.environ.get("EXHIBIT_UPLOAD_DIR")
        if not upload_dir:
            base_dir = os.path.abspath(os.path.dirname(__file__))
            upload_dir = os.path.join(base_dir, "..", "instance", "exhibits")

        absolute_upload_dir = os.path.abspath(upload_dir)
        os.makedirs(absolute_upload_dir, exist_ok=True)
        logger.info(f"Local exhibit storage fallback active. Absolute directory: {absolute_upload_dir}")

        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        saved_filename = f"{timestamp}_{secure_filename(filename)}"
        absolute_file_path = os.path.join(absolute_upload_dir, saved_filename)

        try:
            file_obj.seek(0)
            file_obj.save(absolute_file_path)
            return absolute_file_path
        except Exception as e:
            logger.error(f"Local file save failed for application {application_id}, file {filename}: {str(e)}", exc_info=True)
            if os.path.exists(absolute_file_path):
                try:
                    os.remove(absolute_file_path)
                except Exception:
                    pass
            raise


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit", methods=["POST"])
@login_required
def upload_exhibit(application_id, criterion_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        criterion = Criterion.query.get(criterion_id)
        if not criterion:
            return jsonify({"error": "Criterion not found"}), 404

        if criterion.answer_type != "exhibit":
            return jsonify({
                "error": "Invalid criterion answer type",
                "details": f"Criterion {criterion_id} has answer_type '{criterion.answer_type}', expected 'exhibit'."
            }), 400

        if "file" not in request.files:
            return jsonify({"error": "No file part in the request"}), 400

        file = request.files["file"]
        if not file or file.filename == "":
            return jsonify({"error": "No selected file"}), 400

        if not allowed_file(file.filename):
            return jsonify({
                "error": "Invalid file type",
                "details": f"Allowed formats are: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            }), 400

        file.seek(0, os.SEEK_END)
        file_size_bytes = file.tell()
        file.seek(0)

        if file_size_bytes > MAX_FILE_SIZE:
            return jsonify({
                "error": "File size exceeds limit",
                "details": f"Maximum allowed file size is 25MB. Uploaded file is {round(file_size_bytes / (1024 * 1024), 2)}MB."
            }), 413

        response_record = Response.query.filter_by(
            application_id=application_id,
            criterion_id=criterion_id
        ).first()

        now = datetime.utcnow()
        if not response_record:
            response_record = Response(
                application_id=application_id,
                criterion_id=criterion_id,
                narrative_text="",
                not_applicable=False,
                created_at=now,
                updated_at=now
            )
            db.session.add(response_record)
            db.session.flush()

        secure_name = secure_filename(file.filename)
        storage_path = upload_file_storage(file, secure_name, application_id)

        evidence_doc = EvidenceDocument(
            response_id=response_record.id,
            filename=secure_name,
            gcs_path=storage_path,
            uploaded_at=now,
            file_size_bytes=file_size_bytes
        )
        db.session.add(evidence_doc)
        db.session.commit()

        logger.info(f"Successfully uploaded exhibit '{secure_name}' for app {application_id}, criterion {criterion_id}")

        return jsonify({
            "id": evidence_doc.id,
            "response_id": evidence_doc.response_id,
            "filename": evidence_doc.filename,
            "gcs_path": evidence_doc.gcs_path,
            "file_size_bytes": evidence_doc.file_size_bytes,
            "uploaded_at": evidence_doc.uploaded_at.isoformat()
        }), 201

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error during exhibit upload (app {application_id}, criterion {criterion_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "Database error during exhibit upload", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error during exhibit upload (app {application_id}, criterion {criterion_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error during exhibit upload", "details": str(e)}), 500


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit", methods=["GET"])
@login_required
def get_exhibits(application_id, criterion_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        criterion = Criterion.query.get(criterion_id)
        if not criterion:
            return jsonify({"error": "Criterion not found"}), 404

        response_record = Response.query.filter_by(
            application_id=application_id,
            criterion_id=criterion_id
        ).first()

        if not response_record:
            return jsonify([]), 200

        documents = EvidenceDocument.query.filter_by(response_id=response_record.id).all()

        result = [{
            "id": doc.id,
            "filename": doc.filename,
            "gcs_path": doc.gcs_path,
            "file_size_bytes": doc.file_size_bytes,
            "uploaded_at": doc.uploaded_at.isoformat()
        } for doc in documents]

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error retrieving exhibits for app {application_id}, criterion {criterion_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@exhibit_bp.route("/api/applications/<int:application_id>/responses/<criterion_id>/exhibit/<int:evidence_id>", methods=["DELETE"])
@login_required
def delete_exhibit(application_id, criterion_id, evidence_id):
    try:
        app_record, err_resp, err_code = verify_application_ownership(application_id)
        if err_resp:
            return err_resp, err_code

        criterion = Criterion.query.get(criterion_id)
        if not criterion:
            return jsonify({"error": "Criterion not found"}), 404

        response_record = Response.query.filter_by(
            application_id=application_id,
            criterion_id=criterion_id
        ).first()

        if not response_record:
            return jsonify({"error": "Response record not found"}), 404

        doc = EvidenceDocument.query.filter_by(id=evidence_id, response_id=response_record.id).first()
        if not doc:
            return jsonify({"error": "EvidenceDocument not found"}), 404

        gcs_bucket_name = os.environ.get("GCS_BUCKET_NAME")

        if gcs_bucket_name and doc.gcs_path.startswith("gs://"):
            try:
                from google.cloud import storage
                client = storage.Client()
                bucket = client.bucket(gcs_bucket_name)
                prefix = f"gs://{gcs_bucket_name}/"
                blob_path = doc.gcs_path[len(prefix):] if doc.gcs_path.startswith(prefix) else doc.gcs_path
                blob = bucket.blob(blob_path)
                blob.delete(timeout=30)
                logger.info(f"Successfully deleted GCS blob: {doc.gcs_path}")
            except Exception as e:
                logger.error(f"Failed to delete GCS blob {doc.gcs_path}: {str(e)}", exc_info=True)
                return jsonify({"error": "Failed to delete underlying storage file from GCS", "details": str(e)}), 500
        else:
            local_path = doc.gcs_path
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    logger.info(f"Successfully deleted local file: {local_path}")
                except Exception as e:
                    logger.error(f"Failed to delete local file {local_path}: {str(e)}", exc_info=True)
                    return jsonify({"error": "Failed to delete underlying local file", "details": str(e)}), 500

        db.session.delete(doc)
        db.session.commit()
        logger.info(f"Successfully deleted EvidenceDocument ID {evidence_id} for app {application_id}, criterion {criterion_id}")

        return jsonify({"message": "Exhibit deleted successfully", "evidence_id": evidence_id}), 200

    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"Database error deleting exhibit {evidence_id} (app {application_id}, criterion {criterion_id}): {str(e)}", exc_info=True)
        return jsonify({"error": "Database error during exhibit deletion", "details": str(e)}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(f"Unexpected error deleting exhibit {evidence_id}: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error during exhibit deletion", "details": str(e)}), 500