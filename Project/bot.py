import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load env variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Optional fast sync to a specific server (guild)
# Put your server ID in env as GUILD_ID
GUILD_ID_ENV = os.getenv("GUILD_ID")
GUILD_ID = int(GUILD_ID_ENV) if GUILD_ID_ENV and GUILD_ID_ENV.isdigit() else None

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Cache ----------
recent_channels = {}  # {user_id: {guild_id: [channel_ids]}}

def update_recent_channel(user_id: int, guild_id: int, channel_id: int):
    if user_id not in recent_channels:
        recent_channels[user_id] = {}
    if guild_id not in recent_channels[user_id]:
        recent_channels[user_id][guild_id] = []
    if channel_id in recent_channels[user_id][guild_id]:
        recent_channels[user_id][guild_id].remove(channel_id)
    recent_channels[user_id][guild_id].insert(0, channel_id)
    if len(recent_channels[user_id][guild_id]) > 30:
        recent_channels[user_id][guild_id].pop()

# ---------- Helpers ----------
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
    # recent channels
    if user_id in recent_channels and guild.id in recent_channels[user_id]:
        for cid in recent_channels[user_id][guild.id][:10]:
            channel = guild.get_channel(cid)
            if channel and current.lower() in channel.name.lower():
                choices.append(app_commands.Choice(
                    name=f"⭐ {channel.name}", value=str(channel.id)
                ))
    # normal channels
    for channel in guild.text_channels:
        if current.lower() in channel.name.lower():
            choices.append(app_commands.Choice(
                name=channel.name, value=str(channel.id)
            ))
        if len(choices) >= 25:
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
    except Exception:
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

# ---------- HELP ----------
@client.tree.command(name="help", description="Show all available commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Bot Commands", color=discord.Color.green())
    embed.add_field(name="Messaging", value="/say, /embed, /edit, /recent", inline=False)
    embed.add_field(name="Music", value="/play, /pause, /resume, /stop, /skip, /queue, /nowplaying, /loop, /clearlist, /volume, /effect", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- MUSIC PLACEHOLDER ----------
@client.tree.command(name="play", description="Play a song from YouTube")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.send_message(f"🎵 Searching: {query}", ephemeral=True)

@client.tree.command(name="pause", description="Pause current song")
async def pause(interaction: discord.Interaction):
    await interaction.response.send_message("⏸️ Song paused", ephemeral=True)

@client.tree.command(name="resume", description="Resume paused song")
async def resume(interaction: discord.Interaction):
    await interaction.response.send_message("▶️ Resumed", ephemeral=True)

@client.tree.command(name="stop", description="Stop playing music")
async def stop(interaction: discord.Interaction):
    await interaction.response.send_message("⏹️ Music stopped", ephemeral=True)

@client.tree.command(name="skip", description="Skip current song")
async def skip(interaction: discord.Interaction):
    await interaction.response.send_message("⏭️ Skipped", ephemeral=True)

@client.tree.command(name="queue", description="Show music queue")
async def queue(interaction: discord.Interaction):
    await interaction.response.send_message("🎶 Queue: [placeholder]", ephemeral=True)

@client.tree.command(name="nowplaying", description="Show current song")
async def nowplaying(interaction: discord.Interaction):
    await interaction.response.send_message("🎧 Now playing: [placeholder]", ephemeral=True)

@client.tree.command(name="loop", description="Loop the current song (admin only)")
async def loop(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can loop.", ephemeral=True)
    await interaction.response.send_message("🔁 Loop enabled", ephemeral=True)

@client.tree.command(name="clearlist", description="Clear the music queue (admin only)")
async def clearlist(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can clear queue.", ephemeral=True)
    await interaction.response.send_message("🗑️ Queue cleared", ephemeral=True)

@client.tree.command(name="volume", description="Change music volume")
async def volume(interaction: discord.Interaction, level: int):
    await interaction.response.send_message(f"🔊 Volume set to {level}%", ephemeral=True)

@client.tree.command(name="effect", description="Add sound effect like reverb/lofi")
async def effect(interaction: discord.Interaction, effect: str):
    await interaction.response.send_message(f"✨ Effect applied: {effect}", ephemeral=True)

# ---------- Events ----------
@client.event
async def on_ready():
    try:
        # 1) Fast guild sync so commands instantly appear in your server
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced_guild = await client.tree.sync(guild=guild)
            print(f"✅ Synced {len(synced_guild)} commands to guild {GUILD_ID}")

        # 2) (Optional) Also push global sync for all servers
        synced_global = await client.tree.sync()
        print(f"🌍 Synced {len(synced_global)} global commands")

    except Exception as e:
        print(f"❌ Sync error: {e}")

    print(f"✅ Logged in as {client.user}")

client.run(TOKEN)
