# bot.py — Final merged version (status loop + all features)
import os
import re
import json
import random
import asyncio
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_ENV = os.getenv("GUILD_ID")  # optional: prefer using this for status member count
AUTO_FILE = os.getenv("AUTO_FILE", "automsg.json")

# ---------- Config (change if needed) ----------
AUTO_CHANNEL_ID = 1412316924536422405  # as you gave
AUTO_INTERVAL = 300  # seconds (5 min)
BYPASS_ROLE = "Basic"  # role name that bypasses filters
STATUS_SWITCH_SECONDS = 10  # 10 seconds between the two statuses
COUNTER_UPDATE_SECONDS = 5  # counter update frequency

# ---------- Intents / Client / Tree ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ---------- In-memory stores ----------
recent_channels = {}            # {user_id: {guild_id: [channel_ids]}}
last_joined_member = {}         # {guild_id: member_name}
custom_status = {}              # {guild_id: status_string or None}
counter_channels = {}           # {guild_id: {channel_id: base_name}}
AUTO_MESSAGES = []              # loaded from AUTO_FILE
REPORT_CHANNELS = {}            # {guild_id: channel_id}

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
    # recent channels first
    user_id = interaction.user.id
    if user_id in recent_channels and guild.id in recent_channels[user_id]:
        for cid in recent_channels[user_id][guild.id][:10]:
            ch = guild.get_channel(cid)
            if ch and current.lower() in ch.name.lower():
                choices.append(app_commands.Choice(name=f"⭐ {ch.name}", value=str(ch.id)))
    # then guild text channels
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

# ---------- STATUS LOOP (member-based) ----------
async def status_loop():
    await client.wait_until_ready()
    # prefer using provided GUILD_ID env if present
    target_guild = None
    if GUILD_ID_ENV:
        try:
            gid = int(GUILD_ID_ENV)
            target_guild = client.get_guild(gid)
        except Exception:
            target_guild = None

    # if not provided, use first guild the bot is in
    while not client.is_closed():
        try:
            guild = target_guild or (client.guilds[0] if client.guilds else None)
            if not guild:
                await asyncio.sleep(5)
                continue

            # check if custom status set for this guild
            if custom_status.get(guild.id):
                # show custom status until cleared
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=custom_status[guild.id]))
                await asyncio.sleep(STATUS_SWITCH_SECONDS)
                continue

            # 1) Total members
            count = guild.member_count
            await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Total: {count} Members"))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)

            # 2) Welcome last joined or waiting
            last = last_joined_member.get(guild.id)
            if last:
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Welcome {last}"))
            else:
                await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="Waiting for New Member"))
            await asyncio.sleep(STATUS_SWITCH_SECONDS)
        except Exception as e:
            print(f"⚠️ status_loop error: {e}")
            await asyncio.sleep(5)

# ---------- COUNTER UPDATER ----------
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

# ---------- AUTO MESSAGE TASK ----------
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
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- PURGE (defer to avoid "app did not respond") ----------
@tree.command(name="purge", description="Delete messages (Admin only)")
async def purge(interaction: discord.Interaction, number: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    if number < 1 or number > 100:
        return await interaction.response.send_message("❌ Choose between 1-100", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=number)
    await interaction.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)

# ---------- COUNTER ----------
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
        # set initial name and register for updates
        try:
            await new_ch.edit(name=f"{channel_name} {interaction.guild.member_count}")
        except Exception:
            pass
        if interaction.guild.id not in counter_channels:
            counter_channels[interaction.guild.id] = {}
        counter_channels[interaction.guild.id][new_ch.id] = channel_name
    await interaction.response.send_message(f"✅ Counter created: {new_ch.mention}", ephemeral=True)

# ---------- STATUS CONTROL ----------
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

# ---------- AUTO MESSAGES (persistent) ----------
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

# ---------- REPORTS (logs) ----------
@tree.command(name="setreport", description="Set report log channel (Admin only)")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def setreport(interaction: discord.Interaction, channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Not allowed", ephemeral=True)
    REPORT_CHANNELS[interaction.guild.id] = int(channel_id)
    ch = interaction.guild.get_channel(int(channel_id))
    await interaction.response.send_message(f"✅ Reports will be sent to {ch.mention}", ephemeral=True)

# ---------- MESSAGE FILTER (bad words + links) ----------
@client.event
async def on_message(message: discord.Message):
    # allow bots and DMs through
    if message.author.bot or message.guild is None:
        return

    # allow admins
    if message.author.guild_permissions.administrator:
        return

    # allow bypass role
    try:
        if any(role.name == BYPASS_ROLE for role in message.author.roles):
            return
    except Exception:
        pass

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
            # report log if set
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

    # text-command fallback (simple ping)
    if message.content.strip().lower().startswith("!ping"):
        try:
            await message.channel.send(f"🏓 Pong! Latency: {round(client.latency * 1000)}ms")
        except Exception:
            pass

# ---------- EVENTS ----------
@client.event
async def on_ready():
    try:
        await tree.sync()
        print(f"✅ Commands synced. Logged in as: {client.user}")
    except Exception as e:
        print(f"⚠️ Sync error: {e}")
    # start background tasks
    client.loop.create_task(status_loop())
    client.loop.create_task(counter_updater())
    client.loop.create_task(auto_message_task())

@client.event
async def on_member_join(member):
    last_joined_member[member.guild.id] = member.name

# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing — set it in Railway variables.")
    client.run(TOKEN)
