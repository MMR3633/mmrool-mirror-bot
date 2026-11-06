"""Настоящие голосовые звонки Telegram через py-tgcalls: звук звонка идёт в браузер мини-аппа и обратно.
Формат звука между сервером и браузером: PCM 16 бит, моно, 48 кГц, кадры по 10 мс (960 байт)."""
import asyncio
import logging
import time

log = logging.getLogger("mirror.calls")
RATE = 48000
FRAME_BYTES = RATE // 100 * 2


def split_frames(buf: bytearray):
    out = []
    while len(buf) >= FRAME_BYTES:
        out.append(bytes(buf[:FRAME_BYTES]))
        del buf[:FRAME_BYTES]
    return out


class Calls:
    def __init__(self, account, hub):
        self.account = account
        self.hub = hub
        self.app = None
        self.state = {}
        self.listeners = {}
        self.pending = {}

    async def start(self):
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Device, Direction, StreamFrames
        self.app = PyTgCalls(self.account.client)

        @self.app.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def incoming(_, update):
            self.state[update.chat_id] = {"state": "ringing", "since": time.time()}
            await self.hub.broadcast({"type": "call", "peer": update.chat_id, "state": "ringing"})

        @self.app.on_update(filters.chat_update(ChatUpdate.Status.DISCARDED_CALL | ChatUpdate.Status.LEFT_CALL))
        async def ended(_, update):
            self.state.pop(update.chat_id, None)
            self.pending.pop(update.chat_id, None)
            await self.hub.broadcast({"type": "call", "peer": update.chat_id, "state": "ended"})
            for ws in list(self.listeners.pop(update.chat_id, set())):
                try:
                    await ws.close()
                except Exception:
                    pass

        @self.app.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def frames(_, update: StreamFrames):
            socks = self.listeners.get(update.chat_id)
            if not socks:
                return
            data = b"".join(f.frame for f in update.frames)
            for ws in list(socks):
                try:
                    await ws.send_bytes(data)
                except Exception:
                    socks.discard(ws)

        await self.app.start()
        log.info("звонки: py-tgcalls запущен")

    async def connect(self, peer):
        """Принять входящий (если звонят) или позвонить самому; звук берём из браузера."""
        from pytgcalls.types import AudioQuality, ExternalMedia, MediaStream, RecordStream
        from pytgcalls.types.raw import AudioParameters
        params = AudioParameters(RATE, 1)
        self.state[peer] = {"state": "connecting", "since": time.time()}
        await self.hub.broadcast({"type": "call", "peer": peer, "state": "connecting"})
        await self.app.play(peer, MediaStream(ExternalMedia.AUDIO, params, video_flags=MediaStream.Flags.IGNORE))
        await self.app.record(peer, RecordStream(audio=True, audio_parameters=params))
        self.state[peer] = {"state": "active", "since": time.time()}
        await self.hub.broadcast({"type": "call", "peer": peer, "state": "active"})

    async def hangup(self, peer):
        try:
            await self.app.leave_call(peer)
        finally:
            self.state.pop(peer, None)
            await self.hub.broadcast({"type": "call", "peer": peer, "state": "ended"})

    async def mic(self, peer, data: bytes):
        from pytgcalls.types import Device
        buf = self.pending.setdefault(peer, bytearray())
        buf.extend(data)
        for frame in split_frames(buf):
            await self.app.send_frame(peer, Device.MICROPHONE, frame)

    def attach(self, peer, ws):
        self.listeners.setdefault(peer, set()).add(ws)

    def detach(self, peer, ws):
        self.listeners.get(peer, set()).discard(ws)


def available():
    try:
        import ntgcalls  # noqa: F401
        import pytgcalls  # noqa: F401
        return True
    except Exception as e:
        log.warning("звонки выключены: нет py-tgcalls/ntgcalls (%s)", e)
        return False
