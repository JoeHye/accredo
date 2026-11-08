"""Shared types for ACCSC schemas.

Two layers of checking, on purpose:
  * ValidationError (raised by pydantic) for data that is malformed or impossible -- blank names,
    negative counts, more graduates than starts. That data is rejected at ingestion.
  * ComplianceFinding (returned by each model's findings()) for data that is well-formed but falls
    short of an ACCSC threshold. That data is kept, because reporting the gap is the whole point.
"""
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Unknown fields are an error (a typo in an ingested key must not silently drop evidence), strings
# are stripped, and assignments after construction are re-validated.
STRICT_CONFIG = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class ComplianceStatus(str, Enum):
    COMPLIANT = "COMPLIANT"
    PARTIAL_DEFICIENCY = "PARTIAL_DEFICIENCY"
    SUBSTANTIVE_DEFICIENCY = "SUBSTANTIVE_DEFICIENCY"
    UNVERIFIED = "UNVERIFIED"


# Worst wins when statuses are combined. UNVERIFIED ranks above COMPLIANT (missing evidence is not
# a pass) but below a deficiency that the evidence actually shows.
_SEVERITY = {
    ComplianceStatus.COMPLIANT: 0,
    ComplianceStatus.UNVERIFIED: 1,
    ComplianceStatus.PARTIAL_DEFICIENCY: 2,
    ComplianceStatus.SUBSTANTIVE_DEFICIENCY: 3,
}


def worst_status(statuses: Iterable[ComplianceStatus]) -> ComplianceStatus:
    """Most severe status in statuses; COMPLIANT when there are none."""
    return max(statuses, key=_SEVERITY.__getitem__, default=ComplianceStatus.COMPLIANT)


def clean_names(values: list[str], field_name: str) -> list[str]:
    """Strip each entry; reject blanks and case-insensitive duplicates (a name counted twice inflates attendance)."""
    cleaned, seen = [], set()
    for raw in values:
        name = raw.strip()
        if not name:
            raise ValueError(f"{field_name} contains a blank entry")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"{field_name} lists {name!r} more than once")
        seen.add(key)
        cleaned.append(name)
    return cleaned


class AccreditationStandard(BaseModel):
    model_config = STRICT_CONFIG

    section_code: str = Field(min_length=1, description="ACCSC section, e.g. 'II.B'")
    standard_title: str = Field(min_length=1)
    citation_ref: str = Field(min_length=1, description="Citation in the Standards of Accreditation")
    evidence_document_refs: list[str] = Field(default_factory=list)

    @field_validator("evidence_document_refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        return clean_names(v, "evidence_document_refs")


class ComplianceFinding(BaseModel):
    """One gap (or confirmation) produced by a model's findings()."""
    model_config = STRICT_CONFIG

    status: ComplianceStatus
    rule: str = Field(min_length=1, description="Stable rule id, e.g. 'PAC_MIN_EXTERNAL_MEMBERS'")
    subject: str = Field(min_length=1, description="What the finding is about, e.g. a program or faculty id")
    message: str = Field(min_length=1)
