import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import cache, lru_cache
from typing import Any, NoReturn, cast

import litellm

# from litellm import get_supported_openai_params
from litellm import get_max_tokens

from unstract.sdk1.adapters.constants import Common
from unstract.sdk1.adapters.llm1 import adapters
from unstract.sdk1.auth.openai_oauth import (
    OPENAI_OAUTH_CHATGPT_API_BASE,
    is_openai_oauth_adapter,
)
from unstract.sdk1.constants import Common as SdkCommon
from unstract.sdk1.constants import ToolEnv
from unstract.sdk1.exceptions import LLMError, SdkError, strip_litellm_prefix
from unstract.sdk1.platform import PlatformHelper
from unstract.sdk1.tool.base import BaseTool
from unstract.sdk1.utils.common import (
    LLMResponseCompat,
    capture_metrics,
)
from unstract.sdk1.utils.retry_utils import (
    acall_with_retry,
    call_with_retry,
    is_retryable_litellm_error,
    iter_with_retry,
    pop_litellm_retry_kwargs,
)

logger = logging.getLogger(__name__)


# Truthy-looking values that people commonly set expecting a boolean flag to
# turn on — but only "true" enables caching. We warn (once each) so a stray
# ENABLE_PROMPT_CACHING=1 doesn't leave caching silently off.
_PROMPT_CACHING_TRUTHY_LOOKALIKES = frozenset(
    {"1", "yes", "y", "on", "t", "enable", "enabled"}
)


@cache
def _warn_prompt_caching_lookalike(value: str) -> None:
    logger.warning(
        "ENABLE_PROMPT_CACHING=%r is not recognized as enabled; only 'true' "
        "(case-insensitive) turns prompt caching on — caching stays OFF.",
        value,
    )


def is_prompt_caching_enabled() -> bool:
    """Whether LLM prompt caching is enabled platform-wide (opt-in, default off).

    A single master switch (``ENABLE_PROMPT_CACHING`` env var) that turns the
    caching capability on for every ``LLM`` on a supported provider, so callers
    don't each have to pass ``enable_prompt_caching``. Consumers still decide
    *what* to cache by passing ``cache_prefix``. Exposed for consumers that gate
    their own prompt-restructuring on the same flag.

    Only ``"true"`` (case-insensitive) enables it; a truthy-looking value like
    ``"1"``/``"yes"``/``"on"`` logs a one-time warning and stays off.
    """
    raw = os.environ.get("ENABLE_PROMPT_CACHING", "").strip().lower()
    if raw == "true":
        return True
    if raw in _PROMPT_CACHING_TRUTHY_LOOKALIKES:
        _warn_prompt_caching_lookalike(raw)
    return False


# Lets tests force a deterministic completion without a provider or a secret.
# Unset in production, where this is a no-op.
_MOCK_RESPONSE_ENV = "UNSTRACT_LLM_MOCK_RESPONSE"


@lru_cache(maxsize=1)
def _warn_mock_active() -> None:
    # Once per process: the hatch is silent otherwise, and a stray env var in
    # production would fake every completion and its billing.
    logger.warning(
        "%s is set — returning canned completions instead of calling the "
        "provider, with synthetic token usage. Unset it outside tests.",
        _MOCK_RESPONSE_ENV,
    )


def _inject_mock_response(completion_kwargs: dict[str, object]) -> None:
    mock = os.getenv(_MOCK_RESPONSE_ENV)
    if not mock or "mock_response" in completion_kwargs:
        return
    _warn_mock_active()
    completion_kwargs["mock_response"] = mock


# Drop unsupported params rather than raising errors.
# Set once at module level instead of per-call to avoid repeated
# global mutation in concurrent environments.
litellm.drop_params = True

# Request-id response headers across providers, checked in order:
# OpenAI/Azure OpenAI, Anthropic, AWS Bedrock, Azure API Management.
# litellm forwards these in _hidden_params["additional_headers"]
# prefixed with "llm_provider-".
_PROVIDER_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-amzn-requestid",
    "apim-request-id",
)


def extract_provider_ids(response: object) -> tuple[str | None, str | None]:
    """Extract the provider's response id and request id from a litellm response.

    Returns (response_id, request_id) where response_id is the body-level id
    (e.g. OpenAI "chatcmpl-...", Anthropic "msg_...") and request_id is the
    provider's request-id response header — the value providers ask for when
    troubleshooting a specific API call.

    Either value may be None; some providers (e.g. VertexAI/Gemini) expose
    neither, in which case litellm generates a synthetic response id.
    """
    if response is None:
        return None, None
    try:
        response_id = response.get("id")
    except (AttributeError, TypeError):
        response_id = None
    if response_id is None:
        response_id = getattr(response, "id", None)

    hidden_params = getattr(response, "_hidden_params", None) or {}
    headers = hidden_params.get("additional_headers") or {}
    normalized = {
        key.lower().removeprefix("llm_provider-"): value for key, value in headers.items()
    }
    request_id = next(
        (normalized[h] for h in _PROVIDER_REQUEST_ID_HEADERS if normalized.get(h)),
        None,
    )
    return response_id, request_id


# ── Emulated llama-index types ───────────────────────────────────────────────
# These types emulate the llama-index interface without requiring the dependency.
# This allows LLMCompat to work with llama-index components like
# SubQuestionQueryEngine, QueryFusionRetriever, etc.


class MessageRole(str, Enum):
    """Emulates llama_index.core.base.llms.types.MessageRole."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    FUNCTION = "function"
    TOOL = "tool"


@dataclass
class ChatMessage:
    """Emulates llama_index.core.base.llms.types.ChatMessage."""

    role: MessageRole = MessageRole.USER
    content: str | None = None
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    """Emulates llama_index.core.base.llms.types.ChatResponse."""

    message: ChatMessage = field(default_factory=ChatMessage)
    raw: Any = None
    delta: str | None = None
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResponse:
    """Emulates llama_index.core.base.llms.types.CompletionResponse."""

    text: str = ""
    raw: Any = None
    delta: str | None = None
    additional_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMMetadata:
    """Emulates llama_index.core.base.llms.types.LLMMetadata."""

    context_window: int = 4096
    num_output: int = 256
    is_chat_model: bool = True
    is_function_calling_model: bool = False
    model_name: str = ""
    system_role: MessageRole = MessageRole.SYSTEM


class LLM:
    """Unified LLM interface powered by LiteLLM.

    Internally invokes Unstract LLM adapters.

    Accepts either of the following pairs for init:
    - adapter ID and metadata       (e.g. test connection)
    - adapter instance ID and tool  (e.g. edit adapter)
    """

    SYSTEM_PROMPT = "You are a helpful assistant."
    MAX_TOKENS = 4096
    JSON_REGEX = re.compile(r"\[(?:.|\n)*\]|\{(?:.|\n)*\}")
    JSON_CONTENT_MARKER = os.environ.get("JSON_SELECTION_MARKER", "§§§")

    def __init__(  # noqa: C901
        self,
        adapter_id: str = "",
        adapter_metadata: dict[str, object] | None = None,
        adapter_instance_id: str = "",
        tool: BaseTool | None = None,
        usage_kwargs: dict[str, object] | None = None,
        system_prompt: str = "",
        kwargs: dict[str, object] | None = None,
        capture_metrics: bool = False,
        enable_prompt_caching: bool = False,
    ) -> None:
        """Initialize the LLM interface.

        Args:
            adapter_id: Adapter identifier for LLM model
            adapter_metadata: Configuration metadata for the adapter
            adapter_instance_id: Instance identifier for the adapter
            tool: BaseTool instance for tool-specific operations
            usage_kwargs: Usage tracking parameters
            system_prompt: System prompt for the LLM
            kwargs: Additional keyword arguments for configuration
            capture_metrics: Whether to capture performance metrics
            enable_prompt_caching: Force provider prompt caching on for
                supported providers (Anthropic / Bedrock-Anthropic), regardless
                of the stored adapter metadata. Ignored for other providers.
        """
        if adapter_metadata is None:
            adapter_metadata = {}
        if usage_kwargs is None:
            usage_kwargs = {}
        if kwargs is None:
            kwargs = {}
        self._usage_kwargs = usage_kwargs
        self._capture_metrics = capture_metrics
        try:
            llm_config = None

            if adapter_instance_id:
                if not tool:
                    raise SdkError(
                        "Broken LLM adapter tool binding: " + adapter_instance_id
                    )
                llm_config = PlatformHelper.get_adapter_config(tool, adapter_instance_id)

            if llm_config:
                self._adapter_id = llm_config[Common.ADAPTER_ID]
                self._adapter_metadata = llm_config[Common.ADAPTER_METADATA]
                self._adapter_instance_id = adapter_instance_id
                self._adapter_name = llm_config.pop(SdkCommon.ADAPTER_NAME, "")
                self._tool = tool
            else:
                self._adapter_id = adapter_id
                if adapter_metadata:
                    self._adapter_metadata = adapter_metadata
                else:
                    self._adapter_metadata = adapters[self._adapter_id][Common.METADATA]
                self._adapter_instance_id = ""
                self._adapter_name = ""
                self._tool = None

            # Retrieve the adapter class.
            self.adapter = adapters[self._adapter_id][Common.MODULE]
        except KeyError as e:
            raise SdkError(
                f"LLM adapter not supported: {adapter_id or adapter_instance_id}"
            ) from e

        try:
            self.platform_kwargs = {**kwargs, **usage_kwargs}

            if self._adapter_instance_id:
                self.platform_kwargs["adapter_instance_id"] = self._adapter_instance_id

            self.kwargs = self.adapter.validate(self._adapter_metadata)
            self._cost_model = self.kwargs.pop("cost_model", None)
            self.kwargs.pop("context_window", None)
            # Opt-in provider prompt caching (Anthropic / Bedrock-Anthropic).
            # Enabled either via adapter metadata (from the stored adapter
            # config) or the explicit constructor arg (for callers that build
            # the LLM by ``adapter_instance_id`` and can't edit stored metadata).
            # Popped so it never reaches litellm; applied on the message payload.
            self._enable_prompt_caching = (
                bool(self.kwargs.pop("enable_prompt_caching", False))
                or enable_prompt_caching
                or is_prompt_caching_enabled()
            )

            # REF: https://docs.litellm.ai/docs/completion/input#translated-openai-params
            # supported = get_supported_openai_params(model=self.kwargs["model"],
            #     custom_llm_provider=self.provider)
            # for s in supported:
            #     if s not in self.kwargs:
            #         logger.warning("Missing supported parameter for '%s': %s",
            #             self.adapter.get_provider(), s)
        except ValueError as e:
            # `pydantic.ValidationError` subclasses `ValueError` — this catches both.
            raise SdkError("Invalid LLM adapter metadata: " + str(e)) from e

        self._system_prompt = system_prompt or self.SYSTEM_PROMPT

        if self._tool:
            self._platform_api_key = self._tool.get_env_or_die(ToolEnv.PLATFORM_API_KEY)
            if not self._platform_api_key:
                raise SdkError(f"Missing env variable '{ToolEnv.PLATFORM_API_KEY}'")
        else:
            self._platform_api_key = os.environ.get(ToolEnv.PLATFORM_API_KEY, "")

        # Metrics capture.
        self._run_id = self.platform_kwargs.get("run_id")
        # Only override capture_metrics if it's explicitly set in platform_kwargs
        capture_metrics_from_platform = self.platform_kwargs.get("capture_metrics")
        if capture_metrics_from_platform is not None:
            self._capture_metrics = capture_metrics_from_platform
        self._metrics: dict[str, object] = {}
        self._pending_usage: list[dict] = []

    def _get_adapter_info(self) -> str:
        """Build a display string identifying this adapter for errors."""
        provider = self.adapter.get_provider()
        if self._adapter_name:
            return f"{self._adapter_name} ({provider})"
        return provider

    def test_connection(self) -> bool:
        """Test connection to the LLM provider."""
        try:
            response = self.complete("What is the capital of Tamilnadu?")
            text = response["response"].text

            find_match = re.search("chennai", text.lower())
            if find_match:
                return True

            logger.error("LLM test response: %s", text)
            msg = (
                "LLM based test failed. The credentials was valid however a sane "
                "response was not obtained from the LLM provider, please recheck "
                "the configuration."
            )
            raise LLMError(message=msg, status_code=400)
        except LLMError:
            # Already wrapped in LLMError from complete(), re-raise as is
            raise
        except SdkError:
            # Already wrapped in SdkError, re-raise as is
            raise
        except Exception as e:
            # Catch any unexpected exceptions and wrap them
            logger.error("Failed to test connection for LLM: %s", e)

            # Extract status code if available
            status_code = None
            if hasattr(e, "status_code"):
                status_code = e.status_code
            elif hasattr(e, "http_status"):
                status_code = e.http_status

            # Wrap in LLMError with context
            raise LLMError(
                message=f"Failed to test LLM connection: {str(e)}",
                status_code=status_code,
                actual_err=e,
            ) from e

    # Providers for which we emit explicit ``cache_control`` blocks. Anthropic
    # and Bedrock-Anthropic support message-level prompt caching this way;
    # OpenAI / Azure auto-cache server-side (no marker needed) and other
    # providers don't support it, so we never tag their payloads.
    _PROMPT_CACHE_PROVIDERS = frozenset({"anthropic", "bedrock"})
    # Bedrock hosts many model families (Anthropic Claude, Amazon Titan/Nova,
    # Meta Llama, Cohere, Mistral, AI21). Only Anthropic/Claude models on
    # Bedrock honor ``cache_control``; emitting the blocks for other families
    # would be ineffective and could produce unsupported message shapes. These
    # substrings identify the cache-capable Bedrock models by their model id.
    _BEDROCK_CACHE_MODEL_MARKERS = ("anthropic", "claude")

    def _prompt_caching_active(self) -> bool:
        """Whether to emit ``cache_control`` blocks for this call."""
        if not self._enable_prompt_caching:
            return False
        provider = self.adapter.get_provider()
        if provider not in self._PROMPT_CACHE_PROVIDERS:
            return False
        if provider == "bedrock":
            # Gate on the underlying model, not just the provider: only
            # Anthropic/Claude models on Bedrock support cache_control. Check
            # both ``model`` and ``model_id`` — when a caller routes through a
            # Bedrock Application Inference Profile, the ARN in ``model`` is
            # opaque and the Claude id appears only in ``model_id``.
            recognized = any(
                marker in str(self.kwargs.get(field, "")).lower()
                for field in ("model", "model_id")
                for marker in self._BEDROCK_CACHE_MODEL_MARKERS
            )
            if not recognized:
                # Enabled but the model can't be confirmed as Anthropic/Claude
                # (e.g. a fully opaque Application Inference Profile ARN with no
                # Claude id in model/model_id). Skipping is safe; leave a
                # breadcrumb so operators can diagnose a Claude-on-Bedrock call
                # that unexpectedly isn't caching.
                logger.debug(
                    "Prompt caching enabled but skipped for Bedrock: "
                    "model=%r model_id=%r not recognized as Anthropic/Claude",
                    self.kwargs.get("model"),
                    self.kwargs.get("model_id"),
                )
            return recognized
        return True

    def is_prompt_caching_active(self) -> bool:
        """Public: whether ``cache_control`` blocks are emitted for this LLM.

        True only when caching is enabled (adapter flag, constructor arg, or the
        ``ENABLE_PROMPT_CACHING`` master switch) *and* the provider/model
        supports it. Callers use this to decide whether reordering a prompt into
        a cached prefix is worthwhile — reordering for a non-caching provider
        changes prompt structure with no benefit.
        """
        return self._prompt_caching_active()

    def _build_messages(
        self, prompt: str, cache_prefix: str | None = None
    ) -> list[dict[str, object]]:
        """Build the system + user message list for a chat completion.

        When prompt caching is active (opt-in flag + a supported provider), a
        stable prefix is tagged with ``cache_control`` so providers that support
        prefix caching (Anthropic, Bedrock-Anthropic) reuse it across calls.
        LiteLLM forwards ``cache_control`` blocks to the provider unchanged.

        - non-empty ``cache_prefix`` given: the user turn is split into a cached
          stable prefix block followed by the per-request volatile block. The
          text the model sees is ``cache_prefix + prompt`` — identical to
          passing the concatenation as a single prompt, so no prompt semantics
          change.
        - otherwise: the stable system prompt is cached.

        Only the stable portion is tagged; per-request content is never cached.
        An empty ``cache_prefix`` is treated as absent — Anthropic rejects empty
        text content blocks, and an empty prefix carries no caching benefit.
        """
        if self._prompt_caching_active() and cache_prefix:
            return [
                {"role": "system", "content": self._system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": cache_prefix,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                },
            ]
        if self._prompt_caching_active():
            return [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": self._system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "user", "content": prompt},
            ]
        # Caching inactive (opt-in off, or an unsupported provider): emit no
        # cache_control, but if the caller split the prompt into
        # (cache_prefix, prompt) the model must still see the full text —
        # concatenate rather than drop the prefix.
        user_content = cache_prefix + prompt if cache_prefix else prompt
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _uses_openai_oauth(self) -> bool:
        """Whether this LLM uses the per-account ChatGPT OAuth adapter."""
        return is_openai_oauth_adapter(self._adapter_id)

    @staticmethod
    def _response_value(
        response: object, key: str, default: object | None = None
    ) -> object | None:
        """Read a field from a LiteLLM dict, model, or response event."""
        if isinstance(response, Mapping):
            return response.get(key, default)
        try:
            value = response[key]  # type: ignore[index]
        except (KeyError, TypeError, AttributeError, IndexError):
            value = getattr(response, key, default)
        return value if value is not None else default

    @staticmethod
    def _responses_block(block: object) -> dict[str, object]:
        """Convert one chat content block to a Responses input block."""
        if not isinstance(block, Mapping):
            return {"type": "input_text", "text": str(block)}
        block_type = block.get("type")
        if block_type in ("text", "input_text"):
            return {"type": "input_text", "text": block.get("text", "")}
        if block_type == "image_url":
            image_url = block.get("image_url")
            detail = None
            if isinstance(image_url, Mapping):
                detail = image_url.get("detail")
                image_url = image_url.get("url")
            image_block: dict[str, object] = {
                "type": "input_image",
                "image_url": image_url,
            }
            if detail:
                image_block["detail"] = detail
            return image_block
        if isinstance(block_type, str) and block_type.startswith("input_"):
            # Preserve already-converted Responses blocks for callers that use
            # complete_vision() directly.
            return dict(block)
        return {"type": "input_text", "text": json.dumps(dict(block))}

    @classmethod
    def _responses_content(cls, content: object) -> list[dict[str, object]]:
        """Convert OpenAI chat content blocks to Responses input blocks."""
        if isinstance(content, str):
            return [{"type": "input_text", "text": content}]
        if not isinstance(content, list):
            if content is None:
                return []
            return [{"type": "input_text", "text": str(content)}]
        return [cls._responses_block(block) for block in content]

    @classmethod
    def _responses_input(
        cls, messages: list[dict[str, object]]
    ) -> tuple[str, list[dict[str, object]]]:
        instructions: list[str] = []
        response_input: list[dict[str, object]] = []
        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content")
            if role == "system":
                text_blocks = cls._responses_content(content)
                instructions.extend(
                    str(block.get("text", ""))
                    for block in text_blocks
                    if block.get("type") == "input_text"
                )
                continue
            if role not in {"user", "assistant"}:
                role = "user"
            response_input.append(
                {
                    "type": "message",
                    "role": role,
                    "content": cls._responses_content(content),
                }
            )
        return "\n".join(text for text in instructions if text), response_input

    @staticmethod
    def _responses_tools(tools: object) -> object:
        """Translate chat function tools to the Responses tool shape."""
        if not isinstance(tools, list):
            return tools
        converted: list[object] = []
        for tool in tools:
            if not isinstance(tool, Mapping) or tool.get("type") != "function":
                converted.append(tool)
                continue
            function = tool.get("function")
            if not isinstance(function, Mapping):
                converted.append(tool)
                continue
            converted.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description"),
                    "parameters": function.get("parameters"),
                }
            )
        return converted

    def _build_openai_oauth_responses_kwargs(
        self,
        messages: list[dict[str, object]],
        completion_kwargs: dict[str, object],
        *,
        stream: bool,
    ) -> dict[str, object]:
        """Build a Responses API call with credentials for one adapter only."""
        values = dict(completion_kwargs)
        access_token = str(values.pop("oauth_access_token"))
        account_id = str(values.pop("oauth_account_id"))
        values.pop("oauth_refresh_token", None)
        values.pop("oauth_id_token", None)
        values.pop("oauth_account_email", None)
        values.pop("oauth_expires_at", None)
        values.pop("oauth_authenticated", None)

        model = str(values.pop("model"))
        api_base = str(values.pop("api_base", OPENAI_OAUTH_CHATGPT_API_BASE))
        max_tokens = values.pop("max_tokens", None)
        values.pop("temperature", None)
        values.pop("n", None)
        values.pop("api_version", None)
        values.pop("enable_reasoning", None)
        values.pop("max_retries", None)
        values.pop("num_retries", None)
        values.pop("cost_model", None)
        values.pop("context_window", None)

        if max_tokens is not None:
            values["max_output_tokens"] = max_tokens
        reasoning_effort = values.pop("reasoning_effort", None)
        # The ChatGPT Codex endpoint expects this field on Responses requests
        # so encrypted reasoning can be returned and reused by the account.
        values["include"] = ["reasoning.encrypted_content"]
        if reasoning_effort:
            values["reasoning"] = {"effort": reasoning_effort}
        if "tools" in values:
            values["tools"] = self._responses_tools(values["tools"])

        instructions, response_input = self._responses_input(messages)
        values.update(
            {
                "model": model,
                "input": response_input,
                "custom_llm_provider": "openai",
                # LiteLLM's OpenAI handler uses this key to select its client;
                # the explicit Authorization header below is account-specific.
                "api_key": access_token,
                "api_base": api_base,
                "stream": stream,
                "store": False,
            }
        )
        if instructions:
            values["instructions"] = instructions

        headers = dict(values.pop("extra_headers", {}) or {})
        headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "ChatGPT-Account-Id": account_id,
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "originator": "unstract",
                "session-id": str(uuid.uuid4()),
            }
        )
        values["extra_headers"] = headers
        return values

    @classmethod
    def _responses_output_text(cls, response: object) -> str | None:
        output_text = cls._response_value(response, "output_text")
        if isinstance(output_text, str):
            return output_text
        output = cls._response_value(response, "output")
        if not isinstance(output, list):
            return None
        text_parts: list[str] = []
        for item in output:
            if cls._response_value(item, "type") != "message":
                continue
            content = cls._response_value(item, "content")
            if not isinstance(content, list):
                continue
            for block in content:
                if cls._response_value(block, "type") in {
                    "output_text",
                    "text",
                }:
                    text = cls._response_value(block, "text")
                    if isinstance(text, str):
                        text_parts.append(text)
        return "".join(text_parts) or None

    @classmethod
    def _responses_usage(cls, usage: object) -> dict[str, int]:
        if usage is None:
            return {}

        def integer(key: str, fallback: str) -> int:
            value = cls._response_value(usage, key)
            if value is None:
                value = cls._response_value(usage, fallback, 0)
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "prompt_tokens": integer("input_tokens", "prompt_tokens"),
            "completion_tokens": integer("output_tokens", "completion_tokens"),
            "total_tokens": integer("total_tokens", "total_tokens"),
        }

    def _collect_openai_oauth_response(
        self,
        messages: list[dict[str, object]],
        completion_kwargs: dict[str, object],
        max_retries: int,
    ) -> tuple[str | None, object | None, dict[str, int]]:
        """Consume a streaming-only Responses API call as one completion.

        The ChatGPT/Codex endpoint rejects ``stream=False``.  Keep the public
        ``complete()`` contract by consuming the stream internally and
        returning the assembled text, completed response, and usage.
        """
        response_kwargs = self._build_openai_oauth_responses_kwargs(
            messages, completion_kwargs, stream=True
        )
        text_parts: list[str] = []
        completed_response: object | None = None
        last_event: object | None = None

        for event in iter_with_retry(
            lambda: litellm.responses(**response_kwargs),
            max_retries=max_retries,
            retry_predicate=is_retryable_litellm_error,
            description=self._get_adapter_info(),
        ):
            last_event = event
            event_type = self._response_value(event, "type")
            if event_type == "response.output_text.delta":
                text = self._response_value(event, "delta", "")
                if isinstance(text, str):
                    text_parts.append(text)
            elif event_type == "response.completed":
                response = self._response_value(event, "response")
                if response is not None:
                    completed_response = response

        response = (
            completed_response if completed_response is not None else last_event
        )
        response_text = "".join(text_parts) or self._responses_output_text(response)
        usage_source = (
            completed_response if completed_response is not None else response
        )
        usage = self._responses_usage(self._response_value(usage_source, "usage"))
        return response_text, response, usage

    async def _acollect_openai_oauth_response(
        self,
        messages: list[dict[str, object]],
        completion_kwargs: dict[str, object],
        max_retries: int,
    ) -> tuple[str | None, object | None, dict[str, int]]:
        """Async counterpart to :meth:`_collect_openai_oauth_response`."""
        response_kwargs = self._build_openai_oauth_responses_kwargs(
            messages, completion_kwargs, stream=True
        )

        async def consume_stream() -> tuple[
            str | None, object | None, dict[str, int]
        ]:
            text_parts: list[str] = []
            completed_response: object | None = None
            last_event: object | None = None
            stream = await litellm.aresponses(**response_kwargs)

            async for event in stream:
                last_event = event
                event_type = self._response_value(event, "type")
                if event_type == "response.output_text.delta":
                    text = self._response_value(event, "delta", "")
                    if isinstance(text, str):
                        text_parts.append(text)
                elif event_type == "response.completed":
                    response = self._response_value(event, "response")
                    if response is not None:
                        completed_response = response

            response = (
                completed_response if completed_response is not None else last_event
            )
            response_text = "".join(text_parts) or self._responses_output_text(
                response
            )
            usage_source = (
                completed_response if completed_response is not None else response
            )
            usage = self._responses_usage(
                self._response_value(usage_source, "usage")
            )
            return response_text, response, usage

        return await acall_with_retry(
            consume_stream,
            max_retries=max_retries,
            retry_predicate=is_retryable_litellm_error,
            description=self._get_adapter_info(),
        )

    def _stream_openai_oauth(
        self,
        messages: list[dict[str, object]],
        completion_kwargs: dict[str, object],
        callback_manager: object | None,
        max_retries: int,
    ) -> Generator[LLMResponseCompat, None, None]:
        """Yield text events from one account's Responses API stream."""
        response_kwargs = self._build_openai_oauth_responses_kwargs(
            messages, completion_kwargs, stream=True
        )
        for event in iter_with_retry(
            lambda: litellm.responses(**response_kwargs),
            max_retries=max_retries,
            retry_predicate=is_retryable_litellm_error,
            description=self._get_adapter_info(),
        ):
            event_type = self._response_value(event, "type")
            if event_type == "response.completed":
                completed_response = self._response_value(event, "response")
                usage = self._responses_usage(
                    self._response_value(completed_response, "usage")
                )
                if usage:
                    self._record_usage(
                        self._cost_model or self.kwargs["model"],
                        messages,
                        usage,
                        "stream_complete",
                        response=completed_response,
                    )
                continue
            if event_type != "response.output_text.delta":
                continue
            text = self._response_value(event, "delta", "")
            if not isinstance(text, str) or not text:
                continue
            if callback_manager and hasattr(callback_manager, "on_stream"):
                callback_manager.on_stream(text)
            stream_response = LLMResponseCompat(text)
            stream_response.delta = text
            yield stream_response

    @capture_metrics
    def complete(
        self,
        prompt: str,
        cache_prefix: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        """Return a standard chat completion dict with optional metrics capture.

        Return a standard chat completion dict and optionally captures metrics if run
        ID is provided.

        Args:
            prompt   (str)   The input text prompt for generating the completion.
            cache_prefix (str | None) Stable text to cache ahead of ``prompt``.
                When prompt caching is active for a supported provider, this is
                emitted as a ``cache_control`` block so repeated calls sharing
                the same prefix reuse it. The model sees ``cache_prefix + prompt``
                unchanged. Ignored when caching is off or unsupported.
            **kwargs (Any)   Additional arguments passed to the completion function.

        Returns:
            dict[str, Any]  : A dictionary containing the result of the completion,
                any processed output, and the captured metrics (if applicable).
        """
        try:
            messages = self._build_messages(prompt, cache_prefix=cache_prefix)
            logger.debug(
                f"[sdk1][LLM]Invoking {self.adapter.get_provider()} completion API"
            )

            completion_kwargs = self.adapter.validate({**self.kwargs, **kwargs})
            _inject_mock_response(completion_kwargs)
            completion_kwargs.pop("cost_model", None)
            completion_kwargs.pop("enable_prompt_caching", None)
            completion_kwargs.pop("context_window", None)

            # if hasattr(self, "model") and self.model not in O1_MODELS:
            #     completion_kwargs["temperature"] = 0.003
            # if hasattr(self, "thinking_dict") and self.thinking_dict is not None:
            #     completion_kwargs["temperature"] = 1

            max_retries = pop_litellm_retry_kwargs(
                completion_kwargs, self._get_adapter_info()
            )
            if self._uses_openai_oauth():
                response_text, response, usage = self._collect_openai_oauth_response(
                    messages,
                    completion_kwargs,
                    max_retries,
                )
                finish_reason = None
            else:
                response = call_with_retry(
                    lambda: litellm.completion(messages=messages, **completion_kwargs),
                    max_retries=max_retries,
                    retry_predicate=is_retryable_litellm_error,
                    description=self._get_adapter_info(),
                )
                response_text = response["choices"][0]["message"]["content"]
                finish_reason = response["choices"][0].get("finish_reason")
                usage = response.get("usage")

            self._record_usage(
                self._cost_model or self.kwargs["model"],
                messages,
                usage,
                "complete",
                response=response,
            )

            # Handle refusal or empty content from the LLM provider
            if response_text is None:
                self._raise_for_empty_response(finish_reason)

            # NOTE:
            # The typecasting was required to stop the type checker from complaining.
            # Improvements in readability are definitely welcome.
            extract_json: bool = cast("bool", kwargs.get("extract_json", False))
            post_process_fn: (
                Callable[[LLMResponseCompat, bool, str], dict[str, object]] | None
            ) = cast(
                "Callable[[LLMResponseCompat, bool, str], dict[str, object]] | None",
                kwargs.get("process_text", None),
            )

            response_text, post_processed_output = self._post_process_response(
                response_text, extract_json, post_process_fn
            )

            response_object = LLMResponseCompat(response_text)
            response_object.raw = (
                response  # Attach raw litellm response for metadata access
            )
            return {"response": response_object, **post_processed_output}

        except LLMError:
            # Already wrapped LLMError, re-raise as is
            raise
        except SdkError:
            # Already wrapped SdkError, re-raise as is
            raise
        except Exception as e:
            # Wrap all other exceptions in LLMError with provider context
            logger.error(f"[sdk1][LLM] Error during completion: {e}")

            # Extract status code if available
            status_code = None
            if hasattr(e, "status_code"):
                status_code = e.status_code
            elif hasattr(e, "http_status"):
                status_code = e.http_status

            error_msg = (
                f"Error from LLM adapter '{self._get_adapter_info()}': "
                f"{strip_litellm_prefix(str(e))}"
            )

            raise LLMError(
                message=error_msg, status_code=status_code, actual_err=e
            ) from e

    @capture_metrics
    def complete_vision(
        self,
        messages: list[dict[str, Any]],
        **kwargs: object,
    ) -> dict[str, object]:
        """Chat completion with multimodal (text + image) messages.

        Accepts pre-built messages with image_url content blocks::

            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "..."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,..."},
                        },
                    ],
                }
            ]

        LiteLLM auto-translates the OpenAI-style image format for
        Anthropic, Bedrock, Vertex, and other providers.

        Same error handling, usage tracking, and metrics as complete().

        Args:
            messages: List of message dicts with multimodal content.
            **kwargs: Additional arguments passed to litellm.completion().

        Returns:
            dict with "response" key containing LLMResponseCompat.
        """
        try:
            litellm.drop_params = True

            logger.debug(
                f"[sdk1][LLM]Invoking {self.adapter.get_provider()} vision completion API"
            )

            completion_kwargs = self.adapter.validate({**self.kwargs, **kwargs})
            _inject_mock_response(completion_kwargs)
            completion_kwargs.pop("cost_model", None)
            completion_kwargs.pop("enable_prompt_caching", None)
            completion_kwargs.pop("context_window", None)

            if self._uses_openai_oauth():
                max_retries = pop_litellm_retry_kwargs(
                    completion_kwargs, self._get_adapter_info()
                )
                response_text, response, usage = self._collect_openai_oauth_response(
                    messages,
                    completion_kwargs,
                    max_retries,
                )
                finish_reason = None
            else:
                response = litellm.completion(
                    messages=messages,
                    **completion_kwargs,
                )
                response_text = response["choices"][0]["message"]["content"]
                finish_reason = response["choices"][0].get("finish_reason")
                usage = response.get("usage")

            self._record_usage(
                self._cost_model or self.kwargs["model"],
                messages,
                usage,
                "complete_vision",
                response=response,
            )

            if response_text is None:
                self._raise_for_empty_response(finish_reason)

            response_object = LLMResponseCompat(response_text)
            response_object.raw = response
            return {"response": response_object}

        except LLMError:
            raise
        except SdkError:
            raise
        except Exception as e:
            logger.error(f"[sdk1][LLM] Error during vision completion: {e}")

            status_code = None
            if hasattr(e, "status_code"):
                status_code = e.status_code
            elif hasattr(e, "http_status"):
                status_code = e.http_status

            error_msg = (
                f"Error from LLM adapter '{self._get_adapter_info()}': "
                f"{strip_litellm_prefix(str(e))}"
            )

            raise LLMError(
                message=error_msg, status_code=status_code, actual_err=e
            ) from e

    def stream_complete(
        self,
        prompt: str,
        callback_manager: object | None = None,
        cache_prefix: str | None = None,
        **kwargs: object,
    ) -> Generator[LLMResponseCompat, None, None]:
        """Yield LLMResponseCompat objects with text chunks.

        Chunks arrive as they stream from the provider. ``cache_prefix`` behaves
        as in :meth:`complete` — a stable prefix cached ahead of ``prompt`` when
        prompt caching is active for a supported provider.
        """
        try:
            messages = self._build_messages(prompt, cache_prefix=cache_prefix)
            logger.debug(
                f"[sdk1][LLM]Invoking {self.adapter.get_provider()} stream completion API"
            )

            completion_kwargs = self.adapter.validate({**self.kwargs, **kwargs})
            _inject_mock_response(completion_kwargs)
            completion_kwargs.pop("cost_model", None)
            completion_kwargs.pop("enable_prompt_caching", None)
            completion_kwargs.pop("context_window", None)

            max_retries = pop_litellm_retry_kwargs(
                completion_kwargs, self._get_adapter_info()
            )
            has_yielded_content = False
            if self._uses_openai_oauth():
                yield from self._stream_openai_oauth(
                    messages, completion_kwargs, callback_manager, max_retries
                )
            else:
                for chunk in iter_with_retry(
                    lambda: litellm.completion(
                        messages=messages,
                        stream=True,
                        stream_options={"include_usage": True},
                        **completion_kwargs,
                    ),
                    max_retries=max_retries,
                    retry_predicate=is_retryable_litellm_error,
                    description=self._get_adapter_info(),
                ):
                    if chunk.get("usage"):
                        self._record_usage(
                            self._cost_model or self.kwargs["model"],
                            messages,
                            chunk.get("usage"),
                            "stream_complete",
                            response=chunk,
                        )

                    response = self._process_stream_chunk(
                        chunk, callback_manager, has_yielded_content
                    )
                    if response is not None:
                        has_yielded_content = True
                        yield response

        except LLMError:
            # Already wrapped LLMError, re-raise as is
            raise
        except SdkError:
            # Already wrapped SdkError, re-raise as is
            raise
        except Exception as e:
            # Wrap all other exceptions in LLMError with provider context
            logger.error(f"[sdk1][LLM] Error during stream completion: {e}")

            # Extract status code if available
            status_code = None
            if hasattr(e, "status_code"):
                status_code = e.status_code
            elif hasattr(e, "http_status"):
                status_code = e.http_status

            error_msg = (
                f"Error from LLM adapter '{self._get_adapter_info()}': "
                f"{strip_litellm_prefix(str(e))}"
            )

            raise LLMError(
                message=error_msg, status_code=status_code, actual_err=e
            ) from e

    async def acomplete(
        self,
        prompt: str,
        cache_prefix: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        """Asynchronous chat completion (wrapper around ``litellm.acompletion``).

        ``cache_prefix`` mirrors :meth:`complete` / :meth:`stream_complete`: when
        prompt caching is active it is emitted as a cached stable prefix ahead of
        ``prompt``; otherwise it is concatenated so the model sees the same text.
        """
        try:
            messages = self._build_messages(prompt, cache_prefix=cache_prefix)
            logger.debug(
                f"[sdk1][LLM]Invoking {self.adapter.get_provider()} async completion API"
            )

            completion_kwargs = self.adapter.validate({**self.kwargs, **kwargs})
            _inject_mock_response(completion_kwargs)
            completion_kwargs.pop("cost_model", None)
            completion_kwargs.pop("enable_prompt_caching", None)
            completion_kwargs.pop("context_window", None)

            max_retries = pop_litellm_retry_kwargs(
                completion_kwargs, self._get_adapter_info()
            )
            if self._uses_openai_oauth():
                response_text, response, usage = await (
                    self._acollect_openai_oauth_response(
                        messages,
                        completion_kwargs,
                        max_retries,
                    )
                )
                finish_reason = None
            else:
                response = await acall_with_retry(
                    lambda: litellm.acompletion(messages=messages, **completion_kwargs),
                    max_retries=max_retries,
                    retry_predicate=is_retryable_litellm_error,
                    description=self._get_adapter_info(),
                )
                response_text = response["choices"][0]["message"]["content"]
                finish_reason = response["choices"][0].get("finish_reason")
                usage = response.get("usage")

            self._record_usage(
                self._cost_model or self.kwargs["model"],
                messages,
                usage,
                "acomplete",
                response=response,
            )

            # Handle refusal or empty content from the LLM provider
            if response_text is None:
                self._raise_for_empty_response(finish_reason)

            response_object = LLMResponseCompat(response_text)
            response_object.raw = (
                response  # Attach raw litellm response for metadata access
            )
            return {"response": response_object}

        except LLMError:
            # Already wrapped LLMError, re-raise as is
            raise
        except SdkError:
            # Already wrapped SdkError, re-raise as is
            raise
        except Exception as e:
            # Wrap all other exceptions in LLMError with provider context
            logger.error(f"[sdk1][LLM] Error during async completion: {e}")

            # Extract status code if available
            status_code = None
            if hasattr(e, "status_code"):
                status_code = e.status_code
            elif hasattr(e, "http_status"):
                status_code = e.http_status

            error_msg = (
                f"Error from LLM adapter '{self._get_adapter_info()}': "
                f"{strip_litellm_prefix(str(e))}"
            )

            raise LLMError(
                message=error_msg, status_code=status_code, actual_err=e
            ) from e

    @classmethod
    def get_context_window_size(
        cls, adapter_id: str, adapter_metadata: dict[str, object]
    ) -> int:
        """Returns the context window size of the LLM."""
        try:
            validated = adapters[adapter_id][Common.MODULE].validate(
                dict(adapter_metadata)
            )
            context_window = validated.get("context_window")
            if isinstance(context_window, int):
                return context_window
            model = cast("str", validated.get("cost_model") or validated["model"])
            model_info = litellm.get_model_info(model)
            context_window = model_info.get("max_input_tokens")
            if isinstance(context_window, int):
                return context_window
            fallback = get_max_tokens(model)
            if isinstance(fallback, int):
                return fallback
            raise ValueError(f"Context window is unavailable for model {model}.")
        except Exception as e:
            logger.warning(f"Failed to get context window size for {adapter_id}: {e}")
            return cls.MAX_TOKENS

    @classmethod
    def get_max_tokens(
        cls, adapter_instance_id: str, tool: BaseTool, reserved_for_output: int = 0
    ) -> int:
        """Returns the maximum number of tokens limit for the LLM."""
        try:
            llm_config = PlatformHelper.get_adapter_config(tool, adapter_instance_id)
            adapter_id = llm_config[Common.ADAPTER_ID]
            adapter_metadata = llm_config[Common.ADAPTER_METADATA]
            return (
                cls.get_context_window_size(adapter_id, adapter_metadata)
                - reserved_for_output
            )
        except Exception as e:
            logger.warning(
                f"Failed to get context window size for {adapter_instance_id}: {e}"
            )
            return cls.MAX_TOKENS - reserved_for_output

    def get_model_name(self) -> str:
        """Gets the name of the LLM model.

        Returns:
            LLM model name
        """
        return self.kwargs["model"]

    def get_metrics(self) -> dict[str, object]:
        return self._metrics

    def get_last_usage(self) -> Mapping[str, int]:
        """Token usage from the most recent LLM call (sync, async, or streaming)."""
        if not self._pending_usage:
            return {}
        last = self._pending_usage[-1]
        return {
            "prompt_tokens": last["prompt_tokens"],
            "completion_tokens": last["completion_tokens"],
            "total_tokens": last["total_tokens"],
        }

    def get_last_usage_record(self) -> dict | None:
        """Full usage record for the most recent LLM call.

        Returns tokens + cost + model + reason metadata; ``None`` if no
        call has been made yet.
        """
        if not self._pending_usage:
            return None
        return self._pending_usage[-1]

    def get_usage_reason(self) -> object:
        return self.platform_kwargs.get("llm_usage_reason")

    def flush_pending_usage(self) -> list[dict]:
        """Return and clear all pending usage records.

        Called at executor finalization.
        """
        records = self._pending_usage
        self._pending_usage = []
        return records

    def _compute_call_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        has_cache_tokens: bool,
        response: object | None,
    ) -> float:
        """Compute the dollar cost of a single call.

        When caching is active, cache-read tokens are billed at ~0.1x and
        cache-write at ~1.25x of the base input rate. ``litellm.cost_per_token``
        prices every prompt token at the full input rate, so it over-reports
        cost on cache hits. In that case let litellm read the cache token counts
        off the response for an accurate figure, falling back to the per-token
        path (which is exact when no caching is involved).
        """
        if has_cache_tokens and response is not None:
            try:
                # Pass ``model`` so cached calls price against the same model as
                # the ``cost_per_token`` fallback below. Without it,
                # ``completion_cost`` derives the model from the response and
                # ignores any ``cost_model`` override.
                return litellm.completion_cost(completion_response=response, model=model)
            except Exception:
                # Warn (not debug): the cost_per_token fallback prices every
                # prompt token at the full input rate, so it over-reports cost
                # by up to ~10x on cache hits. Operators watching spend need to
                # see that a recorded cost may be inflated.
                logger.warning(
                    "completion_cost() failed for model=%s; falling back to "
                    "cost_per_token — recorded cost may be OVER-reported for "
                    "this cached call",
                    model,
                    exc_info=True,
                )
        try:
            prompt_cost, compl_cost = litellm.cost_per_token(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            return prompt_cost + compl_cost
        except Exception:
            logger.warning(
                "Failed to compute cost for model=%s; recording as 0.0",
                model,
                exc_info=True,
            )
            return 0.0

    def _record_usage(
        self,
        model: str,
        messages: list[dict[str, str]],
        usage: Mapping[str, int] | None,
        llm_api: str,
        response: object | None = None,
    ) -> None:
        usage_data: Mapping[str, int] = usage or {}
        prompt_tokens = usage_data.get("prompt_tokens", 0)
        completion_tokens = usage_data.get("completion_tokens", 0)
        total_tokens = usage_data.get("total_tokens", 0)
        # Prompt-caching token counts (populated by Anthropic / Bedrock-Anthropic
        # when caching is enabled; 0 for every other provider/call).
        cache_creation_tokens = usage_data.get("cache_creation_input_tokens", 0) or 0
        cache_read_tokens = usage_data.get("cache_read_input_tokens", 0) or 0

        # Fall back to litellm when providers omit prompt tokens — avoids 0-token billing.
        if prompt_tokens == 0 and messages:
            try:
                prompt_tokens = litellm.token_counter(model=model, messages=messages)
                if total_tokens == 0:
                    total_tokens = prompt_tokens + completion_tokens
            except Exception:
                logger.warning(
                    "[sdk1][LLM][%s] prompt_tokens missing on response and "
                    "litellm.token_counter() fallback failed; recording 0",
                    model,
                    exc_info=True,
                )

        # Provider ids ride on the existing per-call usage line to aid
        # troubleshooting (shareable with the provider) without extra log noise.
        # Absent ids are omitted so providers without them don't add clutter.
        response_id, request_id = extract_provider_ids(response)
        id_suffix = ""
        if response_id is not None:
            id_suffix += f" response_id={response_id}"
        if request_id is not None:
            id_suffix += f" request_id={request_id}"
        cache_suffix = ""
        if cache_creation_tokens or cache_read_tokens:
            cache_suffix = (
                f" cache_write={cache_creation_tokens} cache_read={cache_read_tokens}"
            )
        logger.info(
            "[sdk1][LLM][%s][%s] Usage: prompt=%d completion=%d total=%d%s%s",
            model,
            llm_api,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cache_suffix,
            id_suffix,
        )

        cost = self._compute_call_cost(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            has_cache_tokens=bool(cache_creation_tokens or cache_read_tokens),
            response=response,
        )

        # Trailing segment matches legacy Audit semantics (e.g. bedrock/anthropic/claude).
        display_model = model.rsplit("/", 1)[-1] if model else model

        # Spread _usage_kwargs first so computed billing fields below win.
        self._pending_usage.append(
            {
                **self._usage_kwargs,
                "usage_type": "llm",
                "model_name": display_model,
                "provider": self.adapter.get_provider(),
                "adapter_instance_id": self.platform_kwargs.get(
                    "adapter_instance_id", ""
                ),
                # run_id lands in a UUIDField — "" fails the cast; keep None.
                "run_id": self.platform_kwargs.get("run_id") or None,
                "execution_id": self.platform_kwargs.get("execution_id", ""),
                # "" isn't a valid choice for llm_usage_reason.
                "llm_usage_reason": self.platform_kwargs.get("llm_usage_reason") or None,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "embedding_tokens": 0,
                "cost_in_dollars": cost,
                "status": "SUCCESS",
            }
        )

    # Finish reasons indicating a safety/policy refusal across providers:
    # - "refusal": Anthropic
    # - "content_filter": OpenAI / Azure OpenAI
    REFUSAL_FINISH_REASONS = {"refusal", "content_filter"}

    def _raise_for_empty_response(self, finish_reason: str | None) -> NoReturn:
        """Raise an appropriate error when the LLM response content is None.

        This typically happens when the LLM provider refuses to generate a
        response (e.g. Anthropic's safety filters, OpenAI's content filter)
        or returns an empty response.

        Args:
            finish_reason: The finish_reason from the LLM response.

        Raises:
            LLMError: With a descriptive message based on the finish_reason.
        """
        if finish_reason in self.REFUSAL_FINISH_REASONS:
            raise LLMError(
                message=(
                    "The LLM refused to generate a response due to safety "
                    f"restrictions (finish_reason: {finish_reason!r}). "
                    "Please review your prompt and try again."
                ),
                status_code=400,
            )
        raise LLMError(
            message=(
                f"The LLM returned an empty response "
                f"(finish_reason: {finish_reason}). This may indicate "
                f"the model could not generate content for the given prompt."
            ),
            status_code=500,
        )

    def _process_stream_chunk(
        self,
        chunk: dict[str, object],
        callback_manager: object | None,
        has_yielded_content: bool = False,
    ) -> LLMResponseCompat | None:
        """Process a single streaming chunk and return a response if content.

        Args:
            chunk: A streaming chunk from litellm.
            callback_manager: Optional callback manager for stream events.
            has_yielded_content: Whether any content has already been yielded.

        Returns:
            LLMResponseCompat with the text chunk, or None if no content.

        Raises:
            LLMError: If the chunk indicates a refusal and no content has
                been yielded yet. If content was already streamed, logs a
                warning instead to avoid confusing late errors.
        """
        if not chunk.get("choices"):
            return None

        finish_reason = chunk["choices"][0].get("finish_reason")
        if finish_reason in self.REFUSAL_FINISH_REASONS:
            if has_yielded_content:
                logger.warning(
                    "[sdk1][LLM] Provider sent refusal after content was "
                    "already streamed. Partial content may have been returned."
                )
                return None
            self._raise_for_empty_response(finish_reason)

        text = chunk["choices"][0].get("delta", {}).get("content", "")
        if not text:
            return None

        if callback_manager and hasattr(callback_manager, "on_stream"):
            callback_manager.on_stream(text)

        stream_response = LLMResponseCompat(text)
        stream_response.delta = text
        return stream_response

    def _post_process_response(
        self,
        response_text: str,
        extract_json: bool,
        post_process_fn: Callable[[LLMResponseCompat, bool, str], dict[str, object]]
        | None,
    ) -> tuple[str, dict[str, object]]:
        post_processed_output: dict[str, object] = {}

        # Save original text before any modifications
        original_text = response_text

        if extract_json:
            start = response_text.find(LLM.JSON_CONTENT_MARKER)
            if start != -1:
                response_text = response_text[
                    start + len(LLM.JSON_CONTENT_MARKER) :
                ].lstrip()
            end = response_text.rfind(LLM.JSON_CONTENT_MARKER)
            if end != -1:
                response_text = response_text[:end].rstrip()
            match = LLM.JSON_REGEX.search(response_text)
            if match:
                response_text = match.group(0)

        if post_process_fn:
            try:
                response_compat = LLMResponseCompat(response_text)
                post_processed_output = post_process_fn(
                    response_compat, extract_json, original_text
                )
                # Needed as the text is modified in place.
                response_text = response_compat.text
            except Exception as e:
                logger.error(
                    f"[sdk1][LLM][complete] Failed to post process response: {e}"
                )
                post_processed_output = {}

        return (response_text, post_processed_output)


class LLMCompat:
    """Compatibility wrapper that emulates the llama-index LLM interface.

    This class emulates ``llama_index.core.llms.llm.LLM`` without requiring
    the llama-index dependency. It allows llama-index components like
    SubQuestionQueryEngine, QueryFusionRetriever, and RouterQueryEngine
    to work with SDK1's LLM.

    Unlike :class:`EmbeddingCompat` (which inherits from llama-index's
    ``BaseEmbedding``), this class is a plain Python object with no
    llama-index inheritance. The prompt-service's ``RetrieverLLM``
    provides the llama-index base class and delegates to this wrapper.

    Prefer :meth:`from_llm` when an SDK1 ``LLM`` instance already
    exists — it reuses the instance directly, bypassing ``__init__``.
    """

    def __init__(
        self,
        adapter_id: str = "",
        adapter_metadata: dict[str, object] | None = None,
        adapter_instance_id: str = "",
        tool: BaseTool | None = None,
        usage_kwargs: dict[str, object] | None = None,
        system_prompt: str = "",
        kwargs: dict[str, object] | None = None,
        capture_metrics: bool = False,
    ) -> None:
        """Initialize the LLMCompat wrapper for compatibility.

        Args:
            adapter_id: Adapter identifier for LLM model
            adapter_metadata: Configuration metadata for the adapter
            adapter_instance_id: Instance identifier for the adapter
            tool: BaseTool instance for tool-specific operations
            usage_kwargs: Usage tracking parameters
            system_prompt: System prompt for the LLM
            kwargs: Additional keyword arguments for configuration
            capture_metrics: Whether to capture performance metrics
        """
        adapter_metadata = adapter_metadata or {}
        usage_kwargs = usage_kwargs or {}
        kwargs = kwargs or {}

        self._llm_instance = LLM(
            adapter_id=adapter_id,
            adapter_metadata=adapter_metadata,
            adapter_instance_id=adapter_instance_id,
            tool=tool,
            usage_kwargs=usage_kwargs,
            system_prompt=system_prompt,
            kwargs=kwargs,
            capture_metrics=capture_metrics,
        )
        self._tool = tool
        self._adapter_instance_id = adapter_instance_id

        # For compatibility with SDK Callback Manager.
        self.model_name = self._llm_instance.get_model_name()
        self.callback_manager = None

        if not PlatformHelper.is_public_adapter(adapter_id=adapter_instance_id):
            if self._tool:
                platform_api_key = self._tool.get_env_or_die(ToolEnv.PLATFORM_API_KEY)
            else:
                platform_api_key = os.environ.get(ToolEnv.PLATFORM_API_KEY, "")

            from unstract.sdk1.utils.callback_manager import CallbackManager

            CallbackManager.set_callback(
                platform_api_key=platform_api_key,
                model=self,
                kwargs={
                    **self._llm_instance.platform_kwargs,
                    "adapter_instance_id": adapter_instance_id,
                },
            )

    # ── Properties (llama-index interface) ───────────────────────────────────

    @property
    def metadata(self) -> LLMMetadata:
        """Return LLM metadata for llama-index compatibility."""
        return LLMMetadata(
            is_chat_model=True,
            model_name=self._llm_instance.get_model_name(),
        )

    # ── Sync methods (llama-index interface) ─────────────────────────────────
    # All LLM calls delegate to self._llm_instance (SDK1 LLM) so that
    # litellm invocation, error handling, and usage auditing stay in one
    # place.

    def chat(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,  # noqa: ANN401
    ) -> ChatResponse:
        """Synchronous chat completion.

        Extracts the last user message as the prompt and delegates to
        ``LLM.complete()``.
        """
        prompt = self._messages_to_prompt(messages)
        result = self._llm_instance.complete(prompt, **kwargs)
        resp = result["response"]
        return ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=resp.text),
            raw=resp.raw,
        )

    def complete(
        self,
        prompt: str,
        formatted: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> CompletionResponse:
        """Synchronous completion."""
        result = self._llm_instance.complete(prompt, **kwargs)
        resp = result["response"]
        return CompletionResponse(text=resp.text, raw=resp.raw)

    def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,  # noqa: ANN401
    ) -> Generator[ChatResponse, None, None]:
        """Streaming chat - not implemented."""
        raise NotImplementedError("stream_chat is not supported by LLMCompat.")

    def stream_complete(
        self,
        prompt: str,
        formatted: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> Generator[CompletionResponse, None, None]:
        """Streaming completion - not implemented."""
        raise NotImplementedError("stream_complete is not supported by LLMCompat.")

    # ── Async methods (llama-index interface) ────────────────────────────────

    async def achat(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,  # noqa: ANN401
    ) -> ChatResponse:
        """Asynchronous chat completion.

        Extracts the last user message as the prompt and delegates to
        ``LLM.acomplete()``.
        """
        prompt = self._messages_to_prompt(messages)
        result = await self._llm_instance.acomplete(prompt, **kwargs)
        resp = result["response"]
        return ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=resp.text),
            raw=resp.raw,
        )

    async def acomplete(
        self,
        prompt: str,
        formatted: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> CompletionResponse:
        """Asynchronous completion."""
        result = await self._llm_instance.acomplete(prompt, **kwargs)
        resp = result["response"]
        return CompletionResponse(text=resp.text, raw=resp.raw)

    async def astream_chat(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Async streaming chat - not implemented."""
        raise NotImplementedError("astream_chat is not supported by LLMCompat.")

    async def astream_complete(
        self,
        prompt: str,
        formatted: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Async streaming completion - not implemented."""
        raise NotImplementedError("astream_complete is not supported by LLMCompat.")

    # ── Helper methods ───────────────────────────────────────────────────────

    @staticmethod
    def _messages_to_prompt(messages: Sequence[ChatMessage]) -> str:
        """Flatten a message sequence into a single prompt string.

        Concatenates all messages with role prefixes so that
        system-level task instructions (e.g. from llama-index's
        ``LLMQuestionGenerator`` or ``KeywordTableIndex``) are
        preserved when forwarded to ``LLM.complete()``.
        """
        parts: list[str] = []
        for msg in messages:
            role = getattr(msg.role, "value", str(msg.role))
            content = msg.content or ""
            parts.append(f"{role}: {content}")
        return "\n".join(parts)

    # ── Factory methods ────────────────────────────────────────────────────

    @classmethod
    def from_llm(cls, llm: "LLM") -> "LLMCompat":
        """Create an LLMCompat instance reusing an existing SDK1 LLM.

        Reuses the already-initialised ``LLM`` object directly, avoiding
        redundant adapter validation and ``PlatformHelper`` calls that
        would occur if we re-created the instance from scratch.

        Args:
            llm: An SDK1 LLM instance.

        Returns:
            A new LLMCompat wrapping the same LLM instance.
        """
        instance = cls.__new__(cls)
        instance._llm_instance = llm
        instance._tool = llm._tool
        instance._adapter_instance_id = llm._adapter_instance_id

        # For compatibility with SDK Callback Manager.
        instance.model_name = llm.get_model_name()
        instance.callback_manager = None

        if not PlatformHelper.is_public_adapter(adapter_id=llm._adapter_instance_id):
            if llm._tool:
                platform_api_key = llm._tool.get_env_or_die(ToolEnv.PLATFORM_API_KEY)
            else:
                platform_api_key = os.environ.get(ToolEnv.PLATFORM_API_KEY, "")

            from unstract.sdk1.utils.callback_manager import CallbackManager

            CallbackManager.set_callback(
                platform_api_key=platform_api_key,
                model=instance,
                kwargs={
                    **llm.platform_kwargs,
                    "adapter_instance_id": llm._adapter_instance_id,
                },
            )

        return instance

    # ── SDK1 compatibility methods ───────────────────────────────────────────

    def get_model_name(self) -> str:
        """Gets the name of the LLM model."""
        return self._llm_instance.get_model_name()

    def get_metrics(self) -> dict[str, object]:
        """Get captured metrics."""
        return self._llm_instance.get_metrics()

    def get_last_usage(self) -> Mapping[str, int]:
        """Token usage from the most recent complete() call."""
        return self._llm_instance.get_last_usage()

    def get_usage_reason(self) -> object:
        """Get usage reason from platform kwargs."""
        return self._llm_instance.get_usage_reason()

    def flush_pending_usage(self) -> list[dict]:
        """Return and clear all pending usage records."""
        return self._llm_instance.flush_pending_usage()

    def test_connection(self) -> bool:
        """Test connection to the LLM provider."""
        return self._llm_instance.test_connection()
