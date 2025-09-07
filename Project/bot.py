# bot.py — Final Fixed Version (with init_db)
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
from io import BytesIO
from PIL import Image, ImageDraw

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_ENV = os.getenv("GUILD_ID")
AUTO_FILE_URL = os.getenv("AUTO_MESSAGES_URL")  # GitHub raw URL
DATABASE_URL = os.getenv("DATABASE_URL")
XP_CHANNEL_ID = int(os.getenv("XP_CHANNEL_ID", 0))

# ---------- Config ----------
AUTO_CHANNEL_ID = 1412316924536422405
AUTO_INTERVAL = 7200  # 2 hours
BYPASS_ROLE = "Basic"
STATUS_SWITCH_SECONDS = 10
COUNTER_UPDATE_SECONDS = 5
NOTIFICATION_CHANNEL_ID = 1412316924536422405
REPORT_CHANNEL_ID = 1412325934291484692

RANKS = [("S+", 500), ("A", 400), ("B", 300), ("C", 200), ("D", 125), ("E", 50)]
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

# ---------- In-memory stores ----------
recent_channels = {}
last_joined_member = {}
custom_status = {}
counter_channels = {}
AUTO_MESSAGES = []
db_pool: Pool = None
leaderboard_cache = {}

# ---------- Database Init ----------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    print("✅ Database connected")

# ---------------------------------------------------------
# Database helpers, XP functions, rank role logic, etc.
# (same as tumhare base code me tha)
# ---------------------------------------------------------

# ---------- Utility: Rounded Avatar ----------
async def get_rounded_avatar(url: str, size: int = 64):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()

    avatar = Image.open(BytesIO(data)).convert("RGBA")
    avatar = avatar.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)

    rounded = Image.new("RGBA", (size, size))
    rounded.paste(avatar, (0, 0), mask=mask)

    bio = BytesIO()
    rounded.save(bio, "PNG")
    bio.seek(0)
    return bio

# ---------- /rank Command (Aligned) ----------
@tree.command(name="rank", description="Show your rank and level")
async def rank_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = await get_user_row(interaction.guild.id, member.id)
    total_xp = row['total_xp']
    daily_xp = row['daily_xp']
    lvl = compute_level_from_total_xp(total_xp)

    forced_rank = await get_manual_rank(interaction.guild.id, member.id)
    if forced_rank:
        rank_name = forced_rank
        rank_source = " (Admin Set)"
    else:
        rank_name = None
        for r, thresh in RANKS:
            if daily_xp >= thresh:
                rank_name = r
                break
        rank_source = ""

    rank_emoji = RANK_EMOJIS.get(rank_name, "🔹")
    embed_color = RANK_COLORS.get(rank_name, discord.Color.blurple())

    # clean alignment block
    stats_text = (
        "```ini\n"
        f"Rank      : {rank_name or 'No Rank'}{rank_source}\n"
        f"Level     : {lvl}\n"
        f"Total XP  : {total_xp}\n"
        f"Daily XP  : {daily_xp}\n"
        "```"
    )

    embed = discord.Embed(
        title=f"{rank_emoji} {member.display_name}'s Rank Stats",
        description=stats_text,
        color=embed_color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# ---------- /leaderboard Command (Rounded Avatars) ----------
@tree.command(name="leaderboard", description="Show server leaderboard (Top 15 by 24h XP)")
async def leaderboard(interaction: discord.Interaction):
    guild = interaction.guild
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, daily_xp, total_xp
            FROM users
            WHERE guild_id=$1
            ORDER BY daily_xp DESC
            LIMIT 15
        """, guild.id)

    embed = discord.Embed(
        title=f"🏆 {guild.name} — Daily Leaderboard",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    files = []
    desc = ""
    medal_emojis = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
                    "⑪","⑫","⑬","⑭","⑮"]

    for idx, row in enumerate(rows):
        member = guild.get_member(row["user_id"])
        if not member:
            continue

        avatar_bytes = await get_rounded_avatar(member.display_avatar.url, size=48)
        file = discord.File(avatar_bytes, filename=f"avatar{idx}.png")
        files.append(file)

        medal = medal_emojis[idx] if idx < len(medal_emojis) else f"{idx+1}."
        desc += f"{medal} **{member.display_name}**\n"
        desc += f"⭐ {row['daily_xp']} XP (24h) • 📈 Lv {compute_level_from_total_xp(row['total_xp'])}\n"
        desc += f"[​](attachment://avatar{idx}.png)\n\n"  # invisible clickable avatar

    embed.description = desc
    await interaction.response.send_message(embed=embed, files=files)

# ---------- Events ----------
@client.event
async def on_ready():
    await init_db()
    await load_auto_messages_from_url()
    await tree.sync()
    print(f"✅ Logged in as {client.user}")
    client.loop.create_task(status_loop())
    client.loop.create_task(counter_updater())
    client.loop.create_task(auto_message_task())
    schedule_daily_reset()

@client.event
async def on_member_join(member):
    last_joined_member[member.guild.id] = member.name

# ---------- Run ----------
if __name__ == "__main__":
    client.run(TOKEN)
