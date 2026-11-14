"""Minecraft Server Status Checker + Player Lookup, with a live unique-visitor counter.

Local development:
    pip install -r requirements.txt
    python server.py

For persistent visitor counts on Render, set DATABASE_URL to a PostgreSQL
connection string. SQLite is used automatically when DATABASE_URL is absent,
but Render's free web-service filesystem is ephemeral.
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from flask import Flask, Response, jsonify, make_response, request, send_from_directory
from mcstatus import BedrockServer, JavaServer

app = Flask(__name__, static_folder="static", static_url_path="/static")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USER_AGENT = "MinecraftServerChecker/2.0"


# --------------------------------------------------------------------------- #
# Visitor counter
# --------------------------------------------------------------------------- #
def _postgres_url():
    # Render may provide postgres:// while psycopg2 expects postgresql://.
    if DATABASE_URL.startswith("postgres://"):
        return "postgresql://" + DATABASE_URL[len("postgres://"):]
    return DATABASE_URL


def db_connect():
    url = _postgres_url()
    if url.startswith(("postgresql://", "postgresql+")):
        import psycopg2
        return psycopg2.connect(url), True

    db_path = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "visitors.db"))
    conn = sqlite3.connect(db_path, timeout=15)
    return conn, False


def init_visitor_db():
    conn, _ = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS visitors (
                visitor_id VARCHAR(64) PRIMARY KEY,
                first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()


# The count is polled by every open browser tab once per second, so the value is
# cached for a very short time to keep database load at ~1 query/second/worker.
_count_cache = {"t": 0.0, "v": 0}
_count_lock = threading.Lock()
COUNT_TTL = 0.9


def get_visitor_count(force=False):
    now = time.monotonic()
    with _count_lock:
        if not force and now - _count_cache["t"] < COUNT_TTL:
            return _count_cache["v"]
    conn, _ = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM visitors")
        value = int(cur.fetchone()[0])
    finally:
        conn.close()
    with _count_lock:
        _count_cache.update(t=time.monotonic(), v=value)
    return value


def register_visitor(visitor_id):
    conn, is_postgres = db_connect()
    try:
        cur = conn.cursor()
        placeholder = "%s" if is_postgres else "?"
        cur.execute(
            f"INSERT INTO visitors (visitor_id) VALUES ({placeholder}) "
            "ON CONFLICT(visitor_id) DO NOTHING",
            (visitor_id,)
        )
        inserted = cur.rowcount == 1
        conn.commit()
        return inserted
    finally:
        conn.close()


# Create the table when the application starts.
init_visitor_db()


def no_store(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def is_private(host: str) -> bool:
    """Block local/private network addresses unless ALLOW_PRIVATE=1 is set."""
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except socket.gaierror:
        pass
    return False


@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.route("/api/visitors")
def visitors():
    """Registers this browser (first-party random cookie, no IPs) and returns the count."""
    visitor_id = request.cookies.get("visitor_id", "")
    try:
        visitor_id = str(uuid.UUID(visitor_id))
        is_new = False
    except (ValueError, TypeError, AttributeError):
        visitor_id = str(uuid.uuid4())
        is_new = True

    try:
        register_visitor(visitor_id)
        count = get_visitor_count(force=True)
        response = make_response(jsonify(unique_visitors=count))
        if is_new:
            response.set_cookie(
                "visitor_id",
                visitor_id,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https",
                samesite="Lax",
                path="/"
            )
        return no_store(response)
    except Exception:
        app.logger.exception("Visitor counter database error")
        return no_store(jsonify(error="Visitor counter is temporarily unavailable.")), 503


@app.route("/api/visitors/count")
def visitors_count():
    """Read-only endpoint polled every second by the page."""
    try:
        return no_store(jsonify(unique_visitors=get_visitor_count()))
    except Exception:
        app.logger.exception("Visitor counter database error")
        return no_store(jsonify(error="Visitor counter is temporarily unavailable.")), 503


# --------------------------------------------------------------------------- #
# Server status
# --------------------------------------------------------------------------- #
@app.route("/api/status")
def status():
    kind = request.args.get("type", "java").lower()
    address = request.args.get("address", "").strip()
    if not address or len(address) > 253 or not re.fullmatch(r"[A-Za-z0-9.\-_:\[\]]+", address):
        return jsonify(error="Please enter a valid server address."), 400

    host = address.rsplit(":", 1)[0] if address.count(":") == 1 else address
    if not ALLOW_PRIVATE and is_private(host):
        return jsonify(error="Private or local network addresses are disabled."), 400

    try:
        if kind == "bedrock":
            srv = BedrockServer.lookup(address, timeout=4)
            st = srv.status()
            data = {
                "edition": "bedrock",
                "motd_raw": st.motd.raw,
                "version": st.version.name,
                "protocol": st.version.protocol,
                "brand": st.version.brand,
                "players_online": st.players.online,
                "players_max": st.players.max,
                "players": [],
                "map": st.map_name,
                "gamemode": st.gamemode,
                "icon": None,
            }
        else:
            srv = JavaServer.lookup(address, timeout=4)
            st = srv.status()
            sample = [{"name": p.name, "id": p.id} for p in (st.players.sample or [])]
            data = {
                "edition": "java",
                "motd_raw": st.motd.raw,
                "version": st.version.name,
                "protocol": st.version.protocol,
                "players_online": st.players.online,
                "players_max": st.players.max,
                "players": sample,
                "icon": st.icon,
            }
        data.update(online=True, host=srv.address.host, port=srv.address.port, latency=round(st.latency, 1))
    except Exception as e:
        return jsonify(online=False, error=f"Unable to reach server ({type(e).__name__}). Verify the address and edition."), 200

    # Server icons are returned as data URLs and are never written to disk.
    return no_store(jsonify(data))


# --------------------------------------------------------------------------- #
# Player lookup (Mojang API)
# --------------------------------------------------------------------------- #
class PlayerNotFound(Exception):
    pass


class UpstreamError(Exception):
    def __init__(self, message, code=502):
        super().__init__(message)
        self.code = code


NAME_RE = re.compile(r"[A-Za-z0-9_]{1,16}")
UUID_RE = re.compile(r"[0-9a-fA-F]{32}")
TEXTURE_HASH_RE = re.compile(r"[0-9a-f]{8,64}")
MAX_BODY = 2 * 1024 * 1024


def http_get(url, accept="application/json", timeout=6):
    """Returns (status_code, body_bytes). Never raises for HTTP error statuses."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise UpstreamError("Could not reach Mojang's servers. Please try again in a moment.") from e


def http_json(url):
    status, body = http_get(url)
    if body:
        try:
            return status, json.loads(body)
        except ValueError:
            return status, None
    return status, None


_player_cache = {}
_player_lock = threading.Lock()
PLAYER_TTL = 120
PLAYER_CACHE_MAX = 500


def _undash(value):
    return value.replace("-", "").lower()


def _dash(compact):
    return f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:32]}"


def _signed32(n):
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n & 0x80000000 else n


def uuid_int_array(compact):
    """UUID as the four signed 32-bit ints used by /give, /summon and NBT."""
    return [_signed32(int(compact[i:i + 8], 16)) for i in range(0, 32, 8)]


def offline_uuid(name):
    """UUID a cracked / offline-mode server assigns to this name (Java UUID.nameUUIDFromBytes)."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def default_skin_model(compact):
    """Mojang gives Alex (slim) to UUIDs with an odd hashCode, Steve (classic) otherwise."""
    a, b, c, d = uuid_int_array(compact)
    return "slim" if (a ^ b ^ c ^ d) & 1 else "classic"


def _texture_hash(url):
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname != "textures.minecraft.net":
        return None
    candidate = parsed.path.rsplit("/", 1)[-1]
    return candidate if TEXTURE_HASH_RE.fullmatch(candidate) else None


def _resolve_uuid(query):
    compact = query.replace("-", "")
    if UUID_RE.fullmatch(compact):
        return compact.lower()
    if not NAME_RE.fullmatch(query):
        raise ValueError("Enter a valid Java username (3-16 letters, numbers or _) or a UUID.")

    status, data = http_json(f"https://api.mojang.com/users/profiles/minecraft/{quote(query)}")
    if status == 200 and isinstance(data, dict) and data.get("id"):
        return data["id"].lower()
    if status in (204, 404):
        raise PlayerNotFound()
    if status == 429:
        raise UpstreamError("Mojang is rate-limiting requests right now. Please wait a few seconds.", 429)

    # Fallback endpoint if the primary one is having trouble.
    status, data = http_json(f"https://api.minecraftservices.com/minecraft/profile/lookup/name/{quote(query)}")
    if status == 200 and isinstance(data, dict) and data.get("id"):
        return data["id"].lower()
    if status in (204, 404):
        raise PlayerNotFound()
    raise UpstreamError("Mojang's API is not responding correctly. Please try again later.")


def build_player(query):
    compact = _resolve_uuid(query)
    status, profile = http_json(
        f"https://sessionserver.mojang.com/session/minecraft/profile/{compact}?unsigned=true"
    )
    if status in (204, 404):
        raise PlayerNotFound()
    if status == 429:
        raise UpstreamError("Mojang is rate-limiting requests right now. Please wait a few seconds.", 429)
    if status != 200 or not isinstance(profile, dict):
        raise UpstreamError("Mojang's API is not responding correctly. Please try again later.")

    name = profile.get("name") or query
    textures, timestamp = {}, None
    for prop in profile.get("properties", []):
        if prop.get("name") == "textures":
            try:
                decoded = json.loads(base64.b64decode(prop.get("value", "")))
                textures = decoded.get("textures", {}) or {}
                timestamp = decoded.get("timestamp")
            except (ValueError, TypeError):
                pass

    skin = textures.get("SKIN") or {}
    cape = textures.get("CAPE") or {}
    skin_hash = _texture_hash(skin.get("url"))
    cape_hash = _texture_hash(cape.get("url"))
    custom_model = (skin.get("metadata") or {}).get("model")
    model = "slim" if custom_model == "slim" else ("classic" if skin_hash else default_skin_model(compact))

    fetched_at = None
    if isinstance(timestamp, (int, float)):
        fetched_at = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat()

    return {
        "name": name,
        "uuid": _dash(compact),
        "uuid_compact": compact,
        "uuid_ints": uuid_int_array(compact),
        "offline_uuid": offline_uuid(name),
        "skin_hash": skin_hash,
        "skin_url": skin.get("url") if skin_hash else None,
        "skin_model": model,
        "cape_hash": cape_hash,
        "cape_url": cape.get("url") if cape_hash else None,
        "has_skin": bool(skin_hash),
        "legacy": bool(profile.get("legacy")),
        "textures_updated": fetched_at,
        "namemc_url": f"https://namemc.com/profile/{compact}",
    }


@app.route("/api/player")
def player():
    query = request.args.get("query", "").strip()
    if not query or len(query) > 40:
        return jsonify(error="Please enter a player name or UUID."), 400

    key = query.lower().replace("-", "")
    now = time.monotonic()
    with _player_lock:
        hit = _player_cache.get(key)
        if hit and now - hit[0] < PLAYER_TTL:
            return no_store(jsonify(hit[1]))

    try:
        data = build_player(query)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except PlayerNotFound:
        return jsonify(error=f"No Java Edition player found for \"{query}\"."), 404
    except UpstreamError as e:
        return jsonify(error=str(e)), e.code

    with _player_lock:
        if len(_player_cache) >= PLAYER_CACHE_MAX:
            _player_cache.clear()
        _player_cache[key] = (now, data)
        _player_cache[data["uuid_compact"]] = (now, data)
    return no_store(jsonify(data))


@app.route("/api/texture/<string:texture_hash>.png")
def texture(texture_hash):
    """Same-origin proxy for skins/capes so they can be drawn on a canvas and downloaded."""
    if not TEXTURE_HASH_RE.fullmatch(texture_hash):
        return jsonify(error="Invalid texture."), 400
    try:
        status, body = http_get(
            f"https://textures.minecraft.net/texture/{texture_hash}", accept="image/png"
        )
    except UpstreamError as e:
        return jsonify(error=str(e)), e.code
    if status != 200 or not body.startswith(b"\x89PNG"):
        return jsonify(error="Texture not found."), 404

    resp = Response(body, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    if request.args.get("download") == "1":
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", request.args.get("name", "texture"))[:40] or "texture"
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe}.png"'
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
