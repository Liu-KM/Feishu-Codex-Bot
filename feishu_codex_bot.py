from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    MentionEvent,
    P2ImMessageReceiveV1,
)


LOG = logging.getLogger("feishu_codex_bot")
ATTACH_PREFIX = "ATTACH:"
VALID_EFFORTS = {"low", "medium", "high", "xhigh"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".csv": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_text_content(raw_content: Optional[str]) -> str:
    if not raw_content:
        return ""
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content
    return str(payload.get("text", "")).strip()


def format_text_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def chunk_text(text: str, max_len: int = 3500) -> List[str]:
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    current = text
    while current:
        split_at = current.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(current[:split_at].rstrip())
        current = current[split_at:].lstrip()
    return [chunk for chunk in chunks if chunk]


def now_ts() -> float:
    return time.time()


def strip_leading_mentions(text: str, mentions: Optional[List[MentionEvent]]) -> str:
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    cleaned = re.sub(r"^(<at[^>]*>.*?</at>\s*)+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(@_user_[^\s]+\s*)+", "", cleaned)

    for mention in mentions or []:
        if mention.name:
            pattern = r"^@" + re.escape(mention.name) + r"[\s\u00a0]*"
            cleaned = re.sub(pattern, "", cleaned, count=1)

    return cleaned.strip()


def parse_attach_lines(text: str) -> Tuple[str, List[str]]:
    keep: List[str] = []
    attachments: List[str] = []
    for line in text.splitlines():
        if line.strip().startswith(ATTACH_PREFIX):
            attachments.append(line.split(ATTACH_PREFIX, 1)[1].strip())
        else:
            keep.append(line)
    return "\n".join(keep).strip(), attachments


def looks_like_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def infer_file_type(path: Path) -> str:
    return FILE_TYPE_MAP.get(path.suffix.lower(), "stream")


@dataclass
class SessionState:
    chat_id: str
    chat_type: str
    session_id: Optional[str] = None
    display_name: str = ""
    default_model: str = ""
    default_effort: str = ""
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "SessionState":
        return cls(
            chat_id=str(data.get("chat_id", "")),
            chat_type=str(data.get("chat_type", "")),
            session_id=data.get("session_id") or None,
            display_name=str(data.get("display_name", "")),
            default_model=str(data.get("default_model", "")),
            default_effort=str(data.get("default_effort", "")),
            updated_at=float(data.get("updated_at", 0.0)),
        )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._sessions: Dict[str, SessionState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            LOG.exception("failed to load session store")
            return

        if not isinstance(payload, dict):
            return

        for chat_id, data in payload.items():
            if isinstance(data, dict):
                self._sessions[chat_id] = SessionState.from_dict(data)

    def get(self, chat_id: str, chat_type: str, default_model: str, default_effort: str) -> SessionState:
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                session = SessionState(
                    chat_id=chat_id,
                    chat_type=chat_type,
                    default_model=default_model,
                    default_effort=default_effort,
                    updated_at=now_ts(),
                )
                self._sessions[chat_id] = session
                self._save_unlocked()
            return session

    def update(self, session: SessionState) -> None:
        with self._lock:
            session.updated_at = now_ts()
            self._sessions[session.chat_id] = session
            self._save_unlocked()

    def list_sessions(self) -> List[SessionState]:
        with self._lock:
            sessions = list(self._sessions.values())
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def reset_session_id(self, chat_id: str) -> None:
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None:
                return
            session.session_id = None
            session.updated_at = now_ts()
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {chat_id: asdict(session) for chat_id, session in self._sessions.items()}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class ProcessedMessageCache:
    def __init__(self, max_items: int = 2048) -> None:
        self._max_items = max_items
        self._items: List[str] = []
        self._set: Set[str] = set()
        self._lock = threading.Lock()

    def add_if_new(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self._set:
                return False
            self._items.append(message_id)
            self._set.add(message_id)
            while len(self._items) > self._max_items:
                old = self._items.pop(0)
                self._set.discard(old)
            return True


class FeishuCodexBot:
    def __init__(self) -> None:
        app_id = require_env("APP_ID")
        app_secret = require_env("APP_SECRET")

        self.workspace = Path(os.getenv("CODEX_WORKSPACE", ".")).expanduser().resolve()
        if not self.workspace.exists():
            raise RuntimeError(f"Workspace does not exist: {self.workspace}")

        self.default_model = os.getenv("CODEX_MODEL", "").strip()
        self.default_effort = os.getenv("CODEX_REASONING_EFFORT", "").strip().lower()
        if self.default_effort and self.default_effort not in VALID_EFFORTS:
            raise RuntimeError(f"Invalid CODEX_REASONING_EFFORT: {self.default_effort}")

        self.codex_timeout = int(os.getenv("CODEX_TIMEOUT_SECONDS", "1800"))
        self.codex_sandbox = os.getenv("CODEX_SANDBOX", "workspace-write").strip()
        self.trigger_prefix = os.getenv("BOT_TRIGGER_PREFIX", "/codex").strip()
        self.allow_p2p_without_prefix = os.getenv("ALLOW_P2P_WITHOUT_PREFIX", "true").lower() == "true"
        self.allowed_open_ids = {
            item.strip()
            for item in os.getenv("ALLOWED_OPEN_IDS", "").split(",")
            if item.strip()
        }
        self.bot_open_id = os.getenv("BOT_OPEN_ID", "").strip()
        self.auto_send_attachments = os.getenv("AUTO_SEND_ATTACHMENTS", "true").lower() == "true"
        session_store_path = Path(os.getenv("SESSION_STORE_PATH", ".feishu_codex_sessions.json")).expanduser()
        if not session_store_path.is_absolute():
            session_store_path = self.workspace / session_store_path

        log_level_name = os.getenv("BOT_LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_level_name, logging.INFO)
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        self.sessions = SessionStore(session_store_path)
        self.processed_messages = ProcessedMessageCache()
        self.chat_locks: Dict[str, threading.Lock] = {}
        self.chat_locks_guard = threading.Lock()

        self.event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self.handle_message) \
            .build()

    def run(self) -> None:
        LOG.info("starting Feishu Codex bot")
        LOG.info("workspace=%s", self.workspace)
        LOG.info("session_store=%s", self.sessions.path)
        lark.ws.Client(
            os.environ["APP_ID"],
            os.environ["APP_SECRET"],
            event_handler=self.event_handler,
            log_level=lark.LogLevel.INFO,
        ).start()

    def handle_message(self, data: P2ImMessageReceiveV1) -> None:
        event = data.event
        if not event or not event.message or not event.sender:
            LOG.warning("received malformed event")
            return

        message = event.message
        sender = event.sender

        if sender.sender_type != "user":
            return

        if message.message_id and not self.processed_messages.add_if_new(message.message_id):
            return

        user_open_id = ""
        if sender.sender_id:
            user_open_id = sender.sender_id.open_id or ""

        if self.allowed_open_ids and user_open_id not in self.allowed_open_ids:
            self.reply_text(message.chat_id, "You are not allowed to use this bot.")
            return

        if message.message_type != "text":
            self.reply_text(message.chat_id, "Only text messages are supported right now.")
            return

        raw_text = parse_text_content(message.content)
        if not raw_text:
            return

        triggered, text = self.normalize_trigger(message.chat_type or "", raw_text, message.mentions)
        if not triggered or not text:
            return

        session = self.sessions.get(
            chat_id=message.chat_id or "",
            chat_type=message.chat_type or "",
            default_model=self.default_model,
            default_effort=self.default_effort,
        )

        handled = self.handle_control_command(message.chat_id or "", message.chat_type or "", text, session)
        if handled:
            return

        self.run_task_async(
            chat_id=message.chat_id or "",
            user_text=text,
            session=session,
        )

    def normalize_trigger(
        self,
        chat_type: str,
        raw_text: str,
        mentions: Optional[List[MentionEvent]],
    ) -> Tuple[bool, str]:
        text = raw_text.strip()
        if not text:
            return False, ""

        if text.startswith(self.trigger_prefix):
            return True, text[len(self.trigger_prefix):].strip()

        if chat_type == "p2p":
            if self.allow_p2p_without_prefix:
                return True, text
            return False, ""

        if self.is_bot_mentioned(mentions):
            return True, strip_leading_mentions(text, mentions)

        return False, ""

    def is_bot_mentioned(self, mentions: Optional[List[MentionEvent]]) -> bool:
        if not mentions:
            return False
        if not self.bot_open_id:
            return True
        for mention in mentions:
            if mention.id and mention.id.open_id == self.bot_open_id:
                return True
        return False

    def handle_control_command(self, chat_id: str, chat_type: str, text: str, session: SessionState) -> bool:
        if text in {"/help", "help"}:
            self.reply_text(chat_id, self.help_text(chat_type))
            return True

        if text == "/ping":
            self.reply_text(chat_id, "pong")
            return True

        if text == "/status":
            self.reply_text(chat_id, self.status_text(session))
            return True

        if text == "/sessions":
            self.reply_text(chat_id, self.sessions_text(current_chat_id=chat_id))
            return True

        if text == "/reset":
            self.sessions.reset_session_id(chat_id)
            session.session_id = None
            self.reply_text(chat_id, "Codex session reset. Next task will start a new session.")
            return True

        if text.startswith("/session name "):
            session.display_name = text[len("/session name "):].strip()
            self.sessions.update(session)
            self.reply_text(chat_id, f"Session name set to: {session.display_name}")
            return True

        if text == "/session":
            self.reply_text(chat_id, self.status_text(session))
            return True

        if text.startswith("/model "):
            session.default_model = text[len("/model "):].strip()
            self.sessions.update(session)
            self.reply_text(chat_id, f"Default model set to: {session.default_model or '(global default)'}")
            return True

        if text == "/model":
            self.reply_text(chat_id, f"Current model: {session.default_model or self.default_model or '(Codex default)'}")
            return True

        if text.startswith("/effort "):
            effort = text[len("/effort "):].strip().lower()
            if effort not in VALID_EFFORTS:
                self.reply_text(chat_id, "Effort must be one of: low, medium, high, xhigh")
                return True
            session.default_effort = effort
            self.sessions.update(session)
            self.reply_text(chat_id, f"Default effort set to: {effort}")
            return True

        if text == "/effort":
            self.reply_text(chat_id, f"Current effort: {session.default_effort or self.default_effort or '(Codex default)'}")
            return True

        if text.startswith("/send "):
            requested = text[len("/send "):].strip()
            self.send_path_command(chat_id, requested)
            return True

        return False

    def help_text(self, chat_type: str) -> str:
        if chat_type == "p2p":
            trigger_line = "当前是私聊：你可以直接发自然语言，不需要先输入 /codex。"
            examples = [
                "直接提问示例：帮我看看当前仓库有哪些未提交修改",
                "直接提问示例：请排查测试失败原因，并给出修复方案",
            ]
        else:
            trigger_line = "当前是群聊：请先 @bot，或者以 /codex 开头，避免误触发。"
            examples = [
                "群聊示例：@bot 帮我看一下这个仓库的未提交修改",
                "群聊示例：/codex 帮我总结一下当前目录结构",
            ]

        lines = [
            "飞书 Codex Bot 帮助",
            trigger_line,
            "",
            "一、基础用法",
            "1. 直接提问",
            "作用：让 Codex 在当前 session 里继续处理你的任务。",
            *examples,
            "",
            "2. /ping",
            "作用：检查 bot 是否在线。",
            "示例：/ping",
            "",
            "3. /help",
            "作用：查看这份帮助说明。",
            "示例：/help",
            "",
            "二、Session 相关",
            "4. /status",
            "作用：查看当前会话绑定的 chat_id、Codex session、默认模型、默认强度、工作目录。",
            "示例：/status",
            "",
            "5. /sessions",
            "作用：查看这个 bot 已记录的所有 session 列表，包括私聊和群聊。",
            "示例：/sessions",
            "",
            "6. /session name <名字>",
            "作用：给当前 session 起一个你看得懂的名字，方便管理。",
            "示例：/session name 飞书手机工作台",
            "",
            "7. /reset",
            "作用：重置当前 chat 绑定的 Codex session。下一条任务会新开一个 session。",
            "示例：/reset",
            "",
            "三、模型与推理强度",
            "8. /model <模型名>",
            "作用：设置当前 session 默认使用的模型。",
            "示例：/model gpt-5.4",
            "",
            "9. /effort <low|medium|high|xhigh>",
            "作用：设置当前 session 默认推理强度。",
            "示例：/effort high",
            "",
            "10. /ask --model <模型> --effort <强度> <问题>",
            "作用：只对这一条请求临时覆盖模型或强度，不改 session 默认值。",
            "示例：/ask --effort xhigh 深入分析这个并发死锁问题",
            "示例：/ask --model gpt-5.4 --effort medium 帮我总结这个仓库的测试结构",
            "",
            "四、文件与图片",
            "11. /send <路径>",
            "作用：把工作目录里的本地文件或图片发回飞书。",
            "示例：/send README_BOT.md",
            "示例：/send outputs/result.png",
            "",
            "12. 自动发送附件",
            "作用：如果 Codex 在最终回复里附上 ATTACH: /绝对路径，bot 会尝试自动上传该文件或图片。",
            "示例：ATTACH: /home/liu/feishu_codex/outputs/report.md",
            "",
            "五、补充说明",
            "每个私聊或群聊都会绑定一个独立 session。",
            "新开一个群聊，通常就会有一个新的 session。",
            "群聊里推荐总是用 @bot。",
            "文件路径必须在当前工作目录之下才允许发送。",
        ]
        return "\n".join(lines)

    def status_text(self, session: SessionState) -> str:
        lines = [
            f"chat_id: {session.chat_id}",
            f"chat_type: {session.chat_type}",
            f"session_name: {session.display_name or '(unset)'}",
            f"codex_session_id: {session.session_id or '(new session on next task)'}",
            f"default_model: {session.default_model or self.default_model or '(Codex default)'}",
            f"default_effort: {session.default_effort or self.default_effort or '(Codex default)'}",
            f"workspace: {self.workspace}",
        ]
        return "\n".join(lines)

    def sessions_text(self, current_chat_id: str) -> str:
        sessions = self.sessions.list_sessions()
        if not sessions:
            return "No sessions recorded yet."

        lines = ["Known sessions:"]
        for session in sessions[:20]:
            marker = "*" if session.chat_id == current_chat_id else "-"
            model = session.default_model or self.default_model or "(default)"
            effort = session.default_effort or self.default_effort or "(default)"
            name = session.display_name or "(unnamed)"
            lines.append(
                f"{marker} {name} | {session.chat_type} | chat_id={session.chat_id} | "
                f"codex_session_id={session.session_id or '(new)'} | model={model} | effort={effort}"
            )
        if len(sessions) > 20:
            lines.append(f"... {len(sessions) - 20} more")
        return "\n".join(lines)

    def run_task_async(self, chat_id: str, user_text: str, session: SessionState) -> None:
        model_override = ""
        effort_override = ""
        prompt = user_text

        if user_text.startswith("/ask "):
            parsed = self.parse_ask_command(user_text[len("/ask "):].strip())
            if parsed is None:
                self.reply_text(chat_id, "Invalid /ask syntax. Example: /ask --effort xhigh fix the failing test")
                return
            model_override, effort_override, prompt = parsed

        if not prompt.strip():
            self.reply_text(chat_id, "Empty request.")
            return

        lock = self.get_chat_lock(chat_id)
        if not lock.acquire(blocking=False):
            self.reply_text(chat_id, "This session is busy. Wait for the current task to finish.")
            return

        threading.Thread(
            target=self.process_codex_task,
            args=(chat_id, session, prompt, model_override, effort_override, lock),
            daemon=True,
        ).start()

    def get_chat_lock(self, chat_id: str) -> threading.Lock:
        with self.chat_locks_guard:
            lock = self.chat_locks.get(chat_id)
            if lock is None:
                lock = threading.Lock()
                self.chat_locks[chat_id] = lock
            return lock

    def parse_ask_command(self, content: str) -> Optional[Tuple[str, str, str]]:
        try:
            parts = shlex.split(content)
        except ValueError:
            return None

        model = ""
        effort = ""
        prompt_parts: List[str] = []
        index = 0
        while index < len(parts):
            part = parts[index]
            if part == "--model" and index + 1 < len(parts):
                model = parts[index + 1]
                index += 2
                continue
            if part == "--effort" and index + 1 < len(parts):
                effort = parts[index + 1].lower()
                index += 2
                continue
            prompt_parts = parts[index:]
            break

        if effort and effort not in VALID_EFFORTS:
            return None
        prompt = " ".join(prompt_parts).strip()
        if not prompt:
            return None
        return model, effort, prompt

    def process_codex_task(
        self,
        chat_id: str,
        session: SessionState,
        user_text: str,
        model_override: str,
        effort_override: str,
        lock: threading.Lock,
    ) -> None:
        try:
            effective_model = model_override or session.default_model or self.default_model
            effective_effort = effort_override or session.default_effort or self.default_effort

            running_line = "Task received. Running Codex..."
            details: List[str] = []
            if effective_model:
                details.append(f"model={effective_model}")
            if effective_effort:
                details.append(f"effort={effective_effort}")
            if details:
                running_line += " " + " ".join(details)
            self.reply_text(chat_id, running_line)

            prompt = self.build_codex_prompt(user_text)
            reply, session_id = self.run_codex(prompt, session.session_id, effective_model, effective_effort)
            if session_id and session.session_id != session_id:
                session.session_id = session_id
                self.sessions.update(session)

            visible_reply, attachments = parse_attach_lines(reply)
            if visible_reply:
                self.reply_text(chat_id, visible_reply)

            if self.auto_send_attachments and attachments:
                self.send_attachments(chat_id, attachments)
        finally:
            lock.release()

    def build_codex_prompt(self, user_text: str) -> str:
        return (
            f"You are working inside {self.workspace}.\n"
            "The user is talking to you through a Feishu bot.\n"
            "Be concise, but include concrete outcomes.\n"
            "If you modify files, mention the paths you changed.\n"
            "If you run commands, summarize the important output.\n"
            f"If you want the bot to send files or images back, end your final message with one or more lines starting with {ATTACH_PREFIX} followed by an absolute path.\n"
            "Only reference files that already exist.\n"
            f"Only reference files under {self.workspace}.\n\n"
            f"User request:\n{user_text}\n"
        )

    def run_codex(
        self,
        prompt: str,
        session_id: Optional[str],
        model: str,
        effort: str,
    ) -> Tuple[str, Optional[str]]:
        cmd = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            self.codex_sandbox,
            "-C",
            str(self.workspace),
        ]
        if model:
            cmd.extend(["-m", model])
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
        if session_id:
            cmd.extend(["resume", session_id, prompt])
        else:
            cmd.append(prompt)

        LOG.info("running codex command: resume=%s workspace=%s", bool(session_id), self.workspace)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.codex_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"Codex timed out after {self.codex_timeout} seconds.", session_id
        except Exception as exc:
            LOG.exception("codex invocation failed")
            return f"Failed to start Codex: {exc}", session_id

        reply, discovered_session_id = self.extract_codex_result(completed.stdout)
        effective_session_id = discovered_session_id or session_id
        if completed.returncode == 0 and reply:
            return reply, effective_session_id

        parts: List[str] = []
        if reply:
            parts.append(reply)
        if completed.stderr.strip():
            parts.append(f"stderr:\n{completed.stderr.strip()}")
        if not parts:
            parts.append(f"Codex exited with code {completed.returncode}.")
        return "\n\n".join(parts), effective_session_id

    def extract_codex_result(self, stdout: str) -> Tuple[str, Optional[str]]:
        last_agent_message = ""
        session_id = None

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "thread.started":
                session_id = event.get("thread_id") or session_id
                continue

            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    last_agent_message = str(item["text"]).strip()

        if last_agent_message:
            return last_agent_message, session_id
        return "Codex finished without a final message.", session_id

    def reply_text(self, chat_id: Optional[str], text: str) -> None:
        if not chat_id:
            return

        normalized = text.strip() or "(empty response)"
        for chunk in chunk_text(normalized):
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(format_text_content(chunk))
                    .uuid(str(uuid.uuid4()))
                    .build()
                ) \
                .build()
            response = self.client.im.v1.message.create(request)
            if not response.success():
                LOG.error(
                    "failed to reply text, code=%s msg=%s log_id=%s",
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )

    def send_path_command(self, chat_id: str, requested_path: str) -> None:
        path = self.resolve_attachment_path(requested_path)
        if path is None:
            self.reply_text(chat_id, "Path must be inside the configured workspace and point to a file.")
            return
        error = self.send_single_attachment(chat_id, path)
        if error:
            self.reply_text(chat_id, error)

    def send_attachments(self, chat_id: str, raw_paths: List[str]) -> None:
        errors: List[str] = []
        sent: Set[Path] = set()
        for raw_path in raw_paths:
            path = self.resolve_attachment_path(raw_path)
            if path is None:
                errors.append(f"Attachment rejected: {raw_path}")
                continue
            if path in sent:
                continue
            sent.add(path)
            error = self.send_single_attachment(chat_id, path)
            if error:
                errors.append(error)

        if errors:
            self.reply_text(chat_id, "\n".join(errors))

    def resolve_attachment_path(self, raw_path: str) -> Optional[Path]:
        candidate = Path(raw_path.strip())
        if not candidate.is_absolute():
            candidate = (self.workspace / candidate).resolve()
        else:
            candidate = candidate.resolve()

        try:
            candidate.relative_to(self.workspace)
        except ValueError:
            return None

        if not candidate.exists() or not candidate.is_file():
            return None
        return candidate

    def send_single_attachment(self, chat_id: str, path: Path) -> Optional[str]:
        try:
            if looks_like_image(path):
                image_key = self.upload_image(path)
                if not image_key:
                    return f"Failed to upload image: {path}"
                return self.send_image_message(chat_id, image_key)

            file_key = self.upload_file(path)
            if not file_key:
                return f"Failed to upload file: {path}"
            return self.send_file_message(chat_id, file_key)
        except Exception as exc:
            LOG.exception("failed to send attachment")
            return f"Failed to send attachment {path}: {exc}"

    def upload_image(self, path: Path) -> Optional[str]:
        with path.open("rb") as image_file:
            request = CreateImageRequest.builder() \
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(image_file)
                    .build()
                ) \
                .build()
            response = self.client.im.v1.image.create(request)

        if not response.success():
            LOG.error(
                "failed to upload image, code=%s msg=%s log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return None
        return response.data.image_key if response.data else None

    def upload_file(self, path: Path) -> Optional[str]:
        with path.open("rb") as file_obj:
            request = CreateFileRequest.builder() \
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(infer_file_type(path))
                    .file_name(path.name)
                    .file(file_obj)
                    .build()
                ) \
                .build()
            response = self.client.im.v1.file.create(request)

        if not response.success():
            LOG.error(
                "failed to upload file, code=%s msg=%s log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return None
        return response.data.file_key if response.data else None

    def send_image_message(self, chat_id: str, image_key: str) -> Optional[str]:
        content = json.dumps({"image_key": image_key}, ensure_ascii=False)
        return self.send_structured_message(chat_id, "image", content)

    def send_file_message(self, chat_id: str, file_key: str) -> Optional[str]:
        content = json.dumps({"file_key": file_key}, ensure_ascii=False)
        return self.send_structured_message(chat_id, "file", content)

    def send_structured_message(self, chat_id: str, msg_type: str, content: str) -> Optional[str]:
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .uuid(str(uuid.uuid4()))
                .build()
            ) \
            .build()
        response = self.client.im.v1.message.create(request)
        if response.success():
            return None
        LOG.error(
            "failed to send %s message, code=%s msg=%s log_id=%s",
            msg_type,
            response.code,
            response.msg,
            response.get_log_id(),
        )
        return f"Failed to send {msg_type} message."


def main() -> None:
    load_env_file(Path(".env"))
    bot = FeishuCodexBot()
    bot.run()


if __name__ == "__main__":
    main()
