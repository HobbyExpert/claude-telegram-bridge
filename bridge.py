#!/usr/bin/env python3
"""Claude Code Telegram Bridge — multi-session, control Claude Code from Telegram."""

import asyncio
import html as html_mod
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv(Path(__file__).parent / ".env")

# --- Config -----------------------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_IDS = [
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
]
DEFAULT_CWD = os.getenv("DEFAULT_CWD", str(Path.home() / "Sites"))
DEFAULT_BUDGET = float(os.getenv("DEFAULT_BUDGET_USD", "2.0"))
DEFAULT_MAX_TURNS = int(os.getenv("DEFAULT_MAX_TURNS", "10"))
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT_SECONDS", "300"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "sonnet")
SITES_DIR = os.getenv("SITES_DIR", str(Path.home() / "Sites"))

STREAM_EDIT_INTERVAL = 1.5
STREAM_MIN_DELTA = 50
SESSION_IDLE_TIMEOUT = int(os.getenv("SESSION_IDLE_TIMEOUT", "3600"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "60"))

DANGEROUS_PATTERNS = [
    (r"\brm\s+(-r|-rf|-fr)\b", "recursive delete"),
    (r"\bgit\s+push\s+(-f|--force)\b", "force push"),
    (r"\bgit\s+reset\s+--hard\b", "hard reset"),
    (r"\bdrop\s+(table|database)\b", "drop table/database"),
    (r"\btruncate\s+table\b", "truncate table"),
    (r"\bchmod\s+777\b", "chmod 777"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+if=\b", "raw disk write"),
    (r"\bgit\s+branch\s+-D\b", "force delete branch"),
]


# --- Session Slot -----------------------------------------------------------
class SessionSlot:
    """Represents one named Claude Code session."""

    def __init__(
        self,
        name: str,
        cwd: str = DEFAULT_CWD,
        model: str = DEFAULT_MODEL,
        budget: float = DEFAULT_BUDGET,
        max_turns: int = DEFAULT_MAX_TURNS,
    ):
        self.name = name
        self.cwd = cwd
        self.model = model
        self.budget = budget
        self.max_turns = max_turns
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stream_task: Optional[asyncio.Task] = None
        self.last_session_id: Optional[str] = None
        self.task_start: Optional[float] = None
        self.last_activity: float = 0.0
        self.chat_id: Optional[int] = None
        self.pending_dangerous: Optional[dict] = None
        self.auto_continue: bool = True

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None


# --- Global State -----------------------------------------------------------
class GlobalState:
    def __init__(self):
        self.sessions: Dict[str, SessionSlot] = {}
        self.active_name: str = "default"
        self.task_timestamps: list = []
        self.daily_costs: dict = {}

    def active(self) -> SessionSlot:
        if self.active_name not in self.sessions:
            self.sessions[self.active_name] = SessionSlot(self.active_name)
        return self.sessions[self.active_name]

    def get_or_create(self, name: str, cwd: str = DEFAULT_CWD) -> SessionSlot:
        if name not in self.sessions:
            self.sessions[name] = SessionSlot(name, cwd)
        return self.sessions[name]

    def multi(self) -> bool:
        return len(self.sessions) > 1


gst = GlobalState()


# --- Helpers ----------------------------------------------------------------
def authorized(user_id: int) -> bool:
    return bool(ALLOWED_USER_IDS) and user_id in ALLOWED_USER_IDS


def md_to_html(text: str) -> str:
    """Convert Markdown to Telegram-safe HTML."""
    blocks, codes = [], []

    def _save_block(m):
        blocks.append(m.group(2))
        return f"\x00BLK{len(blocks) - 1}\x00"

    def _save_code(m):
        codes.append(m.group(1))
        return f"\x00CDE{len(codes) - 1}\x00"

    text = re.sub(r"```(\w*)\n?(.*?)```", _save_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", _save_code, text)
    text = html_mod.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"^#{1,3}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^---+$", "\u2014" * 20, text, flags=re.MULTILINE)
    for i, block in enumerate(blocks):
        text = text.replace(
            f"\x00BLK{i}\x00",
            f"<pre>{html_mod.escape(block.strip())}</pre>",
        )
    for i, code in enumerate(codes):
        text = text.replace(
            f"\x00CDE{i}\x00",
            f"<code>{html_mod.escape(code)}</code>",
        )
    return text


def find_split_point(text: str, max_len: int) -> int:
    """Find natural split: paragraph > line > sentence > word > hard cut."""
    if len(text) <= max_len:
        return len(text)
    start = int(max_len * 0.8)
    region = text[start:max_len]
    for sep in ("\n\n",):
        idx = region.rfind(sep)
        if idx != -1:
            return start + idx
    idx = region.rfind("\n")
    if idx != -1:
        return start + idx
    for sep in (". ", "! ", "? "):
        idx = region.rfind(sep)
        if idx != -1:
            return start + idx + len(sep)
    idx = region.rfind(" ")
    return (start + idx) if idx != -1 else max_len


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def _send_chunk(target, text: str):
    """Send a single chunk, falling back to plain text on any error."""
    text = text[:4096]
    try:
        await target.reply_text(text, parse_mode="HTML")
    except Exception:
        await target.reply_text(_strip_html(text)[:4096])


async def send_long(message, text: str):
    """Convert Markdown to HTML and send, splitting at natural boundaries."""
    MAX = 3900
    if not text:
        await message.reply_text("(empty response)")
        return
    formatted = md_to_html(text)
    while formatted:
        if len(formatted) <= MAX:
            await _send_chunk(message, formatted)
            break
        split = find_split_point(formatted, MAX)
        if split <= 0:
            split = MAX
        chunk = formatted[:split]
        formatted = formatted[split:].lstrip("\n")
        await _send_chunk(message, chunk)


async def edit_safe(msg, text: str, parse_mode: str = "HTML"):
    """Edit message text, truncating to 4096 and falling back on error."""
    text = text[:4096]
    try:
        await msg.edit_text(text, parse_mode=parse_mode)
    except Exception as e:
        if "not modified" not in str(e).lower():
            try:
                await msg.edit_text(re.sub(r"<[^>]+>", "", text))
            except Exception:
                pass


async def react(message, emoji: str):
    """Set reaction. Silently fails if unsupported."""
    try:
        await message.set_reaction(reaction=[ReactionTypeEmoji(emoji=emoji)])
    except Exception:
        pass


def tool_to_emoji(name: str) -> str:
    n = name.lower()
    if any(t in n for t in ("write", "edit", "bash", "notebook")):
        return "\U0001f468\u200d\U0001f4bb"
    if any(t in n for t in ("read", "glob", "grep")):
        return "\U0001f50d"
    if any(t in n for t in ("web", "fetch", "search")):
        return "\u26a1"
    return "\U0001f914"


def check_dangerous(prompt: str) -> Optional[str]:
    lower = prompt.lower()
    for pattern, desc in DANGEROUS_PATTERNS:
        if re.search(pattern, lower):
            return desc
    return None


def check_rate_limit() -> Optional[str]:
    """Check sliding window rate limits. Returns error message or None."""
    now = time.time()
    gst.task_timestamps = [t for t in gst.task_timestamps if now - t < 3600]
    recent_min = sum(1 for t in gst.task_timestamps if now - t < 60)
    if recent_min >= RATE_LIMIT_PER_MIN:
        return f"Rate limit: {RATE_LIMIT_PER_MIN} tasks/minute. Try again shortly."
    if len(gst.task_timestamps) >= RATE_LIMIT_PER_HOUR:
        return f"Rate limit: {RATE_LIMIT_PER_HOUR} tasks/hour."
    return None


def detect_sendable_files(text: str) -> list:
    """Detect file paths in response that could be sent as Telegram documents."""
    paths = re.findall(
        r'(?:/tmp/|/Users/)[^\s\'"<>|]+\.(?:png|jpg|jpeg|gif|pdf|zip|csv|xlsx)',
        text, re.IGNORECASE,
    )
    result = []
    for p in paths:
        fp = Path(p)
        if fp.exists() and fp.is_file() and fp.stat().st_size <= 10 * 1024 * 1024:
            result.append(fp)
    return result[:5]


def detect_numbered_options(text: str) -> list:
    """Detect numbered options (1. Do X, 2. Do Y) for inline keyboard."""
    matches = re.findall(r'^(\d+)[.)]\s+(.{3,80})$', text, re.MULTILINE)
    if 2 <= len(matches) <= 6:
        return [(num, label.strip()) for num, label in matches]
    return []


def _get_system_claude_processes() -> list:
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        procs = []
        for line in r.stdout.split("\n"):
            if "claude" not in line.lower():
                continue
            if any(skip in line for skip in ("bridge.py", "grep", "ps aux")):
                continue
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            procs.append(dict(
                pid=parts[1], cpu=parts[2], mem=parts[3],
                time=parts[9], cmd=parts[10][:120],
            ))
        return procs
    except Exception:
        return []


def _session_cwd(session_id: str) -> Optional[str]:
    """Find the cwd a historical session was recorded in by reading its JSONL."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    for proj in projects_dir.iterdir():
        jsonl = proj / f"{session_id}.jsonl"
        if not jsonl.exists():
            continue
        try:
            with jsonl.open() as f:
                for line in f:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except Exception:
                        continue
                    if cwd and os.path.isdir(cwd):
                        return cwd
        except Exception:
            pass
        return None
    return None


def _session_tag(slot: SessionSlot) -> str:
    """Return [name] prefix when running multiple sessions."""
    if gst.multi():
        marker = " \u25b6" if slot.name == gst.active_name else ""
        return f"[<b>{html_mod.escape(slot.name)}</b>{marker}] "
    return ""


# --- Commands ---------------------------------------------------------------
async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        await update.message.reply_text(f"Not authorized. Your ID: {uid}")
        return
    await update.message.reply_text(
        "Claude Code Bridge\n\n"
        "Send any text to run as a task.\n"
        "Send a photo or file to include it.\n\n"
        "/status \u2014 processes & state\n"
        "/stop \u2014 kill running task\n"
        "/new \u2014 start fresh session (clears context)\n"
        "/context \u2014 toggle auto-continue (on by default)\n"
        "/continue <msg> \u2014 resume last session\n"
        "/sessions \u2014 browse sessions (live + history)\n"
        "/switch [name] \u2014 switch active session\n"
        "/spawn <name> [path] \u2014 create a new named session\n"
        "/kill [name] \u2014 terminate a session\n"
        "/projects \u2014 switch project\n"
        "/model \u2014 switch model (Opus/Sonnet/Haiku)\n"
        "/budget <N> \u2014 USD limit\n"
        "/turns <N> \u2014 max turns\n"
        "/cost \u2014 daily cost summary\n"
        "/cwd <path> \u2014 working directory\n"
        "/id \u2014 your Telegram user ID"
    )


async def cmd_id(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your user ID: {update.effective_user.id}")


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return

    lines = ["**Active sessions:**\n"]
    if not gst.sessions:
        lines.append("  (none yet — send a message to start)")
    else:
        for name, slot in gst.sessions.items():
            marker = " \u25b6 active" if name == gst.active_name else ""
            status = "running" if slot.is_running() else "idle"
            if slot.is_running() and slot.task_start:
                status += f" ({int(time.time() - slot.task_start)}s)"
            lines.append(
                f"  **{name}**{marker}: {status} | `{slot.cwd}` | "
                f"{slot.model} | ${slot.budget:.2f} | {slot.max_turns} turns"
            )

    lines.append("")
    slot = gst.active()
    lines += [
        f"**Context:** {'ON (auto-continue)' if slot.auto_continue else 'OFF (fresh)'}",
        f"**Last session:** `{slot.last_session_id or 'none'}`",
    ]

    procs = _get_system_claude_processes()
    lines.append("")
    if procs:
        lines.append(f"**Claude processes on this Mac:** {len(procs)}")
        for p in procs:
            lines.append(
                f"  PID `{p['pid']}` | CPU {p['cpu']}% | "
                f"MEM {p['mem']}% | {p['time']}\n"
                f"  `{p['cmd']}`"
            )
    else:
        lines.append("**Claude processes on this Mac:** none")
    await send_long(update.message, "\n".join(lines))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    name = ctx.args[0] if ctx.args else gst.active_name
    slot = gst.sessions.get(name)
    if slot and slot.is_running():
        if slot.stream_task:
            slot.stream_task.cancel()
        slot.process.terminate()
        await update.message.reply_text(f"Session [{name}] terminated.")
    else:
        await update.message.reply_text(f"No task running in [{name}].")


async def cmd_new(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Clear session context — next message starts fresh."""
    if not authorized(update.effective_user.id):
        return
    gst.active().last_session_id = None
    await update.message.reply_text("Session cleared. Next message starts fresh.")


async def cmd_context(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-continue mode for the active session."""
    if not authorized(update.effective_user.id):
        return
    slot = gst.active()
    slot.auto_continue = not slot.auto_continue
    state = "ON" if slot.auto_continue else "OFF"
    desc = (
        "Messages auto-resume the last session (keeps context)."
        if slot.auto_continue
        else "Each message starts a fresh session (no context)."
    )
    await update.message.reply_text(f"[{slot.name}] Auto-continue: {state}\n{desc}")


async def cmd_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    slot = gst.active()
    if ctx.args:
        try:
            slot.budget = float(ctx.args[0])
            await update.message.reply_text(f"[{slot.name}] Budget: ${slot.budget:.2f}")
        except ValueError:
            await update.message.reply_text("Usage: /budget 5.00")
    else:
        await update.message.reply_text(f"[{slot.name}] Budget: ${slot.budget:.2f}")


async def cmd_turns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    slot = gst.active()
    if ctx.args:
        try:
            slot.max_turns = int(ctx.args[0])
            await update.message.reply_text(f"[{slot.name}] Max turns: {slot.max_turns}")
        except ValueError:
            await update.message.reply_text("Usage: /turns 10")
    else:
        await update.message.reply_text(f"[{slot.name}] Max turns: {slot.max_turns}")


async def cmd_cwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    slot = gst.active()
    if ctx.args:
        path = os.path.expanduser(" ".join(ctx.args))
        if os.path.isdir(path):
            slot.cwd = path
            await update.message.reply_text(f"[{slot.name}] CWD: {slot.cwd}")
        else:
            await update.message.reply_text(f"Not found: {path}")
    else:
        await update.message.reply_text(f"[{slot.name}] CWD: {slot.cwd}")


async def cmd_continue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    slot = gst.active()
    if not slot.last_session_id:
        await update.message.reply_text("No previous session to continue.")
        return
    msg = " ".join(ctx.args) if ctx.args else "Continue."
    await run_claude(update, msg, continue_session=True)


async def cmd_spawn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Create (and optionally switch to) a named session.

    Usage: /spawn <name> [project-path]
    """
    if not authorized(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /spawn <name> [path]\nExample: /spawn akeneo /Users/santibm/Sites/akeneo")
        return

    name = ctx.args[0]
    if len(ctx.args) > 1:
        raw_path = " ".join(ctx.args[1:])
        cwd = os.path.expanduser(raw_path)
        if not os.path.isdir(cwd):
            await update.message.reply_text(f"Path not found: {cwd}")
            return
    else:
        cwd = gst.active().cwd

    existed = name in gst.sessions
    slot = gst.get_or_create(name, cwd)
    gst.active_name = name

    verb = "Switched to" if existed else "Created"
    await update.message.reply_text(
        f"{verb} session <b>{html_mod.escape(name)}</b>\n"
        f"CWD: <code>{html_mod.escape(slot.cwd)}</code>\n"
        f"Now active. Send messages to interact with it.",
        parse_mode="HTML",
    )


async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Switch the active session. Shows keyboard if no arg given."""
    if not authorized(update.effective_user.id):
        return

    if ctx.args:
        name = ctx.args[0]
        slot = gst.get_or_create(name)
        gst.active_name = name
        status = "running" if slot.is_running() else "idle"
        await update.message.reply_text(
            f"Switched to <b>{html_mod.escape(name)}</b> ({status})\n"
            f"CWD: <code>{html_mod.escape(slot.cwd)}</code>",
            parse_mode="HTML",
        )
        return

    # Show inline keyboard of all sessions
    if not gst.sessions:
        await update.message.reply_text("No sessions yet. Use /spawn <name> to create one.")
        return

    keyboard = []
    for name, slot in gst.sessions.items():
        status = "\u25b6 " if name == gst.active_name else ""
        running = " \U0001f7e2" if slot.is_running() else " \u26aa"
        keyboard.append([InlineKeyboardButton(
            f"{status}{name}{running}",
            callback_data=f"sw:{name[:56]}",
        )])
    keyboard.append([InlineKeyboardButton(
        "\u2795 New session", callback_data="sw:__new__"
    )])
    await update.message.reply_text(
        "Switch active session:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kill a named session (or active if no name given)."""
    if not authorized(update.effective_user.id):
        return
    name = ctx.args[0] if ctx.args else gst.active_name
    slot = gst.sessions.get(name)
    if not slot:
        await update.message.reply_text(f"Session [{name}] not found.")
        return

    if slot.stream_task:
        slot.stream_task.cancel()
    if slot.is_running():
        slot.process.terminate()

    del gst.sessions[name]

    # Fall back to default if we killed the active session
    if gst.active_name == name:
        gst.active_name = next(iter(gst.sessions), "default")

    await update.message.reply_text(
        f"Session [{name}] killed.\n"
        f"Active: [{gst.active_name}]"
    )


async def cmd_sessions(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Show live sessions + recent JSONL history."""
    if not authorized(update.effective_user.id):
        return

    keyboard = []

    # --- Live sessions section ---
    if gst.sessions:
        for name, slot in gst.sessions.items():
            status = "\u25b6 " if name == gst.active_name else ""
            running = " \U0001f7e2" if slot.is_running() else " \u26aa"
            elapsed = ""
            if slot.is_running() and slot.task_start:
                elapsed = f" {int(time.time() - slot.task_start)}s"
            label = f"{status}{name}{running}{elapsed}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"sw:{name[:56]}")])

    # --- JSONL history section ---
    projects_dir = Path.home() / ".claude" / "projects"
    history = []
    if projects_dir.exists():
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    mtime = f.stat().st_mtime
                    project = proj.name.replace("-", "/").split("/")[-1]
                    history.append(dict(id=f.stem, project=project, mtime=mtime))
                except Exception:
                    continue
    history.sort(key=lambda s: s["mtime"], reverse=True)
    for s in history[:6]:
        age = int(time.time() - s["mtime"])
        if age < 3600:
            age_str = f"{age // 60}m ago"
        elif age < 86400:
            age_str = f"{age // 3600}h ago"
        else:
            age_str = f"{age // 86400}d ago"
        label = f"\U0001f4dc {s['project']} ({age_str})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ses:{s['id'][:56]}")])

    if not keyboard:
        await update.message.reply_text("No sessions found.")
        return

    await update.message.reply_text(
        "Sessions (tap to switch/resume):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_projects(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Show ~/Sites/ projects as inline keyboard."""
    if not authorized(update.effective_user.id):
        return
    sites = Path(SITES_DIR)
    if not sites.exists():
        await update.message.reply_text(f"Not found: {SITES_DIR}")
        return
    dirs = sorted(d.name for d in sites.iterdir() if d.is_dir() and not d.name.startswith("."))
    keyboard = []
    slot = gst.active()
    for i in range(0, len(dirs), 2):
        row = []
        for d in dirs[i : i + 2]:
            marker = " \u2713" if str(sites / d) == slot.cwd else ""
            row.append(InlineKeyboardButton(d + marker, callback_data=f"cwd:{d}"))
        keyboard.append(row)
    current = Path(slot.cwd).name
    await update.message.reply_text(
        f"Switch project (current: <code>{html_mod.escape(current)}</code>):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def cmd_model(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Show model selection keyboard."""
    if not authorized(update.effective_user.id):
        return
    slot = gst.active()
    models = [
        ("claude-sonnet-4-6", "Sonnet 4.6"),
        ("claude-opus-4-6", "Opus 4.6"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    ]
    keyboard = []
    for model_id, label in models:
        marker = " \u2713" if slot.model == model_id else ""
        keyboard.append([InlineKeyboardButton(
            label + marker, callback_data=f"model:{model_id}"
        )])
    keyboard.append([InlineKeyboardButton(
        "Default" + (" \u2713" if slot.model is None else ""),
        callback_data="model:default",
    )])
    current = slot.model or "default"
    await update.message.reply_text(
        f"[{slot.name}] Select model (current: <code>{html_mod.escape(current)}</code>):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def cmd_cost(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Show daily cost summary."""
    if not authorized(update.effective_user.id):
        return
    today = time.strftime("%Y-%m-%d")
    dates = sorted(gst.daily_costs.keys(), reverse=True)[:7]
    if not dates:
        await update.message.reply_text("No cost data yet.")
        return
    lines = ["**Daily Cost Summary**\n"]
    for d in dates:
        costs = gst.daily_costs[d]
        total = sum(costs)
        marker = " \u2190 today" if d == today else ""
        lines.append(f"`{d}`: ${total:.4f} ({len(costs)} tasks){marker}")
    grand = sum(sum(c) for c in gst.daily_costs.values())
    lines.append(f"\n**All time:** ${grand:.4f}")
    await send_long(update.message, "\n".join(lines))


# --- Callback handler -------------------------------------------------------
async def handle_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("cwd:"):
        dirname = data[4:]
        path = str(Path(SITES_DIR) / dirname)
        if os.path.isdir(path):
            gst.active().cwd = path
            await query.edit_message_text(f"[{gst.active_name}] Switched to: {path}")
        else:
            await query.edit_message_text(f"Not found: {path}")

    elif data.startswith("sw:"):
        name = data[3:]
        if name == "__new__":
            await query.edit_message_text(
                "Use /spawn <name> [path] to create a new session."
            )
        else:
            slot = gst.get_or_create(name)
            gst.active_name = name
            status = "running" if slot.is_running() else "idle"
            await query.edit_message_text(
                f"Switched to <b>{html_mod.escape(name)}</b> ({status})\n"
                f"CWD: <code>{html_mod.escape(slot.cwd)}</code>",
                parse_mode="HTML",
            )

    elif data.startswith("ses:"):
        session_id = data[4:]
        slot = gst.active()
        slot.last_session_id = session_id
        slot.auto_continue = True
        recorded_cwd = _session_cwd(session_id)
        if recorded_cwd:
            slot.cwd = recorded_cwd
        cwd_line = (
            f"\nCWD: <code>{html_mod.escape(slot.cwd)}</code>"
            if recorded_cwd else ""
        )
        await query.edit_message_text(
            f"[{gst.active_name}] Loaded session "
            f"<code>{html_mod.escape(session_id[:20])}</code>"
            f"{cwd_line}\n"
            "Your next message will resume it, or tap below.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "\u25b6 Resume now",
                    callback_data="quickact:continue",
                ),
            ]]),
        )

    elif data == "confirm_dangerous":
        slot = gst.active()
        if slot.pending_dangerous:
            prompt = slot.pending_dangerous["prompt"]
            chat_id = slot.pending_dangerous.get("chat_id")
            slot.pending_dangerous = None
            await query.edit_message_text("Confirmed. Running...")
            status_msg = await query.message.chat.send_message("\u23f3")
            _launch_streaming(slot, chat_id, query.get_bot(), status_msg, prompt)
        else:
            await query.edit_message_text("No pending task.")

    elif data == "cancel_dangerous":
        gst.active().pending_dangerous = None
        await query.edit_message_text("Cancelled.")

    elif data.startswith("model:"):
        model_val = data[6:]
        slot = gst.active()
        if model_val == "default":
            slot.model = DEFAULT_MODEL
            await query.edit_message_text(f"[{slot.name}] Model: default")
        else:
            slot.model = model_val
            await query.edit_message_text(f"[{slot.name}] Model: {model_val}")

    elif data.startswith("quickact:"):
        action = data[9:]
        slot = gst.active()
        if action == "continue":
            await query.edit_message_text("Send your follow-up message.")
        elif action == "new":
            slot.last_session_id = None
            await query.edit_message_text(f"[{slot.name}] Session cleared. Next message starts fresh.")
        elif action == "projects":
            sites = Path(SITES_DIR)
            if sites.exists():
                dirs = sorted(
                    d.name for d in sites.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                )
                keyboard = []
                for i in range(0, len(dirs), 2):
                    row = []
                    for d in dirs[i : i + 2]:
                        marker = " \u2713" if str(sites / d) == slot.cwd else ""
                        row.append(InlineKeyboardButton(
                            d + marker, callback_data=f"cwd:{d}"
                        ))
                    keyboard.append(row)
                await query.edit_message_text(
                    "Switch project:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )

    elif data.startswith("option:"):
        option_text = data[7:]
        await query.edit_message_text(f"Selected: {option_text}")
        status_msg = await query.message.chat.send_message("\u23f3")
        slot = gst.active()
        _launch_streaming(
            slot, query.message.chat_id, query.get_bot(), status_msg,
            option_text, continue_session=True,
        )


# --- Photo & document handlers ----------------------------------------------
async def handle_photo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    photo = update.message.photo[-1]
    file = await photo.get_file()
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir="/tmp")
    await file.download_to_drive(tmp.name)
    caption = update.message.caption or "Analyze this image"
    await run_claude(update, f"{caption}\n\nImage file path: {tmp.name}")


async def handle_document(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("File too large (max 10MB).")
        return
    file = await doc.get_file()
    suffix = Path(doc.file_name).suffix if doc.file_name else ""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir="/tmp")
    await file.download_to_drive(tmp.name)
    caption = update.message.caption or f"Analyze this file: {doc.file_name}"
    await run_claude(update, f"{caption}\n\nFile path: {tmp.name}")


# --- Core: streaming Claude runner ------------------------------------------
def _launch_streaming(
    slot: SessionSlot,
    chat_id: int,
    bot,
    status_msg,
    prompt: str,
    continue_session: bool = False,
):
    """Launch _run_claude_streaming as a background asyncio task."""
    task = asyncio.create_task(
        _run_claude_streaming(slot, chat_id, bot, status_msg, prompt, continue_session)
    )
    slot.stream_task = task


async def run_claude(
    update: Update, prompt: str, continue_session: bool = False
):
    if not authorized(update.effective_user.id):
        return

    slot = gst.active()

    if slot.is_running():
        await update.message.reply_text(
            f"[{slot.name}] A task is already running. /stop it first."
        )
        return

    # Session idle timeout — auto-clear stale sessions
    if slot.last_session_id and slot.last_activity:
        if time.time() - slot.last_activity > SESSION_IDLE_TIMEOUT:
            slot.last_session_id = None

    # Rate limiting
    rate_msg = check_rate_limit()
    if rate_msg:
        await update.message.reply_text(rate_msg)
        return
    gst.task_timestamps.append(time.time())
    slot.last_activity = time.time()
    slot.chat_id = update.effective_chat.id

    # Dangerous command check
    danger = check_dangerous(prompt)
    if danger:
        slot.pending_dangerous = {
            "prompt": prompt,
            "chat_id": update.effective_chat.id,
        }
        keyboard = [[
            InlineKeyboardButton("\u2705 Confirm", callback_data="confirm_dangerous"),
            InlineKeyboardButton("\u274c Cancel", callback_data="cancel_dangerous"),
        ]]
        await update.message.reply_text(
            f"\u26a0\ufe0f <b>Potentially dangerous:</b> {html_mod.escape(danger)}\n\n"
            f"<code>{html_mod.escape(prompt[:200])}</code>\n\nProceed?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    await react(update.message, "\U0001f44d")

    tag = _session_tag(slot)
    status_msg = await update.message.reply_text(f"{tag}\u23f3")
    await react(status_msg, "\U0001f440")

    should_continue = continue_session or (slot.auto_continue and slot.last_session_id)

    _launch_streaming(
        slot,
        update.effective_chat.id,
        update.get_bot(),
        status_msg,
        prompt,
        should_continue,
    )


async def _run_claude_streaming(
    slot: SessionSlot,
    chat_id: int,
    bot,
    status_msg,
    prompt: str,
    continue_session: bool = False,
):
    """Run Claude Code with stream-json and live-edit the Telegram message."""
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model", slot.model,
        "--max-turns", str(slot.max_turns),
        "--max-budget-usd", str(slot.budget),
    ]
    if continue_session and slot.last_session_id:
        cmd.extend(["-r", slot.last_session_id])
    cmd.append(prompt)

    tag = _session_tag(slot)
    start_time = time.time()
    slot.task_start = start_time
    accumulated = ""
    last_edit_time = 0.0
    last_edit_len = 0
    cost = 0.0
    turns = 0
    is_error = False
    current_reaction = "\U0001f440"

    typing_task = asyncio.create_task(_typing_loop(chat_id, bot))

    try:
        slot.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=slot.cwd,
        )

        buf = b""
        eof = False
        while not eof:
            try:
                chunk = await asyncio.wait_for(
                    slot.process.stdout.read(256 * 1024), timeout=TASK_TIMEOUT
                )
            except asyncio.TimeoutError:
                slot.process.terminate()
                await edit_safe(status_msg, f"{tag}\u23f0 Timed out.")
                return

            if not chunk:
                eof = True
                lines = [buf] if buf else []
            else:
                buf += chunk
                parts = buf.split(b"\n")
                lines = parts[:-1]
                buf = parts[-1]

            for raw_line in lines:
                raw = raw_line.decode(errors="replace").strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue

                ev_type = event.get("type", "")

                if ev_type == "assistant":
                    content_blocks = event.get("message", {}).get("content", [])
                    for block in content_blocks:
                        block_type = block.get("type", "")

                        if block_type == "text":
                            accumulated = block.get("text", "")

                        elif block_type == "tool_use":
                            tool_name = block.get("name", "")
                            emoji = tool_to_emoji(tool_name)
                            if emoji != current_reaction:
                                current_reaction = emoji
                                await react(status_msg, emoji)

                            inp = block.get("input", {})
                            desc = (
                                inp.get("command", "")[:80]
                                or inp.get("file_path", "")
                                or inp.get("pattern", "")
                                or inp.get("url", "")
                            )
                            tool_line = f"\U0001f527 {tool_name}"
                            if desc:
                                tool_line += f": {desc}"
                            display = tag + f"<i>{html_mod.escape(tool_line)}</i>"
                            if accumulated:
                                raw_txt = accumulated[-2000:] if len(accumulated) > 2000 else accumulated
                                display = tag + md_to_html(raw_txt) + "\n\n" + f"<i>{html_mod.escape(tool_line)}</i>"
                            await edit_safe(status_msg, display)
                            last_edit_time = time.time()

                    now = time.time()
                    if (
                        accumulated
                        and now - last_edit_time >= STREAM_EDIT_INTERVAL
                        and len(accumulated) - last_edit_len >= STREAM_MIN_DELTA
                    ):
                        raw_txt = accumulated[-2500:] if len(accumulated) > 2500 else accumulated
                        display = tag + md_to_html(raw_txt) + "\n\n<i>generating\u2026</i>"
                        await edit_safe(status_msg, display)
                        last_edit_time = now
                        last_edit_len = len(accumulated)

                elif ev_type == "result":
                    cost = event.get("total_cost_usd", 0) or event.get("cost_usd", 0)
                    turns = event.get("num_turns", 0)
                    is_error = event.get("is_error", False)
                    slot.last_session_id = event.get("session_id")
                    final_text = event.get("result", "")
                    if final_text:
                        accumulated = final_text
                    eof = True
                    break

        await slot.process.wait()

    except FileNotFoundError:
        await edit_safe(status_msg, f"{tag}Claude not found: {CLAUDE_BIN}")
        return
    except asyncio.CancelledError:
        return
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        await edit_safe(status_msg, f"{tag}Error: {type(e).__name__}: {e}")
        return
    finally:
        typing_task.cancel()
        slot.process = None
        slot.task_start = None
        slot.stream_task = None

    # --- Final response ---
    elapsed = int(time.time() - start_time)
    prefix = "\u26a0\ufe0f " if is_error else ""
    footer = f"\n\n\u2014\u2014\u2014\n${cost:.4f} | {turns} turns | {elapsed}s"
    full_text = tag + prefix + accumulated + footer

    formatted = md_to_html(full_text)
    if len(formatted) <= 3900:
        await edit_safe(status_msg, formatted)
    else:
        try:
            await status_msg.delete()
        except Exception:
            pass
        MAX = 3900
        remaining = formatted
        while remaining:
            if len(remaining) <= MAX:
                try:
                    await bot.send_message(chat_id, remaining[:4096], parse_mode="HTML")
                except Exception:
                    await bot.send_message(chat_id, _strip_html(remaining)[:4096])
                break
            split = find_split_point(remaining, MAX)
            if split <= 0:
                split = MAX
            chunk = remaining[:split]
            remaining = remaining[split:].lstrip("\n")
            try:
                await bot.send_message(chat_id, chunk[:4096], parse_mode="HTML")
            except Exception:
                await bot.send_message(chat_id, _strip_html(chunk)[:4096])

    done_emoji = "\U0001f44d" if not is_error else "\U0001f494"
    try:
        if len(formatted) <= 4000:
            await react(status_msg, done_emoji)
    except Exception:
        pass

    today = time.strftime("%Y-%m-%d")
    if today not in gst.daily_costs:
        gst.daily_costs[today] = []
    gst.daily_costs[today].append(cost)

    files = detect_sendable_files(accumulated)
    for fp in files:
        try:
            await bot.send_document(
                chat_id, document=open(fp, "rb"), filename=fp.name,
            )
        except Exception:
            pass

    # Quick-action buttons
    buttons = []
    options = detect_numbered_options(accumulated)
    if options:
        for num, label in options:
            buttons.append([InlineKeyboardButton(
                f"{num}. {label[:40]}",
                callback_data=f"option:{label[:56]}",
            )])

    # Session switcher row when multiple sessions exist
    if gst.multi():
        sw_row = []
        for name in list(gst.sessions.keys())[:3]:
            marker = "\u25b6" if name == gst.active_name else ""
            sw_row.append(InlineKeyboardButton(
                f"{marker}{name}", callback_data=f"sw:{name[:20]}"
            ))
        buttons.append(sw_row)

    buttons.append([
        InlineKeyboardButton("\u25b6 Continue", callback_data="quickact:continue"),
        InlineKeyboardButton("\u2728 New", callback_data="quickact:new"),
        InlineKeyboardButton("\U0001f4c2 Projects", callback_data="quickact:projects"),
    ])
    try:
        await bot.send_message(
            chat_id, "\u26a1",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        pass


async def _typing_loop(chat_id, bot):
    try:
        while True:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# --- Message handler --------------------------------------------------------
async def handle_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    if not authorized(uid):
        await update.message.reply_text(f"Not authorized. Your ID: {uid}")
        return
    await run_claude(update, update.message.text)


# --- Stale task watchdog ----------------------------------------------------
async def _stale_task_watchdog(app):
    """Periodically kill zombie tasks that outlive their timeout."""
    bot = app.bot
    while True:
        await asyncio.sleep(60)
        for name, slot in list(gst.sessions.items()):
            if slot.process is not None and slot.task_start:
                elapsed = time.time() - slot.task_start
                if elapsed > TASK_TIMEOUT * 2:
                    try:
                        slot.process.kill()
                    except Exception:
                        pass
                    slot.process = None
                    slot.task_start = None
                    if slot.stream_task:
                        slot.stream_task.cancel()
                        slot.stream_task = None
                    if slot.chat_id:
                        try:
                            await bot.send_message(
                                slot.chat_id,
                                f"\u23f0 [{name}] Task auto-terminated (stale — no output).",
                            )
                        except Exception:
                            pass


async def _post_init(app):
    await app.bot.set_my_commands([
        BotCommand("status", "show active sessions + current settings"),
        BotCommand("sessions", "browse live sessions + recent history"),
        BotCommand("switch", "switch active session (name or picker)"),
        BotCommand("spawn", "create a named session: /spawn <name> [path]"),
        BotCommand("kill", "terminate a session: /kill [name]"),
        BotCommand("stop", "stop running task in session"),
        BotCommand("new", "clear active session context"),
        BotCommand("continue", "resume last session with a message"),
        BotCommand("context", "toggle auto-continue on/off"),
        BotCommand("projects", "switch project directory"),
        BotCommand("cwd", "show/set working directory"),
        BotCommand("model", "switch Claude model"),
        BotCommand("budget", "show/set USD budget"),
        BotCommand("turns", "show/set max turns"),
        BotCommand("cost", "daily cost summary"),
        BotCommand("id", "show your Telegram user id"),
        BotCommand("help", "show help"),
    ])
    asyncio.create_task(_stale_task_watchdog(app))


# --- Main -------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set. Edit .env.")
        raise SystemExit(1)
    if not ALLOWED_USER_IDS:
        print("WARNING: ALLOWED_USER_IDS empty. Send /id to the bot to find yours.")

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("turns", cmd_turns))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("continue", cmd_continue))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("spawn", cmd_spawn))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    active = gst.active_name
    print(f"Bridge running | Active: {active} | Users: {ALLOWED_USER_IDS}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
