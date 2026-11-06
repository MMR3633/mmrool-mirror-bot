import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from mirror import bot as mbot
from mirror import config, web
from mirror.account import Account, peer_of
from mirror.core import Store, display_name

log = logging.getLogger("mirror")


async def setup_menu(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Что умеет бот"),
        BotCommand(command="chats", description="Кому писать"),
        BotCommand(command="stop", description="Перестать писать выбранному"),
        BotCommand(command="read", description="Отметить прочитанным выбранный чат"),
        BotCommand(command="online", description="«В сети» постоянно: вкл/выкл"),
        BotCommand(command="net", description="Не трогать «в сети»: вкл/выкл"),
        BotCommand(command="off", description="Выключить зеркало (вход сохраняется)"),
        BotCommand(command="on", description="Включить зеркало"),
        BotCommand(command="login", description="Подключить аккаунт по QR"),
        BotCommand(command="phone", description="Подключить по номеру"),
        BotCommand(command="logout", description="Отключить аккаунт"),
    ])
    if config.WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Чаты", web_app=WebAppInfo(url=config.WEBAPP_URL + "/")))


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not config.BOT_TOKEN or not config.OWNER_ID or not config.API_HASH:
        raise SystemExit("нет BOT_TOKEN, OWNER_ID или API_HASH в .env")
    config.DATA.mkdir(parents=True, exist_ok=True)
    store = Store(config.DATA / "mirror.db")
    store.prune()
    account = Account(config.DATA)
    account.paused = bool(store.get("paused", False))
    hub = web.Hub()
    session = AiohttpSession(proxy=config.PROXY) if config.PROXY else None
    bot = Bot(config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    mbot.CTX.update(bot=bot, account=account, store=store, hub=hub)

    async def to_webapp(event):
        m = event.message
        name = None
        if not m.is_private and not m.out:
            try:
                name = display_name(await event.get_sender())
            except Exception:
                pass
        await hub.broadcast({"type": "message", "peer": peer_of(event), "message": account.message_json(m, name)})

    async def read_to_webapp(update):
        from telethon import utils
        peer = getattr(update, "peer", None)
        if peer is not None:
            await hub.broadcast({"type": "read", "peer": utils.get_peer_id(peer), "max_id": update.max_id,
                                 "outbox": type(update).__name__.endswith("Outbox")})

    account.on_message += [mbot.push_incoming, to_webapp]
    account.on_read.append(read_to_webapp)
    account.on_call.append(mbot.push_call)
    calls = None
    if config.CALLS:
        from mirror import calls as mcalls
        if mcalls.available():
            calls = mcalls.Calls(account, hub)
    mbot.CTX["calls"] = calls

    async def boot():
        await account.start()
        while calls is not None:
            if await account.ready():
                try:
                    await calls.start()
                except Exception:
                    log.exception("звонки не запустились")
                break
            await asyncio.sleep(10)

    asyncio.create_task(boot())
    account.always_online = bool(store.get("always_online", False))
    account.hands_off = bool(store.get("hands_off", False))
    asyncio.create_task(account.keep_online())
    runner = await web.start(web.make_app(account, config.BOT_TOKEN, config.OWNER_ID, hub, calls, mbot.allowed_id),
                             config.WEB_HOST, config.WEB_PORT)
    dp = Dispatcher()
    dp.include_router(mbot.router)
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            me = await bot.me()
            mbot.CTX["bot_id"] = me.id
            log.info("бот @%s запущен", me.username)
            break
        except Exception as e:
            log.warning("Telegram не отвечает (%s), повтор через 15 сек", e)
            await asyncio.sleep(15)
    try:
        await setup_menu(bot)
    except Exception as e:
        log.warning("меню не поставил: %s", e)
    if config.WEB_HOST.startswith("127.") and os.environ.get("LOCAL_TUNNEL", "1") == "1":
        from mirror.tunnel import Tunnel
        async def on_new_url(url):
            await setup_menu(bot)
            from mirror.bot import message_keyboard
            fixed = 0
            for chat_id, bot_msg_id, peer_id, tg_msg_id in store.recent():
                try:
                    await bot.edit_message_reply_markup(chat_id=chat_id, message_id=bot_msg_id,
                                                        reply_markup=message_keyboard(peer_id, tg_msg_id))
                    fixed += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            log.info("кнопки обновлены на новый адрес: %s сообщений", fixed)
        asyncio.create_task(Tunnel(on_new_url).run())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
