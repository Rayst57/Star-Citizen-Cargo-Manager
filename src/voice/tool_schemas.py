"""
OpenAI function-calling tool schemas for the Giant Loadmaster.

Mirrors docs/llm_tools.md.  Imported by LLMThread when building the
chat completion request.
"""

from __future__ import annotations


def _fn(name: str, description: str, parameters: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


GIANT_TOOLS: list[dict] = [
    # ── Workday ─────────────────────────────────────────────────────
    _fn(
        "start_workday",
        "Start a new workday. Clears all contracts, route, and zone "
        "assignments from any previous workday.",
        {
            "type": "object",
            "properties": {
                "origin_station": {
                    "type": "string",
                    "description": "Station name where the ship is currently located.",
                },
                "final_destination": {
                    "type": "string",
                    "description": "Optional last stop of the day.",
                },
                "round_robin": {
                    "type": "boolean",
                    "description": "If true, return to origin after the last delivery.",
                },
            },
            "required": ["origin_station"],
        },
    ),
    _fn("resume_workday", "Resume the most recent open workday."),
    _fn("end_workday", "Close the active workday."),
    _fn(
        "set_origin",
        "Change the departure origin for the active workday.",
        {
            "type": "object",
            "properties": {"station_name": {"type": "string"}},
            "required": ["station_name"],
        },
    ),
    _fn(
        "set_final_destination",
        "Set or change the final destination for the active workday.",
        {
            "type": "object",
            "properties": {
                "station_name": {
                    "type": "string",
                    "description": "Pass null or omit to clear the final destination.",
                }
            },
        },
    ),
    _fn(
        "set_round_robin",
        "Enable or disable round-robin (return to origin after final delivery).",
        {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    ),
    _fn(
        "recompute_plan",
        "Recompute the route, zone assignments, and conflict analysis. "
        "Clears the plan_dirty flag.",
    ),

    # ── Contracts ───────────────────────────────────────────────────
    _fn(
        "add_contract",
        "Add a new cargo contract to the active workday. A contract has one "
        "pickup station and one or more deliveries.",
        {
            "type": "object",
            "properties": {
                "pickup_station": {"type": "string"},
                "max_pallet_size": {
                    "type": "integer",
                    "enum": [1, 2, 4, 8, 16, 24, 32],
                },
                "deliveries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "destination": {"type": "string"},
                            "commodity":   {"type": "string"},
                            "scu":         {"type": "integer", "minimum": 1},
                        },
                        "required": ["destination", "commodity", "scu"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["pickup_station", "deliveries"],
        },
    ),
    _fn(
        "remove_contract",
        "Remove a contract from the active workday.",
        {
            "type": "object",
            "properties": {"contract_number": {"type": "integer"}},
            "required": ["contract_number"],
        },
    ),
    _fn(
        "edit_contract",
        "Modify an existing contract.",
        {
            "type": "object",
            "properties": {
                "contract_number": {"type": "integer"},
                "max_pallet_size": {
                    "type": "integer",
                    "enum": [1, 2, 4, 8, 16, 24, 32],
                },
                "deliveries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "destination": {"type": "string"},
                            "commodity":   {"type": "string"},
                            "scu":         {"type": "integer", "minimum": 1},
                        },
                        "required": ["destination", "commodity", "scu"],
                    },
                },
            },
            "required": ["contract_number"],
        },
    ),
    _fn("list_contracts", "Return a summary of all active contracts."),
    _fn("clear_contracts", "Remove ALL contracts from the active workday."),
    _fn("undo", "Undo the last contract add, edit, or remove."),

    # ── Route ───────────────────────────────────────────────────────
    _fn("get_route", "Return the full route stop list."),
    _fn("get_next_stop", "Return the details of the next stop."),
    _fn("complete_current_stop", "Mark the current stop as done and advance."),
    _fn(
        "skip_stop",
        "Skip a specific station in the route.",
        {
            "type": "object",
            "properties": {"station_name": {"type": "string"}},
            "required": ["station_name"],
        },
    ),

    # ── Voice / meta ────────────────────────────────────────────────
    _fn("pause_listening", "Mute the microphone."),
    _fn("resume_listening", "Unmute the microphone."),
    _fn("list_capabilities", "Return a short list of what the Loadmaster can do."),
]
