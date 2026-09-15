"""Structured review output. This is what `synthesize` must produce and what the API returns."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["low", "medium", "high", "critical"]
Risk = Literal["low", "medium", "high"]


class LineRange(BaseModel):
    """Inclusive line range in the HEAD version of the file."""

    start: int = Field(ge=1, description="First line (1-based, head version of the file).")
    end: int = Field(ge=1, description="Last line, inclusive; >= start.")

    @model_validator(mode="after")
    def _ordered(self) -> "LineRange":
        if self.end < self.start:
            raise ValueError("lines.end must be >= lines.start")
        return self


class Finding(BaseModel):
    """One concrete, line-anchored problem."""

    file: str = Field(description="Path of a file changed in this pull request.")
    lines: LineRange
    severity: Severity
    title: str = Field(description="One-line statement of the problem.")
    detail: str = Field(description="Why it is a problem, referring to the specific code.")
    suggestion: str | None = Field(default=None, description="Concrete fix, or null.")


class ReviewResult(BaseModel):
    """The final pull-request review. An empty findings list is a valid review."""

    summary: str = Field(description="Two or three sentences on what the PR does and its state.")
    risk: Risk
    findings: list[Finding] = Field(default_factory=list)
    files_reviewed: list[str] = Field(default_factory=list)
