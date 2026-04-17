from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from astrbot.core.provider.provider import Provider
from astrbot.core.provider.fallback import get_fallback_chat_providers


def test_get_fallback_chat_providers_filters_invalid_entries_and_duplicates():
    provider = MagicMock(spec=Provider)
    provider.provider_config = {"id": "main-provider"}
    fallback_a = MagicMock(spec=Provider)
    fallback_a.provider_config = {"id": "fallback-a"}
    fallback_b = MagicMock(spec=Provider)
    fallback_b.provider_config = {"id": "fallback-b"}

    def _get_provider_by_id(provider_id: str):
        mapping = {
            "main-provider": provider,
            "fallback-a": fallback_a,
            "fallback-b": fallback_b,
            "missing": None,
        }
        return mapping.get(provider_id)

    plugin_context = SimpleNamespace(get_provider_by_id=_get_provider_by_id)

    with patch("astrbot.core.provider.fallback.logger") as mock_logger:
        result = get_fallback_chat_providers(
            provider,
            plugin_context,
            {
                "fallback_chat_models": [
                    "main-provider",
                    "fallback-a",
                    "missing",
                    "fallback-b",
                    "fallback-a",
                    123,
                    "",
                ]
            },
        )

    assert result == [fallback_a, fallback_b]
    mock_logger.warning.assert_called_once_with(
        "Fallback chat provider `%s` not found, skip.",
        "missing",
    )


def test_get_fallback_chat_providers_rejects_non_list_config():
    provider = MagicMock(spec=Provider)
    provider.provider_config = {"id": "main-provider"}
    plugin_context = SimpleNamespace(get_provider_by_id=lambda _provider_id: None)

    with patch("astrbot.core.provider.fallback.logger") as mock_logger:
        result = get_fallback_chat_providers(
            provider,
            plugin_context,
            {"fallback_chat_models": "not-a-list"},
        )

    assert result == []
    mock_logger.warning.assert_called_once_with(
        "fallback_chat_models setting is not a list, skip fallback providers."
    )


def test_get_fallback_chat_providers_rejects_missing_provider_settings():
    provider = MagicMock(spec=Provider)
    provider.provider_config = {"id": "main-provider"}
    plugin_context = SimpleNamespace(get_provider_by_id=lambda _provider_id: None)

    with patch("astrbot.core.provider.fallback.logger") as mock_logger:
        result = get_fallback_chat_providers(provider, plugin_context, None)

    assert result == []
    mock_logger.warning.assert_called_once_with(
        "provider_settings is not a dict, skip fallback providers."
    )
