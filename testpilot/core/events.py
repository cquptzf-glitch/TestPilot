"""领域事件 — RPC 推送（run.progress/case.finished/step.finished）的序列化源。

LLM token 用量随 run 统计落库（``stats.llm_usage``），不再单发事件。
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DomainEvent:
    event: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"event": self.event, "payload": self.payload}


@dataclass
class StepFinished(DomainEvent):
    event: str = "step.finished"

    @classmethod
    def of(cls, run_id, case_exec_id, step_no, step_id, status, driver):
        return cls(payload={
            "run_id": run_id, "case_exec_id": case_exec_id,
            "step_no": step_no, "step_id": step_id,
            "status": status, "driver": driver})


@dataclass
class CaseFinished(DomainEvent):
    event: str = "case.finished"

    @classmethod
    def of(cls, run_id, case_exec_id, case_id, status, duration_ms, sig):
        return cls(payload={
            "run_id": run_id, "case_exec_id": case_exec_id, "case_id": case_id,
            "status": status, "duration_ms": duration_ms, "sig": sig})


@dataclass
class RunFinished(DomainEvent):
    event: str = "run.progress"

    @classmethod
    def of(cls, run_id, stats):
        return cls(payload={"run_id": run_id, "stats": stats})
