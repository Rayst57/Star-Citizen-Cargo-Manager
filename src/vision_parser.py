"""
Screenshot → contract parsing via OpenAI's GPT-4o vision API.

The user grabs a region of their Star Citizen window (or any window /
monitor), this module sends it to the vision model with a structured
prompt, and the model returns a JSON dict matching the
AddContractDialog's expected shape.

Pure function: takes a raw PNG byte string and api_key, returns a dict
or raises RuntimeError. Safe to call from a worker thread.
"""

from __future__ import annotations

import base64
import json


VISION_SYSTEM_PROMPT = """\
You are a contract-parsing assistant for a Star Citizen cargo planner.

The user will send you a screenshot from Star Citizen — typically the
in-game contract / mission UI. Extract the contract data and return it
as a JSON object.

OUTPUT SCHEMA (return ONLY this JSON object, no markdown fences, no
commentary):

{
  "pickup_station": "<canonical or alias station name>",
  "max_pallet_size": <one of 1, 2, 4, 8, 16, 24, 32>,
  "deliveries": [
    {"destination": "<station name>", "commodity": "<commodity name>", "scu": <integer>},
    ...
  ],
  "pickup_candidates": ["<alt pickup 1>", "<alt pickup 2>", ...]
}

RULES
- A single contract has ONE pickup station and one or more deliveries.
- If the contract lists multiple possible pickup stations (the "cargo
  might be at any of these" variant), put the primary in
  "pickup_station" and the rest in "pickup_candidates". Omit
  "pickup_candidates" entirely when there's only one pickup.
- If max pallet size isn't visible, default to 8.
- SCU amounts are positive integers.
- Use canonical station names where possible (e.g. "Everus Harbor",
  "Baijini Point", "Port Tressler", "CRU-L1 Ambitious Dream Station").
- If the screenshot doesn't look like a contract / mission UI at all,
  return: {"error": "No contract data visible in screenshot"}
"""


def parse_contract_from_image(image_bytes: bytes, api_key: str) -> dict:
    """Send a PNG image to gpt-4o vision and extract a contract dict.

    Args:
        image_bytes: Raw PNG bytes of the screenshot.
        api_key:     OpenAI API key.

    Returns:
        A dict matching the shape AddContractDialog expects:
            {
                "pickup_station": str,
                "max_pallet_size": int,
                "deliveries": [
                    {"destination": str, "commodity": str, "scu": int},
                    ...
                ],
                # optional:
                "pickup_candidates": [str, ...],
            }

    Raises:
        RuntimeError: if api_key is empty, the openai package is not
            installed, the API call fails, the response can't be
            parsed as JSON, or the model returned an explicit error.
    """
    if not api_key:
        raise RuntimeError(
            "OpenAI API key not set. Open Settings → OpenAI to add one."
        )
    if not image_bytes:
        raise RuntimeError("No image data to parse.")

    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(f"openai package not installed: {e}") from e

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Extract the contract data from this "
                                "screenshot and return the JSON object."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI vision request failed: {e}") from e

    try:
        content = resp.choices[0].message.content or ""
    except (AttributeError, IndexError) as e:
        raise RuntimeError(f"Malformed OpenAI response: {e}") from e

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"OpenAI response was not valid JSON: {e}; content was: {content!r}"
        ) from e

    if not isinstance(data, dict):
        raise RuntimeError(
            f"OpenAI response was not a JSON object: {content!r}"
        )

    if "error" in data:
        raise RuntimeError(str(data["error"]))

    # Light schema sanity — surface clear errors instead of letting the
    # AddContractDialog blow up on missing keys.
    if "pickup_station" not in data or "deliveries" not in data:
        raise RuntimeError(
            "Vision response missing required keys "
            "(pickup_station, deliveries): " + repr(data)
        )

    # Normalise: ensure deliveries is a list of dicts with the right keys.
    deliveries = data.get("deliveries") or []
    if not isinstance(deliveries, list):
        raise RuntimeError(
            f"Vision response 'deliveries' is not a list: {deliveries!r}"
        )
    data["deliveries"] = deliveries

    # Default pallet size when omitted.
    if "max_pallet_size" not in data or not data["max_pallet_size"]:
        data["max_pallet_size"] = 8

    return data
