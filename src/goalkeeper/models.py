from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer

MODEL = "gpt-6.1-sol"
EFFORT = "xhigh"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RunConfig(StrictModel):
    model: str = MODEL
    effort: str = EFFORT
    seconds_budget: float = Field(default=1800, gt=0)
    token_budget: int | None = Field(default=1_000_000, gt=0)
    summary_enabled: bool = True
    compact_trigger: int = Field(default=48_000, gt=0)
    context_ceiling: int = Field(default=64_000, gt=0)
    keep_recent_tokens: int = Field(default=20_000, gt=0)
    summary_max_tokens: int = Field(default=2_000, gt=0)
    summary_input_tokens: int = Field(default=40_000, gt=0)
    summary_budget_fraction: float | None = Field(default=.1, gt=0, le=.5)
    summary_time_fraction: float | None = Field(default=.15, gt=0, le=.5)
    tool_result_tokens: int = Field(default=4_096, gt=0)
    output_forecast_tokens: int = Field(default=8_192, gt=0)
    delivery_save_seconds: float = Field(default=10, gt=0)
    recovery_limit: Literal[1] = 1

    def require_current_model(self) -> None:
        if (self.model, self.effort) != (MODEL, EFFORT):
            raise ValueError("本轮所有模型角色必须固定 gpt-6.1-sol / xhigh。")

    @model_validator(mode="after")
    def valid(self):
        self.require_current_model()
        if not self.summary_max_tokens < self.keep_recent_tokens < self.compact_trigger < self.context_ceiling:
            raise ValueError("摘要、近期正文、压缩触发值与输入上限不匹配。")
        if self.summary_input_tokens <= self.summary_max_tokens + 512:
            raise ValueError("摘要输入必须容纳摘要正文、格式说明与纠正消息。")
        return self


class Check(StrictModel):
    id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def valid_argv(self):
        if not self.argv[0] or any("\x00" in part for part in self.argv):
            raise ValueError("规定检查命令无效。")
        return self


class Feedback(StrictModel):
    probe: Check
    text: str
    checks: list[Check]
    acceptance: list[str]


class Task(StrictModel):
    id: str
    objective: str
    constraints: list[str]
    acceptance: list[str] = Field(min_length=1)
    write_paths: list[str] = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)
    protected_paths: list[str] = Field(default_factory=list)
    feedback: Feedback | None = None
    user_turns: list[str] = Field(default_factory=list)
    preferences: dict[str, str] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler):
        value = handler(self)
        if not self.user_turns:
            value.pop("user_turns", None)
        return value

    @model_validator(mode="after")
    def checks_unique(self):
        if self.feedback and self.user_turns:
            raise ValueError("业务反馈和顺序用户轮次不能混用。")
        if any(not text.strip() for text in self.user_turns):
            raise ValueError("后续用户轮次不能为空。")
        all_checks = [*self.checks, *([self.feedback.probe, *self.feedback.checks] if self.feedback else [])]
        if len({c.id for c in all_checks}) != len(all_checks):
            raise ValueError("检查ID必须唯一。")
        from pathlib import PurePosixPath
        for path in [*self.write_paths, *self.protected_paths]:
            parts = PurePosixPath(path).parts
            if path == "./":
                continue  # Explicit access to this synthetic workspace only, protected files still apply.
            if (not parts or "\x00" in path or PurePosixPath(path).is_absolute()
                    or any(p in {"..", ".git", ".tmp", ".home"} for p in parts)):
                raise ValueError("任务范围必须是相对路径。")
        return self

    def active_checks(self, feedback_delivered: bool) -> list[Check]:
        return [*self.checks, *(self.feedback.checks if feedback_delivered and self.feedback else [])]

    def active_definition(self, feedback_delivered: bool, turn_index: int = 0) -> dict:
        """Runtime view of a fixed task; never promote checks into its definition."""
        value = self.model_dump(exclude={"feedback", "user_turns"})
        for text in self.user_turns[:turn_index]:
            value["objective"] += "\n用户下一轮要求：" + text
        if feedback_delivered and self.feedback:
            value["objective"] += "\n用户追加要求：" + self.feedback.text
            value["acceptance"].extend(self.feedback.acceptance)
            value["checks"] = [c.model_dump() for c in self.active_checks(True)]
        return value


class ModelRequest(StrictModel):
    instructions: str
    input: list[dict]
    tools: list[dict] = Field(default_factory=list)
    config: RunConfig
    cache_key: str | None = None


class ModelEvent(StrictModel):
    type: str
    data: dict


class State(StrictModel):
    id: str
    task: Task
    config: RunConfig
    workspace: str
    started_at: float
    deadline: float
    active_seconds: float = 0
    ended_at: float | None = None
    phase: str = "prepared"
    first_stop_reason: str | None = None
    stops: list[dict] = Field(default_factory=list)
    goal_version: int = 1
    feedback_delivered: bool = False
    turn_index: int = 0
    history: list[dict] = Field(default_factory=list)
    context_base: dict | None = None
    summary: dict | None = None
    compaction: dict | None = None
    compaction_disabled: str | None = None
    calls: list[dict] = Field(default_factory=list)
    pending_tools: list[dict] = Field(default_factory=list)
    operations: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    delivery: dict | None = None
    recovery_used: int = 0
    protocol_reminders: int = 0
    sequence: int = 0
    events: list[dict[str, Any]] = Field(default_factory=list)
