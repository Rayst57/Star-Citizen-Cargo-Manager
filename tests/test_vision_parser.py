"""Tests for src.vision_parser — the OpenAI vision contract extractor.

All tests mock the openai client so no real network calls are made.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.vision_parser import parse_contract_from_image


def _mock_openai_response(content: str) -> MagicMock:
    """Build a fake OpenAI ChatCompletion response with the given
    message content (the JSON string the model would have produced)."""
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def test_parse_response_handles_valid_json():
    """A well-formed JSON response should be parsed into the dict
    AddContractDialog expects."""
    valid_contract = {
        "pickup_station": "Yellow Core",
        "max_pallet_size": 8,
        "deliveries": [
            {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55},
            {"destination": "Baijini Point", "commodity": "Tungsten", "scu": 27},
        ],
    }
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps(valid_contract)
    )

    with patch.dict(
        "sys.modules",
        {"openai": SimpleNamespace(OpenAI=lambda **kw: fake_client)},
    ):
        result = parse_contract_from_image(b"fake png bytes", "sk-test")

    assert result["pickup_station"] == "Yellow Core"
    assert result["max_pallet_size"] == 8
    assert len(result["deliveries"]) == 2
    assert result["deliveries"][0]["destination"] == "Everus Harbor"
    assert result["deliveries"][0]["scu"] == 55


def test_parse_response_handles_error_response():
    """When the model returns {"error": ...} (e.g. no contract visible)
    the parser raises RuntimeError so the dialog can show a warning."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps({"error": "No contract data visible in screenshot"})
    )

    with patch.dict(
        "sys.modules",
        {"openai": SimpleNamespace(OpenAI=lambda **kw: fake_client)},
    ):
        with pytest.raises(RuntimeError) as exc_info:
            parse_contract_from_image(b"fake png bytes", "sk-test")
    assert "No contract data visible" in str(exc_info.value)


def test_parse_response_handles_no_api_key():
    """An empty api_key short-circuits with a clear error message —
    we never even try to hit the API."""
    with pytest.raises(RuntimeError) as exc_info:
        parse_contract_from_image(b"fake png bytes", "")
    assert "API key" in str(exc_info.value)
