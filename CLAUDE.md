# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build and Run Commands

### Core Application
```bash
uv sync                # Install Python dependencies (first time, takes ~6-7 min)
uv run main.py         # Run AstrBot, exposes API on http://localhost:6185
uv run ruff format .   # Format code
uv run ruff check .    # Lint code
```

### Dashboard (Vue.js WebUI)
```bash
cd dashboard
pnpm install           # Install dependencies (first time)
pnpm dev               # Development server on http://localhost:3000
pnpm build             # Production build
```

### Testing
```bash
uv run pytest                     # Run all tests
uv run pytest tests/test_xxx.py   # Run a single test file
uv run pytest -v                  # Verbose output
```

## Architecture Overview

AstrBot is a multi-platform LLM chatbot framework with a plugin-based architecture. The main components are:

### Core Lifecycle (`astrbot/core/core_lifecycle.py`)
The central orchestrator that initializes and manages all components: ProviderManager, PlatformManager, ConversationManager, PluginManager, PipelineScheduler, EventBus, and PersonaManager.

### Plugin System ("Stars")
- **Location**: `astrbot/core/star/` - "Star" is the internal name for plugins
- **Built-in plugins**: `astrbot/builtin_stars/` - includes astrbot, builtin_commands, session_controller, telegram_keyboard_demo
- **User plugins**: `data/plugins/` - user-installed plugins
- **API**: Use `astrbot.api.star.register()` to register plugins, `Star` class for plugin base
- **Handlers**: Plugins register handlers for events via `star_handlers_registry` (see `EventType` enum)

### Platform Adapters (`astrbot/core/platform/sources/`)
Platform-specific adapters for messaging services:
- QQ/OneBot (`aiocqhttp/`), Telegram (`telegram/`), Discord (`discord/`), Slack (`slack/`)
- WeChat (`wecom/`, `weixin_official_account/`), Feishu/Lark (`lark/`), DingTalk (`dingtalk/`)
- LINE (`line/`), Misskey (`misskey/`), Mattermost (`mattermost/`), Satori (`satori/`)
- WebChat (`webchat/`) for built-in web chat interface

Each adapter implements `Platform` base class and handles platform-specific message events.

### LLM Provider System (`astrbot/core/provider/sources/`)
Model service integrations:
- OpenAI-compatible (`openai_source.py`, `anthropic_source.py`, `gemini_source.py`)
- Chinese providers (`zhipu_source.py`, `dashscope/`, `kimi_code_source.py`)
- TTS/STT services (`openai_tts_api_source.py`, `whisper_api_source.py`, etc.)
- LLMOps platforms: Dify, Coze, Alibaba Bailian (`astrbot/core/agent/runners/`)

### Agent System (`astrbot/core/agent/`)
- `agent.py`: Agent dataclass with tools, instructions, hooks
- `runners/`: Different agent execution strategies (tool loop, Dify, Coze, DeerFlow, Dashscope)
- `context/`: Context management including token counting, truncation, compression
- `tool_executor.py`: Executes function tools from LLM responses
- `mcp_client.py`: MCP (Model Context Protocol) integration

### Pipeline Scheduler (`astrbot/core/pipeline/scheduler.py`)
Processes incoming message events through a pipeline of handlers before dispatching to agents.

### Computer/Sandbox (`astrbot/core/computer/`)
Agent sandbox for isolated execution of Python code, shell commands, file operations, and browser actions:
- `olayer/`: Operation layers (python, shell, browser, filesystem)
- `booters/`: Sandbox bootstrapping (local, shipyard, shipyard_neo, boxlite)

## Key Conventions

1. **Path handling**: Use `pathlib.Path` instead of string paths. Use `astrbot.core.utils.astrbot_path` helpers for AstrBot-specific paths (`get_astrbot_data_path`, `get_astrbot_temp_path`, `get_astrbot_plugin_path`, etc.)

2. **Plugin development**: Plugin code changes must NOT be committed to the AstrBot root repository - only commit within the plugin's own directory.

3. **Code style**: Run `ruff format .` and `ruff check .` before committing. Use conventional commit messages (e.g., `feat: add feature`, `fix: resolve bug`).

4. **Comments**: Use English for all new comments.

5. **No report files**: Do not add report/summary markdown files (e.g., `xxx_SUMMARY.md`).

6. **Plugin documentation**: When modifying plugins, update `CHANGELOG.md`, `README.md`, `metadata.yaml`, and config schemas.

## Important Files

- `main.py`: Application entry point, handles dashboard download and lifecycle startup
- `astrbot/core/config/default.py`: Contains VERSION constant and default configurations
- `astrbot/api/`: Public API for plugin development (`star/`, `event/`, `platform/`, `provider/`, `message_components.py`)
- `astrbot/cli/`: CLI commands (`init`, `run`, `plug`, `conf`)