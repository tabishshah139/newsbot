import os
import json
import random
import asyncio
import discord
from discord import app_commands
from dotenv import load_dotenv

# ------------------------
# ENV / TOKEN
# ------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Optional: set AUTO_FILE via env to a Volume path (e.g., /data/automsg.json) for persistence across deploys
AUTO_FILE = os.getenv("AUTO_FILE", "automsg.json")

# ------------------------
# INTENTS + CLIENT
# ------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True  # make sure "Server Members Intent" is enabled in Dev Portal

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ------------------------
# CONFIG
# ------------------------
STATUS_LIST = [
    "Managing the server",
    "Protecting the chat",
    "Counting members 👥",
    "Automating tasks 🤖",
]

# Auto-message target & interval
AUTO_CHANNEL_ID = 1412316924536422405  # <- tumhara channel ID
AUTO_INTERVAL = 300  # seconds (5 minutes)

# Filter / logs
BYPASS_ROLE = "Basic"     # is role wale users bypass karenge filters
REPORT_CHANNEL_ID = None  # /setreport se set hoga

# ------------------------
# AUTO-MESSAGE STORAGE
# ------------------------
def load_auto_messages():
    try:
        if os.path.exists(AUTO_FILE):
            with open(AUTO_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        # Default empty list if file not found or invalid
        return []
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

# ------------------------
# BAD WORDS LOAD (graceful)
# ------------------------
try:
    with open("badwords.txt", "r", encoding="utf-8") as f:
        BAD_WORDS = [w.strip().lower() for w in f if w.strip()]
    print(f"✅ Loaded {len(BAD_WORDS)} bad words.")
except FileNotFoundError:
    BAD_WORDS = []
    print("⚠️ 'badwords.txt' not found. Filter will run with an empty list.")
except Exception as e:
    BAD_WORDS = []
    print(f"⚠️ Error loading 'badwords.txt': {e}")

# ------------------------
# BACKGROUND TASKS
# ------------------------
async def status_task():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            status = random.choice(STATUS_LIST)
            await client.change_presence(activity=discord.Game(name=status))
        except Exception as e:
            print(f"⚠️ status_task error: {e}")
        await asyncio.sleep(30)

async def auto_message_task():
    await client.wait_until_ready()
    channel = client.get_channel(AUTO_CHANNEL_ID)
    if not channel:
        print("⚠️ Auto-message channel not found! Check AUTO_CHANNEL_ID.")
        return
    while not client.is_closed():
        try:
            if AUTO_MESSAGES:
                msg = random.choice(AUTO_MESSAGES)
                await channel.send(msg)
                print(f"✅ Auto message sent: {msg}")
        except Exception as e:
            print(f"⚠️ Error sending auto message: {e}")
        await asyncio.sleep(AUTO_INTERVAL)

# ------------------------
# COMMANDS (Admins Only)
# ------------------------
@tree.command(name="say", description="Make the bot say something (admin only).")
async def say(interaction: discord.Interaction, message: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)
    await interaction.channel.send(message)
    await interaction.response.send_message("✅ Message sent!", ephemeral=True)

@tree.command(name="purge", description="Delete a number of messages (admin only).")
async def purge(interaction: discord.Interaction, amount: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)

# ----- Auto message commands -----
@tree.command(name="addautomsg", description="Add a new auto message (admin only).")
async def addautomsg(interaction: discord.Interaction, message: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)
    AUTO_MESSAGES.append(message)
    save_auto_messages()
    await interaction.response.send_message(f"✅ Added:\n`{message}`", ephemeral=True)

@tree.command(name="listautomsg", description="List all stored auto messages (admin only).")
async def listautomsg(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)
    if not AUTO_MESSAGES:
        return await interaction.response.send_message("ℹ️ No auto messages stored yet.", ephemeral=True)
    msg_list = "\n".join([f"{idx+1}. {msg}" for idx, msg in enumerate(AUTO_MESSAGES)])
    embed = discord.Embed(title="📜 Stored Auto Messages", description=msg_list, color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def auto_message_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    for idx, msg in enumerate(AUTO_MESSAGES, start=1):
        if current.lower() in msg.lower():
            choices.append(app_commands.Choice(name=f"{idx}. {msg[:50]}", value=str(idx)))
        if len(choices) >= 25:
            break
    return choices

@tree.command(name="removeautomsg", description="Remove an auto message by index (admin only).")
@app_commands.autocomplete(index=auto_message_autocomplete)
async def removeautomsg(interaction: discord.Interaction, index: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)
    try:
        idx = int(index)
    except ValueError:
        return await interaction.response.send_message("❌ Invalid index.", ephemeral=True)
    if idx < 1 or idx > len(AUTO_MESSAGES):
        return await interaction.response.send_message("❌ Index out of range. Use `/listautomsg`.", ephemeral=True)
    removed = AUTO_MESSAGES.pop(idx - 1)
    save_auto_messages()
    await interaction.response.send_message(f"🗑️ Removed:\n`{removed}`", ephemeral=True)

# ----- Filter logs channel -----
@tree.command(name="setreport", description="Set the channel for filter logs (admin only).")
async def setreport(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)
    global REPORT_CHANNEL_ID
    REPORT_CHANNEL_ID = channel.id
    await interaction.response.send_message(f"✅ Report logs will go to {channel.mention}", ephemeral=True)

# ------------------------
# MESSAGE FILTERS
# ------------------------
@client.event
async def on_message(message: discord.Message):
    # Ignore bots and DMs
    if message.author.bot or message.guild is None:
        return

    # BYPASS role
    try:
        if any(role.name == BYPASS_ROLE for role in message.author.roles):
            return
    except Exception:
        pass

    content_lower = message.content.lower()

    # Bad words filter (simple contains match)
    for bad in BAD_WORDS:
        if bad and bad in content_lower:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.channel.send(
                    f"⚠️ Hey {message.author.mention}, please avoid using bad language or you may be penalized.",
                    delete_after=10
                )
            except Exception:
                pass
            if REPORT_CHANNEL_ID:
                log_ch = client.get_channel(REPORT_CHANNEL_ID)
                if log_ch:
                    try:
                        await log_ch.send(f"🚨 {message.author.mention} used bad word: `{bad}` in {message.channel.mention}")
                    except Exception:
                        pass
            return

    # Link / advertise filter
    if "http://" in content_lower or "https://" in content_lower or "discord.gg/" in content_lower:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.channel.send(
                f"⚠️ {message.author.mention}, advertising is not allowed. Please contact admins for partnerships.",
                delete_after=10
            )
        except Exception:
            pass
        if REPORT_CHANNEL_ID:
            log_ch = client.get_channel(REPORT_CHANNEL_ID)
            if log_ch:
                try:
                    await log_ch.send(f"🚨 {message.author.mention} tried to advertise: `{message.content}` in {message.channel.mention}")
                except Exception:
                    pass
        return

# ------------------------
# READY
# ------------------------
@client.event
async def on_ready():
    try:
        await tree.sync()
        print(f"✅ Synced slash commands. Logged in as {client.user}")
    except Exception as e:
        print(f"⚠️ Slash sync error: {e}")
    client.loop.create_task(status_task())
    client.loop.create_task(auto_message_task())

# ------------------------
# RUN
# ------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN missing. Set it in Railway variables.")
    client.run(TOKEN)
