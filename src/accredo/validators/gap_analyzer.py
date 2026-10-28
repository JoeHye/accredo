"""Cross-record checks and the combined gap report.

Per-record rules live on each schema's findings(). Rules that need several records together (PAC
meeting frequency per program) live here, and run_gap_analysis() merges everything into one report.
"""
from collections import Counter
from datetime import date
from typing import Iterable, Optional

from pydantic import BaseModel, computed_field, model_validator

from ..schemas.base import STRICT_CONFIG, ComplianceFinding, ComplianceStatus, worst_status
from ..schemas.section_i_management import ManagementOperationsRecord
from ..schemas.section_ii_program_pac import PAC_MIN_MEETINGS_PER_YEAR, PACMeetingRecord
from ..schemas.section_iii_faculty import FacultyMember
from ..schemas.section_vii_student_ge import CohortEmploymentMetrics


class ReviewPeriod(BaseModel):
    """The 12-month window the PAC frequency rule is measured over (inclusive)."""
    model_config = STRICT_CONFIG

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self):
        if self.end <= self.start:
            raise ValueError("review period end must be after start")
        if (self.end - self.start).days > 366:
            raise ValueError("review period must not exceed one year; run one analysis per year")
        return self


def check_pac_frequency(meetings: Iterable[PACMeetingRecord], period: ReviewPeriod,
                        programs: Optional[Iterable[str]] = None) -> list[ComplianceFinding]:
    """SUBSTANTIVE_DEFICIENCY for each program with fewer than PAC_MIN_MEETINGS_PER_YEAR meetings in period.

    Pass programs (the institution's full program list) so a program with zero meetings is caught;
    without it, only programs that appear in meetings can be checked. Program names match
    case-insensitively. Every meeting held counts, even one that is itself deficient -- that
    meeting gets its own finding from PACMeetingRecord.findings().
    """
    counts, display = Counter(), {}
    for m in meetings:
        key = m.program_name.casefold()
        display.setdefault(key, m.program_name)
        if period.start <= m.meeting_date <= period.end:
            counts[key] += 1
    for p in programs or ():
        display.setdefault(p.strip().casefold(), p.strip())

    out = []
    for key in sorted(display):
        n = counts[key]
        if n < PAC_MIN_MEETINGS_PER_YEAR:
            out.append(ComplianceFinding(
                status=ComplianceStatus.SUBSTANTIVE_DEFICIENCY, rule="PAC_MIN_MEETINGS_PER_YEAR",
                subject=f"{display[key]} PAC {period.start.isoformat()}..{period.end.isoformat()}",
                message=f"{n} PAC meeting(s) in the review period; at least {PAC_MIN_MEETINGS_PER_YEAR} required."))
    return out


class GapReport(BaseModel):
    model_config = STRICT_CONFIG

    findings: list[ComplianceFinding]

    @computed_field
    @property
    def overall_status(self) -> ComplianceStatus:
        return worst_status(f.status for f in self.findings)

    @computed_field
    @property
    def counts_by_status(self) -> dict[str, int]:
        c = Counter(f.status.value for f in self.findings)
        return {s.value: c.get(s.value, 0) for s in ComplianceStatus if s is not ComplianceStatus.COMPLIANT}


def run_gap_analysis(*, period: ReviewPeriod,
                     management: Iterable[ManagementOperationsRecord] = (),
                     pac_meetings: Iterable[PACMeetingRecord] = (),
                     programs: Optional[Iterable[str]] = None,
                     faculty: Iterable[FacultyMember] = (),
                     cohorts: Iterable[CohortEmploymentMetrics] = ()) -> GapReport:
    """All findings for one review period. Faculty evaluations are aged against period.end.

    Faculty experience minimums (3.0 years non-degree, 4.0 degree-level) come from
    FacultyMember.findings(), so the per-record and combined results can never disagree.
    """
    pac_meetings = list(pac_meetings)
    findings: list[ComplianceFinding] = []
    for rec in management:
        findings += rec.findings()
    for m in pac_meetings:
        if period.start <= m.meeting_date <= period.end:
            findings += m.findings()
    findings += check_pac_frequency(pac_meetings, period, programs)
    for f in faculty:
        findings += f.findings(as_of=period.end)
    for c in cohorts:
        findings += c.findings()
    return GapReport(findings=findings)
