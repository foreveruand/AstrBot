import pytest

from astrbot.core.utils.llm_metadata import LLM_METADATAS, get_llm_metadata


@pytest.fixture(autouse=True)
def restore_llm_metadata():
    original_metadata = LLM_METADATAS.copy()
    yield
    LLM_METADATAS.clear()
    LLM_METADATAS.update(original_metadata)


def test_get_llm_metadata_matches_exact_model_id():
    metadata = {"id": "gpt-5.4"}

    LLM_METADATAS.clear()
    LLM_METADATAS["gpt-5.4"] = metadata  # type: ignore[assignment]

    assert get_llm_metadata("gpt-5.4") is metadata


def test_get_llm_metadata_matches_provider_prefixed_model_id():
    metadata = {"id": "gpt-5.4"}

    LLM_METADATAS.clear()
    LLM_METADATAS["gpt-5.4"] = metadata  # type: ignore[assignment]

    assert get_llm_metadata("team/gpt-5.4") is metadata


def test_get_llm_metadata_returns_none_for_unknown_model_id():
    LLM_METADATAS.clear()

    assert get_llm_metadata("team/unknown") is None
