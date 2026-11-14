"""Minecraft Server Status Checker + Player Lookup + PixelCraft Studio.

Local development:
    pip install -r requirements.txt
    python server.py

Production (Render): see README.md. Set DATABASE_URL for persistent visitor counts.
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO
from urllib.parse import quote, urlparse

import numpy as np
from flask import Flask, Response, jsonify, make_response, request, send_file, send_from_directory
from mcstatus import BedrockServer, JavaServer
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

PIXEL_MAX_PIXELS = 20_000_000
Image.MAX_IMAGE_PIXELS = PIXEL_MAX_PIXELS  # Pillow raises DecompressionBombError above 2x this

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 9 * 1024 * 1024
# Render (and most hosts) sit behind one reverse proxy: trust exactly one hop so
# request.remote_addr is the real client IP and cannot be spoofed with X-Forwarded-For.
if os.environ.get("TRUST_PROXY", "1") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USER_AGENT = "MinecraftServerChecker/2.1"

if ALLOW_PRIVATE:
    app.logger.warning("ALLOW_PRIVATE=1: the status checker can reach private/internal addresses. "
                       "Never enable this on a public deployment.")


# --------------------------------------------------------------------------- #
# Security headers, error handling, rate limiting
# --------------------------------------------------------------------------- #
CSP = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: blob: https://minotar.net",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
])


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.is_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


def no_store(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.errorhandler(HTTPException)
def http_error(e):
    if request.path.startswith("/api/"):
        return no_store(jsonify(error=e.description or e.name)), e.code
    return e


class RateLimiter:
    """Small in-memory sliding-window limiter (per worker process)."""

    def __init__(self, max_keys=50_000):
        self._hits = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._last_sweep = time.monotonic()

    def check(self, key, limit, window):
        """Returns 0 when allowed, otherwise the number of seconds to wait."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep > 60:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, int(window - (now - hits[0])) + 1)
            hits.append(now)
            return 0

    def _sweep(self, now):
        self._last_sweep = now
        for k in [k for k, d in self._hits.items() if not d or now - d[-1] > 300]:
            del self._hits[k]
        if len(self._hits) > self._max_keys:
            self._hits.clear()


_limiter = RateLimiter()


def rate_limited(name, limit, window=60):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            wait = _limiter.check((name, request.remote_addr or "?"), limit, window)
            if wait:
                resp = jsonify(error="Too many requests. Please slow down and try again shortly.")
                resp.status_code = 429
                resp.headers["Retry-After"] = str(wait)
                return no_store(resp)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


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


# The page polls the count every ~20 s per open tab; a short cache keeps database
# load negligible even with many visitors.
_count_cache = {"t": 0.0, "v": 0}
_count_lock = threading.Lock()
COUNT_TTL = 5.0


def get_visitor_count(force=False):
    now = time.monotonic()
    with _count_lock:
        if not force and _count_cache["t"] and now - _count_cache["t"] < COUNT_TTL:
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


@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nDisallow: /api/\n", mimetype="text/plain")


@app.route("/api/visitors")
@rate_limited("visitors", 10)
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
                "visitor_id", visitor_id,
                max_age=60 * 60 * 24 * 365, httponly=True,
                secure=request.is_secure, samesite="Lax", path="/",
            )
        return no_store(response)
    except Exception:
        app.logger.exception("Visitor counter database error")
        return no_store(jsonify(error="Visitor counter is temporarily unavailable.")), 503


@app.route("/api/visitors/count")
@rate_limited("visitors_count", 30)
def visitors_count():
    """Read-only endpoint polled by the page."""
    try:
        return no_store(jsonify(unique_visitors=get_visitor_count()))
    except Exception:
        app.logger.exception("Visitor counter database error")
        return no_store(jsonify(error="Visitor counter is temporarily unavailable.")), 503


# --------------------------------------------------------------------------- #
# Server status (SSRF-hardened)
# --------------------------------------------------------------------------- #
ADDRESS_RE = re.compile(r"[A-Za-z0-9.\-_:\[\]]+")
ICON_RE = re.compile(r"data:image/png;base64,[A-Za-z0-9+/=]{1,300000}")
_status_slots = threading.BoundedSemaphore(int(os.environ.get("STATUS_CONCURRENCY", "16")))


def split_host_port(address):
    """Parses 'host', 'host:port', '[v6]' or '[v6]:port'. Raises ValueError when malformed."""
    m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?", address)
    if m:
        host, port = m.group(1), m.group(2)
    elif address.count(":") == 1:
        host, port = address.split(":")
    elif address.count(":") == 0:
        host, port = address, None
    else:  # bare IPv6 literal
        ipaddress.IPv6Address(address)
        host, port = address, None
    if not host or (port is not None and not (port.isdigit() and 1 <= int(port) <= 65535)):
        raise ValueError("bad address")
    return host, port


def classify_host(host):
    """'public' | 'private' | 'unresolved'. Every resolved address must be globally routable."""
    try:
        infos = socket.getaddrinfo(host.strip("[]"), None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return "unresolved"
    if not infos:
        return "unresolved"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global:
            return "private"
    return "public"


def clip(value, n=200):
    return value[:n] if isinstance(value, str) else value


def safe_icon(icon):
    return icon if isinstance(icon, str) and ICON_RE.fullmatch(icon) else None


def safe_motd(raw):
    try:
        return raw if len(json.dumps(raw, default=str)) <= 20000 else ""
    except (TypeError, ValueError):
        return ""


@app.route("/api/status")
@rate_limited("status", 60)
def status():
    kind = request.args.get("type", "java").lower()
    address = request.args.get("address", "").strip()
    bad = jsonify(error="Please enter a valid server address."), 400
    if not address or len(address) > 253 or not ADDRESS_RE.fullmatch(address):
        return bad
    try:
        host, _ = split_host_port(address)
    except ValueError:
        return bad

    private_msg = (jsonify(error="Private or local network addresses are disabled."), 400)
    unreachable = (jsonify(online=False, error="Unable to reach server. Verify the address and edition."), 200)
    if not ALLOW_PRIVATE:
        kind_of_host = classify_host(host)
        if kind_of_host == "private":
            return private_msg
        if kind_of_host == "unresolved":
            return unreachable

    if not _status_slots.acquire(blocking=False):
        return no_store(jsonify(error="The checker is busy. Try again in a moment.")), 503
    try:
        if kind == "bedrock":
            srv = BedrockServer.lookup(address, timeout=4)
        else:
            srv = JavaServer.lookup(address, timeout=4)
        # SRV records can redirect to a different (possibly internal) host: re-check the target.
        if not ALLOW_PRIVATE and classify_host(srv.address.host) != "public":
            return private_msg
        st = srv.status()
        if kind == "bedrock":
            data = {
                "edition": "bedrock",
                "motd_raw": safe_motd(st.motd.raw),
                "version": clip(st.version.name, 100),
                "protocol": st.version.protocol,
                "brand": clip(st.version.brand, 100),
                "players_online": st.players.online,
                "players_max": st.players.max,
                "players": [],
                "map": clip(st.map_name, 100),
                "gamemode": clip(st.gamemode, 50),
                "icon": None,
            }
        else:
            sample = [{"name": clip(p.name, 64)} for p in (st.players.sample or [])[:50]]
            data = {
                "edition": "java",
                "motd_raw": safe_motd(st.motd.raw),
                "version": clip(st.version.name, 100),
                "protocol": st.version.protocol,
                "players_online": st.players.online,
                "players_max": st.players.max,
                "players": sample,
                "icon": safe_icon(st.icon),
            }
        data.update(online=True, host=clip(srv.address.host, 253), port=srv.address.port,
                    latency=round(st.latency, 1))
    except Exception:
        app.logger.debug("status check failed", exc_info=True)
        return unreachable
    finally:
        _status_slots.release()

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_get(url, accept="application/json", timeout=6):
    """Returns (status_code, body_bytes). Never follows redirects, never raises for HTTP statuses."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with _opener.open(req, timeout=timeout) as resp:
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
_notfound_cache = {}
_player_lock = threading.Lock()
PLAYER_TTL = 120
NOTFOUND_TTL = 60
PLAYER_CACHE_MAX = 500


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
    if not NAME_RE.fullmatch(str(name)):
        name = query if NAME_RE.fullmatch(query) else compact[:16]
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
@rate_limited("player", 20)
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
        miss = _notfound_cache.get(key)
        if miss and now - miss < NOTFOUND_TTL:
            return jsonify(error=f"No Java Edition player found for \"{query}\"."), 404

    try:
        data = build_player(query)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except PlayerNotFound:
        with _player_lock:
            if len(_notfound_cache) >= PLAYER_CACHE_MAX:
                _notfound_cache.clear()
            _notfound_cache[key] = now
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
@rate_limited("texture", 60)
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


# --------------------------------------------------------------------------- #
# Pixel Art -> Litematica schematic
# --------------------------------------------------------------------------- #
PIXEL_MAX_UPLOAD = 8 * 1024 * 1024
PIXEL_RESOLUTIONS = (16, 32, 48, 64, 96, 128)

# Approximate in-game colours per block family. The browser fetches this same table
# from /api/pixel-art/palette so preview and export use identical colours.
# Order matters (ties resolve to the first entry) and is the same for every family.
_NAMES = ["white", "light_gray", "gray", "black", "brown", "red", "orange", "yellow",
          "lime", "green", "cyan", "light_blue", "blue", "purple", "magenta", "pink"]
_RGB = {
    "concrete": [(207, 213, 214), (142, 142, 134), (62, 68, 71), (8, 10, 15), (115, 71, 42),
                 (161, 39, 34), (224, 97, 0), (241, 175, 21), (94, 168, 24), (73, 91, 36),
                 (21, 137, 145), (58, 175, 217), (53, 57, 157), (122, 42, 173), (189, 68, 179),
                 (237, 141, 172)],
    "wool": [(233, 236, 236), (142, 142, 134), (62, 68, 71), (20, 21, 25), (114, 71, 40),
             (160, 39, 34), (240, 118, 19), (248, 198, 39), (112, 185, 25), (84, 109, 27),
             (21, 137, 145), (58, 179, 218), (53, 57, 157), (137, 50, 184), (189, 68, 179),
             (237, 141, 172)],
    "terracotta": [(209, 177, 161), (135, 107, 98), (57, 41, 35), (37, 22, 16), (76, 50, 35),
                   (142, 60, 46), (159, 82, 36), (186, 133, 36), (103, 117, 53), (76, 82, 42),
                   (87, 92, 92), (112, 108, 138), (76, 62, 92), (122, 73, 88), (149, 87, 108),
                   (160, 77, 78)],
}
PIXEL_PALETTES = {fam: [(*rgb, name) for rgb, name in zip(cols, _NAMES)] for fam, cols in _RGB.items()}


@app.route("/api/pixel-art/palette")
@rate_limited("palette", 30)
def pixel_palette():
    resp = jsonify({fam: [list(c) for c in cols] for fam, cols in PIXEL_PALETTES.items()})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def _dist(r, g, b, p):
    """'Redmean' colour distance: much closer to human perception than plain RGB distance."""
    rm = (r + p[0]) / 2
    dr, dg, db = r - p[0], g - p[1], b - p[2]
    return (2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db


def quantize(pixels, palette, dither=False):
    """pixels: (h, w, 3) uint8 array -> (h, w) array of palette indices."""
    h, w, _ = pixels.shape
    if not dither:
        pal = np.array([p[:3] for p in palette], dtype=np.float64)
        px = pixels.astype(np.float64)
        r, g, b = px[..., 0:1], px[..., 1:2], px[..., 2:3]
        rm = (r + pal[:, 0]) / 2
        d = ((2 + rm / 256) * (r - pal[:, 0]) ** 2 + 4 * (g - pal[:, 1]) ** 2
             + (2 + (255 - rm) / 256) * (b - pal[:, 2]) ** 2)
        return d.argmin(axis=2)

    # Floyd-Steinberg error diffusion.
    plist = [tuple(float(v) for v in p[:3]) for p in palette]
    buf = [[[float(v) for v in px] for px in row] for row in pixels.tolist()]
    out = np.zeros((h, w), dtype=np.int64)
    for y in range(h):
        for x in range(w):
            cur = buf[y][x]
            r = min(255.0, max(0.0, cur[0]))
            g = min(255.0, max(0.0, cur[1]))
            b = min(255.0, max(0.0, cur[2]))
            best, best_d = 0, float("inf")
            for i, p in enumerate(plist):
                d = _dist(r, g, b, p)
                if d < best_d:
                    best, best_d = i, d
            out[y, x] = best
            p = plist[best]
            err = (r - p[0], g - p[1], b - p[2])
            for dx, dy, wgt in ((1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and ny < h:
                    t = buf[ny][nx]
                    t[0] += err[0] * wgt
                    t[1] += err[1] * wgt
                    t[2] += err[2] * wgt
    return out


def to_rgb_on_white(image):
    """Flattens transparency onto white (instead of black) and returns an RGB image."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        return bg.convert("RGB")
    return image.convert("RGB")


_export_slots = threading.BoundedSemaphore(int(os.environ.get("EXPORT_CONCURRENCY", "2")))


@app.route("/api/pixel-art/export", methods=["POST"])
@rate_limited("export", 6)
def export_pixel_art():
    upload = request.files.get("image")
    if not upload or not upload.filename:
        return jsonify(error="Choose an image first."), 400
    raw = upload.stream.read(PIXEL_MAX_UPLOAD + 1)
    if len(raw) > PIXEL_MAX_UPLOAD:
        return jsonify(error="Image is too large. Maximum size is 8 MB."), 413
    if not raw:
        return jsonify(error="The uploaded image is empty."), 400

    try:
        resolution = int(request.form.get("resolution", "64"))
        depth = int(request.form.get("depth", "1"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid resolution or depth."), 400
    family = request.form.get("palette", "concrete").lower()
    dither = request.form.get("dither", "0") == "1"
    if resolution not in PIXEL_RESOLUTIONS or not 1 <= depth <= 4:
        return jsonify(error="Choose a supported resolution and depth."), 400
    if family not in PIXEL_PALETTES:
        return jsonify(error="Invalid block palette."), 400

    if not _export_slots.acquire(blocking=False):
        return jsonify(error="The exporter is busy right now. Please try again in a few seconds."), 503
    try:
        try:
            image = Image.open(BytesIO(raw), formats=["PNG", "JPEG", "WEBP"])
            if image.width * image.height > PIXEL_MAX_PIXELS:
                return jsonify(error="Image dimensions are too large. Please use an image under 20 megapixels."), 400
            image.draft("RGB", (4096, 4096))  # cheaper JPEG decoding for big photos
            image = ImageOps.exif_transpose(image)
            image = to_rgb_on_white(image)
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            # Fit to a square canvas without stretching the source image.
            fitted = ImageOps.fit(image, (resolution, resolution),
                                  method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        except (ValueError, UnidentifiedImageError, Image.DecompressionBombError, OSError):
            return jsonify(error="This image could not be read. Try a PNG, JPG or WEBP file."), 400

        try:
            from litemapy import BlockState, Region

            palette = PIXEL_PALETTES[family]
            indices = quantize(np.asarray(fitted, dtype=np.uint8), palette, dither)
            states = [BlockState(f"minecraft:{c[3]}_{family}") for c in palette]

            region = Region(0, 0, 0, resolution, resolution, depth)
            for py in range(resolution):
                y = resolution - 1 - py
                row = indices[py]
                for x in range(resolution):
                    block = states[int(row[x])]
                    for z in range(depth):
                        region[x, y, z] = block

            schematic = region.as_schematic(
                name="PixelCraft Art", author="PixelCraft Studio",
                description=f"Generated from an image · {resolution}x{resolution} · {family}"
            )
            tmp = tempfile.NamedTemporaryFile(prefix="pixelcraft-", suffix=".litematic", delete=False)
            tmp_path = tmp.name
            tmp.close()
            try:
                schematic.save(tmp_path)
                response = send_file(tmp_path, mimetype="application/octet-stream",
                                     as_attachment=True, download_name="pixelcraft-art.litematic")
                response.call_on_close(lambda: os.path.exists(tmp_path) and os.remove(tmp_path))
                response.headers["Cache-Control"] = "no-store"
                return response
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except ImportError:
            app.logger.exception("Pixel Art dependencies are missing")
            return jsonify(error="Pixel Art export is not installed on the server. Install the requirements and redeploy."), 503
        except Exception:
            app.logger.exception("Pixel Art schematic export failed")
            return jsonify(error="Could not create the schematic. Please try another image."), 500
    finally:
        _export_slots.release()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
