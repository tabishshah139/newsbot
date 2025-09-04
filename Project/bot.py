import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load .env file
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = os.getenv("APPLICATION_ID")
GUILD_ID = int(os.getenv("GUILD_ID"))  # 👈 apna server ID .env me daalna hoga

# Bot setup
intents = discord.Intents.default()
intents.message_content = True  # warning hatane k liye
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

@client.tree.command(name="say", description="Send formatted message to channel")
async def say(interaction: discord.Interaction, channel: discord.TextChannel, content: str, bold: bool=False, underline: bool=False, code_lang: str="", typing_ms: int=0):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    await interaction.response.send_message("Sending...", ephemeral=True)
    if typing_ms > 0:
        async with channel.typing():
            await asyncio.sleep(typing_ms / 1000)
    final = format_content(content, bold, underline, code_lang)
    sent = await channel.send(final)
    await interaction.edit_original_response(content=f"Sent ✅ ({sent.jump_url})")

@client.tree.command(name="embed", description="Send embed message")
async def embed(interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: str, color: str="#5865F2", url: str=""):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    await interaction.response.send_message("Sending embed...", ephemeral=True)
    try:
        col = discord.Color(int(color.replace("#", ""), 16))
    except:
        col = discord.Color.blurple()
    e = discord.Embed(title=title, description=description, color=col)
    if url:
        e.url = url
    sent = await channel.send(embed=e)
    await interaction.edit_original_response(content=f"Embed sent ✅ ({sent.jump_url})")

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
        client.tree.clear_commands(guild=guild)   # 👈 purane commands sirf isi server ke liye clear
        await client.tree.sync(guild=guild)       # 👈 sirf is guild ke liye sync karo (instant update)
        print(f"✅ Commands synced for guild {GUILD_ID}")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

    print(f"✅ Logged in as {client.user}")

client.run(TOKEN)
