import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import datetime
import random
import httpx
import asyncio
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# ── FLASK API SERVER ─────────────────────────────────
app = Flask(__name__, static_folder='static')
CORS(app)

# ── CONFIG ──────────────────────────────────────────
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GROQ_KEY      = os.environ.get("GROQ_KEY")
PREFIX        = "!"
LOG_FILE      = "data.json"

# ── DATA STORE ──────────────────────────────────────
def load_data():
    if Path(LOG_FILE).exists():
        with open(LOG_FILE) as f:
            return json.load(f)
    return {
        "custom_commands": {},
        "auto_replies":    {},
        "schedules":       [],
        "xp":              {},
        "warns":           {},
        "welcome":         {},
        "log_channel":     {},
        "ai_channels":     [],
        "mutes":           {},
    }

def save_data(data):
    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)

DATA = load_data()

# ── BOT SETUP ───────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
ai_history = {}

# ── GROQ AI ─────────────────────────────────────────
async def ask_groq(channel_id: int, username: str, prompt: str) -> str:
    if not GROQ_KEY:
        return "❌ Groq API key not set. Add `GROQ_KEY` in Railway variables."

    history = ai_history.get(channel_id, [])
    history.append({"role": "user", "content": f"{username}: {prompt}"})
    if len(history) > 20:
        history = history[-20:]

    messages = [
        {
            "role": "system",
            "content": (
                "You are CallMest, a friendly and helpful Discord bot assistant. "
                "You are fun, concise, and supportive. Keep replies under 1800 characters. "
                "You remember the conversation context within this channel."
            )
        }
    ] + history

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": messages,
                    "max_tokens": 500,
                    "temperature": 0.8,
                },
            )
            res.raise_for_status()
            reply = res.json()["choices"][0]["message"]["content"].strip()
            history.append({"role": "assistant", "content": reply})
            ai_history[channel_id] = history
            return reply
    except httpx.HTTPStatusError as e:
        return f"❌ Groq API error: {e.response.status_code}"
    except Exception as e:
        return f"❌ Something went wrong: {str(e)}"

# ── EVENTS ──────────────────────────────────────────
@bot.event
async def on_ready():
    await bot.tree.sync()
    check_schedules.start()
    check_mutes.start()
    print(f"✅ {bot.user} is online!")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="!help | Callmest"))

@bot.event
async def on_member_join(member):
    gid = str(member.guild.id)
    cfg = DATA["welcome"].get(gid)
    if not cfg:
        return
    ch = member.guild.get_channel(cfg["channel_id"])
    if ch:
        msg = cfg["message"].replace("{user}", member.mention).replace("{server}", member.guild.name)
        embed = discord.Embed(description=msg, color=0x5865F2)
        embed.set_thumbnail(url=member.display_avatar.url)
        await ch.send(embed=embed)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    uid = str(message.author.id)
    gid = str(message.guild.id) if message.guild else None

    # XP system
    if gid:
        DATA["xp"].setdefault(uid, 0)
        old_level = DATA["xp"][uid] // 100
        DATA["xp"][uid] += random.randint(5, 15)
        new_level = DATA["xp"][uid] // 100
        if new_level > old_level:
            await message.channel.send(f"🎉 {message.author.mention} leveled up to **Level {new_level}**!")
        save_data(DATA)

    # Auto-replies
    content_lower = message.content.lower()
    for trigger, response in DATA["auto_replies"].items():
        if trigger.lower() in content_lower:
            await message.channel.send(response)
            break

    # AI channel mode
    if message.channel.id in DATA["ai_channels"]:
        async with message.channel.typing():
            reply = await ask_groq(message.channel.id, message.author.display_name, message.content)
        await message.channel.send(reply)
        return

    # Custom commands
    if message.content.startswith(PREFIX):
        cmd_name = message.content[len(PREFIX):].split()[0].lower()
        if cmd_name in DATA["custom_commands"]:
            await message.channel.send(DATA["custom_commands"][cmd_name])
            return

    await bot.process_commands(message)

# ── BACKGROUND TASKS ────────────────────────────────
@tasks.loop(minutes=1)
async def check_schedules():
    now = datetime.datetime.utcnow()
    for sched in DATA["schedules"]:
        next_run = datetime.datetime.fromisoformat(sched["next_run"])
        if now >= next_run:
            ch = bot.get_channel(sched["channel_id"])
            if ch:
                await ch.send(sched["message"])
            delta = datetime.timedelta(minutes=sched["interval_minutes"])
            sched["next_run"] = (next_run + delta).isoformat()
    save_data(DATA)

@tasks.loop(minutes=1)
async def check_mutes():
    now = datetime.datetime.utcnow().timestamp()
    to_remove = []
    for uid, end_ts in DATA["mutes"].items():
        if now >= end_ts:
            to_remove.append(uid)
            for guild in bot.guilds:
                member = guild.get_member(int(uid))
                mute_role = discord.utils.get(guild.roles, name="Muted")
                if member and mute_role and mute_role in member.roles:
                    await member.remove_roles(mute_role)
    for uid in to_remove:
        del DATA["mutes"][uid]
    if to_remove:
        save_data(DATA)

# ── HELP ────────────────────────────────────────────
@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="🤖 Callmest — Commands", color=0x5865F2)
    embed.add_field(name="🛡️ Mod", value="`!kick` `!ban` `!unban` `!mute` `!unmute` `!warn` `!warns` `!clearwarn` `!purge` `!slowmode` `!lock` `!unlock`", inline=False)
    embed.add_field(name="🤖 AI", value="`!ai <question>` — ask AI anything\n`/ai-channel set/remove` — AI auto-reply channel\n`!ai-reset` — clear AI memory", inline=False)
    embed.add_field(name="⏰ Schedule", value="`/schedule add/list/remove`", inline=False)
    embed.add_field(name="💬 Auto-Reply", value="`/autoreply add/list/remove`", inline=False)
    embed.add_field(name="🔧 Custom Cmds", value="`/cmd add/list/remove`", inline=False)
    embed.add_field(name="📊 Fun", value="`!poll` `!rank` `!leaderboard` `!coinflip` `!roll` `!8ball` `!userinfo` `!serverinfo` `!ping`", inline=False)
    embed.add_field(name="⚙️ Config", value="`/welcome set/off` • `/logchannel set/off`", inline=False)
    await ctx.send(embed=embed)

# ── MODERATION ──────────────────────────────────────
def mod_embed(action, target, reason, color=0xff4444):
    e = discord.Embed(title=f"🔨 {action}", color=color, timestamp=datetime.datetime.utcnow())
    e.add_field(name="User", value=str(target))
    e.add_field(name="Reason", value=reason or "No reason given")
    return e

@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason=None):
    await member.kick(reason=reason)
    await ctx.send(embed=mod_embed("Kick", member, reason))

@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason=None):
    await member.ban(reason=reason)
    await ctx.send(embed=mod_embed("Ban", member, reason))

@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, *, user_str: str):
    bans = [entry async for entry in ctx.guild.bans()]
    for entry in bans:
        if str(entry.user) == user_str or str(entry.user.id) == user_str:
            await ctx.guild.unban(entry.user)
            return await ctx.send(f"✅ Unbanned {entry.user}")
    await ctx.send("❌ User not found in ban list.")

@bot.command()
@commands.has_permissions(manage_roles=True)
async def mute(ctx, member: discord.Member, minutes: int = 10, *, reason=None):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not mute_role:
        mute_role = await ctx.guild.create_role(name="Muted")
        for channel in ctx.guild.channels:
            await channel.set_permissions(mute_role, send_messages=False, speak=False)
    await member.add_roles(mute_role, reason=reason)
    end_ts = (datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)).timestamp()
    DATA["mutes"][str(member.id)] = end_ts
    save_data(DATA)
    await ctx.send(embed=mod_embed("Mute", member, f"{reason} ({minutes}m)", 0xffa500))

@bot.command()
@commands.has_permissions(manage_roles=True)
async def unmute(ctx, member: discord.Member):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if mute_role and mute_role in member.roles:
        await member.remove_roles(mute_role)
        DATA["mutes"].pop(str(member.id), None)
        save_data(DATA)
        await ctx.send(embed=mod_embed("Unmute", member, None, 0x00ff00))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason=None):
    DATA["warns"].setdefault(str(member.id), []).append({
        "reason": reason, "by": str(ctx.author), "at": str(datetime.datetime.utcnow())
    })
    save_data(DATA)
    await ctx.send(embed=mod_embed("Warn", member, reason, 0xffff00))

@bot.command()
async def warns(ctx, member: discord.Member = None):
    member = member or ctx.author
    w = DATA["warns"].get(str(member.id), [])
    embed = discord.Embed(title=f"⚠️ Warns for {member.display_name}", color=0xffff00)
    for i, entry in enumerate(w):
        embed.add_field(name=f"#{i+1}", value=f"{entry['reason']} — by {entry['by']}", inline=False)
    if not w:
        embed.description = "No warnings."
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clearwarn(ctx, member: discord.Member):
    DATA["warns"].pop(str(member.id), None)
    save_data(DATA)
    await ctx.send(f"✅ Cleared warnings for {member.mention}")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int = 10):
    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🗑️ Deleted {amount} messages.", delete_after=3)

@bot.command()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int = 0):
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"✅ Slowmode set to {seconds}s.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send("🔓 Channel unlocked.")

# ── AI COMMANDS ──────────────────────────────────────
@bot.command(name="ai")
async def ai_cmd(ctx, *, prompt: str):
    async with ctx.typing():
        reply = await ask_groq(ctx.channel.id, ctx.author.display_name, prompt)
    await ctx.send(reply)

@bot.command(name="ai-reset")
async def ai_reset(ctx):
    ai_history.pop(ctx.channel.id, None)
    await ctx.send("🧹 AI memory cleared for this channel!")

ai_group = app_commands.Group(name="ai-channel", description="AI channel mode")

@ai_group.command(name="set", description="Make this channel AI-only (every message gets a reply)")
@app_commands.checks.has_permissions(manage_channels=True)
async def ai_set(interaction: discord.Interaction):
    if interaction.channel_id not in DATA["ai_channels"]:
        DATA["ai_channels"].append(interaction.channel_id)
        save_data(DATA)
    await interaction.response.send_message("✅ AI mode enabled for this channel.")

@ai_group.command(name="remove", description="Disable AI mode for this channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def ai_remove(interaction: discord.Interaction):
    if interaction.channel_id in DATA["ai_channels"]:
        DATA["ai_channels"].remove(interaction.channel_id)
        save_data(DATA)
    await interaction.response.send_message("✅ AI mode disabled.")

bot.tree.add_command(ai_group)

# ── SCHEDULE ────────────────────────────────────────
sched_group = app_commands.Group(name="schedule", description="Scheduled messages")

@sched_group.command(name="add", description="Schedule a recurring message")
@app_commands.checks.has_permissions(manage_guild=True)
async def schedule_add(interaction: discord.Interaction, channel: discord.TextChannel, message: str, interval_minutes: int):
    next_run = (datetime.datetime.utcnow() + datetime.timedelta(minutes=interval_minutes)).isoformat()
    DATA["schedules"].append({"channel_id": channel.id, "message": message, "interval_minutes": interval_minutes, "next_run": next_run})
    save_data(DATA)
    await interaction.response.send_message(f"✅ Scheduled every {interval_minutes}m in {channel.mention}")

@sched_group.command(name="list", description="List scheduled messages")
async def schedule_list(interaction: discord.Interaction):
    if not DATA["schedules"]:
        return await interaction.response.send_message("No schedules.", ephemeral=True)
    lines = [f"#{i}: every {s['interval_minutes']}m → {s['message'][:40]}" for i, s in enumerate(DATA["schedules"])]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@sched_group.command(name="remove", description="Remove a schedule by index")
@app_commands.checks.has_permissions(manage_guild=True)
async def schedule_remove(interaction: discord.Interaction, index: int):
    if 0 <= index < len(DATA["schedules"]):
        DATA["schedules"].pop(index)
        save_data(DATA)
        await interaction.response.send_message(f"✅ Schedule #{index} removed.")
    else:
        await interaction.response.send_message("❌ Invalid index.", ephemeral=True)

bot.tree.add_command(sched_group)

# ── AUTO-REPLY ──────────────────────────────────────
ar_group = app_commands.Group(name="autoreply", description="Auto-reply triggers")

@ar_group.command(name="add", description="Add a trigger → response")
@app_commands.checks.has_permissions(manage_messages=True)
async def ar_add(interaction: discord.Interaction, trigger: str, response: str):
    DATA["auto_replies"][trigger.lower()] = response
    save_data(DATA)
    await interaction.response.send_message(f"✅ Auto-reply: **{trigger}** → {response}")

@ar_group.command(name="list", description="List auto-replies")
async def ar_list(interaction: discord.Interaction):
    if not DATA["auto_replies"]:
        return await interaction.response.send_message("No auto-replies.", ephemeral=True)
    await interaction.response.send_message(
        "\n".join(f"• `{t}` → {r}" for t, r in DATA["auto_replies"].items()), ephemeral=True)

@ar_group.command(name="remove", description="Remove an auto-reply")
@app_commands.checks.has_permissions(manage_messages=True)
async def ar_remove(interaction: discord.Interaction, trigger: str):
    if trigger.lower() in DATA["auto_replies"]:
        del DATA["auto_replies"][trigger.lower()]
        save_data(DATA)
        await interaction.response.send_message(f"✅ Removed **{trigger}**.")
    else:
        await interaction.response.send_message("❌ Not found.", ephemeral=True)

bot.tree.add_command(ar_group)

# ── CUSTOM COMMANDS ─────────────────────────────────
cmd_group = app_commands.Group(name="cmd", description="Custom prefix commands")

@cmd_group.command(name="add", description="Add a custom command")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_add(interaction: discord.Interaction, name: str, response: str):
    DATA["custom_commands"][name.lower()] = response
    save_data(DATA)
    await interaction.response.send_message(f"✅ Command `!{name}` created.")

@cmd_group.command(name="list", description="List custom commands")
async def cmd_list(interaction: discord.Interaction):
    if not DATA["custom_commands"]:
        return await interaction.response.send_message("No custom commands.", ephemeral=True)
    await interaction.response.send_message(
        "\n".join(f"• `!{n}` → {r[:60]}" for n, r in DATA["custom_commands"].items()), ephemeral=True)

@cmd_group.command(name="remove", description="Remove a custom command")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_remove(interaction: discord.Interaction, name: str):
    if name.lower() in DATA["custom_commands"]:
        del DATA["custom_commands"][name.lower()]
        save_data(DATA)
        await interaction.response.send_message(f"✅ Removed `!{name}`.")
    else:
        await interaction.response.send_message("❌ Not found.", ephemeral=True)

bot.tree.add_command(cmd_group)

# ── WELCOME & LOG ────────────────────────────────────
welcome_group = app_commands.Group(name="welcome", description="Welcome messages")

@welcome_group.command(name="set", description="Set welcome channel and message")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_set(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    DATA["welcome"][str(interaction.guild_id)] = {"channel_id": channel.id, "message": message}
    save_data(DATA)
    await interaction.response.send_message(f"✅ Welcome messages → {channel.mention}")

@welcome_group.command(name="off", description="Disable welcome messages")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_off(interaction: discord.Interaction):
    DATA["welcome"].pop(str(interaction.guild_id), None)
    save_data(DATA)
    await interaction.response.send_message("✅ Welcome messages disabled.")

bot.tree.add_command(welcome_group)

log_group = app_commands.Group(name="logchannel", description="Message logging")

@log_group.command(name="set", description="Set log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def log_set(interaction: discord.Interaction, channel: discord.TextChannel):
    DATA["log_channel"][str(interaction.guild_id)] = channel.id
    save_data(DATA)
    await interaction.response.send_message(f"✅ Logging to {channel.mention}.")

@log_group.command(name="off", description="Disable logging")
@app_commands.checks.has_permissions(manage_guild=True)
async def log_off(interaction: discord.Interaction):
    DATA["log_channel"].pop(str(interaction.guild_id), None)
    save_data(DATA)
    await interaction.response.send_message("✅ Logging disabled.")

bot.tree.add_command(log_group)

# ── FUN COMMANDS ─────────────────────────────────────
@bot.command()
async def poll(ctx, *, text: str):
    parts = [p.strip() for p in text.split("|")]
    question = parts[0]
    options = parts[1:] if len(parts) > 1 else ["✅ Yes", "❌ No"]
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    desc = "\n".join(f"{emojis[i]} {opt}" for i, opt in enumerate(options[:10]))
    embed = discord.Embed(title=f"📊 {question}", description=desc, color=0x5865F2)
    msg = await ctx.send(embed=embed)
    for i in range(min(len(options), 10)):
        await msg.add_reaction(emojis[i])

@bot.command()
async def rank(ctx, member: discord.Member = None):
    member = member or ctx.author
    xp = DATA["xp"].get(str(member.id), 0)
    level = xp // 100
    embed = discord.Embed(
        title=f"⭐ {member.display_name}",
        description=f"Level **{level}** | XP: **{xp}**",
        color=0x5865F2)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command()
async def leaderboard(ctx):
    sorted_xp = sorted(DATA["xp"].items(), key=lambda x: x[1], reverse=True)[:10]
    medals = ["🥇","🥈","🥉"]
    lines = []
    for i, (uid, xp) in enumerate(sorted_xp):
        user = ctx.guild.get_member(int(uid))
        name = user.display_name if user else f"User {uid}"
        lines.append(f"{medals[i] if i < 3 else f'{i+1}.'} **{name}** — {xp} XP (Lv {xp//100})")
    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines) or "No data.", color=0xffd700)
    await ctx.send(embed=embed)

@bot.command()
async def coinflip(ctx):
    await ctx.send(f"**{random.choice(['Heads 🪙', 'Tails 🪙'])}**")

@bot.command()
async def roll(ctx, sides: int = 6):
    await ctx.send(f"🎲 You rolled a **{random.randint(1, sides)}** (d{sides})")

@bot.command(name="8ball")
async def eightball(ctx, *, question: str):
    responses = [
        "It is certain.", "Without a doubt.", "Yes, definitely.",
        "You may rely on it.", "Most likely.", "Outlook good.",
        "Don't count on it.", "My reply is no.", "Very doubtful.",
        "Ask again later.", "Cannot predict now.", "Concentrate and ask again."
    ]
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=0x5865F2)
    embed.add_field(name="Question", value=question, inline=False)
    embed.add_field(name="Answer", value=random.choice(responses), inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"👤 {member}", color=0x5865F2)
    embed.add_field(name="ID", value=member.id)
    embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d"))
    embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"))
    embed.add_field(name="Roles", value=", ".join(r.mention for r in member.roles[1:]) or "None")
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command()
async def serverinfo(ctx):
    g = ctx.guild
    embed = discord.Embed(title=f"🌐 {g.name}", color=0x5865F2)
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Channels", value=len(g.channels))
    embed.add_field(name="Roles", value=len(g.roles))
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"))
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=embed)

@bot.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

# ── TICKET SYSTEM (COMPLETE) ────────────────────────
ticket_group = app_commands.Group(name="ticket", description="Support ticket system")

# Store data
TICKET_DATA = "tickets.json"
TICKET_CONFIG = "ticket_config.json"

def load_tickets():
    if Path(TICKET_DATA).exists():
        with open(TICKET_DATA) as f:
            return json.load(f)
    return {}

def save_tickets(tickets):
    with open(TICKET_DATA, "w") as f:
        json.dump(tickets, f, indent=2)

def load_ticket_config():
    if Path(TICKET_CONFIG).exists():
        with open(TICKET_CONFIG) as f:
            return json.load(f)
    return {}

def save_ticket_config(config):
    with open(TICKET_CONFIG, "w") as f:
        json.dump(config, f, indent=2)

TICKETS = load_tickets()
TICKET_CONFIG_DATA = load_ticket_config()

# ── TICKET CONFIG COMMANDS ──────────────────────────
@ticket_group.command(name="add", description="Add staff role to ticket system")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_add_staff(interaction: discord.Interaction, role: discord.Role):
    """Add a staff role to access all tickets"""
    guild_id = str(interaction.guild_id)
    
    if guild_id not in TICKET_CONFIG_DATA:
        TICKET_CONFIG_DATA[guild_id] = {"staff_roles": [], "log_channel": None, "archive_channel": None}
    
    if role.id not in TICKET_CONFIG_DATA[guild_id]["staff_roles"]:
        TICKET_CONFIG_DATA[guild_id]["staff_roles"].append(role.id)
        save_ticket_config(TICKET_CONFIG_DATA)
        
        embed = discord.Embed(
            title="✅ Staff Role Added",
            description=f"{role.mention} can now access all tickets",
            color=0x00ff00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        embed = discord.Embed(
            title="⚠️ Already Added",
            description=f"{role.mention} is already a staff role",
            color=0xffff00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@ticket_group.command(name="remove", description="Remove staff role from ticket system")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_remove_staff(interaction: discord.Interaction, role: discord.Role):
    """Remove a staff role"""
    guild_id = str(interaction.guild_id)
    
    if guild_id in TICKET_CONFIG_DATA and role.id in TICKET_CONFIG_DATA[guild_id]["staff_roles"]:
        TICKET_CONFIG_DATA[guild_id]["staff_roles"].remove(role.id)
        save_ticket_config(TICKET_CONFIG_DATA)
        
        embed = discord.Embed(
            title="✅ Staff Role Removed",
            description=f"{role.mention} can no longer access all tickets",
            color=0x00ff00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        embed = discord.Embed(
            title="❌ Not Found",
            description=f"{role.mention} is not a staff role",
            color=0xff4444
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@ticket_group.command(name="set", description="Set ticket logs channel")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_set_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set the channel where ticket logs will be sent"""
    guild_id = str(interaction.guild_id)
    
    if guild_id not in TICKET_CONFIG_DATA:
        TICKET_CONFIG_DATA[guild_id] = {"staff_roles": [], "log_channel": None, "archive_channel": None}
    
    TICKET_CONFIG_DATA[guild_id]["log_channel"] = channel.id
    save_ticket_config(TICKET_CONFIG_DATA)
    
    embed = discord.Embed(
        title="✅ Logs Channel Set",
        description=f"Ticket logs will be sent to {channel.mention}",
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@ticket_group.command(name="archive_set", description="Set ticket archive channel")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_archive_set(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set the channel where closed tickets will be archived"""
    guild_id = str(interaction.guild_id)
    
    if guild_id not in TICKET_CONFIG_DATA:
        TICKET_CONFIG_DATA[guild_id] = {"staff_roles": [], "log_channel": None, "archive_channel": None}
    
    TICKET_CONFIG_DATA[guild_id]["archive_channel"] = channel.id
    save_ticket_config(TICKET_CONFIG_DATA)
    
    embed = discord.Embed(
        title="✅ Archive Channel Set",
        description=f"Closed tickets will be archived to {channel.mention}",
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── HELPER FUNCTIONS ────────────────────────────────
def is_staff(member, guild_id):
    """Check if member has staff role"""
    guild_id = str(guild_id)
    if guild_id not in TICKET_CONFIG_DATA:
        return False
    
    staff_roles = TICKET_CONFIG_DATA[guild_id]["staff_roles"]
    return any(role.id in staff_roles for role in member.roles)

async def send_log(guild, log_type, embed):
    """Send a log to the logs channel"""
    guild_id = str(guild.id)
    if guild_id not in TICKET_CONFIG_DATA:
        return
    
    log_channel_id = TICKET_CONFIG_DATA[guild_id].get("log_channel")
    if not log_channel_id:
        return
    
    try:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(embed=embed)
    except:
        pass

async def save_messages_to_pdf(channel, ticket_id, user_name, category):
    """Save ticket messages to PDF"""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        
        messages = []
        async for message in channel.history(limit=None, oldest_first=True):
            messages.append({
                "author": str(message.author),
                "timestamp": str(message.created_at),
                "content": message.content
            })
        
        filename = f"ticket_{ticket_id}_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.pdf"
        doc = SimpleDocTemplate(filename, pagesize=letter)
        story = []
        styles = getSampleStyleSheet()
        
        # Title
        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=20)
        story.append(Paragraph(f"Ticket Report: {ticket_id}", title_style))
        story.append(Paragraph(f"<b>User:</b> {user_name}", styles['Normal']))
        story.append(Paragraph(f"<b>Category:</b> {category}", styles['Normal']))
        story.append(Paragraph(f"<b>Date:</b> {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
        story.append(Spacer(1, 20))
        
        # Messages
        for msg in messages:
            story.append(Paragraph(f"<b>[{msg['timestamp']}] {msg['author']}:</b>", styles['Normal']))
            story.append(Paragraph(msg['content'], styles['Normal']))
            story.append(Spacer(1, 10))
        
        doc.build(story)
        return filename
    except ImportError:
        # Fallback to txt if reportlab not installed
        return await save_messages_to_txt(channel, ticket_id)

async def save_messages_to_txt(channel, ticket_id):
    """Fallback: Save messages to txt file"""
    messages = []
    async for message in channel.history(limit=None, oldest_first=True):
        messages.append({
            "author": str(message.author),
            "timestamp": str(message.created_at),
            "content": message.content
        })
    
    filename = f"ticket_{ticket_id}_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(f"[{msg['timestamp']}] {msg['author']}: {msg['content']}\n")
    
    return filename

# ── TICKET CREATION ─────────────────────────────────
@ticket_group.command(name="create", description="Create a support ticket")
async def ticket_create(interaction: discord.Interaction, category: str, description: str):
    """Create a new support ticket"""
    categories = ["bug", "suggestion", "appeal", "report", "general"]
    if category.lower() not in categories:
        return await interaction.response.send_message(
            f"❌ Invalid category. Choose: {', '.join(categories)}", ephemeral=True)

    user_tickets = [t for t in TICKETS.values() if t["user_id"] == interaction.user.id and t["status"] == "open"]
    if len(user_tickets) >= 3:
        return await interaction.response.send_message(
            "❌ You have too many open tickets. Close one first.", ephemeral=True)

    ticket_id = f"ticket-{len(TICKETS) + 1}"
    
    guild_id = str(interaction.guild_id)
    staff_roles = []
    if guild_id in TICKET_CONFIG_DATA:
        staff_roles = [interaction.guild.get_role(rid) for rid in TICKET_CONFIG_DATA[guild_id]["staff_roles"]]
        staff_roles = [r for r in staff_roles if r]
    
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    
    for role in staff_roles:
        overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    
    ticket_channel = await interaction.guild.create_text_channel(
        name=ticket_id,
        topic=f"Support Ticket | {interaction.user} | {category.upper()}",
        overwrites=overwrites
    )

    embed = discord.Embed(
        title="🎫 Support Ticket Created",
        description=description,
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name="Ticket ID", value=f"`{ticket_id}`", inline=True)
    embed.add_field(name="Category", value=category.upper(), inline=True)
    embed.add_field(name="Priority", value="🟡 MEDIUM", inline=True)
    embed.add_field(name="Status", value="🟢 OPEN", inline=True)
    embed.add_field(name="User", value=interaction.user.mention, inline=True)
    embed.add_field(name="Assigned To", value="Unassigned", inline=True)
    embed.set_footer(text="Use /ticket close to close this ticket")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    await ticket_channel.send(embed=embed)

    TICKETS[ticket_id] = {
        "user_id": interaction.user.id,
        "channel_id": ticket_channel.id,
        "category": category.lower(),
        "status": "open",
        "priority": "medium",
        "claimed_by": None,
        "assigned_to": None,
        "created_at": str(datetime.datetime.utcnow()),
        "created_by": str(interaction.user),
        "closed_by": None,
        "closed_at": None,
        "description": description
    }
    save_tickets(TICKETS)

    log_embed = discord.Embed(
        title="📝 Ticket Created",
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow()
    )
    log_embed.add_field(name="Ticket ID", value=f"`{ticket_id}`", inline=True)
    log_embed.add_field(name="User", value=f"{interaction.user} ({interaction.user.id})", inline=True)
    log_embed.add_field(name="Category", value=category.upper(), inline=True)
    log_embed.add_field(name="Description", value=description[:200], inline=False)
    log_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    
    await send_log(interaction.guild, "create", log_embed)

    await interaction.response.send_message(
        f"✅ Ticket created! {ticket_channel.mention}", ephemeral=True)

# ── TICKET CLAIM ────────────────────────────────────
@ticket_group.command(name="claim", description="Claim a ticket")
async def ticket_claim(interaction: discord.Interaction):
    """Claim a ticket to work on it"""
    if not is_staff(interaction.user, interaction.guild_id) and not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "❌ You don't have permission to claim tickets.", ephemeral=True)

    ticket_id = None
    for tid, data in TICKETS.items():
        if data["channel_id"] == interaction.channel_id:
            ticket_id = tid
            break

    if not ticket_id:
        return await interaction.response.send_message(
            "❌ This is not a ticket channel.", ephemeral=True)

    ticket = TICKETS[ticket_id]
    
    if ticket["claimed_by"]:
        return await interaction.response.send_message(
            f"❌ This ticket is already claimed by {ticket['claimed_by']}", ephemeral=True)

    TICKETS[ticket_id]["claimed_by"] = str(interaction.user)
    save_tickets(TICKETS)

    embed = discord.Embed(
        title="✅ Ticket Claimed",
        description=f"{interaction.user.mention} is now working on this ticket",
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed)

# ── TICKET PRIORITY ─────────────────────────────────
@ticket_group.command(name="priority", description="Set ticket priority")
async def ticket_priority(interaction: discord.Interaction, level: str):
    """Set ticket priority: low, medium, high"""
    if not is_staff(interaction.user, interaction.guild_id) and not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "❌ You don't have permission to change priority.", ephemeral=True)

    if level.lower() not in ["low", "medium", "high"]:
        return await interaction.response.send_message(
            "❌ Priority must be: low, medium, or high", ephemeral=True)

    ticket_id = None
    for tid, data in TICKETS.items():
        if data["channel_id"] == interaction.channel_id:
            ticket_id = tid
            break

    if not ticket_id:
        return await interaction.response.send_message(
            "❌ This is not a ticket channel.", ephemeral=True)

    priority_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}
    TICKETS[ticket_id]["priority"] = level.lower()
    save_tickets(TICKETS)

    embed = discord.Embed(
        title="✅ Priority Updated",
        description=f"Priority set to {priority_emoji[level.lower()]} {level.upper()}",
        color=0x5865F2
    )
    await interaction.response.send_message(embed=embed)

# ── TICKET RENAME ───────────────────────────────────
@ticket_group.command(name="rename", description="Rename ticket description")
async def ticket_rename(interaction: discord.Interaction, new_description: str):
    """Update ticket description"""
    if not is_staff(interaction.user, interaction.guild_id) and not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "❌ You don't have permission to rename tickets.", ephemeral=True)

    ticket_id = None
    for tid, data in TICKETS.items():
        if data["channel_id"] == interaction.channel_id:
            ticket_id = tid
            break

    if not ticket_id:
        return await interaction.response.send_message(
            "❌ This is not a ticket channel.", ephemeral=True)

    TICKETS[ticket_id]["description"] = new_description
    save_tickets(TICKETS)

    embed = discord.Embed(
        title="✅ Ticket Description Updated",
        description=new_description,
        color=0x5865F2
    )
    await interaction.response.send_message(embed=embed)

# ── TICKET ASSIGN ───────────────────────────────────
@ticket_group.command(name="assign", description="Assign ticket to staff member")
async def ticket_assign(interaction: discord.Interaction, staff: discord.Member):
    """Assign ticket to a specific staff member"""
    if not is_staff(interaction.user, interaction.guild_id) and not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "❌ You don't have permission to assign tickets.", ephemeral=True)

    ticket_id = None
    for tid, data in TICKETS.items():
        if data["channel_id"] == interaction.channel_id:
            ticket_id = tid
            break

    if not ticket_id:
        return await interaction.response.send_message(
            "❌ This is not a ticket channel.", ephemeral=True)

    TICKETS[ticket_id]["assigned_to"] = str(staff)
    save_tickets(TICKETS)

    embed = discord.Embed(
        title="✅ Ticket Assigned",
        description=f"Assigned to {staff.mention}",
        color=0x5865F2
    )
    await interaction.response.send_message(embed=embed)

    try:
        await staff.send(f"📝 You have been assigned to ticket: <#{interaction.channel_id}>")
    except:
        pass

# ── TICKET CLOSE ────────────────────────────────────
@ticket_group.command(name="close", description="Close a support ticket")
async def ticket_close(interaction: discord.Interaction):
    """Close the current ticket"""
    if not is_staff(interaction.user, interaction.guild_id) and not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "❌ You don't have permission to close tickets.", ephemeral=True)

    ticket_id = None
    for tid, data in TICKETS.items():
        if data["channel_id"] == interaction.channel_id:
            ticket_id = tid
            break

    if not ticket_id:
        return await interaction.response.send_message(
            "❌ This is not a ticket channel.", ephemeral=True)

    ticket = TICKETS[ticket_id]
    user_id = ticket["user_id"]

    # Save messages to PDF
    try:
        user = await bot.fetch_user(user_id)
        user_name = str(user)
    except:
        user_name = f"User {user_id}"
    
    try:
        filename = await save_messages_to_pdf(interaction.channel, ticket_id, user_name, ticket["category"])
    except:
        filename = None

    embed = discord.Embed(
        title="🎫 Ticket Closed",
        description="This ticket has been closed by staff.",
        color=0xff4444,
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name="Ticket ID", value=f"`{ticket_id}`", inline=True)
    embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
    embed.set_footer(text="Archiving ticket...")

    await interaction.response.send_message(embed=embed)

    TICKETS[ticket_id]["status"] = "closed"
    TICKETS[ticket_id]["closed_by"] = str(interaction.user)
    TICKETS[ticket_id]["closed_at"] = str(datetime.datetime.utcnow())
    save_tickets(TICKETS)

    # Archive ticket
    guild_id = str(interaction.guild_id)
    archive_channel_id = TICKET_CONFIG_DATA.get(guild_id, {}).get("archive_channel")
    
    if archive_channel_id:
        try:
            archive_channel = interaction.guild.get_channel(archive_channel_id)
            if archive_channel:
                archive_embed = discord.Embed(
                    title=f"📦 Archived Ticket: {ticket_id}",
                    color=0x808080,
                    timestamp=datetime.datetime.utcnow()
                )
                archive_embed.add_field(name="User", value=user_name, inline=True)
                archive_embed.add_field(name="Category", value=ticket["category"].upper(), inline=True)
                archive_embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
                archive_embed.add_field(name="Description", value=ticket["description"][:300], inline=False)
                
                if filename:
                    archive_embed.add_field(name="Log File", value=f"`{filename}`", inline=False)
                
                await archive_channel.send(embed=archive_embed)
                
                if filename:
                    try:
                        with open(filename, "rb") as f:
                            await archive_channel.send(file=discord.File(f, filename))
                    except:
                        pass
        except:
            pass

    # Send log
    log_embed = discord.Embed(
        title="📝 Ticket Closed",
        color=0xff4444,
        timestamp=datetime.datetime.utcnow()
    )
    log_embed.add_field(name="Ticket ID", value=f"`{ticket_id}`", inline=True)
    log_embed.add_field(name="User", value=user_name, inline=True)
    log_embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Category", value=ticket["category"].upper(), inline=True)
    if ticket["assigned_to"]:
        log_embed.add_field(name="Assigned To", value=ticket["assigned_to"], inline=True)
    if filename:
        log_embed.add_field(name="Log File", value=f"`{filename}`", inline=False)
    
    await send_log(interaction.guild, "close", log_embed)

    await asyncio.sleep(10)
    await interaction.channel.delete()

    try:
        user = await bot.fetch_user(user_id)
        dm_embed = discord.Embed(
            title="🎫 Your Ticket Was Closed",
            description=f"Your support ticket `{ticket_id}` has been closed by {interaction.user}",
            color=0xff4444
        )
        dm_embed.add_field(name="Category", value=ticket["category"].upper())
        await user.send(embed=dm_embed)
    except:
        pass

# ── TICKET STATS ────────────────────────────────────
@ticket_group.command(name="stats", description="View staff ticket statistics")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_stats(interaction: discord.Interaction):
    """View staff statistics"""
    closed_tickets = [t for t in TICKETS.values() if t["status"] == "closed"]
    
    if not closed_tickets:
        embed = discord.Embed(
            title="📊 Ticket Statistics",
            description="No closed tickets yet.",
            color=0x5865F2
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    stats = {}
    for ticket in closed_tickets:
        closer = ticket["closed_by"]
        if closer not in stats:
            stats[closer] = {"count": 0, "avg_time": 0, "times": []}
        
        stats[closer]["count"] += 1
        
        created = datetime.datetime.fromisoformat(ticket["created_at"])
        closed = datetime.datetime.fromisoformat(ticket["closed_at"])
        duration = (closed - created).total_seconds() / 3600
        stats[closer]["times"].append(duration)

    # Calculate averages
    for staff, data in stats.items():
        data["avg_time"] = sum(data["times"]) / len(data["times"])

    # Sort by most tickets closed
    sorted_stats = sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True)

    embed = discord.Embed(
        title="📊 Staff Ticket Statistics",
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow()
    )

    for staff, data in sorted_stats:
        embed.add_field(
            name=f"{staff}",
            value=f"**Closed:** {data['count']} tickets\n**Avg Time:** {data['avg_time']:.1f}h",
            inline=False
        )

    embed.set_footer(text=f"Total Closed Tickets: {len(closed_tickets)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── TICKET BULK CLOSE ───────────────────────────────
@ticket_group.command(name="bulkclose", description="Auto-close inactive tickets")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_bulk_close(interaction: discord.Interaction, days: int = 7):
    """Close all tickets inactive for X days"""
    await interaction.response.defer(ephemeral=True)
    
    now = datetime.datetime.utcnow()
    closed_count = 0
    
    for ticket_id, ticket in TICKETS.items():
        if ticket["status"] == "open":
            created = datetime.datetime.fromisoformat(ticket["created_at"])
            age_days = (now - created).days
            
            if age_days >= days:
                channel = interaction.guild.get_channel(ticket["channel_id"])
                if channel:
                    try:
                        embed = discord.Embed(
                            title="🤖 Auto-Closed - Inactivity",
                            description=f"This ticket was closed automatically due to {days}+ days of inactivity.",
                            color=0xff4444
                        )
                        await channel.send(embed=embed)
                        
                        TICKETS[ticket_id]["status"] = "closed"
                        TICKETS[ticket_id]["closed_by"] = "System"
                        TICKETS[ticket_id]["closed_at"] = str(now)
                        closed_count += 1
                        
                        await asyncio.sleep(5)
                        await channel.delete()
                    except:
                        pass

    save_tickets(TICKETS)
    
    embed = discord.Embed(
        title="✅ Bulk Close Complete",
        description=f"Closed {closed_count} inactive tickets",
        color=0x00ff00
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── TICKET QUEUE ────────────────────────────────────
@ticket_group.command(name="queue", description="View open tickets queue")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_queue(interaction: discord.Interaction):
    """View all open tickets"""
    open_tickets = [t for t in TICKETS.items() if t[1]["status"] == "open"]

    if not open_tickets:
        embed = discord.Embed(
            title="📋 Ticket Queue",
            description="No open tickets!",
            color=0x00ff00
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # Sort by priority
    priority_order = {"high": 0, "medium": 1, "low": 2}
    open_tickets.sort(key=lambda x: priority_order.get(x[1]["priority"], 3))

    embed = discord.Embed(
        title="📋 Open Tickets Queue",
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow()
    )

    priority_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}

    for ticket_id, data in open_tickets:
        try:
            user = await bot.fetch_user(data["user_id"])
            user_name = str(user)
        except:
            user_name = f"User {data['user_id']}"
        
        created = datetime.datetime.fromisoformat(data["created_at"])
        age = (datetime.datetime.utcnow() - created).seconds // 60

        assigned_text = f"Assigned to {data['assigned_to']}" if data['assigned_to'] else "Unassigned"

        embed.add_field(
            name=f"{priority_emoji[data['priority']]} {ticket_id}",
            value=f"**User:** {user_name}\n**Category:** {data['category'].upper()}\n**Age:** {age}m\n**{assigned_text}**",
            inline=False
        )

    embed.set_footer(text=f"Total: {len(open_tickets)} open tickets")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── TICKET LIST ─────────────────────────────────────
@ticket_group.command(name="list", description="List your tickets")
async def ticket_list(interaction: discord.Interaction):
    """View your tickets"""
    user_tickets = [(tid, t) for tid, t in TICKETS.items() if t["user_id"] == interaction.user.id]

    if not user_tickets:
        embed = discord.Embed(
            title="🎫 Your Tickets",
            description="You have no tickets.",
            color=0x5865F2
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    embed = discord.Embed(
        title="🎫 Your Support Tickets",
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow()
    )

    for ticket_id, data in user_tickets:
        status_emoji = "🟢" if data["status"] == "open" else "🔴"
        created = datetime.datetime.fromisoformat(data["created_at"])
        age = (datetime.datetime.utcnow() - created).seconds // 60

        embed.add_field(
            name=f"{status_emoji} {ticket_id}",
            value=f"**Category:** {data['category'].upper()}\n**Status:** {data['status'].upper()}\n**Age:** {age}m",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── TICKET INFO ─────────────────────────────────────
@ticket_group.command(name="info", description="Get ticket info")
async def ticket_info(interaction: discord.Interaction):
    """Get info about current ticket"""
    if not is_staff(interaction.user, interaction.guild_id) and not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message(
            "❌ You don't have permission to view ticket info.", ephemeral=True)

    ticket_id = None
    for tid, data in TICKETS.items():
        if data["channel_id"] == interaction.channel_id:
            ticket_id = tid
            break

    if not ticket_id:
        return await interaction.response.send_message(
            "❌ This is not a ticket channel.", ephemeral=True)

    ticket = TICKETS[ticket_id]
    try:
        user = await bot.fetch_user(ticket["user_id"])
        user_name = str(user)
    except:
        user_name = f"User {ticket['user_id']}"
    
    created = datetime.datetime.fromisoformat(ticket["created_at"])
    age = (datetime.datetime.utcnow() - created).seconds // 60

    priority_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}

    embed = discord.Embed(
        title="📊 Ticket Information",
        color=0x5865F2,
        timestamp=datetime.datetime.utcnow()
    )
    embed.add_field(name="Ticket ID", value=f"`{ticket_id}`", inline=True)
    embed.add_field(name="User", value=user_name, inline=True)
    embed.add_field(name="Category", value=ticket["category"].upper(), inline=True)
    embed.add_field(name="Priority", value=f"{priority_emoji[ticket['priority']]} {ticket['priority'].upper()}", inline=True)
    embed.add_field(name="Status", value=ticket["status"].upper(), inline=True)
    embed.add_field(name="Age", value=f"{age} minutes", inline=True)
    if ticket["assigned_to"]:
        embed.add_field(name="Assigned To", value=ticket["assigned_to"], inline=True)
    if ticket["claimed_by"]:
        embed.add_field(name="Claimed By", value=ticket["claimed_by"], inline=True)
    embed.add_field(name="Description", value=ticket["description"][:300], inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── TICKET SETUP ────────────────────────────────────

# In-memory store for setup sessions: {message_id: {config_data}}
SETUP_SESSIONS = {}

class CategorySelect(discord.ui.Select):
    """Dropdown to pick category type during setup"""
    def __init__(self):
        options = [
            discord.SelectOption(label="General Support",    value="general",    emoji="💬", description="General help and questions"),
            discord.SelectOption(label="Bug Report",         value="bug",        emoji="🐛", description="Report a bug or issue"),
            discord.SelectOption(label="Store Support",      value="store",      emoji="🛒", description="Purchase or payment issues"),
            discord.SelectOption(label="Staff Report",       value="report",     emoji="📋", description="Report a staff member"),
            discord.SelectOption(label="Ban Appeal",         value="appeal",     emoji="⚖️", description="Appeal a ban or punishment"),
            discord.SelectOption(label="Suggestion",         value="suggestion", emoji="💡", description="Submit a suggestion"),
            discord.SelectOption(label="Partnership",        value="partner",    emoji="🤝", description="Partnership requests"),
            discord.SelectOption(label="Custom",             value="custom",     emoji="⚙️", description="Custom category"),
        ]
        super().__init__(placeholder="Select a category type...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        session = SETUP_SESSIONS.get(interaction.message.id)
        if not session:
            return await interaction.response.send_message("❌ Session expired. Run `/ticket setup` again.", ephemeral=True)
        
        session["category"] = self.values[0]
        label_map = {o.value: o.label for o in self.options}
        session["category_label"] = label_map.get(self.values[0], self.values[0])
        
        # Update the embed to show selected category
        embed = build_setup_embed(session)
        await interaction.response.edit_message(embed=embed, view=SetupView(session, interaction.message.id))


class SetupView(discord.ui.View):
    """The main setup panel with all config buttons"""
    def __init__(self, session: dict, message_id: int):
        super().__init__(timeout=300)
        self.session = session
        self.message_id = message_id
        # Add dropdown first
        self.add_item(CategorySelect())

    @discord.ui.button(label="Title", emoji="📝", style=discord.ButtonStyle.primary, row=2)
    async def set_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetupModal("title", "Set Category Title", "Enter the title for this ticket category", self.session, self.message_id))

    @discord.ui.button(label="Description", emoji="📄", style=discord.ButtonStyle.primary, row=2)
    async def set_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetupModal("description", "Set Description", "Enter the embed description", self.session, self.message_id, long=True))

    @discord.ui.button(label="Color", emoji="🎨", style=discord.ButtonStyle.primary, row=2)
    async def set_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetupModal("color", "Set Color", "Enter hex color (e.g. #5865F2)", self.session, self.message_id))

    @discord.ui.button(label="Image", emoji="🖼️", style=discord.ButtonStyle.secondary, row=3)
    async def set_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetupModal("image", "Set Image URL", "Enter the image URL", self.session, self.message_id))

    @discord.ui.button(label="Thumbnail", emoji="🖼️", style=discord.ButtonStyle.secondary, row=3)
    async def set_thumbnail(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetupModal("thumbnail", "Set Thumbnail URL", "Enter the thumbnail URL", self.session, self.message_id))

    @discord.ui.button(label="JSON", emoji="📋", style=discord.ButtonStyle.secondary, row=3)
    async def set_json(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetupModal("json_config", "Custom JSON Config", "Paste JSON config (optional advanced settings)", self.session, self.message_id, long=True))

    @discord.ui.button(label="Save & Set Category", emoji="✅", style=discord.ButtonStyle.success, row=4)
    async def save_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = SETUP_SESSIONS.get(self.message_id)
        if not session:
            return await interaction.response.send_message("❌ Session expired.", ephemeral=True)

        category_type = session.get("category")
        if not category_type:
            return await interaction.response.send_message("❌ Please select a category type first!", ephemeral=True)

        channel_id = session.get("channel_id")
        guild_id = str(interaction.guild_id)

        # Save to ticket config
        if guild_id not in TICKET_CONFIG_DATA:
            TICKET_CONFIG_DATA[guild_id] = {"staff_roles": [], "log_channel": None, "archive_channel": None}

        if "categories" not in TICKET_CONFIG_DATA[guild_id]:
            TICKET_CONFIG_DATA[guild_id]["categories"] = {}

        TICKET_CONFIG_DATA[guild_id]["categories"][category_type] = {
            "label":       session.get("category_label", category_type.title()),
            "title":       session.get("title", f"{category_type.title()} Support"),
            "description": session.get("description", "Please describe your issue."),
            "color":       session.get("color", "#5865F2"),
            "image":       session.get("image"),
            "thumbnail":   session.get("thumbnail"),
            "channel_id":  channel_id,
        }
        save_ticket_config(TICKET_CONFIG_DATA)

        # Send the ticket panel embed to the target channel
        if channel_id:
            target_channel = interaction.guild.get_channel(channel_id)
            if target_channel:
                try:
                    color_int = int(session.get("color", "#5865F2").lstrip("#"), 16)
                except:
                    color_int = 0x5865F2

                panel_embed = discord.Embed(
                    title=session.get("title", f"{category_type.title()} Support"),
                    description=session.get("description", "Click the button below to open a ticket."),
                    color=color_int
                )
                if session.get("image"):
                    panel_embed.set_image(url=session["image"])
                if session.get("thumbnail"):
                    panel_embed.set_thumbnail(url=session["thumbnail"])
                panel_embed.set_footer(text="Click the button below to open a ticket")

                open_view = OpenTicketView(category_type)
                await target_channel.send(embed=panel_embed, view=open_view)

        # Done — edit the setup message
        done_embed = discord.Embed(
            title="✅ Category Saved!",
            description=f"Category **{session.get('category_label', category_type)}** has been configured and the ticket panel was sent to <#{channel_id}>.",
            color=0x00ff00
        )
        done_embed.add_field(name="Category Type", value=f"`{category_type}`", inline=True)
        done_embed.add_field(name="Title", value=session.get("title", "—"), inline=True)

        SETUP_SESSIONS.pop(self.message_id, None)
        await interaction.response.edit_message(embed=done_embed, view=None)

    @discord.ui.button(label="Exit", emoji="❌", style=discord.ButtonStyle.danger, row=4)
    async def exit_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        SETUP_SESSIONS.pop(self.message_id, None)
        embed = discord.Embed(title="❌ Setup Cancelled", description="Ticket setup was cancelled.", color=0xff4444)
        await interaction.response.edit_message(embed=embed, view=None)


class SetupModal(discord.ui.Modal):
    """Generic modal for text input during setup"""
    def __init__(self, field: str, title: str, label: str, session: dict, message_id: int, long: bool = False):
        super().__init__(title=title)
        self.field = field
        self.session = session
        self.message_id = message_id

        self.input = discord.ui.TextInput(
            label=label,
            style=discord.TextStyle.long if long else discord.TextStyle.short,
            required=False,
            default=session.get(field, ""),
            max_length=1000
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        session = SETUP_SESSIONS.get(self.message_id)
        if not session:
            return await interaction.response.send_message("❌ Session expired.", ephemeral=True)

        session[self.field] = self.input.value.strip()
        embed = build_setup_embed(session)
        await interaction.response.edit_message(embed=embed, view=SetupView(session, self.message_id))


class OpenTicketView(discord.ui.View):
    """Persistent 'Open Ticket' button on the panel embed"""
    def __init__(self, category: str):
        super().__init__(timeout=None)
        self.category = category

    @discord.ui.button(label="Open Ticket", emoji="🎫", style=discord.ButtonStyle.success, custom_id="open_ticket_btn")
    async def open_ticket_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Find category config
        guild_id = str(interaction.guild_id)
        cats = TICKET_CONFIG_DATA.get(guild_id, {}).get("categories", {})
        # Try to match by channel
        matched_cat = None
        for cat_key, cat_data in cats.items():
            if cat_data.get("channel_id") == interaction.channel_id:
                matched_cat = cat_key
                break

        # Open a modal for description then create ticket
        await interaction.response.send_modal(OpenTicketModal(matched_cat or "general"))


class OpenTicketModal(discord.ui.Modal, title="Open a Ticket"):
    description = discord.ui.TextInput(
        label="Describe your issue",
        style=discord.TextStyle.long,
        placeholder="Please describe your issue in detail...",
        required=True,
        max_length=1000
    )

    def __init__(self, category: str):
        super().__init__()
        self.category = category

    async def on_submit(self, interaction: discord.Interaction):
        # Reuse ticket_create logic
        guild_id = str(interaction.guild_id)
        user = interaction.user
        description = self.description.value

        user_open = [t for t in TICKETS.values() if t["user_id"] == user.id and t["status"] == "open"]
        if len(user_open) >= 3:
            return await interaction.response.send_message("❌ You already have 3 open tickets. Close one first.", ephemeral=True)

        ticket_id = f"ticket-{len(TICKETS) + 1}"

        staff_roles = []
        if guild_id in TICKET_CONFIG_DATA:
            staff_roles = [interaction.guild.get_role(rid) for rid in TICKET_CONFIG_DATA[guild_id].get("staff_roles", [])]
            staff_roles = [r for r in staff_roles if r]

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        for role in staff_roles:
            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        ticket_channel = await interaction.guild.create_text_channel(
            name=ticket_id,
            topic=f"Ticket | {user} | {self.category.upper()}",
            overwrites=overwrites
        )

        try:
            color_int = int(TICKET_CONFIG_DATA.get(guild_id, {}).get("categories", {}).get(self.category, {}).get("color", "#5865F2").lstrip("#"), 16)
        except:
            color_int = 0x5865F2

        embed = discord.Embed(
            title="🎫 Support Ticket Created",
            description=description,
            color=color_int,
            timestamp=datetime.datetime.utcnow()
        )
        embed.add_field(name="Ticket ID",  value=f"`{ticket_id}`",    inline=True)
        embed.add_field(name="Category",   value=self.category.upper(), inline=True)
        embed.add_field(name="Status",     value="🟢 OPEN",            inline=True)
        embed.add_field(name="User",       value=user.mention,          inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="Use /ticket close to close this ticket")

        await ticket_channel.send(embed=embed)

        TICKETS[ticket_id] = {
            "user_id":    user.id,
            "channel_id": ticket_channel.id,
            "category":   self.category,
            "status":     "open",
            "priority":   "medium",
            "claimed_by": None,
            "assigned_to": None,
            "created_at": str(datetime.datetime.utcnow()),
            "created_by": str(user),
            "closed_by":  None,
            "closed_at":  None,
            "description": description
        }
        save_tickets(TICKETS)
        await send_log(interaction.guild, "create", embed)
        await interaction.response.send_message(f"✅ Ticket created! {ticket_channel.mention}", ephemeral=True)


def build_setup_embed(session: dict) -> discord.Embed:
    """Build a live preview embed showing current setup config"""
    try:
        color_int = int(session.get("color", "#5865F2").lstrip("#"), 16)
    except:
        color_int = 0x5865F2

    embed = discord.Embed(
        title="⚙️ Ticket Category Setup",
        description="Select a category type from the dropdown, then configure settings with the buttons below.",
        color=color_int
    )
    embed.add_field(name="📂 Category Type", value=f"`{session.get('category_label', 'Not selected')}`", inline=True)
    embed.add_field(name="📝 Title",         value=session.get("title", "—") or "—",                    inline=True)
    embed.add_field(name="🎨 Color",         value=session.get("color", "#5865F2"),                     inline=True)
    embed.add_field(name="📄 Description",   value=(session.get("description") or "—")[:200],           inline=False)
    embed.add_field(name="🖼️ Image URL",     value=session.get("image") or "—",                         inline=True)
    embed.add_field(name="🖼️ Thumbnail URL", value=session.get("thumbnail") or "—",                     inline=True)
    embed.set_footer(text="When done, click ✅ Save & Set Category")
    return embed


@ticket_group.command(name="setup", description="Set up a ticket category panel")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    """Interactive setup for a ticket category"""
    session = {
        "channel_id": channel.id,
        "category": None,
        "category_label": None,
        "title": None,
        "description": None,
        "color": "#5865F2",
        "image": None,
        "thumbnail": None,
    }

    embed = build_setup_embed(session)
    embed.description = (
        f"Setting up a ticket panel for {channel.mention}\n\n"
        "**Step 1:** Select a category type from the dropdown\n"
        "**Step 2:** Customize with the buttons (optional)\n"
        "**Step 3:** Click ✅ Save & Set Category"
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)
    msg = await interaction.original_response()
    SETUP_SESSIONS[msg.id] = session

    # Edit to attach the interactive view (ephemerals need followup for view)
    await interaction.edit_original_response(embed=embed, view=SetupView(session, msg.id))


bot.tree.add_command(ticket_group)

# ── FLASK API ROUTES ─────────────────────────────────

@app.route('/dashboard')
@app.route('/')
def serve_dashboard():
    return send_from_directory('.', 'dashboard.html')

@app.route('/api/stats')
def api_stats():
    open_tickets = sum(1 for t in TICKETS.values() if t.get('status') == 'open')
    return jsonify({
        'auto_replies':    len(DATA.get('auto_replies', {})),
        'custom_commands': len(DATA.get('custom_commands', {})),
        'members_tracked': len(DATA.get('xp', {})),
        'schedules':       len(DATA.get('schedules', [])),
        'open_tickets':    open_tickets,
    })

@app.route('/api/autoreplies', methods=['GET'])
def api_get_autoreplies():
    return jsonify([{'trigger': k, 'response': v} for k, v in DATA.get('auto_replies', {}).items()])

@app.route('/api/autoreplies', methods=['POST'])
def api_add_autoreply():
    body = request.json or {}
    trigger  = body.get('trigger', '').lower().strip()
    response = body.get('response', '').strip()
    if not trigger or not response:
        return jsonify({'error': 'trigger and response required'}), 400
    DATA.setdefault('auto_replies', {})[trigger] = response
    save_data(DATA)
    return jsonify({'ok': True})

@app.route('/api/autoreplies/<trigger>', methods=['DELETE'])
def api_del_autoreply(trigger):
    DATA.get('auto_replies', {}).pop(trigger, None)
    save_data(DATA)
    return jsonify({'ok': True})

@app.route('/api/commands', methods=['GET'])
def api_get_commands():
    return jsonify([{'name': k, 'response': v} for k, v in DATA.get('custom_commands', {}).items()])

@app.route('/api/commands', methods=['POST'])
def api_add_command():
    body = request.json or {}
    name     = body.get('name', '').lower().strip()
    response = body.get('response', '').strip()
    if not name or not response:
        return jsonify({'error': 'name and response required'}), 400
    DATA.setdefault('custom_commands', {})[name] = response
    save_data(DATA)
    return jsonify({'ok': True})

@app.route('/api/commands/<name>', methods=['DELETE'])
def api_del_command(name):
    DATA.get('custom_commands', {}).pop(name, None)
    save_data(DATA)
    return jsonify({'ok': True})

@app.route('/api/welcome/<guild_id>', methods=['GET'])
def api_get_welcome(guild_id):
    cfg = DATA.get('welcome', {}).get(guild_id, {})
    return jsonify(cfg)

@app.route('/api/welcome/<guild_id>', methods=['POST'])
def api_set_welcome(guild_id):
    body = request.json or {}
    channel_id = body.get('channel_id', '').strip()
    message    = body.get('message', '').strip()
    if not channel_id or not message:
        return jsonify({'error': 'channel_id and message required'}), 400
    DATA.setdefault('welcome', {})[guild_id] = {'channel_id': int(channel_id), 'message': message}
    save_data(DATA)
    return jsonify({'ok': True})

@app.route('/api/leaderboard')
def api_leaderboard():
    sorted_xp = sorted(DATA.get('xp', {}).items(), key=lambda x: x[1], reverse=True)[:20]
    return jsonify([{'user_id': uid, 'xp': xp, 'level': xp // 100} for uid, xp in sorted_xp])

# ── TICKET API ROUTES ────────────────────────────────

@app.route('/api/ticket/categories/<guild_id>', methods=['GET'])
def api_get_ticket_categories(guild_id):
    cats = TICKET_CONFIG_DATA.get(guild_id, {}).get('categories', {})
    return jsonify(cats)

@app.route('/api/ticket/category', methods=['POST'])
def api_save_ticket_category():
    body       = request.json or {}
    guild_id   = body.get('guild_id', '').strip()
    category   = body.get('category', '').strip()
    channel_id = body.get('channel_id', '').strip()
    title      = body.get('title', '').strip()
    description = body.get('description', '').strip()
    color      = body.get('color', '#5865F2').strip()
    image      = body.get('image', '').strip() or None
    thumbnail  = body.get('thumbnail', '').strip() or None

    if not guild_id or not category or not channel_id:
        return jsonify({'error': 'guild_id, category, and channel_id required'}), 400

    if guild_id not in TICKET_CONFIG_DATA:
        TICKET_CONFIG_DATA[guild_id] = {'staff_roles': [], 'log_channel': None, 'archive_channel': None}
    TICKET_CONFIG_DATA[guild_id].setdefault('categories', {})[category] = {
        'label':       title or category.title(),
        'title':       title or f'{category.title()} Support',
        'description': description or 'Click the button below to open a ticket.',
        'color':       color,
        'image':       image,
        'thumbnail':   thumbnail,
        'channel_id':  int(channel_id),
    }
    save_ticket_config(TICKET_CONFIG_DATA)
    return jsonify({'ok': True})

@app.route('/api/ticket/category/<guild_id>/<category>', methods=['DELETE'])
def api_del_ticket_category(guild_id, category):
    cats = TICKET_CONFIG_DATA.get(guild_id, {}).get('categories', {})
    cats.pop(category, None)
    save_ticket_config(TICKET_CONFIG_DATA)
    return jsonify({'ok': True})

@app.route('/api/ticket/staff', methods=['POST'])
def api_add_ticket_staff():
    body     = request.json or {}
    guild_id = body.get('guild_id', '').strip()
    role_id  = body.get('role_id', '').strip()
    if not guild_id or not role_id:
        return jsonify({'error': 'guild_id and role_id required'}), 400
    if guild_id not in TICKET_CONFIG_DATA:
        TICKET_CONFIG_DATA[guild_id] = {'staff_roles': [], 'log_channel': None, 'archive_channel': None}
    role_int = int(role_id)
    if role_int not in TICKET_CONFIG_DATA[guild_id]['staff_roles']:
        TICKET_CONFIG_DATA[guild_id]['staff_roles'].append(role_int)
    save_ticket_config(TICKET_CONFIG_DATA)
    return jsonify({'ok': True})

@app.route('/api/ticket/logchannel', methods=['POST'])
def api_set_log_channel():
    body       = request.json or {}
    guild_id   = body.get('guild_id', '').strip()
    channel_id = body.get('channel_id', '').strip()
    if not guild_id or not channel_id:
        return jsonify({'error': 'guild_id and channel_id required'}), 400
    if guild_id not in TICKET_CONFIG_DATA:
        TICKET_CONFIG_DATA[guild_id] = {'staff_roles': [], 'log_channel': None, 'archive_channel': None}
    TICKET_CONFIG_DATA[guild_id]['log_channel'] = int(channel_id)
    save_ticket_config(TICKET_CONFIG_DATA)
    return jsonify({'ok': True})

@app.route('/api/ticket/archivechannel', methods=['POST'])
def api_set_archive_channel():
    body       = request.json or {}
    guild_id   = body.get('guild_id', '').strip()
    channel_id = body.get('channel_id', '').strip()
    if not guild_id or not channel_id:
        return jsonify({'error': 'guild_id and channel_id required'}), 400
    if guild_id not in TICKET_CONFIG_DATA:
        TICKET_CONFIG_DATA[guild_id] = {'staff_roles': [], 'log_channel': None, 'archive_channel': None}
    TICKET_CONFIG_DATA[guild_id]['archive_channel'] = int(channel_id)
    save_ticket_config(TICKET_CONFIG_DATA)
    return jsonify({'ok': True})

@app.route('/api/ticket/queue', methods=['GET'])
def api_ticket_queue():
    now = datetime.datetime.utcnow()
    open_tickets = []
    for tid, t in TICKETS.items():
        if t.get('status') == 'open':
            try:
                created = datetime.datetime.fromisoformat(t['created_at'])
                age_minutes = int((now - created).total_seconds() / 60)
            except Exception:
                age_minutes = 0
            open_tickets.append({
                'ticket_id':   tid,
                'category':    t.get('category', 'general'),
                'priority':    t.get('priority', 'medium'),
                'user_id':     t.get('user_id'),
                'assigned_to': t.get('assigned_to'),
                'age_minutes': age_minutes,
            })
    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    open_tickets.sort(key=lambda x: priority_order.get(x['priority'], 3))
    return jsonify(open_tickets)

@app.route('/api/ticket/close', methods=['POST'])
def api_ticket_close():
    body      = request.json or {}
    ticket_id = body.get('ticket_id', '').strip()
    if not ticket_id or ticket_id not in TICKETS:
        return jsonify({'error': 'Ticket not found'}), 404
    TICKETS[ticket_id]['status']    = 'closed'
    TICKETS[ticket_id]['closed_by'] = 'Dashboard'
    TICKETS[ticket_id]['closed_at'] = str(datetime.datetime.utcnow())
    save_tickets(TICKETS)
    return jsonify({'ok': True})

@app.route('/api/ticket/bulkclose', methods=['POST'])
def api_ticket_bulkclose():
    body     = request.json or {}
    days     = int(body.get('days', 7))
    now      = datetime.datetime.utcnow()
    count    = 0
    for tid, t in TICKETS.items():
        if t.get('status') == 'open':
            try:
                created = datetime.datetime.fromisoformat(t['created_at'])
                if (now - created).days >= days:
                    TICKETS[tid]['status']    = 'closed'
                    TICKETS[tid]['closed_by'] = 'Dashboard/Auto'
                    TICKETS[tid]['closed_at'] = str(now)
                    count += 1
            except Exception:
                pass
    if count:
        save_tickets(TICKETS)
    return jsonify({'ok': True, 'closed_count': count})

@app.route('/api/ticket/stats', methods=['GET'])
def api_ticket_stats():
    closed = [t for t in TICKETS.values() if t.get('status') == 'closed']
    stats  = {}
    for t in closed:
        closer = t.get('closed_by', 'Unknown') or 'Unknown'
        stats.setdefault(closer, {'count': 0, 'times': []})
        stats[closer]['count'] += 1
        try:
            created = datetime.datetime.fromisoformat(t['created_at'])
            closed_at = datetime.datetime.fromisoformat(t['closed_at'])
            stats[closer]['times'].append((closed_at - created).total_seconds() / 3600)
        except Exception:
            pass
    result = []
    for staff, data in sorted(stats.items(), key=lambda x: x[1]['count'], reverse=True):
        avg = sum(data['times']) / len(data['times']) if data['times'] else 0
        result.append({'staff': staff, 'count': data['count'], 'avg_time': avg})
    return jsonify(result)

# ── START FLASK IN BACKGROUND THREAD ─────────────────
def run_flask():
    port = int(os.environ.get('DASHBOARD_PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# ── RUN ──────────────────────────────────────────────
bot.run(DISCORD_TOKEN)
