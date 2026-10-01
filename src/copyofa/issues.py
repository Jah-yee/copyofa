"""Issues, acceptances and the run outcome."""

from dataclasses import asdict, dataclass, field

PASS = "PASS"
ACCEPTED = "PASS WITH ACCEPTED ISSUES"
FAIL = "FAIL"


@dataclass
class Issue:
    code: str
    severity: str  # blocking | warning | info
    title: str
    what: str
    why: str
    actions: list[str] = field(default_factory=list)
    acceptable: bool = True
    values: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Issue":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


def render(issue: Issue) -> str:
    """Render an issue: what happened, why it matters, how to resolve it."""
    label = {"blocking": "BLOCKING", "warning": "WARNING", "info": "INFO"}[issue.severity]
    lines = [f"{label} {issue.code}: {issue.what}", f"  why: {issue.why}"]
    for action in issue.actions:
        lines.append(f"  resolve: {action}")
    if issue.severity == "blocking" and not issue.acceptable:
        lines.append("  (cannot be accepted)")
    return "\n".join(lines)


def accepted_codes(acceptances: list[dict]) -> set[str]:
    return {a["code"] for a in acceptances}


def open_blocking(issues: list[Issue], acceptances: list[dict]) -> list[Issue]:
    codes = accepted_codes(acceptances)
    return [i for i in issues if i.severity == "blocking" and (i.code not in codes or not i.acceptable)]


def outcome(issues: list[Issue], acceptances: list[dict]) -> str:
    if open_blocking(issues, acceptances):
        return FAIL
    return ACCEPTED if acceptances else PASS
