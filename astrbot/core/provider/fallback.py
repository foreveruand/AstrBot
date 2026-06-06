from __future__ import annotations

from typing import Any

from astrbot import logger

from .provider import Provider


def get_fallback_chat_providers(
    provider: Provider,
    plugin_context: Any,
    provider_settings: dict[str, Any] | None,
) -> list[Provider]:
    """Resolve fallback chat providers for a provider selection."""
    if not isinstance(provider_settings, dict):
        logger.warning("provider_settings is not a dict, skip fallback providers.")
        return []

    fallback_ids = provider_settings.get("fallback_chat_models", [])
    if not isinstance(fallback_ids, list):
        logger.warning(
            "fallback_chat_models setting is not a list, skip fallback providers."
        )
        return []

    provider_id = str(provider.provider_config.get("id", ""))
    seen_provider_ids: set[str] = {provider_id} if provider_id else set()
    fallbacks: list[Provider] = []

    for fallback_id in fallback_ids:
        if not isinstance(fallback_id, str) or not fallback_id:
            continue
        if fallback_id in seen_provider_ids:
            continue
        fallback_provider = plugin_context.get_provider_by_id(fallback_id)
        if fallback_provider is None:
            logger.warning("Fallback chat provider `%s` not found, skip.", fallback_id)
            continue
        if not isinstance(fallback_provider, Provider):
            logger.warning(
                "Fallback chat provider `%s` is invalid type: %s, skip.",
                fallback_id,
                type(fallback_provider),
            )
            continue
        fallbacks.append(fallback_provider)
        seen_provider_ids.add(fallback_id)
    return fallbacks
