# Jarvis - Your Personal AI Assistant

Windows-first local AI agent runtime with voice controls, persistent memory, and guarded execution.

## Features

### Core Capabilities

- Local-first runtime with Ollama or Groq integration
- Automatic provider fallback between Ollama and Groq
- Voice-first interaction with wake word detection
- Persistent memory for conversation and user preferences
- Guarded execution for system-changing actions
- System tray runtime with quick controls

### User Experience

- Edge Aura HUD with a clearly visible persistent border on all four screen edges
- Control Center for runtime monitoring and diagnostics
- Voice, typed, and direct command execution modes
- Natural follow-ups with speech interruption support

### Technical Stack

- Faster Whisper STT
- OpenWakeWord detection
- Piper, pyttsx3, or Groq-backed TTS
- Semantic memory and conversation history
- Windows startup integration

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
ollama pull llama3.1:8b
python app.py --voice
```

Say `Hey Jarvis, what can you do?` to start, and interrupt Jarvis mid-response to ask a follow-up.

## Usage

| Command | Mode | Description |
| --- | --- | --- |
| `python app.py` | Tray | Default persistent tray mode |
| `python app.py --voice` | Voice | Voice-first tray runtime |
| `python app.py --interactive` | Interactive | Typed command prompt |
| `python app.py "open chrome"` | Direct | Execute a single command |
| `python app.py --tray` | Tray | Explicit tray mode |

### Example Voice Commands

```text
Hey Jarvis, open Chrome
Hey Jarvis, focus Notepad
Hey Jarvis, type hello world in Notepad
Hey Jarvis, press control l in Chrome
Hey Jarvis, maximize VS Code
Hey Jarvis, what do you remember?
```

## Architecture

### Project Structure

```text
Jarvis/
|- core/
|- interface/
|- voice/
|- system/
|- memory/
|- logs/
|- app.py
`- requirements.txt
```

### Runtime Flow

```text
Voice Input -> Wake Detection -> STT -> Transcript Normalization
User Input  -> Planner -> Executor -> Tools
TTS Output  <- Result <- Execution <- Confirmation
      ^
      |-- User barge-in can interrupt speech and begin a new follow-up
```

## Notes

- The current HUD is single-monitor oriented.
- Set `JARVIS_HUD_DEBUG_VISIBLE=true` to force a brighter high-contrast border for visibility testing.
- In-app work focuses on opening/focusing apps, typing text, sending shortcuts, and window controls.
- Arbitrary screen clicking remains out of the default voice workflow.
