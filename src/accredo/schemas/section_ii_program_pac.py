"""Section II: Program Requirements & Program Advisory Committee (PAC) meetings.

A single PACMeetingRecord can only judge its own meeting. "At least two meetings a year per
program" spans many records, so that rule lives in validators.gap_analyzer.check_pac_frequency.
"""
from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

from .base import STRICT_CONFIG, ComplianceFinding, ComplianceStatus, clean_names, worst_status

PAC_MIN_EXTERNAL_MEMBERS = 2
PAC_MIN_MEETINGS_PER_YEAR = 2


class PACMeetingRecord(BaseModel):
    model_config = STRICT_CONFIG

    meeting_date: date
    program_name: str = Field(min_length=1)
    # External industry practitioners NOT employed by the institution.
    external_members_present: list[str]
    # Institution employees who attended. Recorded so an employee can't also be counted as external.
    institution_staff_present: list[str] = Field(default_factory=list)
    reviewed_curriculum: bool
    reviewed_equipment_and_facilities: bool
    reviewed_ge_data: bool
    minutes_documented: bool

    @field_validator("external_members_present", "institution_staff_present")
    @classmethod
    def _names(cls, v: list[str], info) -> list[str]:
        return clean_names(v, info.field_name)

    @model_validator(mode="after")
    def _no_employee_counted_as_external(self):
        staff = {n.casefold() for n in self.institution_staff_present}
        both = [n for n in self.external_members_present if n.casefold() in staff]
        if both:
            raise ValueError(f"listed as both external member and institution staff: {', '.join(both)}")
        return self

    def findings(self) -> list[ComplianceFinding]:
        subject = f"{self.program_name} PAC {self.meeting_date.isoformat()}"
        out = []

        def add(status, rule, message):
            out.append(ComplianceFinding(status=status, rule=rule, subject=subject, message=message))

        n = len(self.external_members_present)
        if n < PAC_MIN_EXTERNAL_MEMBERS:
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "PAC_MIN_EXTERNAL_MEMBERS",
                f"{n} external industry member(s) present; at least {PAC_MIN_EXTERNAL_MEMBERS} required.")
        if not self.minutes_documented:
            add(ComplianceStatus.PARTIAL_DEFICIENCY, "PAC_MINUTES",
                "No documented minutes; the meeting cannot be evidenced to the Commission.")
        for flag, topic in ((self.reviewed_curriculum, "curriculum"),
                            (self.reviewed_equipment_and_facilities, "equipment and facilities"),
                            (self.reviewed_ge_data, "graduation and employment data")):
            if not flag:
                add(ComplianceStatus.PARTIAL_DEFICIENCY, "PAC_REVIEW_SCOPE",
                    f"Minutes do not show a review of {topic}.")
        return out

    def compliance_status(self) -> ComplianceStatus:
        return worst_status(f.status for f in self.findings())
