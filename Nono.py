import discord
from discord.ext import commands
import requests
import asyncio
import json
import time
from collections import defaultdict, deque



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



# ================= MESSAGES LIMITS=================

def split_message(text, limit=2000):
    chunks = []
    while len(text) > limit:
        split_at = text.rfind(" ", 0, limit)  # avoid cutting words
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)
    return chunks

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

    # ================= FILTER =================
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

    # ================= FIRST MESSAGE =================
    reply = await message.channel.send("Responding...")

    full = ""
    last_update = 0

    # ================= STREAM =================
    async for partial in run_ollama_stream(messages, current_model):
        full = partial

        now = time.time()
        if now - last_update > 0.8:
            safe = full[:1990] + " ▌"
            await reply.edit(content=safe)
            last_update = now

    # ================= FINAL SAFE SPLIT SEND =================
    def send_chunks(text, limit=2000):
        return [text[i:i+limit] for i in range(0, len(text), limit)]

    chunks = send_chunks(full, 2000)

    # edit first message
    await reply.edit(content=chunks[0])

    # send continuation messages
    prev = reply
    for chunk in chunks[1:]:
        prev = await prev.reply(chunk)

    memory[cid].append(("assistant", full))


bot.run(DISCORD_TOKEN)
