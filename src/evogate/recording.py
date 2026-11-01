"""Request logging, cumulative call limits and opt-in environment loading."""

import json
import os
import shlex
import threading
import time
import uuid

from .model import Model


def load_environment(path):
    """Explicit opt-in .env loading without executing shell code or logging secrets."""
    allowed = {"DEEPSEEK_API_KEY", "EVOGATE_API_KEY", "EVOGATE_MODEL", "EVOGATE_API_BASE",
               "EVOGATE_OPTIMIZER"}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or name.strip() not in allowed:
            continue
        values = shlex.split(value, comments=True)
        if len(values) == 1 and not os.environ.get(name.strip()):
            os.environ[name.strip()] = values[0]


class RecordedModel(Model):
    def __init__(self, directory, max_calls, quiet=False):
        super().__init__(live=True)
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.log = directory / "requests.jsonl"
        self.max_calls = max_calls
        self.quiet = quiet
        self.lock = threading.RLock()
        self.calls = 0
        if self.log.exists():
            with self.log.open() as source:
                self.calls = sum(json.loads(line)["kind"] == "request" for line in source)

    def append(self, row):
        with self.lock, self.log.open("a") as output:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")

    def complete(self, messages, tools=None, max_tokens=2048):
        request_id = uuid.uuid4().hex
        with self.lock:
            if self.calls >= self.max_calls:
                raise ValueError("本次运行已达到调用上限，停止新增模型请求。")
            self.calls += 1
            self.append({"kind": "request", "id": request_id, "time": time.time(),
                         "model": self.info(), "messages": messages, "tools": tools,
                         "max_tokens": max_tokens})
        start = time.monotonic()
        try:
            message, usage = super().complete(messages, tools, max_tokens)
        except Exception as exc:
            self.append({"kind": "error", "id": request_id, "error": str(exc)})
            raise
        self.append({"kind": "response", "id": request_id, "message": message,
                     "usage": usage, "elapsed_ms": round((time.monotonic() - start) * 1000)})
        if not self.quiet:
            print(json.dumps({"event": "model_response", "usage": usage,
                              "tool_calls": len(message.get("tool_calls") or [])}), flush=True)
        return message, usage
