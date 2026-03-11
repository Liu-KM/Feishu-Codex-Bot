# Feishu Codex Bot

This project runs a Feishu bot that receives text messages over Feishu WebSocket events, calls local `codex exec`, keeps one Codex session per Feishu chat, and can send text, files, or images back to the same chat.

## What it does

- Private chats can trigger Codex directly, unless disabled by config.
- Group chats trigger when the bot is mentioned or the message starts with `/codex`.
- Each Feishu chat keeps its own persisted Codex session id in `.feishu_codex_sessions.json` by default.
- The bot supports per-chat model and reasoning-effort defaults.
- If Codex ends its final reply with `ATTACH: /absolute/path`, the bot can upload that file or image back to Feishu automatically.

## Files in this directory

- `feishu_codex_bot.py`: main bot process
- `.env.example`: environment variable template
- `requirements.txt`: pinned Python package list for deployment
- `scripts/deploy.sh`: local bootstrap script for a fresh machine
- `VERSION`: current starter version
- `CHANGELOG.md`: human-readable release history
- `systemd/feishu-codex-bot.service.example`: service unit template
- `README_BOT_ZH.md`: Chinese README
- `oapi-sdk-python/`: Feishu Python SDK submodule used by this bot

## Requirements

- Python 3.10+
- Local `codex` CLI available in `PATH`
- A Feishu app with bot capability enabled
- WebSocket / long connection event delivery enabled in Feishu Open Platform

## Clone

```bash
git clone --recurse-submodules <your-private-repo-url>
```

If you already cloned without submodules, run:

```bash
git submodule update --init --recursive
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install setuptools wheel requests requests_toolbelt pycryptodome websockets httpx
pip install --no-build-isolation ./oapi-sdk-python
```

The SDK in `./oapi-sdk-python` installs more reliably with `--no-build-isolation` than with editable mode.

## Configure

```bash
cp .env.example .env
```

Minimum required values:

- `APP_ID`
- `APP_SECRET`
- `CODEX_WORKSPACE`

Common optional values:

- `CODEX_MODEL`: default model for new chat sessions
- `CODEX_REASONING_EFFORT`: `low`, `medium`, `high`, or `xhigh`
- `CODEX_TIMEOUT_SECONDS`: Codex subprocess timeout
- `CODEX_SANDBOX`: passed to `codex exec --sandbox`
- `BOT_TRIGGER_PREFIX`: default `/codex`
- `ALLOW_P2P_WITHOUT_PREFIX`: if `true`, direct messages can trigger without `/codex`
- `ALLOWED_OPEN_IDS`: comma-separated Feishu `open_id` allowlist
- `BOT_OPEN_ID`: recommended for strict mention detection in group chats
- `AUTO_SEND_ATTACHMENTS`: if `true`, upload `ATTACH:` files automatically
- `SESSION_STORE_PATH`: defaults to `.feishu_codex_sessions.json` under `CODEX_WORKSPACE`
- `BOT_LOG_LEVEL`: default `INFO`

Important detail: attachment paths must resolve to files inside `CODEX_WORKSPACE`.

## Run

```bash
source .venv/bin/activate
python feishu_codex_bot.py
```

At startup the bot logs the active workspace and the session store path, then opens a Feishu WebSocket connection.

## Quick deployment

For a fresh machine, the starter now includes:

```bash
./scripts/deploy.sh
```

That script creates `.venv`, installs Python dependencies, installs the local Feishu SDK, and creates `.env` from `.env.example` when needed.

## Feishu setup

In Feishu Open Platform:

1. Create an internal app.
2. Enable the bot for the app.
3. Enable event subscription with WebSocket / long connection mode.
4. Subscribe to `im.message.receive_v1`.
5. Grant IM permissions needed to send messages, upload files, and upload images.
6. Install or publish the app to the tenant so users can chat with it.

Console labels may change, but those capabilities are what this bot needs.

## How triggering works

- Private chat:
  - plain text works when `ALLOW_P2P_WITHOUT_PREFIX=true`
  - `/codex <request>` also works
- Group chat:
  - `@bot <request>`
  - `/codex <request>`

Only incoming text messages are supported. Non-text inbound messages are rejected with a short reply.

## Built-in commands

- `/help`: show help text
- `/ping`: health check
- `/status`: show current chat id, Codex session id, defaults, and workspace
- `/sessions`: list known stored chat sessions
- `/session`: same as status for the current chat
- `/session name <name>`: set a readable name for this chat session
- `/reset`: clear the current chat's stored Codex session id
- `/model`: show current default model
- `/model <name>`: set default model for this chat
- `/effort`: show current default reasoning effort
- `/effort <low|medium|high|xhigh>`: set default effort for this chat
- `/ask --model <name> --effort <level> <request>`: one-off override without changing stored defaults
- `/send <path>`: upload a file or image from inside `CODEX_WORKSPACE`

Only one Codex task runs at a time per Feishu chat. If a chat already has a running task, the bot replies that the session is busy.

## Attachment workflow

Codex can request outbound attachments by ending its final message with lines like:

```text
ATTACH: /home/liu/feishu_codex/outputs/report.md
ATTACH: /home/liu/feishu_codex/outputs/diagram.png
```

Rules:

- only existing files are accepted
- paths must stay inside `CODEX_WORKSPACE`
- image suffixes such as `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.webp` are sent as Feishu images
- other files are uploaded with Feishu file APIs

## Runtime behavior

- The bot invokes Codex with `codex exec --json`.
- If a stored session id exists for the chat, the bot uses `codex exec ... resume <session_id>`.
- The bot only forwards Codex's final agent message back to Feishu.
- Long replies are split into chunks before sending.

## Current limits

- Incoming messages must be text.
- The bot does not stream intermediate Codex progress back to Feishu.
- Session storage is local JSON, not a shared database.
- Attachment sending is limited to files under the configured workspace.

## Version management

- `VERSION` stores the current starter version.
- `CHANGELOG.md` records repository-level changes.
- `.gitignore` excludes local secrets, logs, virtualenvs, and session state from git.
- `.gitmodules` tracks the Feishu SDK submodule.

## Official references

- Feishu Python SDK setup:
  - https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/preparations-before-development
- Feishu Python SDK event handling:
  - https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/handle-events
- Local SDK README in this repo:
  - `oapi-sdk-python/README.md`
