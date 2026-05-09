# Giant Loadmaster — LLM Tool Schemas & System Prompt

Defines the OpenAI function-calling interface between the app and the
"Giant" Loadmaster AI. Every voice or manual command that mutates app state
goes through this interface.

---

## Architecture overview

```
User speech / typed command
        │
        ▼
STT engine (OpenAI Whisper or Windows Speech)
        │  raw transcript
        ▼
LLM call  ──  system prompt (with injected current state snapshot)
              user message (transcript)
              tool definitions (below)
        │
        ├─ tool_call → AppController.dispatch_tool(name, args)
        │                    → planner / DB mutation
        │                    → confirmation response
        └─ text response → display in UI + optional TTS
```

One LLM call per voice utterance. The conversation history sent is the
last 6 turns (3 user + 3 assistant), enough for "scratch that" / "undo"
to work without ballooning token count.

---

## System prompt

The system prompt is a template. `[CONTEXT]` is replaced at call time by
`AppController.build_context_snapshot()`.

```
You are Giant, the Loadmaster AI for a Star Citizen cargo operation
aboard a C2 Hercules starship.

ROLE
You help the pilot manage cargo contracts, route planning, and bay
loading during live flight.  Execute commands via the tools provided.
Answer direct questions from the context snapshot below.  Be brief —
the pilot is busy.

CURRENT STATE
[CONTEXT]

COMMUNICATION RULES
- Short, direct responses.  No filler ("Sure!", "Absolutely!").
- After a tool call, confirm in one sentence what was done.
- If something is ambiguous, name the ambiguity and ask — never guess.
- Use approved action labels: Depart, Arrive, Load, Unload, Final unload.
- Zone labels: F1 F2 F3 (forward bay), R1 R2 R3 R4 (rear bay).

WHAT NOT TO DO
- Never recompute automatically — user must trigger it.
- Never invent station or zone names not in the database.
- Never reference cargo not in the active workday.
- Never confirm an action you have not executed via a tool call.
```

---

## Context snapshot format

`AppController.build_context_snapshot()` produces a compact string injected
into `[CONTEXT]`.  Tokens must be kept low — target under 300 tokens.

```
WORKDAY: origin=Seraphim Station  final=Port Tressler  round_robin=off
STOP: 3 of 11  current=Yellow Core (Load)

CONTRACTS:
  #1  Yellow Core | Tungsten 55 SCU → Everus Harbor   max=8  ⚠CONFLICT
  #2  Yellow Core | Tungsten 27 SCU → Baijini Point   max=8  ⚠CONFLICT

LOADOUT:
  R1  55 SCU Tungsten → Everus Harbor   ⚠CONFLICT
  R4  27 SCU Tungsten → Baijini Point   ⚠CONFLICT

CONFLICTS:
  G1  Yellow Core × Tungsten
      Ambiguous: 1×2 + 1×1 SCU
      Everus unique: 6×8 + 1×4 | zone R1
      Baijini unique: 3×8      | zone R4
```

If no workday is active: `WORKDAY: none`.
If no contracts: `CONTRACTS: none`.
If no route computed: `STOP: not computed — recompute required`.

---

## Tool definitions

OpenAI `tools` array format.  All tools use `"type": "function"`.

---

### Workday

#### `start_workday`
```json
{
  "name": "start_workday",
  "description": "Start a new workday. Clears all contracts, route, and zone assignments from any previous workday.",
  "parameters": {
    "type": "object",
    "properties": {
      "origin_station": {
        "type": "string",
        "description": "Station name where the ship is currently located (departure point)."
      },
      "final_destination": {
        "type": "string",
        "description": "Optional. Last stop of the day. Omit if not specified by user."
      },
      "round_robin": {
        "type": "boolean",
        "description": "If true, return to origin after the last delivery. Default false."
      }
    },
    "required": ["origin_station"]
  }
}
```

#### `resume_workday`
```json
{
  "name": "resume_workday",
  "description": "Resume the most recent open workday. No parameters.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `end_workday`
```json
{
  "name": "end_workday",
  "description": "Close the active workday. Marks it as ended in the database.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `set_origin`
```json
{
  "name": "set_origin",
  "description": "Change the departure origin for the active workday.",
  "parameters": {
    "type": "object",
    "properties": {
      "station_name": { "type": "string" }
    },
    "required": ["station_name"]
  }
}
```

#### `set_final_destination`
```json
{
  "name": "set_final_destination",
  "description": "Set or change the final destination for the active workday.",
  "parameters": {
    "type": "object",
    "properties": {
      "station_name": {
        "type": "string",
        "description": "Pass null or omit to clear the final destination."
      }
    }
  }
}
```

#### `set_round_robin`
```json
{
  "name": "set_round_robin",
  "description": "Enable or disable round-robin (return to origin after final delivery).",
  "parameters": {
    "type": "object",
    "properties": {
      "enabled": { "type": "boolean" }
    },
    "required": ["enabled"]
  }
}
```

#### `recompute_plan`
```json
{
  "name": "recompute_plan",
  "description": "Recompute the route, zone assignments, and conflict analysis. Clears the plan_dirty flag.",
  "parameters": { "type": "object", "properties": {} }
}
```

---

### Contract management

#### `add_contract`
```json
{
  "name": "add_contract",
  "description": "Add a new cargo contract to the active workday. A contract has one pickup station and one or more deliveries.",
  "parameters": {
    "type": "object",
    "properties": {
      "pickup_station": {
        "type": "string",
        "description": "Station name where the cargo is collected."
      },
      "max_pallet_size": {
        "type": "integer",
        "enum": [1, 2, 4, 8, 16, 24, 32],
        "description": "Largest SCU pallet size the contract allows. Default 8 if not specified."
      },
      "deliveries": {
        "type": "array",
        "description": "List of cargo deliveries included in this contract.",
        "items": {
          "type": "object",
          "properties": {
            "destination": {
              "type": "string",
              "description": "Delivery station name."
            },
            "commodity": {
              "type": "string",
              "description": "Commodity name (e.g. 'Tungsten', 'Processed Food')."
            },
            "scu": {
              "type": "integer",
              "minimum": 1,
              "description": "Total SCU amount to deliver."
            }
          },
          "required": ["destination", "commodity", "scu"]
        },
        "minItems": 1
      }
    },
    "required": ["pickup_station", "deliveries"]
  }
}
```

#### `remove_contract`
```json
{
  "name": "remove_contract",
  "description": "Remove a contract from the active workday.",
  "parameters": {
    "type": "object",
    "properties": {
      "contract_number": {
        "type": "integer",
        "description": "1-based contract number shown in the contracts list."
      }
    },
    "required": ["contract_number"]
  }
}
```

#### `edit_contract`
```json
{
  "name": "edit_contract",
  "description": "Modify an existing contract. Only supply the fields that change.",
  "parameters": {
    "type": "object",
    "properties": {
      "contract_number": {
        "type": "integer",
        "description": "1-based contract number to edit."
      },
      "max_pallet_size": {
        "type": "integer",
        "enum": [1, 2, 4, 8, 16, 24, 32],
        "description": "New max pallet size. Omit if unchanged."
      },
      "deliveries": {
        "type": "array",
        "description": "Replacement delivery list. Omit if unchanged. Replaces ALL existing deliveries for this contract.",
        "items": {
          "type": "object",
          "properties": {
            "destination": { "type": "string" },
            "commodity":   { "type": "string" },
            "scu":         { "type": "integer", "minimum": 1 }
          },
          "required": ["destination", "commodity", "scu"]
        }
      }
    },
    "required": ["contract_number"]
  }
}
```

#### `list_contracts`
```json
{
  "name": "list_contracts",
  "description": "Return a summary of all active contracts. Prefer answering from the CONTRACTS section of the context snapshot before calling this tool.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `clear_contracts`
```json
{
  "name": "clear_contracts",
  "description": "Remove ALL contracts from the active workday. Irreversible without undo.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `undo`
```json
{
  "name": "undo",
  "description": "Undo the last contract add, edit, or remove. One level of undo only.",
  "parameters": { "type": "object", "properties": {} }
}
```

---

### Route

#### `get_route`
```json
{
  "name": "get_route",
  "description": "Return the full route stop list. Prefer answering from the STOP section of the context snapshot for simple 'what's next' queries.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `get_next_stop`
```json
{
  "name": "get_next_stop",
  "description": "Return the details of the next stop: station, action, unload/load summary.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `complete_current_stop`
```json
{
  "name": "complete_current_stop",
  "description": "Mark the current stop as done and advance to the next stop.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `skip_stop`
```json
{
  "name": "skip_stop",
  "description": "Skip a specific station in the route. Use only when the pilot explicitly requests a skip.",
  "parameters": {
    "type": "object",
    "properties": {
      "station_name": {
        "type": "string",
        "description": "Name of the station to skip."
      }
    },
    "required": ["station_name"]
  }
}
```

---

### Cargo / Loading

#### `get_load_plan`
```json
{
  "name": "get_load_plan",
  "description": "Return the full cargo load plan: zone assignments, pallet breakdowns, and conflict notes.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `get_zone_contents`
```json
{
  "name": "get_zone_contents",
  "description": "Return what is currently loaded in a specific cargo zone.",
  "parameters": {
    "type": "object",
    "properties": {
      "zone_label": {
        "type": "string",
        "enum": ["F1", "F2", "F3", "R1", "R2", "R3", "R4"],
        "description": "Zone to query."
      }
    },
    "required": ["zone_label"]
  }
}
```

#### `get_capacity`
```json
{
  "name": "get_capacity",
  "description": "Return total SCU capacity, used SCU, and remaining SCU across all zones.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `explain_placement`
```json
{
  "name": "explain_placement",
  "description": "Explain why a piece of cargo was placed in its current zone (conflict avoidance, load order, capacity, etc.).",
  "parameters": {
    "type": "object",
    "properties": {
      "description": {
        "type": "string",
        "description": "Natural-language description of the cargo or zone to explain. E.g. 'the Tungsten in F2' or 'why is R4 empty'."
      }
    },
    "required": ["description"]
  }
}
```

---

### Session

#### `save_session`
```json
{
  "name": "save_session",
  "description": "Save the current workday plan under a named label for later recall.",
  "parameters": {
    "type": "object",
    "properties": {
      "name": { "type": "string", "description": "Label for this saved session, e.g. 'Hurston loop'." }
    },
    "required": ["name"]
  }
}
```

#### `load_session`
```json
{
  "name": "load_session",
  "description": "Restore a previously saved named session.",
  "parameters": {
    "type": "object",
    "properties": {
      "name": { "type": "string" }
    },
    "required": ["name"]
  }
}
```

#### `export_plan`
```json
{
  "name": "export_plan",
  "description": "Export the current route and load plan to a file.",
  "parameters": {
    "type": "object",
    "properties": {
      "format": {
        "type": "string",
        "enum": ["pdf", "txt"],
        "description": "Output format. Default 'txt'."
      }
    }
  }
}
```

---

### Voice / meta

#### `pause_listening`
```json
{
  "name": "pause_listening",
  "description": "Mute the microphone and stop responding to the wake word until resume_listening is called.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `resume_listening`
```json
{
  "name": "resume_listening",
  "description": "Unmute the microphone and resume wake-word detection.",
  "parameters": { "type": "object", "properties": {} }
}
```

#### `list_capabilities`
```json
{
  "name": "list_capabilities",
  "description": "Return a short list of what the Loadmaster can do.",
  "parameters": { "type": "object", "properties": {} }
}
```

---

## Disambiguation rules

These guide how `AppController` handles ambiguous input before or after
the LLM call:

| Situation | Behaviour |
|-----------|-----------|
| Station name matches multiple stations | LLM lists matches, asks pilot to confirm by number |
| Contract reference unclear ("the Seraphim run") | LLM lists matching contracts from context, asks for contract number |
| SCU amount not specified in `add_contract` | LLM asks: "How many SCU?" |
| max_pallet_size not mentioned | Default 8; no clarification needed |
| Commodity name unrecognised by alias table | AppController returns error; LLM asks pilot to rephrase |
| Destination is a gateway station (e.g. Pyro Jump Point) | AppController warns: "That's a gateway, not a delivery station" |

---

## Tool execution flow in AppController

```python
def dispatch_tool(self, tool_name: str, args: dict) -> str:
    """Called by LLMThread after parsing a tool_call from OpenAI response.

    Returns a confirmation string to feed back as the tool result message.
    Raises ToolError on invalid args (returned to LLM as an error message).
    """
    match tool_name:
        case "add_contract":
            contract_id = self.add_contract(args)
            self._set_dirty()
            return f"Contract {self.workday_contract_count()} added."

        case "remove_contract":
            n = args["contract_number"]
            self.remove_contract_by_number(n)
            self._set_dirty()
            return f"Contract {n} removed."

        case "recompute_plan":
            self.recompute()          # spawns RecomputeThread
            return "Recomputing…"

        case "complete_current_stop":
            stop = self.advance_stop()
            return f"Stop {stop.stop_number - 1} complete. Next: {stop.station_name}."

        # … etc.

        case _:
            raise ToolError(f"Unknown tool: {tool_name}")
```

---

## Reserved always-on phrases (no LLM call)

These bypass the full LLM pipeline and are handled directly by the voice
layer in `VoiceThread`:

| Phrase | Action |
|--------|--------|
| `"Stand by, Giant"` | Immediately mute mic, cancel any in-progress TTS |
| `"Cancel that"` | Abort the LLM call currently in flight |

These are matched by exact string comparison after STT, before the LLM
call is made.

---

## Query tools vs mutation tools

Query tools (`list_contracts`, `get_route`, `get_next_stop`, `get_load_plan`,
`get_zone_contents`, `get_capacity`, `explain_placement`, `list_capabilities`)
return text only and do not change state.

The LLM is instructed to answer simple queries directly from the context
snapshot when the information is already there, saving one round-trip.
Tool calls for queries are reserved for when the pilot asks for verbose
detail beyond what the snapshot provides.

---

## LLM call configuration

```python
OPENAI_CALL_CONFIG = {
    "model":       "gpt-4o",          # or claude-sonnet-4-6 if using Anthropic
    "temperature": 0.1,               # low — we want deterministic tool calls
    "max_tokens":  512,               # responses are short
    "tool_choice": "auto",            # LLM picks tool or text response
}
```

History window: last 6 messages (3 user + 3 assistant).  System prompt
(with fresh context snapshot) is always regenerated per call — not cached.
