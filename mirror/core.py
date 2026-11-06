import base64
import hashlib
import hmac
import json
import re
import sqlite3
import time
from urllib.parse import parse_qsl

INIT_MAX_AGE = 24 * 3600
SESSION_DAYS = 7

KIND_LABEL = {
    "photo": "фото", "video": "видео", "video_note": "кружок", "voice": "голосовое",
    "audio": "аудио", "sticker": "стикер", "animation": "гифка", "document": "файл",
    "geo": "геолокация", "contact": "контакт", "poll": "опрос", "media": "медиа",
}


def display_name(entity, fallback="Без имени"):
    if entity is None:
        return fallback
    name = " ".join(x for x in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if x)
    return name or getattr(entity, "title", None) or (f"@{entity.username}" if getattr(entity, "username", None) else fallback)


def media_kind(m):
    if getattr(m, "photo", None):
        return "photo"
    if getattr(m, "video_note", None):
        return "video_note"
    if getattr(m, "voice", None):
        return "voice"
    if getattr(m, "sticker", None):
        return "sticker"
    if getattr(m, "gif", None):
        return "animation"
    if getattr(m, "video", None):
        return "video"
    if getattr(m, "audio", None):
        return "audio"
    if getattr(m, "document", None):
        return "document"
    if getattr(m, "geo", None):
        return "geo"
    if getattr(m, "contact", None):
        return "contact"
    if getattr(m, "poll", None):
        return "poll"
    if getattr(m, "media", None):
        return "media"
    return None


def preview(text, kind):
    text = (text or "").strip()
    if kind and text:
        return f"[{KIND_LABEL.get(kind, kind)}] {text}"
    if kind:
        return f"[{KIND_LABEL.get(kind, kind)}]"
    return text


def parse_phone(text):
    phone = re.sub(r"[^\d+]", "", text or "")
    if len(re.sub(r"\D", "", phone)) < 10:
        return None
    if phone.startswith("8") and len(phone) == 11:
        phone = "7" + phone[1:]
    return phone if phone.startswith("+") else "+" + phone


def parse_code(text):
    if not re.fullmatch(r"[\d\s.\-]{5,20}", text or ""):
        return None
    code = re.sub(r"\D", "", text)
    return code if 5 <= len(code) <= 6 else None


def check_init_data(init_data, token, now=None, max_age=INIT_MAX_AGE):
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received or not token:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return int(json.loads(pairs.get("user", "{}"))["id"])
    except (KeyError, ValueError, TypeError):
        return None


def sign_init_data(fields, token):
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    from urllib.parse import urlencode
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def _session_key(token):
    return hmac.new(b"mirror-session", token.encode(), hashlib.sha256).digest()


def make_session(user_id, token, now=None):
    exp = int((now or time.time()) + SESSION_DAYS * 86400)
    body = f"{user_id}.{exp}"
    sig = hmac.new(_session_key(token), body.encode(), hashlib.sha256).digest()[:18]
    return f"{body}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def read_session(value, token, now=None):
    try:
        uid, exp, sig = value.split(".")
        good = hmac.new(_session_key(token), f"{uid}.{exp}".encode(), hashlib.sha256).digest()[:18]
        if not hmac.compare_digest(base64.urlsafe_b64encode(good).decode().rstrip("="), sig):
            return None
        if int(exp) < (now or time.time()):
            return None
        return int(uid)
    except (ValueError, AttributeError):
        return None


MAX_FAILS = 5
BLOCK_SECONDS = 15 * 60


class Store:
    def __init__(self, path):
        self.con = sqlite3.connect(str(path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("CREATE TABLE IF NOT EXISTS routes2 (chat_id INTEGER NOT NULL, bot_msg_id INTEGER NOT NULL,"
                         " peer_id INTEGER NOT NULL, tg_msg_id INTEGER, name TEXT, created REAL,"
                         " PRIMARY KEY (chat_id, bot_msg_id))")
        self.con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
        self.con.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, added REAL)")
        self.con.execute("CREATE TABLE IF NOT EXISTS attempts (user_id INTEGER PRIMARY KEY, fails INTEGER NOT NULL,"
                         " blocked_until REAL NOT NULL DEFAULT 0)")
        self.con.commit()

    def remember(self, chat_id, bot_msg_id, peer_id, tg_msg_id=None, name=None):
        self.con.execute("INSERT OR REPLACE INTO routes2 VALUES (?, ?, ?, ?, ?, ?)",
                         (chat_id, bot_msg_id, peer_id, tg_msg_id, name, time.time()))
        self.con.commit()

    def route(self, chat_id, bot_msg_id):
        row = self.con.execute("SELECT peer_id, tg_msg_id, name FROM routes2 WHERE chat_id=? AND bot_msg_id=?",
                               (chat_id, bot_msg_id)).fetchone()
        return (row["peer_id"], row["tg_msg_id"], row["name"]) if row else None

    def recent(self, hours=24, limit=60):
        rows = self.con.execute("SELECT chat_id, bot_msg_id, peer_id, tg_msg_id FROM routes2 WHERE created > ? "
                                "ORDER BY created DESC LIMIT ?", (time.time() - hours * 3600, limit)).fetchall()
        return [(r["chat_id"], r["bot_msg_id"], r["peer_id"], r["tg_msg_id"]) for r in rows]

    def set(self, key, value):
        if value is None:
            self.con.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            self.con.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))
        self.con.commit()

    def get(self, key, default=None):
        row = self.con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def prune(self, days=60):
        self.con.execute("DELETE FROM routes2 WHERE created < ?", (time.time() - days * 86400,))
        self.con.commit()

    def users(self):
        return [r["user_id"] for r in self.con.execute("SELECT user_id FROM users ORDER BY added")]

    def is_user(self, user_id):
        return self.con.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is not None

    def add_user(self, user_id, name=None):
        self.con.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?)", (user_id, name, time.time()))
        self.con.execute("DELETE FROM attempts WHERE user_id=?", (user_id,))
        self.con.commit()

    def remove_user(self, user_id):
        self.con.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        self.con.commit()

    def blocked_for(self, user_id, now=None):
        row = self.con.execute("SELECT blocked_until FROM attempts WHERE user_id=?", (user_id,)).fetchone()
        left = (row["blocked_until"] - (now or time.time())) if row else 0
        return max(0, int(left))

    def try_password(self, user_id, given, password, name=None, now=None):
        """ok / wrong / blocked. Пять промахов подряд = блок на 15 минут."""
        now = now or time.time()
        if self.blocked_for(user_id, now):
            return "blocked"
        if password and hmac.compare_digest(given.strip().encode(), password.encode()):
            self.add_user(user_id, name)
            return "ok"
        row = self.con.execute("SELECT fails, blocked_until FROM attempts WHERE user_id=?", (user_id,)).fetchone()
        fails = (row["fails"] if row else 0) + 1
        until = now + BLOCK_SECONDS if fails >= MAX_FAILS else 0
        self.con.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?, ?)", (user_id, 0 if until else fails, until))
        self.con.commit()
        return "blocked" if until else "wrong"


def resolve_target(store, chat_id, reply_to_bot_msg_id):
    """Кому уйдёт сообщение из бота: свайп-ответ на продублированное сообщение важнее выбранного чата."""
    if reply_to_bot_msg_id:
        hit = store.route(chat_id, reply_to_bot_msg_id)
        if hit:
            return {"peer": hit[0], "reply_to": hit[1], "name": hit[2]}
    cur = store.get(f"target:{chat_id}")
    if cur:
        return {"peer": cur["peer"], "reply_to": None, "name": cur.get("name")}
    return None


def send_kwargs(kind, duration=0, width=0, height=0):
    """Как userbot отправит файл, чтобы у получателя он был настоящим кружком или голосовым, а не видео/файлом."""
    from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo
    if kind == "video_note":
        side = width or height or 384
        return {"video_note": True, "attributes": [DocumentAttributeVideo(
            duration=int(duration or 0), w=side, h=side, round_message=True, supports_streaming=True)]}
    if kind == "voice":
        return {"voice_note": True, "attributes": [DocumentAttributeAudio(duration=int(duration or 0), voice=True)]}
    if kind == "video":
        return {"supports_streaming": True}
    if kind in ("document", "audio"):
        return {"force_document": kind == "document"}
    return {}
