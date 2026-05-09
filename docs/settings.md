# Settings

User-configurable options for the app. Persisted to the SQLite
`app_settings` table so they survive restarts.

---

## Speech-to-Text Engine

The dictation pipeline has two distinct phases:

1. **Audio → text transcription**
2. **Text → structured contract** (LLM reasoning)

The user picks the transcription provider; the LLM reasoning step
always uses OpenAI for now (the only provider that reliably converts
free-form speech like *"contract one starts at Seraphim, collect 5 SCU
processed food to Ambitious Dream"* into validated contract JSON).

### Engine choice

| Engine | Pros | Cons |
|--------|------|------|
| **OpenAI Whisper / Realtime API** *(default)* | Best accuracy, handles SC station/commodity names well, low latency on Realtime | Requires internet; per-use cost |
| **Windows Speech Recognition** *(SAPI)* | Offline, free | Trained on general dictation, struggles with names like "Baijini", "Seraphim", "Corundum"; can't reason about contract structure on its own |

Star Citizen is online-only (MMORPG), so internet availability while
using the planner is effectively guaranteed. **OpenAI is the
recommended default.** Windows Speech is provided as a fallback for
users who specifically want offline operation, but its raw transcripts
will still be passed to OpenAI for parsing into contracts unless the
user disables that step (in which case they get manual-entry-only
behavior).

### Pipeline diagram

```
mic audio
    │
    ▼
┌─────────────────┐
│ STT engine      │  ← user setting: OpenAI | Windows
│ → raw transcript│
└─────────────────┘
    │
    ▼
┌─────────────────┐
│ OpenAI LLM      │  ← always; uses tools to mutate app state
│ → tool calls    │
└─────────────────┘
    │
    ▼
app state mutation (add_contract, remove_contract, etc.)
```

---

## Voice Input

### Listening Mode

- **Wake word only** *(default)* — always listening, acts on "Hey Giant…"
- **Push-to-talk** — wake word disabled; mic hot only while bind is held
- **Push-to-toggle** — tap bind to start listening, tap again to stop
- **Hybrid** — wake word always on; PTT bind also activates the mic and
  bypasses the wake phrase for that utterance

### Push-to-Talk Bind

Bind any single input or up to **3 simultaneous binds** so PTT works
regardless of which device the user's hand is on. Captured via a
"Press input to bind" dialog — the user never types a key name.

Supported input sources:

- **Keyboard** — any key or modifier combo
- **Mouse buttons** — including extra buttons (M4, M5, tilt-wheel)
- **Gamepad** — Xbox / DualShock / generic XInput buttons + triggers
- **HOTAS / Joystick** — any DirectInput button (Virpil, VKB, Warthog,
  X56, T.16000M, etc.); axis-as-button supported for trigger pulls

### Star Citizen Bind Conflict Warnings

When the user binds an input, we check it against:

1. Other Loadmaster binds (hard reject — can't double-bind)
2. A list of common Star Citizen defaults (soft warning — still allow)

Soft conflict shows a non-blocking toast: *"This is also bound to Star
Citizen's landing gear toggle — you may want to pick something else."*

---

## Microphone

- **Input device** — dropdown of system mics
- **Input gain** — slider with live VU meter
- **Noise suppression** — on/off (RNNoise or platform-native)
- **Activation threshold** — minimum dB to trigger transcription
  (filters background hum during always-on listening)

---

## Wake Word

- **Sensitivity** — slider (low = fewer false triggers, high = catches
  mumbles)
- **Custom wake phrase** *(future)* — replace "Hey Giant" with anything
  else

---

## Voice Output (TTS)

*Personality and TTS are deferred for v1; settings reserved for later.*

- **Enabled** — default off for v1
- **Voice** — dropdown (OpenAI TTS voices)
- **Speed** — 0.5x – 2.0x
- **Volume** — independent of system volume

---

## OpenAI

- **API key** — stored in Windows Credential Manager, never in plain
  text on disk
- **Model** — default to current best Anthropic-recommended Claude
  model **OR** OpenAI model (TBD which provider; the parsing step is
  pluggable)
- **Realtime API vs separate STT/LLM** — toggle when using OpenAI
  (Realtime = lowest latency, separate = cheaper)

---

## API Plugins

Third-party API integrations are configured in a plugins panel rather
than hardcoded into the app. Each plugin contributes additional
capabilities and tools the Loadmaster can call.

Initial planned plugins:

- **UEX Corp** — live commodity prices, station inventory, trade
  routes. Unlocks future profit-optimization features.
- **scunpacked** — authoritative ship/cargo grid data sourced
  directly from Data.p4k. Used to validate `data/ships.json` against
  the actual game.
- **Star Citizen Wiki** — ship matrix, station metadata cross-reference.

Plugin format: a JSON manifest declaring base URL, auth method,
exposed tools (functions the LLM can call), and rate limits. New
plugins can be dropped into a `plugins/` folder without code changes.

---

## Appearance

- **Theme** — uses `theme/colors.json` by default; allow user override
- **Window opacity** — for overlay-style use while SC is foregrounded
- **Always on top** — on/off

---

## Hotkeys (non-voice)

All bindable the same way as PTT:

- **Cancel current command** — default `Esc`
- **Mute/unmute mic** — default unbound
- **Show/hide window** — default unbound
- **Trigger Recompute** — default unbound (binding it is convenient
  during play)
