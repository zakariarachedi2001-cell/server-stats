"""Minecraft Server Status Checker with unique visitor counter.

Local development:
    pip install -r requirements.txt
    python server.py

For persistent visitor counts on Render, set DATABASE_URL to a PostgreSQL
connection string. SQLite is used automatically when DATABASE_URL is absent,
but Render's free web-service filesystem is ephemeral.
"""
import ipaddress
import os
import re
import socket
import sqlite3
import uuid
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory, make_response
from mcstatus import BedrockServer, JavaServer

app = Flask(__name__, static_folder="static", static_url_path="/static")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


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
    conn, is_postgres = db_connect()
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


def get_visitor_count():
    conn, _ = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM visitors")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


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
        conn.commit()
    finally:
        conn.close()


# Create the table when the application starts.
init_visitor_db()


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
    # A random first-party cookie identifies a browser without storing IP addresses.
    visitor_id = request.cookies.get("visitor_id", "")
    try:
        visitor_id = str(uuid.UUID(visitor_id))
        is_new = False
    except (ValueError, TypeError, AttributeError):
        visitor_id = str(uuid.uuid4())
        is_new = True

    try:
        register_visitor(visitor_id)
        count = get_visitor_count()
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
        return response
    except Exception:
        app.logger.exception("Visitor counter database error")
        return jsonify(error="Visitor counter is temporarily unavailable."), 503


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
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
