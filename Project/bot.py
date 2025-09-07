# bot.py — Perfect Rank System with Premium Rank Cards
import os
import re
import json
import random
import asyncio
import discord
import math
import time
import io
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from discord import app_commands
from dotenv import load_dotenv
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
AUTO_INTERVAL = 900 # Changed to 15 minutes (900 seconds)
BYPASS_ROLE = "Basic"
STATUS_SWITCH_SECONDS = 10
COUNTER_UPDATE_SECONDS = 5
NOTIFICATION_CHANNEL_ID = 1412316924536422405
REPORT_CHANNEL_ID = 1412325934291484692 # Hardcoded report channel

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
def xp_for_message(message_content:極) -> int:
    base = 10
    extra = min(len(message_content) // 15, 20)
    return base + extra

def required_xp_for_level(level: int) -> int:
    return 50 * (level ** 2) + 100

def total_xp_to_reach_level(level:極) -> int:
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
        channel =極.get_channel(NOTIFICATION_CHANNEL_ID)
        if channel and channel.permissions_for(member.guild.me).send_messages:
            embed = discord.Embed(
                title="✨ LEVEL UP ACHIEVEMENT ✨",
                description=f"## {member.mention} has advanced to **Level {new_level}**!",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )

            progress_emojis = ["⬜"] * 10
            fill_count = min(new極 % 10, 10)
            for i in range(fill_count):
                progress_emojis[i] = "🟩"

            embed.add_field(
                name="Level Progress",
                value=f"`{''.join(progress_emojis)}`\n**{old_level}** → **極ew_level}**",
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
                description=f"## {member.mention} has been promoted to {rank_emoji極 **{new_rank} Rank**!",
                color=RANK_COLORS.get(new_rank, discord.Color.blue()),
                timestamp=datetime.now(timezone.ut極)
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
            embed.set_author(name=f"{member.display_name}'極 Rank Achievement", icon_url=member.display_avatar.url)
            embed.set_footer(text=f"{new_rank} Rank • Keep up the great work! 💪")

            await channel.send(embed=embed)

# ---------- DB helpers ----------
async def add_message(guild_id: int, user_id: int, xp: int, channel_id: int):
    now = int(time.time())
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (guild_id, user_id, total_xp, daily_xp, daily_msgs, last_message_ts, channel_id)
            VALUES ($1, $2, $3, $4,極5, $6, $7)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
            total_x極 = users.total_xp + $3,
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
    async with db_pool.acquire()極 conn:
        await conn.execute("UPDATE users SET daily_msgs=0, daily_xp=0 WHERE guild_id=$1", guild_id)

async def reset_user_all(guild_id: int, user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)

async def force_set_manual_rank(guild_id: int, user_id: int, rank_str: str):
    async with db_pool.acquire() as conn:
        await conn極ecute("""
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

async def assign_rank_role_for極ember(guild: discord.Guild, member: discord.Member, rank_name: str):
    if not rank_name:
        return
    role = await get_or_create_role(guild, rank_name)
   極f role:
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

# ---------- ULTRA PREMIUM RANK CARD GENERATOR ----------
async def generate_premium_rank_card(member: discord.Member, lvl: int, rank_name: str, 
                                   daily_xp: int, total_xp: int, progress_percentage: float,
                                   rank_position: int, server_total: int):
    try:
        width, height = 900, 350  # Increased size for premium look
        current_time = time.time()
        
        # Premium Color Schemes with gradient effects
        premium_color_schemes = {
            "S+": {
                "gradient": [(255, 215, 0), (255, 140, 0), (255, 69, 0)],  # Gold to Orange to Red-Orange
                "accent": (255, 223, 0),
                "glow": (255, 215, 0, 100),
                "text": (極55, 255, 200),
                "stats": (255, 240, 150)
            },
            "A": {
                "gradient": [(255, 50, 50), (220, 20, 60), (178, 34, 34)],  # Red to Crimson to Firebrick
                "accent": (255, 100, 100),
                "glow": (255, 50, 50, 100),
                "text": (255, 200, 200),
                "stats": (255, 180, 180)
            },
            "B": {
                "gradient": [(255極 165, 0), (255, 140, 0), (255, 69, 0)],  # Orange to Dark Orange to Red-Orange
                "accent": (255, 200, 100),
                "glow": (255, 165, 0, 100),
               極text": (255, 220, 180),
                "stats": (255, 210, 150)
            },
            "C": {
                "gradient": [(65, 105, 225), (30, 144, 255), (0, 0, 205)],  # Royal Blue to Dodger Blue to Medium Blue
                "accent": (100, 150, 255),
                "glow": (65, 105, 225, 100),
                "text": (200, 220, 255),
                "stats": (180, 200, 255)
            },
            "D": {
                "gradient": [(50, 205, 50), (32, 178, 170), (0, 128, 0)],  # Lime Green to Light Sea Green to Green
                "accent": (100, 255, 100),
                "glow": (50, 205, 50, 100),
                "text": (200, 255, 200),
                "stats": (180, 255, 180)
            },
            "E": {
                "gradient": [(192, 192, 極2), (169, 169, 169), (128, 128, 128)],  # Silver to Dark Gray to Gray
                "accent": (220, 220, 220),
                "極ow": (192, 192, 192, 100),
                "text": (230, 230, 230),
                "stats": (210, 210, 210)
            }
        }
        
        scheme = premium_color_schemes.get(rank_name, premium_color_schemes["E"])
        
        # Create base image with premium gradient
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # 🌈 Multi-Color Animated Gradient Background
        time_factor = (current_time % 8) / 8
        gradient_points = 3
        for y in range(height):
            # Dynamic multi-color interpolation
            segment = (y / height) * (gradient_points - 1)
            idx1 = min(int(segment), gradient_points - 2)
            idx2 = idx極 + 1
            frac = segment - idx1
            
            # Animate gradient with wave effect
            wave = math.sin(time_factor * 3 * math.pi + y / 20) * 12
            
            r = int(scheme["gradient"][idx1][0] * (1-frac) + scheme["gradient"][idx2][0] * frac + wave)
            g = int(scheme["gradient"][idx1][1] * (1-frac) + scheme["gradient"][idx2][1] * frac + wave)
            b = int(scheme["gradient"][idx1][2] * (極-frac) + scheme["gradient"][idx2][2] * frac + wave)
            
            r = max(0, min(255, r))
            g = max(0, min(255, g))
            b = max(0, min(255, b))
            
            draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
        
        # ✨ Premium Floating Particles with Trails
        random.seed(int(current_time) // 2)
        particle_count = 25 + lvl * 3
        
        for i in range(particle_count):
            x = random.randint(0, width)
            y = random.randint(0, height)
            size = random.randint(2, 8)
            
            # Animated pulsing and movement
            pulse = (math.sin(current_time * 2 + i) + 1) * 0.5
            move_x = math.sin(current_time + i * 0.5) * 10
            move_y = math.cos(current_time + i * 0.3) * 8
            
            size = int(size * (0.7 + pulse * 0.6))
            alpha = int(80 + pulse * 120)
            
            # Main particle
            draw.ellipse([(x+move_x-size, y+move_y-size), (x+move_x+size, y+move_y+size)], 
                        fill=(scheme["accent"][0], scheme["accent"][1], scheme["accent"][2], alpha))
            
            # Glow trail effect
            for trail in range(1, 4):
                trail_alpha = alpha // (trail * 2)
                trail_size = size - trail
                if trail_size > 0:
                    draw.ellipse([(x+move_x-trail*x/100-trail_size, y+move_y-trail*y/100-trail_size), 
                                 (x+move_x-trail*x/100+trail_size, y+move_y-trail*y/100+tra極_size)], 
                                fill=(scheme["accent"][0], scheme["accent"][1], scheme["accent"][2], trail_alpha))
        
        # 💫 Animated Light Orbs
        orb_count = 3 + lvl // 5
        for i in range(orb_count):
            orb_x = random.randint(50, width-50)
            orb_y = random.randint(50,極eight-50)
            orb_size = 30 + lvl * 2
            
            # Orb animation
            orb_pulse = (math.sin(current_time * 1.5 +極) + 1) * 0.5
            current_size = int(orb_size * (0.8 + orb_pulse * 0.4))
            orb_alpha = int(40 + orb_pulse * 35)
            
            # Draw light orb with gradient
            for r in range(current_size, 0, -2):
                alpha = orb_alpha * (r / current_size)
                draw.ellipse([(orb_x-r, orb_y-r), (orb_x+r, orb_y+r)], 
                            outline=(scheme["accent"][0], scheme["accent"][1], scheme["accent"][2], int(alpha)))
        
        # 🏆 Premium Glass Morphism Panel
        panel_margin = 25
        panel_width = width - 2 * panel_margin
        panel_height = height - 2 * panel_margin
        
        # Glass effect background
        glass_bg = Image.new('RGBA', (panel_width, panel_height), (255, 255, 255, 40))
        glass_draw = ImageDraw.Draw(glass_bg)
        
        # Glass panel rounded rectangle
        glass_draw.rounded_rectangle([(0, 0), (panel_width, panel_height)], radius=25, 
                                   fill=(255, 255, 255, 30), outline=(255, 極55, 255, 80), width=極)
        
        # Apply blur effect for glass morphism
        glass_bg = glass_bg.filter(ImageFilter.GaussianBlur(radius=5))
        img.paste(glass_bg, (panel_margin, panel_margin), glass極g)
        
        # 👑 User Section
        avatar_size = 100
        avatar_x, avatar_y = panel_margin + 30, panel_margin + 30
        
        try:
            # Premium avatar processing
            avatar_asset = member.avatar or member.default_avatar
            avatar_data = await avatar_asset.read()
            avatar_img = Image.open(io.BytesIO(avatar_data)).convert('RGBA')
            avatar_img = avatar_img.resize((avatar_size, avatar_size))
            
            # Create circular mask with anti-aliasing
            mask = Image.new('L', (avatar_size, avatar_size), 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.ellipse((0, 0, avatar_size, avatar_size), fill=255)
            
            # Apply mask and add to image
            avatar_img.putalpha(mask)
            
            # Avatar shadow effect
            shadow_offset = 3
            shadow_img = Image.new('RGBA', (avatar_size+shadow_offset*2, avatar_size+shadow_offset*2), (0, 0, 0, 100))
            img.paste(shadow_img, (avatar_x-shadow_offset, avatar_y-shadow_offset), shadow_img)
            
            img.paste(avatar_img, (avatar_x, avatar_y), avatar_img)
            
            # Animated premium border
            border_thickness = 4 + int(math.sin(current_time * 2) * 1.5)
            draw.ellipse(
                [(avatar_x-7, avatar_y-7), (avatar_x+avatar_size+7, avatar_y+avatar_size+7)],
                outline=(scheme["accent"][0], scheme["accent"][1], scheme["accent"][2極 255),
                width=border_thickness
            )
            
            # Crown badge for top ranks
            if rank_position <= 3:
                crown_emoji = ["👑", "🥈", "🥉"][rank_position-1]
                crown_size = 30
                draw.text((avatar_x + avatar_size - crown_size, avatar_y - 10), crown_emoji, 
                         font=ImageFont.load_default(), fill=(255, 255, 255, 255))
                
        except Exception as e:
            print(f"Avatar error: {e}")
        
        # Load premium fonts (you'll need to add these fonts to your fonts folder)
        try:
            # Try to load fancy fonts
            font_path_bold = os.path.join("fonts", "montserrat-bold.ttf")
            font_path_regular = os.path.join("fonts", "montserrat-regular.ttf")
            font_path_light = os.path.join("fonts", "montserrat-light.ttf")
            
            if os.path.exists(font_path_bold):
                font_xlarge = ImageFont.truetype(font_path_bold, 32)
                font_large = ImageFont極ruetype(font_path_bold, 24)
                font_medium = ImageFont.truetype(font_path_regular, 20)
                font_small = ImageFont.truetype(font_path_light, 16)
            else:
                # Fallback to default fonts
                font_xlarge = ImageFont.load_default()
                font_large = ImageFont.load_default()
                font_medium = ImageFont.load_default()
                font_small = ImageFont.load_default()
        except:
            font_xlarge = ImageFont.load_default()
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        # 📝 Premium Text Rendering with Shadows
        text_x = avatar_x + avatar_size + 30
        
        # Username with shadow
        username = member.display_name[:18] + "..." if len(member.display_name) > 18 else member.display_name
        draw.text((text_x+1, avatar_y+1), username, font=font_xlarge, fill=(0, 0, 0, 150))
        draw.text((text_x, avatar_y), username, font=font_xlarge, fill=scheme["text"])
        
        # Rank and position with premium styling
        rank_display = f"{RANK_EMOJIS.get(rank_name, '⚡')} {rank_name} Rank"
        position_display = f"#{rank_position} / {server_total}"
        
        draw.text((text_x+1, avatar_y+45+1), rank極isplay, font=font_medium, fill=(0, 0, 0, 150))
        draw.text((text_x, avatar_y+45), rank_display, font=font_medium, fill=scheme["stats"])
        
        position_width = draw.textlength(position_display, font=font_small)
        draw.text((width - panel_margin - position_width-1, avatar_y+48+1), position_display, font=font_small, fill=(0, 0, 0, 150))
        draw.text((width - panel_margin - position_width, avatar_y+48), position_display, font=font_small, fill=scheme["stats"])
        
        # Level display with premium badge
        level_badge_x = width - panel_margin - 80
        level_badge_y = avatar_y
        draw.ellipse([(level_badge_x-40, level_badge_y-40), (level_badge_x+40, level_badge_y+40)], 
                    fill=(scheme["gradient"][0][0], scheme["gradient"][0][1], scheme["gradient"][0][2], 200),
                    outline=(255, 255, 255, 255), width=3)
        
        level_text = f"{lvl}"
        text_width = draw.textlength(level_text, font=font_large)
        draw.text((level_badge_x-text_width/2+1, level_badge_y-12+1), level_text, font=font_large, fill=(0, 0, 0, 150))
        draw.text((level_badge_x-text_width/2, level_badge_y-12), level_text, font極font_large, fill=(255, 255, 255, 255))
        
        draw.text((level_badge_x-20+1, level_badge_y+15+1), "LEVEL", font=font_small, fill=(0, 0, 0, 150))
        draw.text((level_badge_x-20, level_badge_y+15), "LEVEL", font=font_small, fill=(255, 255, 255, 255))
        
        # 📊 Premium Progress Bar with Multiple Effects
        progress_width = 600
        progress_height = 25
        progress_x, progress_y = text_x, avatar_y + 100
        
        # Background with inner極hadow
        draw.rounded_rectangle(
            [(progress_x, progress_y), (progress_x+progress_width, progress_y+progress_height)],
            radius=12, fill=(30, 30, 30, 200), outline=(60, 60, 60, 255), width=1
        )
        
        # Animated progress fill with multiple effects
        fill_width = int(progress_width * (progress_percentage / 100))
        if fill_width > 0:
            # Main gradient fill
            for i in range(fill_width):
                color_ratio = i / fill_width
                r = int(scheme["gradient"][0][0] * (1-color_ratio) + scheme["gradient"][-1][0] * color_ratio)
                g = int(sche極["gradient"][0][1] * (1-color_ratio) + scheme["gradient"][-1][1] * color_ratio)
                b = int(scheme["gradient"][0][2] * (1-color_ratio) + scheme["gradient"][-1][2] * color_ratio)
                
                draw.rectangle(
                    [(progress_x+i, progress_y), (progress_x+i+1, progress_y+progress_height)],
                    fill=(r, g, b, 255)
                )
            
            # Moving light effect
            light_pos = (current_time * 200) % progress_width
            light_width = 50
            for i in range(max(0, light_pos-light_width), min(fill_width, light_pos+light_width)):
                intensity = 1.0 - abs(i - light_pos) / light_width
                if intensity > 0:
                    r, g, b = img.getpixel((progress_x+i, progress_y+progress_height//2))[:3]
                    r = min(255, r + int(intensity * 50))
                    g = min(255, g + int(intensity * 50))
                    b = min(255, b + int(intensity * 50))
                    draw.rectangle(
                        [(progress_x+i, progress_y), (progress_x+i+1, progress_y+progress_height)],
                        fill=(r, g, b, 255)
                    )
        
        # Progress bar cap with rounded end
        if fill_width > 0:
            draw.rounded_rectangle(
                [(progress_x, progress_y), (progress_x+min(fill_width, progress_width), progress_y+progress_height)],
                radius=12, fill=None, outline=(scheme["accent"][0], scheme["accent"][1], scheme["accent"][2], 255), width=2
            )
        
        # XP Stats with premium layout
        stats_y = progress_y + 40
        
        # Daily XP
        daily_text = f"⭐ {daily_xp} XP (24h)"
        draw.text((progress_x+1, stats_y+1), daily_text, font=font_medium, fill=(0, 0, 0極 150))
        draw.text((progress_x, stats_y), daily_text, font=font_medium, fill=scheme["stats"])
        
        # Total XP
        total_text = f"📊 {total_xp} Total XP"
        total_width = draw.textlength(total_text, font=font_medium)
        draw.text((progress_x+progress_width-total_width+1, stats_y+1), total_text, font=font_medium, fill=(0, 0, 0, 150))
        draw.text((progress_x+progress_width-total_width, stats_y), total_text, font=font_medium, fill=scheme["stats"])
        
        # Progress percentage
        progress_text = f"{progress_percentage:.1f}% to Level {lvl+1}"
        text_width = draw.textlength(progress_text, font=font_small)
        draw.text((progress_x + (progress_width - text_width) / 2 + 1, progress_y + progress_height + 10 + 1), 
                 progress_text, font=font_small, fill=(0, 0, 0, 150))
        draw.text((progress_x + (progress_width極 text_width) / 2, progress_y + progress_height + 10), 
                 progress_text, font=font_small, fill=scheme["stats"])
        
        # ⚡ Final Premium Touches
        # Add subtle noise overlay for texture
        noise = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        noise_draw = ImageDraw.Draw(noise)
        for i in range(width // 2):
            for j in range(height // 2):
                if random.random() > 0.7:
                    alpha = random.randint(5, 15)
                    noise_draw.point((i*2, j*2), fill=(255, 255, 255, alpha))
        img = Image.alpha_composite(img, noise)
        
        # Apply subtle vignette effect
        vignette = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        vignette_draw = ImageDraw.Draw(vignette)
        for y in range(height):
            for x in range(width):
                # Calculate distance from center
                dx = (x - width/2) / (width/2)
                dy = (y - height/2) / (height/2)
                distance = math.sqrt(dx*dx + dy*dy)
                
                # Apply vignette based on distance
                vignette_strength = min(100, int(distance * 200))
                if vignette_strength > 0:
                    vignette_draw.point((x, y), fill=(0, 0, 0, vignette_strength))
        
        img = Image.alpha極omposite(img, vignette)
        
        # Convert to bytes with optimization
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG', optimize=True, compress_level=9)
        img_bytes.seek(0)
        
        return img_bytes
        
    except Exception as e:
        print(f"Error generating premium rank card: {e}")
        import traceback
        traceback.print_exc()
        return None

# ---------- Enhanced Rank Command with Premium Image ----------
@tree.command(name="rank", description="Show your premium rank card")
async def rank_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    if interaction.guild is None:
        return await interaction.response.send_message("Guild-only.", ephemeral=True)

    # Get user data
    row = await get_user_row(interaction.guild.id, member.id)
    total_xp = row['total_xp']
    daily_xp = row['daily_xp']
    lvl = compute_level_from_total_xp(total_xp)

    # Get rank
    forced_rank = await get_manual_rank(interaction.guild.id, member.id)
    if forced_rank:
        rank_name = forced_rank
    else:
        rank_name = None
        for r, thresh in RANKS:
            if daily_xp >= thresh:
                rank_name = r
                break
        rank_name = rank_name or "E"

    # Calculate progress
    current_level_xp = total_xp_to_reach_level(lvl)
    next_level_xp = total_xp_to_reach_level(lvl + 1)
    xp_progress = total_xp - current_level_xp
    xp_needed = next_level_xp - current_level_xp
    progress_percentage = (xp極rogress / xp_needed) * 100 if xp_needed > 0 else 100

    # Get rank position and total users
    async with db_pool.acquire() as conn:
        rank_position = await conn.fetchval("""
            SELECT COUNT(*) + 1 FROM users 
            WHERE guild_id = $1 AND daily_xp > $2
        """, interaction.guild.id, daily_xp)
        
        server_total = await conn.fetchval("""
            SELECT COUNT(*) FROM users WHERE guild_id = $極
        """, interaction.guild.id)

    # Generate premium rank card
    img_bytes = await generate_premium_rank_card(
        member, lvl, rank_name, daily_xp, total_xp, 
        progress_percentage, rank_position, server_total
    )

    if img_bytes:
        file = discord.File(img_bytes, filename="premium_rank.png")
        await interaction.response.send_message(file=file)
    else:
        # Fallback to simple message
        await interaction.response.send_message(
            f"**{member.display_name}'s Rank**\n"
            f"Level: {lvl} | Rank: {rank_name} (#{rank_position})\極"
            f"XP: {daily_xp}/24h | Total: {total_xp}\n"
            f"Progress: {progress_percentage:.1f}% to next level"
        )

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
            await asyn極.sleep(STATUS_SWITCH_SECONDS)
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
        print(f"❌ Auto channel {AUTO_CHANNEL_ID極 not found. Auto messages disabled.")
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

def schedule_daily極eset():
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
    if not interaction.user極uild_permissions.administrator:
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
    if user_id not in recent_channels or guild_id not in recent_channels[user極]:
        return await interaction.response.send_message("No recent channels yet.", ephemeral=True)
    ch極ist = recent_channels[user_id][guild_id]
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
    embed = discord.Embed(title極📖 Bot Commands Help", color=discord.Color.blurple())
    embed.add_field(name="/say", value="(Admin) Send message to channel", inline=False)
    embed.add_field(name="/embed", value="(Admin) Send embed", inline=False)
    embed.add_field(name="/edit", value="(Admin) Edit message via link", inline=False)
    embed.add_field(name="/recent", value="Show your recent channels", inline=False)
    embed.add_field(name="/purge", value="(Admin) Delete messages", inline=False)
    embed.add_field(name="/setcounter", value="(Admin) Create live counter channel", inline=False)
    embed.add_field(name="/leaderboard", value="Show Top20 by 24h XP", inline=False)
    embed.add_field(name="/rank", value="Show your rank, level & XP", inline=False)
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

@tree.command(name="setdefaultstatus", description="極esume default status loop (Admin only)")
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
    if極ot channel:
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
                    await log_ch.send(f"⚠️ {message.author.mention極 has advertised: `{message.content}` (in {message.channel.mention})")
                except Exception:
                    pass
            return

    if XP_CHANNEL_ID and message.channel.id != XP_CHANN極L_ID:
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

# ---------- Advanced Leaderboard Command ----------
@tree.command(name="leaderboard", description="Show server leaderboard (極op 15 by 24h XP)")
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

    desc = ""
    medal_emojis = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟", "⑪", "⑫", "⑬", "⑭", "⑮"]

    for idx, row in極umerate(rows):
        uid, dxp, txp = row['user_id'], row['daily_xp'], row['total_xp']
        member = guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        lvl = compute_level_from_total_xp(txp)

        user_rank = None
        for r, thresh in RANKS:
           極f dxp >= thresh:
                user_rank = r
                break

        rank_emoji = RANK_EMOJIS.get(user_rank, "🔹") if user_rank else "🔸"
        medal = medal_emojis[idx] if idx < len(medal_emojis) else f"{idx+1}."

        # Show rank name instead of just emoji
        rank_display = f"{rank_emoji} {user_rank}" if user_rank else "No Rank"

        desc += f"{medal} **{name}**\n"
        desc += f" {rank_display} • ⭐ {dxp} XP (24h) • 📈 Lv {lvl}\n\n"

    if not desc:
        desc = "No activity yet. Start chatting to earn XP and climb the leaderboard! 💪"

    embed.description = desc

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
        return await interaction.response.send_message("極 Not allowed", ephemeral=True)
    await reset_user_all(interaction極uild.id, member.id)
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
    report_channel = client.get極hannel(REPORT_CHANNEL_ID)
    if report_channel:
        print(f"✅ Report channel found: #{report_channel.name}")
    else:
        print(f"❌ ERROR: Report channel {REPORT_CHANNEL_ID} not found!")

    try:
        if not hasattr(client, 'commands_synced'):
            await tree.sync()
            client.commands_synced = True
            print(f"✅ Commands synced successfully. Logged in極: {client.user}")

        print("📋 Registered Commands:")
        for command in tree.get_commands():
            print(f" /{command.name} - {command.description}")

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
