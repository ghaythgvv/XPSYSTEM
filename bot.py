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
ADMIN_ROLE_IDS = {int(x) for x in os.environ.get("ADMIN_ROLE_IDS", "").split(",") if x.strip()}  # high-rank roles (optional; server Administrators always count)
PUNISH_CHANNEL_ID = int(os.environ.get("PUNISH_CHANNEL_ID") or 0)  # channel where the punishment bot posts its cards
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID") or 0)  # private channel where the bot keeps DB backups


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
            if vol and not os.path.abspath(path).startswith(os.path.abspath(vol)):
                print("WARNING: database is NOT on the Railway volume, XP will be lost on redeploy!", flush=True)
            return path
        except OSError as e:
            print(f"Can't use {path}: {e}", flush=True)
    raise SystemExit("No writable database path found")


DB_PATH = pick_db_path()
TOP_N = 15
PAGE_SIZE = 10
POINTS_PER_HOUR = 10
POINTS_PER_VERIFY = 10
POINTS_PER_REPORT = 10
POINTS_PER_BUMP = 10
POINTS_PER_PUNISH = 10  # /warn /ban /kick /timeout /unwarn
BANNER_URL = os.environ.get("BANNER_URL", "").strip()  # optional: link to a banner image (only used if there is no banner.gif next to bot.py)
BANNER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.gif")  # animated ELITE banner shown under the leaderboard
DISBOARD_ID = 302050872383242240  # the Disboard bot that answers /bump

# ---------- DATABASE ----------
db = sqlite3.connect(DB_PATH)
db.execute("""CREATE TABLE IF NOT EXISTS staff (
    user_id INTEGER PRIMARY KEY,
    voice_seconds INTEGER DEFAULT 0,
    verifications INTEGER DEFAULT 0,
    reports INTEGER DEFAULT 0)""")
for _col in ("bumps", "punishments"):
    try:
        db.execute(f"ALTER TABLE staff ADD COLUMN {_col} INTEGER DEFAULT 0")  # adds the column to an existing database
        db.commit()
    except sqlite3.OperationalError:
        pass  # already there
db.execute("CREATE TABLE IF NOT EXISTS processed (message_id INTEGER, kind TEXT, PRIMARY KEY (message_id, kind))")
db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)")
db.commit()

COLUMNS = {"voice_seconds", "verifications", "reports", "bumps", "punishments"}


def add(user_id: int, column: str, amount: int):
    if column not in COLUMNS:
        raise ValueError(f"bad column {column}")
    db.execute("INSERT OR IGNORE INTO staff (user_id) VALUES (?)", (user_id,))
    db.execute(f"UPDATE staff SET {column} = {column} + ? WHERE user_id = ?", (amount, user_id))
    db.commit()


def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def points(voice_seconds, verifications, reports, bumps=0, punishments=0):
    return (int(voice_seconds / 3600 * POINTS_PER_HOUR) + verifications * POINTS_PER_VERIFY
            + reports * POINTS_PER_REPORT + bumps * POINTS_PER_BUMP + punishments * POINTS_PER_PUNISH)


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
        await restore_backup()
        voice_tick.start()
        update_leaderboard.start()
        backup_db.start()


bot = Bot()


# ---------- BACKUP / RESTORE (XP can never be lost on redeploy) ----------
_last_backup = None
_restore_ok = True


async def _backup_channel():
    ch = bot.get_channel(BACKUP_CHANNEL_ID)
    return ch or await bot.fetch_channel(BACKUP_CHANNEL_ID)


async def restore_backup():
    """If this is a brand-new empty database, load the newest backup from Discord."""
    global _restore_ok
    if get_setting("initialized") is not None:
        return  # database already has data
    if BACKUP_CHANNEL_ID:
        try:
            ch = await _backup_channel()
            async for m in ch.history(limit=50):
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
            _restore_ok = False  # don't start overwriting backups with an empty DB
            return
    set_setting("initialized", 1)


@tasks.loop(minutes=5)
async def backup_db():
    global _last_backup
    if not BACKUP_CHANNEL_ID or not _restore_ok:
        return
    snap = repr(db.execute("SELECT * FROM staff ORDER BY user_id").fetchall()) + \
        repr(db.execute("SELECT * FROM settings ORDER BY key").fetchall())
    if snap == _last_backup:
        return  # nothing changed
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
    for m in old[5:]:  # keep only the 5 newest
        try:
            await m.delete()
        except discord.HTTPException:
            pass


@backup_db.before_loop
async def _backup_wait():
    await bot.wait_until_ready()


@backup_db.error
async def _backup_err(error):
    traceback.print_exception(type(error), error, error.__traceback__)


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
    print(f"Message Content Intent: {'ON' if intents.message_content else 'OFF -> verify/report/bump/punishment points will NOT count'}", flush=True)
    for name, cid in (("VERIFY", VERIFY_CHANNEL_ID), ("REPORT", REPORT_CHANNEL_ID), ("PUNISH", PUNISH_CHANNEL_ID), ("BACKUP", BACKUP_CHANNEL_ID)):
        ch = bot.get_channel(cid) if cid else None
        print(f"{name}_CHANNEL_ID={cid} -> {('#' + ch.name) if ch else 'NOT SET or the bot cannot see this channel'}", flush=True)
    print(f"Staff role IDs: {sorted(STAFF_ROLE_IDS)}", flush=True)


def is_staff(member) -> bool:
    roles = getattr(member, "roles", None)
    if not roles:
        return False
    return any(r.id in STAFF_ROLE_IDS for r in roles)


def is_admin(member) -> bool:
    """High rank: server Administrator or one of ADMIN_ROLE_IDS."""
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
                if m.bot or not is_staff(m) or m.voice is None:
                    continue
                if m.voice.self_deaf or m.voice.deaf:
                    continue
                add(m.id, "voice_seconds", 60)


@voice_tick.before_loop
async def _voice_wait():
    await bot.wait_until_ready()


@voice_tick.error
async def _voice_err(error):
    traceback.print_exception(type(error), error, error.__traceback__)


# ---------- LEADERBOARD PANEL ----------
EMBED_COLOR = 0x8B5CF6


def ranked(guild=None):
    """Every CURRENT staff member (even with 0 pts), sorted by points:
    (user_id, points, voice_seconds, verifications, reports, bumps, punishments)."""
    data = {r[0]: r[1:] for r in db.execute(
        "SELECT user_id, voice_seconds, verifications, reports, bumps, punishments FROM staff")}
    if guild is not None:
        ids = {m.id for m in guild.members if not m.bot and is_staff(m)}
    else:
        ids = set(data)
    out = []
    for uid in ids:
        vs, ver, rep, b, pu = data.get(uid, (0, 0, 0, 0, 0))
        out.append((uid, points(vs, ver, rep, b, pu), vs, ver, rep, b, pu))
    out.sort(key=lambda r: (-r[1], -r[2], r[0]))
    return out


def fmt_time(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m"


def bar(value: int, top: int, size: int = 8) -> str:
    """Little progress bar relative to the leader."""
    filled = round(size * value / top) if top else 0
    return "\u25b0" * filled + "\u25b1" * (size - filled)


def build_embed(guild: discord.Guild, banner: bool = False) -> discord.Embed:
    all_rows = ranked(guild)
    rows = all_rows[:TOP_N]
    embed = discord.Embed(
        title=f"{em('crown')}STAFF LEADERBOARD",
        description=(
            "Earn points by staying active in voice, verifying members, resolving reports, bumping the server and punishing rule breakers.\n"
            f"`{POINTS_PER_HOUR} pts / hour in VC`  \u00b7  `{POINTS_PER_VERIFY} pts / verification`  \u00b7  "
            f"`{POINTS_PER_REPORT} pts / report`  \u00b7  `{POINTS_PER_BUMP} pts / bump`  \u00b7  "
            f"`{POINTS_PER_PUNISH} pts / punishment`\n\u200b"
        ),
        color=EMBED_COLOR,
    )

    if not rows:
        embed.description = "*Nobody has any points yet.*\nJoin a voice channel and start earning!"
    else:
        # podium: three big columns side by side
        places = ["1st place", "2nd place", "3rd place"]
        for i, (uid, p, vs, *_rest) in enumerate(rows[:3]):
            embed.add_field(
                name=f"{em('crown') if i == 0 else ''}{places[i]}",
                value=f"<@{uid}>\n**{p:,}** pts\n`{fmt_time(vs)}` in voice",
                inline=True,
            )
        # everyone else, with a bar showing how close they are to the leader
        if len(rows) > 3:
            top = rows[0][1]
            rest = "\n".join(
                f"`#{i + 4:>2}`  <@{uid}>  `{bar(p, top)}`  **{p:,}** pts"
                for i, (uid, p, *_r) in enumerate(rows[3:])
            )
            embed.add_field(name=f"{em('spark')}Rankings", value=rest, inline=False)

        # team totals
        embed.add_field(
            name=f"{em('hourglass')}Team totals",
            value=(
                f"`{len(all_rows)}` staff  \u00b7  `{sum(r[1] for r in all_rows):,}` pts  \u00b7  "
                f"`{fmt_time(sum(r[2] for r in all_rows))}` in voice\n"
                f"`{sum(r[3] for r in all_rows)}` verifications  \u00b7  `{sum(r[4] for r in all_rows)}` reports  \u00b7  "
                f"`{sum(r[5] for r in all_rows)}` bumps  \u00b7  `{sum(r[6] for r in all_rows)}` punishments"
            ),
            inline=False,
        )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if banner:
        embed.set_image(url="attachment://banner.gif")  # the banner.gif sent together with the panel
    elif BANNER_URL:
        embed.set_image(url=BANNER_URL)
    embed.set_footer(text="Updates every minute  \u00b7  Press the button to see your own stats")
    embed.timestamp = discord.utils.utcnow()
    return embed


def build_stats_embed(member: discord.Member) -> discord.Embed:
    rows = ranked(member.guild)
    pos = next((i for i, r in enumerate(rows) if r[0] == member.id), None)
    if pos is None:
        total, vs, ver, rep, bump, pun = 0, 0, 0, 0, 0, 0
        rank_text = "Unranked"
    else:
        _, total, vs, ver, rep, bump, pun = rows[pos]
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
    embed.add_field(name=f"{em('warn')}Punishments", value=f"{pun}\n`{pun * POINTS_PER_PUNISH:,} pts`", inline=True)

    if pos == 0:
        embed.add_field(name=f"{em('hourglass')}Progress", value="You're **#1** \u2014 keep it up!", inline=False)
    elif pos is not None:
        gap = rows[pos - 1][1] - total
        embed.add_field(name=f"{em('hourglass')}Progress", value=f"**{gap:,} pts** to reach **#{pos}**", inline=False)
    embed.set_footer(text=f"1h in VC = {POINTS_PER_HOUR} pts  \u00b7  verification / report / bump / punishment = {POINTS_PER_VERIFY} pts")
    return embed


# ---------- FULL LEADERBOARD (high rank) ----------
def build_full_embed(page: int, guild=None) -> tuple[discord.Embed, int]:
    rows = ranked(guild)
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    embed = discord.Embed(title=f"{em('crown')}FULL STAFF LEADERBOARD", color=EMBED_COLOR)
    if not chunk:
        embed.description = "*Nobody has any points yet.*"
    else:
        lines = []
        for i, (uid, p, vs, ver, rep, b, pu) in enumerate(chunk):
            n = page * PAGE_SIZE + i + 1
            lines.append(
                f"`#{n:>2}`  <@{uid}>  **{p:,}** pts\n"
                f"\u2003`{fmt_time(vs)}` VC  \u00b7  `{ver}` ver  \u00b7  `{rep}` rep  \u00b7  `{b}` bump  \u00b7  `{pu}` pun"
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
        super().__init__(timeout=None)  # persistent: keeps working after restarts
        e = EMOJI.get("profile")
        if e:
            self.my_stats.emoji = discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated)

    @discord.ui.button(label="My Stats", style=discord.ButtonStyle.primary, custom_id="lb:mystats")
    async def my_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user) and not is_admin(interaction.user):
            return await interaction.response.send_message("This is for staff members only.", ephemeral=True)
        await interaction.response.send_message(embed=build_stats_embed(interaction.user), ephemeral=True)

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
        await channel.get_partial_message(msg_id).edit(embed=build_embed(channel.guild, banner=bool(get_setting("lb_banner"))))
    except discord.NotFound:
        pass  # panel was deleted; run /xp_leaderboard again
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
@admin_only()
async def setup_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # answer within 3s no matter what
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
            text = f"Reset all points for {self.target.mention}."
        else:
            db.execute("DELETE FROM staff")
            text = "Reset points for **everyone**."
        db.commit()
        self.stop()
        await interaction.response.edit_message(content=f"{text} The panel updates within a minute.", view=None)

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


# ---------- WATCH THE VERIFICATION / REPORT / PUNISHMENT BOTS ----------
MENTION = re.compile(r"<@!?(\d+)>")


async def handle_message(msg: discord.Message):
    if not msg.embeds or msg.guild is None:
        return
    fields = {f.name.strip().lower(): f.value for f in msg.embeds[0].fields}

    if msg.channel.id == VERIFY_CHANNEL_ID:
        kind, column = "verify", "verifications"
        value = fields.get("verified")  # field the verify bot adds with the staff mention
        if not value:
            value = next((v for k, v in fields.items()
                          if any(w in k for w in ("verified", "verifier", "staff", "moderator", "handled")) and MENTION.search(v)), None)
        if not value:
            print(f"[VERIFY DEBUG] embed in verify channel but no staff mention found. fields={dict(fields)} "
                  f"description={msg.embeds[0].description!r}", flush=True)
            return
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


async def handle_punish(msg: discord.Message):
    """Punishment bot cards (/warn /ban /kick /timeout /unwarn) - the mod who ran the command gets points."""
    if not PUNISH_CHANNEL_ID or msg.guild is None or msg.channel.id != PUNISH_CHANNEL_ID or not msg.author.bot:
        return
    staff_id = None
    meta = getattr(msg, "interaction_metadata", None)
    user = getattr(meta, "user", None) if meta else None
    if user is None and getattr(msg, "interaction", None):
        user = msg.interaction.user
    if user is not None:
        staff_id = user.id  # the person who typed the slash command
    elif msg.embeds:
        keys = ("moderator", "mod", "staff", "issued by", "by")
        for f in msg.embeds[0].fields:
            if any(k in f.name.lower() for k in keys):
                m = MENTION.search(f.value)
                if m:
                    staff_id = int(m.group(1))
                    break
    if staff_id is None:
        print(f"[PUNISH DEBUG] card found but couldn't tell who the mod is. "
              f"fields={[(f.name, f.value) for f in msg.embeds[0].fields] if msg.embeds else None}", flush=True)
        return
    member = msg.guild.get_member(staff_id)
    if member is None or not is_staff(member):
        print(f"[PUNISH DEBUG] mod {staff_id} skipped: not in server cache or has no role from STAFF_ROLE_IDS", flush=True)
        return
    cur = db.execute("INSERT OR IGNORE INTO processed (message_id, kind) VALUES (?, ?)", (msg.id, "punish"))
    db.commit()
    if cur.rowcount:
        add(staff_id, "punishments", 1)


@bot.event
async def on_message(msg: discord.Message):
    if msg.channel.id in (VERIFY_CHANNEL_ID, REPORT_CHANNEL_ID, PUNISH_CHANNEL_ID):
        print(f"[SEEN] channel={msg.channel.id} from={msg.author} embeds={len(msg.embeds)} "
              f"interaction_user={getattr(getattr(msg, 'interaction_metadata', None), 'user', None)}", flush=True)
    await handle_bump(msg)
    await handle_punish(msg)
    await handle_message(msg)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    from_disboard = str((payload.data.get("author") or {}).get("id")) == str(DISBOARD_ID)
    watched = {VERIFY_CHANNEL_ID, REPORT_CHANNEL_ID, PUNISH_CHANNEL_ID}
    if payload.channel_id not in watched and not from_disboard:
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    await handle_bump(msg)
    await handle_punish(msg)
    await handle_message(msg)


@bot.tree.command(name="xp_points", description="Check your (or someone's) points")
@app_commands.guild_only()
async def points_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    row = db.execute("SELECT voice_seconds, verifications, reports, bumps, punishments FROM staff WHERE user_id = ?",
                     (member.id,)).fetchone() or (0, 0, 0, 0, 0)
    await interaction.response.send_message(
        f"**{member.display_name}**: {points(*row):,} pts "
        f"({em('speaker')}{fmt_time(row[0])} \u00b7 {em('check')}{row[1]} \u00b7 {em('warn')}{row[2]} \u00b7 "
        f"{em('bump')}{row[3]} bumps \u00b7 {em('warn')}{row[4]} punishments)", ephemeral=True)


try:
    bot.run(TOKEN)
except discord.PrivilegedIntentsRequired:
    if intents.message_content:
        print("WARNING: 'Message Content Intent' is OFF in the Developer Portal (Bot tab -> Privileged Gateway Intents). "
              "Restarting without it: commands work, but verify / report / bump / punishment points will NOT be counted until you turn it on.",
              flush=True)
        os.environ["NO_MESSAGE_CONTENT"] = "1"
        os.execv(sys.executable, [sys.executable] + sys.argv)
    raise SystemExit("Turn ON 'Server Members Intent' for this bot in the Discord Developer Portal "
                     "(Bot tab -> Privileged Gateway Intents), then redeploy.")
