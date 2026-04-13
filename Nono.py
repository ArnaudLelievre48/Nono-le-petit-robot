import discord
from discord.ext import commands
import requests
import asyncio
import json
import time
import re
import os
import subprocess
import shutil
import tempfile
import sqlite3
from io import BytesIO
from collections import defaultdict
from PIL import Image



# ================= CONFIG =================
BOT_TOKEN = ""
try:
    with open("bot-token.txt","r") as bot_token:
        BOT_TOKEN = bot_token.read()
except:
    pass

DISCORD_TOKEN = "YOUR_TOKEN"

if DISCORD_TOKEN == "YOUR_TOKEN" and BOT_TOKEN != "":
    DISCORD_TOKEN = BOT_TOKEN
elif DISCORD_TOKEN == "YOUR_TOKEN" and BOT_TOKEN == "":
    print("\nERROR : you did not put any token for your bot...\n")
    quit()

OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e2b"

MAX_MEMORY = 20
SUMMARY_TRIGGER = 30

CATEGORY_NAME = "Nono-le-petit-robot"
DB_PATH = "bot_memory.db"


# ================= STATE =================
# Only live Discord message objects stay in RAM — they can't be serialised
last_bot_messages = {}
current_model = DEFAULT_MODEL


# ================= SQLITE MEMORY =================
# Schema:
#   messages(id, channel_id, role, content)
#     id is an autoincrement rowid that preserves insertion order
#   summaries(channel_id, content)
#
# Nothing is ever loaded into a Python list/deque that grows with uptime.
# Every access is a targeted SELECT; every mutation is an INSERT/DELETE on disk.

def _db() -> sqlite3.Connection:
    """Open a connection with WAL mode (faster concurrent writes, safe readers)."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")   # safe + faster than FULL
    return con


def _init_db():
    with _db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                channel_id INTEGER PRIMARY KEY,
                content    TEXT NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel_id)")

_init_db()


# ── memory helpers ────────────────────────────────────────────────────────────

def mem_append(cid: int, role: str, content: str):
    """Append one turn and prune oldest rows so the channel stays ≤ MAX_MEMORY."""
    with _db() as con:
        con.execute(
            "INSERT INTO messages(channel_id, role, content) VALUES (?,?,?)",
            (cid, role, content),
        )
        # Delete every row for this channel that is NOT among the newest MAX_MEMORY
        con.execute("""
            DELETE FROM messages
            WHERE channel_id = ?
              AND id NOT IN (
                  SELECT id FROM messages
                  WHERE channel_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
        """, (cid, cid, MAX_MEMORY))


def mem_all(cid: int) -> list[tuple[str, str]]:
    """Return [(role, content), ...] in insertion order — reads from disk each time."""
    with _db() as con:
        return con.execute(
            "SELECT role, content FROM messages WHERE channel_id=? ORDER BY id ASC",
            (cid,),
        ).fetchall()


def mem_count(cid: int) -> int:
    with _db() as con:
        return con.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id=?", (cid,)
        ).fetchone()[0]


def mem_clear(cid: int):
    with _db() as con:
        con.execute("DELETE FROM messages WHERE channel_id=?", (cid,))


def summary_get(cid: int) -> str:
    with _db() as con:
        row = con.execute(
            "SELECT content FROM summaries WHERE channel_id=?", (cid,)
        ).fetchone()
    return row[0] if row else ""


def summary_set(cid: int, text: str):
    with _db() as con:
        con.execute(
            "INSERT INTO summaries(channel_id, content) VALUES (?,?)"
            " ON CONFLICT(channel_id) DO UPDATE SET content=excluded.content",
            (cid, text),
        )


# ================= LATEX =================

EQUATION_TEXT_COLOR = "#DCDDDE"
EQUATION_PADDING_PT = 5
EQUATION_PADDING_PX = 14
BG_THRESHOLD = 15


def _detect_latex_backend():
    for prog in ("pdflatex", "latex"):
        try:
            subprocess.run([prog, "--version"], capture_output=True, check=True, timeout=5)
            for conv in ("pdftoppm", "convert"):
                try:
                    subprocess.run(
                        [conv, "--version" if conv == "pdftoppm" else "-version"],
                        capture_output=True, check=True, timeout=5,
                    )
                    print(f"[LaTeX] backend: {prog} + {conv}")
                    return prog, conv
                except (subprocess.CalledProcessError, FileNotFoundError):
                    continue
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    try:
        import matplotlib  # noqa: F401
        print("[LaTeX] backend: matplotlib fallback")
        return "matplotlib", None
    except ImportError:
        pass
    print("[LaTeX] WARNING: no rendering backend found — equations sent as code blocks")
    return None, None


LATEX_ENGINE, LATEX_CONVERTER = _detect_latex_backend()

_MATH_RE = re.compile(
    r'(\$)(?!\$)(.*?)(?<!\$)(\$)',
    re.DOTALL,
)


def clean_backticks(text: str) -> str:
    return text.replace('`$', '$').replace('$`', '$').replace(r'$$', r'$')


def has_latex(text: str) -> bool:
    return bool(_MATH_RE.search(text))


def parse_parts(text: str) -> list[tuple[str, str]]:
    text = clean_backticks(text)
    parts = []
    pos = 0
    for m in _MATH_RE.finditer(text):
        if pos < m.start():
            chunk = text[pos:m.start()]
            if chunk:
                parts.append(("text", chunk))
        parts.append(("inline", m.group(2).strip()))
        pos = m.end()
    if pos < len(text):
        tail = text[pos:]
        if tail:
            parts.append(("text", tail))
    return parts


def _make_transparent(raw_png: BytesIO) -> BytesIO:
    img = Image.open(raw_png).convert("RGBA")
    pixels = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if r < BG_THRESHOLD and g < BG_THRESHOLD and b < BG_THRESHOLD:
                pixels[x, y] = (0, 0, 0, 0)
    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out


def _render_pdflatex(expr, display, engine, converter, dpi=200):
    wrapped = f"\\[\n{expr}\n\\]" if display else f"\\({expr}\\)"
    tex = (
        f"\\documentclass[border={EQUATION_PADDING_PT}pt]{{standalone}}\n"
        "\\usepackage{amsmath,amssymb,amsfonts,mathtools}\n"
        "\\usepackage{xcolor}\n"
        "\\pagecolor[HTML]{000000}\n"
        f"\\color[HTML]{{{EQUATION_TEXT_COLOR.lstrip('#')}}}\n"
        "\\begin{document}\n"
        f"{wrapped}\n"
        "\\end{document}\n"
    )
    tmpdir = tempfile.mkdtemp()
    try:
        tex_file = os.path.join(tmpdir, "eq.tex")
        pdf_file = os.path.join(tmpdir, "eq.pdf")
        with open(tex_file, "w") as f:
            f.write(tex)
        r = subprocess.run(
            [engine, "-interaction=nonstopmode", "-output-directory", tmpdir, tex_file],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not os.path.exists(pdf_file):
            return None
        if converter == "pdftoppm":
            png_prefix = os.path.join(tmpdir, "eq_out")
            subprocess.run(
                ["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf_file, png_prefix],
                capture_output=True, timeout=15,
            )
            png_file = png_prefix + ".png"
        else:
            png_file = os.path.join(tmpdir, "eq.png")
            subprocess.run(
                ["convert", "-density", str(dpi), pdf_file,
                 "-background", "black", "-flatten", "-trim", "+repage", png_file],
                capture_output=True, timeout=15,
            )
        if not os.path.exists(png_file):
            return None
        with open(png_file, "rb") as f:
            raw = BytesIO(f.read())
        return _make_transparent(raw)
    except Exception as e:
        print(f"[LaTeX pdflatex] error: {e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _render_matplotlib(expr, display):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fontsize = 18 if display else 14
        latex_str = f"${expr}$"
        fig = plt.figure(figsize=(0.01, 0.01))
        fig.patch.set_alpha(0)
        t = fig.text(0, 0, latex_str, fontsize=fontsize, color=EQUATION_TEXT_COLOR, usetex=False)
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        bbox = t.get_window_extent(renderer=renderer)
        pad = EQUATION_PADDING_PX
        fig.set_size_inches(
            (bbox.width  + pad * 2) / fig.dpi,
            (bbox.height + pad * 2) / fig.dpi,
        )
        t.set_position((pad / (bbox.width + pad * 2), pad / (bbox.height + pad * 2)))
        bio = BytesIO()
        fig.savefig(bio, format="png", bbox_inches="tight", transparent=True, dpi=150)
        plt.close(fig)
        bio.seek(0)
        return bio
    except Exception as e:
        print(f"[LaTeX matplotlib] error: {e}")
        return None


def render_equation(expr, display=False):
    if LATEX_ENGINE == "matplotlib":
        return _render_matplotlib(expr, display)
    if LATEX_ENGINE is not None:
        return _render_pdflatex(expr, display, LATEX_ENGINE, LATEX_CONVERTER)
    return None


# ================= OLLAMA =================
def ollama_stream(messages, model):
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={"model": model, "messages": messages, "stream": True},
        stream=True,
    )
    full = ""
    for line in r.iter_lines():
        if not line:
            continue
        data = json.loads(line.decode())
        if "message" in data:
            full += data["message"].get("content", "")
        if data.get("done"):
            break
        yield full


async def run_ollama_stream(messages, model):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def worker():
        try:
            for partial in ollama_stream(messages, model):
                asyncio.run_coroutine_threadsafe(queue.put(partial), loop)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)

    loop.run_in_executor(None, worker)
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item


def ollama_models():
    r = requests.get(f"{OLLAMA_URL}/api/tags")
    return [m["name"] for m in r.json().get("models", [])]


# ================= MEMORY =================
def build_context(cid: int) -> list[dict]:
    msgs = []
    s = summary_get(cid)
    if s:
        msgs.append({"role": "system", "content": f"Conversation summary:\n{s}"})
    for role, content in mem_all(cid):
        msgs.append({"role": role, "content": content})
    return msgs


def maybe_summarize(cid: int, model: str):
    if mem_count(cid) < SUMMARY_TRIGGER:
        return

    text = "\n".join(f"{r}: {c}" for r, c in mem_all(cid))
    messages = [
        {"role": "system", "content": "Summarize key facts and context concisely."},
        {"role": "user",   "content": text},
    ]

    def run():
        result = ""
        for chunk in ollama_stream(messages, model):
            result = chunk
        summary_set(cid, result)
        mem_clear(cid)

    asyncio.get_running_loop().run_in_executor(None, run)


# ================= MESSAGE UTILS =================
async def tracked_send(channel, content=None, **kwargs):
    msg = await channel.send(content, **kwargs)
    last_bot_messages.setdefault(channel.id, []).append(msg)
    return msg


async def clear_last_bot_messages(cid):
    for msg in last_bot_messages.get(cid, []):
        try:
            await msg.delete()
            await asyncio.sleep(0.5)  # adjust if needed
        except Exception:
            pass
    last_bot_messages[cid] = []


def split_message(text, limit=2000):
    chunks = []
    while len(text) > limit:
        split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)
    return chunks


async def send_response(channel, full_text, placeholder_msg=None):
    if not has_latex(full_text):
        chunks = split_message(full_text)
        if placeholder_msg:
            await placeholder_msg.edit(content=chunks[0])
        else:
            await tracked_send(channel, chunks[0])
        for chunk in chunks[1:]:
            await tracked_send(channel, chunk)
        return

    if placeholder_msg:
        try:
            await placeholder_msg.delete()
        except discord.NotFound:
            pass

    parts = parse_parts(full_text)
    text_buffer = ""
    loop = asyncio.get_running_loop()

    for kind, content in parts:
        if kind == "text":
            text_buffer += content
        else:
            stripped = text_buffer.strip()
            if stripped:
                for chunk in split_message(stripped):
                    await tracked_send(channel, chunk)
            text_buffer = ""

            is_display = (kind == "display")
            png = await loop.run_in_executor(None, render_equation, content, is_display)

            if png:
                label = "display" if is_display else "inline"
                await tracked_send(channel, file=discord.File(png, filename=f"equation_{label}.png"))
            else:
                print("RENDER ERROR")
                delim = "$$" if is_display else "$"
                await tracked_send(channel, f"`{delim}{content}{delim}`")

    stripped = text_buffer.strip()
    if stripped:
        for chunk in split_message(stripped):
            await tracked_send(channel, chunk)


def render_full_latex(text: str) -> BytesIO | None:
    tmpdir = tempfile.mkdtemp()
    try:
        tex_file = os.path.join(tmpdir, "full.tex")
        pdf_file = os.path.join(tmpdir, "full.pdf")
        tex = (
            f"\\documentclass[border={EQUATION_PADDING_PT}pt,varwidth]{{standalone}}\n"
            "\\usepackage{amsmath,amssymb,amsfonts,mathtools}\n"
            "\\usepackage{xcolor}\n"
            "\\pagecolor[HTML]{000000}\n"
            f"\\color[HTML]{{{EQUATION_TEXT_COLOR.lstrip('#')}}}\n"
            "\\begin{document}\n"
            "\\raggedright\n"
            f"{text}\n"
            "\\end{document}\n"
        )
        with open(tex_file, "w") as f:
            f.write(tex)
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-output-directory", tmpdir, tex_file],
            capture_output=True, timeout=30,
        )
        if not os.path.exists(pdf_file):
            return None
        png_file = os.path.join(tmpdir, "out.png")
        subprocess.run(
            ["pdftoppm", "-png", "-singlefile", pdf_file, os.path.join(tmpdir, "out")],
            capture_output=True, timeout=15,
        )
        if not os.path.exists(png_file):
            return None
        with open(png_file, "rb") as f:
            return BytesIO(f.read())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ================= DISCORD =================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="/", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


# ---------------- COMMANDS ----------------
@bot.command()
async def llms(ctx):
    await ctx.send("\n".join(ollama_models()))


@bot.command()
async def model(ctx, *, name):
    global current_model
    current_model = name
    await ctx.send(f"Model set to `{name}`")


@bot.command()
async def clear(ctx):
    """Wipe conversation memory and summary for this channel."""
    cid = ctx.channel.id
    mem_clear(cid)
    summary_set(cid, "")
    await ctx.send("Memory cleared for this channel.")


# ================= CHAT =================
@bot.event
async def on_message(message):
    global current_model

    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.category is None or channel.category.name != CATEGORY_NAME:
        return
    if message.author.bot:
        return
    if message.content.strip() == "":
        return

    await bot.process_commands(message)

    if message.content.startswith(bot.command_prefix):
        return

    cid = message.channel.id

    content_input = message.content
    if message.attachments:
        for attachment in message.attachments:
            if attachment.filename.endswith(".txt"):
                file_bytes = await attachment.read()
                content_input += "\n\n" + file_bytes.decode("utf-8", errors="ignore")

    mem_append(cid, "user", content_input)
    maybe_summarize(cid, current_model)

    messages = build_context(cid)
    messages.insert(0, {
        "role": "system",
        "content": "You are a helpful local AI assistant. Be concise and accurate.",
    })

    reply = await message.channel.send("Responding...")

    full = ""
    last_update = 0

    async for partial in run_ollama_stream(messages, current_model):
        full = partial
        now = time.time()
        if now - last_update > 0.8:
            if not has_latex(full):
                safe = full[:1990] + " ▌"
                await reply.edit(content=safe)
            else:
                await reply.edit(content="Rendering equations... ▌")
            last_update = now

    await send_response(message.channel, full, placeholder_msg=reply)

    if has_latex(full):
        png = await asyncio.get_running_loop().run_in_executor(
            None, render_full_latex, full.replace("\n", r" \\ " + "\n")
        )
        if png:
            await clear_last_bot_messages(channel.id)
            await message.channel.send(file=discord.File(png, filename="full.png"))

    mem_append(cid, "assistant", full)


bot.run(DISCORD_TOKEN)
