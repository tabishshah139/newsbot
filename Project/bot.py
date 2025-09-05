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

# ---------- Cache ----------
recent_channels = {}  # {user_id: {guild_id: [channel_ids]}}
last_joined_user = {}  # {guild_id: str}
custom_status = {}  # {guild_id: str or None}
counter_channels = {}  # {guild_id: [channel_ids]}

# ---------- Cache Helpers ----------
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

# ---------- Format Helpers ----------
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

    # Recent channels
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
async def say(interaction: discord.Interaction, channel_id: str, content: str, bold: bool=False, underline: bool=False, code_lang: str="", typing_ms: int=0):
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
async def embed(interaction: discord.Interaction, channel_id: str, title: str, description: str, color: str="#5865F2", url: str=""):
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
async def edit(interaction: discord.Interaction, message_link: str, new_content: str, bold: bool=False, underline: bool=False, code_lang: str=""):
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
    embed = discord.Embed(title="📌 Your Recent Channels", description="\n".join(names) if names else "None", color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- Status Commands ----------
@client.tree.command(name="set_custom_status", description="Set a custom bot status (Admin only)")
async def set_custom_status(interaction: discord.Interaction, status: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)

    custom_status[interaction.guild.id] = status
    await client.change_presence(activity=discord.Game(name=status))
    await interaction.response.send_message(f"✅ Custom status set: {status}", ephemeral=True)

@client.tree.command(name="set_default_status", description="Re-enable default looping status (Admin only)")
async def set_default_status(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)

    custom_status[interaction.guild.id] = None
    await interaction.response.send_message("✅ Default status loop re-enabled.", ephemeral=True)

# ---------- Purge Command (Fixed) ----------
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

    # Defer response to avoid "did not respond" error
    await interaction.response.defer(ephemeral=True)

    deleted = await interaction.channel.purge(limit=number)
    await interaction.followup.send(f"✅ Deleted {len(deleted)} messages.", ephemeral=True)

# ---------- Counter Command ----------
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

@client.tree.command(name="setcounter", description="Create a counter channel (Admin only)")
@app_commands.autocomplete(category_id=category_autocomplete)
async def setcounter(interaction: discord.Interaction, category_id: str, channel_name: str, channel_type: str, guild_counter: bool):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=True)

    category = discord.utils.get(interaction.guild.categories, id=int(category_id))
    if not category:
        return await interaction.response.send_message("❌ Category not found.", ephemeral=True)

    if channel_type.lower() == "voice":
        new_channel = await category.create_voice_channel(channel_name)
    else:
        new_channel = await category.create_text_channel(channel_name)

    if guild_counter:
        count = interaction.guild.member_count
        await new_channel.edit(name=f"{channel_name} {count}")
        if interaction.guild.id not in counter_channels:
            counter_channels[interaction.guild.id] = []
        counter_channels[interaction.guild.id].append(new_channel.id)

    await interaction.response.send_message(f"✅ Counter channel created: {new_channel.mention} (Guild Counter = {guild_counter})", ephemeral=True)

# ---------- Help ----------
@client.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Bot Commands Help", description="Here are the available commands:", color=discord.Color.blurple())
    embed.add_field(name="/say", value="(Admin only) Send a formatted message to a selected channel.", inline=False)
    embed.add_field(name="/embed", value="(Admin only) Send an embed message with title, description, color, and URL.", inline=False)
    embed.add_field(name="/edit", value="(Admin only) Edit an existing bot message using its link.", inline=False)
    embed.add_field(name="/recent", value="Show your last used channels (only visible to you).", inline=False)
    embed.add_field(name="/set_custom_status", value="(Admin only) Set a custom bot status. Default loop stops.", inline=False)
    embed.add_field(name="/set_default_status", value="(Admin only) Re-enable the default status loop.", inline=False)
    embed.add_field(name="/purge", value="(Admin only) Delete messages (1–100) from current channel.", inline=False)
    embed.add_field(name="/setcounter", value="(Admin only) Create a counter channel (voice/text) in a category.", inline=False)
    embed.add_field(name="!ping", value="Classic text command to check latency.", inline=False)
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
    status_loop.start()
    counter_updater.start()

@client.event
async def on_member_join(member):
    last_joined_user[member.guild.id] = member.name

# ---------- Background Tasks ----------
@tasks.loop(seconds=10)
async def status_loop():
    for guild in client.guilds:
        if custom_status.get(guild.id):
            continue
        # 1. Total members
        await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Total: {guild.member_count} Members"))
        await asyncio.sleep(5)
        # 2. Last joined user
        user = last_joined_user.get(guild.id, None)
        if user:
            await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name=f"Welcome {user}"))
        else:
            await client.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="Waiting for New Member"))

@tasks.loop(seconds=5)
async def counter_updater():
    for guild in client.guilds:
        if guild.id in counter_channels:
            for channel_id in counter_channels[guild.id]:
                channel = guild.get_channel(channel_id)
                if channel:
                    await channel.edit(name=f"Members: {guild.member_count}")

client.run(TOKEN)
