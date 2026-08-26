import os
import io
import logging
from datetime import datetime
from xml.sax.saxutils import escape
from PIL import Image as PILImage
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from app import db
from app.models import Application, StandardPack, Criterion, Response, Portfolio, EvidenceDocument, TableRowData

logger = logging.getLogger(__name__)

def safe_text(s) -> str:
    return escape(str(s)) if s is not None else ""

def generate_report(application_id: int) -> str:
    report_dir = os.environ.get("REPORT_OUTPUT_DIR")
    if not report_dir:
        base_dir = os.path.abspath(os.path.dirname(__file__))
        report_dir = os.path.join(base_dir, "..", "instance", "reports")
        logger.info(f"REPORT_OUTPUT_DIR not set. Defaulting to absolute path under instance folder: {report_dir}")
    else:
        report_dir = os.path.abspath(report_dir)
        logger.info(f"Using REPORT_OUTPUT_DIR from environment: {report_dir}")

    os.makedirs(report_dir, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"application_{application_id}_report_{timestamp}.pdf"
    pdf_path = os.path.join(report_dir, filename)

    stage = "initialization"
    try:
        stage = "loading_application_data"
        app_record = Application.query.get(application_id)
        if not app_record:
            raise ValueError(f"Application with ID {application_id} not found.")

        pack = StandardPack.query.get(app_record.standard_pack_id)
        if not pack:
            raise ValueError(f"StandardPack {app_record.standard_pack_id} not found for application {application_id}.")

        portfolio = Portfolio.query.get(app_record.portfolio_id)
        portfolio_name = portfolio.name if portfolio else "Unknown Portfolio"

        criteria = Criterion.query.filter_by(standard_pack_id=pack.id).order_by(Criterion.sort_order).all()
        responses = Response.query.filter_by(application_id=application_id).all()
        response_map = {r.criterion_id: r for r in responses}

        stage = "structuring_content"
        sections_dict = {}
        for c in criteria:
            sec_code = c.section_code
            sec_name = c.section_name
            sub_code = c.subsection_code or ""
            sub_name = c.subsection_name or ""

            if sec_code not in sections_dict:
                sections_dict[sec_code] = {"section_name": sec_name, "subsections": {}}
            if sub_code not in sections_dict[sec_code]["subsections"]:
                sections_dict[sec_code]["subsections"][sub_code] = {"subsection_name": sub_name, "criteria": []}
            sections_dict[sec_code]["subsections"][sub_code]["criteria"].append(c)

        subsection_na_map = {}
        for sec_code, sec_data in sections_dict.items():
            for sub_code, sub_data in sec_data["subsections"].items():
                crits = sub_data["criteria"]
                all_na = True if crits else False
                for c in crits:
                    resp = response_map.get(c.id)
                    if not resp or not resp.not_applicable:
                        all_na = False
                        break
                subsection_na_map[(sec_code, sub_code)] = all_na

        stage = "pdf_generation"
        doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=54, leftMargin=54, topMargin=54, bottomMargin=54)
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('CoverTitle', parent=styles['Heading1'], fontSize=22, leading=26, textColor=colors.HexColor("#1e293b"), spaceAfter=12, alignment=1)
        subtitle_style = ParagraphStyle('CoverSubtitle', parent=styles['Normal'], fontSize=11, leading=15, textColor=colors.HexColor("#64748b"), spaceAfter=6, alignment=1)
        section_style = ParagraphStyle('SectionHeader', parent=styles['Heading1'], fontSize=15, leading=18, textColor=colors.HexColor("#0f172a"), spaceBefore=18, spaceAfter=8, keepWithNext=True)
        subsection_style = ParagraphStyle('SubsectionHeader', parent=styles['Heading2'], fontSize=12, leading=15, textColor=colors.HexColor("#334155"), spaceBefore=12, spaceAfter=6, keepWithNext=True)
        criterion_title_style = ParagraphStyle('CriterionTitle', parent=styles['Heading3'], fontSize=10, leading=13, textColor=colors.HexColor("#475569"), spaceBefore=8, spaceAfter=4, keepWithNext=True)
        question_style = ParagraphStyle('QuestionText', parent=styles['Normal'], fontSize=9.5, leading=13, textColor=colors.HexColor("#1e293b"), spaceAfter=4)
        narrative_style = ParagraphStyle('NarrativeText', parent=styles['Normal'], fontSize=9.5, leading=13, textColor=colors.HexColor("#0f172a"), leftIndent=12, spaceAfter=8)
        incomplete_style = ParagraphStyle('IncompleteText', parent=styles['Normal'], fontSize=9.5, leading=13, textColor=colors.HexColor("#dc2626"), fontName="Helvetica-Bold", leftIndent=12, spaceAfter=8)
        na_banner_style = ParagraphStyle('NABanner', parent=styles['Normal'], fontSize=10, leading=13, textColor=colors.HexColor("#d97706"), fontName="Helvetica-Bold", leftIndent=12, spaceAfter=8, spaceBefore=4)

        table_cell_style = ParagraphStyle('TableCell', parent=styles['Normal'], fontSize=8.5, leading=11, textColor=colors.HexColor("#0f172a"))
        table_header_style = ParagraphStyle('TableCellHeader', parent=styles['Normal'], fontSize=8.5, leading=11, textColor=colors.HexColor("#0f172a"), fontName='Helvetica-Bold')

        story = []
        story.append(Spacer(1, 100))
        story.append(Paragraph("ACCSC Self-Evaluation Report (SER)", title_style))
        story.append(Spacer(1, 15))
        story.append(Paragraph(f"<b>Portfolio:</b> {safe_text(portfolio_name)}", subtitle_style))
        story.append(Paragraph(f"<b>Application ID:</b> {application_id}", subtitle_style))
        story.append(Paragraph(f"<b>Accrediting Body:</b> {safe_text(pack.accrediting_body)}", subtitle_style))
        story.append(Paragraph(f"<b>Standard Pack Version Date:</b> {safe_text(pack.version_effective_date)}", subtitle_style))
        story.append(Paragraph(f"<b>Application Type:</b> {safe_text(pack.application_type.capitalize())}", subtitle_style))
        story.append(Spacer(1, 35))
        story.append(Paragraph(f"<b>Generated At (UTC):</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}", subtitle_style))
        story.append(PageBreak())

        for sec_code in sorted(sections_dict.keys()):
            sec_data = sections_dict[sec_code]
            sec_heading = f"Section {safe_text(sec_code)}: {safe_text(sec_data['section_name'])}"
            story.append(Paragraph(sec_heading, section_style))
            story.append(Spacer(1, 4))

            for sub_code in sorted(sec_data["subsections"].keys()):
                sub_data = sec_data["subsections"][sub_code]
                sub_heading = f"Subsection {safe_text(sub_code)}: {safe_text(sub_data['subsection_name'])}"
                story.append(Paragraph(sub_heading, subsection_style))

                is_sub_na = subsection_na_map.get((sec_code, sub_code), False)
                if is_sub_na:
                    story.append(Paragraph("MARKED NOT APPLICABLE", na_banner_style))
                    story.append(Spacer(1, 6))
                    continue

                top_criteria = [c for c in sub_data["criteria"] if c.parent_id is None]

                def render_criterion_recursive(crit):
                    resp = response_map.get(crit.id)
                    narrative = resp.narrative_text if resp and resp.narrative_text else ""
                    crit_heading = f"<b>Item {safe_text(crit.item_number)}</b> (Criterion ID: {safe_text(crit.id)})"
                    story.append(Paragraph(crit_heading, criterion_title_style))
                    story.append(Paragraph(safe_text(crit.question_text), question_style))

                    if crit.answer_type == "exhibit":
                        docs = EvidenceDocument.query.filter_by(response_id=resp.id).all() if resp else []
                        if docs:
                            gcs_bucket_name = os.environ.get("GCS_BUCKET_NAME")
                            for d in docs:
                                ext = d.filename.rsplit(".", 1)[1].lower() if "." in d.filename else ""
                                if ext in {"jpg", "jpeg", "png"}:
                                    file_bytes = None
                                    try:
                                        if gcs_bucket_name and d.gcs_path.startswith("gs://"):
                                            from google.cloud import storage
                                            client = storage.Client()
                                            prefix = f"gs://{gcs_bucket_name}/"
                                            blob_path = d.gcs_path[len(prefix):] if d.gcs_path.startswith(prefix) else d.gcs_path
                                            bucket = client.bucket(gcs_bucket_name)
                                            blob = bucket.blob(blob_path)
                                            file_bytes = blob.download_as_bytes()
                                        else:
                                            if os.path.exists(d.gcs_path):
                                                with open(d.gcs_path, "rb") as f:
                                                    file_bytes = f.read()

                                        if file_bytes:
                                            pil_img = PILImage.open(io.BytesIO(file_bytes))
                                            orig_w, orig_h = pil_img.size
                                            max_w = 400
                                            if orig_w > max_w:
                                                scaled_w = max_w
                                                scaled_h = int(orig_h * (max_w / float(orig_w)))
                                            else:
                                                scaled_w = orig_w
                                                scaled_h = orig_h

                                            img_io = io.BytesIO(file_bytes)
                                            rl_img = RLImage(img_io, width=scaled_w, height=scaled_h)
                                            story.append(rl_img)
                                            story.append(Paragraph(f"<b>Attached Image:</b> {safe_text(d.filename)}", narrative_style))
                                        else:
                                            story.append(Paragraph(f"<b>Attached Image:</b> {safe_text(d.filename)} <i>[File content unreadable]</i>", narrative_style))
                                    except Exception as img_err:
                                        logger.error(f"Failed to embed image exhibit {d.filename}: {str(img_err)}", exc_info=True)
                                        story.append(Paragraph(f"<b>Attached Image:</b> {safe_text(d.filename)} <i>[Error loading image]</i>", narrative_style))
                                elif ext == "pdf":
                                    story.append(Paragraph(f"<b>Attached (PDF):</b> {safe_text(d.filename)}", narrative_style))
                                elif ext in {"doc", "docx"}:
                                    story.append(Paragraph(f"<b>Attached (Word):</b> {safe_text(d.filename)}", narrative_style))
                                else:
                                    story.append(Paragraph(f"<b>Attached:</b> {safe_text(d.filename)}", narrative_style))
                        else:
                            story.append(Paragraph("[EXHIBIT REQUIRED — FILE UPLOAD NOT YET IMPLEMENTED]", incomplete_style))
                    elif crit.answer_type == "table_rows":
                        rows = TableRowData.query.filter_by(response_id=resp.id).order_by(TableRowData.row_index).all() if resp else []
                        if rows and crit.table_columns:
                            table_data = [[Paragraph(safe_text(col), table_header_style) for col in crit.table_columns]]
                            for r in rows:
                                table_data.append([Paragraph(safe_text(r.row_data.get(col, "")), table_cell_style) for col in crit.table_columns])
                            t = Table(table_data, hAlign='LEFT')
                            t.setStyle(TableStyle([
                                ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#f1f5f9")),
                                ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor("#0f172a")),
                                ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
                                ('TOPPADDING', (0,0), (-1,-1), 4),
                                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                            ]))
                            story.append(t)
                        else:
                            story.append(Paragraph("[TABLE DATA — NOT YET ANSWERED]", incomplete_style))
                    else:
                        if narrative.strip():
                            escaped_narrative = safe_text(narrative).replace('\n', '<br/>')
                            story.append(Paragraph(escaped_narrative, narrative_style))
                        else:
                            story.append(Paragraph("[INCOMPLETE — NOT YET ANSWERED]", incomplete_style))

                    story.append(Spacer(1, 4))
                    sub_crits = [sc for sc in sub_data["criteria"] if sc.parent_id == crit.id]
                    for sc in sub_crits:
                        render_criterion_recursive(sc)

                for c in top_criteria:
                    render_criterion_recursive(c)

                story.append(Spacer(1, 6))

        doc.build(story)
        logger.info(f"Successfully generated report for application {application_id} at {pdf_path}")
        return pdf_path

    except Exception as e:
        logger.error(f"Error generating report for application {application_id} at stage '{stage}': {str(e)}", exc_info=True)
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
                logger.info(f"Cleaned up partial/corrupt report file at {pdf_path}")
            except Exception as cleanup_err:
                logger.error(f"Failed to delete partial report file {pdf_path}: {str(cleanup_err)}", exc_info=True)
        raise
