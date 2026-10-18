"""Testes para o JevClient (/api/alpha/decisions), sem chamadas reais à API."""

import httpx
import pytest

from recruiter_pipeline.jev_client import DECISIONS_URL, JevClient, JevError


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class TestDecide:
    def test_posts_model_state_and_questions(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResponse(
                200,
                {
                    "model": "typesafe/jev-1.13",
                    "answers": {"is_opportunity": {"type": "noul", "noul": 0.9}},
                    "usage": {"cost": 0.001},
                },
            )

        monkeypatch.setattr(httpx, "post", fake_post)
        client = JevClient(api_key="k", model="typesafe/jev-1.13")
        answers = client.decide(
            state={"message": "hello"},
            questions={"is_opportunity": JevClient.noul("is this a job?")},
        )

        assert captured["url"] == DECISIONS_URL
        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["json"]["model"] == "typesafe/jev-1.13"
        assert captured["json"]["state"] == {"message": "hello"}
        assert answers["is_opportunity"]["noul"] == 0.9

    def test_tracks_cost_and_call_count(self, monkeypatch):
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: _FakeResponse(
                200, {"answers": {"q": {"noul": 0.5}}, "usage": {"cost": 0.002}}
            ),
        )
        client = JevClient(api_key="k")
        client.decide(state={}, questions={"q": JevClient.noul("x")})
        client.decide(state={}, questions={"q": JevClient.noul("x")})
        assert client.call_count == 2
        assert client.total_cost == pytest.approx(0.004)

    def test_non_200_raises_jev_error(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "post", lambda *a, **k: _FakeResponse(400, {"error": "bad model"})
        )
        client = JevClient(api_key="k")
        with pytest.raises(JevError):
            client.decide(state={}, questions={})

    def test_missing_answers_raises_jev_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, {}))
        client = JevClient(api_key="k")
        with pytest.raises(JevError):
            client.decide(state={}, questions={})


class TestQuestionBuilders:
    def test_noul_shape(self):
        assert JevClient.noul("x?") == {"type": "noul", "instructions": "x?"}

    def test_choice_shape(self):
        q = JevClient.choice("pick one", {"a": "option a", "b": "option b"})
        assert q == {
            "type": "choice",
            "instructions": "pick one",
            "criteria": {"a": "option a", "b": "option b"},
        }

    def test_score_shape(self):
        q = JevClient.score("rate it", ["low", "medium", "high"])
        assert q == {
            "type": "score",
            "instructions": "rate it",
            "criteria": ["low", "medium", "high"],
        }
