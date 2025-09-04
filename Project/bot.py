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
intents.message_content = True
intents.members = True
client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Permission Check ----------
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

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

# ---------- Slash Commands ----------
@client.tree.command(name="say", description="Send formatted message to a channel")
@is_admin()
async def say(
    interaction: discord.Interaction,
    channel: discord.TextChannel,   # dropdown selector
    content: str,
    bold: bool = False,
    underline: bool = False,
    code_lang: str = "",
    typing_ms: int = 0
):
    # Prevent cross-server messaging
    if channel.guild.id != interaction.guild_id:
        return await interaction.response.send_message(
            "❌ Cross-server messaging not allowed.", ephemeral=True
        )

    await interaction.response.send_message("Sending...", ephemeral=True)
    if typing_ms > 0:
        async with channel.typing():
            await asyncio.sleep(typing_ms / 1000)
    final = format_content(content, bold, underline, code_lang)
    sent = await channel.send(final)
    await interaction.edit_original_response(content=f"Sent ✅ ({sent.jump_url})")

@client.tree.command(name="embed", description="Send embed message to a channel")
@is_admin()
async def embed(
    interaction: discord.Interaction,
    channel: discord.TextChannel,   # dropdown selector
    title: str,
    description: str,
    color: str = "#5865F2",
    url: str = ""
):
    if channel.guild.id != interaction.guild_id:
        return await interaction.response.send_message(
            "❌ Cross-server messaging not allowed.", ephemeral=True
        )

    await interaction.response.send_message("Sending embed...", ephemeral=True)
    try:
        col = discord.Color(int(color.replace("#", ""), 16))
    except:
        col = discord.Color.blurple()
    e = discord.Embed(title=title, description=description, color=col)
    if url:
        e.url = url
    sent = await channel.send(embed=e)
    await interaction.edit_original_response(content=f"Embed sent ✅ ({sent.jump_url})")

@client.tree.command(name="edit", description="Edit existing message with link")
@is_admin()
async def edit(
    interaction: discord.Interaction,
    message_link: str,
    new_content: str,
    bold: bool = False,
    underline: bool = False,
    code_lang: str = ""
):
    parsed = parse_message_link(message_link)
    if not parsed:
        return await interaction.response.send_message("❌ Invalid message link.", ephemeral=True)

    guild_id, channel_id, msg_id = parsed
    channel = await client.fetch_channel(int(channel_id))
    if channel.guild.id != interaction.guild_id:
        return await interaction.response.send_message(
            "❌ You can only edit messages inside this server.", ephemeral=True
        )

    msg = await channel.fetch_message(int(msg_id))
    final = format_content(new_content, bold, underline, code_lang)
    await msg.edit(content=final)
    await interaction.response.send_message("Edited ✅", ephemeral=True)

# ---------- Sync Command ----------
@client.tree.command(name="sync", description="(Admin) Sync commands to this guild or globally")
@is_admin()
async def sync_cmd(interaction: discord.Interaction, scope: str = "guild"):
    await interaction.response.send_message("🔄 Syncing commands...", ephemeral=True)
    if scope == "global":
        await client.tree.sync()
        await interaction.followup.send("🌍 Global sync complete (may take time).", ephemeral=True)
    else:
        await client.tree.sync(guild=interaction.guild)
        await interaction.followup.send("✅ Guild sync complete.", ephemeral=True)

# ---------- Events ----------
@client.event
async def on_ready():
    for guild in client.guilds:
        try:
            await client.tree.sync(guild=discord.Object(id=guild.id))
            print(f"✅ Synced commands to guild {guild.name} ({guild.id})")
        except Exception as e:
            print(f"❌ Failed syncing {guild.id}: {e}")

    print(f"🤖 Logged in as {client.user}")

client.run(TOKEN)
