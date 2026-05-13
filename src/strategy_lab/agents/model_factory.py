from __future__ import annotations

from typing import Any


def apply_context_profile(
    model: Any,
    *,
    provider: str,
    model_name: str | None = None,
    context_windows: dict[str, Any] | None = None,
    override: dict[str, Any] | None = None,
) -> Any:
    """Apply a conservative context profile for DeepAgents summarization."""

    max_input_tokens = _resolve_effective_max_input_tokens(
        provider=provider,
        model_name=model_name,
        context_windows=context_windows or {},
        override=override or {},
    )
    if max_input_tokens is None:
        return model

    current_profile = getattr(model, "profile", None)
    profile = dict(current_profile) if isinstance(current_profile, dict) else {}
    profile["max_input_tokens"] = max_input_tokens
    object.__setattr__(model, "profile", profile)
    return model


def _resolve_effective_max_input_tokens(
    *,
    provider: str,
    model_name: str | None,
    context_windows: dict[str, Any],
    override: dict[str, Any],
) -> int | None:
    explicit = _optional_int(override.get("effective_max_input_tokens"))
    if explicit:
        return explicit

    key = _provider_context_key(provider=provider, model_name=model_name)
    window_cfg = context_windows.get(key) or {}
    value = _optional_int(window_cfg.get("effective_max_input_tokens"))
    return value


def _provider_context_key(*, provider: str, model_name: str | None) -> str:
    provider_key = (provider or "").lower()
    model_key = (model_name or "").lower()
    if provider_key in {"moonshot", "kimi"} or "kimi" in model_key:
        return "kimi"
    if "deepseek" in provider_key or "deepseek" in model_key:
        return "deepseek"
    return provider_key


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class ReasoningContentChatOpenAI:
    """创建可保留 reasoning_content 的 OpenAI-compatible LangChain 模型。

    Kimi / DeepSeek 的思考模式在工具调用后都要求后续请求继续携带
    assistant message 中的 reasoning_content。LangChain 通用封装并不总是
    稳定透传该字段，因此这里统一补齐。
    """

    @staticmethod
    def create_openai_compatible(**kwargs: Any):
        from langchain_core.messages import AIMessage
        from langchain_core.messages.utils import convert_to_messages
        from langchain_openai import ChatOpenAI

        token_param = kwargs.pop("payload_token_param", None)

        class _ChatOpenAIWithReasoning(ChatOpenAI):
            def _get_request_payload(self, input_: Any, *args: Any, **inner_kwargs: Any) -> dict[str, Any]:
                payload = super()._get_request_payload(input_, *args, **inner_kwargs)
                if token_param == "max_tokens" and "max_completion_tokens" in payload:
                    payload["max_tokens"] = payload.pop("max_completion_tokens")
                _copy_reasoning_content(input_, payload)
                return payload

            def _create_chat_result(self, response: Any, generation_info: dict[str, Any] | None = None):
                result = super()._create_chat_result(response, generation_info=generation_info)
                response_dict = response if isinstance(response, dict) else response.model_dump()
                choices = response_dict.get("choices") or []
                for generation, choice in zip(result.generations, choices, strict=False):
                    message = choice.get("message") or {}
                    reasoning_content = message.get("reasoning_content")
                    if reasoning_content is not None:
                        generation.message.additional_kwargs["reasoning_content"] = reasoning_content
                return result

        return _ChatOpenAIWithReasoning(**kwargs)

    @staticmethod
    def create_deepseek(**kwargs: Any):
        from langchain_deepseek import ChatDeepSeek

        class _ChatDeepSeekWithReasoning(ChatDeepSeek):
            def _get_request_payload(self, input_: Any, *args: Any, **inner_kwargs: Any) -> dict[str, Any]:
                payload = super()._get_request_payload(input_, *args, **inner_kwargs)
                _copy_reasoning_content(input_, payload)
                return payload

        return _ChatDeepSeekWithReasoning(**kwargs)


def _copy_reasoning_content(input_: Any, payload: dict[str, Any]) -> None:
    from langchain_core.messages import AIMessage
    from langchain_core.messages.utils import convert_to_messages

    try:
        source_messages = convert_to_messages(input_)
    except Exception:
        return
    for source, outgoing in zip(source_messages, payload.get("messages", []), strict=False):
        if outgoing.get("role") != "assistant" or not isinstance(source, AIMessage):
            continue
        reasoning_content = source.additional_kwargs.get("reasoning_content")
        if reasoning_content is not None:
            outgoing["reasoning_content"] = reasoning_content
