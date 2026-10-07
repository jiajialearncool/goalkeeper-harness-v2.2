from pydantic import Field

from ..models import Check, State, Task


class DesktopTask(Task):
    acceptance: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)


class DesktopState(State):
    task: DesktopTask
    question: str | None = None
    queued_messages: list[dict] = Field(default_factory=list)
    verification: list[dict] = Field(default_factory=list)
    source_results: list[dict] = Field(default_factory=list)
    waiting_since: float | None = None
    remaining_seconds: float | None = None
