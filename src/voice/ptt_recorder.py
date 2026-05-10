"""
PTT (push-to-talk) microphone capture + Whisper transcription.

The PasteContractsDialog uses this to let the user dictate contracts
directly into the input area instead of typing them. The mic captures
16 kHz mono audio while recording is active, then sends the buffered
WAV to OpenAI's transcription API (`whisper-1`) on a worker thread.

Toggle lifecycle:
    rec = PTTRecorder(api_key)
    rec.transcript.connect(on_text)     # called when transcription finishes
    rec.error.connect(on_error)         # called on any failure
    rec.state.connect(on_state)         # 'recording'|'transcribing'|'idle'|'error'

    rec.start_recording()               # mic opens; samples accumulate
    ...user speaks...
    rec.stop_recording()                # mic closes; transcription posted

Each start/stop cycle produces exactly one transcript signal (or one
error). The caller is responsible for any UI side-effects (playing a
sound cue, updating a button label, appending text, etc.).

The module degrades gracefully when sounddevice or openai isn't
installed — start_recording emits an error instead of throwing, so the
rest of the app stays usable on a system without microphone support.
"""

from __future__ import annotations

import io
import wave

from PySide6.QtCore import QObject, QThread, Signal


class PTTRecorder(QObject):
    transcript = Signal(str)
    error = Signal(str)
    # 'recording' (mic open), 'transcribing' (waiting on Whisper),
    # 'idle' (ready for next cycle), 'error' (last cycle failed).
    state = Signal(str)

    SAMPLE_RATE = 16000   # Whisper accepts 16 kHz mono; smaller payload.

    def __init__(self, api_key: str, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._stream = None
        self._buffer: list = []
        self._recording = False
        self._worker_thread: QThread | None = None
        self._worker: _TranscribeWorker | None = None

    # ── recording ──────────────────────────────────────────────────────

    def start_recording(self) -> None:
        if self._recording:
            return
        try:
            import sounddevice as sd
        except ImportError as e:
            self.error.emit(
                f"sounddevice not installed ({e.name}). "
                f"Install requirements-voice.txt to dictate."
            )
            self.state.emit("error")
            return
        try:
            self._buffer = []
            self._stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=self._on_audio_block,
            )
            self._stream.start()
            self._recording = True
            self.state.emit("recording")
        except Exception as e:
            self.error.emit(f"Mic failed to open: {e}")
            self.state.emit("error")

    def _on_audio_block(self, indata, frames, time_info, status) -> None:
        # The sounddevice callback runs on its own thread; copy out of
        # the read-only buffer immediately so we don't hold the lock.
        if self._recording:
            self._buffer.append(bytes(indata))

    def stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
        self._stream = None

        if not self._buffer:
            self.error.emit("No audio captured.")
            self.state.emit("idle")
            return

        wav_bytes = self._to_wav_bytes(b"".join(self._buffer))
        self._buffer = []
        self.state.emit("transcribing")
        self._post_to_whisper(wav_bytes)

    def _to_wav_bytes(self, pcm: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)              # int16 = 2 bytes
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(pcm)
        return buf.getvalue()

    # ── transcription ──────────────────────────────────────────────────

    def _post_to_whisper(self, wav_bytes: bytes) -> None:
        thread = QThread(self)
        worker = _TranscribeWorker(self._api_key, wav_bytes)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_transcript)
        worker.error.connect(self._on_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._worker_thread = thread
        self._worker = worker
        thread.start()

    def _on_transcript(self, text: str) -> None:
        self.transcript.emit(text)
        self.state.emit("idle")

    def _on_error(self, msg: str) -> None:
        self.error.emit(msg)
        self.state.emit("error")


class _TranscribeWorker(QObject):
    done = Signal(str)
    error = Signal(str)
    finished = Signal()

    def __init__(self, api_key: str, wav_bytes: bytes):
        super().__init__()
        self._api_key = api_key
        self._wav = wav_bytes

    def run(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            self.error.emit(f"openai package missing: {e.name}")
            self.finished.emit()
            return
        try:
            client = OpenAI(api_key=self._api_key)
            wav_io = io.BytesIO(self._wav)
            # The OpenAI SDK reads `.name` to determine the upload's
            # extension — Whisper requires the file to look like a wav.
            wav_io.name = "dictation.wav"
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=wav_io,
            )
            self.done.emit((resp.text or "").strip())
        except Exception as e:
            self.error.emit(f"Transcription failed: {type(e).__name__}: {e}")
        finally:
            self.finished.emit()
