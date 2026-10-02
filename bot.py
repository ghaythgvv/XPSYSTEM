import os
import re
import sys
import sqlite3
import traceback
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
# IMPORTANT: this bot needs its OWN Discord application + token. If DISCORD_TOKEN
# belongs to another bot (e.g. ELT Music), the two bots overwrite each other's
# slash commands and this one never receives them ("The application did not respond").
TOKEN = os.environ["DISCORD_TOKEN"].strip()
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
TOP_N = 10
POINTS_PER_HOUR = 10
POINTS_PER_VERIFY = 10
POINTS_PER_REPORT = 10
POINTS_PER_BUMP = 10
DISBOARD_ID = 302050872383242240  # the Disboard bot that answers /bump

# ---------- DATABASE ----------
db = sqlite3.connect(DB_PATH)
db.execute("""CREATE TABLE IF NOT EXISTS staff (
    user_id INTEGER PRIMARY KEY,
    voice_seconds INTEGER DEFAULT 0,
    verifications INTEGER DEFAULT 0,
    reports INTEGER DEFAULT 0)""")
try:
    db.execute("ALTER TABLE staff ADD COLUMN bumps INTEGER DEFAULT 0")  # adds the column to an existing database
    db.commit()
except sqlite3.OperationalError:
    pass  # already there
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


def points(voice_seconds, verifications, reports, bumps=0):
    return (int(voice_seconds / 3600 * POINTS_PER_HOUR) + verifications * POINTS_PER_VERIFY
            + reports * POINTS_PER_REPORT + bumps * POINTS_PER_BUMP)


# ---------- CUSTOM EMOJIS ----------
# The bot loads its own APPLICATION emojis (Developer Portal -> your XP app -> Emojis).
# Left = what it is used for, right = the emoji name (exact, or a part of the name).
# If an emoji isn't found it is simply left out and a line is printed in the logs.
EMOJI_NAMES = {
    "crown": "crown_match",
    "spark": "darkpurplesparkly",
    "speaker": "1000035589_no_x",
    "check": "positivo",
    "warn": "purple_warning",
    "hourglass": "1000035598",
    "profile": "1000035570",
    "bump": "bump",
}
EMOJI = {}


async def load_emojis(client):
    try:
        found = await client.fetch_application_emojis()
    except Exception as e:
        print(f"Couldn't load application emojis: {e}", flush=True)
        return
    print(f"Application emojis on this bot: {[e.name for e in found]}", flush=True)
    for key, wanted in EMOJI_NAMES.items():
        match = next((e for e in found if e.name == wanted), None) \
            or next((e for e in found if wanted.lower() in e.name.lower()), None)
        if match:
            EMOJI[key] = match
        else:
            print(f"Emoji '{wanted}' (for {key}) not found on this bot's application - leaving it out.", flush=True)


def em(key: str) -> str:
    """Emoji text followed by a space, or '' if that emoji isn't available."""
    e = EMOJI.get(key)
    return f"{e} " if e else ""


# ---------- BOT ----------
intents = discord.Intents.default()
intents.members = True       # enable "Server Members Intent" in the dev portal
intents.voice_states = True
# Message Content Intent (dev portal) is needed to read the verify/report/bump embeds.
# If it is switched off there, the bot restarts without it so the commands still work.
intents.message_content = os.environ.get("NO_MESSAGE_CONTENT") != "1"


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await load_emojis(self)
        self.add_view(StatsView())
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash command(s): {[c.name for c in synced]}", flush=True)
        except Exception:
            traceback.print_exc()
        voice_tick.start()
        update_leaderboard.start()


bot = Bot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    traceback.print_exception(type(error), error, error.__traceback__)
    if isinstance(error, app_commands.CheckFailure):
        return  # already answered by the check
    msg = "Something went wrong, please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) in {len(bot.guilds)} server(s)", flush=True)
    print("If this name is NOT this bot's own name, DISCORD_TOKEN belongs to a different bot!", flush=True)


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


@voice_tick.before_loop
async def _voice_wait():
    await bot.wait_until_ready()


@voice_tick.error
async def _voice_err(error):
    traceback.print_exception(type(error), error, error.__traceback__)


# ---------- LEADERBOARD PANEL ----------
EMBED_COLOR = 0x8B5CF6


def ranked():
    """All staff sorted by points: (user_id, points, voice_seconds, verifications, reports, bumps)."""
    rows = db.execute("SELECT user_id, voice_seconds, verifications, reports, bumps FROM staff").fetchall()
    out = [(uid, points(vs, ver, rep, b), vs, ver, rep, b) for uid, vs, ver, rep, b in rows]
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def fmt_time(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m"


def build_embed(guild: discord.Guild) -> discord.Embed:
    rows = ranked()[:TOP_N]
    embed = discord.Embed(title=f"{em('crown')}STAFF LEADERBOARD", color=EMBED_COLOR)

    if not rows:
        embed.description = "*Nobody has any points yet.*\nJoin a voice channel and start earning!"
    else:
        podium = "\n".join(
            f"`#{i + 1}`  <@{uid}>  ·  **{p:,}** pts" for i, (uid, p, *_) in enumerate(rows[:3])
        )
        embed.add_field(name=f"{em('crown')}Top 3", value=podium, inline=False)
        if len(rows) > 3:
            rest = "\n".join(
                f"`#{i + 4}`  <@{uid}>  ·  {p:,} pts" for i, (uid, p, *_) in enumerate(rows[3:])
            )
            embed.add_field(name=f"{em('spark')}Rankings", value=rest, inline=False)

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="Updates every minute  ·  Press the button to see your own stats")
    embed.timestamp = discord.utils.utcnow()
    return embed


def build_stats_embed(member: discord.Member) -> discord.Embed:
    rows = ranked()
    pos = next((i for i, r in enumerate(rows) if r[0] == member.id), None)
    if pos is None:
        total, vs, ver, rep, bump = 0, 0, 0, 0, 0
        rank_text = "Unranked"
    else:
        _, total, vs, ver, rep, bump = rows[pos]
        rank_text = f"#{pos + 1} of {len(rows)}"

    embed = discord.Embed(title=f"{em('profile')}{member.display_name}'s Stats", color=EMBED_COLOR)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name=f"{em('crown')}Rank", value=f"**{rank_text}**", inline=True)
    embed.add_field(name=f"{em('spark')}Total points", value=f"**{total:,}**", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(name=f"{em('speaker')}Voice time", value=f"{fmt_time(vs)}\n`{int(vs / 3600 * POINTS_PER_HOUR):,} pts`", inline=True)
    embed.add_field(name=f"{em('check')}Verifications", value=f"{ver}\n`{ver * POINTS_PER_VERIFY:,} pts`", inline=True)
    embed.add_field(name=f"{em('warn')}Reports", value=f"{rep}\n`{rep * POINTS_PER_REPORT:,} pts`", inline=True)
    embed.add_field(name=f"{em('bump')}Bumps", value=f"{bump}\n`{bump * POINTS_PER_BUMP:,} pts`", inline=True)

    if pos == 0:
        embed.add_field(name=f"{em('hourglass')}Progress", value="You're **#1** — keep it up!", inline=False)
    elif pos is not None:
        gap = rows[pos - 1][1] - total
        embed.add_field(name=f"{em('hourglass')}Progress", value=f"**{gap:,} pts** to reach **#{pos}**", inline=False)
    embed.set_footer(text=f"1h in VC = {POINTS_PER_HOUR} pts  ·  verification = {POINTS_PER_VERIFY} pts  ·  report = {POINTS_PER_REPORT} pts  ·  bump = {POINTS_PER_BUMP} pts")
    return embed


class StatsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent: keeps working after restarts
        e = EMOJI.get("profile")
        if e:
            self.my_stats.emoji = discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated)

    @discord.ui.button(label="My Stats", style=discord.ButtonStyle.primary, custom_id="lb:mystats")
    async def my_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("This is for staff members only.", ephemeral=True)
        await interaction.response.send_message(embed=build_stats_embed(interaction.user), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = "Something went wrong, please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


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
    except discord.HTTPException as e:
        print(f"Couldn't update leaderboard: {e}", flush=True)


@update_leaderboard.before_loop
async def _lb_wait():
    await bot.wait_until_ready()


@update_leaderboard.error
async def _lb_err(error):
    traceback.print_exception(type(error), error, error.__traceback__)


# ---------- COMMANDS ----------
@bot.tree.command(name="xp_leaderboard", description="Post the live leaderboard panel here")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def setup_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # answer within 3s no matter what
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await interaction.followup.send(
            "I need **View Channel**, **Send Messages** and **Embed Links** in this channel.", ephemeral=True)
    try:
        msg = await interaction.channel.send(embed=build_embed(interaction.guild), view=StatsView())
    except discord.HTTPException as e:
        return await interaction.followup.send(f"Couldn't post the panel: {e}", ephemeral=True)
    set_setting("lb_channel", interaction.channel.id)
    set_setting("lb_message", msg.id)
    await interaction.followup.send("Panel created.", ephemeral=True)


# ---------- WATCH THE VERIFICATION / REPORT BOTS ----------
MENTION = re.compile(r"<@!?(\d+)>")


async def handle_message(msg: discord.Message):
    if not msg.embeds or msg.guild is None:
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


async def handle_bump(msg: discord.Message):
    """Disboard answers /bump with 'Bump done!' - the staff member who ran /bump gets the points."""
    if msg.guild is None or msg.author.id != DISBOARD_ID:
        return
    text = " ".join([msg.content or ""] + [(e.description or "") for e in msg.embeds]).lower()
    if "bump done" not in text:
        return
    meta = getattr(msg, "interaction_metadata", None)
    user = getattr(meta, "user", None) if meta else None
    if user is None and getattr(msg, "interaction", None):
        user = msg.interaction.user
    if user is None:
        print("[BUMP DEBUG] bump message found but couldn't tell who ran /bump", flush=True)
        return
    member = msg.guild.get_member(user.id)
    if member is None or not is_staff(member):
        return
    cur = db.execute("INSERT OR IGNORE INTO processed (message_id, kind) VALUES (?, ?)", (msg.id, "bump"))
    db.commit()
    if cur.rowcount:
        add(member.id, "bumps", 1)


@bot.event
async def on_message(msg: discord.Message):
    await handle_bump(msg)
    await handle_message(msg)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    from_disboard = str((payload.data.get("author") or {}).get("id")) == str(DISBOARD_ID)
    if payload.channel_id not in (VERIFY_CHANNEL_ID, REPORT_CHANNEL_ID) and not from_disboard:
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    await handle_bump(msg)
    await handle_message(msg)


@bot.tree.command(name="xp_points", description="Check your (or someone's) points")
@app_commands.guild_only()
async def points_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = db.execute("SELECT voice_seconds, verifications, reports, bumps FROM staff WHERE user_id = ?",
                     (member.id,)).fetchone() or (0, 0, 0, 0)
    await interaction.response.send_message(
        f"**{member.display_name}**: {points(*row):,} pts "
        f"({em('speaker')}{fmt_time(row[0])} · {em('check')}{row[1]} · {em('warn')}{row[2]} · {em('bump')}{row[3]} bumps)", ephemeral=True)


try:
    bot.run(TOKEN)
except discord.PrivilegedIntentsRequired:
    if intents.message_content:
        print("WARNING: 'Message Content Intent' is OFF in the Developer Portal (Bot tab -> Privileged Gateway Intents). "
              "Restarting without it: commands work, but verify / report / bump points will NOT be counted until you turn it on.",
              flush=True)
        os.environ["NO_MESSAGE_CONTENT"] = "1"
        os.execv(sys.executable, [sys.executable] + sys.argv)
    raise SystemExit("Turn ON 'Server Members Intent' for this bot in the Discord Developer Portal "
                     "(Bot tab -> Privileged Gateway Intents), then redeploy.")
