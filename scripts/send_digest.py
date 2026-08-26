# scripts/send_digest.py
import os
import sys
import logging
from datetime import date, datetime

# Insert project root into sys.path to allow imports from app
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from app import create_app, db
from app.models import Application, ApplicationMember, Portfolio, Users, StandardPack
from app.wizard_routes import calculate_completeness

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("send_digest")

def send_digest_email(to_email: str, subject: str, body_text: str) -> None:
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject,
        plain_text_content=body_text
    )
    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(f"Successfully sent digest email to {to_email} (Status: {response.status_code})")
    except Exception as e:
        logger.error(f"Failed to send digest email to {to_email}: {str(e)}", exc_info=True)
        raise

def main():
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")

    if not api_key or not from_email:
        logger.error("SendGrid configuration missing. Both SENDGRID_API_KEY and SENDGRID_FROM_EMAIL environment variables must be set.")
        sys.exit(1)

    app = create_app()
    users_emailed_count = 0
    sends_failed_count = 0
    users_skipped_count = 0

    with app.app_context():
        try:
            applications = Application.query.all()
            
            # Map user_id -> list of dictionaries containing application and completeness details
            user_applications_map = {}
            # Track users to their email addresses
            user_email_map = {}

            # Populate user email lookup
            all_users = Users.query.all()
            for u in all_users:
                user_email_map[u.id] = u.email

            for app_record in applications:
                # Determine user IDs with access: Owner + Members
                owner_id = app_record.portfolio.owner_user_id if app_record.portfolio else None
                member_user_ids = [m.user_id for m in app_record.members]
                
                access_user_ids = set()
                if owner_id:
                    access_user_ids.add(owner_id)
                for uid in member_user_ids:
                    access_user_ids.add(uid)

                if not access_user_ids:
                    continue

                # Calculate completeness
                try:
                    comp_data = calculate_completeness(app_record.id)
                except Exception as comp_err:
                    logger.error(f"Error calculating completeness for application {app_record.id}: {str(comp_err)}", exc_info=True)
                    continue

                percentage = comp_data.get("percentage", 0.0)
                if percentage >= 100.0:
                    # Skip fully complete applications entirely
                    continue

                # StandardPack details
                pack = StandardPack.query.get(app_record.standard_pack_id)
                accrediting_body = pack.accrediting_body if pack else "Accreditation"
                app_type = pack.application_type.capitalize() if pack and pack.application_type else "Application"

                app_info = {
                    "application": app_record,
                    "accrediting_body": accrediting_body,
                    "application_type": app_type,
                    "completeness_data": comp_data
                }

                for uid in access_user_ids:
                    if uid not in user_applications_map:
                        user_applications_map[uid] = []
                    user_applications_map[uid].append(app_info)

            # Check users and build digest
            all_user_ids = list(user_email_map.keys())
            for uid in all_user_ids:
                user_email = user_email_map.get(uid)
                if not user_email:
                    continue

                app_list = user_applications_map.get(uid, [])
                if not app_list:
                    users_skipped_count += 1
                    continue

                # Compose email body
                body_lines = [
                    "Hello,",
                    "",
                    "Here is your weekly accreditation compliance digest summarizing your incomplete applications:",
                    ""
                ]

                today = date.today()

                for item in app_list:
                    app_rec = item["application"]
                    body = item["completeness_data"]
                    body_lines.append(f"--- {item['accrediting_body']} {item['application_type']} (Application #{app_rec.id}) ---")
                    body_lines.append(f"Completeness: {body['percentage']}% ({body['completed']} / {body['total_required']} items completed)")

                    if app_rec.due_date:
                        due_date_obj = app_rec.due_date
                        diff_days = (due_date_obj - today).days
                        due_str = due_date_obj.strftime("%Y-%m-%d")

                        if diff_days > 14:
                            body_lines.append(f"Due Date: {due_str} (Due in {diff_days} days)")
                        elif 1 <= diff_days <= 14:
                            body_lines.append(f"Due Date: {due_str} (URGENT: Due in {diff_days} days!)")
                        elif diff_days == 0:
                            body_lines.append(f"Due Date: {due_str} (DUE TODAY)")
                        else:
                            overdue_days = abs(diff_days)
                            body_lines.append(f"Due Date: {due_str} (OVERDUE by {overdue_days} days!)")
                    else:
                        body_lines.append("Due Date: No due date set")

                    body_lines.append("Next items to tackle:")
                    incomplete_crits = body.get("incomplete_criteria", [])
                    displayed_crits = incomplete_crits[:5]
                    for idx, crit in enumerate(displayed_crits, 1):
                        q_text = crit.get("question_text", "")
                        if len(q_text) > 100:
                            q_text = q_text[:97] + "..."
                        body_lines.append(f"  {idx}. [{crit.get('section_name', '')}] {q_text}")

                    remaining_count = len(incomplete_crits) - len(displayed_crits)
                    if remaining_count > 0:
                        body_lines.append(f"  ...and {remaining_count} more items remaining.")

                    body_lines.append("")

                body_lines.append("Log in to Accredo to continue working on your submissions.")
                body_lines.append("Best regards,\nAccredo Compliance Automation")

                email_body = "\n".join(body_lines)
                subject = "Accredo Weekly Compliance Digest"

                try:
                    send_digest_email(user_email, subject, email_body)
                    users_emailed_count += 1
                except Exception:
                    sends_failed_count += 1

        except Exception as query_err:
            logger.error(f"Critical error during database query phase of weekly digest: {str(query_err)}", exc_info=True)
            sys.exit(1)

    logger.info("=== Weekly Digest Summary ===")
    logger.info(f"Users Emailed: {users_emailed_count}")
    logger.info(f"Sends Failed: {sends_failed_count}")
    logger.info(f"Users Skipped (Nothing to Report): {users_skipped_count}")

if __name__ == "__main__":
    main()