import os
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load .env / Railway Env Vars
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Bot setup with intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required for on_member_join event
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

# ---------- Status System ----------
use_custom_status = False
custom_status_message = None
last_joined_member = None   # Default: None, so we can show "Waiting for New Member"

async def status_loop():
    await client.wait_until_ready()
    guild_id = os.getenv("GUILD_ID")
    guild = client.get_guild(int(guild_id)) if guild_id else None
    global use_custom_status, custom_status_message, last_joined_member

    toggle = True
    while not client.is_closed():
        if use_custom_status and custom_status_message:
            await client.change_presence(activity=discord.CustomActivity(name=custom_status_message))
            await asyncio.sleep(10)
            continue

        if guild:
            if toggle:
                count = guild.member_count
                await client.change_presence(activity=discord.CustomActivity(name=f"Total: {count} Members"))
            else:
                if last_joined_member:  # agar koi member join kar chuka hai
                    await client.change_presence(activity=discord.CustomActivity(name=f"Welcome {last_joined_member}"))
                else:  # abhi tak koi new member join nahi hua
                    await client.change_presence(activity=discord.CustomActivity(name="Waiting for New Member"))
            toggle = not toggle

        await asyncio.sleep(10)

@client.event
async def on_member_join(member):
    global last_joined_member
    last_joined_member = member.name

@client.tree.command(name="set_custom_status", description="Set a custom bot status (Admin only)")
async def set_custom_status(interaction: discord.Interaction, message: str):
    global use_custom_status, custom_status_message
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)

    use_custom_status = True
    custom_status_message = message
    await client.change_presence(activity=discord.CustomActivity(name=message))
    await interaction.response.send_message(f"✅ Custom status set: {message}", ephemeral=True)

@client.tree.command(name="set_default_status", description="Revert to default looped status (Admin only)")
async def set_default_status(interaction: discord.Interaction):
    global use_custom_status, custom_status_message
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)

    use_custom_status = False
    custom_status_message = None
    await interaction.response.send_message("✅ Default status loop resumed.", ephemeral=True)

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
            await asyncio.sleep(min(typing_ms, 10000) / 1000)
    final = format_content(content, bold, underline, code_lang)
    sent = await channel.send(final)
    update_recent_channel(interaction.user.id, interaction.guild.id, int(channel_id))
    await interaction.edit_original_response(content=f"Sent ✅ ({sent.jump_url})")

@client.tree.command(name="embed", description="Send embed message")
@app_commands.autocomplete(channel_id=channel_autocomplete)
async def embed(interaction: discord.Interaction, channel_id: str, title: str, description: str, color: str="#5865F2", url: str=""):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)

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
        return await interaction.response.send_message("❌ You are not allowed.", ephemeral=True)

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
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Bot Commands Help", description="Here are the available commands:", color=discord.Color.blurple())
    embed.add_field(name="/say", value="(Admin only) Send a formatted message to a selected channel.\nOptions: channel_id, content, bold, underline, code_lang, typing_ms", inline=False)
    embed.add_field(name="/embed", value="(Admin only) Send an embed message with title, description, color, and URL.", inline=False)
    embed.add_field(name="/edit", value="(Admin only) Edit an existing bot message using its link.", inline=False)
    embed.add_field(name="/recent", value="Show your last used channels (only visible to you).", inline=False)
    embed.add_field(name="/set_custom_status", value="(Admin only) Set a custom bot status, stops loop.", inline=False)
    embed.add_field(name="/set_default_status", value="(Admin only) Resume default rotating status.", inline=False)
    embed.add_field(name="/help", value="Show this help menu.", inline=False)
    embed.add_field(name="!ping (text command)", value="Classic text command to check latency.", inline=False)
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

client.run(TOKEN)
