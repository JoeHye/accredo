"""Section VII: Student Services -- Graduation & Employment (G&E) chart, per program cohort."""
from datetime import date
from fractions import Fraction
from typing import Optional

from pydantic import BaseModel, Field, computed_field, model_validator

from .base import STRICT_CONFIG, ComplianceFinding, ComplianceStatus, worst_status

# Kept as an exact fraction so 7 of 10 compares as exactly 70%, never 69.999...%.
EMPLOYMENT_BENCHMARK = Fraction(70, 100)


class CohortEmploymentMetrics(BaseModel):
    model_config = STRICT_CONFIG

    program_name: str = Field(min_length=1)
    reporting_period_start: date
    reporting_period_end: date
    students_started: int = Field(ge=0)
    graduates: int = Field(ge=0)
    graduates_employed_in_field: int = Field(ge=0)
    graduates_unavailable: int = Field(default=0, ge=0, description="Illness, military, continuing education")

    @model_validator(mode="after")
    def _consistent_counts(self):
        problems = []
        if self.reporting_period_end <= self.reporting_period_start:
            problems.append("reporting_period_end must be after reporting_period_start")
        if self.graduates > self.students_started:
            problems.append(f"graduates ({self.graduates}) exceeds students_started ({self.students_started})")
        if self.graduates_unavailable > self.graduates:
            problems.append(f"graduates_unavailable ({self.graduates_unavailable}) exceeds graduates ({self.graduates})")
        elif self.graduates_employed_in_field > self.graduates - self.graduates_unavailable:
            problems.append(f"graduates_employed_in_field ({self.graduates_employed_in_field}) exceeds graduates "
                            f"available for employment ({self.graduates - self.graduates_unavailable})")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def available_for_employment(self) -> int:
        return self.graduates - self.graduates_unavailable

    @computed_field
    @property
    def graduation_rate(self) -> Optional[float]:
        """graduates / students_started; None when there were no starts."""
        if self.students_started == 0:
            return None
        return self.graduates / self.students_started

    @computed_field
    @property
    def employment_rate(self) -> Optional[float]:
        """graduates_employed_in_field / (graduates - graduates_unavailable); None when nobody was available."""
        if self.available_for_employment == 0:
            return None
        return self.graduates_employed_in_field / self.available_for_employment

    def meets_employment_benchmark(self) -> Optional[bool]:
        """Exact integer comparison against EMPLOYMENT_BENCHMARK; None when the rate is undefined."""
        if self.available_for_employment == 0:
            return None
        return (self.graduates_employed_in_field * EMPLOYMENT_BENCHMARK.denominator
                >= EMPLOYMENT_BENCHMARK.numerator * self.available_for_employment)

    def findings(self) -> list[ComplianceFinding]:
        subject = (f"{self.program_name} {self.reporting_period_start.isoformat()}"
                   f"..{self.reporting_period_end.isoformat()}")
        meets = self.meets_employment_benchmark()
        if meets is None:
            return [ComplianceFinding(status=ComplianceStatus.UNVERIFIED, rule="GE_EMPLOYMENT_BENCHMARK", subject=subject,
                                      message="No graduates available for employment; employment rate is undefined.")]
        if not meets:
            return [ComplianceFinding(
                status=ComplianceStatus.SUBSTANTIVE_DEFICIENCY, rule="GE_EMPLOYMENT_BENCHMARK", subject=subject,
                message=(f"Employment rate {self.employment_rate:.1%} ({self.graduates_employed_in_field}/"
                         f"{self.available_for_employment}) is below the {float(EMPLOYMENT_BENCHMARK):.0%} benchmark."))]
        return []

    def compliance_status(self) -> ComplianceStatus:
        return worst_status(f.status for f in self.findings())
