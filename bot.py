import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import json
import os
import datetime
import random
import httpx
from pathlib import Path

# ── CONFIG ──────────────────────────────────────────
DISCORD_TOKEN  = os.environ.get("DISCORD_TOKEN")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY")
PREFIX         = "!"
LOG_FILE       = "data.json"

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

# ── AI HELPER ───────────────────────────────────────
async def ask_ai(channel_id, username, prompt, system=None):
    history = ai_history.setdefault(channel_id, [])
    history.append({"role": "user", "content": f"{username}: {prompt}"})
    if len(history) > 20:
        history.pop(0)
    sys_prompt = system or (
        "You are Callmest, a friendly and helpful Discord bot. "
        "Keep replies concise (under 2000 chars). Use Discord markdown when helpful."
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 600,
                    "system": sys_prompt,
                    "messages": history,
                },
            )
        resp = r.json()
        reply = resp["content"][0]["text"] if "content" in resp else str(resp)
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        return f"AI error: {e}"

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

    if gid:
        DATA["xp"].setdefault(uid, 0)
        old_level = DATA["xp"][uid] // 100
        DATA["xp"][uid] += random.randint(5, 15)
        new_level = DATA["xp"][uid] // 100
        if new_level > old_level:
            await message.channel.send(f"🎉 {message.author.mention} leveled up to **Level {new_level}**!")
        save_data(DATA)

    content_lower = message.content.lower()
    for trigger, response in DATA["auto_replies"].items():
        if trigger.lower() in content_lower:
            await message.channel.send(response)
            break

    if message.channel.id in DATA["ai_channels"]:
        async with message.channel.typing():
            reply = await ask_ai(message.channel.id, message.author.display_name, message.content)
            await message.channel.send(reply)
        return

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
    embed.add_field(name="🤖 AI", value="`!ai <question>` • `/ai-channel set/remove`", inline=False)
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
    DATA["warns"].setdefault(str(member.id), []).append({"reason": reason, "by": str(ctx.author), "at": str(datetime.datetime.utcnow())})
    save_data(DATA)
    await ctx.send(embed=mod_embed("Warn", member, reason, 0xffff00))

@bot.command()
async def warns(ctx, member: discord.Member = None):
    member = member or ctx.author
    w = DATA["warns"].get(str(member.id), [])
    embed = discord.Embed(title=f"⚠️ Warns for {member.display_name}", color=0xffff00)
    for i, warn in enumerate(w):
        embed.add_field(name=f"#{i+1}", value=f"{warn['reason']} — by {warn['by']}", inline=False)
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

# ── AI COMMAND ──────────────────────────────────────
@bot.command(name="ai")
async def ai_cmd(ctx, *, prompt: str):
    async with ctx.typing():
        reply = await ask_ai(ctx.channel.id, ctx.author.display_name, prompt)
    await ctx.send(reply)

ai_group = app_commands.Group(name="ai-channel", description="AI channel mode")

@ai_group.command(name="set", description="Make this channel AI-only")
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
    await interaction.response.send_message("\n".join(f"• `{t}` → {r}" for t, r in DATA["auto_replies"].items()), ephemeral=True)

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
    await interaction.response.send_message("\n".join(f"• `!{n}` → {r[:60]}" for n, r in DATA["custom_commands"].items()), ephemeral=True)

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
    question, options = parts[0], parts[1:] if len(parts) > 1 else ["✅ Yes", "❌ No"]
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
    embed = discord.Embed(title=f"⭐ {member.display_name}", description=f"Level **{level}** | XP: **{xp}**", color=0x5865F2)
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
    result = random.choice(['Heads 🪙', 'Tails 🪙'])
    await ctx.send(f"**{result}**")

# ── RUN ──────────────────────────────────────────────
try:
    bot.run(DISCORD_TOKEN)
except Exception as e:
    print(f"ERROR: {e}", flush=True)
    import traceback
    traceback.print_exc()
