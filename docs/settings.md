# Settings

User-configurable options for "Giant" Loadmaster. Persisted to a local
settings file (e.g. `%APPDATA%/StarCitizenCargoManager/settings.json` on
Windows) so they survive restarts.

---

## Voice Input

### Listening Mode

One of:

- **Wake word only** — always listening, acts on "Hey Giant ..."
- **Push-to-talk** — wake word disabled; mic is hot only while bind is held
- **Push-to-toggle** — tap bind to start listening, tap again to stop
- **Hybrid** *(default)* — wake word always on; PTT bind also activates the mic
  and bypasses the wake phrase for that utterance

### Push-to-Talk / Push-to-Toggle Bind

PTT is bindable to any of the following — picked up via a **"Press input to
bind"** capture dialog so the user never types a key name:

- **Keyboard** — any key or modifier combo (e.g. `Ctrl+\``, `Caps Lock`,
  side keys)
- **Mouse buttons** — including extra buttons (M4, M5, tilt-wheel)
- **Gamepad** — Xbox / DualShock / generic XInput buttons + triggers
- **HOTAS / Joystick** — any DirectInput button (Virpil, VKB, Warthog, X56,
  T.16000M, etc.); axis-as-button supported for trigger pulls
- **Foot pedal / MIDI** *(future)* — common streamer setups

Bind storage format:

```json
{
  "ptt_bind": {
    "type": "joystick",
    "device_id": "VID_231D&PID_0200",
    "device_name": "VPC Stick MT-50CM2",
    "input": "button_12"
  }
}
```

### Bind Conflict Warnings

When the user binds an input, we check it against:

1. Other Loadmaster binds (hard reject — can't double-bind)
2. A list of common Star Citizen defaults (soft warning — still allow)

Conflicts show a non-blocking toast: *"This is also bound to Star Citizen's
landing gear toggle — you may want to pick something else."*

### Multiple Binds

Up to **3 simultaneous PTT binds** allowed (e.g. keyboard + HOTAS button +
mouse side button) so the user can talk regardless of which device their hand
is on.

---

## Microphone

- **Input device** — dropdown of system mics
- **Input gain** — slider with live VU meter
- **Noise suppression** — on/off (uses RNNoise or platform-native)
- **Activation threshold** — minimum dB to trigger transcription (filters
  background hum during always-on listening)

## Wake Word

- **Sensitivity** — slider (low = fewer false triggers, high = catches mumbles)
- **Custom wake phrase** *(future)* — replace "Hey Giant" with anything else

## Voice Output (TTS)

- **Enabled** — on/off
- **Voice** — dropdown of OpenAI TTS voices
- **Speed** — 0.5x – 2.0x
- **Volume** — independent of system volume so SC audio isn't drowned

## OpenAI

- **API key** — stored in OS keychain (Windows Credential Manager / macOS
  Keychain), never in plain text
- **Model** — default to current best, allow override
- **Realtime API vs separate STT/LLM/TTS** — toggle (Realtime = lowest latency,
  separate = cheaper)

## Appearance

- **Theme** — uses `theme/colors.json` by default; allow user override
- **Window opacity** — for overlay-style use while SC is foregrounded
- **Always on top** — on/off

## Hotkeys (non-voice)

All bindable the same way as PTT:

- **Cancel current command** — default `Esc`
- **Mute/unmute mic** — default unbound
- **Show/hide window** — default unbound

---

## Implementation note

Python input capture across all device types is cleanest via **SDL2** (through
`pysdl2-dll` + `pysdl2`), which handles keyboard, mouse, gamepad, and
DirectInput joysticks uniformly. Wake word detection runs in a separate
thread so input capture stays responsive.
