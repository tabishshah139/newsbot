# ==========================
# Importing libraries
# ==========================
import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from collections import deque

# ==========================
# Load ENV variables
# ==========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ==========================
# Setup
# ==========================
intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

# ==========================
# Music system cache
# ==========================
SONG_QUEUES = {}  # {guild_id: deque of (url, title)}

# yt-dlp async helper
async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

# ==========================
# Helper: format text
# ==========================
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

# ==========================
# Autocomplete for channels
# ==========================
recent_channels = {}
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

async def channel_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    guild = interaction.guild
    user_id = interaction.user.id
    if not guild:
        return []
    if user_id in recent_channels and guild.id in recent_channels[user_id]:
        for cid in recent_channels[user_id][guild.id][:10]:
            channel = guild.get_channel(cid)
            if channel and current.lower() in channel.name.lower():
                choices.append(app_commands.Choice(
                    name=f"⭐ {channel.name}", value=str(channel.id)
                ))
    for channel in guild.text_channels:
        if current.lower() in channel.name.lower():
            choices.append(app_commands.Choice(
                name=channel.name, value=str(channel.id)
            ))
        if len(choices) >= 25:
            break
    return choices

# ==========================
# Slash commands - Messaging
# ==========================
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

@client.tree.command(name="help", description="Show all available commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Bot Commands", color=discord.Color.green())
    embed.add_field(name="Messaging", value="/say, /embed, /edit, /recent", inline=False)
    embed.add_field(name="Music", value="/play, /pause, /resume, /stop, /skip, /queue, /nowplaying", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==========================
# Slash commands - Music
# ==========================
@client.tree.command(name="play", description="Play a song or add to queue")
@app_commands.describe(song_query="Song name or URL")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.send_message("🎵 Searching...", ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.edit_original_response(content="❌ You must be in a VC.")

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    try:
        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_channel != voice_client.channel:
            await voice_client.move_to(voice_channel)
    except Exception as e:
        return await interaction.edit_original_response(content=f"❌ Could not join VC: {e}")

    ydl_opts = {"format": "bestaudio[abr<=96]/bestaudio", "noplaylist": True, "quiet": True, "no_warnings": True}
    query = "ytsearch1:" + song_query
    try:
        results = await search_ytdlp_async(query, ydl_opts)
    except Exception as e:
        return await interaction.edit_original_response(content=f"❌ Search failed: {e}")

    tracks = results.get("entries", []) if isinstance(results, dict) else []
    if not tracks:
        return await interaction.edit_original_response(content="❌ No results found.")

    first_track = tracks[0]
    audio_url = first_track.get("url")
    title = first_track.get("title", "Untitled")

    guild_id = str(interaction.guild_id)
    if SONG_QUEUES.get(guild_id) is None:
        SONG_QUEUES[guild_id] = deque()
    SONG_QUEUES[guild_id].append((audio_url, title))

    if voice_client.is_playing() or voice_client.is_paused():
        await interaction.edit_original_response(content=f"➕ Added to queue: **{title}**")
    else:
        await interaction.edit_original_response(content=f"▶️ Now playing: **{title}**")
        await play_next_song(voice_client, guild_id, interaction.channel)

async def play_next_song(voice_client, guild_id, channel):
    if SONG_QUEUES[guild_id]:
        audio_url, title = SONG_QUEUES[guild_id].popleft()
        ffmpeg_opts = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", "options": "-vn"}
        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)

        def after_play(error):
            if error:
                print(f"Error playing {title}: {error}")
            fut = asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel), client.loop)
            try:
                fut.result()
            except Exception as e:
                print(f"Error in after_play: {e}")

        voice_client.play(source, after=after_play)
        await channel.send(f"🎶 Now playing: **{title}**")
    else:
        await voice_client.disconnect()
        SONG_QUEUES[guild_id] = deque()

@client.tree.command(name="skip", description="Skip current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)
    else:
        await interaction.response.send_message("❌ Nothing playing.", ephemeral=True)

@client.tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ Not in VC.", ephemeral=True)
    if vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.", ephemeral=False)
    else:
        await interaction.response.send_message("❌ Nothing playing.", ephemeral=True)

@client.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ Not in VC.", ephemeral=True)
    if vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.", ephemeral=False)
    else:
        await interaction.response.send_message("❌ Not paused.", ephemeral=True)

@client.tree.command(name="stop", description="Stop playback and clear queue")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ Not in VC.", ephemeral=True)
    SONG_QUEUES[str(interaction.guild_id)] = deque()
    vc.stop()
    await vc.disconnect()
    await interaction.response.send_message("⏹️ Stopped and cleared queue.", ephemeral=False)

@client.tree.command(name="queue", description="Show song queue")
async def queue(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if not SONG_QUEUES.get(guild_id):
        return await interaction.response.send_message("Queue is empty.", ephemeral=True)
    qlist = [f"{i+1}. {title}" for i, (_, title) in enumerate(list(SONG_QUEUES[guild_id])[:10])]
    embed = discord.Embed(title="🎶 Queue", description="\n".join(qlist), color=discord.Color.purple())
    await interaction.response.send_message(embed=embed, ephemeral=False)

@client.tree.command(name="nowplaying", description="Show current song")
async def nowplaying(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    await interaction.response.send_message("🎧 A song is currently playing.", ephemeral=True)

# ==========================
# Events
# ==========================
@client.event
async def on_ready():
    await client.tree.sync()
    print(f"✅ Logged in as {client.user}")

# ==========================
# Run
# ==========================
client.run(TOKEN)
