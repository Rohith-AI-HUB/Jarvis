from __future__ import annotations

import ctypes
import logging
import queue
import re
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    import pyttsx3
except ImportError:  # pragma: no cover
    pyttsx3 = None

try:
    from groq import Groq
except ImportError:  # pragma: no cover
    Groq = None

try:
    from piper import PiperVoice
except ImportError:  # pragma: no cover
    PiperVoice = None

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None

try:
    import winsound
except ImportError:  # pragma: no cover
    winsound = None

try:
    from faster_whisper import WhisperModel
except ImportError:  # pragma: no cover
    WhisperModel = None

try:
    from openwakeword.model import Model as WakeModel
    from openwakeword.utils import download_models
except ImportError:  # pragma: no cover
    WakeModel = None
    download_models = None

from system.config import settings


CaptureMode = Literal["wake_word", "command", "confirmation"]
CaptureStatus = Literal["ok", "empty", "timeout", "error"]
LOGGER = logging.getLogger(__name__)


def voice_dependency_status() -> dict[str, bool]:
    return {
        "numpy": np is not None,
        "pyttsx3": pyttsx3 is not None,
        "groq": Groq is not None,
        "piper": PiperVoice is not None,
        "sounddevice": sd is not None,
        "winsound": winsound is not None,
        "faster_whisper": WhisperModel is not None,
        "openwakeword": WakeModel is not None and download_models is not None,
    }


def _set_env_value(path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass(slots=True)
class CaptureResult:
    status: CaptureStatus
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.text.strip())


@dataclass(slots=True)
class SpeechRequest:
    text: str
    done: threading.Event | None = None
    ok: bool = False
    cancel: threading.Event | None = None
    interrupted: bool = False


class VoiceInterface:
    def __init__(self) -> None:
        self.engine = None
        self._wake_lock = threading.Lock()
        self._model_lock = threading.Lock()
        self._wake_thread: threading.Thread | None = None
        self._wake_stop = threading.Event()
        self._wake_ready = threading.Event()
        self._whisper_ready = threading.Event()
        self._speech_lock = threading.Lock()
        self._speech_stop = threading.Event()
        self._speech_thread: threading.Thread | None = None
        self._speech_queue: queue.Queue[SpeechRequest | None] = queue.Queue()
        self._speech_backend = "none"
        self._speech_active = threading.Event()
        self._speech_interrupt = threading.Event()
        self._current_speech_request: SpeechRequest | None = None
        self._wake_model: WakeModel | None = None
        self._whisper_model: WhisperModel | None = None
        self._groq_client = None
        self._piper_voice = None
        self._whisper_device = settings.whisper_device
        self._whisper_compute_type = settings.whisper_compute_type
        self._speech_cache_dir = settings.whisper_download_root.parent / "tts_cache"
        self._sample_rate = 16000
        self._channels = 1
        self._capture_chunk_size = 1600
        self._wake_chunk_size = 1280
        self._cpu_fallback_persisted = False

    def available(self) -> bool:
        """Check if all critical voice dependencies are available."""
        if not settings.offline_stt_enabled:
            return False
        return (
            np is not None
            and sd is not None
            and WakeModel is not None
            and WhisperModel is not None
        )

    def _check_critical_dependencies(self) -> list[str]:
        """Check for critical dependencies and return list of missing ones."""
        missing = []
        if np is None:
            missing.append("numpy")
        if sd is None:
            missing.append("sounddevice")
        if WakeModel is None:
            missing.append("openwakeword")
        if WhisperModel is None:
            missing.append("faster_whisper")
        return missing

    def _handle_dependency_error(self, dependency: str, feature: str, error: Exception = None) -> str:
        """Handle dependency errors with informative messages."""
        if dependency == "numpy":
            return f"NumPy is required for {feature}. Please install it with: pip install numpy"
        elif dependency == "sounddevice":
            return f"SoundDevice is required for {feature}. Please install it with: pip install sounddevice"
        elif dependency == "openwakeword":
            return f"OpenWakeWord is required for {feature}. Please install it with: pip install openwakeword"
        elif dependency == "faster_whisper":
            return f"Faster Whisper is required for {feature}. Please install it with: pip install faster-whisper"
        elif dependency == "piper":
            return f"Piper is required for {feature}. Please install it with: pip install piper-tts"
        elif dependency == "pyttsx3":
            return f"Pyttsx3 is required for {feature}. Please install it with: pip install pyttsx3"
        else:
            error_msg = str(error) if error else "Unknown error"
            return f"{dependency} is required for {feature}. Error: {error_msg}"

    def runtime_status(self) -> dict[str, object]:
        dependencies = voice_dependency_status()
        missing = sorted(name for name, ok in dependencies.items() if not ok and name in {"numpy", "sounddevice", "faster_whisper", "openwakeword"})
        return {
            "available": self.available(),
            "dependencies": dependencies,
            "missing_dependencies": missing,
            "speech_backend": self._speech_backend,
            "whisper_device": self._whisper_device,
            "whisper_compute_type": self._whisper_compute_type,
        }

    def _tts_backend_preference(self) -> str:
        backend = settings.tts_backend
        if backend in {"auto", "piper", "pyttsx3", "groq"}:
            return backend
        return "auto"

    def _piper_model_path(self) -> Path:
        return settings.piper_voice_dir / f"{settings.piper_voice_name}.onnx"

    def _piper_config_path(self) -> Path:
        return settings.piper_voice_dir / f"{settings.piper_voice_name}.onnx.json"

    def _piper_available(self) -> bool:
        """Check if Piper TTS is available with all required dependencies."""
        if PiperVoice is None:
            LOGGER.debug("Piper TTS is not available: PiperVoice module not found")
            return False
        if winsound is None:
            LOGGER.debug("Piper TTS is not available: winsound module not found")
            return False
        model_path = self._piper_model_path()
        config_path = self._piper_config_path()
        if not model_path.exists():
            LOGGER.debug("Piper TTS is not available: model file not found at %s", model_path)
            return False
        if not config_path.exists():
            LOGGER.debug("Piper TTS is not available: config file not found at %s", config_path)
            return False
        return True

    def _can_speak(self) -> bool:
        backend = self._tts_backend_preference()
        if backend == "piper":
            return self._piper_available()
        if backend == "pyttsx3":
            return pyttsx3 is not None
        if backend == "groq":
            return self._groq_tts_available()
        return self._piper_available() or pyttsx3 is not None or self._groq_tts_available()

    def _groq_tts_available(self) -> bool:
        return Groq is not None and winsound is not None and bool(settings.groq_api_key) and settings.groq_tts_enabled

    def _set_speech_backend(self, backend: str) -> None:
        if self._speech_backend == backend:
            return
        self._speech_backend = backend
        if backend == "pyttsx3":
            LOGGER.info("Speech backend ready: pyttsx3")
        elif backend == "piper":
            LOGGER.info("Speech backend ready: piper (%s)", settings.piper_voice_name)
        elif backend == "groq":
            LOGGER.warning("Speech backend switched to Groq TTS using %s (%s).", settings.groq_tts_model, settings.groq_tts_voice)
        elif backend == "none":
            LOGGER.warning("Speech backend is unavailable.")

    def _init_local_engine(self):
        if pyttsx3 is None:
            LOGGER.debug("Pyttsx3 is not available for speech synthesis")
            return None
            
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", settings.speech_rate)
            self.engine = engine
            self._set_speech_backend("pyttsx3")
            return engine
        except Exception as e:
            LOGGER.error("Failed to initialize pyttsx3 engine: %s", e)
            if self.engine:
                try:
                    self.engine.stop()
                except Exception:
                    pass
                self.engine = None
            return None

    def _get_piper_voice(self):
        if not self._piper_available():
            raise RuntimeError(f"Piper voice files are unavailable: {self._piper_model_path()} and {self._piper_config_path()}")
        if self._piper_voice is None:
            self._piper_voice = PiperVoice.load(self._piper_model_path(), config_path=self._piper_config_path())
        return self._piper_voice

    def _get_groq_client(self):
        if not self._groq_tts_available():
            raise RuntimeError("Groq TTS fallback is unavailable.")
        if self._groq_client is None:
            self._groq_client = Groq(api_key=settings.groq_api_key)
        return self._groq_client

    @staticmethod
    def _chunk_speech_text(text: str, max_chars: int = 180) -> list[str]:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []
        if len(normalized) <= max_chars:
            return [normalized]
        chunks: list[str] = []
        current = ""
        segments = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", normalized) if segment.strip()]
        if not segments:
            segments = [normalized]

        def _flush() -> None:
            nonlocal current
            if current:
                chunks.append(current)
                current = ""

        for segment in segments:
            if len(segment) <= max_chars:
                candidate = segment if not current else f"{current} {segment}"
                if len(candidate) <= max_chars:
                    current = candidate
                else:
                    _flush()
                    current = segment
                continue
            words = segment.split()
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if len(candidate) <= max_chars:
                    current = candidate
                    continue
                _flush()
                current = word
        _flush()
        return chunks

    @staticmethod
    def _normalize_tts_error(exc: Exception) -> str:
        message = str(exc).strip()
        lowered = message.lower()
        if "model_terms_required" in lowered or "requires terms acceptance" in lowered:
            return ("Groq TTS needs a one-time terms acceptance for canopylabs/orpheus-v1-english at https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english")
        if "model_decommissioned" in lowered:
            return "The configured Groq TTS model is no longer available."
        return message or exc.__class__.__name__

    def _play_with_piper(self, text: str) -> bool:
        # Check dependencies before proceeding
        if winsound is None:
            raise RuntimeError(self._handle_dependency_error("winsound", "Piper speech playback"))
        if PiperVoice is None:
            raise RuntimeError(self._handle_dependency_error("piper", "Piper speech synthesis"))
            
        try:
            voice = self._get_piper_voice()
        except Exception as e:
            LOGGER.error("Failed to get Piper voice: %s", e)
            raise RuntimeError(f"Failed to initialize Piper voice: {e}") from e
            
        self._speech_cache_dir.mkdir(parents=True, exist_ok=True)
        chunks = self._chunk_speech_text(text)
        if not chunks:
            return False
            
        success = True
        request = self._current_speech_request
        cancel = request.cancel if request else None
        for chunk in chunks:
            if cancel and cancel.is_set():
                if request:
                    request.interrupted = True
                return False
            audio_path = self._speech_cache_dir / f"{uuid.uuid4().hex}.wav"
            try:
                with wave.open(str(audio_path), "wb") as wav_file:
                    voice.synthesize_wav(chunk, wav_file)
                if not self._play_generated_audio(audio_path, cancel):
                    if request:
                        request.interrupted = True
                    return False
            except Exception as e:
                LOGGER.error("Failed to play Piper speech: %s", e)
                success = False
                break
            finally:
                if audio_path.exists():
                    try:
                        audio_path.unlink()
                    except Exception:
                        LOGGER.debug("Unable to remove temporary speech file %s", audio_path, exc_info=True)
                        
        if success:
            self._set_speech_backend("piper")
        return success

    def _play_with_groq(self, text: str) -> bool:
        # Check dependencies before proceeding
        if winsound is None:
            raise RuntimeError(self._handle_dependency_error("winsound", "Groq speech playback"))
        if Groq is None:
            raise RuntimeError(self._handle_dependency_error("groq", "Groq TTS service"))
            
        try:
            client = self._get_groq_client()
        except Exception as e:
            LOGGER.error("Failed to get Groq client: %s", e)
            raise RuntimeError(f"Failed to initialize Groq client: {e}") from e
            
        self._speech_cache_dir.mkdir(parents=True, exist_ok=True)
        chunks = self._chunk_speech_text(text)
        if not chunks:
            return False
            
        success = True
        request = self._current_speech_request
        cancel = request.cancel if request else None
        for chunk in chunks:
            if cancel and cancel.is_set():
                if request:
                    request.interrupted = True
                return False
            response = None
            audio_path = self._speech_cache_dir / f"{uuid.uuid4().hex}.wav"
            try:
                response = client.audio.speech.create(model=settings.groq_tts_model, voice=settings.groq_tts_voice, input=chunk, response_format="wav")
                response.write_to_file(audio_path)
                if not self._play_generated_audio(audio_path, cancel):
                    if request:
                        request.interrupted = True
                    return False
            except Exception as exc:
                LOGGER.error("Failed to generate/play Groq speech: %s", exc)
                success = False
                raise RuntimeError(self._normalize_tts_error(exc)) from exc
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                if audio_path.exists():
                    try:
                        audio_path.unlink()
                    except Exception:
                        LOGGER.debug("Unable to remove temporary speech file %s", audio_path, exc_info=True)
                        
        if success:
            self._set_speech_backend("groq")
        return success

    def _play_generated_audio(self, audio_path: Path, cancel: threading.Event | None) -> bool:
        if winsound is None:
            raise RuntimeError(self._handle_dependency_error("winsound", "speech playback"))
        try:
            with wave.open(str(audio_path), "rb") as wav_file:
                frames = wav_file.getnframes()
                rate = wav_file.getframerate() or 1
                duration = max(0.05, frames / float(rate))
        except Exception:
            duration = 0.6

        winsound.PlaySound(str(audio_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        started = time.monotonic()
        while time.monotonic() - started < duration:
            if cancel and cancel.is_set():
                winsound.PlaySound(None, winsound.SND_PURGE)
                return False
            time.sleep(0.02)
        winsound.PlaySound(None, winsound.SND_PURGE)
        return True

    def _speech_loop(self) -> None:
        engine = None
        com_ready = False
        try:
            ctypes.windll.ole32.CoInitialize(None)
            com_ready = True
        except Exception:
            LOGGER.debug("COM initialization for speech did not complete cleanly.", exc_info=True)

        preferred_backend = self._tts_backend_preference()
        if preferred_backend == "pyttsx3":
            try:
                engine = self._init_local_engine()
            except Exception:
                LOGGER.exception("Speech engine initialization failed.")
                self.engine = None
                engine = None
                self._set_speech_backend("none")

        while not self._speech_stop.is_set():
            try:
                request = self._speech_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if request is None:
                break
            try:
                self._current_speech_request = request
                self._speech_active.set()
                self._speech_interrupt.clear()
                if preferred_backend == "piper" and self._piper_available():
                    request.ok = self._play_with_piper(request.text)
                elif preferred_backend == "groq" and self._groq_tts_available():
                    request.ok = self._play_with_groq(request.text)
                elif engine is not None:
                    try:
                        engine.say(request.text)
                        engine.runAndWait()
                        request.ok = not (request.cancel and request.cancel.is_set())
                        request.interrupted = bool(request.cancel and request.cancel.is_set())
                        self._set_speech_backend("pyttsx3")
                    except Exception:
                        LOGGER.exception("Speech playback failed.")
                        request.ok = False
                        try:
                            engine.stop()
                        except Exception:
                            pass
                        engine = None
                        self.engine = None
                        try:
                            engine = self._init_local_engine()
                        except Exception:
                            LOGGER.exception("Speech engine reinitialization failed.")
                            self.engine = None
                            engine = None
                        if engine is not None:
                            engine.say(request.text)
                            engine.runAndWait()
                            request.ok = not (request.cancel and request.cancel.is_set())
                            request.interrupted = bool(request.cancel and request.cancel.is_set())
                            self._set_speech_backend("pyttsx3")
                        elif self._piper_available():
                            request.ok = self._play_with_piper(request.text)
                        elif self._groq_tts_available():
                            request.ok = self._play_with_groq(request.text)
                        else:
                            raise RuntimeError("Speech engine is unavailable.")
                elif self._piper_available():
                    request.ok = self._play_with_piper(request.text)
                elif self._groq_tts_available():
                    request.ok = self._play_with_groq(request.text)
                elif pyttsx3 is not None:
                    engine = self._init_local_engine()
                    engine.say(request.text)
                    engine.runAndWait()
                    request.ok = not (request.cancel and request.cancel.is_set())
                    request.interrupted = bool(request.cancel and request.cancel.is_set())
                    self._set_speech_backend("pyttsx3")
                else:
                    raise RuntimeError("Speech engine is unavailable.")
            except Exception:
                LOGGER.exception("Speech playback failed.")
                request.ok = False
                try:
                    if engine:
                        engine.stop()
                except Exception:
                    pass
                self.engine = None
                engine = None
                self._set_speech_backend("none")
            finally:
                self._current_speech_request = None
                self._speech_active.clear()
                self._speech_interrupt.clear()
                if request.done:
                    request.done.set()

        try:
            if engine:
                engine.stop()
        except Exception:
            pass
        finally:
            self.engine = None
            if com_ready:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    LOGGER.debug("COM shutdown for speech did not complete cleanly.", exc_info=True)

    def _ensure_speech_thread(self) -> None:
        if not self._can_speak() or self._speech_stop.is_set():
            return
        with self._speech_lock:
            if self._speech_thread and self._speech_thread.is_alive():
                return
            thread = threading.Thread(target=self._speech_loop, daemon=True, name="jarvis-tts")
            thread.start()
            self._speech_thread = thread

    def reset_speech(self) -> None:
        self.stop_speaking()
        self._speech_stop.set()
        with self._speech_lock:
            thread = self._speech_thread
            self._speech_queue.put(None)
        if thread and thread.is_alive():
            thread.join(timeout=settings.speech_timeout_seconds)
        with self._speech_lock:
            self._speech_thread = None
            self._speech_queue = queue.Queue()
        self._speech_stop = threading.Event()
        self.engine = None

    def reset_voice_models(self) -> None:
        with self._model_lock:
            self._wake_model = None
            self._whisper_model = None
            self._wake_ready.clear()
            self._whisper_ready.clear()

    def self_heal(self) -> dict:
        from system.health import run_self_heal
        report = run_self_heal(self, policy="safe_auto")
        actions = [repair.message for repair in report.repairs if repair.changed]
        issues = [repair.message for repair in report.repairs if not repair.ok]
        return {"ok": report.ok, "message": report.message, "actions": actions, "issues": issues}

    def say(self, text: str, wait: bool = True, retry_on_timeout: bool = True) -> bool:
        message = text.strip()
        if not message or not self._can_speak():
            return False
        self._ensure_speech_thread()
        if not self._speech_thread or not self._speech_thread.is_alive():
            return False
        done = threading.Event() if wait else None
        request = SpeechRequest(text=message, done=done, cancel=threading.Event())
        self._speech_queue.put(request)
        if done:
            if not done.wait(timeout=settings.speech_timeout_seconds):
                LOGGER.warning("Speech playback timed out.")
                if retry_on_timeout:
                    self.reset_speech()
                    return self.say("I had trouble speaking that response.", wait=wait, retry_on_timeout=False)
                return False
            if request.interrupted:
                return False
            if not request.ok and retry_on_timeout:
                LOGGER.warning("Speech playback failed; retrying with fallback message.")
                self.reset_speech()
                return self.say("I had trouble speaking that response.", wait=wait, retry_on_timeout=False)
            return request.ok
        return True

    def is_speaking(self) -> bool:
        return self._speech_active.is_set()

    def stop_speaking(self) -> bool:
        interrupted = False
        self._speech_interrupt.set()
        request = self._current_speech_request
        if request and request.cancel:
            request.cancel.set()
            request.interrupted = True
            interrupted = True
        if self.engine is not None:
            try:
                self.engine.stop()
                interrupted = True
            except Exception:
                LOGGER.debug("Failed to stop pyttsx3 speech cleanly.", exc_info=True)
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
                interrupted = True or interrupted
            except Exception:
                LOGGER.debug("Failed to purge winsound playback cleanly.", exc_info=True)
        return interrupted

    def play_listening_cue(self) -> bool:
        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                return True
            except Exception:
                LOGGER.debug("Listening cue beep failed.", exc_info=True)
        return self.say("Listening.", wait=False)

    def preload(self) -> None:
        if not self.available():
            return
        try:
            if download_models is not None:
                download_models([settings.wakeword_model_name])
            self._get_wake_model()
            self._wake_ready.set()
        except Exception:
            pass
        try:
            self._get_whisper_model()
            self._whisper_ready.set()
        except Exception:
            pass

    def _duration_for_mode(self, mode: CaptureMode) -> float:
        if mode == "confirmation":
            return max(1.0, settings.confirmation_phrase_seconds)
        if mode == "command":
            return max(1.5, settings.command_phrase_seconds)
        return max(0.5, settings.wake_phrase_seconds)

    def _silence_for_mode(self, mode: CaptureMode) -> float:
        if mode == "confirmation":
            return max(0.2, settings.confirmation_silence_seconds)
        if mode == "command":
            return max(0.35, settings.command_silence_seconds)
        return max(0.15, settings.wake_silence_seconds)

    def _get_wake_model(self) -> WakeModel:
        if not self.available():
            missing_deps = self._check_critical_dependencies()
            if missing_deps:
                dep_list = ", ".join(missing_deps)
                raise RuntimeError(f"Offline wake runtime is unavailable. Missing dependencies: {dep_list}. "
                                 f"Please install with: pip install {' '.join(missing_deps)}")
            else:
                raise RuntimeError("Offline wake runtime is unavailable.")
                
        # Check specific dependency
        if WakeModel is None:
            raise RuntimeError(self._handle_dependency_error("openwakeword", "wake word detection"))
            
        with self._model_lock:
            if self._wake_model is None:
                try:
                    self._wake_model = WakeModel(wakeword_models=[settings.wakeword_model_name], vad_threshold=settings.wakeword_vad_threshold, inference_framework="onnx")
                    self._wake_ready.set()
                except Exception as e:
                    raise RuntimeError(f"Failed to initialize wake word model: {e}") from e
            return self._wake_model

    def _get_whisper_model(self) -> WhisperModel:
        if not self.available():
            missing_deps = self._check_critical_dependencies()
            if missing_deps:
                dep_list = ", ".join(missing_deps)
                raise RuntimeError(f"Offline transcription runtime is unavailable. Missing dependencies: {dep_list}. "
                                 f"Please install with: pip install {' '.join(missing_deps)}")
            else:
                raise RuntimeError("Offline transcription runtime is unavailable.")
                
        # Check specific dependency
        if WhisperModel is None:
            raise RuntimeError(self._handle_dependency_error("faster_whisper", "speech transcription"))
            
        with self._model_lock:
            if self._whisper_model is None:
                try:
                    local_only = settings.whisper_local_files_only
                    if settings.whisper_download_root.exists():
                        local_only = True
                    self._whisper_model = WhisperModel(settings.whisper_model_name, device=settings.whisper_device, compute_type=settings.whisper_compute_type, download_root=str(settings.whisper_download_root), local_files_only=local_only)
                    self._whisper_device = settings.whisper_device
                    self._whisper_compute_type = settings.whisper_compute_type
                except Exception as e:
                    LOGGER.warning(f"Failed to initialize Whisper model with {settings.whisper_device}: {e}")
                    self._switch_whisper_to_cpu()
                self._whisper_ready.set()
            return self._whisper_model

    def _switch_whisper_to_cpu(self) -> WhisperModel:
        self._whisper_model = WhisperModel(settings.whisper_model_name, device="cpu", compute_type="int8", download_root=str(settings.whisper_download_root), local_files_only=settings.whisper_download_root.exists() or settings.whisper_local_files_only)
        self._whisper_device = "cpu"
        self._whisper_compute_type = "int8"
        self._persist_whisper_cpu_fallback()
        self._whisper_ready.set()
        return self._whisper_model

    def _persist_whisper_cpu_fallback(self) -> None:
        if self._cpu_fallback_persisted:
            return
        try:
            env_path = Path(__file__).resolve().parent.parent / ".env"
            _set_env_value(env_path, "JARVIS_WHISPER_DEVICE", "cpu")
            _set_env_value(env_path, "JARVIS_WHISPER_COMPUTE_TYPE", "int8")
            self._cpu_fallback_persisted = True
            LOGGER.warning("Persisted Whisper CPU fallback after runtime failure.")
        except Exception:
            LOGGER.exception("Failed to persist Whisper CPU fallback.")

    @staticmethod
    def _is_gpu_runtime_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(token in message for token in ("cublas64_12.dll", "cudnn", "cuda", "execution provider", "cannot be loaded"))

    def _open_stream(self, chunk_size: int, level_callback: Callable[[float], None] | None = None) -> tuple:
        if not self.available():
            missing_deps = self._check_critical_dependencies()
            if missing_deps:
                dep_list = ", ".join(missing_deps)
                raise RuntimeError(f"Offline voice runtime is unavailable. Missing dependencies: {dep_list}. "
                                 f"Please install with: pip install {' '.join(missing_deps)}")
            else:
                raise RuntimeError("Offline voice runtime is unavailable.")
        
        # Check individual dependencies before use
        if np is None:
            raise RuntimeError(self._handle_dependency_error("numpy", "audio processing"))
        if sd is None:
            raise RuntimeError(self._handle_dependency_error("sounddevice", "audio streaming"))
            
        audio_queue: queue.Queue = queue.Queue()

        def _callback(indata: bytes, frames: int, callback_time: object, status: object) -> None:
            del frames, callback_time, status
            try:
                frame = np.frombuffer(bytes(indata), dtype=np.int16).copy()
                normalized = frame.astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(np.square(normalized)))) if normalized.size else 0.0
                if level_callback:
                    level_callback(min(1.0, rms * 8.0))
                audio_queue.put((frame, rms))
            except Exception as e:
                if isinstance(e, AttributeError) and "numpy" in str(e).lower():
                    LOGGER.error("NumPy error in audio callback: %s", e)
                else:
                    LOGGER.debug("Audio callback error: %s", e, exc_info=True)

        try:
            stream = sd.RawInputStream(samplerate=self._sample_rate, blocksize=chunk_size, dtype="int16", channels=self._channels, callback=_callback)
            return audio_queue, stream
        except Exception as e:
            raise RuntimeError(f"Failed to initialize audio stream: {e}") from e

    def _transcribe(self, audio, hotwords: str) -> str:
        pcm = audio.astype(np.float32) / 32768.0
        model = self._get_whisper_model()
        try:
            segments, _ = model.transcribe(pcm, language="en", beam_size=settings.whisper_beam_size, best_of=settings.whisper_best_of, vad_filter=True, hotwords=hotwords, condition_on_previous_text=False, no_speech_threshold=settings.whisper_no_speech_threshold, without_timestamps=True)
        except Exception as exc:
            if self._whisper_device != "cpu" and self._is_gpu_runtime_error(exc):
                model = self._switch_whisper_to_cpu()
                segments, _ = model.transcribe(pcm, language="en", beam_size=settings.whisper_beam_size, best_of=settings.whisper_best_of, vad_filter=True, hotwords=hotwords, condition_on_previous_text=False, no_speech_threshold=settings.whisper_no_speech_threshold, without_timestamps=True)
            else:
                raise
        return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()

    def capture(self, mode: CaptureMode, level_callback: Callable[[float], None] | None = None) -> CaptureResult:
        if mode == "wake_word":
            return CaptureResult(status="error", error="Wake capture uses the dedicated wake listener.")
        try:
            audio_queue, stream = self._open_stream(self._capture_chunk_size, level_callback=level_callback)
        except Exception as exc:
            return CaptureResult(status="error", error=str(exc))
        duration = self._duration_for_mode(mode)
        silence_limit = self._silence_for_mode(mode)
        started = time.monotonic()
        last_voice_at = started
        saw_voice = False
        frames = []
        hotwords = settings.whisper_hotwords
        if mode == "confirmation":
            hotwords = f"{hotwords}, yes, no, cancel, continue, stop"
        try:
            with stream:
                while True:
                    if time.monotonic() - started >= duration:
                        break
                    try:
                        frame, rms = audio_queue.get(timeout=0.08)
                    except queue.Empty:
                        continue
                    frames.append(frame)
                    if rms >= settings.voice_min_rms:
                        saw_voice = True
                        last_voice_at = time.monotonic()
                    if saw_voice and (time.monotonic() - last_voice_at) >= silence_limit:
                        break
            if not saw_voice or not frames:
                return CaptureResult(status="timeout")
            audio = np.concatenate(frames)
            transcript = self._transcribe(audio, hotwords=hotwords)
            if transcript:
                return CaptureResult(status="ok", text=transcript)
            return CaptureResult(status="empty")
        except Exception as exc:
            return CaptureResult(status="error", error=str(exc))

    def capture_command(self, level_callback: Callable[[float], None] | None = None) -> CaptureResult:
        return self.capture("command", level_callback=level_callback)

    def capture_confirmation(self, level_callback: Callable[[float], None] | None = None) -> CaptureResult:
        return self.capture("confirmation", level_callback=level_callback)

    def listen_once(self, timeout: int = 5, phrase_time_limit: int = 10, level_callback: Callable[[float], None] | None = None) -> str:
        del timeout, phrase_time_limit
        result = self.capture_command(level_callback=level_callback)
        if result.ok:
            return result.text
        if result.status == "error":
            raise RuntimeError(result.error or "Voice capture failed.")
        return ""

    def start_wake_listener(self, callback: Callable[[str], None], on_error: Callable[[str], None] | None = None) -> threading.Thread | None:
        if not settings.wake_word_enabled or not self.available():
            return None
        with self._wake_lock:
            if self._wake_thread and self._wake_thread.is_alive():
                return self._wake_thread
            self._wake_stop.clear()

        def _runner() -> None:
            last_error_report = 0.0
            try:
                if not self._wake_ready.is_set():
                    self._wake_ready.wait(timeout=0.1)
                if not self._wake_ready.is_set():
                    while not self._wake_stop.is_set() and not self._wake_ready.wait(timeout=0.25):
                        pass
                wake_model = self._get_wake_model()
                audio_queue, stream = self._open_stream(self._wake_chunk_size)
            except Exception as exc:
                if on_error:
                    on_error(str(exc))
                return
            wake_model.reset()
            with stream:
                while not self._wake_stop.is_set():
                    try:
                        frame, _ = audio_queue.get(timeout=0.08)
                    except queue.Empty:
                        continue
                    try:
                        scores = wake_model.predict(frame)
                        if not scores:
                            continue
                        top_score = max(scores.values())
                        if top_score > 0.1:
                            LOGGER.debug("Wake word top score: %.3f (threshold: %.3f)", top_score, settings.wakeword_threshold)
                        if top_score >= settings.wakeword_threshold:
                            LOGGER.info("Wake word detected! Score: %.3f", top_score)
                            callback(settings.wake_word)
                            wake_model.reset()
                            time.sleep(0.12)
                    except Exception as exc:
                        if on_error:
                            now = time.monotonic()
                            if now - last_error_report > 6.0:
                                on_error(str(exc))
                                last_error_report = now
                        wake_model.reset()

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        with self._wake_lock:
            self._wake_thread = thread
        return thread

    def stop_wake_listener(self) -> None:
        with self._wake_lock:
            thread = self._wake_thread
            self._wake_stop.set()
        if thread and thread.is_alive():
            thread.join(timeout=0.8)
        with self._wake_lock:
            self._wake_thread = None

    def shutdown(self) -> None:
        self.stop_wake_listener()
        self.stop_speaking()
        self._speech_stop.set()
        with self._speech_lock:
            thread = self._speech_thread
            self._speech_queue.put(None)
        if thread and thread.is_alive():
            thread.join(timeout=settings.speech_timeout_seconds)
        with self._speech_lock:
            self._speech_thread = None
