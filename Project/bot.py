import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load .env (locally use hota hai, Railway me zaroori nahi but safe hai)
load_dotenv()

# -------- ENV VARIABLES --------
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = os.getenv("APPLICATION_ID")
GUILD_ID = os.getenv("GUILD_ID")

print("DEBUG ENV:", {
    "DISCORD_TOKEN": TOKEN,
    "APPLICATION_ID": APP_ID,
    "GUILD_ID": GUILD_ID
})

if not TOKEN:
    raise ValueError("❌ DISCORD_TOKEN missing in Railway environment!")
if not APP_ID:
    raise ValueError("❌ APPLICATION_ID missing in Railway environment!")
if not GUILD_ID:
    raise ValueError("❌ GUILD_ID missing in Railway environment!")

try:
    GUILD_ID = int(GUILD_ID.strip())
except Exception as e:
    raise ValueError(f"❌ GUILD_ID must be a number, got: {GUILD_ID!r}") from e

# -------- BOT SETUP --------
intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Helper Functions ----------
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

# ---------- Slash Commands ----------
@client.tree.command(name="ping", description="Check if bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🏓 Bot is alive ✅", ephemeral=True)

@client.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Bot Commands Help",
        description="Here are the available commands:",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="/ping",
        value="Check if the bot is alive (anyone can use).",
        inline=False
    )
    embed.add_field(
        name="/say",
        value="(Admin only) Send a formatted message to a selected channel.\n"
              "**Options:** channel, content, bold, underline, code_lang, typing_ms",
        inline=False
    )
    embed.add_field(
        name="/embed",
        value="(Admin only) Send an embed message with title, description, color, and URL.",
        inline=False
    )
    embed.add_field(
        name="/edit",
        value="(Admin only) Edit an existing bot message using its link.",
        inline=False
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

# Say command with custom channel autocomplete
@client.tree.command(name="say", description="Send formatted message to channel")
async def say(
    interaction: discord.Interaction,
    channel: str,
    content: str,
    bold: bool=False,
    underline: bool=False,
    code_lang: str="",
    typing_ms: int=0
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    await interaction.response.send_message("Sending...", ephemeral=True)

    channel_obj = interaction.guild.get_channel(int(channel))
    if typing_ms > 0:
        async with channel_obj.typing():
            await asyncio.sleep(typing_ms / 1000)

    final = format_content(content, bold, underline, code_lang)
    sent = await channel_obj.send(final)
    await interaction.edit_original_response(content=f"Sent ✅ ({sent.jump_url})")

# Autocomplete for /say
@say.autocomplete("channel")
async def channel_autocomplete(interaction: discord.Interaction, current: str):
    try:
        if not interaction.guild:
            return []
        channels = [
            c for c in interaction.guild.text_channels
            if c.permissions_for(interaction.user).send_messages
        ]
        results = [
            app_commands.Choice(name=c.name, value=str(c.id))
            for c in channels if current.lower() in c.name.lower()
        ][:15]
        return results
    except Exception as e:
        print(f"⚠️ Autocomplete error (/say): {e}")
        return []

# Embed command
@client.tree.command(name="embed", description="Send embed message")
async def embed(interaction: discord.Interaction, channel: str, title: str, description: str, color: str="#5865F2", url: str=""):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    await interaction.response.send_message("Sending embed...", ephemeral=True)

    channel_obj = interaction.guild.get_channel(int(channel))
    try:
        col = discord.Color(int(color.replace("#", ""), 16))
    except:
        col = discord.Color.blurple()

    e = discord.Embed(title=title, description=description, color=col)
    if url:
        e.url = url
    sent = await channel_obj.send(embed=e)
    await interaction.edit_original_response(content=f"Embed sent ✅ ({sent.jump_url})")

# Autocomplete for /embed
@embed.autocomplete("channel")
async def embed_channel_autocomplete(interaction: discord.Interaction, current: str):
    try:
        if not interaction.guild:
            return []
        channels = [
            c for c in interaction.guild.text_channels
            if c.permissions_for(interaction.user).send_messages
        ]
        results = [
            app_commands.Choice(name=c.name, value=str(c.id))
            for c in channels if current.lower() in c.name.lower()
        ][:15]
        return results
    except Exception as e:
        print(f"⚠️ Autocomplete error (/embed): {e}")
        return []

# Edit command
@client.tree.command(name="edit", description="Edit existing message with link")
async def edit(interaction: discord.Interaction, message_link: str, new_content: str, bold: bool=False, underline: bool=False, code_lang: str=""):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    parsed = parse_message_link(message_link)
    if not parsed:
        return await interaction.response.send_message("❌ Invalid message link.", ephemeral=True)
    guild_id, channel_id, msg_id = parsed
    if str(interaction.guild_id) != guild_id:
        return await interaction.response.send_message("❌ You cannot edit messages outside this server.", ephemeral=True)

    channel = await client.fetch_channel(int(channel_id))
    msg = await channel.fetch_message(int(msg_id))
    final = format_content(new_content, bold, underline, code_lang)
    await msg.edit(content=final)
    await interaction.response.send_message("Edited ✅", ephemeral=True)

# ---------- Events ----------
@client.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)
        client.tree.clear_commands(guild=guild)
        await client.tree.sync(guild=guild)
        print(f"✅ Commands synced for guild {GUILD_ID}")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

    print(f"✅ Logged in as {client.user}")

client.run(TOKEN)
