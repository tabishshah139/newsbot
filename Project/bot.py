import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Bot setup with intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Cache ----------
recent_channels = {}  # {user_id: {guild_id: [channel_ids]}}
report_channels = {}  # {guild_id: channel_id}
last_joined_member = {}  # {guild_id: member_name}
use_default_status = True
custom_status_message = None


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


# ---------- Autocomplete ----------
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


async def category_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    return [
        app_commands.Choice(name=cat.name, value=str(cat.id))
        for cat in interaction.guild.categories if current.lower() in cat.name.lower()
    ][:15]


async def channeltype_autocomplete(interaction: discord.Interaction, current: str):
    options = [
        ("Text Channel", "text"),
        ("Voice Channel", "voice"),
    ]
    return [
        app_commands.Choice(name=name, value=value)
        for name, value in options if current.lower() in name.lower()
    ]


# ---------- Slash Commands ----------
@client.tree.command(name="say", description="Send formatted message to channel")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def say(interaction: discord.Interaction, channel_id: str, content: str,
              bold: bool = False, underline: bool = False,
              code_lang: str = "", typing_ms: int = 0):
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
async def embed(interaction: discord.Interaction, channel_id: str, title: str, description: str,
                color: str = "#5865F2", url: str = ""):
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
async def edit(interaction: discord.Interaction, message_link: str, new_content: str,
               bold: bool = False, underline: bool = False, code_lang: str = ""):
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
    embed.add_field(
        name="/say",
        value="(Admin only) Send a formatted message to a selected channel.\n"
              "Options: channel_id, content, bold, underline, code_lang, typing_ms",
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
    embed.add_field(
        name="/recent",
        value="Show your last used channels (only visible to you).",
        inline=False
    )
    embed.add_field(
        name="/purge",
        value="(Admin only) Delete a number of messages from a channel.",
        inline=False
    )
    embed.add_field(
        name="/setcounter",
        value="(Admin only) Create a live counter channel (members etc.).",
        inline=False
    )
    embed.add_field(
        name="/setreport",
        value="(Admin only) Set the channel where reports will be sent.",
        inline=False
    )
    embed.add_field(
        name="/help",
        value="Show this help menu.",
        inline=False
    )
    embed.add_field(
        name="!ping (text command)",
        value="Classic text command to check latency.",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Purge ----------
@client.tree.command(name="purge", description="Delete multiple messages from a channel (Admin only)")
async def purge(interaction: discord.Interaction, limit: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=limit)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)


# ---------- Counter ----------
@client.tree.command(name="setcounter", description="Create a counter channel")
@app_commands.autocomplete(category_id=category_autocomplete, channel_type=channeltype_autocomplete)
async def setcounter(interaction: discord.Interaction, category_id: str, channel_name: str,
                     channel_type: str, guild_counter: bool):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    category = discord.utils.get(interaction.guild.categories, id=int(category_id))
    if not category:
        return await interaction.response.send_message("❌ Invalid category.", ephemeral=True)

    async def update_counter(ch):
        while True:
            try:
                if guild_counter:
                    count = interaction.guild.member_count
                    new_name = f"{channel_name} {count}"
                else:
                    new_name = channel_name
                await ch.edit(name=new_name)
            except:
                pass
            await asyncio.sleep(5)

    if channel_type == "text":
        ch = await interaction.guild.create_text_channel(name=f"{channel_name} 0", category=category)
    else:
        ch = await interaction.guild.create_voice_channel(name=f"{channel_name} 0", category=category)

    client.loop.create_task(update_counter(ch))
    await interaction.response.send_message(f"✅ Counter channel created: {ch.mention}", ephemeral=True)


# ---------- Report System ----------
@app_commands.command(name="setreport", description="Set the channel for security reports (Admin only)")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def setreport(interaction: discord.Interaction, channel_id: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    channel = await client.fetch_channel(int(channel_id))
    if not channel:
        return await interaction.response.send_message("❌ Invalid channel.", ephemeral=True)

    report_channels[interaction.guild.id] = channel.id
    await interaction.response.send_message(f"✅ Reports will now be sent to {channel.mention}", ephemeral=True)

client.tree.add_command(setreport)


# ---------- Security Guard ----------
BLOCK_LINKS = True

def load_badwords():
    try:
        with open("badwords.txt", "r", encoding="utf-8") as f:
            return [w.strip().lower() for w in f.readlines() if w.strip()]
    except FileNotFoundError:
        print("⚠️ No badwords.txt file found, filter will not work!")
        return []

BAD_WORDS = load_badwords()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.author.guild_permissions.administrator:
        return
    if any(role.name == "Basic" for role in message.author.roles):
        return

    content_lower = message.content.lower()
    guild_id = message.guild.id if message.guild else None

    for bad_word in BAD_WORDS:
        if bad_word in content_lower:
            try:
                await message.delete()
                await message.channel.send(
                    f"🚫 Hey {message.author.mention}, stop! Do not use offensive language. "
                    f"Continued violations may lead to a ban.",
                    delete_after=7
                )
                if guild_id in report_channels:
                    log_ch = message.guild.get_channel(report_channels[guild_id])
                    if log_ch:
                        await log_ch.send(
                            f"⚠️ {message.author.mention} has misbehaved and used: **{bad_word}**"
                        )
            except:
                pass
            return

    if BLOCK_LINKS and ("http://" in content_lower or "https://" in content_lower or "discord.gg/" in content_lower):
        try:
            await message.delete()
            await message.channel.send(
                f"🚫 {message.author.mention}, please do not advertise or share links here. "
                f"Contact the server admin for partnership opportunities.",
                delete_after=7
            )
            if guild_id in report_channels:
                log_ch = message.guild.get_channel(report_channels[guild_id])
                if log_ch:
                    await log_ch.send(
                        f"⚠️ {message.author.mention} has advertised: `{message.content}`"
                    )
        except:
            pass
        return

    await client.process_commands(message)


# ---------- Status ----------
async def status_task():
    await client.wait_until_ready()
    while not client.is_closed():
        if use_default_status and custom_status_message is None:
            for guild in client.guilds:
                member_count = guild.member_count
                await client.change_presence(activity=discord.Game(name=f"Total: {member_count} Members"))
                await asyncio.sleep(10)
                member_name = last_joined_member.get(guild.id, None)
                if member_name:
                    await client.change_presence(activity=discord.Game(name=f"Welcome {member_name}"))
                else:
                    await client.change_presence(activity=discord.Game(name="Waiting for New Member"))
                await asyncio.sleep(10)
        elif custom_status_message:
            await client.change_presence(activity=discord.Game(name=custom_status_message))
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(5)


@client.tree.command(name="setcustomstatus", description="Set a custom bot status (Admin only)")
async def setcustomstatus(interaction: discord.Interaction, status: str):
    global use_default_status, custom_status_message
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)
    custom_status_message = status
    use_default_status = False
    await interaction.response.send_message(f"✅ Custom status set: {status}", ephemeral=True)


@client.tree.command(name="setdefaultstatus", description="Enable default rotating status (Admin only)")
async def setdefaultstatus(interaction: discord.Interaction):
    global use_default_status, custom_status_message
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)
    custom_status_message = None
    use_default_status = True
    await interaction.response.send_message("✅ Default rotating status enabled.", ephemeral=True)


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
    client.loop.create_task(status_task())


@client.event
async def on_member_join(member):
    last_joined_member[member.guild.id] = member.name


client.run(TOKEN)
