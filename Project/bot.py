# bot.py — Complete Rank System Discord Bot

import os
import re
import json
import random
import asyncio
import discord
from discord import app_commands
from dotenv import load_dotenv
import time
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
import asyncpg
from asyncpg.pool import Pool
import aiohttp

# ---------- ENV ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_ENV = os.getenv("GUILD_ID")
AUTO_FILE_URL = os.getenv("AUTO_MESSAGES_URL")  # GitHub raw URL
DATABASE_URL = os.getenv("DATABASE_URL")
XP_CHANNEL_ID = int(os.getenv("XP_CHANNEL_ID", 0))

# ---------- Config ----------
AUTO_CHANNEL_ID = 1412316924536422405
AUTO_INTERVAL = 900  # 15 minutes
BYPASS_ROLE = "Basic"
STATUS_SWITCH_SECONDS = 10
COUNTER_UPDATE_SECONDS = 5
NOTIFICATION_CHANNEL_ID = 1412316924536422405
REPORT_CHANNEL_ID = 1412325934291484692

RANKS = [
    ("S+", 500), ("A", 400), ("B", 300),
    ("C", 200), ("D", 125), ("E", 50)
]
RANK_ORDER = [r[0] for r in RANKS]
RANK_EMOJIS = {"S+": "🌟", "A": "🔥", "B": "⭐", "C": "💫", "D": "✨", "E": "🔶"}
RANK_COLORS = {
    "S+": discord.Color.gold(),
    "A": discord.Color.red(),
    "B": discord.Color.orange(),
    "C": discord.Color.blue(),
    "D": discord.Color.green(),
    "E": discord.Color.light_grey()
}
ROLE_PREFIX = "Rank "

# ---------- Intents / Client ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---------- In-memory Stores ----------
recent_channels = {}
last_joined_member = {}
custom_status = {}
counter_channels = {}
AUTO_MESSAGES = []
db_pool: Pool = None
leaderboard_cache = {}

# ==================================================
# DATABASE SETUP
# ==================================================
async def init_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        print("✅ Connected to PostgreSQL database")
        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                guild_id BIGINT,
                user_id BIGINT,
                total_xp INTEGER DEFAULT 0,
                daily_msgs INTEGER DEFAULT 0,
                daily_xp INTEGER DEFAULT 0,
                last_message_ts INTEGER DEFAULT 0,
                channel_id BIGINT DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )""")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_ranks (
                guild_id BIGINT,
                user_id BIGINT,
                forced_rank TEXT,
                PRIMARY KEY (guild_id, user_id)
            )""")
        print("✅ Tables verified")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        raise

# ==================================================
# HELPERS / UTILS
# ==================================================
def update_recent_channel(user_id: int, guild_id: int, channel_id: int):
    if user_id not in recent_channels:
        recent_channels[user_id] = {}
    if guild_id not in recent_channels[user_id]:
        recent_channels[user_id][guild_id] = []
    lst = recent_channels[user_id][guild_id]
    if channel_id in lst:
        lst.remove(channel_id)
    lst.insert(0, channel_id)
    if len(lst) > 30:
        lst.pop()

def parse_message_link(link: str):
    match = re.search(r"discord.com/channels/(\d+)/(\d+)/(\d+)", link)
    return match.groups() if match else None

# ==================================================
# AUTO MESSAGES
# ==================================================
async def load_auto_messages_from_url():
    global AUTO_MESSAGES
    if not AUTO_FILE_URL:
        print("⚠️ AUTO_MESSAGES_URL not set")
        AUTO_MESSAGES = []
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(AUTO_FILE_URL) as r:
                if r.status == 200:
                    content = await r.text()
                    try:
                        data = json.loads(content)
                        if isinstance(data, list):
                            AUTO_MESSAGES = data
                            print(f"✅ Loaded {len(AUTO_MESSAGES)} auto messages (JSON)")
                            return
                    except json.JSONDecodeError:
                        pass
                    AUTO_MESSAGES = [
                        line.strip() for line in content.splitlines() if line.strip()
                    ]
                    print(f"✅ Loaded {len(AUTO_MESSAGES)} auto messages (Text)")
                else:
                    AUTO_MESSAGES = []
                    print(f"⚠️ Failed auto messages: {r.status}")
    except Exception as e:
        AUTO_MESSAGES = []
        print(f"⚠️ Error loading auto messages: {e}")

# ==================================================
# BAD WORDS
# ==================================================
try:
    with open("badwords.txt", "r", encoding="utf-8") as f:
        BAD_WORDS = [w.strip().lower() for w in f if w.strip()]
    print(f"✅ Loaded {len(BAD_WORDS)} bad words")
except FileNotFoundError:
    BAD_WORDS = []
    print("⚠️ badwords.txt not found")
except Exception as e:
    BAD_WORDS = []
    print(f"⚠️ Error loading badwords.txt: {e}")

# ==================================================
# XP / LEVEL MECHANICS
# ==================================================
def xp_for_message(content: str) -> int:
    return 10 + min(len(content) // 15, 20)

def required_xp_for_level(level: int) -> int:
    return 50 * (level ** 2) + 100

def total_xp_to_reach_level(level: int) -> int:
    return sum(required_xp_for_level(l) for l in range(1, level + 1))

def compute_level_from_total_xp(total_xp: int) -> int:
    lvl = 0
    while total_xp >= total_xp_to_reach_level(lvl + 1):
        lvl += 1
    return lvl

# ==================================================
# XP DATABASE OPS
# ==================================================
async def add_xp(user: discord.Member, guild: discord.Guild, amount: int, channel_id: int):
    ts = int(time.time())
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow(
            "SELECT * FROM users WHERE guild_id=$1 AND user_id=$2", guild.id, user.id
        )
        if not record:
            await conn.execute(
                "INSERT INTO users (guild_id, user_id, total_xp, daily_msgs, daily_xp, last_message_ts, channel_id) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                guild.id, user.id, amount, 1, amount, ts, channel_id
            )
        else:
            await conn.execute(
                "UPDATE users SET total_xp=total_xp+$1, daily_msgs=daily_msgs+1, daily_xp=daily_xp+$1, last_message_ts=$2, channel_id=$3 WHERE guild_id=$4 AND user_id=$5",
                amount, ts, channel_id, guild.id, user.id
            )

# ==================================================
# EVENTS
# ==================================================
@client.event
async def on_ready():
    await init_db()
    await load_auto_messages_from_url()
    try:
        if not hasattr(client, 'commands_synced'):
            await tree.sync()
            client.commands_synced = True
            print("✅ Slash commands synced")
    except Exception as e:
        print(f"⚠️ Command sync error: {e}")

    client.loop.create_task(status_loop())
    client.loop.create_task(counter_updater())
    client.loop.create_task(auto_message_task())
    schedule_daily_reset()
    print(f"✅ Bot ready as {client.user}")

@client.event
async def on_member_join(member):
    last_joined_member[member.guild.id] = member.name

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if any(r.name == BYPASS_ROLE for r in message.author.roles):
        return

    if any(bad in message.content.lower() for bad in BAD_WORDS):
        await message.delete()
        await message.channel.send(f"{message.author.mention} ❌ That word is not allowed.", delete_after=5)
        return

    if "discord.gg" in message.content.lower() or "http" in message.content.lower():
        await message.delete()
        await message.channel.send(f"{message.author.mention} ❌ No links allowed.", delete_after=5)
        return

    xp = xp_for_message(message.content)
    await add_xp(message.author, message.guild, xp, message.channel.id)
    update_recent_channel(message.author.id, message.guild.id, message.channel.id)

    await client.process_commands(message)

# ==================================================
# SLASH COMMANDS
# ==================================================
@tree.command(name="rank", description="Check your rank and XP")
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    async with db_pool.acquire() as conn:
        record = await conn.fetchrow(
            "SELECT total_xp FROM users WHERE guild_id=$1 AND user_id=$2",
            interaction.guild.id, member.id
        )
    if not record:
        await interaction.response.send_message(f"{member.mention} has no XP yet.", ephemeral=True)
        return

    total_xp = record["total_xp"]
    level = compute_level_from_total_xp(total_xp)
    current_xp = total_xp - total_xp_to_reach_level(level)
    next_xp = required_xp_for_level(level + 1)

    bar_length = 20
    filled = int(bar_length * current_xp / next_xp)
    bar = "█" * filled + "░" * (bar_length - filled)

    embed = discord.Embed(
        title=f"{member.display_name}'s Rank",
        description=f"Level **{level}**\nXP: {current_xp}/{next_xp}\n\n{bar}",
        color=discord.Color.blurple()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@tree.command(name="leaderboard", description="Show top 10 users")
async def leaderboard(interaction: discord.Interaction):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, total_xp FROM users WHERE guild_id=$1 ORDER BY total_xp DESC LIMIT 10",
            interaction.guild.id
        )
    if not rows:
        await interaction.response.send_message("No leaderboard data yet.", ephemeral=True)
        return

    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())
    desc = ""
    for i, row in enumerate(rows, start=1):
        user = interaction.guild.get_member(row["user_id"])
        name = user.display_name if user else f"User {row['user_id']}"
        desc += f"**#{i}** {name} — {row['total_xp']} XP\n"
    embed.description = desc
    await interaction.response.send_message(embed=embed)

@tree.command(name="purge", description="Delete messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)

@tree.command(name="embed", description="Send a styled embed")
async def embed_cmd(interaction: discord.Interaction, title: str, description: str):
    embed = discord.Embed(title=title, description=description, color=discord.Color.green())
    await interaction.response.send_message(embed=embed)

@tree.command(name="say", description="Bot repeats your message")
async def say(interaction: discord.Interaction, message: str):
    await interaction.response.send_message("✅ Sent!", ephemeral=True)
    await interaction.channel.send(message)

@tree.command(name="addrank", description="Give a manual rank override")
@app_commands.checks.has_permissions(administrator=True)
async def addrank(interaction: discord.Interaction, member: discord.Member, rank: str):
    if rank not in RANK_ORDER:
        await interaction.response.send_message(f"Invalid rank. Choices: {', '.join(RANK_ORDER)}", ephemeral=True)
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO manual_ranks (guild_id, user_id, forced_rank) VALUES ($1,$2,$3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET forced_rank=$3",
            interaction.guild.id, member.id, rank
        )
    await interaction.response.send_message(f"✅ {member.mention} was given rank {rank}", ephemeral=True)

@tree.command(name="setstatus", description="Set a custom status message")
async def setstatus(interaction: discord.Interaction, message: str):
    custom_status[interaction.guild.id] = message
    await interaction.response.send_message(f"✅ Status set to: {message}", ephemeral=True)

# ==================================================
# STATUS ROTATION
# ==================================================
async def status_loop():
    await client.wait_until_ready()
    statuses = ["XP System Active ⚡", "Type /rank to check XP", "Custom Rank Bot 💎"]
    while not client.is_closed():
        for guild in client.guilds:
            status_text = custom_status.get(guild.id, random.choice(statuses))
            await client.change_presence(activity=discord.Game(name=status_text))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)

# ==================================================
# COUNTER CHANNEL UPDATER
# ==================================================
async def counter_updater():
    await client.wait_until_ready()
    while not client.is_closed():
        for guild in client.guilds:
            member_count = guild.member_count
            last_joined = last_joined_member.get(guild.id, "None")
            for ch_id, ch_type in counter_channels.get(guild.id, {}).items():
                channel = guild.get_channel(ch_id)
                if not channel:
                    continue
                try:
                    if ch_type == "members":
                        await channel.edit(name=f"👥 Members: {member_count}")
                    elif ch_type == "lastjoin":
                        await channel.edit(name=f"📥 Last Join: {last_joined}")
                except Exception:
                    pass
        await asyncio.sleep(COUNTER_UPDATE_SECONDS)

# ==================================================
# AUTO MESSAGE LOOP
# ==================================================
async def auto_message_task():
    await client.wait_until_ready()
    while not client.is_closed():
        if AUTO_MESSAGES:
            channel = client.get_channel(AUTO_CHANNEL_ID)
            if channel:
                msg = random.choice(AUTO_MESSAGES)
                try:
                    await channel.send(msg)
                except Exception:
                    pass
        await asyncio.sleep(AUTO_INTERVAL)

# ==================================================
# DAILY RESET (Karachi Time)
# ==================================================
def schedule_daily_reset():
    scheduler = AsyncIOScheduler()
    timezone_karachi = pytz.timezone("Asia/Karachi")

    async def reset_daily():
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET daily_msgs=0, daily_xp=0 WHERE guild_id=$1",
                int(GUILD_ID_ENV)
            )
        print("✅ Daily reset done")

    scheduler.add_job(
        lambda: asyncio.create_task(reset_daily()),
        trigger="cron", hour=0, minute=0, timezone=timezone_karachi
    )
    scheduler.start()

# ==================================================
# RUN BOT
# ==================================================
if TOKEN is None:
    print("❌ DISCORD_TOKEN not set in .env")
else:
    client.run(TOKEN)
