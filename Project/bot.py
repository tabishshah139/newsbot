# bot.py — Perfect Rank System with All Fixes
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

# Image generation imports
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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
REPORT_CHANNEL_ID = 1412325934291484692  # Hardcoded report channel

# Rank thresholds (EASY PROGRESSION)
RANKS = [("S+", 500), ("A", 400), ("B", 300), ("C", 200), ("D", 125), ("E", 50)]
RANK_ORDER = [r[0] for r in RANKS]
RANK_EMOJIS = {"S+": "🌟", "A": "🔥", "B": "⭐", "C": "💫", "D": "✨", "E": "🔶"}
# color integers for embed (useable by discord.Color)
RANK_COLOR_HEX = {
    "S+": 0xFFD700,  # gold
    "A": 0xFF4500,   # orange/red
    "B": 0xFFA500,   # orange
    "C": 0x1E90FF,   # dodgerblue
    "D": 0x2ECC71,   # green
    "E": 0x95A5A6    # light grey
}
ROLE_PREFIX = "Rank "

# ---------- Intents / Client / Tree ----------
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

# ---------- Database Setup ----------
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
            )
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_ranks (
                guild_id BIGINT,
                user_id BIGINT,
                forced_rank TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
            """)
        print("✅ Database tables created/verified")
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        raise

# ---------- Helpers ----------
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

def format_content(content: str, bold: bool, underline: bool, code_lang: str):
    if code_lang:
        return f"```{code_lang}\n{content}\n```"
    if bold:
        content = f"**{content}**"
    if underline:
        content = f"__{content}__"
    return content

def parse_message_link(link: str):
    match = re.search(r"discord.com/channels/(\d+)/(\d+)/(\d+)", link)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)

# ---------- Load auto messages from external URL ----------
async def load_auto_messages_from_url():
    global AUTO_MESSAGES
    if not AUTO_FILE_URL:
        print("⚠️ AUTO_MESSAGES_URL not set - auto messages will be empty")
        AUTO_MESSAGES = []
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(AUTO_FILE_URL) as response:
                if response.status == 200:
                    content = await response.text()

                    # Try JSON format first
                    try:
                        data = json.loads(content)
                        if isinstance(data, list):
                            AUTO_MESSAGES = data
                            print(f"✅ Loaded {len(AUTO_MESSAGES)} auto messages from URL (JSON format)")
                            return
                    except json.JSONDecodeError:
                        pass

                    # If not JSON, try text format (one message per line)
                    messages = [line.strip() for line in content.split('\n') if line.strip()]
                    AUTO_MESSAGES = messages
                    print(f"✅ Loaded {len(AUTO_MESSAGES)} auto messages from URL (Text format)")
                else:
                    print(f"⚠️ Failed to load auto messages from URL: HTTP {response.status}")
                    AUTO_MESSAGES = []
    except Exception as e:
        print(f"⚠️ Error loading auto messages from URL: {e}")
        AUTO_MESSAGES = []

# ---------- Load bad words ----------
try:
    with open("badwords.txt", "r", encoding="utf-8") as f:
        BAD_WORDS = [w.strip().lower() for w in f if w.strip()]
    print(f"✅ Loaded {len(BAD_WORDS)} bad words.")
except FileNotFoundError:
    BAD_WORDS = []
    print("⚠️ badwords.txt not found — bad word filter will be empty.")
except Exception as e:
    BAD_WORDS = []
    print(f"⚠️ Error loading badwords.txt: {e}")

# ---------- Autocomplete helpers ----------
async def channel_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    guild = interaction.guild
    if not guild:
        return []
    user_id = interaction.user.id
    if user_id in recent_channels and guild.id in recent_channels[user_id]:
        for cid in recent_channels[user_id][guild.id][:10]:
            ch = guild.get_channel(cid)
            if ch and current.lower() in ch.name.lower():
                choices.append(app_commands.Choice(name=f"⭐ {ch.name}", value=str(ch.id)))
    for ch in guild.text_channels:
        if current.lower() in ch.name.lower():
            choices.append(app_commands.Choice(name=ch.name, value=str(ch.id)))
        if len(choices) >= 15:
            break
    return choices

async def category_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    choices = []
    for cat in interaction.guild.categories:
        if current.lower() in cat.name.lower():
            choices.append(app_commands.Choice(name=cat.name, value=str(cat.id)))
        if len(choices) >= 15:
            break
    return choices

async def channeltype_autocomplete(interaction: discord.Interaction, current: str):
    options = [("Text Channel", "text"), ("Voice Channel", "voice")]
    return [app_commands.Choice(name=n, value=v) for n, v in options if current.lower() in n.lower()][:15]

# ---------- XP / Level mechanics ----------
def xp_for_message(message_content: str) -> int:
    base = 10
    extra = min(len(message_content) // 15, 20)
    return base + extra

def required_xp_for_level(level: int) -> int:
    return 50 * (level ** 2) + 100

def total_xp_to_reach_level(level: int) -> int:
    total = 0
    for L in range(1, level + 1):
        total += required_xp_for_level(L)
    return total

def compute_level_from_total_xp(total_xp: int) -> int:
    level = 0
    while total_xp >= total_xp_to_reach_level(level + 1):
        level += 1
    return level

def get_rank_name_from_daily(daily_xp: int):
    for r, thresh in RANKS:
        if daily_xp >= thresh:
            return r
    return None

# ---------- Advanced Level Up Notification ----------
async def send_level_up_notification(member: discord.Member, old_level: int, new_level: int):
    if new_level > old_level:
        channel = client.get_channel(NOTIFICATION_CHANNEL_ID)
        if channel and channel.permissions_for(member.guild.me).send_messages:
            embed = discord.Embed(
                title="✨ LEVEL UP ACHIEVEMENT ✨",
                description=f"## {member.mention} has advanced to **Level {new_level}**!",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )

            progress_emojis = ["⬜"] * 10
            fill_count = min(new_level % 10, 10)
            for i in range(fill_count):
                progress_emojis[i] = "🟩"

            embed.add_field(
                name="Level Progress",
                value=f"`{''.join(progress_emojis)}`\n**{old_level}** → **{new_level}**",
                inline=False
            )

            if new_level % 10 == 0:
                embed.add_field(
                    name="🎯 Milestone Reached!",
                    value=f"You've reached a special level **{new_level}** milestone!",
                    inline=False
                )

            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_author(name=f"{member.display_name}'s Level Journey", icon_url=member.display_avatar.url)
            embed.set_footer(text=f"Level {new_level} • Keep climbing! 📈")

            await channel.send(embed=embed)

# ---------- Advanced Rank Up Notification ----------
async def send_rank_up_notification(member: discord.Member, old_rank: str, new_rank: str):
    if new_rank != old_rank:
        channel = client.get_channel(NOTIFICATION_CHANNEL_ID)
        if channel and channel.permissions_for(member.guild.me).send_messages:
            rank_emoji = RANK_EMOJIS.get(new_rank, "🏆")

            embed = discord.Embed(
                title=f"🏆 RANK PROMOTION 🏆",
                description=f"## {member.mention} has been promoted to {rank_emoji} **{new_rank} Rank**!",
                color=discord.Color(RANK_COLOR_HEX.get(new_rank, 0x5865F2)),
                timestamp=datetime.now(timezone.utc)
            )

            rank_index = RANK_ORDER.index(new_rank) if new_rank in RANK_ORDER else -1
            if rank_index > 0:
                next_rank = RANK_ORDER[rank_index - 1] if rank_index > 0 else None
                if next_rank:
                    embed.add_field(
                        name="Next Goal",
                        value=f"Next rank: **{next_rank}** {RANK_EMOJIS.get(next_rank, '')}",
                        inline=True
                    )

            embed.add_field(
                name="Rank Progress",
                value=f"**{old_rank if old_rank else 'No Rank'}** → **{new_rank}** {rank_emoji}",
                inline=True
            )

            if new_rank in ["S+", "A"]:
                embed.add_field(
                    name="Elite Status",
                    value="Welcome to the elite ranks! 🎖️",
                    inline=False
                )

            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_author(name=f"{member.display_name}'s Rank Achievement", icon_url=member.display_avatar.url)
            embed.set_footer(text=f"{new_rank} Rank • Keep up the great work! 💪")

            await channel.send(embed=embed)

# ---------- DB helpers ----------
async def add_message(guild_id: int, user_id: int, xp: int, channel_id: int):
    now = int(time.time())
    async with db_pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO users (guild_id, user_id, total_xp, daily_xp, daily_msgs, last_message_ts, channel_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET
            total_xp = users.total_xp + $3,
            daily_xp = users.daily_xp + $4,
            daily_msgs = users.daily_msgs + $5,
            last_message_ts = $6,
            channel_id = $7
        """, guild_id, user_id, xp, xp, 1, now, channel_id)

async def get_user_row(guild_id: int, user_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
        SELECT total_xp, daily_msgs, daily_xp
        FROM users
        WHERE guild_id=$1 AND user_id=$2
        """, guild_id, user_id)

    if not row:
        return {"total_xp": 0, "daily_msgs": 0, "daily_xp": 0}
    return {"total_xp": row['total_xp'], "daily_msgs": row['daily_msgs'], "daily_xp": row['daily_xp']}

async def reset_all_daily(guild_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET daily_msgs=0, daily_xp=0 WHERE guild_id=$1", guild_id)

async def reset_user_all(guild_id: int, user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)

async def force_set_manual_rank(guild_id: int, user_id: int, rank_str: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO manual_ranks (guild_id, user_id, forced_rank)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET forced_rank = $3
        """, guild_id, user_id, rank_str)

async def get_manual_rank(guild_id: int, user_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT forced_rank FROM manual_ranks WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    return row['forced_rank'] if row else None

async def clear_manual_rank(guild_id: int, user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM manual_ranks WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)

# ---------- Role management ----------
async def get_or_create_role(guild: discord.Guild, rank_name: str):
    role_name = f"{ROLE_PREFIX}{rank_name}"
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        return role
    try:
        role = await guild.create_role(
            name=role_name,
            reason="Auto-created rank role",
            color=discord.Color(RANK_COLOR_HEX.get(rank_name, 0))
        )
        return role
    except Exception as e:
        print(f"⚠️ Could not create role {role_name}: {e}")
        return None

async def remove_rank_roles_from_member(guild: discord.Guild, member: discord.Member):
    for rn in RANK_ORDER:
        r = discord.utils.get(guild.roles, name=f"{ROLE_PREFIX}{rn}")
        if r and r in member.roles:
            try:
                await member.remove_roles(r)
            except Exception:
                pass

async def assign_rank_role_for_member(guild: discord.Guild, member: discord.Member, rank_name: str):
    if not rank_name:
        return
    role = await get_or_create_role(guild, rank_name)
    if role:
        try:
            await member.add_roles(role)
        except Exception:
            pass

async def evaluate_and_update_member_rank(guild: discord.Guild, member: discord.Member, daily_xp: int):
    forced = await get_manual_rank(guild.id, member.id)
    if forced:
        await remove_rank_roles_from_member(guild, member)
        await assign_rank_role_for_member(guild, member, forced)
        return forced

    target_rank = None
    for rank, thresh in RANKS:
        if daily_xp >= thresh:
            target_rank = rank
            break

    await remove_rank_roles_from_member(guild, member)

    if target_rank:
        await assign_rank_role_for_member(guild, member, target_rank)

    return target_rank

# ---------- Leaderboard cache ----------
leaderboard_cache = {}

# ---------- STATUS / COUNTER / AUTO TASKS ----------
async def status_loop():
    await client.wait_until_ready()
    target_guild = None
    if GUILD_ID_ENV:
        try:
            gid = int(GUILD_ID_ENV)
            target_guild = client.get_guild(gid)
        except Exception:
            target_guild = None
    while not client.is_closed():
        try:
            guild = target_guild or (client.guilds[0] if client.guilds else None)
            if not guild:
                await asyncio.sleep(5)
                continue
            if custom_status.get(guild.id):
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=custom_status[guild.id]))
                await asyncio.sleep(STATUS_SWITCH_SECONDS)
                continue
            count = guild.member_count
            await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Total: {count} Members"))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)
            last = last_joined_member.get(guild.id)
            if last:
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Welcome {last}"))
            else:
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="Waiting for New Member"))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)
        except Exception as e:
            print(f"⚠️ status_loop error: {e}")
            await asyncio.sleep(5)

async def counter_updater():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for gid, channels in list(counter_channels.items()):
                guild = client.get_guild(gid)
                if not guild:
                    continue
                for ch_id, base_name in list(channels.items()):
                    ch = guild.get_channel(ch_id)
                    if ch:
                        new_name = f"{base_name} {guild.member_count}"
                        if ch.name != new_name:
                            try:
                                await ch.edit(name=new_name)
                            except Exception:
                                pass
        except Exception as e:
            print(f"⚠️ counter_updater error: {e}")
        await asyncio.sleep(COUNTER_UPDATE_SECONDS)

async def auto_message_task():
    await client.wait_until_ready()
    channel = client.get_channel(AUTO_CHANNEL_ID)

    # Debug info
    print(f"🔄 Auto message task started")
    print(f"📝 Loaded {len(AUTO_MESSAGES)} messages")
    print(f"📢 Target channel ID: {AUTO_CHANNEL_ID}")

    if not channel:
        print(f"❌ Auto channel {AUTO_CHANNEL_ID} not found. Auto messages disabled.")
        return

    print(f"✅ Found channel: {channel.name} ({channel.id})")

    while not client.is_closed():
        try:
            # Reload messages every 6 hours
            if AUTO_FILE_URL and int(time.time()) % 21600 == 0:
                print("🔄 Reloading messages from URL...")
                await load_auto_messages_from_url()

            if AUTO_MESSAGES:
                msg = random.choice(AUTO_MESSAGES)
                print(f"📤 Sending message: {msg[:50]}...")  # First 50 chars
                await channel.send(msg)
                print("✅ Message sent successfully")
            else:
                print("⚠️ No auto messages available to send")

        except Exception as e:
            print(f"❌ Auto message error: {e}")

        print(f"⏳ Waiting {AUTO_INTERVAL} seconds...")
        await asyncio.sleep(AUTO_INTERVAL)

# ---------- Daily reset (cron Asia/Karachi 00:00) ----------
async def evaluate_and_reset_for_guild(guild: discord.Guild):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, daily_xp FROM users WHERE guild_id=$1", guild.id)

    for row in rows:
        uid, dxp = row['user_id'], row['daily_xp']
        member = guild.get_member(uid)
        if not member:
            continue
        try:
            await evaluate_and_update_member_rank(guild, member, dxp)
        except Exception as e:
            print(f"⚠️ Rank update error for {member}: {e}")

    await reset_all_daily(guild.id)
    leaderboard_cache.pop(guild.id, None)
    print(f"✅ Daily reset completed for {guild.name}")

async def reset_daily_ranks_async():
    for guild in client.guilds:
        try:
            await evaluate_and_reset_for_guild(guild)
        except Exception as e:
            print(f"⚠️ Daily reset error guild {guild.id}: {e}")

def schedule_daily_reset():
    tz = pytz.timezone("Asia/Karachi")
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(lambda: asyncio.create_task(reset_daily_ranks_async()), "cron", hour=0, minute=0)
    scheduler.start()
    print("✅ Scheduled daily reset (00:00 Asia/Karachi)")

# ---------- SLASH COMMANDS (ADMIN / MISC as before) ----------
@tree.command(name="say", description="Send formatted message to a channel (Admin only)")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def say(interaction: discord.Interaction, channel_id: str, content: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)
    ch = await client.fetch_channel(int(channel_id))
    sent = await ch.send(content)
    update_recent_channel(interaction.user.id, interaction.guild.id, int(channel_id))
    await interaction.response.send_message(f"Sent ✅ ({sent.jump_url})", ephemeral=True)

@tree.command(name="embed", description="Send embed message (Admin only)")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def embed(interaction: discord.Interaction, channel_id: str, title: str, description: str, color: str = "#5865F2", url: str = ""):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)
    await interaction.response.send_message("Sending...", ephemeral=True)
    ch = await client.fetch_channel(int(channel_id))
    try:
        col = discord.Color(int(color.replace("#",""), 16))
    except Exception:
        col = discord.Color.blurple()
    e = discord.Embed(title=title, description=description, color=col)
    if url:
        e.url = url
    sent = await ch.send(embed=e)
    update_recent_channel(interaction.user.id, interaction.guild.id, int(channel_id))
    await interaction.edit_original_response(content=f"Embed sent ✅ ({sent.jump_url})")

@tree.command(name="edit", description="Edit existing message with link (Admin only)")
async def edit(interaction: discord.Interaction, message_link: str, new_content: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)
    parsed = parse_message_link(message_link)
    if not parsed:
        return await interaction.response.send_message("❌ Invalid message link.", ephemeral=True)
    _, channel_id, msg_id = parsed
    ch = await client.fetch_channel(int(channel_id))
    msg = await ch.fetch_message(int(msg_id))
    await msg.edit(content=new_content)
    await interaction.response.send_message("Edited ✅", ephemeral=True)

@tree.command(name="recent", description="Show your last used channels")
async def recent(interaction: discord.Interaction):
    user_id = interaction.user.id
    guild_id = interaction.guild.id
    if user_id not in recent_channels or guild_id not in recent_channels[user_id]:
        return await interaction.response.send_message("No recent channels yet.", ephemeral=True)
    ch_list = recent_channels[user_id][guild_id]
    guild = interaction.guild
    names = []
    for cid in ch_list[:10]:
        ch = guild.get_channel(cid)
        if ch:
            names.append(f"⭐ {ch.mention}")
    embed = discord.Embed(title="📌 Your Recent Channels", description="\n".join(names) if names else "None", color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="help", description="Show help (Admin commands are restricted)")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Bot Commands Help", color=discord.Color.blurple())
    embed.add_field(name="/say", value="(Admin) Send message to channel", inline=False)
    embed.add_field(name="/embed", value="(Admin) Send embed", inline=False)
    embed.add_field(name="/edit", value="(Admin) Edit message via link", inline=False)
    embed.add_field(name="/recent", value="Show your recent channels", inline=False)
    embed.add_field(name="/purge", value="(Admin) Delete messages", inline=False)
    embed.add_field(name="/setcounter", value="(Admin) Create live counter channel", inline=False)
    embed.add_field(name="/leaderboard", value="Show Top 10 by 24h XP (embed)", inline=False)
    embed.add_field(name="/rank", value="Show your rank (image card)", inline=False)
    embed.add_field(name="/addrank", value="(Admin) Force rank to user", inline=False)
    embed.add_field(name="/removefromleaderboard", value="(Admin) Remove user from leaderboard", inline=False)
    embed.add_field(name="/resetleaderboard", value="(Admin) Reset entire leaderboard", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="purge", description="Delete messages (Admin only)")
async def purge(interaction: discord.Interaction, number: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    if number < 1 or number > 100:
        return await interaction.response.send_message("❌ Choose between 1-100", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=number)
    await interaction.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)

@tree.command(name="setcounter", description="Create counter channel (Admin only)")
@app_commands.autocomplete(category_id=category_autocomplete, channel_type=channeltype_autocomplete)
async def setcounter(interaction: discord.Interaction, category_id: str, channel_name: str, channel_type: str, guild_counter: bool):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    category = discord.utils.get(interaction.guild.categories, id=int(category_id))
    if not category:
        return await interaction.response.send_message("❌ Category not found", ephemeral=True)
    if channel_type == "voice":
        new_ch = await category.create_voice_channel(channel_name)
    else:
        new_ch = await category.create_text_channel(channel_name)
    if guild_counter:
        try:
            await new_ch.edit(name=f"{channel_name} {interaction.guild.member_count}")
        except Exception:
            pass
    if interaction.guild.id not in counter_channels:
        counter_channels[interaction.guild.id] = {}
    counter_channels[interaction.guild.id][new_ch.id] = channel_name
    await interaction.response.send_message(f"✅ Counter created: {new_ch.mention}", ephemeral=True)

@tree.command(name="setcustomstatus", description="Set a custom status (Admin only)")
async def setcustomstatus(interaction: discord.Interaction, message: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    custom_status[interaction.guild.id] = message
    await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=message))
    await interaction.response.send_message("✅ Custom status set (default loop paused)", ephemeral=True)

@tree.command(name="setdefaultstatus", description="Resume default status loop (Admin only)")
async def setdefaultstatus(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    custom_status[interaction.guild.id] = None
    await interaction.response.send_message("✅ Default status loop resumed", ephemeral=True)

@tree.command(name="testauto", description="Test auto message system")
async def testauto(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)

    channel = client.get_channel(AUTO_CHANNEL_ID)
    if not channel:
        return await interaction.response.send_message(f"❌ Channel {AUTO_CHANNEL_ID} not found", ephemeral=True)

    await interaction.response.send_message(
        f"✅ Auto message system status:\n"
        f"• Channel: {channel.mention} ({AUTO_CHANNEL_ID})\n"
        f"• Messages loaded: {len(AUTO_MESSAGES)}\n"
        f"• Interval: {AUTO_INTERVAL} seconds\n"
        f"• Next message in: {AUTO_INTERVAL} seconds",
        ephemeral=True
    )

# ---------- MESSAGE FILTER + XP tracking ----------
@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    is_admin = message.author.guild_permissions.administrator
    has_bypass = any(role.name == BYPASS_ROLE for role in message.author.roles) if hasattr(message.author, 'roles') else False

    if not is_admin and not has_bypass:
        content_lower = message.content.lower()

        for bad in BAD_WORDS:
            if bad and bad in content_lower:
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    await message.channel.send(f"🚫 Hey {message.author.mention}, stop! Do not use offensive language. Continued violations may lead to a ban.", delete_after=8)
                except Exception:
                    pass

                # Automatically send to hardcoded report channel
                log_ch = client.get_channel(REPORT_CHANNEL_ID)
                if log_ch:
                    try:
                        await log_ch.send(f"⚠️ {message.author.mention} has misbehaved and used: **{bad}** (in {message.channel.mention})")
                    except Exception:
                        pass
                return

        if ("http://" in content_lower or "https://" in content_lower or "discord.gg/" in content_lower):
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.channel.send(f"🚫 {message.author.mention}, please do not advertise or share promotional links here. Contact the server admin for paid partnerships.", delete_after=8)
            except Exception:
                pass

            # Automatically send to hardcoded report channel
            log_ch = client.get_channel(REPORT_CHANNEL_ID)
            if log_ch:
                try:
                    await log_ch.send(f"⚠️ {message.author.mention} has advertised: `{message.content}` (in {message.channel.mention})")
                except Exception:
                    pass
            return

    if XP_CHANNEL_ID and message.channel.id != XP_CHANNEL_ID:
        return

    try:
        old_data = await get_user_row(message.guild.id, message.author.id)
        old_level = compute_level_from_total_xp(old_data['total_xp'])
        old_rank = None
        for r, thresh in RANKS:
            if old_data['daily_xp'] >= thresh:
                old_rank = r
                break

        xp = xp_for_message(message.content)
        await add_message(message.guild.id, message.author.id, xp, message.channel.id)

        new_data = await get_user_row(message.guild.id, message.author.id)
        new_level = compute_level_from_total_xp(new_data['total_xp'])

        new_rank = None
        for r, thresh in RANKS:
            if new_data['daily_xp'] >= thresh:
                new_rank = r
                break

        current_rank = await evaluate_and_update_member_rank(message.guild, message.author, new_data['daily_xp'])

        if new_level > old_level:
            await send_level_up_notification(message.author, old_level, new_level)

        if new_rank != old_rank:
            await send_rank_up_notification(message.author, old_rank, new_rank)

    except Exception as e:
        print("⚠️ XP add error:", e)

    if message.content.strip().lower().startswith("!ping"):
        try:
            await message.channel.send(f"🏓 Pong! Latency: {round(client.latency * 1000)}ms")
        except Exception:
            pass

# ---------- IMAGE CARD GENERATOR FOR /rank ----------
def _load_font_safe(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

async def _fetch_avatar_bytes(url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        return None
    return None

async def generate_rank_card_image(member: discord.Member, total_xp: int, daily_xp: int, rank_position: int):
    # compute levels & progress
    lvl = compute_level_from_total_xp(total_xp)
    current_total = total_xp_to_reach_level(lvl)
    next_total = total_xp_to_reach_level(lvl + 1)
    xp_in_level = total_xp - current_total
    xp_needed = max(1, next_total - current_total)
    progress_ratio = min(1.0, xp_in_level / xp_needed) if xp_needed > 0 else 1.0

    # canvas size
    W, H = 1100, 360

    # load background image (assets/bg.png)
    bg_path = "assets/bg.png"
    try:
        bg = Image.open(bg_path).convert("RGBA").resize((W, H))
    except Exception:
        # fallback to gradient bg
        bg = Image.new("RGBA", (W, H), (24, 26, 40, 255))
        draw_temp = ImageDraw.Draw(bg)
        for i in range(W):
            t = i / W
            r = int(24 + (60 - 24) * t)
            g = int(26 + (90 - 26) * t)
            b = int(40 + (150 - 40) * t)
            draw_temp.line([(i, 0), (i, H)], fill=(r, g, b))

    base = Image.new("RGBA", (W, H))
    base.paste(bg, (0, 0))

    # glass overlay + blur
    glass = Image.new("RGBA", (W, H), (255, 255, 255, 36))
    glass = glass.filter(ImageFilter.GaussianBlur(10))
    base = Image.alpha_composite(base, glass)

    draw = ImageDraw.Draw(base)

    # rounded translucent panel
    panel = Image.new("RGBA", (W - 80, H - 80), (255, 255, 255, 28))
    panel = panel.filter(ImageFilter.GaussianBlur(2))
    base.paste(panel, (40, 40), panel)

    # avatar (fetch)
    avatar_bytes = await _fetch_avatar_bytes(member.display_avatar.url) if member else None
    av_size = 200
    if avatar_bytes:
        try:
            avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((av_size, av_size))
            mask = Image.new("L", (av_size, av_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
            base.paste(avatar, (60, 80), mask)
            # subtle border
            draw.ellipse((56, 76, 60 + av_size, 80 + av_size), outline=(255,255,255,60), width=4)
        except Exception:
            avatar_bytes = None

    if not avatar_bytes:
        # draw placeholder circle
        draw.ellipse((60, 80, 60 + av_size, 80 + av_size), fill=(110, 110, 120, 255))
        draw.text((60 + av_size//2, 80 + av_size//2), "No\nAvatar", fill=(230,230,230), anchor="mm")

    # fonts
    font_bold = _load_font_safe("fonts/Montserrat-Bold.ttf", 36)
    font_reg = _load_font_safe("fonts/Montserrat-Regular.ttf", 22)
    font_light = _load_font_safe("fonts/Montserrat-Light.ttf", 18)

    # user texts
    name_x = 60 + av_size + 40
    draw.text((name_x, 95), member.display_name, font=font_bold, fill=(255,255,255,255))
    rank_name = await get_manual_rank(member.guild.id, member.id)
    if not rank_name:
        rank_name = get_rank_name_from_daily(daily_xp) or "No Rank"

    draw.text((name_x, 140), f"Level {lvl}  •  Rank {rank_name}", font=font_reg, fill=(230,230,230,230))
    draw.text((name_x, 170), f"Total XP: {total_xp}  •  24h XP: {daily_xp}", font=font_light, fill=(200,200,200,200))

    # progress bar
    pb_x1, pb_y1 = name_x, 240
    pb_w, pb_h = 760 - (name_x - 60), 28
    pb_x2 = pb_x1 + pb_w
    # background bar
    draw.rounded_rectangle([pb_x1, pb_y1, pb_x2, pb_y1 + pb_h], radius=14, fill=(255,255,255,60))
    # filled gradient
    fill_w = int(pb_w * progress_ratio)
    if fill_w > 0:
        # draw gradient fill
        for i in range(fill_w):
            t = i / max(1, fill_w)
            # left green -> right cyan-ish (subtle)
            r = int(46 + (80 - 46) * (1 - t))
            g = int(204 + (220 - 204) * t)
            b = int(113 + (140 - 113) * t)
            draw.line([(pb_x1 + i, pb_y1), (pb_x1 + i, pb_y1 + pb_h)], fill=(r, g, b))
    # progress text
    prog_text = f"{xp_in_level}/{xp_needed} XP  ({int(progress_ratio*100)}%)"
    w, h = draw.textsize(prog_text, font=font_light)
    draw.text((pb_x2 - w - 12, pb_y1 + (pb_h - h)//2), prog_text, font=font_light, fill=(20,20,20,230))

    # footer hint
    footer = f"#{rank_position}  •  Keep chatting to climb higher ✨"
    fw, fh = draw.textsize(footer, font=font_light)
    draw.text((W - fw - 40, H - fh - 24), footer, font=font_light, fill=(240,240,240,170))

    buffer = BytesIO()
    base.convert("RGBA").save(buffer, "PNG")
    buffer.seek(0)
    return buffer

# ---------- /rank command (image card) ----------
@tree.command(name="rank", description="Show your rank and level")
async def rank_cmd(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()  # give bot time to build image

    member = member or interaction.user
    if interaction.guild is None:
        return await interaction.followup.send("Guild-only.", ephemeral=True)

    # fetch user data
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT total_xp, daily_xp FROM users WHERE guild_id=$1 AND user_id=$2", interaction.guild.id, member.id)
            if not row:
                total_xp = 0
                daily_xp = 0
            else:
                total_xp = row['total_xp'] or 0
                daily_xp = row['daily_xp'] or 0

            # calculate rank position by total_xp (within guild)
            rows = await conn.fetch("SELECT user_id, total_xp FROM users WHERE guild_id=$1 ORDER BY total_xp DESC", interaction.guild.id)
            rank_position = len(rows)
            for i, r in enumerate(rows):
                if r['user_id'] == member.id:
                    rank_position = i + 1
                    break
    except Exception as e:
        print("⚠️ Error fetching rank data:", e)
        total_xp = 0
        daily_xp = 0
        rank_position = 0

    try:
        buffer = await generate_rank_card_image(member, total_xp, daily_xp, rank_position)
        file = discord.File(fp=buffer, filename="rank_card.png")
        await interaction.followup.send(file=file)
    except Exception as e:
        print("❌ Error generating rank card:", e)
        # fallback text response
        embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.blurple())
        lvl = compute_level_from_total_xp(total_xp)
        embed.add_field(name="Level", value=str(lvl), inline=True)
        rankname = await get_manual_rank(interaction.guild.id, member.id) or get_rank_name_from_daily(daily_xp) or "No Rank"
        embed.add_field(name="Rank", value=rankname, inline=True)
        embed.add_field(name="24h XP", value=str(daily_xp), inline=True)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed)

# ---------- /leaderboard command (advanced embed) ----------
@tree.command(name="leaderboard", description="Show server leaderboard (Top 10 by 24h XP)")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild
    if not guild:
        return await interaction.followup.send("Guild-only.", ephemeral=True)

    now_ts = time.time()
    cache = leaderboard_cache.get(guild.id)
    if cache and now_ts - cache[0] < 30:
        # cached embed -> send it (we stored the embed object and requester name)
        cached_embed, cached_footer = cache[1]
        # update footer to show current requester
        cached_embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        return await interaction.followup.send(embed=cached_embed)

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, daily_xp, total_xp
                FROM users
                WHERE guild_id=$1
                ORDER BY daily_xp DESC
                LIMIT 10
            """, guild.id)
    except Exception as e:
        print("⚠️ Error fetching leaderboard:", e)
        return await interaction.followup.send("❌ Failed to fetch leaderboard.", ephemeral=True)

    if not rows:
        embed_empty = discord.Embed(title=f"🏆 {guild.name} — Leaderboard", description="No activity yet. Start chatting to earn XP and climb the leaderboard! 💪", color=discord.Color.gold())
        embed_empty.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        return await interaction.followup.send(embed=embed_empty)

    # Build stylish embed
    title = f"🏆 {guild.name} — Top {len(rows)} (24h XP)"
    embed = discord.Embed(title=title, color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    if guild.icon:
        try:
            embed.set_thumbnail(url=guild.icon.url)
        except Exception:
            pass

    description_lines = []
    medal_emojis = ["🥇", "🥈", "🥉"]
    for idx, row in enumerate(rows, start=1):
        uid = row['user_id']
        daily = row['daily_xp'] or 0
        total = row['total_xp'] or 0
        member = guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        lvl = compute_level_from_total_xp(total)
        rank_name = None
        for r, thresh in RANKS:
            if daily >= thresh:
                rank_name = r
                break
        rank_display = f"{rank_name}" if rank_name else "No Rank"
        medal = medal_emojis[idx-1] if idx <= 3 else f"#{idx}"

        # make line with some unicode bullets and spacing for visual
        line = f"**{medal} {name}** — `Lvl {lvl}` • **{rank_display}** • ⭐ `{daily}` (24h) • Total `{total}`"
        description_lines.append(line)

    embed.description = "\n\n".join(description_lines)

    # Add a "Quick view" field with rank guide and legend
    rank_guide = " • ".join([f"{RANK_EMOJIS.get(r,'')} {r}" for r,_ in RANKS])
    embed.add_field(name="Ranks Guide", value=rank_guide, inline=False)
    embed.set_footer(text=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)

    # store in cache (embed object & requester) for short time
    leaderboard_cache[guild.id] = (now_ts, (embed, interaction.user.display_name))

    await interaction.followup.send(embed=embed)

# ---------- Admin Rank Commands ----------
@tree.command(name="addrank", description="Admin: force a rank to a user")
async def addrank(interaction: discord.Interaction, member: discord.Member, rank: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    rank = rank.strip()
    if rank not in RANK_ORDER:
        return await interaction.response.send_message(f"❌ Invalid rank. Choose from: {', '.join(RANK_ORDER)}", ephemeral=True)
    await force_set_manual_rank(interaction.guild.id, member.id, rank)
    await remove_rank_roles_from_member(interaction.guild, member)
    await assign_rank_role_for_member(interaction.guild, member, rank)
    await interaction.response.send_message("✅ Forced rank applied.", ephemeral=True)

@tree.command(name="removefromleaderboard", description="Admin: remove user from leaderboard (clear XP & ranks)")
async def removefromleaderboard(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    await reset_user_all(interaction.guild.id, member.id)
    await clear_manual_rank(interaction.guild.id, member.id)
    await remove_rank_roles_from_member(interaction.guild, member)
    await interaction.response.send_message("✅ Cleared user data and roles.", ephemeral=True)

@tree.command(name="resetleaderboard", description="Admin: reset entire guild leaderboard (clear all XP & ranks)")
async def resetleaderboard(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE guild_id=$1", interaction.guild.id)
        await conn.execute("DELETE FROM manual_ranks WHERE guild_id=$1", interaction.guild.id)
    for member in interaction.guild.members:
        try:
            await remove_rank_roles_from_member(interaction.guild, member)
        except Exception:
            pass
    await interaction.response.send_message("✅ Guild leaderboard reset.", ephemeral=True)

# ---------- EVENTS ----------
@client.event
async def on_ready():
    await init_db()

    # Load auto messages from external URL
    await load_auto_messages_from_url()

    # Channel verification
    channel = client.get_channel(AUTO_CHANNEL_ID)
    if channel:
        print(f"✅ Auto message channel found: #{channel.name}")
    else:
        print(f"❌ ERROR: Auto channel {AUTO_CHANNEL_ID} not found!")

    # Report channel verification
    report_channel = client.get_channel(REPORT_CHANNEL_ID)
    if report_channel:
        print(f"✅ Report channel found: #{report_channel.name}")
    else:
        print(f"❌ ERROR: Report channel {REPORT_CHANNEL_ID} not found!")

    try:
        if not hasattr(client, 'commands_synced'):
            await tree.sync()
            client.commands_synced = True
            print(f"✅ Commands synced successfully. Logged in as: {client.user}")

            print("📋 Registered Commands:")
            for command in tree.get_commands():
                print(f" /{command.name} - {command.description}")
        else:
            print(f"✅ Bot is ready. Commands already synced. Logged in as: {client.user}")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")

    client.loop.create_task(status_loop())
    client.loop.create_task(counter_updater())
    client.loop.create_task(auto_message_task())
    schedule_daily_reset()

@client.event
async def on_member_join(member):
    last_joined_member[member.guild.id] = member.name

# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing — set it in Railway variables.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing — add PostgreSQL database in Railway.")
    client.run(TOKEN)
