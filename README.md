# Minecraft Server Stats

## Run locally
```bash
pip install -r requirements.txt
python server.py
```
Open http://127.0.0.1:5000

## Unique visitor counter
The top bar displays unique browsers. A first-party random cookie is kept for one year,
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
