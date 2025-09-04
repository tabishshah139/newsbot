import os
import re
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

# -------------------- ENV --------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = os.getenv("APPLICATION_ID")
DJ_ROLE_NAME = os.getenv("DJ_ROLE_NAME", "DJ")  # optional role name

# -------------------- BOT --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = commands.Bot(command_prefix="!", intents=intents)

# -------------------- UTIL: ADMIN/DJ CHECK --------------------
def is_admin_or_dj(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name.lower() == DJ_ROLE_NAME.lower() for r in member.roles)

# -------------------- RECENT CHANNEL CACHE (existing) --------------------
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

# -------------------- AUTOCOMPLETE (existing) --------------------
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
        if len(choices) >= 15:
            break
    return choices

# -------------------- MUSIC SYSTEM --------------------
import yt_dlp

YTDL_OPTS = {
    "format": "bestaudio/best",
    "default_search": "ytsearch",
    "noplaylist": True,
    "quiet": True,
    "skip_download": True,
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
IDLE_TIMEOUT = 300  # seconds

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)

class Track:
    def __init__(self, title, webpage_url, stream_url, thumbnail, duration, requester):
        self.title = title
        self.webpage_url = webpage_url
        self.stream_url = stream_url
        self.thumbnail = thumbnail
        self.duration = duration  # seconds (may be None)
        self.requester = requester

    @property
    def duration_str(self):
        if not self.duration:
            return "Unknown"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:d}:{s:02d}"

class GuildMusic:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.voice: discord.VoiceClient | None = None
        self.current: Track | None = None
        self.loop = False
        self.volume = 0.5  # 0.0 - 1.0
        self.effect = "off"  # off|reverb|bassboost|nightcore|lofi
        self.idle_task = None

    # ---- effects -> ffmpeg filter string ----
    def effect_filter(self) -> str | None:
        if self.effect == "off":
            return None
        if self.effect == "reverb":
            return "aecho=0.8:0.88:60:0.4"
        if self.effect == "bassboost":
            # light bass boost
            return "bass=g=8:f=110:w=0.8"
        if self.effect == "nightcore":
            # pitch+tempo up
            return "asetrate=48000*1.25,aresample=48000,atempo=1.0"
        if self.effect == "lofi":
            # slight lowpass + slow
            return "lowpass=f=1200,atempo=0.9"
        return None

    # ---- build audio source ----
    def build_source(self, stream_url: str):
        filter_str = self.effect_filter()
        ffmpeg_opts = {
            "before_options": FFMPEG_BEFORE,
            "options": f"-vn{(' -af ' + filter_str) if filter_str else ''}",
        }
        src = discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts)
        return discord.PCMVolumeTransformer(src, volume=self.volume)

    async def ensure_voice(self, interaction: discord.Interaction):
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return False, "❌ Member not found."
        channel = getattr(interaction.user.voice, "channel", None)
        if channel is None:
            return False, "❌ Pehle kisi **voice channel** me join ho jao."
        if self.voice and self.voice.channel != channel:
            await self.voice.move_to(channel)
        elif not self.voice:
            self.voice = await channel.connect(self_deaf=True)
        return True, None

    async def add_query(self, query: str, requester: discord.Member) -> Track:
        # Extract info: supports direct URL or search
        info = ytdl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        # ensure we have a streaming URL (sometimes need a second resolve)
        if "url" not in info or "webpage_url" not in info:
            info = ytdl.extract_info(info.get("webpage_url", query), download=False)
        title = info.get("title", "Unknown Title")
        webpage_url = info.get("webpage_url", query)
        stream_url = info.get("url")
        thumbnail = (info.get("thumbnails") or [{}])[-1].get("url", None) or info.get("thumbnail")
        duration = info.get("duration")
        track = Track(title, webpage_url, stream_url, thumbnail, duration, requester)
        await self.queue.put(track)
        return track

    def _after(self, error):
        # Runs in voice thread, schedule coroutine on loop
        if error:
            print(f"Voice error: {error}")
        asyncio.run_coroutine_threadsafe(self._advance(), client.loop)

    async def _advance(self):
        # idle timer reset
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None

        if self.loop and self.current:
            # replay current
            src = self.build_source(self.current.stream_url)
            self.voice.play(src, after=self._after)
            return

        # fetch next
        if self.queue.empty():
            self.current = None
            # schedule auto-disconnect
            self.idle_task = client.loop.create_task(self._auto_disconnect())
            return
        self.current = await self.queue.get()
        src = self.build_source(self.current.stream_url)
        self.voice.play(src, after=self._after)

    async def _auto_disconnect(self):
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
            if (not self.voice) or self.voice.is_playing() or not self.voice.channel:
                return
            await self.voice.disconnect(force=True)
            self.voice = None
        except asyncio.CancelledError:
            pass

    async def start_or_queue(self, query: str, requester: discord.Member) -> Track:
        track = await self.add_query(query, requester)
        if not self.voice:
            # will be set by ensure_voice before this call
            pass
        if not self.voice.is_playing() and not self.voice.is_paused() and self.current is None:
            # start immediately
            self.current = await self.queue.get()
            src = self.build_source(self.current.stream_url)
            self.voice.play(src, after=self._after)
        return track

    def clear_queue(self):
        # drain the queue
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Exception:
                break

    async def stop_all(self):
        self.clear_queue()
        self.loop = False
        self.current = None
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()

MUSIC: dict[int, GuildMusic] = {}

def get_mgr(guild: discord.Guild) -> GuildMusic:
    mgr = MUSIC.get(guild.id)
    if not mgr:
        mgr = GuildMusic(guild.id)
        MUSIC[guild.id] = mgr
    return mgr

# -------------------- MUSIC COMMANDS --------------------
@client.tree.command(name="play", description="Play a song from YouTube (name or URL)")
@app_commands.describe(query="Song name or YouTube URL")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not interaction.guild:
        return await interaction.followup.send("❌ Server required.", ephemeral=True)
    mgr = get_mgr(interaction.guild)
    ok, err = await mgr.ensure_voice(interaction)
    if not ok:
        return await interaction.followup.send(err, ephemeral=True)

    try:
        track = await mgr.start_or_queue(query, interaction.user)
    except Exception as e:
        return await interaction.followup.send(f"❌ Failed to load: `{e}`", ephemeral=True)

    if mgr.current and track.title == mgr.current.title:
        # started immediately
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"[{mgr.current.title}]({mgr.current.webpage_url})",
            color=discord.Color.green()
        )
        if mgr.current.thumbnail:
            embed.set_thumbnail(url=mgr.current.thumbnail)
        embed.add_field(name="Duration", value=mgr.current.duration_str)
        embed.add_field(name="Requested by", value=str(mgr.current.requester), inline=True)
        return await interaction.followup.send(embed=embed, ephemeral=False)
    else:
        # queued
        return await interaction.followup.send(
            f"➕ Queued: **[{track.title}]({track.webpage_url})**", ephemeral=False
        )

@client.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    if not interaction.guild:
        return
    mgr = get_mgr(interaction.guild)
    if not mgr.voice or not mgr.voice.is_playing():
        return await interaction.response.send_message("⚠️ Nothing is playing.", ephemeral=True)
    mgr.voice.pause()
    await interaction.response.send_message("⏸️ Paused.", ephemeral=False)

@client.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    if not interaction.guild:
        return
    mgr = get_mgr(interaction.guild)
    if not mgr.voice or not mgr.voice.is_paused():
        return await interaction.response.send_message("⚠️ Nothing is paused.", ephemeral=True)
    mgr.voice.resume()
    await interaction.response.send_message("▶️ Resumed.", ephemeral=False)

@client.tree.command(name="skip", description="Skip current song")
async def skip(interaction: discord.Interaction):
    if not interaction.guild:
        return
    mgr = get_mgr(interaction.guild)
    if not mgr.voice or (not mgr.voice.is_playing() and not mgr.voice.is_paused()):
        return await interaction.response.send_message("⚠️ Nothing to skip.", ephemeral=True)
    mgr.voice.stop()  # triggers after -> next
    await interaction.response.send_message("⏭️ Skipped.", ephemeral=False)

# alias /next
@client.tree.command(name="next", description="Skip to next song (alias of /skip)")
async def next_cmd(interaction: discord.Interaction):
    await skip(interaction)

@client.tree.command(name="stop", description="Stop playback and clear queue")
async def stop(interaction: discord.Interaction):
    if not interaction.guild:
        return
    mgr = get_mgr(interaction.guild)
    await mgr.stop_all()
    await interaction.response.send_message("⏹️ Stopped and cleared queue.", ephemeral=False)

@client.tree.command(name="loop", description="Toggle loop current song (Admin/DJ only)")
async def loop_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return
    if not is_admin_or_dj(interaction.user):
        return await interaction.response.send_message("❌ DJ/Admin only.", ephemeral=True)
    mgr = get_mgr(interaction.guild)
    mgr.loop = not mgr.loop
    await interaction.response.send_message(f"🔁 Loop is now **{'ON' if mgr.loop else 'OFF'}**.", ephemeral=False)

@client.tree.command(name="queue", description="Show the music queue")
async def queue_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return
    mgr = get_mgr(interaction.guild)
    desc = ""
    if mgr.current:
        desc += f"**Now:** [{mgr.current.title}]({mgr.current.webpage_url}) • `{mgr.current.duration_str}` • by {mgr.current.requester}\n\n"
    if mgr.queue.empty():
        desc += "_Queue is empty._"
    else:
        tmp = []
        # Peek safely: convert to list snapshot
        qlist = []
        try:
            while True:
                item = mgr.queue.get_nowait()
                qlist.append(item)
        except Exception:
            pass
        # put back
        for item in qlist:
            tmp.append(item)
            mgr.queue.put_nowait(item)
        for i, t in enumerate(tmp[:10], start=1):
            desc += f"`{i}.` [{t.title}]({t.webpage_url}) • `{t.duration_str}` • by {t.requester}\n"
        if len(tmp) > 10:
            desc += f"\n…and **{len(tmp)-10}** more."

    embed = discord.Embed(title="📜 Queue", description=desc, color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, ephemeral=False)

@client.tree.command(name="clearlist", description="Clear the pending queue (Admin/DJ only)")
async def clearlist_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return
    if not is_admin_or_dj(interaction.user):
        return await interaction.response.send_message("❌ DJ/Admin only.", ephemeral=True)
    mgr = get_mgr(interaction.guild)
    mgr.clear_queue()
    await interaction.response.send_message("🧹 Cleared the queue (current song untouched).", ephemeral=False)

@client.tree.command(name="nowplaying", description="Show info about the current song")
async def nowplaying(interaction: discord.Interaction):
    if not interaction.guild:
        return
    mgr = get_mgr(interaction.guild)
    if not mgr.current:
        return await interaction.response.send_message("⚠️ Nothing is playing.", ephemeral=True)
    e = discord.Embed(
        title="🎶 Now Playing",
        description=f"[{mgr.current.title}]({mgr.current.webpage_url})",
        color=discord.Color.green()
    )
    e.add_field(name="Duration", value=mgr.current.duration_str)
    e.add_field(name="Requested by", value=str(mgr.current.requester))
    if mgr.current.thumbnail:
        e.set_thumbnail(url=mgr.current.thumbnail)
    await interaction.response.send_message(embed=e, ephemeral=False)

@client.tree.command(name="volume", description="Set volume (0-100) (Admin/DJ only)")
@app_commands.describe(level="Volume percent (0-100)")
async def volume(interaction: discord.Interaction, level: int):
    if not interaction.guild:
        return
    if not is_admin_or_dj(interaction.user):
        return await interaction.response.send_message("❌ DJ/Admin only.", ephemeral=True)
    if level < 0 or level > 100:
        return await interaction.response.send_message("⚠️ Please choose between 0 and 100.", ephemeral=True)
    mgr = get_mgr(interaction.guild)
    mgr.volume = level / 100.0
    # apply immediately if playing
    if mgr.voice and (mgr.voice.is_playing() or mgr.voice.is_paused()) and isinstance(mgr.voice.source, discord.PCMVolumeTransformer):
        mgr.voice.source.volume = mgr.volume
    await interaction.response.send_message(f"🔊 Volume set to **{level}%**.", ephemeral=False)

@client.tree.command(name="effect", description="Audio effect (Admin/DJ only)")
@app_commands.choices(name=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="reverb", value="reverb"),
    app_commands.Choice(name="bassboost", value="bassboost"),
    app_commands.Choice(name="nightcore", value="nightcore"),
    app_commands.Choice(name="lofi", value="lofi"),
])
async def effect(interaction: discord.Interaction, name: app_commands.Choice[str]):
    if not interaction.guild:
        return
    if not is_admin_or_dj(interaction.user):
        return await interaction.response.send_message("❌ DJ/Admin only.", ephemeral=True)
    mgr = get_mgr(interaction.guild)
    mgr.effect = name.value
    # To apply instantly, restart current stream if playing
    if mgr.voice and mgr.current and (mgr.voice.is_playing() or mgr.voice.is_paused()):
        was_paused = mgr.voice.is_paused()
        mgr.voice.stop()
        # _advance will replay (loop) or go next. Force replay current:
        if mgr.current:
            src = mgr.build_source(mgr.current.stream_url)
            mgr.voice.play(src, after=mgr._after)
            if was_paused:
                mgr.voice.pause()
    await interaction.response.send_message(f"🎛️ Effect set to **{mgr.effect}**.", ephemeral=False)

# -------------------- EXISTING SLASH COMMANDS --------------------
@client.tree.command(name="say", description="Send formatted message to channel")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def say(
    interaction: discord.Interaction,
    channel_id: str,
    content: str,
    bold: bool = False,
    underline: bool = False,
    code_lang: str = "",
    typing_ms: int = 0
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)
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
async def embed(
    interaction: discord.Interaction,
    channel_id: str,
    title: str,
    description: str,
    color: str = "#5865F2",
    url: str = ""
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)
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
async def edit(
    interaction: discord.Interaction,
    message_link: str,
    new_content: str,
    bold: bool = False,
    underline: bool = False,
    code_lang: str = ""
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)
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
    embed = discord.Embed(
        title="📌 Your Recent Channels",
        description="\n".join(names) if names else "None",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Bot Commands Help",
        description="Here are the available commands:",
        color=discord.Color.blurple()
    )
    # Music
    embed.add_field(name="🎵 Music",
                    value=(
                        "**/play** <query|url>\n"
                        "**/pause**, **/resume**, **/skip** (/**next**), **/stop**\n"
                        "**/queue**, **/nowplaying**\n"
                        "**/loop** (DJ/Admin), **/clearlist** (DJ/Admin)\n"
                        "**/volume** 0-100 (DJ/Admin), **/effect** (DJ/Admin)"
                    ),
                    inline=False)
    # Utility
    embed.add_field(
        name="🛠️ Utility",
        value=(
            "**/say** (Admin) • send formatted message\n"
            "**/embed** (Admin) • send embed\n"
            "**/edit** (Admin) • edit a bot message\n"
            "**/recent** • your last used channels"
        ),
        inline=False
    )
    embed.add_field(name="Legacy", value="**!ping**", inline=False)

    embed.set_thumbnail(url=interaction.client.user.avatar.url if interaction.client.user.avatar else interaction.client.user.default_avatar.url)
    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- Text Command ----------
@client.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! Latency: {round(client.latency * 1000)}ms")

# ---------- Events ----------
@client.event
async def on_ready():
    synced = await client.tree.sync()
    print(f"✅ Synced {len(synced)} commands globally")
    print(f"✅ Logged in as {client.user}")

client.run(TOKEN)
