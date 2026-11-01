"""OpenAI-compatible calls and an explicitly scripted, offline showcase."""

import os

import httpx


class Model:
    def __init__(self, live=False):
        self.live = live
        self.model = os.getenv("EVOGATE_MODEL", "deepseek-flash")
        self.base = os.getenv("EVOGATE_API_BASE", "https://api.deepseek.com").rstrip("/")

    def info(self):
        return {
            "mode": "live" if self.live else "demo",
            "model": self.model if self.live else "scripted-demo",
            "base_url": self.base if self.live else None,
        }

    def complete_skill(self, messages):
        return self.complete(messages, max_tokens=8192)

    def complete(self, messages, tools=None, max_tokens=2048):
        if not self.live:
            raise ValueError("离线模式仅支持内置销售示例；使用 --live 启用真实模型。")
        key = os.getenv("EVOGATE_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError("请设置 EVOGATE_API_KEY 或 DEEPSEEK_API_KEY 后以 --live 启动。")
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if "deepseek.com" in self.base:
            body["thinking"] = {"type": "disabled"}
        if tools:
            body["tools"] = tools
        try:
            response = httpx.post(
                self.base + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
                timeout=120,
            )
            response.raise_for_status()
            payload = response.json()
            message = payload["choices"][0]["message"]
            if payload["choices"][0].get("finish_reason") == "length":
                raise ValueError("模型输出被截断，请缩短文档或输入后重试。")
            if not isinstance(message.get("content"), (str, type(None))):
                raise ValueError("模型返回了无效内容。")
            return message, payload.get("usage")
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"模型服务 HTTP {exc.response.status_code}；请检查配置。") from None
        except httpx.TransportError:
            raise ValueError("模型服务连接失败或超时，请检查网络后重试。") from None
        except (KeyError, IndexError, TypeError):
            raise ValueError("模型服务返回格式无效。") from None
