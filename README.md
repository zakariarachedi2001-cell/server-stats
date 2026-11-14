# Minecraft Server & Player Checker

## Run locally
```bash
pip install -r requirements.txt
python server.py
```
Open http://127.0.0.1:5000

## Tools
The two tiles at the top switch between tools (also reachable via `#server` / `#player`):
- **Creeper tile - Servers:** live Java/Bedrock status refreshed every second, MOTD, icon download.
- **Steve tile - Players:** enter a Java username or UUID to see the UUID (dashed, compact, int-array),
  offline-mode UUID, skin model, cape, front/back skin preview, and download the skin, head or cape.
  Data comes from Mojang's public API (cached for 2 minutes); skins are proxied through
  `/api/texture/<hash>.png` so they can be previewed and downloaded.

## Unique visitor counter
The top bar displays unique browsers and refreshes every second (`/api/visitors/count`, read-only;
polling pauses while the tab is hidden). A first-party random cookie is kept for one year,
so refreshing or reopening the site in the same browser does not add another visitor.
No IP addresses are stored.

### Persistent counter on Render
The app uses SQLite automatically when `DATABASE_URL` is not set. SQLite is suitable
for local use, but Render free web-service storage is ephemeral, so the counter may
reset after a restart or redeploy.

For persistent counts, create a PostgreSQL database and add its **Internal Database URL**
to the web service environment as `DATABASE_URL`, then redeploy. The app automatically
uses PostgreSQL when this variable is set.

## Server icons
Minecraft server icons are returned directly to the browser as data URLs. The app no
longer writes downloaded server icons to `saved_icons/` or stores them on the server.

## PixelCraft Studio (image to Litematica)
The **Pixel Art** tile lets visitors upload a PNG/JPG/WEBP image, preview a square pixel-art conversion, choose a block family (Concrete, Wool or Terracotta), set resolution and wall depth, then download a `.litematic` schematic.

The export endpoint builds a flat wall (or a thicker extruded wall) using vanilla Minecraft blocks. Images are processed in memory and are not saved as permanent uploads. The upload limit is 8 MB and the maximum preview resolution is 128 x 128.

### Install / deploy
The required packages are listed in `requirements.txt`. Locally, run:
```bash
pip install -r requirements.txt
python server.py
```
On Render, commit the updated `requirements.txt` and redeploy so the new `Pillow` and `litemapy` dependencies are installed. The tool requires the Python backend; it cannot generate `.litematic` files on a static-only host.

### Use in Minecraft
1. Download the `.litematic` file.
2. Put it in the Litematica schematics folder (or use Litematica's Schematic Manager to load it).
3. Load the schematic and place the placement in the world.

The image becomes a square wall. The selected depth repeats each pixel's block along the Z axis.
