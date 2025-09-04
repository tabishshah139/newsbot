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

# Bot setup with intents
intents = discord.Intents.default()
intents.message_content = True   # ✅ fix: allow reading message content
client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Cache (per user, max 30 channels) ----------
recent_channels = {}  # {user_id: {guild_id: [channel_ids]}}

def update_recent_channel(user_id: int, guild_id: int, channel_id: int):
    if user_id not in recent_channels:
        recent_channels[user_id] = {}
    if guild_id not in recent_channels[user_id]:
        recent_channels[user_id][guild_id] = []
    if channel_id in recent_channels[user_id][guild_id]:
        recent_channels[user_id][guild_id].remove(channel_id)
    recent_channels[user_id][guild_id].insert(0, channel_id)
    if len(recent_channels[user_id][guild_id]) > 30:  # max 30 channels per guild
        recent_channels[user_id][guild_id].pop()

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

# ---------- Autocomplete ----------
async def channel_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    guild = interaction.guild
    user_id = interaction.user.id
    if not guild:
        return []
    
    # Recent channels for this user in this guild
    if user_id in recent_channels and guild.id in recent_channels[user_id]:
        for cid in recent_channels[user_id][guild.id][:10]:
            channel = guild.get_channel(cid)
            if channel and current.lower() in channel.name.lower():
                choices.append(app_commands.Choice(
                    name=f"⭐ {channel.name}", value=str(channel.id)
                ))

    # Normal channels (matching search)
    for channel in guild.text_channels:
        if current.lower() in channel.name.lower():
            choices.append(app_commands.Choice(
                name=channel.name, value=str(channel.id)
            ))
        if len(choices) >= 25:  # max limit
            break

    return choices

# ---------- Slash Commands ----------
@client.tree.command(name="say", description="Send formatted message to channel")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def say(interaction: discord.Interaction, channel_id: str, content: str, bold: bool=False, underline: bool=False, code_lang: str="", typing_ms: int=0):
    await interaction.response.send_message("Sending...", ephemeral=True)
    channel = await client.fetch_channel(int(channel_id))
    if typing_ms > 0:
        async with channel.typing():
            await asyncio.sleep(typing_ms / 1000)
    final = format_content(content, bold, underline, code_lang)
    sent = await channel.send(final)
    update_recent_channel(interaction.user.id, interaction.guild.id, int(channel_id))
    await interaction.edit_original_response(content=f"Sent ✅ ({sent.jump_url})")

@client.tree.command(name="embed", description="Send embed message")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def embed(interaction: discord.Interaction, channel_id: str, title: str, description: str, color: str="#5865F2", url: str=""):
    await interaction.response.send_message("Sending embed...", ephemeral=True)
    channel = await client.fetch_channel(int(channel_id))
    try:
        col = discord.Color(int(color.replace("#", ""), 16))
    except:
        col = discord.Color.blurple()
    e = discord.Embed(title=title, description=description, color=col)
    if url:
        e.url = url
    sent = await channel.send(embed=e)
    update_recent_channel(interaction.user.id, interaction.guild.id, int(channel_id))
    await interaction.edit_original_response(content=f"Embed sent ✅ ({sent.jump_url})")

@client.tree.command(name="edit", description="Edit existing message with link")
async def edit(interaction: discord.Interaction, message_link: str, new_content: str, bold: bool=False, underline: bool=False, code_lang: str=""):
    parsed = parse_message_link(message_link)
    if not parsed:
        return await interaction.response.send_message("❌ Invalid message link.", ephemeral=True)
    guild_id, channel_id, msg_id = parsed
    channel = await client.fetch_channel(int(channel_id))
    msg = await channel.fetch_message(int(msg_id))
    final = format_content(new_content, bold, underline, code_lang)
    await msg.edit(content=final)
    update_recent_channel(interaction.user.id, int(guild_id), int(channel_id))
    await interaction.response.send_message("Edited ✅", ephemeral=True)

# ---------- Extra: Show your recent channels ----------
@client.tree.command(name="recent", description="Show your last used channels")
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

# ---------- Text Command Example ----------
@client.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! Latency: {round(client.latency * 1000)}ms")

# ---------- Events ----------
@client.event
async def on_ready():
    await client.tree.sync()
    print(f"✅ Logged in as {client.user}")

client.run(TOKEN)
