"""SQL execution limits, results and evaluation records."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[2] = 2


class Limits(Record):
    wall_seconds: float = Field(default=30, gt=0)
    cpu_seconds: int = Field(default=30, gt=0)
    memory_bytes: int = Field(default=512 * 1024**2, gt=0)
    max_rows: int = Field(default=100000, gt=0)
    max_bytes: int = Field(default=16 * 1024**2, gt=0)


class Execution(Record):
    status: Literal[
        "success", "rejected", "sql_error", "timeout", "resource_limit", "infrastructure_error"
    ]
    rows: list[tuple] = Field(default_factory=list)
    detail: str = ""
    restrictions: dict = Field(default_factory=dict)


class Evaluation(Record):
    score: int | None = None
    reason: str
