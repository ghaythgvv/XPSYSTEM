import os
import re
import sys
import time
import sqlite3
import traceback
from datetime import timedelta

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


def _ids(name: str) -> set:
    return {int(x) for x in os.environ.get(name, "").replace(" ", "").split(",") if x.strip().isdigit()}


# ---------- CONFIG (environment variables) ----------
# IMPORTANT: this bot needs its OWN Discord application + token, otherwise two bots
# overwrite each other's slash commands ("The application did not respond").
TOKEN = os.environ["DISCORD_TOKEN"].strip()
STAFF_ROLE_IDS = _ids("STAFF_ROLE_IDS")                      # roles that earn points
ADMIN_ROLE_IDS = _ids("ADMIN_ROLE_IDS")                      # high rank (server Administrators always count)
VERIFY_CHANNEL_ID = int(os.environ["VERIFY_CHANNEL_ID"])     # verification bot posts here
REPORT_CHANNEL_ID = int(os.environ["REPORT_CHANNEL_ID"])     # report bot posts here
PUNISH_CHANNEL_ID = int(os.environ.get("PUNISH_CHANNEL_ID") or 0)   # punishment cards channel
SUPPORT_CHANNEL_ID = int(os.environ.get("SUPPORT_CHANNEL_ID") or 0)  # OPTIONAL: moderator support-request alerts channel
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID") or 0)   # private channel for DB backups
BUMP_BOT_IDS = {302050872383242240} | _ids("BUMP_BOT_IDS")          # Disboard (+ extra bump bots if you add IDs)
VOICE_IGNORE_ALONE = os.environ.get("VOICE_IGNORE_ALONE") == "1"     # 1 = no VC points while sitting alone

POINTS_PER_HOUR = int(os.environ.get("POINTS_PER_HOUR", 10))
POINTS_PER_VERIFY = 10
POINTS_PER_REPORT = 10
POINTS_PER_BUMP = 10
POINTS_PER_PUNISH = 10   # /warn /unwarn /mute /timeout /kick /ban
POINTS_PER_SUPPORT = 10  # resolved support requests (only if SUPPORT_CHANNEL_ID is set)
SHOW_SUPPORT = bool(SUPPORT_CHANNEL_ID)

TOP_N = 15
PAGE_SIZE = 10
DEBUG = os.environ.get("DEBUG") == "1"
BANNER_URL = os.environ.get("BANNER_URL", "").strip()
BANNER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.gif")
EMBED_COLOR = 0x8B5CF6


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
            open(path, "a").close()
            print(f"Saving data to: {path}", flush=True)
            if vol and not os.path.abspath(path).startswith(os.path.abspath(vol)):
                print("WARNING: database is NOT on the Railway volume, XP can be lost on redeploy!", flush=True)
            return path
        except OSError as e:
            print(f"Can't use {path}: {e}", flush=True)
    raise SystemExit("No writable database path found")


DB_PATH = pick_db_path()
if not BACKUP_CHANNEL_ID:
    print("WARNING: BACKUP_CHANNEL_ID is not set - there is no Discord backup of the XP database.", flush=True)

# ---------- DATABASE ----------
COLS = ("voice_seconds", "verifications", "reports", "bumps", "punishments", "supports")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS staff (
    user_id INTEGER PRIMARY KEY,
    voice_seconds INTEGER DEFAULT 0,
    verifications INTEGER DEFAULT 0,
    reports INTEGER DEFAULT 0)""")
for _col in COLS[3:]:
    try:
        db.execute(f"ALTER TABLE staff ADD COLUMN {_col} INTEGER DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass  # already there
db.execute("CREATE TABLE IF NOT EXISTS processed (message_id INTEGER, kind TEXT, PRIMARY KEY (message_id, kind))")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)")
# daily log -> powers the weekly leaderboard
db.execute("""CREATE TABLE IF NOT EXISTS log (
    day TEXT, user_id INTEGER, col TEXT, amount INTEGER,
    PRIMARY KEY (day, user_id, col))""")
db.commit()

_stats = {"claims": 0}


def today() -> str:
    return discord.utils.utcnow().date().isoformat()


def add(user_id: int, column: str, amount: int, commit: bool = True):
    if column not in COLS:
        raise ValueError(f"bad column {column}")
    db.execute("INSERT OR IGNORE INTO staff (user_id) VALUES (?)", (user_id,))
    db.execute(f"UPDATE staff SET {column} = MAX(0, {column} + ?) WHERE user_id = ?", (amount, user_id))
    db.execute(
        "INSERT INTO log (day, user_id, col, amount) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(day, user_id, col) DO UPDATE SET amount = amount + excluded.amount",
        (today(), user_id, column, amount))
    if commit:
        db.commit()


def award(staff_id: int, message_id: int, kind: str, column: str) -> bool:
    """Give 1 count for a message - every message counts exactly once per kind."""
    cur = db.execute("INSERT OR IGNORE INTO processed (message_id, kind) VALUES (?, ?)", (message_id, kind))
    if not cur.rowcount:
        db.commit()
        return False
    add(staff_id, column, 1, commit=False)
    db.commit()
    _stats["claims"] += 1
    return True


def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def points(voice_seconds, verifications=0, reports=0, bumps=0, punishments=0, supports=0):
    return (int(voice_seconds / 3600 * POINTS_PER_HOUR) + verifications * POINTS_PER_VERIFY
            + reports * POINTS_PER_REPORT + bumps * POINTS_PER_BUMP
            + punishments * POINTS_PER_PUNISH + supports * POINTS_PER_SUPPORT)


# ---------- CUSTOM EMOJIS ----------
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
            print(f"Emoji '{wanted}' (for {key}) not found - leaving it out.", flush=True)


def em(key: str) -> str:
    e = EMOJI.get(key)
    return f"{e} " if e else ""


# ---------- BOT ----------
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
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
        await restore_backup()
        voice_tick.start()
        update_leaderboard.start()
        backup_db.start()


bot = Bot()


def is_staff(member) -> bool:
    roles = getattr(member, "roles", None)
    return bool(roles) and any(r.id in STAFF_ROLE_IDS for r in roles)


def is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator or any(r.id in ADMIN_ROLE_IDS for r in member.roles)


def admin_only():
    async def check(interaction: discord.Interaction):
        if is_admin(interaction.user):
            return True
        await interaction.response.send_message("High rank only.", ephemeral=True)
        return False
    return app_commands.check(check)


def staff_ids(guild: discord.Guild) -> set:
    ids = set()
    for rid in STAFF_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            ids.update(m.id for m in role.members if not m.bot)
    return ids


# ---------- BACKUP / RESTORE ----------
_last_backup = None
_restore_ok = True


async def _backup_channel():
    return bot.get_channel(BACKUP_CHANNEL_ID) or await bot.fetch_channel(BACKUP_CHANNEL_ID)


async def restore_backup():
    """Brand-new empty database -> load the newest backup from Discord."""
    global _restore_ok
    if get_setting("initialized") is not None:
        return
    if BACKUP_CHANNEL_ID:
        try:
            ch = await _backup_channel()
            async for m in ch.history(limit=100):
                att = next((a for a in m.attachments if a.filename == "points_backup.db"), None)
                if m.author.id == bot.user.id and att:
                    tmp = DB_PATH + ".restore"
                    with open(tmp, "wb") as f:
                        f.write(await att.read())
                    src = sqlite3.connect(tmp)
                    src.backup(db)
                    src.close()
                    os.remove(tmp)
                    print("Restored XP from the latest Discord backup.", flush=True)
                    break
        except Exception:
            traceback.print_exc()
            _restore_ok = False  # never overwrite good backups with an empty DB
            return
    set_setting("initialized", 1)


async def do_backup(force: bool = False) -> bool:
    global _last_backup
    if not BACKUP_CHANNEL_ID or not _restore_ok:
        return False
    snap = (repr(db.execute("SELECT * FROM staff ORDER BY user_id").fetchall())
            + repr(db.execute("SELECT * FROM settings ORDER BY key").fetchall())
            + repr(db.execute("SELECT COUNT(*) FROM processed").fetchone()))  # processed is part of the file too
    if snap == _last_backup and not force:
        return False
    ch = await _backup_channel()
    tmp = DB_PATH + ".bak"
    dst = sqlite3.connect(tmp)
    db.backup(dst)
    dst.close()
    try:
        await ch.send("XP database backup", file=discord.File(tmp, filename="points_backup.db"))
    finally:
        os.remove(tmp)
    _last_backup = snap
    old = [m async for m in ch.history(limit=50) if m.author.id == bot.user.id and m.attachments]
    for m in old[5:]:
        try:
            await m.delete()
        except discord.HTTPException:
            pass
    return True


# NOTE: a discord.ext.tasks loop STOPS FOR GOOD after an unhandled error, so every loop body is wrapped.
@tasks.loop(minutes=5)
async def backup_db():
    try:
        await do_backup()
        cutoff = (discord.utils.utcnow().date() - timedelta(days=60)).isoformat()
        db.execute("DELETE FROM log WHERE day < ?", (cutoff,))
        db.commit()
    except Exception:
        traceback.print_exc()


@backup_db.before_loop
async def _backup_wait():
    await bot.wait_until_ready()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    traceback.print_exception(type(error), error, error.__traceback__)
    if isinstance(error, app_commands.CheckFailure):
        return
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
    print(f"Message Content Intent: {'ON' if intents.message_content else 'OFF -> verify/report/bump/punishment points will NOT count'}", flush=True)
    for name, cid in (("VERIFY", VERIFY_CHANNEL_ID), ("REPORT", REPORT_CHANNEL_ID), ("PUNISH", PUNISH_CHANNEL_ID),
                      ("SUPPORT", SUPPORT_CHANNEL_ID), ("BACKUP", BACKUP_CHANNEL_ID)):
        ch = bot.get_channel(cid) if cid else None
        print(f"{name}_CHANNEL_ID={cid} -> {('#' + ch.name) if ch else 'NOT SET or the bot cannot see this channel'}", flush=True)
    if not PUNISH_CHANNEL_ID:
        print("PUNISH_CHANNEL_ID is not set -> punishment points can NOT be counted.", flush=True)
    print(f"Staff role IDs: {sorted(STAFF_ROLE_IDS)}", flush=True)


# ---------- VOICE TRACKING ----------
_last_tick = None


@tasks.loop(seconds=60)
async def voice_tick():
    global _last_tick
    try:
        now = time.monotonic()
        # real elapsed time (a slow tick doesn't lose seconds), clamped so a freeze can't give a huge bonus
        delta = 60 if _last_tick is None else int(min(max(now - _last_tick, 30), 120))
        _last_tick = now
        for guild in bot.guilds:
            for vc in list(guild.voice_channels) + list(guild.stage_channels):
                if vc == guild.afk_channel:
                    continue
                humans = [m for m in vc.members if not m.bot]
                if VOICE_IGNORE_ALONE and len(humans) < 2:
                    continue
                for m in humans:
                    if not is_staff(m) or m.voice is None or m.voice.self_deaf or m.voice.deaf:
                        continue
                    add(m.id, "voice_seconds", delta, commit=False)
        db.commit()
    except Exception:
        traceback.print_exc()


@voice_tick.before_loop
async def _voice_wait():
    await bot.wait_until_ready()


# ---------- RANKING ----------
def ranked(guild=None, since: str = None):
    """Current staff sorted by points. since=None -> all time, else a 'YYYY-MM-DD' start day.
    Row: (user_id, points, voice_seconds, verifications, reports, bumps, punishments, supports)"""
    if since is None:
        data = {r[0]: tuple(r[1:]) for r in db.execute(f"SELECT user_id, {', '.join(COLS)} FROM staff")}
    else:
        data = {}
        for uid, col, amt in db.execute("SELECT user_id, col, SUM(amount) FROM log WHERE day >= ? GROUP BY user_id, col", (since,)):
            d = data.setdefault(uid, [0] * len(COLS))
            d[COLS.index(col)] += max(0, amt)
    ids = staff_ids(guild) if guild is not None else set(data)
    out = []
    for uid in ids:
        vals = tuple(data.get(uid, (0,) * len(COLS)))
        out.append((uid, points(*vals)) + vals)
    out.sort(key=lambda r: (-r[1], -r[2], r[0]))
    return out


def week_start(days: int = 7) -> str:
    return (discord.utils.utcnow().date() - timedelta(days=days - 1)).isoformat()


def fmt_time(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m:02d}m"


def bar(value: int, top: int, size: int = 8) -> str:
    filled = round(size * value / top) if top else 0
    return "\u25b0" * filled + "\u25b1" * (size - filled)


def rates_text() -> str:
    t = (f"`{POINTS_PER_HOUR} pts / hour in VC`  \u00b7  `{POINTS_PER_VERIFY} pts / verification`  \u00b7  "
         f"`{POINTS_PER_REPORT} pts / report`  \u00b7  `{POINTS_PER_BUMP} pts / bump`  \u00b7  "
         f"`{POINTS_PER_PUNISH} pts / punishment`")
    if SHOW_SUPPORT:
        t += f"  \u00b7  `{POINTS_PER_SUPPORT} pts / support`"
    return t


def build_embed(guild: discord.Guild, banner: bool = False) -> discord.Embed:
    all_rows = ranked(guild)
    rows = all_rows[:TOP_N]
    embed = discord.Embed(
        title=f"{em('crown')}STAFF LEADERBOARD",
        description=("Earn points by staying active in voice, verifying members, resolving reports, bumping the server "
                     "and punishing rule breakers.\n" + rates_text() + "\n\u200b"),
        color=EMBED_COLOR,
    )
    if not rows or rows[0][1] == 0 and not any(r[2] for r in rows):
        embed.description = "*Nobody has any points yet.*\nJoin a voice channel and start earning!"
    else:
        places = ["1st place", "2nd place", "3rd place"]
        for i, (uid, p, vs, *_rest) in enumerate(rows[:3]):
            embed.add_field(
                name=f"{em('crown') if i == 0 else ''}{places[i]}",
                value=f"<@{uid}>\n**{p:,}** pts\n`{fmt_time(vs)}` in voice",
                inline=True,
            )
        if len(rows) > 3:
            top = rows[0][1]
            rest = "\n".join(
                f"`#{i + 4:>2}`  <@{uid}>  `{bar(p, top)}`  **{p:,}** pts"
                for i, (uid, p, *_r) in enumerate(rows[3:])
            )
            embed.add_field(name=f"{em('spark')}Rankings", value=rest[:1024], inline=False)

        week = [r for r in ranked(guild, week_start()) if r[1] > 0][:3]
        if week:
            embed.add_field(
                name=f"{em('spark')}Top this week",
                value="\n".join(f"`#{i + 1}`  <@{r[0]}>  **{r[1]:,}** pts" for i, r in enumerate(week)),
                inline=False)

        totals = (f"`{len(all_rows)}` staff  \u00b7  `{sum(r[1] for r in all_rows):,}` pts  \u00b7  "
                  f"`{fmt_time(sum(r[2] for r in all_rows))}` in voice\n"
                  f"`{sum(r[3] for r in all_rows)}` verifications  \u00b7  `{sum(r[4] for r in all_rows)}` reports  \u00b7  "
                  f"`{sum(r[5] for r in all_rows)}` bumps  \u00b7  `{sum(r[6] for r in all_rows)}` punishments")
        if SHOW_SUPPORT:
            totals += f"  \u00b7  `{sum(r[7] for r in all_rows)}` supports"
        embed.add_field(name=f"{em('hourglass')}Team totals", value=totals, inline=False)

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if banner:
        embed.set_image(url="attachment://banner.gif")
    elif BANNER_URL:
        embed.set_image(url=BANNER_URL)
    embed.set_footer(text="Updates every minute  \u00b7  Press a button to see your stats")
    embed.timestamp = discord.utils.utcnow()
    return embed


def build_stats_embed(member: discord.Member) -> discord.Embed:
    rows = ranked(member.guild)
    pos = next((i for i, r in enumerate(rows) if r[0] == member.id), None)
    if pos is None:
        total, vs, ver, rep, bump, pun, sup = 0, 0, 0, 0, 0, 0, 0
        rank_text = "Unranked"
    else:
        _, total, vs, ver, rep, bump, pun, sup = rows[pos]
        rank_text = f"#{pos + 1} of {len(rows)}"
    wk = next((r for r in ranked(member.guild, week_start()) if r[0] == member.id), None)
    week_pts = wk[1] if wk else 0

    embed = discord.Embed(title=f"{em('profile')}{member.display_name}'s Stats", color=EMBED_COLOR)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name=f"{em('crown')}Rank", value=f"**{rank_text}**", inline=True)
    embed.add_field(name=f"{em('spark')}Total points", value=f"**{total:,}**", inline=True)
    embed.add_field(name=f"{em('hourglass')}This week", value=f"**{week_pts:,}** pts", inline=True)

    embed.add_field(name=f"{em('speaker')}Voice time", value=f"{fmt_time(vs)}\n`{int(vs / 3600 * POINTS_PER_HOUR):,} pts`", inline=True)
    embed.add_field(name=f"{em('check')}Verifications", value=f"{ver}\n`{ver * POINTS_PER_VERIFY:,} pts`", inline=True)
    embed.add_field(name=f"{em('warn')}Reports", value=f"{rep}\n`{rep * POINTS_PER_REPORT:,} pts`", inline=True)
    embed.add_field(name=f"{em('bump')}Bumps", value=f"{bump}\n`{bump * POINTS_PER_BUMP:,} pts`", inline=True)
    embed.add_field(name=f"{em('warn')}Punishments", value=f"{pun}\n`{pun * POINTS_PER_PUNISH:,} pts`", inline=True)
    if SHOW_SUPPORT:
        embed.add_field(name=f"{em('check')}Supports", value=f"{sup}\n`{sup * POINTS_PER_SUPPORT:,} pts`", inline=True)

    if pos == 0:
        embed.add_field(name=f"{em('hourglass')}Progress", value="You're **#1** \u2014 keep it up!", inline=False)
    elif pos is not None:
        gap = rows[pos - 1][1] - total
        embed.add_field(name=f"{em('hourglass')}Progress", value=f"**{gap:,} pts** to reach **#{pos}**", inline=False)
    embed.set_footer(text=f"1h in VC = {POINTS_PER_HOUR} pts  \u00b7  verification / report / bump / punishment = {POINTS_PER_VERIFY} pts")
    return embed


def build_week_embed(guild: discord.Guild, days: int = 7) -> discord.Embed:
    rows = [r for r in ranked(guild, week_start(days)) if r[1] > 0][:10]
    embed = discord.Embed(title=f"{em('crown')}WEEKLY LEADERBOARD", color=EMBED_COLOR)
    if not rows:
        embed.description = "*Nobody has earned points in the last 7 days.*"
    else:
        embed.description = "\n".join(
            f"`#{i + 1:>2}`  <@{r[0]}>  **{r[1]:,}** pts  \u00b7  `{fmt_time(r[2])}` VC"
            for i, r in enumerate(rows))
    embed.set_footer(text=f"Last {days} days (UTC)")
    return embed


def build_full_embed(page: int, guild=None) -> tuple:
    rows = ranked(guild)
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    embed = discord.Embed(title=f"{em('crown')}FULL STAFF LEADERBOARD", color=EMBED_COLOR)
    if not chunk:
        embed.description = "*Nobody has any points yet.*"
    else:
        lines = []
        for i, (uid, p, vs, ver, rep, b, pu, su) in enumerate(chunk):
            n = page * PAGE_SIZE + i + 1
            extra = f"  \u00b7  `{su}` sup" if SHOW_SUPPORT else ""
            lines.append(
                f"`#{n:>2}`  <@{uid}>  **{p:,}** pts\n"
                f"\u2003`{fmt_time(vs)}` VC  \u00b7  `{ver}` ver  \u00b7  `{rep}` rep  \u00b7  `{b}` bump  \u00b7  `{pu}` pun{extra}"
            )
        embed.description = "\n".join(lines)
    embed.set_footer(text=f"Page {page + 1}/{pages}  \u00b7  {len(rows)} staff")
    return embed, pages


class FullBoardView(discord.ui.View):
    def __init__(self, guild, page: int = 0):
        super().__init__(timeout=180)
        self.guild = guild
        self.page = page
        self._sync()

    def _sync(self):
        _, pages = build_full_embed(self.page, self.guild)
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        embed, _ = build_full_embed(self.page, self.guild)
        self._sync()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        embed, _ = build_full_embed(self.page, self.guild)
        self._sync()
        await interaction.response.edit_message(embed=embed, view=self)


class StatsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        e = EMOJI.get("profile")
        if e:
            self.my_stats.emoji = discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated)

    @discord.ui.button(label="My Stats", style=discord.ButtonStyle.primary, custom_id="lb:mystats")
    async def my_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user) and not is_admin(interaction.user):
            return await interaction.response.send_message("This is for staff members only.", ephemeral=True)
        await interaction.response.send_message(embed=build_stats_embed(interaction.user), ephemeral=True)

    @discord.ui.button(label="This Week", style=discord.ButtonStyle.secondary, custom_id="lb:week")
    async def weekly(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user) and not is_admin(interaction.user):
            return await interaction.response.send_message("This is for staff members only.", ephemeral=True)
        await interaction.response.send_message(embed=build_week_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Full Leaderboard", style=discord.ButtonStyle.secondary, custom_id="lb:full")
    async def full_board(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user):
            return await interaction.response.send_message("Only high rank can view the full leaderboard.", ephemeral=True)
        embed, _ = build_full_embed(0, interaction.guild)
        await interaction.response.send_message(embed=embed, view=FullBoardView(interaction.guild, 0), ephemeral=True)

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
    try:
        ch_id, msg_id = get_setting("lb_channel"), get_setting("lb_message")
        if not ch_id or not msg_id:
            return
        channel = bot.get_channel(ch_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(ch_id)
            except discord.HTTPException:
                return
        try:
            await channel.get_partial_message(msg_id).edit(
                embed=build_embed(channel.guild, banner=bool(get_setting("lb_banner"))))
        except discord.NotFound:
            pass  # panel was deleted; run /xp_leaderboard again
        except discord.HTTPException as e:
            print(f"Couldn't update leaderboard: {e}", flush=True)
    except Exception:
        traceback.print_exc()


@update_leaderboard.before_loop
async def _lb_wait():
    await bot.wait_until_ready()


# ---------- COMMANDS ----------
@bot.tree.command(name="xp_leaderboard", description="Post the live leaderboard panel here")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@admin_only()
async def setup_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await interaction.followup.send(
            "I need **View Channel**, **Send Messages** and **Embed Links** in this channel.", ephemeral=True)
    has_banner = os.path.exists(BANNER_FILE)
    try:
        kwargs = {"file": discord.File(BANNER_FILE, filename="banner.gif")} if has_banner else {}
        msg = await interaction.channel.send(embed=build_embed(interaction.guild, banner=has_banner), view=StatsView(), **kwargs)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"Couldn't post the panel: {e}", ephemeral=True)
    set_setting("lb_channel", interaction.channel.id)
    set_setting("lb_message", msg.id)
    set_setting("lb_banner", 1 if has_banner else 0)
    await interaction.followup.send("Panel created.", ephemeral=True)


class ConfirmReset(discord.ui.View):
    def __init__(self, admin_id: int, target: discord.Member = None):
        super().__init__(timeout=60)
        self.admin_id = admin_id
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.target:
            db.execute("DELETE FROM staff WHERE user_id = ?", (self.target.id,))
            db.execute("DELETE FROM log WHERE user_id = ?", (self.target.id,))
            text = f"Reset all points for {self.target.mention}."
        else:
            db.execute("DELETE FROM staff")
            db.execute("DELETE FROM log")
            text = "Reset points for **everyone**."
        db.commit()
        self.stop()
        await interaction.response.edit_message(content=f"{text} The panel updates within a minute.", view=None)
        try:
            await do_backup(force=True)
        except Exception:
            traceback.print_exc()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Reset cancelled.", view=None)


@bot.tree.command(name="xp_reset", description="High rank: reset points for one member, or everyone")
@app_commands.describe(member="Leave empty to reset EVERYONE")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@admin_only()
async def xp_reset(interaction: discord.Interaction, member: discord.Member = None):
    who = member.mention if member else "**EVERYONE**"
    await interaction.response.send_message(
        f"{em('warn')}Are you sure you want to reset the points of {who}? This can't be undone.",
        view=ConfirmReset(interaction.user.id, member), ephemeral=True)


CATEGORIES = {
    "voice": ("voice_seconds", 60),  # entered in minutes
    "verifications": ("verifications", 1),
    "reports": ("reports", 1),
    "bumps": ("bumps", 1),
    "punishments": ("punishments", 1),
    "supports": ("supports", 1),
}


@bot.tree.command(name="xp_add", description="High rank: add or remove counts for a member (fix missed points)")
@app_commands.describe(member="Staff member", category="What to change", amount="Use a negative number to remove (voice = minutes)")
@app_commands.choices(category=[
    app_commands.Choice(name="Voice (minutes)", value="voice"),
    app_commands.Choice(name="Verifications", value="verifications"),
    app_commands.Choice(name="Reports", value="reports"),
    app_commands.Choice(name="Bumps", value="bumps"),
    app_commands.Choice(name="Punishments", value="punishments"),
    app_commands.Choice(name="Supports", value="supports"),
])
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@admin_only()
async def xp_add(interaction: discord.Interaction, member: discord.Member,
                 category: app_commands.Choice[str], amount: app_commands.Range[int, -100000, 100000]):
    col, mult = CATEGORIES[category.value]
    add(member.id, col, amount * mult)
    note = "" if is_staff(member) else "\n(Note: this member has no staff role, so they won't show on the leaderboard.)"
    await interaction.response.send_message(
        f"Changed **{category.name}** for {member.mention} by **{amount:+d}**.{note}", ephemeral=True)


@bot.tree.command(name="xp_backup", description="High rank: save a database backup to the backup channel now")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@admin_only()
async def xp_backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not BACKUP_CHANNEL_ID:
        return await interaction.followup.send("BACKUP_CHANNEL_ID is not set.", ephemeral=True)
    try:
        ok = await do_backup(force=True)
    except Exception as e:
        return await interaction.followup.send(f"Backup failed: {e}", ephemeral=True)
    await interaction.followup.send("Backup saved." if ok else "Backups are disabled (restore failed on startup).", ephemeral=True)


@bot.tree.command(name="xp_week", description="Show this week's staff leaderboard")
@app_commands.guild_only()
async def xp_week(interaction: discord.Interaction):
    if not is_staff(interaction.user) and not is_admin(interaction.user):
        return await interaction.response.send_message("This is for staff members only.", ephemeral=True)
    await interaction.response.send_message(embed=build_week_embed(interaction.guild), ephemeral=True)


@bot.tree.command(name="xp_points", description="Check your (or someone's) points")
@app_commands.guild_only()
async def points_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = db.execute(f"SELECT {', '.join(COLS)} FROM staff WHERE user_id = ?", (member.id,)).fetchone() or (0,) * len(COLS)
    text = (f"**{member.display_name}**: {points(*row):,} pts "
            f"({em('speaker')}{fmt_time(row[0])} \u00b7 {em('check')}{row[1]} \u00b7 {em('warn')}{row[2]} \u00b7 "
            f"{em('bump')}{row[3]} bumps \u00b7 {em('warn')}{row[4]} punishments")
    if SHOW_SUPPORT:
        text += f" \u00b7 {row[5]} supports"
    text += ")"
    if not is_staff(member):
        text += "\n*(not a staff member - not shown on the leaderboard)*"
    await interaction.response.send_message(text, ephemeral=True)


# ---------- WATCH THE VERIFICATION / REPORT / PUNISHMENT / SUPPORT / BUMP MESSAGES ----------
MENTION = re.compile(r"<@!?(\d+)>")
DONE = re.compile(r"\b(?:resolved|solved|closed)\b", re.I)  # \b so "Unresolved" does NOT count
PLAIN_NAMES = {"description", "title", "footer", "author", "content"}


def embed_pairs(msg: discord.Message):
    """(name, text) for every field + description/title/footer/author/content of every embed."""
    pairs = []
    for e in msg.embeds:
        for f in e.fields:
            pairs.append((f.name.strip().lower(), f.value or ""))
        if e.description:
            pairs.append(("description", e.description))
        if e.title:
            pairs.append(("title", e.title))
        if e.footer and e.footer.text:
            pairs.append(("footer", e.footer.text))
        if e.author and e.author.name:
            pairs.append(("author", e.author.name))
    if msg.content:
        pairs.append(("content", msg.content))
    return pairs


def first_staff(guild: discord.Guild, text: str):
    """First mentioned user in text who currently has a staff role."""
    for uid in MENTION.findall(text or ""):
        member = guild.get_member(int(uid))
        if member and is_staff(member):
            return member.id
    return None


def staff_from_keys(msg, keys):
    for name, value in embed_pairs(msg):
        if name in PLAIN_NAMES:
            continue
        if any(k in name for k in keys):
            found = first_staff(msg.guild, value)
            if found:
                return found
    return None


def staff_from_text(msg, phrases):
    """Finds 'resolved by <@id>' / 'Moderator: <@id>' style text anywhere in the message."""
    pattern = re.compile(r"(?:%s)\W{0,14}<@!?(\d+)>" % "|".join(phrases), re.I)
    for _name, value in embed_pairs(msg):
        for m in pattern.finditer(value):
            member = msg.guild.get_member(int(m.group(1)))
            if member and is_staff(member):
                return member.id
    return None


def interaction_user(msg):
    meta = getattr(msg, "interaction_metadata", None)
    user = getattr(meta, "user", None) if meta else None
    if user is None:
        old = getattr(msg, "interaction", None)
        user = getattr(old, "user", None) if old else None
    return user


def handle_verify(msg):
    staff_id = (staff_from_keys(msg, ("verified",))
                or staff_from_keys(msg, ("verifier", "approved", "staff", "moderator", "handled"))
                or staff_from_text(msg, ("verified by", "verifier", "approved by", "handled by", "staff", "moderator")))
    if staff_id is None:
        if DEBUG or msg.embeds:
            print(f"[VERIFY DEBUG] no staff found. pairs={embed_pairs(msg)}", flush=True)
        return
    award(staff_id, msg.id, "verify", "verifications")


def handle_resolved(msg, kind: str, column: str):
    """Report / support cards: only count once the card says Resolved/Solved/Closed."""
    pairs = embed_pairs(msg)
    status = " ".join(v for n, v in pairs if "status" in n)
    if not status:
        status = " ".join(v for n, v in pairs if n in ("title", "description", "footer"))
    if not DONE.search(status):
        return  # still open, claimed, or member left
    staff_id = (staff_from_keys(msg, ("resolved", "solved", "closed", "handled", "staff", "moderator"))
                or staff_from_text(msg, ("resolved\\s+by", "solved\\s+by", "closed\\s+by", "handled\\s+by",
                                         "resolved", "moderator", "staff")))
    if staff_id is None:
        print(f"[{kind.upper()} DEBUG] resolved card, no staff found. pairs={pairs}", flush=True)
        return
    award(staff_id, msg.id, kind, column)


def handle_bump(msg):
    """Disboard answers /bump with 'Bump done!' - the staff member who ran /bump gets the points."""
    if msg.author.id not in BUMP_BOT_IDS:
        return
    text = " ".join([msg.content or ""] + [(e.description or "") for e in msg.embeds]).lower()
    old = getattr(msg, "interaction", None)
    cmd = (getattr(old, "name", "") or "").lower() if old else ""
    done = "bump done" in text or (cmd == "bump" and msg.embeds and "wait" not in text)
    if not done:
        return
    user = interaction_user(msg)
    staff_id = user.id if user else None
    if staff_id is None:
        m = MENTION.search(text)
        staff_id = int(m.group(1)) if m else None
    if staff_id is None:
        print("[BUMP DEBUG] bump message found but couldn't tell who ran /bump", flush=True)
        return
    member = msg.guild.get_member(staff_id)
    if member is None or not is_staff(member):
        return
    award(member.id, msg.id, "bump", "bumps")


def handle_punish(msg):
    """Punishment cards (/warn /unwarn /mute /timeout /kick /ban) - the mod who ran the command gets points."""
    staff_id = None
    user = interaction_user(msg)
    if user is not None:
        staff_id = user.id
    elif msg.embeds:
        staff_id = (staff_from_keys(msg, ("punisher", "moderator", "issued", "staff", "responsible", "admin", "mod", "by"))
                    or staff_from_text(msg, ("punisher", "moderator", "issued by", "staff", "by")))
    if staff_id is None:
        print(f"[PUNISH DEBUG] card found but couldn't tell who the mod is. pairs={embed_pairs(msg)}", flush=True)
        return
    member = msg.guild.get_member(staff_id)
    if member is None or not is_staff(member):
        print(f"[PUNISH DEBUG] {staff_id} skipped: not in server cache or has no role from STAFF_ROLE_IDS", flush=True)
        return
    award(staff_id, msg.id, "punish", "punishments")


def process(msg: discord.Message):
    if msg.guild is None or (bot.user and msg.author.id == bot.user.id):
        return
    try:
        handle_bump(msg)
        cid = msg.channel.id
        if cid == PUNISH_CHANNEL_ID and msg.author.bot:
            handle_punish(msg)
        elif cid == VERIFY_CHANNEL_ID and msg.embeds:
            handle_verify(msg)
        elif cid == REPORT_CHANNEL_ID and msg.embeds:
            handle_resolved(msg, "report", "reports")
        elif SUPPORT_CHANNEL_ID and cid == SUPPORT_CHANNEL_ID and msg.embeds:
            handle_resolved(msg, "support", "supports")
    except Exception:
        traceback.print_exc()


@bot.event
async def on_message(msg: discord.Message):
    if DEBUG and msg.channel.id in (VERIFY_CHANNEL_ID, REPORT_CHANNEL_ID, PUNISH_CHANNEL_ID, SUPPORT_CHANNEL_ID):
        print(f"[SEEN] channel={msg.channel.id} from={msg.author} embeds={len(msg.embeds)} "
              f"interaction_user={interaction_user(msg)}", flush=True)
    process(msg)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    watched = {c for c in (VERIFY_CHANNEL_ID, REPORT_CHANNEL_ID, PUNISH_CHANNEL_ID, SUPPORT_CHANNEL_ID) if c}
    from_bump_bot = str((payload.data.get("author") or {}).get("id")) in {str(i) for i in BUMP_BOT_IDS}
    if payload.channel_id not in watched and not from_bump_bot:
        return
    try:
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    process(msg)


@bot.tree.command(name="xp_scan", description="High rank: re-check old messages and count anything that was missed")
@app_commands.describe(limit="How many recent messages to check in each watched channel (and this one for bumps)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@admin_only()
async def xp_scan(interaction: discord.Interaction, limit: app_commands.Range[int, 10, 1000] = 200):
    await interaction.response.defer(ephemeral=True)
    ids = [c for c in (VERIFY_CHANNEL_ID, REPORT_CHANNEL_ID, PUNISH_CHANNEL_ID, SUPPORT_CHANNEL_ID, interaction.channel_id) if c]
    before = _stats["claims"]
    problems = []
    for cid in dict.fromkeys(ids):  # unique, keeps order
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            async for m in ch.history(limit=limit):
                process(m)
        except Exception as e:
            problems.append(f"<#{cid}>: {e}")
    new = _stats["claims"] - before
    text = f"Scan finished - **{new}** missed item(s) counted (nothing is ever counted twice)."
    if problems:
        text += "\nCouldn't read: " + "; ".join(problems)
    await interaction.followup.send(text, ephemeral=True)


try:
    bot.run(TOKEN)
except discord.LoginFailure:
    raise SystemExit("DISCORD_TOKEN is invalid - reset it in the Developer Portal and update the variable.")
except discord.PrivilegedIntentsRequired:
    if intents.message_content:
        print("WARNING: 'Message Content Intent' is OFF in the Developer Portal (Bot tab -> Privileged Gateway Intents). "
              "Restarting without it: commands work, but verify / report / bump / punishment points will NOT be counted until you turn it on.",
              flush=True)
        os.environ["NO_MESSAGE_CONTENT"] = "1"
        os.execv(sys.executable, [sys.executable] + sys.argv)
    raise SystemExit("Turn ON 'Server Members Intent' for this bot in the Discord Developer Portal "
                     "(Bot tab -> Privileged Gateway Intents), then redeploy.")
