import discord
from discord import app_commands
import asyncio
import random
import json
import os

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================
# CONFIG
# =========================
STATUS_LIST = ["Managing the server", "Protecting the chat", "Counting members 👥", "Automating tasks 🤖"]

AUTO_CHANNEL_ID = 1412316924536422405  # Final auto-message channel ID
AUTO_INTERVAL = 300  # 5 minutes
AUTO_FILE = "automsg.json"

REPORT_CHANNEL_ID = None  # set dynamically by /setreport
BYPASS_ROLE = "Basic"

# =========================
# AUTO MESSAGE STORAGE
# =========================
def load_auto_messages():
    if os.path.exists(AUTO_FILE):
        with open(AUTO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_auto_messages():
    with open(AUTO_FILE, "w", encoding="utf-8") as f:
        json.dump(AUTO_MESSAGES, f, indent=2, ensure_ascii=False)

AUTO_MESSAGES = load_auto_messages()

# =========================
# STATUS TASK
# =========================
async def status_task():
    await client.wait_until_ready()
    while not client.is_closed():
        status = random.choice(STATUS_LIST)
        await client.change_presence(activity=discord.Game(name=status))
        await asyncio.sleep(30)

# =========================
# AUTO MESSAGE TASK
# =========================
async def auto_message_task():
    await client.wait_until_ready()
    channel = client.get_channel(AUTO_CHANNEL_ID)
    if not channel:
        print("⚠️ Auto-message channel not found! Check AUTO_CHANNEL_ID.")
        return

    while not client.is_closed():
        if AUTO_MESSAGES:
            msg = random.choice(AUTO_MESSAGES)
            try:
                await channel.send(msg)
                print(f"✅ Auto message sent: {msg}")
            except Exception as e:
                print(f"⚠️ Error sending auto message: {e}")
        await asyncio.sleep(AUTO_INTERVAL)

# =========================
# COMMANDS
# =========================
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

# =========================
# AUTO MESSAGE COMMANDS (ADMIN ONLY)
# =========================
@tree.command(name="addautomsg", description="Add a new auto message (admin only).")
async def addautomsg(interaction: discord.Interaction, message: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)

    AUTO_MESSAGES.append(message)
    save_auto_messages()
    await interaction.response.send_message(f"✅ Added new auto message:\n`{message}`", ephemeral=True)

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
            display_text = f"{idx}. {msg[:50]}"
            choices.append(app_commands.Choice(name=display_text, value=str(idx)))
        if len(choices) >= 25:
            break
    return choices

@tree.command(name="removeautomsg", description="Remove an auto message by index (admin only).")
@app_commands.autocomplete(index=auto_message_autocomplete)
async def removeautomsg(interaction: discord.Interaction, index: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)

    idx = int(index)
    if idx < 1 or idx > len(AUTO_MESSAGES):
        return await interaction.response.send_message("❌ Invalid index. Use `/listautomsg` first.", ephemeral=True)

    removed = AUTO_MESSAGES.pop(idx - 1)
    save_auto_messages()
    await interaction.response.send_message(f"🗑️ Removed auto message:\n`{removed}`", ephemeral=True)

# =========================
# FILTER SYSTEM (Bad words + Links)
# =========================
with open("badwords.txt", "r", encoding="utf-8") as f:
    BAD_WORDS = [w.strip().lower() for w in f.readlines() if w.strip()]

@tree.command(name="setreport", description="Set the channel for filter logs.")
async def setreport(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can use this command.", ephemeral=True)

    global REPORT_CHANNEL_ID
    REPORT_CHANNEL_ID = channel.id
    await interaction.response.send_message(f"✅ Report logs will now go to {channel.mention}", ephemeral=True)

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Bypass role check
    if any(role.name == BYPASS_ROLE for role in message.author.roles):
        return

    content_lower = message.content.lower()

    # Bad word filter
    for bad in BAD_WORDS:
        if bad in content_lower:
            await message.delete()
            await message.channel.send(
                f"⚠️ Hey {message.author.mention}, stop using bad words or you may get banned!",
                delete_after=10
            )
            if REPORT_CHANNEL_ID:
                log_channel = client.get_channel(REPORT_CHANNEL_ID)
                if log_channel:
                    await log_channel.send(f"🚨 {message.author.mention} used bad word: `{bad}`")
            return

    # Link filter
    if "http://" in content_lower or "https://" in content_lower:
        await message.delete()
        await message.channel.send(
            f"⚠️ {message.author.mention}, please don’t advertise here. Contact admins for partnership.",
            delete_after=10
        )
        if REPORT_CHANNEL_ID:
            log_channel = client.get_channel(REPORT_CHANNEL_ID)
            if log_channel:
                await log_channel.send(f"🚨 {message.author.mention} tried to advertise: `{message.content}`")

# =========================
# ON READY
# =========================
@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user}")
    client.loop.create_task(status_task())
    client.loop.create_task(auto_message_task())
