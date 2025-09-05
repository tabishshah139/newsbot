# bot.py — PostgreSQL version with XP/Level/Rank/Leaderboard
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

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_ENV = os.getenv("GUILD_ID")
AUTO_FILE = os.getenv("AUTO_FILE", "automsg.json")
DATABASE_URL = os.getenv("DATABASE_URL")
XP_CHANNEL_ID = int(os.getenv("XP_CHANNEL_ID", 0))

# ---------- Config ----------
AUTO_CHANNEL_ID = 1412316924536422405
AUTO_INTERVAL = 300
BYPASS_ROLE = "Basic"
STATUS_SWITCH_SECONDS = 10
COUNTER_UPDATE_SECONDS = 5

# Rank thresholds
RANKS = [("S+", 400), ("A", 350), ("B", 300), ("C", 250), ("D", 200), ("E", 150)]
RANK_ORDER = [r[0] for r in RANKS]
ROLE_PREFIX = ""

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
REPORT_CHANNELS = {}
db_pool: Pool = None

# ---------- Database Setup ----------
async def init_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        print("✅ Connected to PostgreSQL database")
        
        async with db_pool.acquire() as conn:
            # Create users table
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
            
            # Create manual_ranks table
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

# ---------- Load / Save auto messages ----------
def load_auto_messages():
    try:
        if os.path.exists(AUTO_FILE):
            with open(AUTO_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        print(f"⚠️ Failed to load {AUTO_FILE}: {e}")
    return []

def save_auto_messages():
    try:
        with open(AUTO_FILE, "w", encoding="utf-8") as f:
            json.dump(AUTO_MESSAGES, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Failed to save {AUTO_FILE}: {e}")

AUTO_MESSAGES = load_auto_messages()

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
    base = 5
    extra = len(message_content) // 50
    return base + extra

def required_xp_for_level(level: int) -> int:
    req = 50
    if level == 1:
        return req
    for _ in range(2, level + 1):
        inc = max(25, int(req * 0.15))
        req += inc
    return req

def total_xp_to_reach_level(level: int) -> int:
    total = 0
    for L in range(1, level + 1):
        total += required_xp_for_level(L)
    return total

def compute_level_from_total_xp(total_xp: int) -> int:
    level = 0
    while True:
        next_total = total_xp_to_reach_level(level + 1)
        if total_xp >= next_total:
            level += 1
        else:
            break
    return level

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
            WHERE guild_id=$1 AND user_id=$2 AND channel_id=$3
        """, guild_id, user_id, XP_CHANNEL_ID)
        
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
        role = await guild.create_role(name=role_name, reason="Auto-created rank role")
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
async def build_leaderboard_embed(guild: discord.Guild):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, daily_xp, total_xp 
            FROM users 
            WHERE guild_id=$1 AND channel_id=$2 
            ORDER BY daily_xp DESC 
            LIMIT 20
        """, guild.id, XP_CHANNEL_ID)
    
    embed = discord.Embed(title=f"{guild.name} — Leaderboard (Top 20 by 24h XP)", timestamp=datetime.now(timezone.utc))
    try:
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
    except Exception:
        pass
    
    desc = ""
    rank_num = 1
    for row in rows:
        uid, dxp, txp = row['user_id'], row['daily_xp'], row['total_xp']
        member = guild.get_member(uid)
        name = member.display_name if member else f"User ID {uid}"
        lvl = compute_level_from_total_xp(txp)
        desc += f"**{rank_num}. {name}** — {dxp} XP (24h) • {txp} XP total • Lv {lvl}\n"
        rank_num += 1
    
    if not desc:
        desc = "No activity yet."
    embed.description = desc
    embed.set_footer(text="XP = earned per message; Ranks based on 24h XP thresholds")
    return embed

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
    if not channel:
        print(f"⚠️ Auto channel {AUTO_CHANNEL_ID} not found. Auto messages disabled.")
        return
    while not client.is_closed():
        try:
            if AUTO_MESSAGES:
                msg = random.choice(AUTO_MESSAGES)
                await channel.send(msg)
        except Exception as e:
            print(f"⚠️ auto_message_task error: {e}")
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
        except Exception:
            pass
    
    await reset_all_daily(guild.id)
    leaderboard_cache.pop(guild.id, None)

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

# ---------- SLASH COMMANDS ----------
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
    embed.add_field(name="/setreport", value="(Admin) Set report logs channel", inline=False)
    embed.add_field(name="/addautomsg / listautomsg / removeautomsg", value="(Admin) Manage auto messages", inline=False)
    embed.add_field(name="/leaderboard", value="Show Top20 by 24h XP", inline=False)
    embed.add_field(name="/rank", value="Show your rank, level & XP", inline=False)
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

@tree.command(name="addautomsg", description="Add an auto message (Admin only)")
async def addautomsg(interaction: discord.Interaction, message: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    AUTO_MESSAGES.append(message)
    save_auto_messages()
    await interaction.response.send_message("✅ Auto message added", ephemeral=True)

@tree.command(name="listautomsg", description="List auto messages (Admin only)")
async def listautomsg(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    if not AUTO_MESSAGES:
        return await interaction.response.send_message("No auto messages stored.", ephemeral=True)
    text = "\n".join([f"{i+1}. {m}" for i, m in enumerate(AUTO_MESSAGES)])
    embed = discord.Embed(title="📜 Auto Messages", description=text, color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def auto_message_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    for idx, msg in enumerate(AUTO_MESSAGES, start=1):
        if current.lower() in msg.lower():
            choices.append(app_commands.Choice(name=f"{idx}. {msg[:50]}", value=str(idx)))
        if len(choices) >= 25:
            break
    return choices

@tree.command(name="removeautomsg", description="Remove an auto message (Admin only)")
@app_commands.autocomplete(index=auto_message_autocomplete)
async def removeautomsg(interaction: discord.Interaction, index: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    try:
        idx = int(index)
    except Exception:
        return await interaction.response.send_message("❌ Invalid selection", ephemeral=True)
    if idx < 1 or idx > len(AUTO_MESSAGES):
        return await interaction.response.send_message("❌ Index out of range", ephemeral=True)
    removed = AUTO_MESSAGES.pop(idx - 1)
    save_auto_messages()
    await interaction.response.send_message(f"🗑️ Removed: `{removed}`", ephemeral=True)

@tree.command(name="setreport", description="Set report log channel (Admin only)")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def setreport(interaction: discord.Interaction, channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    REPORT_CHANNELS[interaction.guild.id] = int(channel_id)
    ch = interaction.guild.get_channel(int(channel_id))
    await interaction.response.send_message(f"✅ Reports will be sent to {ch.mention}", ephemeral=True)

# ---------- MESSAGE FILTER + XP tracking ----------
@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    if message.author.guild_permissions.administrator:
        return

    try:
        if any(role.name == BYPASS_ROLE for role in message.author.roles):
            return
    except Exception:
        pass

    # ✅ Check if message is in XP channel
    if XP_CHANNEL_ID and message.channel.id != XP_CHANNEL_ID:
        return

    content_lower = message.content.lower()

    # check bad words
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
            rid = REPORT_CHANNELS.get(message.guild.id)
            if rid:
                log_ch = message.guild.get_channel(rid)
                if log_ch:
                    try:
                        await log_ch.send(f"⚠️ {message.author.mention} has misbehaved and used: **{bad}** (in {message.channel.mention})")
                    except Exception:
                        pass
            return

    # check links / adverts
    if ("http://" in content_lower or "https://" in content_lower or "discord.gg/" in content_lower):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.channel.send(f"🚫 {message.author.mention}, please do not advertise or share promotional links here. Contact the server admin for paid partnerships.", delete_after=8)
        except Exception:
            pass
        rid = REPORT_CHANNELS.get(message.guild.id)
        if rid:
            log_ch = message.guild.get_channel(rid)
            if log_ch:
                try:
                    await log_ch.send(f"⚠️ {message.author.mention} has advertised: `{message.content}` (in {message.channel.mention})")
                except Exception:
                    pass
        return

    # XP & daily tracking
    try:
        xp = xp_for_message(message.content)
        await add_message(message.guild.id, message.author.id, xp, message.channel.id)
        row = await get_user_row(message.guild.id, message.author.id)
        try:
            await evaluate_and_update_member_rank(message.guild, message.author, row['daily_xp'])
        except Exception:
            pass
    except Exception as e:
        print("⚠️ XP add error:", e)

    # text-command fallback (simple ping)
    if message.content.strip().lower().startswith("!ping"):
        try:
            await message.channel.send(f"🏓 Pong! Latency: {round(client.latency * 1000)}ms")
        except Exception:
            pass

# ---------- LEADERBOARD & RANK Commands ----------
@tree.command(name="leaderboard", description="Show server leaderboard (Top 20 by 24h XP)")
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

@tree.command(name="rank", description="Show your rank and level")
async def rank_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    if interaction.guild is None:
        return await interaction.response.send_message("Guild-only.", ephemeral=True)
    
    row = await get_user_row(interaction.guild.id, member.id)
    lvl = compute_level_from_total_xp(row['total_xp'])
    rank_name = None
    forced = await get_manual_rank(interaction.guild.id, member.id)
    if forced:
        rank_name = forced
    else:
        for r, thresh in RANKS:
            if row['daily_xp'] >= thresh:
                rank_name = r
                break
    embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Rank (24h)", value=rank_name or "None", inline=True)
    embed.add_field(name="Level", value=str(lvl), inline=True)
    embed.add_field(name="Total XP", value=str(row['total_xp']), inline=True)
    embed.add_field(name="24h XP", value=str(row['daily_xp']), inline=True)
    await interaction.response.send_message(embed=embed)

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
    # Initialize database first
    await init_db()
    
    # Sync commands only once
    try:
        if not hasattr(client, 'commands_synced'):
            synced = await tree.sync()
            client.commands_synced = True
            print(f"✅ {len(synced)} commands synced. Logged in as: {client.user}")
        else:
            print(f"✅ Already synced commands. Logged in as: {client.user}")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")
    
    # Start background tasks
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
