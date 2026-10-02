import os
import re
import sqlite3
import discord
from discord import app_commands
from discord.ext import tasks

# ---------- ENV CHECK ----------
_required = ["DISCORD_TOKEN", "STAFF_ROLE_IDS", "VERIFY_CHANNEL_ID", "REPORT_CHANNEL_ID"]
_missing = [k for k in _required if not os.environ.get(k, "").strip()]
if _missing:
    print("MISSING ENVIRONMENT VARIABLES:", ", ".join(_missing), flush=True)
    print("Variables this service can see:", sorted(k for k in os.environ if not k.startswith("RAILWAY_")), flush=True)
    raise SystemExit(1)

# ---------- CONFIG (set these as environment variables) ----------
TOKEN = os.environ["DISCORD_TOKEN"]
STAFF_ROLE_IDS = {int(x) for x in os.environ["STAFF_ROLE_IDS"].split(",") if x.strip()}  # comma-separated role IDs that earn points
VERIFY_CHANNEL_ID = int(os.environ["VERIFY_CHANNEL_ID"])  # channel where the verification bot posts
REPORT_CHANNEL_ID = int(os.environ["REPORT_CHANNEL_ID"])  # channel where the report bot posts


def pick_db_path() -> str:
    """Use DB_PATH if usable, else the Railway volume, else a local file."""
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    candidates = [os.environ.get("DB_PATH"), os.path.join(vol, "points.db") if vol else None, "points.db"]
    for path in candidates:
        if not path:
            continue
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            open(path, "a").close()  # test that we can write here
            print(f"Saving data to: {path}", flush=True)
            if path == "points.db" and vol:
                print("WARNING: not on the volume, data will be lost on redeploy", flush=True)
            return path
        except OSError as e:
            print(f"Can't use {path}: {e}", flush=True)
    raise SystemExit("No writable database path found")


DB_PATH = pick_db_path()
TOP_N = 15

# ---------- DATABASE ----------
db = sqlite3.connect(DB_PATH)
db.execute("""CREATE TABLE IF NOT EXISTS staff (
    user_id INTEGER PRIMARY KEY,
    voice_seconds INTEGER DEFAULT 0,
    verifications INTEGER DEFAULT 0,
    reports INTEGER DEFAULT 0)""")
db.execute("CREATE TABLE IF NOT EXISTS processed (message_id INTEGER, kind TEXT, PRIMARY KEY (message_id, kind))")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)")
db.commit()


def add(user_id: int, column: str, amount: int):
    db.execute("INSERT OR IGNORE INTO staff (user_id) VALUES (?)", (user_id,))
    db.execute(f"UPDATE staff SET {column} = {column} + ? WHERE user_id = ?", (amount, user_id))
    db.commit()


def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def points(voice_seconds, verifications, reports):
    return voice_seconds / 3600 + verifications + reports  # 1 hour = 1 point


# ---------- BOT ----------
intents = discord.Intents.default()
intents.members = True       # enable "Server Members Intent" in the dev portal
intents.voice_states = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        voice_tick.start()
        update_leaderboard.start()


bot = Bot()


def is_staff(member: discord.Member) -> bool:
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


def staff_only():
    async def check(interaction: discord.Interaction):
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("Staff only.", ephemeral=True)
        return False
    return app_commands.check(check)


# ---------- VOICE TRACKING ----------
# Every minute, every staff member currently sitting in a VC gets +60s.
# Restart-proof: nothing is lost if the bot goes down mid-session.
@tasks.loop(minutes=1)
async def voice_tick():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            if vc == guild.afk_channel:
                continue
            for m in vc.members:
                if not m.bot and is_staff(m) and not m.voice.self_deaf:
                    add(m.id, "voice_seconds", 60)


# ---------- LEADERBOARD PANEL ----------
def build_embed(guild: discord.Guild) -> discord.Embed:
    rows = db.execute("SELECT user_id, voice_seconds, verifications, reports FROM staff").fetchall()
    rows.sort(key=lambda r: points(r[1], r[2], r[3]), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, vs, ver, rep) in enumerate(rows[:TOP_N]):
        rank = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(
            f"{rank} <@{uid}> — **{points(vs, ver, rep):.1f} pts**\n"
            f"　🎙️ {vs / 3600:.1f}h · ✅ {ver} verifs · 🚩 {rep} reports"
        )

    embed = discord.Embed(
        title="🏆 Staff Leaderboard",
        description="\n".join(lines) or "No data yet.",
        color=discord.Color.gold(),
    )
    embed.set_footer(text="1h in VC = 1 pt · 1 verification = 1 pt · 1 report = 1 pt · updates every minute")
    embed.timestamp = discord.utils.utcnow()
    return embed


@tasks.loop(minutes=1)
async def update_leaderboard():
    ch_id, msg_id = get_setting("lb_channel"), get_setting("lb_message")
    if not ch_id or not msg_id:
        return
    channel = bot.get_channel(ch_id)
    if channel is None:
        return
    try:
        await channel.get_partial_message(msg_id).edit(embed=build_embed(channel.guild))
    except discord.NotFound:
        pass  # panel was deleted; run /setup_leaderboard again


# ---------- COMMANDS ----------
@bot.tree.command(name="setup_leaderboard", description="Post the live leaderboard panel here")
@app_commands.default_permissions(manage_guild=True)
async def setup_leaderboard(interaction: discord.Interaction):
    await interaction.response.send_message("Panel created ✅", ephemeral=True)
    msg = await interaction.channel.send(embed=build_embed(interaction.guild))
    set_setting("lb_channel", interaction.channel.id)
    set_setting("lb_message", msg.id)


# ---------- WATCH THE VERIFICATION / REPORT BOTS ----------
MENTION = re.compile(r"<@!?(\d+)>")


async def handle_message(msg: discord.Message):
    if not msg.embeds:
        return
    fields = {f.name.strip().lower(): f.value for f in msg.embeds[0].fields}

    if msg.channel.id == VERIFY_CHANNEL_ID:
        kind, column = "verify", "verifications"
        value = fields.get("verified")  # field the verify bot adds with the staff mention
    elif msg.channel.id == REPORT_CHANNEL_ID:
        kind, column = "report", "reports"
        status = fields.get("status", "")
        if not any(w in status.lower() for w in ("resolved", "solved", "closed")):
            return  # still open, claimed, or member left
        keys = ("resolved", "solved", "closed", "handled", "staff", "moderator")
        value = next((v for k, v in fields.items() if any(w in k for w in keys) and MENTION.search(v)), None)
        if not value and MENTION.search(status):
            value = status  # e.g. "Resolved by @someone" inside the Status field
        if not value:
            # resolved, but we can't tell who did it -> dump the embed so it can be matched
            print(f"[REPORT DEBUG] resolved embed, no staff found. fields={dict(fields)} "
                  f"description={msg.embeds[0].description!r} footer={getattr(msg.embeds[0].footer, 'text', None)!r}",
                  flush=True)
            return
    else:
        return

    if not value:
        return
    m = MENTION.search(value)
    if not m:
        return
    staff_id = int(m.group(1))

    member = msg.guild.get_member(staff_id)
    if member is None or not is_staff(member):
        return

    # each message only counts once, even though the bot may edit it several times
    cur = db.execute("INSERT OR IGNORE INTO processed (message_id, kind) VALUES (?, ?)", (msg.id, kind))
    db.commit()
    if cur.rowcount:
        add(staff_id, column, 1)


@bot.event
async def on_message(msg: discord.Message):
    await handle_message(msg)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.channel_id not in (VERIFY_CHANNEL_ID, REPORT_CHANNEL_ID):
        return
    channel = bot.get_channel(payload.channel_id)
    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    await handle_message(msg)


@bot.tree.command(name="points", description="Check your (or someone's) points")
async def points_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = db.execute("SELECT voice_seconds, verifications, reports FROM staff WHERE user_id = ?",
                     (member.id,)).fetchone() or (0, 0, 0)
    await interaction.response.send_message(
        f"**{member.display_name}**: {points(*row):.1f} pts "
        f"(🎙️ {row[0] / 3600:.1f}h · ✅ {row[1]} · 🚩 {row[2]})", ephemeral=True)


bot.run(TOKEN)
