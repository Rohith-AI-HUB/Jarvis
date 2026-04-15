# 🔴 Jarvis - Your Personal AI Assistant

**A Windows-first local AI agent runtime with voice controls, persistent memory, and safe execution**

[Features](#-features) • [Quick Start](#-quick-start) • [Installation](#-installation) • [Usage](#-usage) • [Architecture](#-architecture)

## ✨ Features

### 🎯 Core Capabilities

- **Local Everything** — No cloud dependencies, runs fully on your machine
- **Voice-First Interface** — Wake word detection with `Hey Jarvis`
- **Persistent Memory** — Remembers conversations and user preferences
- **Safe Execution** — Guarded action execution with user confirmation
- **Ollama/Groq Integration** — Choose your preferred LLM backend
- **System Tray App** — Always accessible with quick controls

### 🎨 User Experience

- **Summon HUD** — Centered overlay for quick commands
- **Control Center** — Runtime monitoring and diagnostics
- **Interactive Modes** — Voice, typed, or direct command execution
- **Natural Conversations** — Context-aware follow-ups

### 🔧 Technical Excellence

- **FAISS Semantic Memory** — Efficient semantic search and retrieval
- **Faster Whisper STT** — Fast speech-to-text
- **OpenWakeWord Detection** — Wake word recognition
- **Piper TTS** — Text-to-speech responses
- **Startup Integration** — Auto-launch on Windows login

## 🚀 Quick Start

```bash
# Clone and setup
git clone <repo-url>
cd Jarvis

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt

# Get Ollama ready
ollama pull llama3.1:8b

# Launch Jarvis in voice mode
python app.py --voice
```

**Say "Hey Jarvis, what can you do?" to get started!**

## 📦 Installation

### Prerequisites

- Python 3.10+
- Windows 10/11
- Ollama (for local LLM) or Groq API key

### Step-by-Step Setup

1. **Create Virtual Environment**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```
2. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```
3. **Get LLM Model**
   ```bash
   ollama pull llama3.1:8b
   ```
4. **First Launch**
   ```bash
   python app.py
   ```
   > **Note:** First launch will automatically download voice models (~100MB total)

## 🎮 Usage

### Launch Modes

| Command | Mode | Description |
| --- | --- | --- |
| `python app.py` | Tray | Default persistent tray mode |
| `python app.py --voice` | Voice | Voice-first interactive mode |
| `python app.py --interactive` | Interactive | Typed command prompt |
| `python app.py "open chrome"` | Direct | Execute single command |
| `python app.py --tray` | Tray | Explicit tray mode |

### 🎤 Voice Commands

#### Basic Interactions

```text
Hey Jarvis, open Chrome
Hey Jarvis, what's the weather?
Hey Jarvis, my name is Rohit
Hey Jarvis, remember this
Hey Jarvis, what do you remember?
```

#### System Control

```text
Hey Jarvis, open Notepad
Hey Jarvis, open VS Code
Hey Jarvis, check system health
Hey Jarvis, fix yourself
```

#### Memory Management

```text
Hey Jarvis, when I say code editor I mean VS Code
Hey Jarvis, forget that
Hey Jarvis, clear memory
```

## 🏗️ Architecture

### Project Structure

```text
Jarvis/
├── core/
│   ├── planner.py
│   ├── executor.py
│   ├── agents.py
│   ├── conversation.py
│   └── capabilities.py
├── interface/
│   ├── control_center.py
│   ├── overlay.py
│   └── theme.py
├── voice/
│   ├── voice.py
│   └── transcript.py
├── system/
│   ├── tools.py
│   ├── config.py
│   ├── diagnostics.py
│   ├── health.py
│   └── mcp_runtime.py
├── memory/
│   ├── semantic/
│   ├── conversation_history.json
│   └── profile.json
├── voice_models/
├── logs/
├── app.py
└── requirements.txt
```

### Data Flow

```text
Voice Input ─► Wake Detection ─► STT ─► Transcript Normalization
                                              │
                                              ▼
User Input  ───────► Planner ───► Executor ───► Tools
                                              │
                                              ▼
TTS Output  ◄─────── Result  ◄─── Execution ◄── Confirmation
                                              │
                                              ▼
                                        Memory Update
```

## 🛠️ Configuration

Edit `system/config.py` to customize:

- LLM provider (Ollama/Groq) and model
- Hotkeys and shortcuts
- Allowed terminal commands
- Voice model paths
- Memory settings

## 🔒 Security

- All processing happens locally
- No data sent externally (unless using Groq)
- Environment variables stored in `.env`
- Guarded execution prevents accidental damage

## 📝 License

MIT License — free to use and modify

Made with ❤️ for Windows

Built for productivity, powered by AI
