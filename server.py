"""Minecraft Server Status Checker
Usage:
    pip install -r requirements.txt
    python server.py
"""
import base64
import ipaddress
import os
import re
import socket
from flask import Flask, jsonify, request, send_from_directory
from mcstatus import BedrockServer, JavaServer

app = Flask(__name__, static_folder="static", static_url_path="/static")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(BASE_DIR, "saved_icons")
os.makedirs(ICON_DIR, exist_ok=True)
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"


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


@app.route("/icons/<path:name>")
def saved_icon(name: str):
    return send_from_directory(ICON_DIR, name, as_attachment=request.args.get("dl") == "1")


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

    data["icon_file"] = None
    if data.get("icon") and "," in data["icon"]:
        fname = re.sub(r"[^A-Za-z0-9._-]", "_", f"{data['host']}_{data['port']}") + ".png"
        with open(os.path.join(ICON_DIR, fname), "wb") as f:
            f.write(base64.b64decode(data["icon"].split(",", 1)[1]))
        data["icon_file"] = fname

    return jsonify(data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
