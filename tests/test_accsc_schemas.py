"""ACCSC core schemas (src/accredo): validation, compliance findings, serialization.

Pure in-memory: no database, network, or cloud. Run from the repo root:
    python -m pytest -v tests/test_accsc_schemas.py
"""
import os
import sys
from datetime import date

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from accredo.schemas import (  # noqa: E402
    AccreditationStandard, AuditOpinion, CohortEmploymentMetrics, ComplianceStatus, FacultyMember,
    ManagementOperationsRecord, PACMeetingRecord, worst_status,
)
from accredo.validators import ReviewPeriod, check_pac_frequency, run_gap_analysis  # noqa: E402

S = ComplianceStatus
PERIOD = ReviewPeriod(start=date(2025, 7, 1), end=date(2026, 6, 30))


def pac(**kw):
    base = dict(meeting_date=date(2025, 10, 1), program_name="HVAC Technician",
                external_members_present=["A. Rivera", "B. Chen"], reviewed_curriculum=True,
                reviewed_equipment_and_facilities=True, reviewed_ge_data=True, minutes_documented=True)
    return PACMeetingRecord(**{**base, **kw})


def faculty(**kw):
    base = dict(faculty_id="F-001", name="Dana Ortiz", courses_assigned=["HVAC 101"],
                relevant_industry_experience_years=5.0, degree_level="Associate",
                official_transcripts_on_file=True, continuing_ed_hours_last_12mo=12.0,
                annual_evaluation_date=date(2026, 3, 1))
    return FacultyMember(**{**base, **kw})


def cohort(**kw):
    base = dict(program_name="HVAC Technician", reporting_period_start=date(2025, 7, 1),
                reporting_period_end=date(2026, 6, 30), students_started=20, graduates=15,
                graduates_employed_in_field=12, graduates_unavailable=0)
    return CohortEmploymentMetrics(**{**base, **kw})


def rules(findings):
    return sorted(f.rule for f in findings)


# ---------- base ----------

def test_worst_status_ordering():
    assert worst_status([]) is S.COMPLIANT
    assert worst_status([S.COMPLIANT, S.UNVERIFIED]) is S.UNVERIFIED
    assert worst_status([S.UNVERIFIED, S.PARTIAL_DEFICIENCY]) is S.PARTIAL_DEFICIENCY
    assert worst_status([S.PARTIAL_DEFICIENCY, S.SUBSTANTIVE_DEFICIENCY, S.COMPLIANT]) is S.SUBSTANTIVE_DEFICIENCY


def test_status_enum_is_a_string_and_rejects_unknown_values():
    assert S("COMPLIANT") == "COMPLIANT"
    with pytest.raises(ValueError):
        S("MOSTLY_FINE")


def test_accreditation_standard_strict():
    std = AccreditationStandard(section_code=" II.B ", standard_title="PAC", citation_ref="Sec II.B.2",
                                evidence_document_refs=["pac-minutes-2025-10.pdf"])
    assert std.section_code == "II.B"
    with pytest.raises(ValidationError, match="extra"):
        AccreditationStandard(section_code="II.B", standard_title="PAC", citation_ref="x", sectoin="typo")
    with pytest.raises(ValidationError, match="more than once"):
        AccreditationStandard(section_code="II.B", standard_title="PAC", citation_ref="x",
                              evidence_document_refs=["a.pdf", "A.PDF"])


# ---------- Section II: PAC ----------

def test_pac_meeting_fully_compliant():
    assert pac().findings() == [] and pac().compliance_status() is S.COMPLIANT


@pytest.mark.parametrize("members", [[], ["A. Rivera"]])
def test_pac_fewer_than_two_external_members_is_substantive(members):
    m = pac(external_members_present=members)
    assert m.compliance_status() is S.SUBSTANTIVE_DEFICIENCY
    assert rules(m.findings()) == ["PAC_MIN_EXTERNAL_MEMBERS"]


def test_pac_duplicate_attendee_cannot_inflate_count():
    with pytest.raises(ValidationError, match="more than once"):
        pac(external_members_present=["A. Rivera", " a. rivera "])


def test_pac_employee_cannot_be_counted_as_external():
    with pytest.raises(ValidationError, match="both external member and institution staff"):
        pac(external_members_present=["A. Rivera", "B. Chen"], institution_staff_present=["b. chen"])


def test_pac_blank_attendee_rejected():
    with pytest.raises(ValidationError, match="blank entry"):
        pac(external_members_present=["A. Rivera", "  "])


def test_pac_missing_minutes_and_review_topics_are_partial():
    m = pac(minutes_documented=False, reviewed_ge_data=False)
    assert m.compliance_status() is S.PARTIAL_DEFICIENCY
    assert rules(m.findings()) == ["PAC_MINUTES", "PAC_REVIEW_SCOPE"]


def test_pac_fewer_than_two_meetings_per_year_is_substantive():
    meetings = [pac(meeting_date=date(2025, 10, 1)),
                pac(meeting_date=date(2025, 6, 30)),                       # day before the period: not counted
                pac(program_name="Medical Assistant", meeting_date=date(2025, 9, 1)),
                pac(program_name="medical assistant", meeting_date=date(2026, 6, 30))]  # last day, case-insensitive
    found = check_pac_frequency(meetings, PERIOD, programs=["HVAC Technician", "Medical Assistant", "Welding"])
    assert {f.subject.split(" PAC")[0]: f.status for f in found} == {
        "HVAC Technician": S.SUBSTANTIVE_DEFICIENCY, "Welding": S.SUBSTANTIVE_DEFICIENCY}
    assert "0 PAC meeting(s)" in next(f.message for f in found if f.subject.startswith("Welding"))


def test_review_period_must_be_ordered_and_at_most_a_year():
    with pytest.raises(ValidationError, match="after start"):
        ReviewPeriod(start=date(2026, 1, 1), end=date(2025, 1, 1))
    with pytest.raises(ValidationError, match="one year"):
        ReviewPeriod(start=date(2025, 1, 1), end=date(2026, 6, 30))


# ---------- Section III: faculty ----------

def test_faculty_compliant():
    assert faculty().compliance_status(as_of=PERIOD.end) is S.COMPLIANT


def test_faculty_missing_transcripts_is_substantive():
    f = faculty(official_transcripts_on_file=False)
    assert f.compliance_status() is S.SUBSTANTIVE_DEFICIENCY and rules(f.findings()) == ["FAC_TRANSCRIPTS"]


@pytest.mark.parametrize("years,flagged", [(1.99, True), (2.0, False), (0, True)])
def test_faculty_industry_experience_threshold(years, flagged):
    f = faculty(relevant_industry_experience_years=years)
    assert ("FAC_INDUSTRY_EXPERIENCE" in rules(f.findings())) is flagged
    assert f.compliance_status() is (S.SUBSTANTIVE_DEFICIENCY if flagged else S.COMPLIANT)


def test_faculty_continuing_ed_below_one_hour_is_partial():
    f = faculty(continuing_ed_hours_last_12mo=0.5)
    assert f.compliance_status() is S.PARTIAL_DEFICIENCY and rules(f.findings()) == ["FAC_CONTINUING_ED"]


def test_faculty_evaluation_missing_stale_or_future():
    assert rules(faculty(annual_evaluation_date=None).findings()) == ["FAC_ANNUAL_EVALUATION"]
    stale = faculty(annual_evaluation_date=date(2025, 1, 1))
    assert stale.findings() == []                                            # no as_of: recency not checked
    assert stale.compliance_status(as_of=date(2026, 6, 30)) is S.PARTIAL_DEFICIENCY
    assert faculty(annual_evaluation_date=date(2026, 9, 1)).compliance_status(as_of=date(2026, 6, 30)) is S.UNVERIFIED


@pytest.mark.parametrize("field,value", [("relevant_industry_experience_years", -1),
                                         ("continuing_ed_hours_last_12mo", -0.5),
                                         ("faculty_id", "  "),
                                         ("courses_assigned", ["HVAC 101", "hvac 101"])])
def test_faculty_malformed_values_rejected(field, value):
    with pytest.raises(ValidationError):
        faculty(**{field: value})


def test_faculty_validate_assignment():
    f = faculty()
    with pytest.raises(ValidationError):
        f.relevant_industry_experience_years = -3


# ---------- Section VII: G&E ----------

def test_ge_rates_computed():
    c = cohort(students_started=20, graduates=15, graduates_employed_in_field=10, graduates_unavailable=2)
    assert c.graduation_rate == pytest.approx(0.75)
    assert c.employment_rate == pytest.approx(10 / 13)


@pytest.mark.parametrize("employed,available,status", [
    (7, 10, S.COMPLIANT),                    # exactly 70% passes
    (69, 100, S.SUBSTANTIVE_DEFICIENCY),     # 69% fails
    (699, 1000, S.SUBSTANTIVE_DEFICIENCY),   # 69.9% fails (no rounding up)
    (10, 10, S.COMPLIANT),
])
def test_ge_employment_benchmark(employed, available, status):
    c = cohort(students_started=available, graduates=available, graduates_employed_in_field=employed)
    assert c.compliance_status() is status


def test_ge_unavailable_graduates_leave_the_denominator():
    # 7 employed of 12 graduates is 58% -- but 2 are in the military, so 7/10 = 70% passes.
    c = cohort(graduates=12, graduates_employed_in_field=7, graduates_unavailable=2)
    assert c.employment_rate == pytest.approx(0.7) and c.compliance_status() is S.COMPLIANT
    assert cohort(graduates=12, graduates_employed_in_field=7).compliance_status() is S.SUBSTANTIVE_DEFICIENCY


def test_ge_undefined_rates_are_unverified_not_errors():
    c = cohort(students_started=0, graduates=0, graduates_employed_in_field=0)
    assert c.graduation_rate is None and c.employment_rate is None
    assert c.compliance_status() is S.UNVERIFIED
    all_unavailable = cohort(graduates=3, graduates_employed_in_field=0, graduates_unavailable=3)
    assert all_unavailable.employment_rate is None and all_unavailable.compliance_status() is S.UNVERIFIED


@pytest.mark.parametrize("kw,msg", [
    (dict(graduates=21), "exceeds students_started"),
    (dict(graduates_unavailable=16), "graduates_unavailable"),
    (dict(graduates_employed_in_field=14, graduates_unavailable=2), "exceeds graduates available"),
    (dict(reporting_period_end=date(2025, 7, 1)), "must be after"),
    (dict(students_started=-1), "greater than or equal to 0"),
])
def test_ge_impossible_counts_rejected(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        cohort(**kw)


def test_ge_json_round_trip_includes_computed_rates():
    c = cohort()
    data = c.model_dump(mode="json")
    assert data["employment_rate"] == pytest.approx(0.8) and data["reporting_period_start"] == "2025-07-01"
    # Computed fields are output-only: strip them before re-validating (extra="forbid").
    data.pop("employment_rate"); data.pop("graduation_rate")
    assert CohortEmploymentMetrics.model_validate(data) == c


# ---------- Section I: management ----------

def test_management_findings():
    ok = ManagementOperationsRecord(institution_name="Example Tech", fiscal_year_end=date(2025, 12, 31),
                                    audited_financial_statements_on_file=True, audit_opinion=AuditOpinion.UNQUALIFIED,
                                    composite_score=2.1, continuity_plan_on_file=True)
    assert ok.compliance_status() is S.COMPLIANT
    zone = ok.model_copy(update={"composite_score": 1.2})
    assert zone.compliance_status() is S.PARTIAL_DEFICIENCY
    bad = ok.model_copy(update={"audit_opinion": AuditOpinion.ADVERSE})
    assert bad.compliance_status() is S.SUBSTANTIVE_DEFICIENCY
    with pytest.raises(ValidationError):
        ManagementOperationsRecord(institution_name="X", fiscal_year_end=date(2025, 12, 31),
                                   audited_financial_statements_on_file=True, composite_score=3.5,
                                   continuity_plan_on_file=True)


# ---------- combined report ----------

def test_gap_report_combines_all_sections_and_serializes():
    report = run_gap_analysis(
        period=PERIOD,
        pac_meetings=[pac(meeting_date=date(2025, 10, 1), external_members_present=["A. Rivera"]),
                      pac(meeting_date=date(2026, 3, 1)),
                      pac(meeting_date=date(2024, 3, 1), minutes_documented=False)],   # outside period: ignored
        programs=["HVAC Technician"],
        faculty=[faculty(), faculty(faculty_id="F-002", official_transcripts_on_file=False)],
        cohorts=[cohort(graduates_employed_in_field=10)],                               # 10/15 = 66.7%
    )
    assert rules(report.findings) == ["FAC_TRANSCRIPTS", "GE_EMPLOYMENT_BENCHMARK", "PAC_MIN_EXTERNAL_MEMBERS"]
    assert report.overall_status is S.SUBSTANTIVE_DEFICIENCY
    assert report.counts_by_status == {"PARTIAL_DEFICIENCY": 0, "SUBSTANTIVE_DEFICIENCY": 3, "UNVERIFIED": 0}
    dumped = report.model_dump(mode="json")
    assert dumped["overall_status"] == "SUBSTANTIVE_DEFICIENCY"
    assert all(isinstance(f["status"], str) for f in dumped["findings"])


def test_empty_gap_report_is_compliant():
    report = run_gap_analysis(period=PERIOD)
    assert report.findings == [] and report.overall_status is S.COMPLIANT


def test_no_network_or_db_modules_imported():
    """Fresh interpreter, so modules the rest of the test run loaded can't hide or fake a result."""
    import subprocess
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    # Diff against a baseline: site .pth files may preload namespaces (google-cloud does). Stdlib socket
    # is not checked -- pydantic pulls in email.utils, which imports it without opening anything.
    code = ("import sys; before = set(sys.modules); sys.path.insert(0, sys.argv[1]); "
            "import accredo.schemas, accredo.validators; "
            "bad = ('flask', 'sqlalchemy', 'google', 'requests', 'httpx', 'urllib3', 'aiohttp', 'grpc', "
            "'psycopg2', 'pg8000'); "
            "print(','.join(sorted(m for m in set(sys.modules) - before if m.split('.')[0] in bad)))")
    out = subprocess.run([sys.executable, "-I", "-c", code, src], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "", out.stdout
