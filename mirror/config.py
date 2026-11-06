import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
API_ID = int(os.environ.get("API_ID", "2040") or 2040)
API_HASH = os.environ.get("API_HASH", "")
PROXY = os.environ.get("PROXY", "")
DATA = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
WEB_PORT = int(os.environ.get("PORT") or os.environ.get("WEB_PORT") or 8095)
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "")
OFFLINE_AFTER = float(os.environ.get("OFFLINE_AFTER", "3") or 3)
AUTO_READ = os.environ.get("AUTO_READ", "0") == "1"
SESSION_STRING = os.environ.get("SESSION_STRING", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
MIRROR_GROUPS = os.environ.get("MIRROR_GROUPS", "0") == "1"
MIRROR_MUTED = os.environ.get("MIRROR_MUTED", "1") == "1"
CALLS = os.environ.get("CALLS", "0") == "1"
CALL_AUTOREPLY = os.environ.get("CALL_AUTOREPLY", "Не могу сейчас говорить, перезвоню чуть позже")


def telethon_proxy():
    if not PROXY:
        return None
    scheme, _, rest = PROXY.partition("://")
    host, _, port = rest.rstrip("/").rpartition(":")
    return (scheme.lower(), host, int(port))
