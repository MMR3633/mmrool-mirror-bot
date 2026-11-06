import asyncio
import logging
import re
import subprocess

import aiohttp

from mirror import config

log = logging.getLogger("mirror.tunnel")
URL_RE = re.compile(r"https://[a-z0-9]+\.lhr\.life")
SSH_ARGS = ["ssh.exe", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes", "-R", f"80:127.0.0.1:{config.WEB_PORT}", "nokey@localhost.run"]


def latest_url(text):
    found = URL_RE.findall(text or "")
    return found[-1] if found else None


def read_log(path):
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def save_env_url(url):
    path = config.ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    out, seen = [], False
    for line in lines:
        if line.startswith("WEBAPP_URL="):
            out.append(f"WEBAPP_URL={url}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"WEBAPP_URL={url}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def kill_old_ssh():
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | Where-Object { $_.CommandLine -match 'localhost\\.run' "
           f"-and $_.CommandLine -match '{config.WEB_PORT}' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
    subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=60)


async def alive(url):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.get(url + "/") as r:
                return r.status < 500
    except Exception:
        return False


class Tunnel:
    def __init__(self, on_url):
        self.log_path = config.DATA / "tunnel.log"
        self.on_url = on_url
        self.proc = None

    def start_ssh(self):
        kill_old_ssh()
        out = open(self.log_path, "w", encoding="utf-8")
        err = open(config.DATA / "tunnel_err.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(SSH_ARGS, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info("туннель перезапущен, pid %s", self.proc.pid)

    async def wait_url(self, timeout=90):
        for _ in range(timeout // 2):
            url = latest_url(read_log(self.log_path))
            if url:
                return url
            if self.proc and self.proc.poll() is not None:
                return None
            await asyncio.sleep(2)
        return None

    async def apply(self, url):
        if url and url != config.WEBAPP_URL:
            log.info("новый адрес мини-аппа: %s", url)
            config.WEBAPP_URL = url
            save_env_url(url)
            try:
                await self.on_url(url)
            except Exception as e:
                log.warning("меню с новым адресом не поставил: %s", e)

    async def run(self):
        fails = 0
        while True:
            url = latest_url(read_log(self.log_path))
            await self.apply(url)
            dead = self.proc is not None and self.proc.poll() is not None
            ok = (not dead) and url and await alive(url)
            fails = 0 if ok else fails + 1
            if dead or fails >= 4 or not url:
                self.start_ssh()
                await self.apply(await self.wait_url())
                fails = 0
                await asyncio.sleep(20)
            await asyncio.sleep(15)
