"""Explicitly configured OpenAI-compatible model adapters; no simulated outputs."""

import math

import httpx

from app.config import get_settings
from app.errors import AppError


def _post(kind: str, payload: dict) -> dict:
    settings = get_settings()
    prefix = "llm" if kind == "chat/completions" else "embedding"
    if not getattr(settings, f"{prefix}_enabled"):
        raise AppError(503, "MODEL_NOT_CONFIGURED", "尚未配置对话模型" if prefix == "llm" else "尚未配置向量模型")
    base_url = getattr(settings, f"{prefix}_base_url").rstrip("/")
    api_key = getattr(settings, f"{prefix}_api_key")
    payload = {"model": getattr(settings, f"{prefix}_model"), **payload}
    try:
        response = httpx.post(f"{base_url}/{kind}", json=payload,
                              headers={"Authorization": f"Bearer {api_key}"}, timeout=settings.model_timeout)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Response must be an object")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        # Provider response bodies can contain credentials or submitted source material.
        raise AppError(502, "MODEL_REQUEST_FAILED", "模型服务调用失败，请检查服务配置或稍后重试") from exc


def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload = {"messages": messages}
    if tools:
        payload["tools"] = tools
    result = _post("chat/completions", payload)
    try:
        message = result["choices"][0]["message"]
        if not isinstance(message, dict) or not (isinstance(message.get("content"), str) or message.get("tool_calls")):
            raise ValueError("Missing model message")
        return message
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AppError(502, "MODEL_RESPONSE_INVALID", "模型服务返回了无效消息") from exc


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    result = _post("embeddings", {"input": texts})
    try:
        rows = result["data"]
        if len(rows) != len(texts) or sorted(row["index"] for row in rows) != list(range(len(texts))):
            raise ValueError("Embedding count or indices do not match")
        vectors = [row["embedding"] for row in sorted(rows, key=lambda row: row["index"])]
        dimension = len(vectors[0])
        if not dimension or any(len(v) != dimension for v in vectors):
            raise ValueError("Inconsistent embedding dimensions")
        if any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for v in vectors for x in v):
            raise ValueError("Non-finite embedding")
        if any(not any(x != 0 for x in v) for v in vectors):
            raise ValueError("Zero embedding cannot be used for cosine distance")
        return vectors
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AppError(502, "MODEL_RESPONSE_INVALID", "向量模型返回了无效结果") from exc
