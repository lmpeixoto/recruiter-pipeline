"""Testes para o tracking de chamadas/tokens do LLMClient."""

from types import SimpleNamespace

from recruiter_pipeline.llm import LLMClient
from recruiter_pipeline.models import Classification


def _fake_response(content, total_tokens=10):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(total_tokens=total_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


class TestCallTracking:
    def test_tracks_call_count_and_tokens(self, monkeypatch):
        client = LLMClient(api_key="k")
        monkeypatch.setattr(
            client.client.chat.completions,
            "create",
            lambda **kwargs: _fake_response(
                '{"is_opportunity": true, "confidence": 0.9, "reason": "x"}',
                total_tokens=42,
            ),
        )
        client.complete(
            system_prompt="s", user_prompt="u", response_model=Classification
        )
        assert client.call_count == 1
        assert client.total_tokens == 42

    def test_accumulates_across_calls(self, monkeypatch):
        client = LLMClient(api_key="k")
        monkeypatch.setattr(
            client.client.chat.completions,
            "create",
            lambda **kwargs: _fake_response(
                '{"is_opportunity": false, "confidence": 0.1, "reason": "x"}',
                total_tokens=5,
            ),
        )
        client.complete(
            system_prompt="s", user_prompt="u", response_model=Classification
        )
        client.complete(
            system_prompt="s", user_prompt="u", response_model=Classification
        )
        assert client.call_count == 2
        assert client.total_tokens == 10

    def test_counts_failed_attempts_too(self, monkeypatch):
        """Um attempt que falha a validação já gastou tokens -- conta."""
        client = LLMClient(api_key="k")
        monkeypatch.setattr(
            client.client.chat.completions,
            "create",
            lambda **kwargs: _fake_response("not json", total_tokens=7),
        )
        try:
            client.complete(
                system_prompt="s",
                user_prompt="u",
                response_model=Classification,
                max_retries=0,
            )
        except RuntimeError:
            pass
        assert client.call_count == 1
        assert client.total_tokens == 7


def test_is_fatal_api_error():
    from recruiter_pipeline.jev_client import JevError
    from recruiter_pipeline.llm import is_fatal_api_error

    class Err(Exception):
        status_code = 402

    assert is_fatal_api_error(Err("x"))
    assert is_fatal_api_error(JevError("/api/alpha/decisions devolveu 401: bad key"))
    assert not is_fatal_api_error(RuntimeError("timeout"))
