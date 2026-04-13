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
from io import BytesIO
from collections import defaultdict, deque
from PIL import Image



# ================= CONFIG =================
BOT_TOKEN = ""
with open("bot-token.txt","r") as bot_token:
    BOT_TOKEN = bot_token.read()

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

# ================= STATE =================
memory = defaultdict(lambda: deque(maxlen=MAX_MEMORY))
summary = defaultdict(str)
current_model = DEFAULT_MODEL


# ================= LATEX =================
# pip install Pillow  (required for transparency post-processing)

EQUATION_TEXT_COLOR = "#DCDDDE"   # Discord's default text colour
EQUATION_PADDING_PT = 10          # padding in LaTeX points (standalone border)
EQUATION_PADDING_PX = 14          # padding in pixels (matplotlib fallback)
BG_THRESHOLD = 15                 # pixels with R,G,B all below this are treated as background


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

# ── regex ────────────────────────────────────────────────────────────────────
# display ($$...$$) is listed first so it wins when both branches could match
_MATH_RE = re.compile(
    r'(\$\$)(.*?)(\$\$)'             # groups 1-3 : display math
    r'|'
    r'(\$)(?!\$)(.*?)(?<!\$)(\$)',   # groups 4-6 : inline math
    re.DOTALL,
)


def has_latex(text: str) -> bool:
    return bool(_MATH_RE.search(text))


def parse_parts(text: str) -> list[tuple[str, str]]:
    """Return [(kind, content)] where kind ∈ {'text', 'inline', 'display'}."""
    parts = []
    pos = 0
    for m in _MATH_RE.finditer(text):
        if pos < m.start():
            chunk = text[pos:m.start()]
            if chunk:
                parts.append(("text", chunk))
        if m.group(1):                              # $$...$$
            parts.append(("display", m.group(2).strip()))
        else:                                       # $...$
            parts.append(("inline", m.group(5).strip()))
        pos = m.end()
    if pos < len(text):
        tail = text[pos:]
        if tail:
            parts.append(("text", tail))
    return parts


# ── transparency helper ───────────────────────────────────────────────────────
def _make_transparent(raw_png: BytesIO) -> BytesIO:
    """
    Replace near-black pixels with fully transparent ones.
    Works because we render LaTeX on a pure black background so the
    chroma-key is clean even for anti-aliased glyph edges.
    """
    img = Image.open(raw_png).convert("RGBA")
    pixels = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if r < BG_THRESHOLD and g < BG_THRESHOLD and b < BG_THRESHOLD:
                pixels[x, y] = (0, 0, 0, 0)   # fully transparent
    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out


# ── pdflatex renderer ─────────────────────────────────────────────────────────
def _render_pdflatex(
    expr: str,
    display: bool,
    engine: str,
    converter: str,
    dpi: int = 200,
) -> BytesIO | None:

    wrapped = f"\\[\n{expr}\n\\]" if display else f"\\({expr}\\)"

    # Black page background → chroma-keyed out later for true transparency.
    # EQUATION_TEXT_COLOR is a light grey that reads well on Discord's dark bg.
    tex = (
        f"\\documentclass[border={EQUATION_PADDING_PT}pt]{{standalone}}\n"
        "\\usepackage{amsmath,amssymb,amsfonts,mathtools}\n"
        "\\usepackage{xcolor}\n"
        f"\\pagecolor[HTML]{{000000}}\n"
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
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0 or not os.path.exists(pdf_file):
            return None

        if converter == "pdftoppm":
            png_prefix = os.path.join(tmpdir, "eq_out")
            subprocess.run(
                ["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf_file, png_prefix],
                capture_output=True,
                timeout=15,
            )
            png_file = png_prefix + ".png"
        else:   # ImageMagick convert
            png_file = os.path.join(tmpdir, "eq.png")
            subprocess.run(
                ["convert", "-density", str(dpi), pdf_file,
                 "-background", "black", "-flatten", "-trim", "+repage", png_file],
                capture_output=True,
                timeout=15,
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


# ── matplotlib renderer (fallback) ────────────────────────────────────────────
def _render_matplotlib(expr: str, display: bool) -> BytesIO | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fontsize = 18 if display else 14
        latex_str = f"${expr}$"

        fig = plt.figure(figsize=(0.01, 0.01))
        fig.patch.set_alpha(0)          # transparent figure background
        t = fig.text(
            0, 0, latex_str,
            fontsize=fontsize,
            color=EQUATION_TEXT_COLOR,  # white-grey text
            usetex=False,               # matplotlib mathtext, no LaTeX install needed
        )

        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        bbox = t.get_window_extent(renderer=renderer)

        pad = EQUATION_PADDING_PX
        fig.set_size_inches(
            (bbox.width  + pad * 2) / fig.dpi,
            (bbox.height + pad * 2) / fig.dpi,
        )
        t.set_position((
            pad / (bbox.width  + pad * 2),
            pad / (bbox.height + pad * 2),
        ))

        bio = BytesIO()
        fig.savefig(bio, format="png", bbox_inches="tight",
                    transparent=True, dpi=150)
        plt.close(fig)
        bio.seek(0)
        return bio

    except Exception as e:
        print(f"[LaTeX matplotlib] error: {e}")
        return None


# ── public entry point ────────────────────────────────────────────────────────
def render_equation(expr: str, display: bool = False) -> BytesIO | None:
    """Render a LaTeX expression to a transparent PNG BytesIO. Returns None on failure."""
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
def build_context(cid):
    msgs = []

    if summary[cid]:
        msgs.append({
            "role": "system",
            "content": f"Conversation summary:\n{summary[cid]}"
        })

    for role, content in memory[cid]:
        msgs.append({"role": role, "content": content})

    return msgs

def maybe_summarize(cid, model):
    if len(memory[cid]) < SUMMARY_TRIGGER:
        return

    text = "\n".join([f"{r}: {c}" for r, c in memory[cid]])

    messages = [
        {"role": "system", "content": "Summarize key facts and context concisely."},
        {"role": "user", "content": text}
    ]

    def run():
        result = ""
        for chunk in ollama_stream(messages, model):
            result = chunk
        summary[cid] = result
        memory[cid].clear()

    asyncio.get_running_loop().run_in_executor(None, run)


# ================= MESSAGE UTILS =================
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
    """
    Send the final response.
    - If it contains LaTeX: delete the placeholder, send text+image parts.
    - Otherwise: edit the placeholder (split if needed).
    """
    if not has_latex(full_text):
        # ── plain text path ──────────────────────────────────────────────
        chunks = split_message(full_text)
        if placeholder_msg:
            await placeholder_msg.edit(content=chunks[0])
        else:
            await channel.send(chunks[0])
        prev = placeholder_msg
        for chunk in chunks[1:]:
            prev = await channel.send(chunk)
        return

    # ── LaTeX path ───────────────────────────────────────────────────────
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
            # Flush accumulated text first
            stripped = text_buffer.strip()
            if stripped:
                for chunk in split_message(stripped):
                    await channel.send(chunk)
            text_buffer = ""

            # Render equation in thread pool (blocking I/O)
            is_display = (kind == "display")
            png: BytesIO | None = await loop.run_in_executor(
                None, render_equation, content, is_display
            )

            if png:
                label = "display" if is_display else "inline"
                await channel.send(
                    file=discord.File(png, filename=f"equation_{label}.png")
                )
            else:
                # Fallback: monospace code block
                delim = "$$" if is_display else "$"
                await channel.send(f"`{delim}{content}{delim}`")

    # Flush any trailing text
    stripped = text_buffer.strip()
    if stripped:
        for chunk in split_message(stripped):
            await channel.send(chunk)


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


# ================= CHAT =================
@bot.event
async def on_message(message):
    global current_model

    # ── filters ──────────────────────────────────────────────────────────
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
    memory[cid].append(("user", message.content))
    maybe_summarize(cid, current_model)

    messages = build_context(cid)
    messages.insert(0, {
        "role": "system",
        "content": "You are a helpful local AI assistant. Be concise and accurate."
    })

    # ── stream to a placeholder ───────────────────────────────────────────
    reply = await message.channel.send("Responding...")

    full = ""
    last_update = 0

    async for partial in run_ollama_stream(messages, current_model):
        full = partial
        now = time.time()
        if now - last_update > 0.8:
            # Only live-preview if no LaTeX detected yet (avoids flicker on $)
            if not has_latex(full):
                safe = full[:1990] + " ▌"
                await reply.edit(content=safe)
            else:
                await reply.edit(content="Rendering equations... ▌")
            last_update = now

    # ── final send (handles plain text and LaTeX) ─────────────────────────
    await send_response(message.channel, full, placeholder_msg=reply)

    memory[cid].append(("assistant", full))


bot.run(DISCORD_TOKEN)
