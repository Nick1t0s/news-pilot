from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from app.config import LLMConfig
from app.providers.retry import with_retries

log = logging.getLogger("llm")

_TRANSIENT = (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError, asyncio.TimeoutError)


class LLMError(RuntimeError):
    pass


class LLMProvider:
    """OpenAI-compatible LLM with vision and structured output support."""

    def __init__(self, cfg: LLMConfig) -> None:
        self._cfg = cfg
        self._http = httpx.AsyncClient(
            proxy=cfg.proxy or None,
            timeout=cfg.timeout_seconds,
        )
        self._client = AsyncOpenAI(
            api_key=cfg.api_key or "missing",
            base_url=cfg.base_url,
            timeout=cfg.timeout_seconds,
            max_retries=0,
            default_headers=cfg.extra_headers or None,
            http_client=self._http,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ):
        """Raw chat completion; returns the openai ChatCompletion object."""

        async def call() -> object:
            return await self._client.chat.completions.create(
                model=self._cfg.model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools,
                tool_choice="auto" if tools else None,
                temperature=self._cfg.temperature if temperature is None else temperature,
                reasoning_effort=self._cfg.reasoning_effort or None,
                extra_body=self._cfg.extra_body or None,
            )

        try:
            start = time.monotonic()
            result = await with_retries(
                call, attempts=self._cfg.retries, exceptions=_TRANSIENT, what="llm.chat"
            )
            log.info("chat ok model=%s tools=%d in %.1fs", self._cfg.model, len(tools or []), time.monotonic() - start)
            return result
        except _TRANSIENT as exc:
            raise LLMError(f"llm chat failed: {exc}") from exc

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        schema_name: str = "response",
        temperature: float | None = None,
    ) -> dict:
        temp = self._cfg_temperature(temperature)
        strict_schema = {
            "name": schema_name,
            "strict": True,
            "schema": _make_strict(schema),
        }
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        # Modes tried in order: json_schema -> json_object (schema in prompt) ->
        # plain prompt; a failure moves to the next mode, a transient error retries in place.
        modes = ["json_schema", "json_object", "json_object"]
        mode_index = 0
        last_error: Exception | None = None
        for attempt in range(1, self._cfg.retries + 1):
            mode = modes[min(mode_index, len(modes) - 1)]
            response_format: dict
            sys_content = system
            if mode == "json_schema":
                response_format = {"type": "json_schema", "json_schema": strict_schema}
            else:
                response_format = {"type": "json_object"}
                sys_content = (
                    system
                    + "\n\nReturn ONLY a valid JSON object matching this JSON Schema (no extra keys):\n"
                    + json.dumps(schema, ensure_ascii=False)
                )
                messages[0] = {"role": "system", "content": sys_content}
            try:
                completion = await self._client.chat.completions.create(
                    model=self._cfg.model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=temp,
                    response_format=response_format,  # type: ignore[arg-type]
                    reasoning_effort=self._cfg.reasoning_effort or None,
                    extra_body=self._cfg.extra_body or None,
                )
            except BadRequestError as exc:
                if mode == "json_schema":
                    log.warning("json_schema rejected by provider, falling back to json_object: %s", exc)
                    mode_index += 1
                    continue
                last_error = exc
            except _TRANSIENT as exc:
                last_error = exc
            else:
                message = completion.choices[0].message
                content = message.content
                raw = content or getattr(message, "reasoning_content", None) or ""
                if content is None or not content.strip():
                    # thinking models often put the answer into reasoning_content
                    content = getattr(message, "reasoning_content", None) or content or ""
                try:
                    return _parse_json(content)
                except (TypeError, ValueError) as exc:
                    last_error = exc
                    log.warning(
                        "llm json parse failed (attempt %d/%d, mode=%s): %s; raw=%r",
                        attempt, self._cfg.retries, mode, exc, _snippet(raw),
                    )
                    # broken output, not transient: retry in a stricter mode
                    if mode_index < len(modes) - 1:
                        mode_index += 1
            if attempt < self._cfg.retries:
                await asyncio.sleep(min(10.0, 0.5 * (2 ** (attempt - 1))))
        raise LLMError(f"llm structured completion failed: {last_error}")

    def _cfg_temperature(self, temperature: float | None) -> float:
        return self._cfg.temperature if temperature is None else temperature


def _make_strict(schema: dict) -> dict:
    """Best-effort conversion to OpenAI strict json_schema requirements:
    all properties required, additionalProperties disabled, nested objects fixed too."""
    out = dict(schema)
    props = out.get("properties", {})
    out["required"] = list(props.keys())
    out["additionalProperties"] = False
    for value in props.values():
        if not isinstance(value, dict):
            continue
        if value.get("type") == "array" and isinstance(value.get("items"), dict):
            value["items"] = _make_strict(value["items"])
        elif "anyOf" in value:
            for variant in value["anyOf"]:
                if isinstance(variant, dict) and variant.get("type") == "object" and "properties" in variant:
                    _strict_object(variant)
        elif value.get("type") == "object" and "properties" in value:
            _strict_object(value)
    return out


def _strict_object(value: dict) -> None:
    props = value.get("properties", {})
    value["required"] = list(props.keys())
    value["additionalProperties"] = False


def _snippet(raw: object) -> str:
    try:
        text = str(raw or "")
    except Exception:  # noqa: BLE001
        return ""
    text = text.replace("\n", "\\n")
    return text[:200]


def _extract_json_object(text: str) -> str:
    """Cut the first balanced {...} block out of the text (models add prose/reasoning around JSON)."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in llm response")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("unbalanced JSON object in llm response")


def _parse_json(content: str | None) -> dict:
    if not content:
        raise ValueError("empty llm response")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # response contains reasoning text around the JSON object
        data = json.loads(_extract_json_object(text))
    if not isinstance(data, dict):
        raise TypeError("expected JSON object")
    return data
