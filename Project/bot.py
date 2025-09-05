import os
import re
import asyncio
import discord
from discord.ext import commands, tasks
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
    if len(recent_channels[user_id][guild_id]) > 30:
        recent_channels[user_id][guild_id].pop()

# ---------- Status Management ----------
default_status_enabled = True
last_joined_user = None

async def status_loop():
    await client.wait_until_ready()
    global default_status_enabled, last_joined_user

    while not client.is_closed():
        if default_status_enabled:
            # Show total members
            for guild in client.guilds:
                await client.change_presence(activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"Total: {guild.member_count} Members"
                ))
                await asyncio.sleep(10)

                # Show last joined user
                name = last_joined_user.name if last_joined_user else None
                display_text = f"Welcome {name}" if name else "Waiting for New Member"
                await client.change_presence(activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name=display_text
                ))
                await asyncio.sleep(10)
        else:
            await asyncio.sleep(5)

@client.event
async def on_member_join(member):
    global last_joined_user
    last_joined_user = member

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

    # Recent channels (per user)
    if user_id in recent_channels and guild.id in recent_channels[user_id]:
        for cid in recent_channels[user_id][guild.id][:10]:
            channel = guild.get_channel(cid)
            if channel and current.lower() in channel.name.lower():
                choices.append(app_commands.Choice(
                    name=f"⭐ {channel.name}", value=str(channel.id)
                ))

    # Normal channels
    for channel in guild.text_channels:
        if current.lower() in channel.name.lower():
            choices.append(app_commands.Choice(
                name=channel.name, value=str(channel.id)
            ))
        if len(choices) >= 15:
            break

    return choices

# ---------- Slash Commands ----------
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

# ---------- Custom Status Commands ----------
@client.tree.command(name="set_custom_status", description="Set a custom status (Admin only)")
async def set_custom_status(interaction: discord.Interaction, message: str):
    global default_status_enabled
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    default_status_enabled = False
    await client.change_presence(activity=discord.Activity(
        type=discord.ActivityType.playing,
        name=message
    ))
    await interaction.response.send_message(f"✅ Custom status set: {message}", ephemeral=True)

@client.tree.command(name="set_default_status", description="Enable the default rotating status (Admin only)")
async def set_default_status(interaction: discord.Interaction):
    global default_status_enabled
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    default_status_enabled = True
    await interaction.response.send_message("✅ Default rotating status enabled again.", ephemeral=True)

# ---------- Purge Command ----------
@client.tree.command(name="purge", description="Delete a number of messages from the current channel (Admin only)")
@app_commands.describe(number="How many messages to delete (max 100)")
@app_commands.choices(number=[
    app_commands.Choice(name="10", value=10),
    app_commands.Choice(name="25", value=25),
    app_commands.Choice(name="50", value=50),
    app_commands.Choice(name="100", value=100),
])
async def purge(interaction: discord.Interaction, number: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    if number < 1 or number > 100:
        return await interaction.response.send_message("❌ Please choose between 1–100 messages.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=number)
    await interaction.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)

# ---------- Counter Command ----------
counter_channels = {}      # {guild_id: [channel_ids]}
counter_base_names = {}    # {guild_id: {channel_id: base_name}}

async def category_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    choices = []
    for category in interaction.guild.categories:
        if current.lower() in category.name.lower():
            choices.append(app_commands.Choice(name=category.name, value=str(category.id)))
        if len(choices) >= 10:
            break
    return choices

async def channeltype_autocomplete(interaction: discord.Interaction, current: str):
    options = [
        ("Voice Channel", "voice"),
        ("Text Channel", "text")
    ]
    return [
        app_commands.Choice(name=name, value=value)
        for name, value in options
        if current.lower() in name.lower()
    ]

@client.tree.command(name="setcounter", description="Create a counter channel (Admin only)")
@app_commands.autocomplete(category_id=category_autocomplete, channel_type=channeltype_autocomplete)
async def setcounter(
    interaction: discord.Interaction,
    category_id: str,
    channel_name: str,
    channel_type: str,
    guild_counter: bool
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    category = discord.utils.get(interaction.guild.categories, id=int(category_id))
    if not category:
        return await interaction.response.send_message("❌ Category not found.", ephemeral=True)

    # Create channel
    if channel_type == "voice":
        new_channel = await category.create_voice_channel(channel_name)
    else:
        new_channel = await category.create_text_channel(channel_name)

    if guild_counter:
        count = interaction.guild.member_count
        await new_channel.edit(name=f"{channel_name} {count}")

        if interaction.guild.id not in counter_channels:
            counter_channels[interaction.guild.id] = []
        counter_channels[interaction.guild.id].append(new_channel.id)

        if interaction.guild.id not in counter_base_names:
            counter_base_names[interaction.guild.id] = {}
        counter_base_names[interaction.guild.id][new_channel.id] = channel_name

    await interaction.response.send_message(
        f"✅ Counter channel created: {new_channel.mention} (Guild Counter = {guild_counter})",
        ephemeral=True
    )

@tasks.loop(seconds=5)
async def counter_updater():
    for guild in client.guilds:
        if guild.id in counter_channels:
            for channel_id in counter_channels[guild.id]:
                channel = guild.get_channel(channel_id)
                if channel:
                    base_name = counter_base_names.get(guild.id, {}).get(channel.id, channel.name)
                    await channel.edit(name=f"{base_name} {guild.member_count}")

# ---------- Help ----------
@client.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Bot Commands Help",
        description="Here are the available commands:",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="/say",
        value="(Admin only) Send a formatted message to a selected channel.",
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
        name="/set_custom_status",
        value="(Admin only) Set a custom status. Disables default rotating status.",
        inline=False
    )
    embed.add_field(
        name="/set_default_status",
        value="(Admin only) Enable default rotating status again.",
        inline=False
    )
    embed.add_field(
        name="/purge",
        value="(Admin only) Delete messages in the current channel (1–100).",
        inline=False
    )
    embed.add_field(
        name="/setcounter",
        value="(Admin only) Create a counter channel inside a selected category.\n"
              "Options: category_id, channel_name, channel_type (voice/text), guild_counter (true/false)",
        inline=False
    )
    embed.add_field(
        name="!ping (text command)",
        value="Classic text command to check latency.",
        inline=False
    )

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
    client.loop.create_task(status_loop())
    counter_updater.start()

client.run(TOKEN)
