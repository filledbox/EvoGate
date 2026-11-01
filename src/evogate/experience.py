"""Persistent evidence identity, replay selection and automatic update policy."""

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from .data import fingerprint


class EvolutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    batch_size: int = Field(default=50, ge=1, le=500)
    recurring_failures: int = Field(default=3, ge=2, le=100)
    cooldown_seconds: int = Field(default=3600, ge=0)
    max_attempts_per_day: int = Field(default=4, ge=1, le=100)
    max_calls_per_attempt: int = Field(default=300, ge=1, le=10000)
    replay_size: int = Field(default=40, ge=4, le=200)
    min_net_improvements: int = Field(default=1, ge=1)


def question_key(question, evidence=""):
    return fingerprint(" ".join(question.lower().split()) + "\n" + evidence.strip())


def failure_kind(observation, score=None):
    execution = observation.get("execution", {})
    if execution.get("status") != "success":
        detail = execution.get("detail", "").lower()
        for marker in ("no such column", "no such table", "syntax", "timeout", "tool_call_limit"):
            if marker in detail:
                return marker
        return "execution_failure"
    return "result_mismatch" if score == 0 else None


def select_replay(experiences, limit):
    """Mix recent evidence, unresolved older failures and successful counterexamples."""
    latest = sorted(experiences, key=lambda e: e["updated"], reverse=True)
    failures = [e for e in reversed(latest) if e.get("score") == 0 and not e.get("resolved")]
    successes = [e for e in latest if e.get("score") == 1 or e.get("resolved")]
    selected = {}
    for row in latest[:limit // 2] + failures[:limit // 4] + successes[:limit // 4] + latest:
        selected.setdefault(row["key"], row)
        if len(selected) >= limit:
            break
    return list(selected.values())


def trigger_reason(experiences, processed, policy):
    # Repeated queries and unchanged confirmations contribute one evidence event.
    fresh = {e["key"]: e for e in experiences
             if e.get("signal") != "unverified" and e["event_id"] not in processed}
    if len(fresh) >= policy.batch_size:
        return "new_feedback_batch"
    counts = Counter(e["failure_kind"] for e in fresh.values() if e.get("failure_kind"))
    if counts and max(counts.values()) >= policy.recurring_failures:
        return "recurring_failure"
    return None


class BudgetModel:
    """A bounded model view shared by proposal and replay calls in one attempt."""

    def __init__(self, model, limit):
        self.model = model
        self.limit = limit
        self.calls = 0
        self.live = model.live

    def info(self):
        return self.model.info()

    def complete(self, *args, **kwargs):
        if self.calls >= self.limit:
            raise ValueError("本次进化已达到模型调用预算。")
        self.calls += 1
        return self.model.complete(*args, **kwargs)

    def complete_skill(self, messages):
        return self.complete(messages, max_tokens=8192)
