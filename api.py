"""
CallMest Dashboard API
======================
Run alongside your bot. Both share data.json.

Environment variables needed (Railway):
  DISCORD_TOKEN        - your bot token
  DISCORD_CLIENT_ID    - OAuth2 app client ID
  DISCORD_CLIENT_SECRET- OAuth2 app client secret
  DISCORD_REDIRECT_URI - e.g. https://your-railway-app.up.railway.app/callback
  SECRET_KEY           - any random string for Flask sessions
  BOT_OWNER_ID         - your Discord user ID (optional, restricts dashboard access)
"""

import os, json, hmac, hashlib, secrets
from pathlib import Path
from functools import wraps
from flask import (
    Flask, session, redirect, request,
    jsonify, url_for, send_from_directory
)
import requests as req

# ── Config ──────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=".")

app.secret_key        = os.environ.get("SECRET_KEY", secrets.token_hex(32))
CLIENT_ID             = os.environ.get("DISCORD_CLIENT_ID", "")
CLIENT_SECRET         = os.environ.get("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI          = os.environ.get("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
BOT_OWNER_ID          = os.environ.get("BOT_OWNER_ID", "")   # optional guard
LOG_FILE              = "data.json"

DISCORD_API           = "https://discord.com/api/v10"
OAUTH_URL             = (
    f"https://discord.com/oauth2/authorize"
    f"?client_id={CLIENT_ID}"
    f"&redirect_uri={REDIRECT_URI}"
    f"&response_type=code"
    f"&scope=identify+guilds"
)

# ── Data helpers ─────────────────────────────────────────────────────────────
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

# ── Auth helpers ─────────────────────────────────────────────────────────────
def exchange_code(code):
    """Exchange OAuth code for access token."""
    r = req.post(f"{DISCORD_API}/oauth2/token", data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    r.raise_for_status()
    return r.json()

def get_discord_user(token):
    r = req.get(f"{DISCORD_API}/users/@me",
                headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()

def get_discord_guilds(token):
    r = req.get(f"{DISCORD_API}/users/@me/guilds",
                headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Not logged in"}), 401
        return f(*args, **kwargs)
    return decorated

# ── OAuth Routes ─────────────────────────────────────────────────────────────
@app.route("/login")
def login():
    return redirect(OAUTH_URL)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Missing code", 400
    try:
        token_data = exchange_code(code)
        user       = get_discord_user(token_data["access_token"])
        guilds     = get_discord_guilds(token_data["access_token"])
        # Filter to guilds where user has Manage Guild permission
        admin_guilds = [
            g for g in guilds
            if (int(g.get("permissions", 0)) & 0x20) == 0x20   # MANAGE_GUILD
        ]
        session["user"]         = user
        session["access_token"] = token_data["access_token"]
        session["guilds"]       = admin_guilds
    except Exception as e:
        return f"OAuth error: {e}", 500
    return redirect("/dashboard.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ── Auth Status ───────────────────────────────────────────────────────────────
@app.route("/api/me")
def api_me():
    if "user" not in session:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "user":      session["user"],
        "guilds":    session.get("guilds", []),
    })

# ── Stats ─────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
@login_required
def api_stats():
    data = load_data()
    return jsonify({
        "auto_replies":    len(data["auto_replies"]),
        "custom_commands": len(data["custom_commands"]),
        "members_tracked": len(data["xp"]),
        "schedules":       len(data["schedules"]),
    })

# ── Auto-Replies ──────────────────────────────────────────────────────────────
@app.route("/api/autoreplies", methods=["GET"])
@login_required
def get_autoreplies():
    data = load_data()
    items = [{"trigger": k, "response": v} for k, v in data["auto_replies"].items()]
    return jsonify(items)

@app.route("/api/autoreplies", methods=["POST"])
@login_required
def add_autoreply():
    body = request.json
    trigger  = (body.get("trigger") or "").strip().lower()
    response = (body.get("response") or "").strip()
    if not trigger or not response:
        return jsonify({"error": "Missing trigger or response"}), 400
    data = load_data()
    data["auto_replies"][trigger] = response
    save_data(data)
    return jsonify({"ok": True})

@app.route("/api/autoreplies/<trigger>", methods=["DELETE"])
@login_required
def delete_autoreply(trigger):
    data = load_data()
    data["auto_replies"].pop(trigger.lower(), None)
    save_data(data)
    return jsonify({"ok": True})

# ── Custom Commands ───────────────────────────────────────────────────────────
@app.route("/api/commands", methods=["GET"])
@login_required
def get_commands():
    data = load_data()
    items = [{"name": k, "response": v} for k, v in data["custom_commands"].items()]
    return jsonify(items)

@app.route("/api/commands", methods=["POST"])
@login_required
def add_command():
    body = request.json
    name     = (body.get("name") or "").strip().lower()
    response = (body.get("response") or "").strip()
    if not name or not response:
        return jsonify({"error": "Missing name or response"}), 400
    data = load_data()
    data["custom_commands"][name] = response
    save_data(data)
    return jsonify({"ok": True})

@app.route("/api/commands/<name>", methods=["DELETE"])
@login_required
def delete_command(name):
    data = load_data()
    data["custom_commands"].pop(name.lower(), None)
    save_data(data)
    return jsonify({"ok": True})

# ── Welcome ───────────────────────────────────────────────────────────────────
@app.route("/api/welcome/<guild_id>", methods=["GET"])
@login_required
def get_welcome(guild_id):
    data = load_data()
    return jsonify(data["welcome"].get(guild_id, {}))

@app.route("/api/welcome/<guild_id>", methods=["POST"])
@login_required
def set_welcome(guild_id):
    body       = request.json
    channel_id = body.get("channel_id")
    message    = (body.get("message") or "").strip()
    if not channel_id or not message:
        return jsonify({"error": "Missing channel_id or message"}), 400
    data = load_data()
    data["welcome"][guild_id] = {"channel_id": int(channel_id), "message": message}
    save_data(data)
    return jsonify({"ok": True})

@app.route("/api/welcome/<guild_id>", methods=["DELETE"])
@login_required
def delete_welcome(guild_id):
    data = load_data()
    data["welcome"].pop(guild_id, None)
    save_data(data)
    return jsonify({"ok": True})

# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.route("/api/leaderboard")
@login_required
def get_leaderboard():
    data   = load_data()
    sorted_xp = sorted(data["xp"].items(), key=lambda x: x[1], reverse=True)[:20]
    return jsonify([{"user_id": uid, "xp": xp, "level": xp // 100} for uid, xp in sorted_xp])

# ── Schedules ─────────────────────────────────────────────────────────────────
@app.route("/api/schedules", methods=["GET"])
@login_required
def get_schedules():
    data = load_data()
    return jsonify(data["schedules"])

@app.route("/api/schedules/<int:index>", methods=["DELETE"])
@login_required
def delete_schedule(index):
    data = load_data()
    if 0 <= index < len(data["schedules"]):
        data["schedules"].pop(index)
        save_data(data)
        return jsonify({"ok": True})
    return jsonify({"error": "Invalid index"}), 404

# ── Serve dashboard ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user" not in session:
        return send_from_directory(".", "login.html")
    return send_from_directory(".", "dashboard.html")

@app.route("/dashboard.html")
def dashboard():
    if "user" not in session:
        return redirect("/")
    return send_from_directory(".", "dashboard.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
