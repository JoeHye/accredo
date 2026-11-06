"""Section I: Management & Administrative Operations (financial stability / continuity).

The directive gave no field list for Section I, so this is a deliberately small placeholder model.
Thresholds here are assumptions to confirm before relying on them.
"""
from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .base import STRICT_CONFIG, ComplianceFinding, ComplianceStatus, clean_names, worst_status

# U.S. Department of Education financial responsibility composite score: 1.5-3.0 passes,
# 1.0-1.4 is the "zone", below 1.0 fails. (Title IV rule, used here as a proxy for ACCSC's
# financial-stability review.)
COMPOSITE_PASS = 1.5
COMPOSITE_ZONE_FLOOR = 1.0


class AuditOpinion(str, Enum):
    UNQUALIFIED = "UNQUALIFIED"
    QUALIFIED = "QUALIFIED"
    ADVERSE = "ADVERSE"
    DISCLAIMER = "DISCLAIMER"


class ManagementOperationsRecord(BaseModel):
    model_config = STRICT_CONFIG

    institution_name: str = Field(min_length=1)
    fiscal_year_end: date
    audited_financial_statements_on_file: bool
    audit_opinion: Optional[AuditOpinion] = None
    going_concern_note: bool = False
    composite_score: Optional[float] = Field(default=None, ge=-1.0, le=3.0)
    continuity_plan_on_file: bool
    key_personnel: list[str] = Field(default_factory=list)

    @field_validator("key_personnel")
    @classmethod
    def _personnel(cls, v: list[str]) -> list[str]:
        return clean_names(v, "key_personnel")

    def findings(self) -> list[ComplianceFinding]:
        subject = f"{self.institution_name} FYE {self.fiscal_year_end.isoformat()}"
        out = []

        def add(status, rule, message):
            out.append(ComplianceFinding(status=status, rule=rule, subject=subject, message=message))

        if not self.audited_financial_statements_on_file:
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "MGMT_AUDITED_FINANCIALS",
                "No audited financial statements on file for this fiscal year.")
        elif self.audit_opinion is None:
            add(ComplianceStatus.UNVERIFIED, "MGMT_AUDIT_OPINION", "Audit opinion not recorded.")
        elif self.audit_opinion in (AuditOpinion.ADVERSE, AuditOpinion.DISCLAIMER):
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "MGMT_AUDIT_OPINION",
                f"Auditor issued a {self.audit_opinion.value.lower()} opinion.")
        elif self.audit_opinion is AuditOpinion.QUALIFIED:
            add(ComplianceStatus.PARTIAL_DEFICIENCY, "MGMT_AUDIT_OPINION", "Auditor issued a qualified opinion.")
        if self.going_concern_note:
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "MGMT_GOING_CONCERN",
                "Financial statements carry a going-concern note.")
        if self.composite_score is None:
            add(ComplianceStatus.UNVERIFIED, "MGMT_COMPOSITE_SCORE", "Composite score not recorded.")
        elif self.composite_score < COMPOSITE_ZONE_FLOOR:
            add(ComplianceStatus.SUBSTANTIVE_DEFICIENCY, "MGMT_COMPOSITE_SCORE",
                f"Composite score {self.composite_score} is below {COMPOSITE_ZONE_FLOOR} (failing).")
        elif self.composite_score < COMPOSITE_PASS:
            add(ComplianceStatus.PARTIAL_DEFICIENCY, "MGMT_COMPOSITE_SCORE",
                f"Composite score {self.composite_score} is in the zone ({COMPOSITE_ZONE_FLOOR}-{COMPOSITE_PASS}).")
        if not self.continuity_plan_on_file:
            add(ComplianceStatus.PARTIAL_DEFICIENCY, "MGMT_CONTINUITY_PLAN",
                "No continuity / succession plan for key personnel on file.")
        return out

    def compliance_status(self) -> ComplianceStatus:
        return worst_status(f.status for f in self.findings())
