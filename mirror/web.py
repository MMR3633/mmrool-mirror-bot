import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web

from . import config
from .core import check_init_data, make_session, read_session

log = logging.getLogger("mirror.web")
PAGE = config.ROOT / "webapp" / "index.html"
HEADERS = {
    "Content-Security-Policy": "frame-ancestors https://web.telegram.org https://*.telegram.org",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
MEDIA_TYPES = {"photo": "image/jpeg", "video_note": "video/mp4", "video": "video/mp4", "voice": "audio/ogg",
               "animation": "video/mp4", "sticker": "image/webp"}
MAX_UPLOAD = 50 * 1024 * 1024


class Hub:
    def __init__(self):
        self.sockets = set()

    async def broadcast(self, data):
        dead = []
        text = json.dumps(data, ensure_ascii=False)
        for ws in list(self.sockets):
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)


def reply(data, status=200):
    return web.json_response(data, status=status, headers={**HEADERS, "Cache-Control": "no-store"},
                             dumps=lambda d: json.dumps(d, ensure_ascii=False, separators=(",", ":")))


def token_of(request):
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else request.query.get("t", "")


def owner_of(request):
    uid = read_session(token_of(request), request.app["token"])
    if not uid or not request.app["allowed"](uid):
        raise web.HTTPUnauthorized(text="нужен вход", headers=HEADERS)
    return uid


def int_arg(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"плохой параметр {name}", headers=HEADERS)


async def page(request):
    return web.FileResponse(PAGE, headers={**HEADERS, "Cache-Control": "no-cache"})


async def login(request):
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="нужен JSON", headers=HEADERS)
    uid = check_init_data(str(body.get("initData", "")), request.app["token"])
    if not uid or not request.app["allowed"](uid):
        return reply({"error": "Нет доступа. Сначала введи пароль в боте."}, 403)
    acc = request.app["account"]
    ready = await acc.ready()
    me = None
    if ready and acc.me:
        from .core import display_name
        me = display_name(acc.me)
    return reply({"token": make_session(uid, request.app["token"]), "ready": ready, "me": me})


async def chats(request):
    owner_of(request)
    acc = request.app["account"]
    if not await acc.ready():
        return reply({"ready": False, "chats": []})
    return reply({"ready": True, "chats": await acc.dialogs(limit=80)})


async def messages(request):
    owner_of(request)
    peer = int_arg(request.match_info["peer"], "peer")
    before = int_arg(request.query.get("before", "0"), "before")
    acc = request.app["account"]
    return reply(await acc.history(peer, limit=40, before=before))


async def mark_read(request):
    owner_of(request)
    peer = int_arg(request.match_info["peer"], "peer")
    await request.app["account"].read(peer)
    await request.app["hub"].broadcast({"type": "read", "peer": peer, "max_id": 0, "outbox": False})
    return reply({"ok": True})


async def send(request):
    owner_of(request)
    peer = int_arg(request.match_info["peer"], "peer")
    acc = request.app["account"]
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        text, kind, path, folder, reply_to, duration = "", "document", None, None, None, 0
        async for part in reader:
            if part.name == "text":
                text = (await part.text())[:4096]
            elif part.name == "kind":
                kind = (await part.text()).strip()
            elif part.name == "reply_to":
                reply_to = int((await part.text()) or 0) or None
            elif part.name == "duration":
                duration = int(float((await part.text()) or 0))
            elif part.name == "file":
                folder = config.DATA / "tmp" / uuid.uuid4().hex
                folder.mkdir(parents=True, exist_ok=True)
                safe = Path(part.filename or "file").name[:80] or "file"
                path = folder / safe
                size = 0
                with open(path, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            shutil.rmtree(folder, ignore_errors=True)
                            raise web.HTTPRequestEntityTooLarge(MAX_UPLOAD, size, headers=HEADERS)
                        f.write(chunk)
        if kind not in ("photo", "video", "voice", "video_note", "document", "audio"):
            kind = "document"
        if not path:
            raise web.HTTPBadRequest(text="нет файла", headers=HEADERS)
        try:
            m = await acc.send_media(peer, path, kind, duration=duration, caption=text or None, reply_to=reply_to)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    else:
        body = await request.json()
        text = str(body.get("text", "")).strip()[:4096]
        if not text:
            raise web.HTTPBadRequest(text="пустое сообщение", headers=HEADERS)
        m = await acc.send_text(peer, text, reply_to=body.get("reply_to") or None)
    return reply({"ok": True, "message": acc.message_json(m)})


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def moov_first(path):
    with open(path, "rb") as f:
        head = f.read(4 * 1024 * 1024)
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    return moov != -1 and (mdat == -1 or moov < mdat)


async def run_ffmpeg(args):
    exe = ffmpeg_exe()
    if not exe:
        return False
    proc = await asyncio.create_subprocess_exec(exe, "-v", "error", "-y", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("ffmpeg: %s", err.decode("utf-8", "ignore")[-300:])
    return proc.returncode == 0


async def playable(path):
    """Видео для iPhone: mp4 с moov в начале (иначе WebView не стартует по Range), webm перекодируем в h264."""
    path = Path(path)
    suf = path.suffix.lower()
    if suf in (".mp4", ".mov", ".m4v"):
        if moov_first(path):
            return path
        tmp = path.with_name(path.stem + "_fs.mp4")
        if await run_ffmpeg(["-i", str(path), "-map", "0", "-c", "copy", "-movflags", "+faststart", str(tmp)]):
            out = path.with_suffix(".mp4")
            tmp.replace(out)
            if out != path:
                path.unlink(missing_ok=True)
            return out
        tmp.unlink(missing_ok=True)
        return path
    if suf in (".oga", ".ogg", ".opus", ".wav", ".amr"):
        out = path.with_suffix(".m4a")
        if await run_ffmpeg(["-i", str(path), "-vn", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(out)]):
            path.unlink(missing_ok=True)
            return out
        out.unlink(missing_ok=True)
        return path
    if suf in (".webm", ".mkv"):
        out = path.with_suffix(".mp4")
        if await run_ffmpeg(["-i", str(path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                             "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                             "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]):
            path.unlink(missing_ok=True)
            return out
        out.unlink(missing_ok=True)
    return path


async def cached_media(acc, peer, mid, thumb=False):
    cache = config.DATA / "cache" / str(peer)
    key = f"{mid}{'_t' if thumb else ''}"
    hits = list(cache.glob(key + ".*")) if cache.exists() else []
    hits = [h for h in hits if not h.name.endswith("_fs.mp4")]
    if not hits:
        m = await acc.get_message(peer, mid)
        if not m or not m.media:
            return None
        tmp = cache / ("tmp_" + uuid.uuid4().hex)
        path = await acc.download(m, tmp, thumb=-1 if thumb else None)
        if not path:
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        final = cache / (key + (Path(path).suffix or ".bin"))
        Path(path).replace(final)
        shutil.rmtree(tmp, ignore_errors=True)
        hits = [final]
    if not thumb:
        hits[0] = await playable(hits[0])
    return hits[0]


async def media(request):
    owner_of(request)
    peer = int_arg(request.match_info["peer"], "peer")
    mid = int_arg(request.match_info["mid"], "mid")
    thumb = request.query.get("thumb") == "1"
    path = await cached_media(request.app["account"], peer, mid, thumb)
    if not path:
        raise web.HTTPNotFound(headers=HEADERS)
    resp = web.FileResponse(path, headers={**HEADERS, "Cache-Control": "private, max-age=86400"})
    resp.force_close()
    return resp


BOT_SEND = {"video_note": ("sendVideoNote", "video_note"), "video": ("sendVideo", "video"),
            "animation": ("sendAnimation", "animation"), "voice": ("sendVoice", "voice"),
            "audio": ("sendAudio", "audio")}


async def to_bot(request):
    """Медиа в чат бота: Telegram сам проигрывает кружки и видео, не нужен медленный туннель."""
    uid = owner_of(request)
    peer = int_arg(request.match_info["peer"], "peer")
    mid = int_arg(request.match_info["mid"], "mid")
    kind = request.query.get("kind", "video")
    path = await cached_media(request.app["account"], peer, mid)
    if not path:
        return reply({"error": "файл не найден"}, 404)
    method, field = BOT_SEND.get(kind, ("sendDocument", "document"))
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("chat_id", str(uid))
    if method == "sendVideo":
        form.add_field("supports_streaming", "true")
    with open(path, "rb") as f:
        form.add_field(field, f.read(), filename=Path(path).name)
    url = f"https://api.telegram.org/bot{request.app['token']}/{method}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(url, data=form, proxy=config.PROXY or None) as r:
            data = await r.json(content_type=None)
    if not data.get("ok"):
        return reply({"error": data.get("description", "Telegram не принял файл")}, 502)
    return reply({"ok": True})


async def avatar(request):
    owner_of(request)
    peer = int_arg(request.match_info["peer"], "peer")
    cache = config.DATA / "cache" / "avatars"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{peer}.jpg"
    if not path.exists():
        raw = await request.app["account"].avatar(peer)
        if not raw:
            raise web.HTTPNotFound(headers=HEADERS)
        path.write_bytes(raw)
    return web.FileResponse(path, headers={**HEADERS, "Cache-Control": "private, max-age=21600"})


async def socket(request):
    owner_of(request)
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    hub: Hub = request.app["hub"]
    hub.sockets.add(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.sockets.discard(ws)
    return ws


async def call_action(request):
    owner_of(request)
    calls = request.app["calls"]
    if calls is None:
        return reply({"error": "звонки выключены на сервере (CALLS=1 и py-tgcalls)"}, 400)
    peer = int_arg(request.match_info["peer"], "peer")
    body = await request.json()
    action = body.get("action")
    if action in ("accept", "start"):
        asyncio.create_task(_connect(calls, peer, request.app["hub"]))
        return reply({"ok": True, "state": "connecting"})
    if action == "hangup":
        await calls.hangup(peer)
        return reply({"ok": True, "state": "ended"})
    raise web.HTTPBadRequest(text="нет такого действия", headers=HEADERS)


async def _connect(calls, peer, hub):
    try:
        await calls.connect(peer)
    except Exception as e:
        log.exception("звонок %s не соединился", peer)
        calls.state.pop(peer, None)
        await hub.broadcast({"type": "call", "peer": peer, "state": "failed", "error": str(e)[:200]})


async def call_state(request):
    owner_of(request)
    calls = request.app["calls"]
    return reply({"enabled": calls is not None, "calls": {str(k): v for k, v in (calls.state if calls else {}).items()}})


async def call_audio(request):
    owner_of(request)
    calls = request.app["calls"]
    if calls is None:
        raise web.HTTPNotFound(headers=HEADERS)
    peer = int_arg(request.match_info["peer"], "peer")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
    await ws.prepare(request)
    calls.attach(peer, ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and peer in calls.state and calls.state[peer]["state"] == "active":
                try:
                    await calls.mic(peer, msg.data)
                except Exception as e:
                    log.debug("кадр микрофона: %s", e)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        calls.detach(peer, ws)
    return ws


@web.middleware
async def presence_guard(request, handler):
    try:
        return await handler(request)
    finally:
        if request.path.startswith("/api/") and not request.path.endswith("/send") and hasattr(request.app["account"], "stay_offline"):
            asyncio.ensure_future(_quiet(request.app["account"]))


async def _quiet(account):
    try:
        await account.stay_offline()
    except Exception as e:
        log.debug("оффлайн после запроса: %s", e)


@web.middleware
async def errors_inner(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        log.exception("ошибка %s", request.path)
        return reply({"error": str(e)[:300]}, 500)


def make_app(account, token, owner, hub=None, calls=None, allowed=None):
    app = web.Application(middlewares=[presence_guard, errors_inner], client_max_size=MAX_UPLOAD + 1024 * 1024)
    app["account"], app["token"], app["owner"] = account, token, owner
    app["hub"] = hub or Hub()
    app["calls"] = calls
    app["allowed"] = allowed or (lambda uid: uid == owner)
    app.router.add_get("/", page)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/chats", chats)
    app.router.add_get("/api/chat/{peer}", messages)
    app.router.add_post("/api/chat/{peer}/send", send)
    app.router.add_post("/api/chat/{peer}/read", mark_read)
    app.router.add_get("/api/media/{peer}/{mid}", media)
    app.router.add_post("/api/media/{peer}/{mid}/tobot", to_bot)
    app.router.add_get("/api/avatar/{peer}", avatar)
    app.router.add_get("/api/ws", socket)
    app.router.add_get("/api/calls", call_state)
    app.router.add_post("/api/call/{peer}", call_action)
    app.router.add_get("/api/call/{peer}/audio", call_audio)
    return app


async def start(app, host, port):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("мини-апп слушает http://%s:%s", host, port)
    return runner
