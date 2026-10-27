"""Reject requests River is documented not to accept, before they cost anything.

Sources: docs.river.ai/guides/inference-controls/ (Responses limitations),
/guides/inference-images/ and /guides/inference-limits/ (JPEG/PNG only).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .config import ValidationConfig
from .errors import GatewayError
from .usage import Endpoint

# Responses features River does not support: it is stateless.
UNSUPPORTED_RESPONSES_PARAMS = {
    "previous_response_id": "River's Responses API is stateless: send the full history in `input` instead.",
    "conversation": "River's Responses API is stateless: send the full history in `input` instead.",
}


def validate_request(endpoint: Endpoint, body: dict[str, Any], config: ValidationConfig) -> None:
    """Validate (and normalise in place) a chat or responses request body."""
    if "stream" in body and not isinstance(body["stream"], bool):
        raise GatewayError(400, "`stream` must be a boolean.", param="stream")
    if endpoint == "chat":
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise GatewayError(400, "`messages` must be a non-empty array.", param="messages")
        _check_images(_chat_image_urls(messages), config, "messages")
    else:
        if "input" not in body:
            raise GatewayError(400, "`input` is required.", param="input")
        if config.reject_unsupported_responses_params:
            _check_responses_params(body)
        if config.responses_store_default_false:
            body.setdefault("store", False)
        _check_images(_responses_image_urls(body.get("input")), config, "input")


def _check_responses_params(body: dict[str, Any]) -> None:
    if body.get("store") is True:
        raise GatewayError(
            400,
            "`store: true` is not supported by River. Omit it or set `store: false`, and send the full history in `input`.",
            code="unsupported_parameter",
            param="store",
        )
    if body.get("background") is True:
        raise GatewayError(
            400, "`background: true` is not supported by River.", code="unsupported_parameter", param="background"
        )
    for param, message in UNSUPPORTED_RESPONSES_PARAMS.items():
        if body.get(param) is not None:
            raise GatewayError(400, f"`{param}` is not supported. {message}", code="unsupported_parameter", param=param)


def _chat_image_urls(messages: list[Any]) -> Iterator[str]:
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if isinstance(url, str):
                    yield url


def _responses_image_urls(items: Any) -> Iterator[str]:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "input_image" and isinstance(item.get("image_url"), str):
            yield item["image_url"]
        content = item.get("content")
        if isinstance(content, list):
            yield from _responses_image_urls(content)


def _check_images(urls: Iterator[str], config: ValidationConfig, param: str) -> None:
    allowed = {mime.lower() for mime in config.image_mime_types}
    for url in urls:
        if not url.startswith("data:"):
            continue
        mime = url[len("data:") :].split(";", 1)[0].split(",", 1)[0].strip().lower()
        if mime not in allowed:
            raise GatewayError(
                400,
                f"Unsupported image type `{mime or 'unknown'}`. River accepts {', '.join(sorted(allowed))} images.",
                code="unsupported_image_type",
                param=param,
            )
