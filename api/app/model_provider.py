"""Application configuration facade over framework-independent model gateways."""

import httpx  # Kept here so existing controlled HTTP tests can patch the transport.

from patentcompass_core.generation import EmbeddingConfig, EmbeddingGateway, GenerationConfig, ResponsesGateway

from app.config import get_settings


def _generation():
    settings = get_settings()
    return ResponsesGateway(GenerationConfig(settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                                            settings.model_timeout, settings.llm_extra_body))


def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    return _generation().chat(messages, tools)


def stream_chat(messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
    return _generation().stream_chat(messages, tools, on_delta)


def embed(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    return EmbeddingGateway(EmbeddingConfig(settings.embedding_base_url, settings.embedding_api_key,
                                          settings.embedding_model, settings.model_timeout)).embed(texts)
