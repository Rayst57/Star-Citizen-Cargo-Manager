"""
Free-text → contract parsing.

The user dictates contracts elsewhere (Windows Voice Typing, Notepad,
phone, etc.), copies the text, and pastes it into the Paste & Parse
dialog. This module turns the dictation into a list of `add_contract`
argument dicts using OpenAI's chat-completions tool-calling, biased
with the workday's known stations and commodities so it doesn't
invent destinations.

The output is reviewed in the dialog before any contracts are added —
this module only PARSES; it never mutates state.

Pure function: takes raw values (api_key, model, station/commodity
name lists, and the text) so it is safe to call from a worker thread
without crossing SQLite connections.
"""

from __future__ import annotations

import json

from .tool_schemas import GIANT_TOOLS


PARSE_SYSTEM_PROMPT = """\
You are a contract-parsing assistant for a Star Citizen cargo planner.
Read the user's free-form dictation and extract EVERY contract it
mentions. Call the add_contract tool ONCE PER CONTRACT — multiple
parallel tool calls are expected when the dictation describes multiple.

A single contract has ONE pickup station and one or more deliveries
from that pickup. If the user mentions a different pickup, that's a
new contract.

KNOWN STATIONS (only use these names — match aliases to the canonical
form, never invent):
{stations}

KNOWN COMMODITIES (use canonical names; aliases like "ice" → "Pressurized
Ice", "tungsten ore" → "Tungsten" are fine):
{commodities}

VALID PALLET SIZES: 1, 2, 4, 8, 16, 24, 32. Default max_pallet_size to 8
if not stated. If the user says "max pallet size N" once, apply N to
every contract that follows in the same dictation unless overridden.

RULES
- Be conservative — when a phrase is ambiguous, pick the closest match
  from the lists. If you genuinely cannot identify a station or
  commodity, SKIP that contract; do not invent.
- SCU amounts are integers > 0.
- One pickup + multiple destinations in the SAME utterance = one
  contract with multiple deliveries.
- Two different pickups = two separate contracts.
- Ignore non-contract chatter (filler words, greetings, etc.).
"""


def parse_contracts_text(
    api_key: str,
    model: str,
    stations: list[str],
    commodities: list[str],
    text: str,
) -> list[dict]:
    """Parse free-form dictation into a list of add_contract argument dicts.

    Args:
        api_key:     OpenAI API key.
        model:       OpenAI chat model (e.g. "gpt-4o").
        stations:    Canonical station names available in the DB.
        commodities: Canonical commodity names available in the DB.
        text:        Free-form dictation to parse.

    Returns:
        A (possibly empty) list of dicts shaped like add_contract args:
        {pickup_station, max_pallet_size, deliveries: [{destination,
        commodity, scu}, ...]}.

    Raises:
        RuntimeError: if the OpenAI client can't be imported.
        Any exception from the OpenAI client is propagated to the caller.
    """
    text = text.strip()
    if not text:
        return []

    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(f"openai package not installed: {e}") from e

    system = PARSE_SYSTEM_PROMPT.format(
        stations="\n".join(f"  - {s}" for s in stations),
        commodities="\n".join(f"  - {c}" for c in commodities),
    )

    add_contract_tool = next(
        t for t in GIANT_TOOLS
        if t["function"]["name"] == "add_contract"
    )

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        tools=[add_contract_tool],
        tool_choice="required",
        temperature=0.0,
    )

    msg = resp.choices[0].message
    if not msg.tool_calls:
        return []

    contracts: list[dict] = []
    for tc in msg.tool_calls:
        if tc.function.name != "add_contract":
            continue
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            continue
        if "pickup_station" in args and args.get("deliveries"):
            contracts.append(args)
    return contracts
