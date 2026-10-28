"""Section III: Educational Administration & Faculty Qualifications."""
import re
from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .base import STRICT_CONFIG, ComplianceFinding, ComplianceStatus, clean_names, worst_status

# Related practical work experience required to teach (ACCSC Section III.B.5, per the Oct 28 directive).
FACULTY_MIN_INDUSTRY_YEARS_NON_DEGREE = 3.0   # diploma / certificate / non-degree courses
FACULTY_MIN_INDUSTRY_YEARS_DEGREE = 4.0       # associate / baccalaureate degree courses
_DEGREE_LEVEL = re.compile(r"\b(associate'?s?|bachelor'?s?|baccalaureate|aas|aos|bs|ba)\b")
_NON_DEGREE_LEVEL = re.compile(r"\b(diploma|certificate|non-?degree)\b")
FACULTY_MIN_CE_HOURS_12MO = 1.0
EVALUATION_MAX_AGE = timedelta(days=366)  # "annual", allowing for a leap year


class FacultyMember(BaseModel):
    model_config = STRICT_CONFIG

    faculty_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    courses_assigned: list[str]
    # One number for all assigned courses; per-subject experience is not modelled yet.
    relevant_industry_experience_years: float = Field(ge=0, le=80)
    # Level of the courses taught (drives the experience minimum), e.g. "Diploma", "Associate (AAS)".
    degree_level: str = Field(min_length=1)
    official_transcripts_on_file: bool
    continuing_ed_hours_last_12mo: float = Field(ge=0, le=8760)
    annual_evaluation_date: Optional[date] = None

    @field_validator("courses_assigned")
    @classmethod
    def _courses(cls, v: list[str]) -> list[str]:
        return clean_names(v, "courses_assigned")

    def teaches_degree_level(self) -> Optional[bool]:
        """True for associate/baccalaureate, False for diploma/certificate/non-degree, None if unrecognised."""
        level = self.degree_level.casefold()
        if _DEGREE_LEVEL.search(level):
            return True
        if _NON_DEGREE_LEVEL.search(level):
            return False
        return None

    def required_industry_years(self) -> float:
        """4.0 for degree-level courses, else the 3.0 non-degree baseline (also used when unrecognised)."""
        if self.teaches_degree_level():
            return FACULTY_MIN_INDUSTRY_YEARS_DEGREE
        return FACULTY_MIN_INDUSTRY_YEARS_NON_DEGREE

    def findings(self, as_of: Optional[date] = None) -> list[ComplianceFinding]:
        """as_of enables the evaluation-recency check; without it only a missing evaluation is flagged."""
        subject = f"faculty {self.faculty_id} ({self.name})"
        out = []

        def add(status, rule, message):
            out.append(ComplianceFinding(status=status, rule=rule, subject=subject, message=message))

        if not self.official_transcripts_on_file:
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "FAC_TRANSCRIPTS",
                "Official transcripts are not on file.")
        required = self.required_industry_years()
        level_known = self.teaches_degree_level() is not None
        if not level_known:
            add(ComplianceStatus.UNVERIFIED, "FAC_DEGREE_LEVEL",
                f"degree_level {self.degree_level!r} not recognised as degree or non-degree; "
                f"the {required:g}-year non-degree baseline was applied.")
        if self.relevant_industry_experience_years < required:
            kind = "degree-level" if self.teaches_degree_level() else "non-degree"
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "FAC_INDUSTRY_EXPERIENCE",
                f"{self.relevant_industry_experience_years:g} years of related practical experience; "
                f"at least {required:g} required for {kind} courses.")
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
