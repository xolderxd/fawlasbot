"""
TELEGRAM MODERATION BOT v4.0
pip install "python-telegram-bot[job-queue]"
python bot.py
"""

import logging, random, re, sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Update, ChatPermissions, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ══════════════════════════════════════════════
BOT_TOKEN      = "YOUR_BOT_TOKEN_HERE"
DB_PATH        = "modbot.db"
DEFAULT_PREFIX = "~"
NOPREFIX       = "<>"   # special sentinel stored in DB when prefix-less mode
FARM_COOLDOWN  = 3600
# ══════════════════════════════════════════════

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            username   TEXT DEFAULT '',
            full_name  TEXT DEFAULT '',
            nickname   TEXT DEFAULT '',
            rank       INTEGER DEFAULT 0,
            ck         INTEGER DEFAULT 0,
            farm_last  TEXT,
            joined_at  TEXT DEFAULT (datetime('now')),
            msgs_total INTEGER DEFAULT 0,
            msgs_day   INTEGER DEFAULT 0,
            msgs_week  INTEGER DEFAULT 0,
            last_date  TEXT DEFAULT '',
            last_week  TEXT DEFAULT '',
            clan_id    INTEGER DEFAULT NULL,
            PRIMARY KEY (user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS warnings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            mod_id    INTEGER NOT NULL,
            reason    TEXT DEFAULT '',
            issued_at TEXT DEFAULT (datetime('now')),
            active    INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS punishments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            mod_id     INTEGER NOT NULL,
            type       TEXT NOT NULL,
            reason     TEXT DEFAULT '',
            issued_at  TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            active     INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS config (
            chat_id  INTEGER NOT NULL,
            key      TEXT NOT NULL,
            min_rank INTEGER DEFAULT 1,
            disabled INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, key)
        );
        CREATE TABLE IF NOT EXISTS contributions (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            amount  INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            prefix  TEXT DEFAULT '~'
        );
        CREATE TABLE IF NOT EXISTS clans (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            name       TEXT NOT NULL,
            owner_id   INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(chat_id, name)
        );
        CREATE TABLE IF NOT EXISTS clan_members (
            clan_id   INTEGER NOT NULL,
            user_id   INTEGER NOT NULL,
            chat_id   INTEGER NOT NULL,
            slot      INTEGER NOT NULL,
            joined_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (clan_id, user_id)
        );
        """)
    log.info("DB ready.")

# ──────────────────────────────────────────────
#  PREFIX
# ──────────────────────────────────────────────
_pcache: dict[int, str] = {}

def get_prefix(chat_id: int) -> str:
    if chat_id in _pcache:
        return _pcache[chat_id]
    with db() as c:
        r = c.execute("SELECT prefix FROM chat_settings WHERE chat_id=?", (chat_id,)).fetchone()
    p = r["prefix"] if r else DEFAULT_PREFIX
    _pcache[chat_id] = p
    return p

def set_prefix(chat_id: int, prefix: str):
    _pcache[chat_id] = prefix
    with db() as c:
        c.execute("""
            INSERT INTO chat_settings (chat_id, prefix) VALUES (?,?)
            ON CONFLICT(chat_id) DO UPDATE SET prefix=?
        """, (chat_id, prefix, prefix))

# ──────────────────────────────────────────────
#  USER HELPERS
# ──────────────────────────────────────────────
def ensure_user(user_id, chat_id, username="", full_name=""):
    with db() as c:
        c.execute("""
            INSERT INTO users (user_id, chat_id, username, full_name)
            VALUES (?,?,?,?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                username  = CASE WHEN excluded.username  != '' THEN excluded.username  ELSE username  END,
                full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE full_name END
        """, (user_id, chat_id, username or "", full_name or ""))

def get_user(user_id, chat_id):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=? AND chat_id=?", (user_id, chat_id)).fetchone()

def get_rank(user_id, chat_id) -> int:
    r = get_user(user_id, chat_id)
    return r["rank"] if r else 0

def set_rank(user_id, chat_id, rank):
    with db() as c:
        c.execute("UPDATE users SET rank=? WHERE user_id=? AND chat_id=?", (rank, user_id, chat_id))

def add_ck(user_id, chat_id, amount):
    with db() as c:
        c.execute("UPDATE users SET ck=MAX(0,ck+?) WHERE user_id=? AND chat_id=?", (amount, user_id, chat_id))

def get_ck(user_id, chat_id) -> int:
    r = get_user(user_id, chat_id)
    return r["ck"] if r else 0

def get_display_name(user_id, chat_id, un="", fn="") -> str:
    r = get_user(user_id, chat_id)
    if r:
        if r["nickname"]: return r["nickname"]
        if r["username"]: return f"@{r['username']}"
        if r["full_name"]: return r["full_name"]
    return f"@{un}" if un else (fn or str(user_id))

def tick_messages(user_id, chat_id):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week  = datetime.now(timezone.utc).strftime("%Y-W%W")
    with db() as c:
        r = c.execute("SELECT last_date,last_week FROM users WHERE user_id=? AND chat_id=?", (user_id, chat_id)).fetchone()
        if not r: return
        rd = 1 if r["last_date"] != today else 0
        rw = 1 if r["last_week"] != week  else 0
        c.execute("""UPDATE users SET
            msgs_total=msgs_total+1,
            msgs_day  =CASE WHEN ?=1 THEN 1 ELSE msgs_day+1 END,
            msgs_week =CASE WHEN ?=1 THEN 1 ELSE msgs_week+1 END,
            last_date=?, last_week=?
            WHERE user_id=? AND chat_id=?""",
            (rd, rw, today, week, user_id, chat_id))

def user_html_link(row) -> str:
    nick = row["nickname"] or row["full_name"] or row["username"] or str(row["user_id"])
    if row["username"]:
        return f'<a href="https://t.me/{row["username"]}">{nick}</a>'
    return f'<a href="tg://user?id={row["user_id"]}">{nick}</a>'

# ──────────────────────────────────────────────
#  RANKS / CONFIG
# ──────────────────────────────────────────────
RANK_NAMES = {0:"Участник",1:"Мл. модератор",2:"Модератор",3:"Админ",4:"Соучредитель",5:"Основатель"}
RANK_BADGE = {0:"⬜",1:"🟦",2:"🟩",3:"🟨",4:"🟧",5:"🟥"}

DEFAULT_RANKS = {
    "варн":1,"пред":1,"снять варн":1,"снять пред":1,
    "мут":2,"заткнуть":2,"анмут":2,"говори":2,
    "бан":3,"чс":3,"анбан":3,"разбан":3,
    "конфиг":4,"ранг":4,"повысить":4,"понизить":4,"снять ранг":4,"префикс":4,
    "фарма":0,"мешок":0,"профиль":0,"перевести":0,"вклад":0,
    "админы":0,"стафф":0,"кто админ":0,"клан":0,"ник":0,"рулетка":0,
    "помощь":0,"команды":0,"help":0,
}
TWO_WORD = {"снять варн","снять пред","снять ранг","кто админ"}

def cmd_min_rank(chat_id, cmd):
    with db() as c:
        r = c.execute("SELECT min_rank,disabled FROM config WHERE chat_id=? AND key=?", (chat_id, cmd)).fetchone()
    if r: return r["min_rank"], bool(r["disabled"])
    return DEFAULT_RANKS.get(cmd, 5), False

# ──────────────────────────────────────────────
#  PERMISSIONS
# ──────────────────────────────────────────────
async def sync_owner(bot, user_id, chat_id) -> int:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        if m.status == ChatMember.OWNER and get_rank(user_id, chat_id) < 5:
            set_rank(user_id, chat_id, 5)
            return 5
    except TelegramError:
        pass
    return get_rank(user_id, chat_id)

async def check_perm(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, cmd: str) -> bool:
    u = upd.effective_user
    c = upd.effective_chat
    ensure_user(u.id, c.id, u.username, u.full_name)
    rank = await sync_owner(ctx.bot, u.id, c.id)
    min_r, disabled = cmd_min_rank(c.id, cmd)
    p = get_prefix(c.id)
    pd = "" if p == NOPREFIX else p
    if disabled:
        await upd.message.reply_text(f"❌ Команда `{pd}{cmd}` отключена.", parse_mode=ParseMode.MARKDOWN)
        return False
    if rank < min_r:
        await upd.message.reply_text(
            f"❌ Нужен ранг *{min_r} — {RANK_NAMES[min_r]}*\nВаш: *{rank} — {RANK_NAMES[rank]}*",
            parse_mode=ParseMode.MARKDOWN)
        return False
    return True

# ──────────────────────────────────────────────
#  PARSE HELPERS
# ──────────────────────────────────────────────
def parse_cmd(text: str, prefix: str):
    if not text:
        return None, []
    if prefix == NOPREFIX:
        raw = text
    else:
        if not text.startswith(prefix):
            return None, []
        raw = text[len(prefix):]
    parts = raw.split()
    if not parts:
        return None, []
    two = (parts[0] + " " + parts[1]).lower() if len(parts) >= 2 else ""
    if two in TWO_WORD:
        return two, parts[2:]
    return parts[0].lower(), parts[1:]

def parse_dur(s: str) -> Optional[timedelta]:
    m = re.fullmatch(r"(\d+)([мчдл])", s.strip().lower())
    if not m: return None
    v, u = int(m.group(1)), m.group(2)
    return {"м":timedelta(minutes=v),"ч":timedelta(hours=v),"д":timedelta(days=v),"л":timedelta(days=v*365)}.get(u)

def dur_label(td: timedelta) -> str:
    s = int(td.total_seconds())
    if s < 3600:      return f"{s//60} мин."
    if s < 86400:     return f"{s//3600} ч."
    if s < 86400*365: return f"{s//86400} д."
    return f"{s//(86400*365)} г."

async def resolve(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args: list) -> Optional[dict]:
    chat_id = upd.effective_chat.id
    if upd.message.reply_to_message:
        u = upd.message.reply_to_message.from_user
        return {"user_id":u.id,"username":u.username or "","full_name":u.full_name}
    if not args:
        await upd.message.reply_text("❌ Укажите @пользователя или ID.")
        return None
    raw = args[0].lstrip("@")
    if raw.lstrip("-").isdigit():
        uid = int(raw)
        try:
            m = await ctx.bot.get_chat_member(chat_id, uid)
            u = m.user
            return {"user_id":u.id,"username":u.username or "","full_name":u.full_name}
        except TelegramError:
            await upd.message.reply_text("❌ Пользователь не найден в чате.")
            return None
    with db() as c:
        r = c.execute("SELECT user_id,username,full_name FROM users WHERE chat_id=? AND LOWER(username)=?",
                      (chat_id, raw.lower())).fetchone()
    if r:
        return {"user_id":r["user_id"],"username":r["username"],"full_name":r["full_name"]}
    try:
        m = await ctx.bot.get_chat_member(chat_id, f"@{raw}")
        u = m.user
        ensure_user(u.id, chat_id, u.username or "", u.full_name)
        return {"user_id":u.id,"username":u.username or "","full_name":u.full_name}
    except TelegramError:
        pass
    await upd.message.reply_text("❌ Не найден. Используй @username, ID или ответь на сообщение.")
    return None

def uname(t: dict) -> str:
    return f"@{t['username']}" if t.get("username") else (t.get("full_name") or str(t["user_id"]))

def aoff(reply: bool) -> int:
    return 0 if reply else 1

# ──────────────────────────────────────────────
#  CLAN HELPERS
# ──────────────────────────────────────────────
def get_clan(clan_id: int):
    with db() as c:
        return c.execute("SELECT * FROM clans WHERE id=?", (clan_id,)).fetchone()

def get_user_clan_id(user_id: int, chat_id: int) -> Optional[int]:
    r = get_user(user_id, chat_id)
    return r["clan_id"] if r else None

def next_slot(clan_id: int) -> int:
    with db() as c:
        r = c.execute("SELECT MAX(slot) as m FROM clan_members WHERE clan_id=?", (clan_id,)).fetchone()
    return (r["m"] or 0) + 1

def clan_members_list(clan_id: int, chat_id: int):
    with db() as c:
        return c.execute("""
            SELECT cm.slot,cm.user_id,u.username,u.full_name,u.nickname
            FROM clan_members cm
            JOIN users u ON u.user_id=cm.user_id AND u.chat_id=cm.chat_id
            WHERE cm.clan_id=? AND cm.chat_id=?
            ORDER BY cm.slot ASC
        """, (clan_id, chat_id)).fetchall()

def clan_info_keyboard(clan_id: int, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    user_clan = get_user_clan_id(user_id, chat_id)
    in_this   = user_clan == clan_id
    in_any    = user_clan is not None
    row = [InlineKeyboardButton("👥 Участники", callback_data=f"cmembers:{clan_id}:{chat_id}")]
    if not in_this:
        if not in_any:
            row.append(InlineKeyboardButton("✅ Вступить", callback_data=f"cjoin:{clan_id}:{user_id}:{chat_id}"))
        else:
            row.append(InlineKeyboardButton("⚠️ Уже в клане", callback_data=f"calready:{user_id}"))
    return InlineKeyboardMarkup([row])

def build_clan_info_text(clan, members) -> str:
    with db() as c:
        owner_row = c.execute(
            "SELECT username,full_name,nickname,user_id FROM users WHERE user_id=? AND chat_id=?",
            (clan["owner_id"], clan["chat_id"])
        ).fetchone()
    if owner_row:
        if owner_row["username"]:
            owner_str = f'<a href="https://t.me/{owner_row["username"]}">{owner_row["nickname"] or owner_row["full_name"] or owner_row["username"]}</a>'
        else:
            owner_str = f'<a href="tg://user?id={clan["owner_id"]}">{owner_row["nickname"] or owner_row["full_name"] or str(clan["owner_id"])}</a>'
    else:
        owner_str = str(clan["owner_id"])
    return (
        f"🏰 <b>{clan['name']}</b>\n"
        f"👑 Владелец: {owner_str}\n"
        f"👥 Участников: {len(members)}\n"
        f"📅 Создан: {clan['created_at'][:10]}"
    )

# ──────────────────────────────────────────────
#  HANDLERS
# ──────────────────────────────────────────────

# NICKNAME
async def h_nick(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    u = upd.effective_user; chat = upd.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    if not args:
        with db() as c:
            c.execute("UPDATE users SET nickname='' WHERE user_id=? AND chat_id=?", (u.id, chat.id))
        await upd.message.reply_text("✅ Ник сброшен.")
        return
    nick = " ".join(args).strip()
    if len(nick) > 32:
        await upd.message.reply_text("❌ Максимум 32 символа.")
        return
    nick = nick.replace("*","").replace("_","").replace("`","").replace("[","").replace("]","")
    with db() as c:
        c.execute("UPDATE users SET nickname=? WHERE user_id=? AND chat_id=?", (nick, u.id, chat.id))
    await upd.message.reply_text(f"✅ Твой ник: *{nick}*", parse_mode=ParseMode.MARKDOWN)

# PREFIX
async def h_prefix(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat
    if not await check_perm(upd, ctx, "префикс"):
        return
    if not args:
        p = get_prefix(chat.id)
        pd = "*(без префикса)*" if p == NOPREFIX else f"`{p}`"
        await upd.message.reply_text(
            f"ℹ️ Текущий префикс: {pd}\n"
            f"Установить: `~префикс .`\n"
            f"Без префикса: `~префикс <>`\n"
            f"Вернуть `~`: `~префикс ~`",
            parse_mode=ParseMode.MARKDOWN)
        return
    raw = args[0].strip('"').strip("'").strip()
    if raw == "<>":
        set_prefix(chat.id, NOPREFIX)
        await upd.message.reply_text(
            "✅ Режим без префикса включён!\n"
            "Теперь пиши просто: `профиль`, `фарма`, `клан` и т.д.\n"
            "Чтобы вернуть префикс: `~префикс ~`",
            parse_mode=ParseMode.MARKDOWN)
        return
    if len(raw) == 0 or len(raw) > 3:
        await upd.message.reply_text("❌ Префикс: 1–3 символа. Или `<>` для режима без префикса.", parse_mode=ParseMode.MARKDOWN)
        return
    set_prefix(chat.id, raw)
    await upd.message.reply_text(f"✅ Новый префикс: `{raw}`\nПример: `{raw}профиль`", parse_mode=ParseMode.MARKDOWN)

# PROFILE
async def h_profile(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat
    u = upd.message.reply_to_message.from_user if upd.message.reply_to_message else upd.effective_user
    ensure_user(u.id, chat.id, u.username, u.full_name)
    await sync_owner(ctx.bot, u.id, chat.id)
    r = get_user(u.id, chat.id)
    if not r: return
    with db() as c:
        w  = c.execute("SELECT COUNT(*) as n FROM warnings WHERE chat_id=? AND user_id=? AND active=1", (chat.id, u.id)).fetchone()
        cv = c.execute("SELECT amount FROM contributions WHERE user_id=? AND chat_id=?", (u.id, chat.id)).fetchone()
        cl = None
        if r["clan_id"]:
            cl = c.execute("SELECT name FROM clans WHERE id=?", (r["clan_id"],)).fetchone()
    rank = r["rank"]
    nick = r["nickname"]
    name = nick if nick else (f"@{u.username}" if u.username else u.full_name)
    clan_str = f"🏰 {cl['name']}\n" if cl else ""
    txt = (
        f"👤 {name} · <code>{u.id}</code>\n"
        f"{RANK_BADGE[rank]} <b>{RANK_NAMES[rank]}</b> (ранг {rank})\n"
        f"━━━━━━━━━━━━━\n"
        f"{clan_str}"
        f"💰 {r['ck']} ck  🤝 {cv['amount'] if cv else 0} ck вклад\n"
        f"💬 {r['msgs_day']}д / {r['msgs_week']}н / {r['msgs_total']} всего\n"
        f"⚠️ Варнов: {w['n'] if w else 0}  📅 с {r['joined_at'][:10]}"
    )
    await upd.message.reply_text(txt, parse_mode=ParseMode.HTML)

# FARM
async def h_farm(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    u = upd.effective_user; chat = upd.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    r   = get_user(u.id, chat.id)
    now = datetime.now(timezone.utc)
    if r["farm_last"]:
        last = datetime.fromisoformat(r["farm_last"]).replace(tzinfo=timezone.utc)
        diff = (now - last).total_seconds()
        if diff < FARM_COOLDOWN:
            left = int(FARM_COOLDOWN - diff)
            m2, s = divmod(left, 60)
            await upd.message.reply_text(f"⏳ Подожди *{m2}м {s}с*", parse_mode=ParseMode.MARKDOWN)
            return
    if random.random() < 0.05:
        amount = random.randint(300, 600)
        msg = f"🎉 *ХРАНИЛИЩЕ!* +*{amount} ck*!"
    else:
        amount = random.randint(1, 150)
        msg = f"⛏ *+{amount} ck* добыто!"
    add_ck(u.id, chat.id, amount)
    with db() as c:
        c.execute("UPDATE users SET farm_last=? WHERE user_id=? AND chat_id=?", (now.isoformat(), u.id, chat.id))
    await upd.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# BAG
async def h_bag(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    u = upd.effective_user; chat = upd.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    name = get_display_name(u.id, chat.id, u.username, u.full_name)
    await upd.message.reply_text(f"🎒 *{name}*\n💰 *{get_ck(u.id, chat.id)} ck*", parse_mode=ParseMode.MARKDOWN)

# TRANSFER
async def h_transfer(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    u = upd.effective_user; chat = upd.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    target = await resolve(upd, ctx, args)
    if not target: return
    if target["user_id"] == u.id:
        await upd.message.reply_text("❌ Нельзя перевести себе.")
        return
    o = aoff(bool(upd.message.reply_to_message))
    amt_s = args[o] if len(args) > o else None
    if not amt_s or not amt_s.isdigit():
        p = get_prefix(chat.id); pd = "" if p == NOPREFIX else p
        await upd.message.reply_text(f"📖 `{pd}перевести @user 100`", parse_mode=ParseMode.MARKDOWN)
        return
    amt = int(amt_s)
    if amt <= 0: await upd.message.reply_text("❌ Сумма > 0."); return
    ck = get_ck(u.id, chat.id)
    if ck < amt:
        await upd.message.reply_text(f"❌ У тебя только *{ck} ck*", parse_mode=ParseMode.MARKDOWN)
        return
    ensure_user(target["user_id"], chat.id, target["username"], target["full_name"])
    add_ck(u.id, chat.id, -amt); add_ck(target["user_id"], chat.id, amt)
    sender = get_display_name(u.id, chat.id, u.username, u.full_name)
    await upd.message.reply_text(f"✅ *{sender}* → {uname(target)}: *{amt} ck*", parse_mode=ParseMode.MARKDOWN)

# CONTRIBUTE
async def h_contribute(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    u = upd.effective_user; chat = upd.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    if not args or not args[0].isdigit():
        with db() as c:
            top = c.execute("""SELECT u.username,u.full_name,u.nickname,u.user_id,cv.amount
                FROM contributions cv JOIN users u ON u.user_id=cv.user_id AND u.chat_id=cv.chat_id
                WHERE cv.chat_id=? ORDER BY cv.amount DESC LIMIT 10""", (chat.id,)).fetchall()
        lines = ["🤝 <b>Топ вкладчиков:</b>\n"]
        for i, row in enumerate(top, 1):
            lines.append(f"{i}. {user_html_link(row)} — {row['amount']} ck")
        if not top: lines.append("Пока никто.")
        await upd.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    amt = int(args[0])
    if amt <= 0: await upd.message.reply_text("❌ Сумма > 0."); return
    ck = get_ck(u.id, chat.id)
    if ck < amt:
        await upd.message.reply_text(f"❌ У тебя только *{ck} ck*", parse_mode=ParseMode.MARKDOWN)
        return
    add_ck(u.id, chat.id, -amt)
    with db() as c:
        c.execute("INSERT INTO contributions(user_id,chat_id,amount) VALUES(?,?,?) ON CONFLICT(user_id,chat_id) DO UPDATE SET amount=amount+?",
                  (u.id, chat.id, amt, amt))
    name = get_display_name(u.id, chat.id, u.username, u.full_name)
    await upd.message.reply_text(f"🤝 *{name}* вложил *{amt} ck* в чат!", parse_mode=ParseMode.MARKDOWN)

# ADMINS
async def h_admins(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat
    with db() as c:
        rows = c.execute("SELECT user_id,username,full_name,nickname,rank FROM users WHERE chat_id=? AND rank>=1",
                         (chat.id,)).fetchall()
    by_rank: dict[int, list] = {r: [] for r in range(5, 0, -1)}
    for row in rows:
        by_rank.setdefault(row["rank"], []).append(row)
    lines = ["👥 <b>Администрация</b>\n"]
    for rank in range(5, 0, -1):
        lines.append(f"{RANK_BADGE[rank]} <b>[{rank}] — {RANK_NAMES[rank]}</b>")
        members = by_rank.get(rank, [])
        if not members:
            lines.append("  <i>Никого нет...</i>")
        else:
            for row in members:
                lines.append(f"  {user_html_link(row)}")
        lines.append("")
    await upd.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# PROMOTE
async def h_promote(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat; caller = upd.effective_user
    if not await check_perm(upd, ctx, "повысить"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    cr = get_rank(caller.id, chat.id); tr = get_rank(target["user_id"], chat.id)
    o = aoff(bool(upd.message.reply_to_message))
    ra = args[o] if len(args) > o and args[o].isdigit() else None
    nr = int(ra) if ra else min(5, tr + 1)
    nr = max(0, min(5, nr))
    if cr < 5 and nr >= cr:
        await upd.message.reply_text("❌ Нельзя выдать ранг ≥ своего!"); return
    if cr < 5 and tr >= cr:
        await upd.message.reply_text("❌ Нельзя повысить равного/выше!"); return
    ensure_user(target["user_id"], chat.id, target["username"], target["full_name"])
    set_rank(target["user_id"], chat.id, nr)
    await upd.message.reply_text(f"⬆️ {uname(target)} → {RANK_BADGE[nr]} *{RANK_NAMES[nr]}*", parse_mode=ParseMode.MARKDOWN)

# DEMOTE
async def h_demote(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat; caller = upd.effective_user
    if not await check_perm(upd, ctx, "понизить"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    cr = get_rank(caller.id, chat.id); tr = get_rank(target["user_id"], chat.id)
    if cr < 5 and tr >= cr:
        await upd.message.reply_text("❌ Нельзя понизить равного/выше!"); return
    o = aoff(bool(upd.message.reply_to_message))
    ra = args[o] if len(args) > o and args[o].isdigit() else None
    nr = int(ra) if ra else max(0, tr - 1)
    if tr <= 1: nr = 0
    nr = max(0, min(5, nr))
    ensure_user(target["user_id"], chat.id, target["username"], target["full_name"])
    set_rank(target["user_id"], chat.id, nr)
    await upd.message.reply_text(f"⬇️ {uname(target)} → {RANK_BADGE[nr]} *{RANK_NAMES[nr]}*", parse_mode=ParseMode.MARKDOWN)

# RESET RANK
async def h_reset_rank(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat; caller = upd.effective_user
    if not await check_perm(upd, ctx, "снять ранг"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    cr = get_rank(caller.id, chat.id); tr = get_rank(target["user_id"], chat.id)
    if cr < 5 and tr >= cr:
        await upd.message.reply_text("❌ Нельзя снять ранг с равного/выше!"); return
    ensure_user(target["user_id"], chat.id, target["username"], target["full_name"])
    set_rank(target["user_id"], chat.id, 0)
    await upd.message.reply_text(f"🚫 Ранг снят с {uname(target)}. Теперь ⬜ Участник.", parse_mode=ParseMode.MARKDOWN)

# SET RANK
async def h_set_rank(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat; caller = upd.effective_user
    if not await check_perm(upd, ctx, "ранг"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    o = aoff(bool(upd.message.reply_to_message))
    ra = args[o] if len(args) > o else None
    if not ra or not ra.isdigit():
        p = get_prefix(chat.id); pd = "" if p == NOPREFIX else p
        await upd.message.reply_text(f"📖 `{pd}ранг @user 0-5`", parse_mode=ParseMode.MARKDOWN); return
    nr = max(0, min(5, int(ra)))
    cr = get_rank(caller.id, chat.id); tr = get_rank(target["user_id"], chat.id)
    if cr < 5 and nr >= cr:
        await upd.message.reply_text("❌ Нельзя выдать ранг ≥ своего!"); return
    if cr < 5 and tr >= cr:
        await upd.message.reply_text("❌ Нельзя изменить ранг равного/выше!"); return
    ensure_user(target["user_id"], chat.id, target["username"], target["full_name"])
    set_rank(target["user_id"], chat.id, nr)
    await upd.message.reply_text(f"✅ {uname(target)} → {RANK_BADGE[nr]} *{RANK_NAMES[nr]}*", parse_mode=ParseMode.MARKDOWN)

# WARN
async def h_warn(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat; caller = upd.effective_user
    if not await check_perm(upd, ctx, "варн"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    o = aoff(bool(upd.message.reply_to_message))
    reason = " ".join(args[o:]) if len(args) > o else "не указана"
    with db() as c:
        c.execute("INSERT INTO warnings(chat_id,user_id,mod_id,reason) VALUES(?,?,?,?)",
                  (chat.id, target["user_id"], caller.id, reason))
        cnt = c.execute("SELECT COUNT(*) as n FROM warnings WHERE chat_id=? AND user_id=? AND active=1",
                        (chat.id, target["user_id"])).fetchone()["n"]
    await upd.message.reply_text(f"⚠️ {uname(target)} получил варн [{cnt}]\n📝 _{reason}_", parse_mode=ParseMode.MARKDOWN)

# UNWARN
async def h_unwarn(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat
    if not await check_perm(upd, ctx, "снять варн"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    with db() as c:
        row = c.execute("SELECT id FROM warnings WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 1",
                        (chat.id, target["user_id"])).fetchone()
        if not row: await upd.message.reply_text("❌ Нет активных варнов."); return
        c.execute("UPDATE warnings SET active=0 WHERE id=?", (row["id"],))
    await upd.message.reply_text(f"✅ Варн снят с {uname(target)}.", parse_mode=ParseMode.MARKDOWN)

# MUTE
async def h_mute(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat; caller = upd.effective_user
    if not await check_perm(upd, ctx, "мут"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    o = aoff(bool(upd.message.reply_to_message))
    dr = args[o] if len(args) > o else None
    td = parse_dur(dr) if dr else None
    reason = " ".join(args[o+(1 if td else 0):]) if args else "не указана"
    until = datetime.now(timezone.utc) + td if td else None
    try:
        await ctx.bot.restrict_chat_member(chat.id, target["user_id"],
            permissions=ChatPermissions(can_send_messages=False), until_date=until)
    except TelegramError as e:
        await upd.message.reply_text(f"❌ {e}"); return
    with db() as c:
        c.execute("INSERT INTO punishments(chat_id,user_id,mod_id,type,reason,expires_at) VALUES(?,?,?,'mute',?,?)",
                  (chat.id, target["user_id"], caller.id, reason, until.isoformat() if until else None))
    await upd.message.reply_text(
        f"🔇 {uname(target)} — мут *{dur_label(td) if td else 'навсегда'}*\n📝 _{reason}_",
        parse_mode=ParseMode.MARKDOWN)

# UNMUTE
async def h_unmute(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat
    if not await check_perm(upd, ctx, "анмут"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    try:
        await ctx.bot.restrict_chat_member(chat.id, target["user_id"],
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
    except TelegramError as e:
        await upd.message.reply_text(f"❌ {e}"); return
    await upd.message.reply_text(f"🔊 Мут снят с {uname(target)}.", parse_mode=ParseMode.MARKDOWN)

# BAN
async def h_ban(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat; caller = upd.effective_user
    if not await check_perm(upd, ctx, "бан"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    cr = get_rank(caller.id, chat.id); tr = get_rank(target["user_id"], chat.id)
    if tr >= cr: await upd.message.reply_text("❌ Нельзя забанить равного/выше!"); return
    o = aoff(bool(upd.message.reply_to_message))
    dr = args[o] if len(args) > o else None
    td = parse_dur(dr) if dr else None
    reason = " ".join(args[o+(1 if td else 0):]) if args else "не указана"
    until = datetime.now(timezone.utc) + td if td else None
    try:
        await ctx.bot.ban_chat_member(chat.id, target["user_id"], until_date=until)
    except TelegramError as e:
        await upd.message.reply_text(f"❌ {e}"); return
    with db() as c:
        c.execute("INSERT INTO punishments(chat_id,user_id,mod_id,type,reason,expires_at) VALUES(?,?,?,'ban',?,?)",
                  (chat.id, target["user_id"], caller.id, reason, until.isoformat() if until else None))
    await upd.message.reply_text(
        f"🚫 {uname(target)} — бан *{dur_label(td) if td else 'навсегда'}*\n📝 _{reason}_",
        parse_mode=ParseMode.MARKDOWN)

# UNBAN
async def h_unban(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat
    if not await check_perm(upd, ctx, "анбан"): return
    target = await resolve(upd, ctx, args)
    if not target: return
    try:
        await ctx.bot.unban_chat_member(chat.id, target["user_id"])
    except TelegramError as e:
        await upd.message.reply_text(f"❌ {e}"); return
    with db() as c:
        c.execute("UPDATE punishments SET active=0 WHERE chat_id=? AND user_id=? AND type='ban' AND active=1",
                  (chat.id, target["user_id"]))
    await upd.message.reply_text(f"✅ Бан снят с {uname(target)}.", parse_mode=ParseMode.MARKDOWN)

# CONFIG
async def h_config(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    chat = upd.effective_chat
    if not await check_perm(upd, ctx, "конфиг"): return
    p = get_prefix(chat.id); pd = "" if p == NOPREFIX else p
    if not args:
        lines = ["⚙️ *Конфиг команд:*\n"]
        for cmd in sorted(DEFAULT_RANKS):
            mr, dis = cmd_min_rank(chat.id, cmd)
            s = "🔴 откл" if dis else f"ранг {mr}"
            lines.append(f"• `{pd}{cmd}` — {s}")
        await upd.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return
    raw = args[0].lstrip(pd or "~")
    disable = raw.startswith("-")
    cn = raw.lstrip("-").lower()
    if cn not in DEFAULT_RANKS:
        await upd.message.reply_text(f"❌ Неизвестная команда: `{cn}`", parse_mode=ParseMode.MARKDOWN); return
    if disable:
        with db() as c:
            c.execute("INSERT INTO config(chat_id,key,disabled) VALUES(?,?,1) ON CONFLICT(chat_id,key) DO UPDATE SET disabled=1",
                      (chat.id, cn))
        await upd.message.reply_text(f"🔴 `{pd}{cn}` отключена.", parse_mode=ParseMode.MARKDOWN)
    else:
        if len(args) < 2 or not args[1].isdigit():
            await upd.message.reply_text(f"📖 `{pd}конфиг {cn} [0-5]`", parse_mode=ParseMode.MARKDOWN); return
        nr = max(0, min(5, int(args[1])))
        with db() as c:
            c.execute("INSERT INTO config(chat_id,key,min_rank,disabled) VALUES(?,?,?,0) ON CONFLICT(chat_id,key) DO UPDATE SET min_rank=?,disabled=0",
                      (chat.id, cn, nr, nr))
        await upd.message.reply_text(f"✅ `{pd}{cn}` — ранг {nr} ({RANK_NAMES[nr]})", parse_mode=ParseMode.MARKDOWN)

# ROULETTE
async def h_roulette(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    u = upd.effective_user; chat = upd.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    name = get_display_name(u.id, chat.id, u.username, u.full_name)
    if random.randint(1, 6) == 1:
        until = datetime.now(timezone.utc) + timedelta(minutes=5)
        try:
            await ctx.bot.restrict_chat_member(chat.id, u.id,
                permissions=ChatPermissions(can_send_messages=False), until_date=until)
        except TelegramError:
            pass
        await upd.message.reply_text(
            f"🔫 *{name}* нажал на курок...\n"
            f"💀 *Не удача!* Отдохни пять минут в покое.",
            parse_mode=ParseMode.MARKDOWN)
    else:
        await upd.message.reply_text(
            f"🔫 *{name}* нажал на курок...\n"
            f"😮‍💨 Удачливый ты тип, будь осторожен в следующий раз.",
            parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
#  CLAN
# ──────────────────────────────────────────────
async def h_clan(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    u = upd.effective_user; chat = upd.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    p = get_prefix(chat.id); pd = "" if p == NOPREFIX else p

    if not args:
        await upd.message.reply_text(
            f"🏰 <b>Кланы</b>\n"
            f"<code>{pd}клан создать &lt;название&gt;</code> — создать\n"
            f"<code>{pd}клан вступить &lt;название&gt;</code> — вступить\n"
            f"<code>{pd}клан инфо [название]</code> — инфо о клане\n"
            f"<code>{pd}клан участники</code> — список участников\n"
            f"<code>{pd}клан покинуть</code> — покинуть\n"
            f"<code>{pd}клан кик &lt;номер&gt;</code> — кик (владелец)\n"
            f"<code>{pd}клан список</code> — все кланы",
            parse_mode=ParseMode.HTML)
        return

    sub = args[0].lower()

    # СОЗДАТЬ
    if sub == "создать":
        if len(args) < 2:
            await upd.message.reply_text(f"📖 <code>{pd}клан создать Название</code>", parse_mode=ParseMode.HTML)
            return
        if get_user_clan_id(u.id, chat.id):
            await upd.message.reply_text("❌ Ты уже в клане. Сначала покинь его."); return
        cname = " ".join(args[1:]).strip()
        if len(cname) > 30:
            await upd.message.reply_text("❌ Максимум 30 символов."); return
        try:
            with db() as c:
                c.execute("INSERT INTO clans(chat_id,name,owner_id) VALUES(?,?,?)", (chat.id, cname, u.id))
                cid = c.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                c.execute("UPDATE users SET clan_id=? WHERE user_id=? AND chat_id=?", (cid, u.id, chat.id))
                c.execute("INSERT INTO clan_members(clan_id,user_id,chat_id,slot) VALUES(?,?,?,1)", (cid, u.id, chat.id))
        except Exception:
            await upd.message.reply_text("❌ Клан с таким названием уже существует."); return
        name = get_display_name(u.id, chat.id, u.username, u.full_name)
        await upd.message.reply_text(f"🏰 Клан *{cname}* создан!\n👑 Владелец: {name}", parse_mode=ParseMode.MARKDOWN)

    # ВСТУПИТЬ
    elif sub == "вступить":
        if len(args) < 2:
            await upd.message.reply_text(f"📖 <code>{pd}клан вступить Название</code>", parse_mode=ParseMode.HTML); return
        if get_user_clan_id(u.id, chat.id):
            await upd.message.reply_text("❌ Ты уже в клане. Сначала покинь его."); return
        cname = " ".join(args[1:]).strip()
        with db() as c:
            clan = c.execute("SELECT * FROM clans WHERE chat_id=? AND LOWER(name)=?",
                             (chat.id, cname.lower())).fetchone()
        if not clan:
            await upd.message.reply_text(f"❌ Клан «{cname}» не найден.\nПосмотри список: <code>{pd}клан список</code>",
                                         parse_mode=ParseMode.HTML); return
        slot = next_slot(clan["id"])
        with db() as c:
            c.execute("INSERT INTO clan_members(clan_id,user_id,chat_id,slot) VALUES(?,?,?,?)",
                      (clan["id"], u.id, chat.id, slot))
            c.execute("UPDATE users SET clan_id=? WHERE user_id=? AND chat_id=?", (clan["id"], u.id, chat.id))
        name = get_display_name(u.id, chat.id, u.username, u.full_name)
        await upd.message.reply_text(f"✅ *{name}* вступил в клан *{clan['name']}*!", parse_mode=ParseMode.MARKDOWN)

    # ИНФ О
    elif sub == "инфо":
        if args[1:]:
            cname = " ".join(args[1:]).strip()
            with db() as c:
                clan = c.execute("SELECT * FROM clans WHERE chat_id=? AND LOWER(name)=?",
                                 (chat.id, cname.lower())).fetchone()
            if not clan:
                await upd.message.reply_text(f"❌ Клан «{cname}» не найден."); return
        else:
            cid = get_user_clan_id(u.id, chat.id)
            if not cid:
                await upd.message.reply_text(
                    f"❌ Ты не в клане.\n<code>{pd}клан инфо &lt;название&gt;</code> — инфо о любом клане.",
                    parse_mode=ParseMode.HTML); return
            clan = get_clan(cid)
        members = clan_members_list(clan["id"], chat.id)
        text = build_clan_info_text(clan, members)
        kb   = clan_info_keyboard(clan["id"], chat.id, u.id)
        await upd.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)

    # УЧАСТНИКИ
    elif sub == "участники":
        cid = get_user_clan_id(u.id, chat.id)
        if not cid:
            await upd.message.reply_text("❌ Ты не в клане."); return
        clan = get_clan(cid); members = clan_members_list(cid, chat.id)
        lines = [f"🏰 <b>Клан «{clan['name']}»</b> — {len(members)} чел.\n"]
        for m in members:
            nick = m["nickname"] or m["full_name"] or m["username"] or str(m["user_id"])
            crown = " 👑" if m["user_id"] == clan["owner_id"] else ""
            if m["username"]:
                link = f'<a href="https://t.me/{m["username"]}">{nick}</a>'
            else:
                link = f'<a href="tg://user?id={m["user_id"]}">{nick}</a>'
            lines.append(f"[{m['slot']}] {link}{crown}")
        text = "\n".join(lines)
        if len(members) > 20:
            await upd.message.reply_text(f"<blockquote>{text}</blockquote>", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await upd.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    # ПОКИНУТЬ
    elif sub == "покинуть":
        cid = get_user_clan_id(u.id, chat.id)
        if not cid:
            await upd.message.reply_text("❌ Ты не в клане."); return
        clan = get_clan(cid)
        if clan["owner_id"] == u.id:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, удалить клан", callback_data=f"cdisband:{cid}:{u.id}:{chat.id}"),
                InlineKeyboardButton("❌ Отмена",           callback_data=f"ccancel:{u.id}")
            ]])
            await upd.message.reply_text(
                f"⚠️ Вы уверены, что хотите выйти?\n"
                f"Это приведёт к *удалению клана* «{clan['name']}».",
                parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            with db() as c:
                c.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?", (cid, u.id))
                c.execute("UPDATE users SET clan_id=NULL WHERE user_id=? AND chat_id=?", (u.id, chat.id))
            name = get_display_name(u.id, chat.id, u.username, u.full_name)
            await upd.message.reply_text(f"👋 *{name}* покинул клан.", parse_mode=ParseMode.MARKDOWN)

    # КИК
    elif sub == "кик":
        cid = get_user_clan_id(u.id, chat.id)
        if not cid:
            await upd.message.reply_text("❌ Ты не в клане."); return
        clan = get_clan(cid)
        if clan["owner_id"] != u.id:
            await upd.message.reply_text("❌ Только владелец может кикать."); return
        if len(args) < 2 or not args[1].isdigit():
            await upd.message.reply_text(f"📖 <code>{pd}клан кик &lt;номер&gt;</code>", parse_mode=ParseMode.HTML); return
        slot_n = int(args[1])
        with db() as c:
            mrow = c.execute("SELECT user_id FROM clan_members WHERE clan_id=? AND slot=?", (cid, slot_n)).fetchone()
        if not mrow:
            await upd.message.reply_text(f"❌ Участник [{slot_n}] не найден."); return
        if mrow["user_id"] == u.id:
            await upd.message.reply_text("❌ Нельзя кикнуть себя."); return
        kid = mrow["user_id"]
        with db() as c:
            c.execute("DELETE FROM clan_members WHERE clan_id=? AND slot=?", (cid, slot_n))
            c.execute("UPDATE users SET clan_id=NULL WHERE user_id=? AND chat_id=?", (kid, chat.id))
        kicked = get_display_name(kid, chat.id)
        await upd.message.reply_text(f"👢 [{slot_n}] {kicked} исключён.", parse_mode=ParseMode.MARKDOWN)

    # СПИСОК
    elif sub == "список":
        with db() as c:
            clans = c.execute("""SELECT cl.id,cl.name,cl.owner_id,COUNT(cm.user_id) as cnt
                FROM clans cl LEFT JOIN clan_members cm ON cm.clan_id=cl.id
                WHERE cl.chat_id=? GROUP BY cl.id ORDER BY cnt DESC""", (chat.id,)).fetchall()
        if not clans:
            await upd.message.reply_text("📭 Кланов нет."); return
        lines = ["🏰 <b>Кланы чата:</b>\n"]
        buttons = []
        for i, cl in enumerate(clans, 1):
            orow = get_user(cl["owner_id"], chat.id)
            if orow:
                on = orow["nickname"] or orow["full_name"] or orow["username"] or str(cl["owner_id"])
                ol = f'<a href="https://t.me/{orow["username"]}">{on}</a>' if orow["username"] else f'<a href="tg://user?id={cl["owner_id"]}">{on}</a>'
            else:
                ol = str(cl["owner_id"])
            lines.append(f"[{i}] <b>{cl['name']}</b> — {cl['cnt']} чел. 👑 {ol}")
            buttons.append([InlineKeyboardButton(f"[{i}] {cl['name']}", callback_data=f"cinfo:{cl['id']}:{u.id}:{chat.id}")])
        await upd.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await upd.message.reply_text(f"❓ Неизвестная подкоманда. Напиши <code>{pd}клан</code>", parse_mode=ParseMode.HTML)

# ──────────────────────────────────────────────
#  CLAN CALLBACKS
# ──────────────────────────────────────────────
async def clan_callback(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    await q.answer()
    d = q.data; qu = q.from_user

    if d.startswith("ccancel:"):
        uid = int(d.split(":")[1])
        if qu.id != uid: await q.answer("❌ Это не твоя кнопка.", show_alert=True); return
        await q.edit_message_text("❌ Отменено.")

    elif d.startswith("cdisband:"):
        _, cid, uid, chat_id = d.split(":")
        cid, uid, chat_id = int(cid), int(uid), int(chat_id)
        if qu.id != uid: await q.answer("❌ Это не твоя кнопка.", show_alert=True); return
        clan = get_clan(cid)
        if not clan: await q.edit_message_text("❌ Клан уже удалён."); return
        with db() as c:
            for m in c.execute("SELECT user_id FROM clan_members WHERE clan_id=?", (cid,)).fetchall():
                c.execute("UPDATE users SET clan_id=NULL WHERE user_id=? AND chat_id=?", (m["user_id"], chat_id))
            c.execute("DELETE FROM clan_members WHERE clan_id=?", (cid,))
            c.execute("DELETE FROM clans WHERE id=?", (cid,))
        await q.edit_message_text(f"🏚 Клан «{clan['name']}» распущен.")

    elif d.startswith("cjoin:"):
        _, cid, uid, chat_id = d.split(":")
        cid, uid, chat_id = int(cid), int(uid), int(chat_id)
        if qu.id != uid: await q.answer("❌ Это не твоя кнопка.", show_alert=True); return
        if get_user_clan_id(uid, chat_id):
            await q.answer("❌ Ты уже в клане!", show_alert=True); return
        clan = get_clan(cid)
        if not clan: await q.edit_message_text("❌ Клан не существует."); return
        ensure_user(uid, chat_id, qu.username or "", qu.full_name)
        slot = next_slot(cid)
        with db() as c:
            c.execute("INSERT INTO clan_members(clan_id,user_id,chat_id,slot) VALUES(?,?,?,?)", (cid, uid, chat_id, slot))
            c.execute("UPDATE users SET clan_id=? WHERE user_id=? AND chat_id=?", (cid, uid, chat_id))
        nick = get_display_name(uid, chat_id, qu.username or "", qu.full_name)
        await q.edit_message_text(f"✅ <b>{nick}</b> вступил в клан <b>{clan['name']}</b>!", parse_mode=ParseMode.HTML)

    elif d.startswith("calready:"):
        await q.answer("Ты уже в другом клане. Сначала покинь его.", show_alert=True)

    elif d.startswith("cmembers:"):
        _, cid, chat_id = d.split(":")
        cid, chat_id = int(cid), int(chat_id)
        clan = get_clan(cid)
        if not clan: await q.answer("❌ Клан не найден.", show_alert=True); return
        members = clan_members_list(cid, chat_id)
        lines = [f"🏰 <b>Клан «{clan['name']}»</b> — {len(members)} чел.\n"]
        for m in members:
            nick = m["nickname"] or m["full_name"] or m["username"] or str(m["user_id"])
            crown = " 👑" if m["user_id"] == clan["owner_id"] else ""
            link = f'<a href="https://t.me/{m["username"]}">{nick}</a>' if m["username"] else f'<a href="tg://user?id={m["user_id"]}">{nick}</a>'
            lines.append(f"[{m['slot']}] {link}{crown}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"cinfo:{cid}:{qu.id}:{chat_id}")]])
        try:
            await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True, reply_markup=kb)
        except Exception:
            pass

    elif d.startswith("cinfo:"):
        _, cid, uid, chat_id = d.split(":")
        cid, uid, chat_id = int(cid), int(uid), int(chat_id)
        clan = get_clan(cid)
        if not clan: await q.answer("❌ Клан не найден.", show_alert=True); return
        members = clan_members_list(cid, chat_id)
        text = build_clan_info_text(clan, members)
        kb   = clan_info_keyboard(cid, chat_id, uid)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb)
        except Exception:
            pass

# HELP
async def h_help(upd: Update, ctx: ContextTypes.DEFAULT_TYPE, args):
    p = get_prefix(upd.effective_chat.id)
    pd = "" if p == NOPREFIX else p
    mode = "*(без префикса)*" if p == NOPREFIX else f"`{p}`"
    await upd.message.reply_text(
        f"📖 Префикс: {mode}\n"
        f"Пример: `{pd}профиль` · `{pd}фарма` · `{pd}клан`\n\n"
        f"Полный список команд: https://t.me/",
        parse_mode=ParseMode.MARKDOWN)

# ──────────────────────────────────────────────
#  ROUTER
# ──────────────────────────────────────────────
ROUTES = {
    "профиль":h_profile,"фарма":h_farm,"мешок":h_bag,"ник":h_nick,
    "префикс":h_prefix,"перевести":h_transfer,"вклад":h_contribute,
    "админы":h_admins,"стафф":h_admins,"кто админ":h_admins,
    "варн":h_warn,"пред":h_warn,"снять варн":h_unwarn,"снять пред":h_unwarn,
    "мут":h_mute,"заткнуть":h_mute,"анмут":h_unmute,"говори":h_unmute,
    "бан":h_ban,"чс":h_ban,"анбан":h_unban,"разбан":h_unban,
    "повысить":h_promote,"понизить":h_demote,"снять ранг":h_reset_rank,"ранг":h_set_rank,
    "конфиг":h_config,"клан":h_clan,"рулетка":h_roulette,
    "помощь":h_help,"команды":h_help,"help":h_help,
}

async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Private chats
    if update.effective_chat.type == "private":
        text = update.message.text.strip()
        first = text.split()[0].lower() if text.split() else ""
        if first in ROUTES or (text and not text[0].isalnum()):
            await update.message.reply_text("В личных сообщениях я не работаю.")
        return

    u = update.effective_user; chat = update.effective_chat
    ensure_user(u.id, chat.id, u.username, u.full_name)
    tick_messages(u.id, chat.id)
    await sync_owner(context.bot, u.id, chat.id)

    text   = update.message.text.strip()
    prefix = get_prefix(chat.id)

    # In no-prefix mode also allow ~префикс ~ to restore
    if prefix == NOPREFIX and text.startswith(DEFAULT_PREFIX):
        cmd2, args2 = parse_cmd(text, DEFAULT_PREFIX)
        if cmd2 == "префикс":
            await h_prefix(update, context, args2)
            return

    cmd, args = parse_cmd(text, prefix)
    if not cmd:
        return

    # In no-prefix mode: only react if first word exactly matches a known command
    if prefix == NOPREFIX and cmd not in ROUTES:
        return

    handler = ROUTES.get(cmd)
    if handler:
        await handler(update, context, args)

# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Вставь токен бота в BOT_TOKEN!")
        return
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, router))
    app.add_handler(CallbackQueryHandler(clan_callback, pattern=r"^c"))
    log.info("Bot v4 started!")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
