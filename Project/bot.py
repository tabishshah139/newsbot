import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.utils import DownloadError

# Load env variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: fast guild sync

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Music Data Structures ----------
music_queues = {}       # {guild_id: [ {title, url, requester, effect} ]}
now_playing = {}        # {guild_id: {title, url, requester, effect}}
loop_flags = {}         # {guild_id: bool}
volumes = {}            # {guild_id: float}  (0.0 - 2.0)

ffmpeg_base_opts = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

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
        # If connected somewhere else, try to move
        if vc.channel.id != channel.id:
            try:
                await vc.move_to(channel)
            except Exception:
                pass
    return vc

def ytdl_get_info(query: str):
    """
    Try to get a playable stream URL and title for the given query or url.
    - First tries the query/url directly.
    - On failure (non-auth), tries ytsearch and returns top result.
    - If the extractor complains about sign-in / age restriction, raises RuntimeError("AGE_RESTRICTED").
    - If nothing found, raises RuntimeError("NOT_FOUND").
    """
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'source_address': '0.0.0.0',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # 1) try direct (url or query)
        try:
            info = ydl.extract_info(query, download=False)
        except DownloadError as e:
            err = str(e)
            # Detect age-restricted / sign-in required messages from yt-dlp
            if "Sign in to confirm" in err or "sign in to confirm" in err.lower() or "age-restricted" in err.lower():
                raise RuntimeError("AGE_RESTRICTED") from e
            # otherwise try a search fallback
            try:
                search = ydl.extract_info(f"ytsearch1:{query}", download=False)
                entries = search.get('entries')
                if not entries:
                    raise RuntimeError("NOT_FOUND")
                info = entries[0]
            except Exception:
                raise RuntimeError("NOT_FOUND") from e
        except Exception:
            # other unexpected errors: try search as well
            try:
                search = ydl.extract_info(f"ytsearch1:{query}", download=False)
                entries = search.get('entries')
                if not entries:
                    raise RuntimeError("NOT_FOUND")
                info = entries[0]
            except Exception as e:
                raise RuntimeError("NOT_FOUND") from e

        # If we got a playlist/search result, normalize to single video dict
        if isinstance(info, dict) and 'entries' in info and info['entries']:
            info = info['entries'][0]

        # Try to find a direct stream url in info
        stream_url = info.get('url')
        if not stream_url:
            formats = info.get('formats') or []
            if not formats:
                raise RuntimeError("NOT_FOUND")
            # pick the best audio-like format (usually last is best)
            stream_url = formats[-1].get('url')

        title = info.get('title', 'Unknown Title')
        return stream_url, title

def build_ffmpeg_options(effect: str = None):
    opts = ffmpeg_base_opts.copy()
    if effect:
        # simple supported effects mapping
        if effect == "bass":
            af = "bass=g=10"
        elif effect == "lofi":
            af = "lowpass=f=8000,asetrate=44100*0.8"
        else:
            af = None
        if af:
            opts = opts.copy()
            # include audio filter
            opts["options"] = f"-vn -af \"{af}\""
    return opts

async def play_next_in_queue(guild_id: int):
    """Pop next entry and start playback. Called internally."""
    queue = music_queues.get(guild_id, [])
    guild = client.get_guild(guild_id)
    if not guild:
        return
    vc = guild.voice_client

    if loop_flags.get(guild_id) and now_playing.get(guild_id):
        # loop current song (do not pop)
        entry = now_playing[guild_id]
    else:
        if not queue:
            now_playing.pop(guild_id, None)
            # disconnect if idle
            if vc and not vc.is_playing():
                try:
                    await asyncio.sleep(3)
                    if vc and not vc.is_playing():
                        await vc.disconnect()
                except Exception:
                    pass
            return
        entry = queue.pop(0)
        music_queues[guild_id] = queue

    stream_url = entry['url']
    effect = entry.get('effect')
    ff_opts = build_ffmpeg_options(effect)
    try:
        source = discord.FFmpegPCMAudio(stream_url, **ff_opts)
    except Exception as e:
        # if ffmpeg cannot open, skip to next
        print(f"[Music] FFmpeg error for guild {guild_id}: {e}")
        now_playing.pop(guild_id, None)
        await play_next_in_queue(guild_id)
        return

    volume = volumes.get(guild_id, 0.25)
    player = discord.PCMVolumeTransformer(source, volume=volume)

    def after_play(error):
        if error:
            print(f"[Music] playback error in guild {guild_id}: {error}")
        fut = asyncio.run_coroutine_threadsafe(play_next_in_queue(guild_id), client.loop)
        try:
            fut.result()
        except Exception:
            pass

    try:
        if not vc:
            # nothing to do if no voice client (should normally be connected)
            return
        vc.play(player, after=after_play)
        now_playing[guild_id] = entry
    except Exception as e:
        now_playing.pop(guild_id, None)
        print(f"[Music] play error in guild {guild_id}: {e}")

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

# ---------- HELP ----------
@client.tree.command(name="help", description="Show all available commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Bot Commands", color=discord.Color.green())
    embed.add_field(name="Messaging", value="/say, /embed, /edit, /recent", inline=False)
    embed.add_field(name="Music", value="/play, /pause, /resume, /stop, /skip, /queue, /nowplaying, /loop, /clearlist, /volume, /effect", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- MUSIC COMMANDS (Option 2: NO COOKIES) ----------
@client.tree.command(name="play", description="Play a song from YouTube (name or url)")
async def play(interaction: discord.Interaction, query: str, effect: str = ""):
    # Defer early (may take a bit to search)
    await interaction.response.defer(ephemeral=False)
    vc = await ensure_voice_connected(interaction)
    if not vc:
        return
    guild_id = interaction.guild.id
    try:
        stream_url, title = ytdl_get_info(query)
    except RuntimeError as e:
        reason = str(e)
        if reason == "AGE_RESTRICTED":
            # Friendly explanation and options
            return await interaction.followup.send(
                "❌ This video looks **age-restricted / sign-in required / private**. I don't use YouTube cookies, so I can't play restricted videos.\n\n"
                "Try one of these:\n"
                "• Use a **different public upload** of the same song (different uploader)\n"
                "• Paste a direct link from another source (SoundCloud/etc.)\n"
                "• Search with slightly different keywords (e.g., add `official audio` or `audio`)\n",
                ephemeral=False
            )
        elif reason == "NOT_FOUND":
            return await interaction.followup.send("❌ Could not find that song. Try a different query or paste a YouTube URL.", ephemeral=False)
        else:
            return await interaction.followup.send(f"❌ Search failed: {reason}", ephemeral=False)
    except Exception as e:
        return await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=False)

    entry = {"title": title, "url": stream_url, "requester": interaction.user.display_name, "effect": effect or None}
    music_queues.setdefault(guild_id, []).append(entry)

    guild_vc = interaction.guild.voice_client
    # If nothing playing, start playback
    if not guild_vc or not guild_vc.is_playing():
        await play_next_in_queue(guild_id)
        await interaction.followup.send(f"▶️ Now playing **{title}** — requested by {entry['requester']}")
    else:
        await interaction.followup.send(f"➕ Queued **{title}** — position {len(music_queues[guild_id])}")

@client.tree.command(name="pause", description="Pause current song")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    vc.pause()
    await interaction.response.send_message("⏸️ Paused", ephemeral=True)

@client.tree.command(name="resume", description="Resume paused song")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_paused():
        return await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)
    vc.resume()
    await interaction.response.send_message("▶️ Resumed", ephemeral=True)

@client.tree.command(name="stop", description="Stop playback and clear queue")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("❌ I'm not connected to a VC.", ephemeral=True)
    try:
        vc.stop()
    except Exception:
        pass
    music_queues.pop(interaction.guild.id, None)
    now_playing.pop(interaction.guild.id, None)
    try:
        await vc.disconnect()
    except Exception:
        pass
    await interaction.response.send_message("⏹️ Stopped and cleared queue.", ephemeral=True)

@client.tree.command(name="skip", description="Skip current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    vc.stop()
    await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)

@client.tree.command(name="queue", description="Show music queue")
async def queue_cmd(interaction: discord.Interaction):
    q = music_queues.get(interaction.guild.id, [])
    if not q and not now_playing.get(interaction.guild.id):
        return await interaction.response.send_message("🎶 Queue is empty.", ephemeral=True)
    lines = []
    current = now_playing.get(interaction.guild.id)
    if current:
        lines.append(f"▶️ Now: **{current['title']}** — requested by {current['requester']}")
    for i, e in enumerate(q[:10], start=1):
        lines.append(f"{i}. {e['title']} — {e['requester']}")
    await interaction.response.send_message("🎶\n" + "\n".join(lines), ephemeral=True)

@client.tree.command(name="nowplaying", description="Show current song")
async def nowplaying_cmd(interaction: discord.Interaction):
    current = now_playing.get(interaction.guild.id)
    if not current:
        return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
    await interaction.response.send_message(f"🎧 Now playing: **{current['title']}** — requested by {current['requester']}", ephemeral=True)

@client.tree.command(name="loop", description="Loop the current song (admin only)")
async def loop_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can toggle loop.", ephemeral=True)
    gid = interaction.guild.id
    loop_flags[gid] = not loop_flags.get(gid, False)
    await interaction.response.send_message(f"🔁 Loop set to {loop_flags[gid]}", ephemeral=True)

@client.tree.command(name="clearlist", description="Clear the music queue (admin only)")
async def clearlist(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Only admins can clear queue.", ephemeral=True)
    music_queues.pop(interaction.guild.id, None)
    await interaction.response.send_message("🗑️ Queue cleared", ephemeral=True)

@client.tree.command(name="volume", description="Change music volume (0-200)")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if level < 0 or level > 200:
        return await interaction.response.send_message("❌ Volume must be 0-200.", ephemeral=True)
    gid = interaction.guild.id
    volumes[gid] = max(0.0, min(level / 100.0, 2.0))
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = volumes[gid]
    await interaction.response.send_message(f"🔊 Volume set to {level}%", ephemeral=True)

@client.tree.command(name="effect", description="Apply simple audio effect to queued song (e.g. bass, lofi)")
async def effect_cmd(interaction: discord.Interaction, effect: str):
    valid = {"bass", "lofi"}
    if effect not in valid:
        return await interaction.response.send_message("❌ Supported effects: bass, lofi", ephemeral=True)
    gid = interaction.guild.id
    q = music_queues.setdefault(gid, [])
    if not q:
        return await interaction.response.send_message("❌ Queue empty — add a song first.", ephemeral=True)
    q[-1]['effect'] = effect
    await interaction.response.send_message(f"✨ Effect '{effect}' applied to queued song **{q[-1]['title']}**", ephemeral=True)

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

client.run(TOKEN)
