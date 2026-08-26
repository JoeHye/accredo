import json
import os
import logging
from datetime import datetime
from app import db
from app.models import StandardPack, Criterion

loader_logger = logging.getLogger(__name__)

def load_standard_pack(json_path: str) -> StandardPack:
    absolute_path = os.path.abspath(json_path)

    if not os.path.exists(absolute_path):
        raise FileNotFoundError(f"Standard pack JSON file not found at absolute path: {absolute_path}")

    try:
        with open(absolute_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        loader_logger.error(f"Failed to parse JSON file at {absolute_path}: {str(e)}", exc_info=True)
        raise

    pack_id = data.get("standard_pack_id")
    accrediting_body = data.get("accrediting_body")
    application_type = data.get("application_type")
    version_date_str = data.get("version_effective_date")

    try:
        version_effective_date = datetime.strptime(version_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError) as e:
        loader_logger.error(f"Invalid or missing date format for version_effective_date: {version_date_str}", exc_info=True)
        raise

    existing_pack = StandardPack.query.filter_by(
        id=pack_id
    ).first() or StandardPack.query.filter_by(
        accrediting_body=accrediting_body,
        application_type=application_type,
        version_effective_date=version_effective_date
    ).first()

    if existing_pack:
        loader_logger.info(f"StandardPack already exists (ID: {existing_pack.id}). Skipping creation and import.")
        return existing_pack

    try:
        new_pack = StandardPack(
            id=pack_id,
            accrediting_body=accrediting_body,
            application_type=application_type,
            version_effective_date=version_effective_date
        )
        db.session.add(new_pack)
        db.session.flush()

        global_sort_order = 0

        sections = data.get("sections", [])
        for section in sections:
            sec_code = section.get("section_code")
            sec_name = section.get("section_name")

            subsections = section.get("subsections", [])
            for subsection in subsections:
                sub_code = subsection.get("subsection_code")
                sub_name = subsection.get("subsection_name")

                criteria = subsection.get("criteria", [])
                for criterion_data in criteria:
                    global_sort_order += 1

                    def process_criterion(c_data, parent_criterion_id=None):
                        nonlocal global_sort_order
                        crit_id = c_data.get("criterion_id")
                        question_text = c_data.get("question") or c_data.get("question_text")
                        answer_type = c_data.get("answer_type", "narrative")
                        required = c_data.get("required", True)
                        table_columns = c_data.get("table_columns", None)
                        item_number = c_data.get("item_number", crit_id)

                        criterion_record = Criterion(
                            id=crit_id,
                            standard_pack_id=new_pack.id,
                            parent_id=parent_criterion_id,
                            section_code=sec_code,
                            section_name=sec_name,
                            subsection_code=sub_code,
                            subsection_name=sub_name,
                            item_number=str(item_number),
                            question_text=question_text,
                            answer_type=answer_type,
                            required=required,
                            table_columns=table_columns,
                            sort_order=global_sort_order
                        )
                        db.session.add(criterion_record)

                        sub_criteria_list = c_data.get("sub_criteria", []) or c_data.get("sub_items", [])
                        for sub_crit in sub_criteria_list:
                            global_sort_order += 1
                            process_criterion(sub_crit, parent_criterion_id=crit_id)

                    process_criterion(criterion_data, parent_criterion_id=None)

        db.session.commit()
        loader_logger.info(f"Successfully loaded StandardPack '{pack_id}' with all associated criteria.")
        return new_pack

    except Exception as e:
        db.session.rollback()
        loader_logger.error(f"Database write failed during load_standard_pack operation for pack ID '{pack_id}': {str(e)}", exc_info=True)
        raise
