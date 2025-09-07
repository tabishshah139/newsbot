# bot.py — Perfect Rank System with Alignment Fix + Leaderboard Avatars
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

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_ENV = os.getenv("GUILD_ID")
AUTO_FILE_URL = os.getenv("AUTO_MESSAGES_URL")  # GitHub raw URL
DATABASE_URL = os.getenv("DATABASE_URL")
XP_CHANNEL_ID = int(os.getenv("XP_CHANNEL_ID", 0))

# ---------- Config ----------
AUTO_CHANNEL_ID = 1412316924536422405
AUTO_INTERVAL = 900
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

# ---------------------------------------------------------
# (Database setup, helpers, XP system, rank system, etc.)
# Yeh sab code same hai jo tumne diya tha, maine sirf
# /rank aur /leaderboard sections update kiye hain niche
# ---------------------------------------------------------

# ---------- Enhanced Rank Command ----------
@tree.command(name="rank", description="Show your rank and level")
async def rank_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    if interaction.guild is None:
        return await interaction.response.send_message("Guild-only.", ephemeral=True)

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

    current_level_xp = total_xp_to_reach_level(lvl)
    next_level_xp = total_xp_to_reach_level(lvl + 1)
    xp_progress = total_xp - current_level_xp
    xp_needed = next_level_xp - current_level_xp
    progress_percentage = (xp_progress / xp_needed) * 100 if xp_needed > 0 else 100

    rank_emoji = RANK_EMOJIS.get(rank_name, "🔹")
    embed_color = RANK_COLORS.get(rank_name, discord.Color.blurple())

    embed = discord.Embed(
        title=f"{rank_emoji} {member.display_name}'s Rank Stats",
        color=embed_color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    # Alignment Fix
    embed.add_field(name="🏆 Current Rank", value=f"**{rank_emoji} {rank_name or 'No Rank'}{rank_source}**", inline=False)
    embed.add_field(name="📈 Level", value=f"`{lvl}`", inline=False)
    embed.add_field(name="💎 Total XP", value=f"`{total_xp}`", inline=False)

    filled_blocks = int(progress_percentage / 10)
    progress_bar = "🟩" * filled_blocks + "⬜" * (10 - filled_blocks)
    embed.add_field(
        name="🚀 Level Progress",
        value=f"{progress_bar}\n`{xp_progress}` / `{xp_needed}` XP ({progress_percentage:.1f}%)",
        inline=False
    )

    embed.add_field(name="⭐ 24h XP", value=f"`{daily_xp}`", inline=False)

    next_rank = None
    if rank_name:
        current_rank_index = RANK_ORDER.index(rank_name)
        if current_rank_index > 0:
            next_rank = RANKS[current_rank_index - 1]
        else:
            next_rank = RANKS[-1]

    if next_rank:
        xp_needed = max(0, next_rank[1] - daily_xp)
        next_emoji = RANK_EMOJIS.get(next_rank[0], "⚡")
        embed.add_field(
            name=f"{next_emoji} Next Rank",
            value=f"**{next_rank[0]}** - `{xp_needed}` XP needed",
            inline=False
        )

    rank_info = " | ".join([f"{r}: {t} XP" for r, t in RANKS])
    embed.set_footer(text=f"Rank Requirements: {rank_info}")

    await interaction.response.send_message(embed=embed)

# ---------- Advanced Leaderboard Command ----------
@tree.command(name="leaderboard", description="Show server leaderboard (Top 15 by 24h XP)")
async def leaderboard(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("Guild-only.", ephemeral=True)

    now_ts = time.time()
    cache = leaderboard_cache.get(guild.id)
    if cache and now_ts - cache[0] < 60:
        return await interaction.response.send_message(embed=cache[1])

    embed = await build_leaderboard_embed(guild)
    leaderboard_cache[guild.id] = (now_ts, embed)
    await interaction.response.send_message(embed=embed)

async def build_leaderboard_embed(guild: discord.Guild):
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

    medal_emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟",
                    "⑪", "⑫", "⑬", "⑭", "⑮"]

    desc = ""
    for idx, row in enumerate(rows):
        uid, dxp, txp = row['user_id'], row['daily_xp'], row['total_xp']
        member = guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        lvl = compute_level_from_total_xp(txp)

        user_rank = None
        for r, thresh in RANKS:
            if dxp >= thresh:
                user_rank = r
                break

        rank_emoji = RANK_EMOJIS.get(user_rank, "🔹") if user_rank else "🔸"
        medal = medal_emojis[idx] if idx < len(medal_emojis) else f"{idx+1}."

        avatar_url = member.display_avatar.url if member else "https://cdn.discordapp.com/embed/avatars/0.png"
        avatar_md = f"[​]({avatar_url})"  # Zero-width space as clickable link (hacky way to simulate avatar)

        desc += f"{medal} {avatar_md} **{name}**\n"
        desc += f" {rank_emoji} {user_rank or 'No Rank'} • ⭐ {dxp} XP (24h) • 📈 Lv {lvl}\n\n"

    if not desc:
        desc = "No activity yet. Start chatting to earn XP and climb the leaderboard! 💪"

    embed.description = desc
    rank_guide = " | ".join([f"{RANK_EMOJIS.get(r, '')} {r}" for r in RANK_ORDER])
    embed.set_footer(text=f"Ranks: {rank_guide} | Reset daily at 12:00 AM PKT")
    return embed

# ---------- Events ----------
@client.event
async def on_ready():
    await init_db()
    await load_auto_messages_from_url()
    try:
        if not hasattr(client, 'commands_synced'):
            await tree.sync()
            client.commands_synced = True
            print(f"✅ Commands synced successfully. Logged in as: {client.user}")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")
    client.loop.create_task(status_loop())
    client.loop.create_task(counter_updater())
    client.loop.create_task(auto_message_task())
    schedule_daily_reset()

@client.event
async def on_member_join(member):
    last_joined_member[member.guild.id] = member.name

# ---------- Run ----------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing — set it in Railway variables.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing — add PostgreSQL database in Railway.")
    client.run(TOKEN)
