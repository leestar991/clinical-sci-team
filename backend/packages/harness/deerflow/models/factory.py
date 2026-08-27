import logging

from langchain.chat_models import BaseChatModel

from deerflow.config import get_app_config
from deerflow.config.app_config import AppConfig
from deerflow.reflection import resolve_class
from deerflow.tracing import build_tracing_callbacks

logger = logging.getLogger(__name__)


def _deep_merge_dicts(base: dict | None, override: dict) -> dict:
    """Recursively merge two dictionaries without mutating the inputs."""
    merged = dict(base or {})
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _vllm_disable_chat_template_kwargs(chat_template_kwargs: dict) -> dict:
    """Build the disable payload for vLLM/Qwen chat template kwargs."""
    disable_kwargs: dict[str, bool] = {}
    if "thinking" in chat_template_kwargs:
        disable_kwargs["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable_kwargs["enable_thinking"] = False
    return disable_kwargs


def _enable_stream_usage_by_default(model_use_path: str, model_settings_from_config: dict) -> None:
    """Enable stream usage for OpenAI-compatible models unless explicitly configured.

    LangChain only auto-enables ``stream_usage`` for OpenAI models when no custom
    base URL or client is configured. DeerFlow frequently uses OpenAI-compatible
    gateways, so token usage tracking would otherwise stay empty and the
    TokenUsageMiddleware would have nothing to log.
    """
    if model_use_path != "langchain_openai:ChatOpenAI":
        return
    if "stream_usage" in model_settings_from_config:
        return
    if "base_url" in model_settings_from_config or "openai_api_base" in model_settings_from_config:
        model_settings_from_config["stream_usage"] = True


# Default chunk-gap budget for OpenAI-compatible streaming responses.
#
# langchain-openai raises ``StreamChunkTimeoutError`` after this many seconds
# without receiving a chunk. Its own default is 60s, which is too aggressive for
# reasoning models (DeepSeek-R1, Doubao-thinking, GPT-5) whose first chunk can
# legitimately take 90~150s. We default to 240s so the streaming layer rarely
# trips on long thinking pauses; the LLMErrorHandlingMiddleware still retries
# (budget=2) if a real stall happens. Users can override per-model in config.yaml.
_DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS: float = 240.0


# DeepSeek collapses several ``reasoning_effort`` values onto the same internal
# budget: ``medium`` / ``high`` / ``xhigh`` all mean ``high``
# (https://api-docs.deepseek.com/zh-cn/guides/thinking_mode). The frontend's four
# levels (minimal / low / medium / high) would therefore give the user only three
# distinct behaviours, with the UI's top two levels indistinguishable, while
# DeepSeek's genuinely deepest level (``max``) stayed unreachable. This table
# spreads the four UI levels across four values that DeepSeek actually
# differentiates.
#
# Note ``high -> max`` raises both latency and cost for the UI's top level; that
# is the intended trade-off for making "高" mean something stronger than "中".
_DEEPSEEK_EFFORT_REMAP: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "high",
    "high": "max",
}


def _is_deepseek_native_endpoint(model_settings_from_config: dict) -> bool:
    """Whether the model talks to DeepSeek's own API rather than a compatible gateway.

    ``PatchedChatDeepSeek`` is deliberately reused for many OpenAI-compatible
    providers (Doubao/Ark, Kimi, Novita, ...), so the provider class says nothing
    about which effort vocabulary the endpoint accepts — only DeepSeek's own API
    is known to take ``max``. Matching on the endpoint host keeps the remap from
    sending a value those gateways would reject.
    """
    for key in ("base_url", "api_base", "openai_api_base"):
        value = model_settings_from_config.get(key)
        if value and "api.deepseek.com" in str(value):
            return True
    return False


def _resolve_reasoning_effort(model_settings_from_config: dict, kwargs: dict) -> None:
    """Collapse the runtime and config ``reasoning_effort`` into a single value.

    Two independent sources can supply ``reasoning_effort``: the caller's kwargs
    (the frontend's reasoning-depth selection, threaded through
    ``make_lead_agent``) and ``model_settings_from_config`` (either an explicit
    ``config.yaml`` field or the ``minimal`` written by the thinking-disabled
    branch above). Leaving both in place makes the final
    ``model_class(**kwargs, **model_settings_from_config)`` raise
    ``TypeError: got multiple values for keyword argument 'reasoning_effort'``.

    The runtime value wins because it carries the user's per-request choice, but
    an explicit ``None`` is dropped rather than allowed to erase a configured
    value — ``make_lead_agent`` passes ``None`` whenever the run context omits
    the key (IM channels never set it).
    """
    if "reasoning_effort" not in kwargs:
        return
    explicit = kwargs.pop("reasoning_effort")
    if explicit is not None:
        model_settings_from_config["reasoning_effort"] = explicit


def _apply_stream_chunk_timeout_default(model_use_path: str, model_settings_from_config: dict) -> None:
    """Inject a generous ``stream_chunk_timeout`` for OpenAI-compatible clients.

    The ``stream_chunk_timeout`` kwarg is specific to ``langchain_openai:ChatOpenAI``
    and is rejected by other providers' constructors as an unexpected keyword
    argument. Behaviour:

    * OpenAI-compatible path: an explicit value in ``config.yaml`` is preserved.
      An explicit ``null`` is dropped upstream by ``model_dump(exclude_none=True)``
      and therefore treated as "unset", so the default is injected.
    * Non-OpenAI path: drop the key so it is never forwarded to an incompatible
      constructor (which would raise ``TypeError: unexpected keyword argument``).
    """
    if model_use_path != "langchain_openai:ChatOpenAI":
        model_settings_from_config.pop("stream_chunk_timeout", None)
        return
    if "stream_chunk_timeout" in model_settings_from_config:
        return
    model_settings_from_config["stream_chunk_timeout"] = _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS


def create_chat_model(name: str | None = None, thinking_enabled: bool = False, *, app_config: AppConfig | None = None, attach_tracing: bool = True, **kwargs) -> BaseChatModel:
    """Create a chat model instance from the config.

    Args:
        name: The name of the model to create. If None, the first model in the config will be used.
        thinking_enabled: Enable the model's extended-thinking mode when supported.
        app_config: Explicit application config; falls back to the cached global if omitted.
        attach_tracing: When True (default), attach tracing callbacks (Langfuse,
            LangSmith) directly to the model instance. Standalone callers — anything
            that invokes the model outside a LangGraph run that already wires tracing
            at the invocation root (``MemoryUpdater``, ad-hoc utilities, etc.) — keep
            this default so the model-level callback still produces traces. Callers
            that already attach tracing at the graph root (``make_lead_agent``, the
            in-graph ``TitleMiddleware``) MUST pass ``attach_tracing=False``; otherwise
            the same LLM call emits duplicate spans (one rooted at the graph, one at
            the model) and ``session_id`` / ``user_id`` metadata never reach the trace
            because the model becomes a nested observation whose ``langfuse_*`` keys
            get stripped.

    Returns:
        A chat model instance.
    """
    config = app_config or get_app_config()
    if name is None:
        name = config.models[0].name
    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config") from None
    model_class = resolve_class(model_config.use, BaseChatModel)
    model_settings_from_config = model_config.model_dump(
        exclude_none=True,
        exclude={
            "use",
            "name",
            "display_name",
            "description",
            "supports_thinking",
            "supports_reasoning_effort",
            "when_thinking_enabled",
            "when_thinking_disabled",
            "thinking",
            "supports_vision",
        },
    )
    # Compute effective when_thinking_enabled by merging in the `thinking` shortcut field.
    # The `thinking` shortcut is equivalent to setting when_thinking_enabled["thinking"].
    has_thinking_settings = (model_config.when_thinking_enabled is not None) or (model_config.thinking is not None)
    effective_wte: dict = dict(model_config.when_thinking_enabled) if model_config.when_thinking_enabled else {}
    if model_config.thinking is not None:
        merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
        effective_wte = {**effective_wte, "thinking": merged_thinking}
    if thinking_enabled and has_thinking_settings:
        if not model_config.supports_thinking:
            raise ValueError(f"Model {name} does not support thinking. Set `supports_thinking` to true in the `config.yaml` to enable thinking.") from None
        if effective_wte:
            model_settings_from_config.update(effective_wte)
    if not thinking_enabled:
        if model_config.when_thinking_disabled is not None:
            # User-provided disable settings take full precedence
            model_settings_from_config.update(model_config.when_thinking_disabled)
        elif has_thinking_settings and effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            # OpenAI-compatible gateway: thinking is nested under extra_body
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"thinking": {"type": "disabled"}},
            )
            model_settings_from_config["reasoning_effort"] = "minimal"
        elif has_thinking_settings and (disable_chat_template_kwargs := _vllm_disable_chat_template_kwargs(effective_wte.get("extra_body", {}).get("chat_template_kwargs") or {})):
            # vLLM uses chat template kwargs to switch thinking on/off.
            model_settings_from_config["extra_body"] = _deep_merge_dicts(
                model_settings_from_config.get("extra_body"),
                {"chat_template_kwargs": disable_chat_template_kwargs},
            )
        elif has_thinking_settings and effective_wte.get("thinking", {}).get("type"):
            # Native langchain_anthropic: thinking is a direct constructor parameter
            model_settings_from_config["thinking"] = {"type": "disabled"}
        elif not has_thinking_settings:
            # Every branch above needs the model to have declared *some* thinking setting,
            # so a model that declares none silently ignores `thinking_enabled=False` and
            # reasons at whatever the provider defaults to. That silence is what let the
            # bug in session ``a7c19ea1`` survive: ``deepseek-v4-flash`` is the summariser
            # (``summarization.model_name``), the middleware had always passed
            # ``thinking_enabled=False``, and reasoning still consumed the model's entire
            # ``max_tokens: 8192`` — 16 of 54 compaction attempts (30%) returned an empty
            # summary, every one ``finish_reason='length'`` with 13k–25k reasoning chars
            # and no body. Nothing in the logs pointed at the model config.
            # Warn rather than raise: a model with no thinking settings may simply not
            # support thinking, which is a legitimate configuration.
            logger.warning(
                "Model %s: thinking was requested OFF but the model declares no thinking settings "
                "(when_thinking_disabled / when_thinking_enabled / thinking), so the request is a "
                "no-op and reasoning stays uncapped. If this model reasons, add `when_thinking_disabled` "
                "in config.yaml — an uncapped reasoning budget can consume all of max_tokens (%s) and "
                "return an empty body.",
                name,
                model_settings_from_config.get("max_tokens", "unset"),
            )
    if not model_config.supports_reasoning_effort:
        kwargs.pop("reasoning_effort", None)
        model_settings_from_config.pop("reasoning_effort", None)
    else:
        # Must run *after* the thinking-disabled branch above, so the ``minimal``
        # that branch injects is already in ``model_settings_from_config`` and can
        # be overridden by an explicit runtime value.
        _resolve_reasoning_effort(model_settings_from_config, kwargs)
        if _is_deepseek_native_endpoint(model_settings_from_config):
            remapped = _DEEPSEEK_EFFORT_REMAP.get(model_settings_from_config.get("reasoning_effort", ""))
            if remapped is not None:
                model_settings_from_config["reasoning_effort"] = remapped

    _enable_stream_usage_by_default(model_config.use, model_settings_from_config)
    _apply_stream_chunk_timeout_default(model_config.use, model_settings_from_config)

    # For Codex Responses API models: map thinking mode to reasoning_effort
    from deerflow.models.openai_codex_provider import CodexChatModel

    if issubclass(model_class, CodexChatModel):
        # The ChatGPT Codex endpoint currently rejects max_tokens/max_output_tokens.
        model_settings_from_config.pop("max_tokens", None)

        # ``_resolve_reasoning_effort`` has already folded any runtime kwarg into
        # ``model_settings_from_config``, so the effective value is read from
        # there rather than from kwargs.
        effective_effort = model_settings_from_config.get("reasoning_effort")
        if not thinking_enabled:
            model_settings_from_config["reasoning_effort"] = "none"
        elif effective_effort not in ("low", "medium", "high", "xhigh"):
            # The Codex endpoint's vocabulary does not include ``minimal``, so
            # anything outside the supported set — including an unset value —
            # falls back to the default rather than being forwarded verbatim.
            model_settings_from_config["reasoning_effort"] = "medium"

    # For MindIE models: enforce conservative retry defaults.
    # Timeout normalization is handled inside MindIEChatModel itself.
    if getattr(model_class, "__name__", "") == "MindIEChatModel":
        # Enforce max_retries constraint to prevent cascading timeouts.
        model_settings_from_config["max_retries"] = model_settings_from_config.get("max_retries", 1)

    # Ensure stream_usage is enabled so that token usage metadata is available
    # in streaming responses.  LangChain's BaseChatOpenAI only defaults
    # stream_usage=True when no custom base_url/api_base is set, so models
    # hitting third-party endpoints (e.g. doubao, deepseek) silently lose
    # usage data.  We default it to True unless explicitly configured.
    if "stream_usage" not in model_settings_from_config and "stream_usage" not in kwargs:
        if "stream_usage" in getattr(model_class, "model_fields", {}):
            model_settings_from_config["stream_usage"] = True

    model_instance = model_class(**kwargs, **model_settings_from_config)

    if attach_tracing:
        callbacks = build_tracing_callbacks()
        if callbacks:
            existing_callbacks = model_instance.callbacks or []
            model_instance.callbacks = [*existing_callbacks, *callbacks]
            logger.debug(f"Tracing attached to model '{name}' with providers={len(callbacks)}")
    return model_instance
