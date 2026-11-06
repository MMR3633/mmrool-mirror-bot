import html
import logging
import shutil
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)
from telethon import types as tt

from . import config
from .account import Account, peer_of
from .core import KIND_LABEL, Store, display_name, media_kind, parse_code, parse_phone, resolve_target

log = logging.getLogger("mirror.bot")
router = Router()
BOT_UPLOAD_LIMIT = 49 * 1024 * 1024
CTX = {}


def acc() -> Account:
    return CTX["account"]


def store() -> Store:
    return CTX["store"]


def allowed_id(uid):
    """Кто может пользоваться ботом: с паролем = все, кто его ввёл; без пароля = только OWNER_ID."""
    if not uid:
        return False
    if config.BOT_PASSWORD:
        return store().is_user(uid)
    return uid == config.OWNER_ID


def is_owner(obj):
    u = getattr(obj, "from_user", None)
    return bool(u and allowed_id(u.id))


def owner_only():
    return F.func(is_owner)


def recipients():
    if config.BOT_PASSWORD:
        return store().users()
    return [config.OWNER_ID] if config.OWNER_ID else []


def webapp_url(peer=None):
    if not config.WEBAPP_URL:
        return None
    return f"{config.WEBAPP_URL}/?chat={peer}" if peer else f"{config.WEBAPP_URL}/"


def message_keyboard(peer, tg_msg_id=None, read=False):
    row = [InlineKeyboardButton(text="Ответить", callback_data=f"to:{peer}")]
    url = webapp_url(peer)
    if url:
        row.append(InlineKeyboardButton(text="Открыть чат", web_app=WebAppInfo(url=url)))
    rows = [row]
    if tg_msg_id:
        rows.append([InlineKeyboardButton(text="✓ Прочитано", callback_data="noop") if read else
                     InlineKeyboardButton(text="👁 Прочитать", callback_data=f"rd:{peer}:{tg_msg_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def call_keyboard(call_id, peer):
    rows = [[InlineKeyboardButton(text="Сбросить", callback_data=f"cd:{call_id}"),
             InlineKeyboardButton(text="Сбросить и написать «перезвоню»", callback_data=f"cr:{call_id}:{peer}")]]
    url = webapp_url(peer)
    if url and CTX.get("calls") is not None:
        rows.insert(0, [InlineKeyboardButton(text="Принять в мини-аппе", web_app=WebAppInfo(url=f"{url}&call=1"))])
    elif url:
        rows.append([InlineKeyboardButton(text="Открыть чат", web_app=WebAppInfo(url=url))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def answer(msg: Message, text, keyboard=None):
    return await msg.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


async def push_incoming(event):
    """Входящее в аккаунт → сообщение в боте с именем отправителя. Прочитанным НЕ отмечаем."""
    m = event.message
    if m.out:
        return
    bot: Bot = CTX["bot"]
    peer = peer_of(event)
    chat = await event.get_chat()
    sender = await event.get_sender()
    if isinstance(chat, tt.User):
        if chat.bot and chat.id == CTX.get("bot_id"):
            return
    elif not config.MIRROR_GROUPS:
        return
    if isinstance(chat, tt.Channel) and not chat.megagroup:
        return
    if not config.MIRROR_MUTED and await acc().is_muted(peer):
        return
    who = display_name(sender) if sender else display_name(chat)
    title = who if isinstance(chat, tt.User) else f"{who} → {display_name(chat)}"
    head = f"<b>{html.escape(title)}</b>"
    text = m.message or ""
    kind = media_kind(m)
    kb = message_keyboard(peer, m.id, read=config.AUTO_READ)
    users = recipients()
    if not users:
        return
    folder = None
    try:
        path = None
        too_big = None
        if kind not in (None, "geo", "contact", "poll", "media"):
            size = m.file.size if m.file else 0
            if size and size > BOT_UPLOAD_LIMIT:
                too_big = round(size / 1024 / 1024)
            else:
                folder = config.DATA / "tmp" / uuid.uuid4().hex
                path = await acc().download(m, folder)
        for uid in users:
            try:
                sent = await deliver_to(bot, uid, m, kind, head, text, kb, path, too_big)
            except Exception as e:
                log.exception("не продублировал %s/%s пользователю %s", peer, m.id, uid)
                sent = [await bot.send_message(uid, f"{head}\n<i>{KIND_LABEL.get(kind, 'сообщение')}: не смог передать "
                                                    f"({html.escape(str(e)[:120])}), открой чат</i>", reply_markup=kb)]
            for s in sent:
                store().remember(uid, s.message_id, peer, m.id, who)
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    if config.AUTO_READ:
        await acc().read(peer, max_id=m.id)


async def deliver_to(bot: Bot, uid, m, kind, head, text, kb, path, too_big):
    if kind in (None, "geo", "contact", "poll", "media"):
        body = html.escape(text) if text else f"<i>{KIND_LABEL.get(kind, 'сообщение')}</i>"
        if kind == "geo" and m.geo:
            body += f"\nhttps://maps.google.com/?q={m.geo.lat},{m.geo.long}"
        if kind == "contact" and m.contact:
            body = html.escape(f"{m.contact.first_name or ''} {m.contact.last_name or ''} {m.contact.phone_number or ''}".strip())
        return [await bot.send_message(uid, f"{head}\n{body}", reply_markup=kb, disable_web_page_preview=True)]
    if too_big:
        return [await bot.send_message(uid, f"{head}\n<i>{KIND_LABEL.get(kind)}, {too_big} МБ: боту нельзя больше 50 МБ, "
                                            f"открой чат</i>\n{html.escape(text)}", reply_markup=kb)]
    if not path:
        raise RuntimeError("файл не скачался")
    f = FSInputFile(path)
    cap = f"{head}\n{html.escape(text)}" if text else head
    dur = int(m.file.duration or 0) if m.file and m.file.duration else None
    if kind == "photo":
        return [await bot.send_photo(uid, f, caption=cap, reply_markup=kb)]
    if kind == "voice":
        return [await bot.send_voice(uid, f, caption=cap, reply_markup=kb, duration=dur)]
    if kind == "video_note":
        first = await bot.send_message(uid, f"{head}\n<i>кружок</i>")
        note = await bot.send_video_note(uid, f, duration=dur, length=m.file.width or 384, reply_markup=kb)
        return [first, note]
    if kind == "video":
        return [await bot.send_video(uid, f, caption=cap, reply_markup=kb, supports_streaming=True, duration=dur,
                                     width=m.file.width, height=m.file.height)]
    if kind == "animation":
        return [await bot.send_animation(uid, f, caption=cap, reply_markup=kb)]
    if kind == "audio":
        return [await bot.send_audio(uid, f, caption=cap, reply_markup=kb)]
    if kind == "sticker":
        first = await bot.send_message(uid, f"{head}\n<i>стикер</i>")
        try:
            st = await bot.send_sticker(uid, f, reply_markup=kb)
        except Exception:
            st = await bot.send_document(uid, FSInputFile(path), reply_markup=kb)
        return [first, st]
    return [await bot.send_document(uid, f, caption=cap, reply_markup=kb)]


async def push_call(info):
    """Входящий звонок аккаунта → «Звонит …» в боте с кнопками; по окончании правим сообщение."""
    bot: Bot = CTX["bot"]
    calls = CTX.setdefault("call_msgs", {})
    uid = info.get("user_id")
    name = "Кто-то"
    if uid:
        try:
            name = display_name(await acc().client.get_entity(uid))
        except Exception:
            pass
    if info["state"] == "PhoneCallRequested":
        what = "Видеозвонок" if info.get("video") else "Звонок"
        tail = ("Принять: кнопка ниже, разговор пойдёт через мини-апп." if CTX.get("calls") is not None else
                "Ответить можно с телефона второго аккаунта, отсюда: сбросить или написать.")
        sent_ids = []
        for user in recipients():
            sent = await bot.send_message(user, f"📞 <b>{what}: {html.escape(name)}</b>\nЗвонит прямо сейчас. {tail}",
                                          reply_markup=call_keyboard(info["id"], uid or 0))
            sent_ids.append((user, sent.message_id))
            if uid:
                store().remember(user, sent.message_id, uid, None, name)
        calls[info["id"]] = (sent_ids, name)
    elif info["state"] == "PhoneCallDiscarded" and info["id"] in calls:
        sent_ids, name = calls.pop(info["id"])
        reason = {"PhoneCallDiscardReasonMissed": "пропущенный", "PhoneCallDiscardReasonBusy": "сброшен",
                  "PhoneCallDiscardReasonHangup": "завершён"}.get(info.get("reason"), "завершён")
        kb = message_keyboard(uid) if uid else None
        for user, mid in sent_ids:
            try:
                await bot.edit_message_text(f"📞 Звонок от <b>{html.escape(name)}</b>: {reason}", chat_id=user,
                                            message_id=mid, reply_markup=kb)
            except Exception as e:
                log.debug("правка звонка: %s", e)


@router.message(F.func(lambda m: not is_owner(m)), F.chat.type == "private")
async def gate(msg: Message):
    """Пока не введён пароль, бот отвечает только просьбой ввести пароль. Сообщение с паролем сразу удаляется."""
    if not config.BOT_PASSWORD:
        return
    uid = msg.from_user.id if msg.from_user else 0
    left = store().blocked_for(uid)
    if left:
        await answer(msg, f"Слишком много неверных попыток. Попробуй через {left // 60 + 1} мин.")
        return
    text = (msg.text or "").strip()
    if not text or text.startswith("/"):
        await answer(msg, "🔒 Бот закрыт паролем. Пришли пароль одним сообщением.")
        return
    try:
        await msg.delete()
    except Exception:
        pass
    result = store().try_password(uid, text, config.BOT_PASSWORD, name=display_name(msg.from_user))
    if result == "ok":
        log.info("пароль принят для %s", uid)
        await cmd_start(msg)
    elif result == "blocked":
        await answer(msg, "Неверный пароль. Пять ошибок подряд: вход закрыт на 15 минут.")
    else:
        await answer(msg, "Неверный пароль.")


@router.callback_query(F.func(lambda c: not is_owner(c)))
async def gate_callback(call: CallbackQuery):
    await call.answer("Сначала пароль", show_alert=False)


@router.message(CommandStart(), owner_only())
async def cmd_start(msg: Message):
    ok = await acc().ready()
    who = display_name(acc().me) if ok and acc().me else None
    head = (f"Подключён аккаунт: <b>{html.escape(who)}</b>" if who else
            "⏸ Зеркало выключено. Включить: /on" if getattr(acc(), "paused", False) else "Аккаунт не подключён.")
    lines = [head,
             "",
             "/off выключить зеркало (вход сохраняется) · /on включить",
             "/chats выбрать, кому писать",
             "/stop перестать писать выбранному",
             "/read отметить прочитанным выбранный чат",
             "/online «в сети» постоянно: вкл/выкл",
             "/net бот перестаёт трогать «в сети» (статус как у обычного аккаунта): вкл/выкл",
             "/login вход по QR · /phone вход по номеру",
             "/logout отключить аккаунт",
             "",
             "Сообщения аккаунта приходят сюда сами и НЕ отмечаются прочитанными, пока не нажмёшь «👁 Прочитать». "
             "Ответить: свайпни сообщение или жми «Ответить». Кружок или голосовое, записанные здесь, уйдут "
             "собеседнику как настоящие кружок и голосовое, перед этим у него будет «записывает…»."]
    kb = None
    if config.WEBAPP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть все чаты",
                                                                         web_app=WebAppInfo(url=webapp_url()))]])
    await answer(msg, "\n".join(lines), kb)


@router.message(Command("login"), owner_only())
async def cmd_login(msg: Message, bot: Bot):
    if await acc().ready():
        await answer(msg, "Аккаунт уже подключён. Сменить: /logout, потом /login.")
        return
    png = await acc().qr_png()
    await bot.send_photo(msg.chat.id, BufferedInputFile(png, "login.png"),
                         caption="На телефоне второго аккаунта: Настройки → Устройства → Подключить устройство, "
                                 "наведи камеру на этот код (открой его на другом экране). Код живёт 3 минуты.\n"
                                 "Нет второго экрана: /phone +7XXXXXXXXXX")
    result = await acc().wait_qr()
    if result == "ok":
        await answer(msg, f"Подключил: <b>{html.escape(display_name(acc().me))}</b>. Сообщения пойдут сюда.")
    elif result == "password":
        await answer(msg, "У аккаунта облачный пароль. Пришли: /pass пароль (сообщение сразу удалю).")
    else:
        await answer(msg, "Код истёк. Напиши /login ещё раз.")


@router.message(Command("phone"), owner_only())
async def cmd_phone(msg: Message):
    if await acc().ready():
        await answer(msg, "Аккаунт уже подключён, повторный вход не нужен.")
        return
    phone = parse_phone((msg.text or "").partition(" ")[2])
    if not phone:
        await answer(msg, "Напиши так: /phone +79991234567")
        return
    try:
        await acc().send_code(phone)
    except Exception as e:
        await answer(msg, f"Не смог запросить код: {html.escape(str(e))}")
        return
    await answer(msg, "Telegram прислал код в чат «Telegram» второго аккаунта. Пришли его сюда <b>с пробелами</b>: "
                      "1 2 3 4 5. Без пробелов Telegram сочтёт код украденным и отменит.")


@router.message(owner_only(), F.text, F.func(lambda m: CTX.get("account") is not None and acc().pending_phone
                                              and parse_code(m.text)))
async def on_login_code(msg: Message):
    code = parse_code(msg.text)
    try:
        await msg.delete()
    except Exception:
        pass
    try:
        result = await acc().sign_in_code(code)
    except Exception as e:
        acc().pending_phone = None
        await answer(msg, f"Код не подошёл: {html.escape(str(e))}. Напиши /phone ещё раз.")
        return
    if result == "ok":
        await answer(msg, f"Подключил: <b>{html.escape(display_name(acc().me))}</b>. Сообщения пойдут сюда.")
    else:
        await answer(msg, "У аккаунта облачный пароль. Пришли: /pass пароль (сообщение сразу удалю).")


@router.message(Command("pass"), owner_only())
async def cmd_pass(msg: Message):
    pwd = (msg.text or "").partition(" ")[2].strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if not pwd:
        await answer(msg, "Напиши так: /pass пароль")
        return
    try:
        await acc().password(pwd)
        acc().pending_phone = None
        await answer(msg, f"Подключил: <b>{html.escape(display_name(acc().me))}</b>. Сообщения пойдут сюда.")
    except Exception as e:
        await answer(msg, f"Пароль не подошёл: {html.escape(str(e))}. Попробуй /login заново.")


@router.message(Command("logout"), owner_only())
async def cmd_logout(msg: Message):
    if not await acc().ready():
        await answer(msg, "Аккаунт и так не подключён.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, отключить", callback_data="lo:yes"),
        InlineKeyboardButton(text="Нет", callback_data="lo:no")]])
    await answer(msg, "Точно отключить аккаунт от бота? Сессия будет удалена, снова подключать придётся по QR.", kb)


@router.callback_query(F.data.startswith("lo:"), owner_only())
async def cb_logout(call: CallbackQuery):
    if call.data != "lo:yes":
        await call.answer("Оставил как есть")
        await call.message.edit_text("Аккаунт остаётся подключён.")
        return
    await acc().logout()
    await call.answer("Отключил")
    await call.message.edit_text("Отключил аккаунт, сессия удалена. Подключить снова: /login")


@router.message(Command("stop"), owner_only())
async def cmd_stop(msg: Message):
    store().set(f"target:{msg.chat.id}", None)
    await answer(msg, "Ок, больше никому не пишу. Отвечай свайпом на сообщение или выбери в /chats.")


@router.message(Command("off"), owner_only())
async def cmd_off(msg: Message):
    a = acc()
    if a.paused:
        await answer(msg, "Зеркало уже выключено. Включить: /on")
        return
    await a.pause()
    store().set("paused", True)
    await answer(msg, "⏸ Зеркало выключено. Сообщения и звонки аккаунта сюда не приходят, бот к аккаунту не "
                      "подключается. Вход НЕ удалён: включить обратно командой /on, без QR и кода.")


@router.message(Command("on"), owner_only())
async def cmd_on(msg: Message):
    a = acc()
    if not a.paused and await a.ready():
        await answer(msg, "Зеркало и так включено.")
        return
    try:
        ok = await a.resume()
    except Exception as e:
        await answer(msg, f"Не смог подключиться: {html.escape(str(e))}. Попробуй /on ещё раз через минуту.")
        return
    store().set("paused", None)
    if ok:
        await answer(msg, f"▶️ Зеркало включено: <b>{html.escape(display_name(a.me))}</b>. "
                          "Новые сообщения снова идут сюда. Пропущенное за паузу смотри в «Чатах».")
    else:
        await answer(msg, "Вход в аккаунт слетел, подключи заново: /login")


@router.message(Command("online"), owner_only())
async def cmd_online(msg: Message):
    a = acc()
    a.always_online = not a.always_online
    store().set("always_online", a.always_online)
    await a.set_online(a.always_online)
    await answer(msg, "Аккаунт теперь постоянно «в сети»." if a.always_online else
                 "Постоянный «в сети» выключен: аккаунт в сети только пока отвечаешь, потом через 30 с выходит.")


@router.message(Command("net"), owner_only())
async def cmd_net(msg: Message):
    a = acc()
    a.hands_off = not a.hands_off
    store().set("hands_off", a.hands_off)
    if a.hands_off:
        await answer(msg, "🌐 Бот больше не управляет «в сети». Зеркало работает как раньше, но статус аккаунта "
                          "теперь живёт сам: в сети, когда ты сидишь с телефона, и не в сети, когда вышел. "
                          "Вернуть управление: /net")
    else:
        await answer(msg, "🌐 Бот снова управляет «в сети»: " + ("постоянно в сети (/online)." if a.always_online else
                          "аккаунт не светится в сети, кроме моментов, когда отвечаешь из бота."))
        await a.set_online(a.always_online)


@router.message(Command("read"), owner_only())
async def cmd_read(msg: Message):
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    target = resolve_target(store(), msg.chat.id, reply_id)
    if not target:
        await answer(msg, "Какой чат прочитать? Ответь /read на сообщение или выбери чат в /chats.")
        return
    await acc().read(target["peer"])
    await answer(msg, f"Отметил прочитанным: {html.escape(target.get('name') or '')}")


@router.callback_query(F.data.startswith("rd:"), owner_only())
async def cb_read(call: CallbackQuery):
    _, peer, mid = call.data.split(":")
    peer, mid = int(peer), int(mid)
    try:
        await acc().read(peer, max_id=mid)
    except Exception as e:
        await call.answer(f"Не вышло: {e}"[:190], show_alert=True)
        return
    await call.answer("Прочитано")
    try:
        await call.message.edit_reply_markup(reply_markup=message_keyboard(peer, mid, read=True))
    except Exception as e:
        log.debug("кнопка прочтения: %s", e)


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@router.message(Command("chats"), owner_only())
async def cmd_chats(msg: Message):
    if not await acc().ready():
        await answer(msg, "Сначала подключи аккаунт: /login")
        return
    ds = [d for d in await acc().dialogs(limit=40) if d["type"] == "user" and not d["bot"]][:16]
    rows = [[InlineKeyboardButton(text=(f"{d['name']} · {d['unread']}" if d["unread"] else d["name"])[:60],
                                  callback_data=f"to:{d['id']}")] for d in ds]
    if config.WEBAPP_URL:
        rows.insert(0, [InlineKeyboardButton(text="Открыть все чаты", web_app=WebAppInfo(url=webapp_url()))])
    await answer(msg, "Кому писать?", InlineKeyboardMarkup(inline_keyboard=rows))
    await acc().stay_offline()


@router.callback_query(F.data.startswith("to:"), owner_only())
async def cb_target(call: CallbackQuery):
    peer = int(call.data[3:])
    try:
        name = display_name(await acc().client.get_entity(peer))
    except Exception:
        hit = store().route(call.message.chat.id, call.message.message_id) if call.message else None
        name = hit[2] if hit and hit[2] else str(peer)
    store().set(f"target:{call.message.chat.id}", {"peer": peer, "name": name})
    await acc().stay_offline()
    await call.answer(f"Пишешь: {name}")
    await call.message.answer(f"Пишешь <b>{html.escape(name)}</b>. Текст, кружок, голосовое, фото: всё уйдёт сюда. "
                              f"Хватит: /stop")


@router.callback_query(F.data.startswith(("cd:", "cr:")), owner_only())
async def cb_call(call: CallbackQuery):
    parts = call.data.split(":")
    call_id = int(parts[1])
    try:
        ok = await acc().discard_call(call_id)
    except Exception as e:
        await call.answer(f"Не сбросил: {e}"[:190], show_alert=True)
        return
    if parts[0] == "cr" and len(parts) > 2 and int(parts[2]):
        await acc().send_text(int(parts[2]), config.CALL_AUTOREPLY)
        await call.answer("Сбросил и написал, что перезвонишь")
    else:
        await call.answer("Сбросил" if ok else "Звонок уже закончился")


async def deliver(msg: Message, bot: Bot, target):
    peer, reply_to = target["peer"], target["reply_to"]
    a = acc()
    if msg.text:
        await a.send_text(peer, msg.text, reply_to=reply_to)
        return
    item, kind, dur, w, h = None, None, 0, 0, 0
    if msg.video_note:
        item, kind, dur, w = msg.video_note, "video_note", msg.video_note.duration, msg.video_note.length
        h = w
    elif msg.voice:
        item, kind, dur = msg.voice, "voice", msg.voice.duration
    elif msg.photo:
        item, kind = msg.photo[-1], "photo"
    elif msg.video:
        item, kind, dur, w, h = msg.video, "video", msg.video.duration, msg.video.width, msg.video.height
    elif msg.animation:
        item, kind = msg.animation, "animation"
    elif msg.audio:
        item, kind = msg.audio, "audio"
    elif msg.document:
        item, kind = msg.document, "document"
    elif msg.sticker:
        item, kind = msg.sticker, "sticker"
    if item is None:
        raise ValueError("такой тип пока не умею отправлять")
    folder = config.DATA / "tmp" / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    ext = {"video_note": ".mp4", "voice": ".ogg", "photo": ".jpg", "video": ".mp4", "animation": ".mp4",
           "sticker": ".webp"}.get(kind, "")
    name = getattr(item, "file_name", None) or f"{kind}{ext}"
    path = folder / name
    try:
        await bot.download(item, destination=path)
        await a.send_media(peer, path, kind, duration=dur or 0, width=w or 0, height=h or 0,
                           caption=msg.caption, reply_to=reply_to)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.message(owner_only(), F.chat.type == "private")
async def on_owner_message(msg: Message, bot: Bot):
    if msg.text and msg.text.startswith("/"):
        await answer(msg, "Не знаю такую команду. /start покажет все.")
        return
    if not await acc().ready():
        await answer(msg, "Аккаунт не подключён: /login")
        return
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    target = resolve_target(store(), msg.chat.id, reply_id)
    if not target:
        await answer(msg, "Кому отправить? Свайпни продублированное сообщение и ответь, или выбери в /chats.")
        return
    try:
        await deliver(msg, bot, target)
    except Exception as e:
        log.exception("не отправил")
        await answer(msg, f"Не отправил {html.escape(target.get('name') or '')}: {html.escape(str(e)[:200])}")
        return
    try:
        from aiogram.types import ReactionTypeEmoji
        await bot.set_message_reaction(msg.chat.id, msg.message_id, [ReactionTypeEmoji(emoji="👌")])
    except Exception:
        await answer(msg, f"Отправил: {html.escape(target.get('name') or '')}")
