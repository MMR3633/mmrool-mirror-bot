import asyncio
import io
import logging
import time
from pathlib import Path

from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import SessionPasswordNeededError

from . import config
from .core import display_name, media_kind, preview, send_kwargs

log = logging.getLogger("mirror.account")


class Account:
    def __init__(self, data_dir: Path, client=None):
        self.data_dir = data_dir
        self.client = client
        self.pending_qr = None
        self.pending_phone = None
        self.me = None
        self.on_message = []
        self.on_call = []
        self.on_read = []
        self.calls = {}
        self._muted = {}
        self.always_online = False
        self.hands_off = False
        self.paused = False
        self.offline_after = config.OFFLINE_AFTER
        self.sending = 0
        self.pace = 1.0
        self._offline_task = None

    async def start(self):
        session = str(self.data_dir / "account")
        if config.SESSION_STRING and not (self.data_dir / "account.session").exists():
            from telethon.sessions import StringSession
            session = StringSession(config.SESSION_STRING)
        self.client = TelegramClient(session, config.API_ID, config.API_HASH,
                                     proxy=config.telethon_proxy(), connection_retries=None, retry_delay=5,
                                     auto_reconnect=True, device_model="TgMirror", app_version="1.0")
        self.client.add_event_handler(self._new_message, events.NewMessage())
        self.client.add_event_handler(self._raw, events.Raw((types.UpdatePhoneCall, types.UpdateReadHistoryInbox,
                                                             types.UpdateReadHistoryOutbox)))
        if self.paused:
            log.info("аккаунт на паузе: не подключаюсь")
            return
        try:
            await self.client.connect()
            if await self.client.is_user_authorized():
                await self._after_login()
        except Exception as e:
            log.warning("аккаунт: нет связи на старте: %s", e)

    async def pause(self):
        """Выключить зеркало: соединение аккаунта рвём, сессию НЕ трогаем, доступ остаётся."""
        self.paused = True
        if self.client is not None and self.client.is_connected():
            try:
                if self.me is not None:
                    await self.set_online(False)
            except Exception:
                pass
            await self.client.disconnect()

    async def resume(self):
        """Включить обратно тем же входом, без повторного QR. Пропущенное за паузу в бота не сыплем."""
        self.paused = False
        if self.client is None:
            return False
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            return False
        self.me = await self.client.get_me()
        await self.set_online(False)
        return True

    async def _after_login(self):
        self.me = await self.client.get_me()
        await self.set_online(False)
        await self.client.catch_up()
        log.info("аккаунт подключён: %s", display_name(self.me))

    async def ready(self):
        if self.client is None or self.paused:
            return False
        try:
            if not self.client.is_connected():
                await self.client.connect()
            return await self.client.is_user_authorized()
        except Exception as e:
            log.warning("аккаунт: проверка входа не прошла: %s", e)
            return False

    async def qr_png(self):
        import qrcode
        if not self.client.is_connected():
            await self.client.connect()
        self.pending_qr = await self.client.qr_login()
        img = qrcode.make(self.pending_qr.url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def wait_qr(self, timeout=180):
        try:
            await self.pending_qr.wait(timeout)
            await self._after_login()
            return "ok"
        except SessionPasswordNeededError:
            return "password"
        except asyncio.TimeoutError:
            return "timeout"

    async def password(self, pwd):
        await self.client.sign_in(password=pwd)
        await self._after_login()

    async def send_code(self, phone):
        if not self.client.is_connected():
            await self.client.connect()
        sent = await self.client.send_code_request(phone)
        self.pending_phone = (phone, sent.phone_code_hash)

    async def sign_in_code(self, code):
        phone, code_hash = self.pending_phone
        try:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
            self.pending_phone = None
            await self._after_login()
            return "ok"
        except SessionPasswordNeededError:
            return "password"

    async def logout(self):
        await self.client.log_out()
        self.me = None

    async def is_muted(self, peer_id):
        hit = self._muted.get(peer_id)
        if hit and hit[1] > time.time():
            return hit[0]
        muted = False
        try:
            s = await self.client(functions.account.GetNotifySettingsRequest(
                peer=types.InputNotifyPeer(await self.client.get_input_entity(peer_id))))
            until = getattr(s, "mute_until", None)
            muted = bool(until and (until.timestamp() if hasattr(until, "timestamp") else until) > time.time())
        except Exception as e:
            log.debug("настройки уведомлений %s: %s", peer_id, e)
        self._muted[peer_id] = (muted, time.time() + 300)
        return muted

    async def _new_message(self, event):
        for cb in self.on_message:
            try:
                await cb(event)
            except Exception:
                log.exception("обработчик сообщения упал")
        await self.stay_offline()

    async def stay_offline(self):
        """Любые служебные запросы (сущности, медиа, диалоги) не должны светить аккаунт «в сети»."""
        if not self.always_online and not self.sending and self.me is not None:
            await self.set_online(False)

    async def _raw(self, update):
        if isinstance(update, types.UpdatePhoneCall):
            call = update.phone_call
            if isinstance(call, types.PhoneCallRequested):
                self.calls[call.id] = call
            info = {"id": call.id, "state": type(call).__name__,
                    "user_id": getattr(call, "admin_id", None), "video": bool(getattr(call, "video", False))}
            if isinstance(call, types.PhoneCallDiscarded):
                info["reason"] = type(call.reason).__name__ if call.reason else None
                prev = self.calls.pop(call.id, None)
                info["user_id"] = getattr(prev, "admin_id", None)
            if self.me and info["user_id"] == self.me.id:
                return
            for cb in self.on_call:
                try:
                    await cb(info)
                except Exception:
                    log.exception("обработчик звонка упал")
            await self.stay_offline()
            return
        for cb in self.on_read:
            try:
                await cb(update)
            except Exception:
                log.exception("обработчик прочтения упал")

    async def discard_call(self, call_id):
        call = self.calls.pop(call_id, None)
        if call is None:
            return False
        await self.client(functions.phone.DiscardCallRequest(
            peer=types.InputPhoneCall(id=call.id, access_hash=call.access_hash),
            duration=0, reason=types.PhoneCallDiscardReasonBusy(), connection_id=0))
        await self.stay_offline()
        return True

    async def dialogs(self, limit=60):
        out = []
        async for d in self.client.iter_dialogs(limit=limit):
            ent = d.entity
            if isinstance(ent, types.User) and ent.is_self:
                name = "Избранное"
            else:
                name = display_name(ent)
            m = d.message
            kind = media_kind(m) if m else None
            out.append({
                "id": d.id, "name": name, "unread": d.unread_count,
                "type": "user" if d.is_user else ("group" if d.is_group else "channel"),
                "bot": bool(getattr(ent, "bot", False)), "pinned": bool(d.pinned),
                "muted": bool(d.dialog.notify_settings and d.dialog.notify_settings.mute_until),
                "last": preview(getattr(m, "message", ""), kind) if m else "",
                "last_out": bool(getattr(m, "out", False)),
                "date": int(m.date.timestamp()) if m and m.date else 0,
                "photo": bool(getattr(ent, "photo", None)) and not isinstance(
                    getattr(ent, "photo", None), (types.UserProfilePhotoEmpty, types.ChatPhotoEmpty)),
            })
        return out

    def message_json(self, m, sender_name=None):
        kind = media_kind(m)
        data = {"id": m.id, "out": bool(m.out), "date": int(m.date.timestamp()) if m.date else 0,
                "text": m.message or "", "kind": kind, "sender": sender_name,
                "reply_to": m.reply_to.reply_to_msg_id if m.reply_to else None,
                "duration": None, "file_name": None, "service": isinstance(m, types.MessageService)}
        if kind in ("voice", "video_note", "video", "audio", "animation") and m.file:
            data["duration"] = int(m.file.duration or 0) if m.file.duration else None
        if kind in ("document", "audio") and m.file:
            data["file_name"] = m.file.name
        if data["service"]:
            data["text"] = service_text(m)
        return data

    async def history(self, peer_id, limit=40, before=0):
        entity = await self.client.get_entity(peer_id)
        msgs = await self.client.get_messages(entity, limit=limit, offset_id=before or 0)
        group = not isinstance(entity, types.User)
        out = []
        for m in reversed(msgs):
            name = None
            if group and not m.out:
                try:
                    name = display_name(await m.get_sender())
                except Exception:
                    name = None
            out.append(self.message_json(m, name))
        return {"peer": peer_id, "name": "Избранное" if getattr(entity, "is_self", False) else display_name(entity),
                "type": "user" if isinstance(entity, types.User) else "group", "messages": out,
                "status": user_status(entity)}

    async def read(self, peer_id, max_id=None):
        """Отметить прочитанным. Вызывается ТОЛЬКО по явной команде владельца (кнопка, /read, мини-апп)."""
        await self.client.send_read_acknowledge(peer_id, max_id=max_id)
        await self.stay_offline()
        return True

    async def set_online(self, online):
        if self.hands_off:
            return
        try:
            await self.client(functions.account.UpdateStatusRequest(offline=not online))
        except Exception as e:
            log.debug("статус: %s", e)

    def _schedule_offline(self):
        if self._offline_task and not self._offline_task.done():
            self._offline_task.cancel()
        if self.always_online:
            return

        async def later():
            await asyncio.sleep(self.offline_after)
            if not self.always_online:
                await self.set_online(False)

        self._offline_task = asyncio.ensure_future(later())

    async def _show_activity(self, peer_id, kind, text_len):
        """Перед отправкой: «в сети» и «печатает…» / «записывает кружок…», как живой человек."""
        await self.set_online(True)
        action, seconds = typing_plan(kind, text_len)
        seconds *= self.pace
        if seconds <= 0:
            return
        try:
            async with self.client.action(peer_id, action):
                await asyncio.sleep(seconds)
        except Exception as e:
            log.debug("действие %s: %s", action, e)

    async def send_text(self, peer_id, text, reply_to=None):
        self.sending += 1
        try:
            await self._show_activity(peer_id, None, len(text))
            m = await self.client.send_message(peer_id, text, reply_to=reply_to, link_preview=True)
        finally:
            self.sending -= 1
            self._schedule_offline()
        return m

    async def send_media(self, peer_id, path, kind, duration=0, width=0, height=0, caption=None, reply_to=None):
        kw = send_kwargs(kind, duration, width, height)
        self.sending += 1
        try:
            await self._show_activity(peer_id, kind, len(caption or ""))
            m = await self.client.send_file(peer_id, str(path), caption=caption or None, reply_to=reply_to, **kw)
        finally:
            self.sending -= 1
            self._schedule_offline()
        return m

    async def keep_online(self, every=20):
        """Сторож присутствия: постоянный «в сети» по /online, иначе раз в 20 с гасим случайный онлайн."""
        while True:
            try:
                if self.client is not None and self.me is not None and not self.paused:
                    if self.always_online:
                        await self.set_online(True)
                    elif not self.sending:
                        await self.set_online(False)
            except Exception as e:
                log.debug("сторож: %s", e)
            await asyncio.sleep(every)

    async def get_message(self, peer_id, msg_id):
        msg = await self.client.get_messages(peer_id, ids=msg_id)
        return msg

    async def download(self, msg, folder: Path, thumb=None):
        folder.mkdir(parents=True, exist_ok=True)
        if thumb is not None and getattr(msg, "photo", None):
            sizes = [s for s in msg.photo.sizes if getattr(s, "type", "") in ("x", "m")]
            thumb = sizes[-1] if sizes else None
        return await self.client.download_media(msg, file=str(folder), thumb=thumb)

    async def avatar(self, peer_id):
        return await self.client.download_profile_photo(peer_id, file=bytes, download_big=False)


ACTIONS = {None: "typing", "video_note": "record-round", "voice": "record-audio", "photo": "photo",
           "video": "video", "audio": "audio", "document": "document", "animation": "video", "sticker": "typing"}


def typing_plan(kind, text_len):
    action = ACTIONS.get(kind, "typing")
    if kind is None:
        return action, min(6.0, max(1.0, text_len / 14))
    return action, 1.5


def user_status(entity):
    st = getattr(entity, "status", None)
    if getattr(entity, "bot", False):
        return "бот"
    if isinstance(st, types.UserStatusOnline):
        return "в сети"
    if isinstance(st, types.UserStatusRecently):
        return "был(а) недавно"
    if isinstance(st, types.UserStatusOffline) and st.was_online:
        return "был(а) " + st.was_online.astimezone().strftime("%d.%m в %H:%M")
    if isinstance(entity, types.Channel):
        return "группа" if entity.megagroup else "канал"
    if isinstance(entity, types.Chat):
        return "группа"
    return ""


def service_text(m):
    a = m.action
    if isinstance(a, types.MessageActionPhoneCall):
        missed = isinstance(a.reason, (types.PhoneCallDiscardReasonMissed, types.PhoneCallDiscardReasonBusy))
        what = "Видеозвонок" if a.video else "Звонок"
        if missed:
            return f"{what}: пропущенный" if not m.out else f"{what}: отменён"
        mins, secs = divmod(a.duration or 0, 60)
        return f"{what}, {mins}:{secs:02d}"
    if isinstance(a, types.MessageActionPinMessage):
        return "Закреплено сообщение"
    if isinstance(a, types.MessageActionContactSignUp):
        return "Теперь в Telegram"
    return "Служебное сообщение"


def peer_of(event):
    return utils.get_peer_id(event.message.peer_id)
