from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
VOICE_DIR = BASE_DIR / "voice_models"
VOICE_DIR.mkdir(exist_ok=True)
MEMORY_DIR = BASE_DIR / "memory"
MEMORY_DIR.mkdir(exist_ok=True)
RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_DIR.mkdir(exist_ok=True)


@dataclass(slots=True)
class Settings:
    app_name: str = "Jarvis"
    ollama_base_url: str = os.getenv("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("JARVIS_OLLAMA_MODEL", "qwen2.5:7b")
    groq_api_key: str | None = os.getenv("GROQ_API_KEY")
    groq_model: str = os.getenv("JARVIS_GROQ_MODEL", "llama-3.3-70b-versatile")
    groq_tts_enabled: bool = os.getenv("JARVIS_GROQ_TTS_ENABLED", "false").lower() == "true"
    groq_tts_model: str = os.getenv("JARVIS_GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
    groq_tts_voice: str = os.getenv("JARVIS_GROQ_TTS_VOICE", "hannah")
    tts_backend: str = os.getenv("JARVIS_TTS_BACKEND", "auto").lower()
    piper_voice_name: str = os.getenv("JARVIS_PIPER_VOICE", "en_US-lessac-medium")
    piper_voice_dir: Path = VOICE_DIR / "piper"
    planner_provider: str = os.getenv("JARVIS_PLANNER_PROVIDER", "ollama")
    command_hotkey: str = os.getenv("JARVIS_COMMAND_HOTKEY", "ctrl+shift+j")
    push_to_talk_hotkey: str = os.getenv("JARVIS_PUSH_TO_TALK_HOTKEY", "ctrl+shift+space")
    wake_word_enabled: bool = os.getenv("JARVIS_WAKE_WORD_ENABLED", "true").lower() == "true"
    wake_word: str = os.getenv("JARVIS_WAKE_WORD", "jarvis")
    speech_rate: int = int(os.getenv("JARVIS_SPEECH_RATE", "180"))
    speech_timeout_seconds: float = float(os.getenv("JARVIS_SPEECH_TIMEOUT_SECONDS", "15"))
    wake_phrase_seconds: float = float(os.getenv("JARVIS_WAKE_PHRASE_SECONDS", "0.6"))
    command_phrase_seconds: float = float(os.getenv("JARVIS_COMMAND_PHRASE_SECONDS", "6.0"))
    confirmation_phrase_seconds: float = float(os.getenv("JARVIS_CONFIRMATION_PHRASE_SECONDS", "3.5"))
    voice_min_rms: float = float(os.getenv("JARVIS_VOICE_MIN_RMS", "0.006"))
    wake_silence_seconds: float = float(os.getenv("JARVIS_WAKE_SILENCE_SECONDS", "0.25"))
    command_silence_seconds: float = float(os.getenv("JARVIS_COMMAND_SILENCE_SECONDS", "0.65"))
    confirmation_silence_seconds: float = float(os.getenv("JARVIS_CONFIRMATION_SILENCE_SECONDS", "0.5"))
    offline_stt_enabled: bool = os.getenv("JARVIS_OFFLINE_STT_ENABLED", "true").lower() == "true"
    wakeword_model_name: str = os.getenv("JARVIS_WAKEWORD_MODEL_NAME", "jarvis")
    wakeword_threshold: float = float(os.getenv("JARVIS_WAKEWORD_THRESHOLD", "0.45"))
    wakeword_vad_threshold: float = float(os.getenv("JARVIS_WAKEWORD_VAD_THRESHOLD", "0.35"))
    whisper_model_name: str = os.getenv("JARVIS_WHISPER_MODEL_NAME", "small.en")
    whisper_device: str = os.getenv("JARVIS_WHISPER_DEVICE", "cuda")
    whisper_compute_type: str = os.getenv("JARVIS_WHISPER_COMPUTE_TYPE", "float16")
    whisper_download_root: Path = VOICE_DIR / "faster_whisper"
    whisper_local_files_only: bool = os.getenv("JARVIS_WHISPER_LOCAL_FILES_ONLY", "false").lower() == "true"
    whisper_beam_size: int = int(os.getenv("JARVIS_WHISPER_BEAM_SIZE", "2"))
    whisper_best_of: int = int(os.getenv("JARVIS_WHISPER_BEST_OF", "2"))
    whisper_no_speech_threshold: float = float(os.getenv("JARVIS_WHISPER_NO_SPEECH_THRESHOLD", "0.45"))
    whisper_hotwords: str = os.getenv(
        "JARVIS_WHISPER_HOTWORDS",
        "jarvis, chrome, edge, vscode, visual studio code, powershell, notepad, explorer",
    )
    self_heal_enabled: bool = os.getenv("JARVIS_SELF_HEAL_ENABLED", "true").lower() == "true"
    self_heal_restart_delay_seconds: float = float(os.getenv("JARVIS_SELF_HEAL_RESTART_DELAY_SECONDS", "1.0"))
    self_heal_max_attempts: int = int(os.getenv("JARVIS_SELF_HEAL_MAX_ATTEMPTS", "3"))
    stop_grace_seconds: float = float(os.getenv("JARVIS_STOP_GRACE_SECONDS", "1.5"))
    logs_path: Path = LOG_DIR / "jarvis.log"
    memory_profile_path: Path = MEMORY_DIR / "profile.json"
    conversation_history_path: Path = MEMORY_DIR / "conversation_history.json"
    mcp_servers_path: Path = BASE_DIR / "mcp_servers.json"
    agent_state_path: Path = MEMORY_DIR / "agents.json"
    agent_step_limit: int = int(os.getenv("JARVIS_AGENT_STEP_LIMIT", "1"))
    agent_poll_interval_seconds: float = float(os.getenv("JARVIS_AGENT_POLL_INTERVAL_SECONDS", "0.1"))
    conversation_history_limit: int = int(os.getenv("JARVIS_CONVERSATION_HISTORY_LIMIT", "24"))
    conversation_prompt_turn_limit: int = int(os.getenv("JARVIS_CONVERSATION_PROMPT_TURN_LIMIT", "8"))
    semantic_index_path: Path = MEMORY_DIR / "semantic"
    semantic_embedding_model: str = os.getenv("JARVIS_SEMANTIC_EMBEDDING_MODEL", "")
    semantic_top_k: int = int(os.getenv("JARVIS_SEMANTIC_TOP_K", "5"))
    semantic_chunk_size: int = int(os.getenv("JARVIS_SEMANTIC_CHUNK_SIZE", "3"))
    semantic_chunk_overlap: int = int(os.getenv("JARVIS_SEMANTIC_CHUNK_OVERLAP", "1"))
    startup_folder: Path = Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_script_name: str = os.getenv("JARVIS_STARTUP_SCRIPT_NAME", "jarvis-startup.vbs")
    startup_auto_install: bool = os.getenv("JARVIS_STARTUP_AUTO_INSTALL", "true").lower() == "true"
    startup_launch_delay_seconds: float = float(os.getenv("JARVIS_STARTUP_LAUNCH_DELAY_SECONDS", "8"))
    hud_edge_thickness: int = int(os.getenv("JARVIS_HUD_EDGE_THICKNESS", "26"))
    hud_edge_inset: int = int(os.getenv("JARVIS_HUD_EDGE_INSET", "8"))
    hud_debug_visible: bool = os.getenv("JARVIS_HUD_DEBUG_VISIBLE", "false").lower() == "true"
    hud_startup_pulse_seconds: float = float(os.getenv("JARVIS_HUD_STARTUP_PULSE_SECONDS", "2.5"))
    allowed_terminal_commands: tuple[str, ...] = (
        "git status",
        "git pull",
        "docker ps",
        "docker compose up",
        "docker compose down",
        "npm install",
        "npm run dev",
        "npm test",
        "python -m http.server",
        "dir",
        "ls",
        "code .",
    )
    app_registry: dict[str, str] = field(
        default_factory=lambda: {
            "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            "vscode": "code",
            "notepad": "notepad.exe",
            "explorer": "explorer.exe",
            "powershell": "powershell.exe",
            "cmd": "cmd.exe",
            "spotify": r"C:\Users\%USERNAME%\AppData\Roaming\Spotify\Spotify.exe",
        }
    )
    terminal_workflows: dict[str, str] = field(
        default_factory=lambda: {
            "project_here": "code .",
            "git_status": "git status",
            "docker_ps": "docker ps",
        }
    )
    mcp_servers: dict[str, dict[str, object]] = field(default_factory=dict)


settings = Settings()
