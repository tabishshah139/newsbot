# bot.py — merged (old project's music system integrated)
import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.utils import DownloadError
from collections import deque

# Load env variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: fast guild sync

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Music (from old project) ----------
# SONG_QUEUES maps guild_id (string) -> deque of (audio_url, title)
SONG_QUEUES = {}
# CURRENT playing title per guild (for nowplaying)
CURRENT = {}

async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

async def play_next_song(voice_client, guild_id_str, text_channel):
    """
    Pop next song from SONG_QUEUES[guild_id_str] and play it on voice_client.
    Sends "Now playing" message to text_channel.
    Disconnects when queue is empty.
    """
    if guild_id_str not in SONG_QUEUES:
        SONG_QUEUES[guild_id_str] = deque()

    if SONG_QUEUES[guild_id_str]:
        audio_url, title = SONG_QUEUES[guild_id_str].popleft()
        # store current
        CURRENT[guild_id_str] = title

        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            # omit special codec flags so ffmpeg on host chooses appropriate codec
            "options": "-vn"
        }

        try:
            source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options)
        except Exception:
            # fallback to PCM if opus not available
            try:
                source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_options)
            except Exception as e:
                print(f"[Music] FFmpeg error for guild {guild_id_str}: {e}")
                # try next song
                CURRENT.pop(guild_id_str, None)
                await play_next_song(voice_client, guild_id_str, text_channel)
                return

        def after_play(error):
            if error:
                print(f"[Music] Error playing {title}: {error}")
            # schedule next
            fut = asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id_str, text_channel), client.loop)
            try:
                fut.result()
            except Exception:
                pass

        try:
            voice_client.play(source, after=after_play)
            # send now playing message (fire-and-forget)
            try:
                asyncio.create_task(text_channel.send(f"▶️ Now playing: **{title}**"))
            except Exception:
                pass
        except Exception as e:
            print(f"[Music] playback error in guild {guild_id_str}: {e}")
            CURRENT.pop(guild_id_str, None)
            # try next
            await play_next_song(voice_client, guild_id_str, text_channel)
    else:
        # queue empty: clear current and disconnect after short delay
        CURRENT.pop(guild_id_str, None)
        try:
            await asyncio.sleep(1)
            if voice_client and not voice_client.is_playing() and not voice_client.is_paused():
                await voice_client.disconnect()
        except Exception:
            pass
        # re-init empty deque
        SONG_QUEUES[guild_id_str] = deque()

# ---------- General Cache ----------
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

async def ensure_voice_connected(interaction: discord.Interaction):
    """Ensure bot is connected to user's voice channel. Returns voice_client or None."""
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Pehle VC join karo Jaani!", ephemeral=True)
        return None
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        try:
            vc = await channel.connect()
        except Exception as e:
            await interaction.response.send_message(f"❌ Could not join VC: {e}", ephemeral=True)
            return None
    else:
        if vc.channel.id != channel.id:
            try:
                await vc.move_to(channel)
            except Exception:
                pass
    return vc

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

# ---------- Slash Commands (Messaging) ----------
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

# ---------- MUSIC COMMANDS (old project's system) ----------
@client.tree.command(name="play", description="Play a song or add it to the queue.")
@app_commands.describe(song_query="Search query or url")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()
    # Ensure user in VC
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ You must be in a voice channel to use this command.", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        try:
            voice_client = await voice_channel.connect()
        except Exception as e:
            await interaction.followup.send(f"❌ Could not join your voice channel: {e}", ephemeral=True)
            return
    elif voice_channel != voice_client.channel:
        try:
            await voice_client.move_to(voice_channel)
        except Exception:
            pass

    ydl_options = {
        "format": "bestaudio[abr<=96]/bestaudio",
        "noplaylist": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
        "quiet": True,
        "no_warnings": True,
    }

    # Use ytsearch to find best match
    query = "ytsearch1:" + song_query
    try:
        results = await search_ytdlp_async(query, ydl_options)
    except Exception as e:
        await interaction.followup.send(f"❌ Search failed: {e}", ephemeral=True)
        return

    tracks = results.get("entries", []) if isinstance(results, dict) else []
    if not tracks:
        await interaction.followup.send("No results found.", ephemeral=True)
        return

    first_track = tracks[0]
    audio_url = first_track.get("url")
    title = first_track.get("title", "Untitled")

    guild_id_str = str(interaction.guild_id)
    if SONG_QUEUES.get(guild_id_str) is None:
        SONG_QUEUES[guild_id_str] = deque()

    SONG_QUEUES[guild_id_str].append((audio_url, title))

    if voice_client.is_playing() or voice_client.is_paused():
        await interaction.followup.send(f"➕ Added to queue: **{title}**", ephemeral=False)
    else:
        await interaction.followup.send(f"▶️ Now playing: **{title}**", ephemeral=False)
        # start the playback loop
        await play_next_song(voice_client, guild_id_str, interaction.channel)

@client.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped the current song.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Not playing anything to skip.", ephemeral=True)

@client.tree.command(name="pause", description="Pause the currently playing song.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        return await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
    if not voice_client.is_playing():
        return await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)
    voice_client.pause()
    await interaction.response.send_message("⏸️ Playback paused!", ephemeral=True)

@client.tree.command(name="resume", description="Resume the currently paused song.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        return await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
    if not voice_client.is_paused():
        return await interaction.response.send_message("❌ I’m not paused right now.", ephemeral=True)
    voice_client.resume()
    await interaction.response.send_message("▶️ Playback resumed!", ephemeral=True)

@client.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return await interaction.response.send_message("❌ I'm not connected to any voice channel.", ephemeral=True)

    guild_id_str = str(interaction.guild_id)
    if guild_id_str in SONG_QUEUES:
        SONG_QUEUES[guild_id_str].clear()
    CURRENT.pop(guild_id_str, None)

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    try:
        await voice_client.disconnect()
    except Exception:
        pass

    await interaction.response.send_message("⏹️ Stopped playback and disconnected!", ephemeral=True)

@client.tree.command(name="queue", description="Show music queue")
async def queue_cmd(interaction: discord.Interaction):
    guild_id_str = str(interaction.guild_id)
    q = SONG_QUEUES.get(guild_id_str, deque())
    if (not q) and (guild_id_str not in CURRENT or not CURRENT.get(guild_id_str)):
        return await interaction.response.send_message("🎶 Queue is empty.", ephemeral=True)
    lines = []
    current = CURRENT.get(guild_id_str)
    if current:
        lines.append(f"▶️ Now: **{current}**")
    for i, (url, title) in enumerate(list(q)[:10], start=1):
        lines.append(f"{i}. {title}")
    await interaction.response.send_message("🎶\n" + "\n".join(lines), ephemeral=True)

@client.tree.command(name="nowplaying", description="Show current song")
async def nowplaying_cmd(interaction: discord.Interaction):
    current = CURRENT.get(str(interaction.guild_id))
    if not current:
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    await interaction.response.send_message(f"🎧 Now playing: **{current}**", ephemeral=True)

# ---------- DEBUG SYNC ----------
@client.tree.command(name="sync", description="Manually resync commands (Admin only)")
async def sync_commands(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can sync.", ephemeral=True)
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            synced = await client.tree.sync(guild=guild)
            await interaction.response.send_message(f"✅ Resynced {len(synced)} commands.", ephemeral=True)
        else:
            synced = await client.tree.sync()
            await interaction.response.send_message(f"🌍 Globally resynced {len(synced)} commands.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Sync failed: {e}", ephemeral=True)

# ---------- Events ----------
@client.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            synced = await client.tree.sync(guild=guild)
            print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await client.tree.sync()
            print(f"🌍 Globally synced {len(synced)} commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")
    print(f"✅ Logged in as {client.user}")

# Run the bot
client.run(TOKEN)
