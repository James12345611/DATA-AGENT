"""LLM access layer.

Two implementations share one contract:

* :class:`ChatLLMClient` - the real provider (DeepSeek / any OpenAI-compatible
  chat completions endpoint) through ``langchain-openai``.
* :class:`~text2sql.offline_llm.DeterministicLLMClient` - a rules based client
  used when no API key is configured, so the graph, validator and retry loop can
  be exercised without network access.

Chat history is passed as real LangChain message objects; it is never
stringified before being handed to a message-typed LLM (section 4.4).
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Sequence

from .config import Settings, get_settings
from .prompts import Prompt

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


class LLMError(RuntimeError):
    """Raised when the model cannot produce a usable answer."""


def strip_code_fences(text: str) -> str:
    """Remove Markdown fences and a trailing semicolon from a model answer."""
    value = (text or "").strip()
    if "```" in value:
        blocks = re.findall(r"```[a-zA-Z0-9]*\s*(.*?)```", value, flags=re.S)
        value = (blocks[0] if blocks else _FENCE_RE.sub("", value)).strip()
    value = value.strip().strip("`").strip()
    while value.endswith(";"):
        value = value[:-1].strip()
    return value


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object in a model answer."""
    value = strip_code_fences(text or "")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.S)
        if not match:
            raise LLMError("模型输出中找不到 JSON 对象") from None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出的 JSON 无法解析: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("模型输出的 JSON 必须是对象")
    return parsed


class BaseLLMClient(ABC):
    """The only interface the nodes use."""

    name: str = "base"

    @abstractmethod
    def complete_text(self, prompt: Prompt, *, history: Sequence[Any] | None = None) -> str:
        """Free-form completion."""

    def complete_json(
        self,
        prompt: Prompt,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
        history: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        """Completion parsed (and optionally validated) as a JSON object."""
        raw = self.complete_text(prompt, history=history)
        payload = extract_json_object(raw)
        if validator is not None:
            validator(payload)
        return payload

    def complete_sql(self, prompt: Prompt, *, history: Sequence[Any] | None = None) -> str:
        """Completion normalized into a single SQL statement (unfenced)."""
        raw = self.complete_text(prompt, history=history)
        sql = strip_code_fences(raw)
        if not sql:
            raise LLMError("模型没有返回 SQL")
        return sql

    def close(self) -> None:  # pragma: no cover - nothing to release by default
        return None


class ChatLLMClient(BaseLLMClient):
    """OpenAI-compatible chat client (DeepSeek by default)."""

    name = "chat"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.llm_api_key:
            raise LLMError("LLM_API_KEY 未配置，无法创建真实模型客户端")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise LLMError("需要安装 langchain-openai 才能调用真实模型") from exc

        self._llm = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )
        try:
            self._json_llm = self._llm.bind(response_format={"type": "json_object"})
        except Exception:  # pragma: no cover - provider without json mode
            self._json_llm = self._llm

    # ------------------------------------------------------------- internals
    def _messages(self, prompt: Prompt, history: Sequence[Any] | None) -> list[Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages: list[Any] = [SystemMessage(content=prompt.system)]
        for message in list(history if history is not None else prompt.history):
            # Keep real message objects: never stringify BaseMessage instances.
            if isinstance(message, (SystemMessage,)) or hasattr(message, "content"):
                messages.append(message)
        messages.append(HumanMessage(content=prompt.user))
        return messages

    def complete_text(self, prompt: Prompt, *, history: Sequence[Any] | None = None) -> str:
        client = self._json_llm if prompt.json_mode else self._llm
        try:
            response = client.invoke(self._messages(prompt, history))
        except Exception as exc:  # noqa: BLE001 - provider/network errors
            raise LLMError(self.settings.redact(f"模型调用失败: {exc}")) from exc
        content = getattr(response, "content", response)
        if isinstance(content, list):  # multi-part content
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return str(content)


def build_llm(
    settings: Settings | None = None,
    *,
    catalog: Any | None = None,
    force_offline: bool = False,
) -> BaseLLMClient:
    """Return the best available client for the current configuration."""
    settings = settings or get_settings()
    if force_offline or not settings.llm_api_key:
        from .offline_llm import DeterministicLLMClient

        if not force_offline:
            logger.warning("LLM_API_KEY 未配置，使用离线确定性客户端（不调用网络模型）")
        return DeterministicLLMClient(settings=settings, catalog=catalog)
    return ChatLLMClient(settings)


__all__ = [
    "BaseLLMClient",
    "ChatLLMClient",
    "LLMError",
    "build_llm",
    "extract_json_object",
    "strip_code_fences",
]
