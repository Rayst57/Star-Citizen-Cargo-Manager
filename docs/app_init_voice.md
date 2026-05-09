# App Initialization & Voice Pipeline

End-to-end specification for: launching the app, wiring the voice
subsystem, capturing PTT inputs on Windows, the STT/LLM/TTS pipeline,
and clean shutdown.

---

## Library choices

| Concern | Library | Why |
|---------|---------|-----|
| UI framework | PySide6 | See `ui_spec.md` |
| Wake word detection | **Picovoice Porcupine** | Local, low-CPU, custom wake words; free for personal use |
| Audio capture | **sounddevice** | Simple API, cross-platform, low-latency PortAudio backend |
| Global keyboard / mouse hotkeys | **pynput** | Captures keys even when SC is foregrounded |
| Gamepad (XInput) | **inputs** | Lightweight, works for Xbox / DualShock controllers |
| HOTAS / Joystick (DirectInput) | **pygame.joystick** | Best DirectInput coverage on Windows |
| OpenAI client | **openai** (≥1.30) | Official SDK; supports Realtime API and Whisper |
| Credential storage | **keyring** | Wraps Windows Credential Manager |
| Windows packaging | **PyInstaller** | One-folder or one-file `.exe` bundles |

Add to `requirements.txt`:
```
pvporcupine>=3.0
sounddevice>=0.4
pynput>=1.7
inputs>=0.5
pygame>=2.5
keyring>=24
```

---

## Application launch sequence

```
main.py
  │
  ├─ QApplication(sys.argv)
  │
  ├─ ui.style.apply_stylesheet(app)            # load QSS from theme/colors.json
  │
  ├─ conn = db.init_db.initialize_database()   # create + seed on first run
  │
  ├─ controller = AppController(conn)
  │     │
  │     ├─ load_settings()                     # from app_settings table
  │     ├─ init_openai_client()                # if api_key in keyring
  │     ├─ init_voice_subsystem()              # if listening_mode != "off"
  │     ├─ open_workday = find_open_workday()  # WHERE ended_at IS NULL
  │     └─ undo_stack = UndoStack(maxlen=1)
  │
  ├─ WorkdayScreen(controller).exec()
  │     ├─ resume → controller.resume_workday(open_workday.id)
  │     ├─ start  → controller.start_workday(origin, final, round_robin)
  │     └─ rejected → sys.exit(0)
  │
  ├─ window = MainWindow(controller)
  │     ├─ wires panels and signals
  │     ├─ refreshes panels from controller state
  │     └─ window.show()
  │
  ├─ controller.start_voice_listening()        # if PTT/wake word enabled
  │
  └─ sys.exit(app.exec())
        │
        └─ on exit:
              ├─ controller.stop_voice_listening()
              ├─ controller.shutdown_threads()
              └─ conn.close()
```

### First-run special cases

- **No `data/` folder present** — fatal. Show error dialog and exit.
- **No OpenAI API key** — non-fatal. Voice subsystem stays disabled until
  user enters key in Settings. App is still usable via manual entry.
- **No microphone available** — non-fatal. Same as above.
- **DB schema present but empty** — re-run seeds.

---

## Voice subsystem architecture

```
                ┌──────────────────────────────────────────────┐
                │ VoiceController  (lives in AppController)    │
                │   owns: wake_listener, ptt_listener, mic     │
                └──┬────────────┬────────────────────┬─────────┘
                   │            │                    │
                   ▼            ▼                    ▼
          ┌─────────────┐ ┌──────────────┐  ┌──────────────────┐
          │ WakeWord    │ │ PTT Listener │  │ Mic Capture      │
          │ (Porcupine) │ │ (pynput +    │  │ (sounddevice)    │
          │ background  │ │  pygame)     │  │ on demand        │
          │  thread     │ │  threads     │  │                  │
          └──────┬──────┘ └──────┬───────┘  └─────────┬────────┘
                 │ trigger        │ trigger             │ raw audio bytes
                 └────────┬───────┘                     │
                          ▼                             ▼
                   ┌────────────────────────────────────────────┐
                   │  VoiceThread (QThread)                     │
                   │   captures utterance →                     │
                   │     transcribe (STT) →                     │
                   │       run LLM call (LLMThread) →           │
                   │         dispatch tool / show response      │
                   └────────────────────────────────────────────┘
                                   │
                                   ▼
                            controller.dispatch_tool()
                                   │
                                   ▼
                            Qt signals → MainWindow refresh
```

### Listening modes (from `settings.md`)

| Mode | Wake word? | PTT? | Behaviour |
|------|------------|------|-----------|
| `wake_word`  | ✓ | ✗ | Always listening; only acts on "Hey Giant…" |
| `ptt`        | ✗ | ✓ | Mic hot ONLY while PTT bind is held |
| `ptt_toggle` | ✗ | ✓ | PTT tap to start, tap again to stop |
| `hybrid`     | ✓ | ✓ | Both — PTT bypasses wake phrase for that utterance |
| `off`        | ✗ | ✗ | Voice disabled entirely; manual entry only |

Switching modes at runtime restarts the relevant subsystems.

---

## Wake word detection (Porcupine)

```python
import pvporcupine
import sounddevice as sd

class WakeWordListener(QThread):
    triggered = Signal()           # emitted when wake phrase detected

    def __init__(self, access_key: str, wake_phrase: str = "Hey Giant"):
        super().__init__()
        # For v1: use built-in keyword "computer" or similar.
        # Custom "Hey Giant" requires Picovoice Console training (paid tier).
        self.porcupine = pvporcupine.create(
            access_key=access_key,
            keywords=["computer"],       # placeholder until custom trained
            sensitivities=[0.6],         # tunable in Settings
        )
        self.frame_length = self.porcupine.frame_length    # 512 samples
        self.sample_rate = self.porcupine.sample_rate      # 16000

    def run(self):
        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype='int16',
            blocksize=self.frame_length,
        ) as stream:
            while not self.isInterruptionRequested():
                pcm, _ = stream.read(self.frame_length)
                idx = self.porcupine.process(pcm.flatten())
                if idx >= 0:
                    self.triggered.emit()

    def stop(self):
        self.requestInterruption()
        self.wait()
        self.porcupine.delete()
```

**Wake phrase decision:**
- v1: ship with Porcupine built-in keyword (`computer`, `jarvis`, etc.)
- v2: train custom "Hey Giant" via Picovoice Console — single-line config
  swap, no code changes.

Sensitivity slider in Settings → maps to Porcupine `sensitivities[0]`
(0.0–1.0). Default 0.6.

---

## Push-to-Talk capture

Three input sources run as separate threads. All emit a single
`ptt_pressed` / `ptt_released` signal that `VoiceController` listens to.

### Keyboard / mouse (`pynput`)

```python
from pynput import keyboard, mouse

class KeyboardPTTListener(QThread):
    pressed  = Signal()
    released = Signal()

    def __init__(self, bind: dict):
        # bind = {"keys": [Key.f15], "modifiers": []}
        ...

    def run(self):
        with keyboard.Listener(on_press=..., on_release=...) as listener:
            listener.join()
```

`pynput` runs its own listener thread internally; we wrap it for Qt
signal compatibility.

### Gamepad / HOTAS (`pygame.joystick`)

```python
import pygame

class JoystickPTTListener(QThread):
    pressed  = Signal()
    released = Signal()

    def __init__(self, device_id: int, button_id: int):
        ...

    def run(self):
        pygame.init()
        pygame.joystick.init()
        joy = pygame.joystick.Joystick(self.device_id)
        joy.init()
        prev_state = False
        while not self.isInterruptionRequested():
            pygame.event.pump()
            state = joy.get_button(self.button_id)
            if state and not prev_state:
                self.pressed.emit()
            elif not state and prev_state:
                self.released.emit()
            prev_state = state
            self.msleep(15)            # ~66 Hz polling
```

**Hybrid devices (Virpil, VKB, Warthog)** — pygame exposes them as
generic DirectInput joysticks, including all extra buttons.

### Bind capture dialog

When the user clicks "Press input to bind" in Settings:

```
PTTBindDialog (QDialog)
  ├─ QLabel "Press the input to bind for PTT"
  ├─ QLabel  status      (updates as inputs are detected)
  └─ QPushButton "Cancel"
```

While open, all three listener threads run in "capture" mode (don't
trigger PTT, just detect any input event). First non-modifier event
captured becomes the bind. Up to 3 simultaneous binds supported.

---

## Audio capture & utterance segmentation

When the wake word fires (or PTT pressed), `VoiceController` starts
recording until either:

- 2 seconds of trailing silence (RMS < threshold), OR
- PTT released, OR
- 30 second cap (safety)

```python
class UtteranceRecorder:
    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.silence_threshold_rms = 250        # tunable
        self.silence_duration = 2.0             # seconds
        self.max_duration = 30.0

    def record(self) -> bytes:
        """Blocking record until silence or max_duration. Returns WAV bytes."""
        ...
```

The returned WAV bytes are passed to STT.

**Audio settings exposed in `settings.md`:**
- Input device (sounddevice device list)
- Input gain (dBFS adjust)
- Noise suppression (RNNoise via `noisereduce` library) — toggle
- Activation threshold (RMS dB)

---

## STT pipeline (two engines)

### OpenAI Whisper API (default — recommended)

```python
def transcribe_openai(wav_bytes: bytes) -> str:
    response = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=("utterance.wav", wav_bytes, "audio/wav"),
        language="en",
        prompt=(
            "Star Citizen cargo planning. "
            "Stations: Seraphim, Baijini Point, Everus Harbor, Port Tressler, "
            "Yellow Core, Shallow Fields, Wide Forest, Lively Pathway. "
            "Commodities: Tungsten, Aluminum, Titanium, Pressurized Ice, "
            "Processed Food, Quartz, Corundum."
        ),
    )
    return response.text
```

The `prompt=` field biases Whisper toward SC-specific names. We populate
it dynamically from active station and commodity tables on each call.

### Windows Speech Recognition (offline fallback)

Uses Python's `speech_recognition` library wrapping SAPI:

```python
import speech_recognition as sr

def transcribe_windows(wav_bytes: bytes) -> str:
    r = sr.Recognizer()
    audio = sr.AudioData(wav_bytes, sample_rate=16000, sample_width=2)
    return r.recognize_sphinx(audio)        # or recognize_azure / recognize_google
```

Per `settings.md`: even with WSR transcription, the resulting text is
still passed to OpenAI for tool-call reasoning unless the user explicitly
disables that step (in which case voice becomes manual-entry-trigger only).

### OpenAI Realtime API (advanced, low-latency option)

When `use_realtime_api=true` in settings, the entire flow uses a
WebSocket connection to OpenAI Realtime:

```
mic → realtime API → tool call → confirmation back → TTS audio
```

This bypasses our discrete STT/LLM/TTS stages.  Implementation is
isolated in `voice/realtime.py` and switched by settings flag.

---

## LLM call layer

```python
class LLMThread(QThread):
    response_text = Signal(str)
    tool_call     = Signal(str, dict)    # tool_name, args
    error         = Signal(str)

    def __init__(self, controller, transcript: str, conversation_history: list):
        super().__init__()
        self.controller = controller
        self.transcript = transcript
        self.history = conversation_history

    def run(self):
        try:
            messages = [
                {"role": "system", "content": self._build_system_prompt()},
                *self.history,
                {"role": "user", "content": self.transcript},
            ]
            resp = openai_client.chat.completions.create(
                model=self.controller.settings["model"],
                messages=messages,
                tools=GIANT_TOOLS,           # list from llm_tools.md
                tool_choice="auto",
                temperature=0.1,
                max_tokens=512,
            )
            msg = resp.choices[0].message
            if msg.tool_calls:
                tc = msg.tool_calls[0]
                args = json.loads(tc.function.arguments)
                self.tool_call.emit(tc.function.name, args)
            else:
                self.response_text.emit(msg.content)
        except Exception as e:
            self.error.emit(str(e))

    def _build_system_prompt(self) -> str:
        ctx = self.controller.build_context_snapshot()
        return SYSTEM_PROMPT_TEMPLATE.replace("[CONTEXT]", ctx)
```

`GIANT_TOOLS` is the Python list of tool definitions from `llm_tools.md`,
imported from `src/voice/tool_schemas.py`.

After `tool_call.emit()`:

```python
def on_tool_call(self, tool_name: str, args: dict):
    try:
        result_text = self.controller.dispatch_tool(tool_name, args)
        self._show_response(result_text)
        if self.tts_enabled:
            self._speak(result_text)
    except ToolError as e:
        self._show_response(f"Error: {e}")
```

---

## Reserved phrases (no LLM call)

`VoiceController` matches these against the raw transcript BEFORE
spawning `LLMThread`:

```python
RESERVED_PHRASES = {
    "stand by giant":  lambda c: c.mute_mic(),
    "cancel that":     lambda c: c.abort_inflight_llm(),
}

def handle_transcript(self, text: str):
    normalized = re.sub(r'[^a-z ]', '', text.lower()).strip()
    for phrase, action in RESERVED_PHRASES.items():
        if phrase in normalized:
            action(self.controller)
            return
    # otherwise → LLMThread
```

These bypass LLM latency for safety-critical commands.

---

## TTS output (deferred — seam only for v1)

```python
class TTSPlayer(QObject):
    def __init__(self, voice="alloy", speed=1.0):
        ...

    def speak(self, text: str) -> None:
        if not self.enabled:
            return
        audio = openai_client.audio.speech.create(
            model="tts-1",
            voice=self.voice,
            input=text,
        )
        # play through sounddevice
```

For v1 the TTS toggle is OFF and `speak()` is a no-op. Seam stays so
v2 can wire it on.

---

## Settings persistence

```python
class AppSettings:
    """Wraps app_settings table; exposes typed getters/setters."""

    DEFAULTS = {
        "stt_engine":       "openai",       # 'openai' | 'windows'
        "listening_mode":   "wake_word",
        "wake_phrase":      "Hey Giant",
        "wake_sensitivity": 0.6,
        "ptt_binds":        "[]",           # JSON list of bind dicts
        "input_device":     "",             # sounddevice device name
        "input_gain":       0.0,
        "noise_suppression": False,
        "activation_threshold": -45.0,      # dBFS
        "tts_enabled":      False,
        "tts_voice":        "alloy",
        "tts_speed":        1.0,
        "model":            "gpt-4o",
        "use_realtime_api": False,
        "theme":            "default",
        "window_opacity":   1.0,
        "always_on_top":    False,
        "hotkey_recompute": "[]",
        "hotkey_cancel":    "Escape",
    }
```

OpenAI API key is stored separately in Windows Credential Manager via
`keyring`:

```python
keyring.set_password("Star Citizen Cargo Manager", "openai_api_key", key)
api_key = keyring.get_password("Star Citizen Cargo Manager", "openai_api_key")
```

Never in `app_settings` table, never in plain text on disk.

---

## Thread lifecycle

```
AppController owns:
  ├─ wake_thread     : WakeWordListener     (when listening_mode in {wake_word, hybrid})
  ├─ ptt_threads     : list[KeyboardPTTListener | JoystickPTTListener]
  ├─ recompute_thread: RecomputeThread or None      (transient)
  └─ llm_thread      : LLMThread or None             (transient)

start_voice_listening():
  spawn wake_thread, ptt_threads as appropriate

stop_voice_listening():
  for each thread: thread.requestInterruption(); thread.wait()

shutdown_threads():
  cancel any in-flight LLM call (httpx cancel)
  stop_voice_listening()
  if recompute_thread.isRunning(): recompute_thread.wait()
```

All threads check `isInterruptionRequested()` between work units. The
LLM HTTP call is the only blocking I/O — cancel via httpx client close.

---

## Module file structure (additions)

```
src/
├── app_controller.py         # main bridge (already specified in ui_spec.md)
├── undo.py                   # one-step undo stack
├── voice/
│   ├── __init__.py
│   ├── voice_controller.py   # spawns/manages all voice threads
│   ├── wake_word.py          # Porcupine listener
│   ├── ptt_keyboard.py       # pynput PTT
│   ├── ptt_joystick.py       # pygame PTT
│   ├── recorder.py           # UtteranceRecorder
│   ├── stt_openai.py         # Whisper API
│   ├── stt_windows.py        # SAPI / sphinx
│   ├── llm_thread.py         # LLM call + tool dispatch
│   ├── tts.py                # OpenAI TTS (seam, no-op for v1)
│   ├── realtime.py           # OpenAI Realtime WebSocket (alt path)
│   ├── tool_schemas.py       # GIANT_TOOLS list (from llm_tools.md)
│   └── system_prompt.py      # SYSTEM_PROMPT_TEMPLATE
└── settings.py               # AppSettings + keyring wrapper
```

---

## End-to-end voice command example

User says: *"Hey Giant, new contract from Yellow Core, 55 SCU Tungsten to Everus Harbor, max 8."*

```
1. WakeWordListener.run() detects "Hey Giant" → triggered.emit()
2. VoiceController starts UtteranceRecorder
3. After ~3 sec of speech + 2 sec silence → recorder returns wav_bytes
4. VoiceController calls transcribe_openai(wav_bytes)
   → "new contract from Yellow Core, 55 SCU Tungsten to Everus Harbor, max 8"
5. VoiceController checks RESERVED_PHRASES → no match
6. VoiceController spawns LLMThread(controller, transcript, history)
7. LLMThread builds system prompt with current context snapshot
8. OpenAI returns:
     tool_call("add_contract", {
       "pickup_station": "Yellow Core",
       "max_pallet_size": 8,
       "deliveries": [
         {"destination": "Everus Harbor", "commodity": "Tungsten", "scu": 55}
       ]
     })
9. LLMThread.tool_call.emit("add_contract", {...})
10. AppController.dispatch_tool("add_contract", {...})
    → canonicalize station/commodity names
    → INSERT INTO contracts + cargo_lines
    → set plan_dirty = 1
    → emit contracts_changed signal
    → return "Contract 1 added: 55 SCU Tungsten Yellow Core → Everus Harbor."
11. ContractsPanel refreshes (new contract card appears)
12. RecomputeBanner becomes visible
13. UI shows the response text in a transient toast
14. (TTS off in v1; v2 would speak it)
```

---

## Error handling

| Error | Where caught | User-facing |
|-------|--------------|-------------|
| Mic device disappeared | sounddevice exception | Toast + status bar mic icon → cancelled |
| OpenAI API down | LLMThread.error signal | Status bar API health → red dot; toast |
| Invalid OpenAI key | LLMThread first call | Modal: "OpenAI key invalid. Check Settings." |
| Wake word library not loaded | startup | Disable voice; settings shows "voice unavailable" |
| Tool args invalid | dispatch_tool raises ToolError | LLM gets error message → asks pilot to clarify |
| DB write failure | controller method | Modal error; rollback transaction |

All voice errors degrade gracefully — the app remains usable via manual
entry even if the entire voice subsystem fails to initialise.
