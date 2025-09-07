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
AUTO_FILE_URL = os.getenv("AUTO_MESSAGES_URL") # GitHub raw URL
DATABASE_URL = os.getenv("DATABASE_URL")
XP_CHANNEL_ID = int(os.getenv("XP_CHANNEL_ID", 0))

# ---------- Config ----------
AUTO_CHANNEL_ID = 1412316924536422405
AUTO_INTERVAL = 1800 # Changed to 30 minutes (1800 seconds)
BYPASS_ROLE = "Basic"
STATUS_SWITCH_SECONDS = 30 # Increased from 10 to 30 seconds
COUNTER_UPDATE_SECONDS = 30 # Increased from 5 to 30 seconds
NOTIFICATION_CHANNEL_ID = 1412316924536422405
REPORT_CHANNEL_ID = 1412325934291484692 # Hardcoded report channel
CACHE_DURATION = 300 # 5 minutes cache for leaderboard

# Rank thresholds (EASY PROGRESSION)
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
                color=RANK_COLORS.get(new_rank, discord.Color.blue()),
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
        await conn.execute("DELETE FROM manual_ranks WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)

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
            color=RANK_COLORS.get(rank_name, discord.Color.default())
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
            
            # Custom status check (priority)
            if custom_status.get(guild.id):
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=custom_status[guild.id]))
                await asyncio.sleep(STATUS_SWITCH_SECONDS)
                continue
            
            # ✅ APKA ORIGINAL STATUS LOOP WAPIS
            # 1. Member count status
            count = guild.member_count
            await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Members: {count}"))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)
            
            # 2. Welcome recent member status
            last = last_joined_member.get(guild.id)
            if last:
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Welcome {last}"))
            else:
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="Active & Online"))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)
            
            # 3. Leaderboard watching status
            await client.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="the leaderboard"))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)
            
        except Exception as e:
            print(f"⚠️ status_loop error: {e}")
            await asyncio.sleep(30)

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

    print(f"✅ Found channel: #{channel.name} ({channel.id})")

    last_reload_time = 0
    
    while not client.is_closed():
        try:
            # Reload messages every 12 hours (43200 seconds)
            current_time = time.time()
            if AUTO_FILE_URL and (current_time - last_reload_time) >= 43200:
                print("🔄 Reloading messages from URL...")
                await load_auto_messages_from_url()
                last_reload_time = current_time

            if AUTO_MESSAGES:
                msg = random.choice(AUTO_MESSAGES)
                print(f"📤 Sending message: {msg[:50]}...") # First 50 chars
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
    scheduler.add_job(
        lambda: asyncio.create_task(reset_daily_ranks_async()), 
        "cron", 
        hour=0, 
        minute=0,
        misfire_grace_time=3600  # Allow 1 hour grace period
    )
    scheduler.start()
    print("✅ Scheduled daily reset (00:00 Asia/Karachi)")

# ---------- Auto Cleanup Left Users ----------
async def cleanup_left_users():
    """Remove users who have left the server from database"""
    for guild in client.guilds:
        try:
            async with db_pool.acquire() as conn:
                # Get all user IDs from database for this guild
                db_users = await conn.fetch("SELECT user_id FROM users WHERE guild_id=$1", guild.id)
                db_user_ids = {row['user_id'] for row in db_users}
                
                # Get all current member IDs
                current_member_ids = {member.id for member in guild.members}
                
                # Find users who left
                left_user_ids = db_user_ids - current_member_ids
                
                if left_user_ids:
                    # Remove left users from database
                    await conn.execute("DELETE FROM users WHERE guild_id=$1 AND user_id = ANY($2::bigint[])", 
                                     guild.id, list(left_user_ids))
                    await conn.execute("DELETE FROM manual_ranks WHERE guild_id=$1 AND user_id = ANY($2::bigint[])", 
                                     guild.id, list(left_user_ids))
                    
                    print(f"✅ Removed {len(left_user_ids)} left users from database for guild {guild.name}")
                    
        except Exception as e:
            print(f"⚠️ Error cleaning up left users for guild {guild.id}: {e}")

def schedule_user_cleanup():
    """Schedule automatic user cleanup every hour"""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(cleanup_left_users, 'interval', hours=1)
    scheduler.start()
    print("✅ Scheduled user cleanup (every 1 hour)")

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
    embed.add_field(name="/leaderboard", value="Show Top20 by 24h XP", inline=False)
    embed.add_field(name="/rank", value="Show your rank, level & XP", inline=False)
    embed.add_field(name="/addrank", value="(Admin) Force rank to user", inline=False)
    embed.add_field(name="/removefromleaderboard", value="(Admin) Remove user from leaderboard (clear XP & ranks)", inline=False)
    embed.add_field(name="/resetleaderboard", value="(Admin) Reset entire leaderboard (clear all XP & ranks)", inline=False)
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
    
    # Guild ID ke hisaab se custom status clear karo
    if interaction.guild.id in custom_status:
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

# ---------- MESSAGE FILTER + XP tracking (FIXED FOR ADMINS) ----------
@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    # Pehle XP check karein (lighter operation)
    if XP_CHANNEL_ID and message.channel.id == XP_CHANNEL_ID:
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
    
    # Phir moderation check karein (heavier operation)
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

    if message.content.strip().lower().startswith("!ping"):
        try:
            await message.channel.send(f"🏓 Pong! Latency: {round(client.latency * 1000)}ms")
        except Exception:
            pass

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

    rank_value = f"{rank_emoji} **{rank_name}**{rank_source}" if rank_name else "🔸 **No Rank Yet**"
    embed.add_field(name="🏆 Current Rank", value=rank_value, inline=True)

    embed.add_field(name="📈 Level", value=f"**{lvl}**", inline=True)

    embed.add_field(name="💎 Total XP", value=f"**{total_xp}**", inline=True)

    filled_blocks = int(progress_percentage / 10)
    progress_bar = "🟩" * filled_blocks + "⬜" * (10 - filled_blocks)

    embed.add_field(
        name="🚀 Level Progress",
        value=f"{progress_bar}\n**{xp_progress}**/{xp_needed} XP (**{progress_percentage:.1f}%**)",
        inline=False
    )

    embed.add_field(name="⭐ 24h XP", value=f"**{daily_xp}**", inline=True)

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
            value=f"**{next_rank[0]}** - {xp_needed} XP needed",
            inline=True
        )

    rank_info = " | ".join([f"{r}: {t} XP" for r, t in RANKS])
    embed.set_footer(text=f"Rank Requirements: {rank_info}")

    await interaction.response.send_message(embed=embed)

# ---------- Fixed Leaderboard Command (No Duplication) ----------
@tree.command(name="leaderboard", description="Show server leaderboard (Top 15 by 24h XP)")
async def leaderboard(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("Guild-only.", ephemeral=True)
    
    now_ts = time.time()
    cache = leaderboard_cache.get(guild.id)
    if cache and now_ts - cache[0] < CACHE_DURATION:
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
            LIMIT 10
        """, guild.id)

    embed = discord.Embed(
        title=f"🏆 {guild.name} — Daily Leaderboard",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    desc = ""
    medal_emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for idx, row in enumerate(rows):
        uid, dxp, txp = row['user_id'], row['daily_xp'], row['total_xp']
        member = guild.get_member(uid)
        
        if member:
            name = member.display_name
            lvl = compute_level_from_total_xp(txp)

            user_rank = None
            for r, thresh in RANKS:
                if dxp >= thresh:
                    user_rank = r
                    break

            rank_emoji = RANK_EMOJIS.get(user_rank, "🔹") if user_rank else "🔸"
            medal = medal_emojis[idx] if idx < len(medal_emojis) else f"{idx+1}."

            # Sirf ek entry dikhao - Top 3 ke liye medal, baaki ke liye number
            rank_display = f"{rank_emoji} {user_rank}" if user_rank else "No Rank"
            desc += f"{medal} **{name}** - {rank_display} • ⭐ {dxp} XP • 📈 Lv {lvl}\n\n"

    if not desc:
        desc = "No activity yet. Start chatting to earn XP and climb the leaderboard! 💪"

    embed.description = desc

    # Top 3 members ko alag se fields mein na dikhao (yehi duplication cause kar raha tha)
    rank_guide = " | ".join([f"{RANK_EMOJIS.get(r, '')} {r}" for r in RANK_ORDER])
    embed.set_footer(text=f"Ranks: {rank_guide} | Reset daily at 12:00 AM PKT")

    return embed

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
    schedule_user_cleanup()

@client.event
async def on_member_join(member):
    last_joined_member[member.guild.id] = member.name

@client.event
async def on_member_remove(member):
    """Automatically remove user from database when they leave"""
    try:
        await reset_user_all(member.guild.id, member.id)
        print(f"✅ Removed {member.name} from database (left server)")
    except Exception as e:
        print(f"⚠️ Error removing {member.name} from database: {e}")

# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing — set it in Railway variables.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing — add PostgreSQL database in Railway.")
    
    # Add error handling for client run
    try:
        client.run(TOKEN)
    except Exception as e:
        print(f"❌ Critical error in client run: {e}")

