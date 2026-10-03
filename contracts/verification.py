"""Independent verification is success, observed failure, or incomplete."""
from dataclasses import asdict, dataclass
from typing import Literal

VerificationStatus = Literal["VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE"]


@dataclass(frozen=True)
class VerificationCheck:
    id: str
    status: Literal["PASS", "FAIL", "INCOMPLETE"]
    detail: str


@dataclass(frozen=True)
class VerificationResult:
    verification_status: VerificationStatus
    checks: tuple[VerificationCheck, ...]

    def to_dict(self):
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d
