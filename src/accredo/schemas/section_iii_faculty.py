"""Section III: Educational Administration & Faculty Qualifications."""
from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .base import STRICT_CONFIG, ComplianceFinding, ComplianceStatus, clean_names, worst_status

FACULTY_MIN_INDUSTRY_YEARS = 2.0
FACULTY_MIN_CE_HOURS_12MO = 1.0
EVALUATION_MAX_AGE = timedelta(days=366)  # "annual", allowing for a leap year


class FacultyMember(BaseModel):
    model_config = STRICT_CONFIG

    faculty_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    courses_assigned: list[str]
    # One number for all assigned courses; per-subject experience is not modelled yet.
    relevant_industry_experience_years: float = Field(ge=0, le=80)
    degree_level: str = Field(min_length=1)
    official_transcripts_on_file: bool
    continuing_ed_hours_last_12mo: float = Field(ge=0, le=8760)
    annual_evaluation_date: Optional[date] = None

    @field_validator("courses_assigned")
    @classmethod
    def _courses(cls, v: list[str]) -> list[str]:
        return clean_names(v, "courses_assigned")

    def findings(self, as_of: Optional[date] = None) -> list[ComplianceFinding]:
        """as_of enables the evaluation-recency check; without it only a missing evaluation is flagged."""
        subject = f"faculty {self.faculty_id} ({self.name})"
        out = []

        def add(status, rule, message):
            out.append(ComplianceFinding(status=status, rule=rule, subject=subject, message=message))

        if not self.official_transcripts_on_file:
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "FAC_TRANSCRIPTS",
                "Official transcripts are not on file.")
        if self.relevant_industry_experience_years < FACULTY_MIN_INDUSTRY_YEARS:
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "FAC_INDUSTRY_EXPERIENCE",
                f"{self.relevant_industry_experience_years:g} years of related occupational experience; "
                f"at least {FACULTY_MIN_INDUSTRY_YEARS:g} required.")
        if self.continuing_ed_hours_last_12mo < FACULTY_MIN_CE_HOURS_12MO:
            add(ComplianceStatus.PARTIAL_DEFICIENCY, "FAC_CONTINUING_ED",
                f"{self.continuing_ed_hours_last_12mo:g} continuing-education hours in the last 12 months; "
                f"at least {FACULTY_MIN_CE_HOURS_12MO:g} required.")
        if self.annual_evaluation_date is None:
            add(ComplianceStatus.PARTIAL_DEFICIENCY, "FAC_ANNUAL_EVALUATION", "No annual evaluation on file.")
        elif as_of is not None:
            if self.annual_evaluation_date > as_of:
                add(ComplianceStatus.UNVERIFIED, "FAC_ANNUAL_EVALUATION",
                    f"Evaluation date {self.annual_evaluation_date.isoformat()} is after {as_of.isoformat()}.")
            elif as_of - self.annual_evaluation_date > EVALUATION_MAX_AGE:
                add(ComplianceStatus.PARTIAL_DEFICIENCY, "FAC_ANNUAL_EVALUATION",
                    f"Last evaluation {self.annual_evaluation_date.isoformat()} is more than a year before "
                    f"{as_of.isoformat()}.")
        return out

    def compliance_status(self, as_of: Optional[date] = None) -> ComplianceStatus:
        return worst_status(f.status for f in self.findings(as_of))
