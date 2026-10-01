# Deploy RuangDengar

RuangDengar is a single-user private audiobook player. Run it behind HTTPS and keep app data and OAuth credentials on the server.

## Requirements

- Python 3.11+
- Node.js and npm to build the frontend
- Google Drive API enabled in Google Cloud
- Google OAuth **Web application** client
- An audiobook folder in Google Drive
- HTTPS reverse proxy to the app's Uvicorn port

## Google OAuth

Add this exact redirect URI to the OAuth Web client:

```text
https://YOUR_HOST/auth/callback
```

The application requests read-only Drive access. The allowed user is set with `APP_ALLOWED_EMAIL`.

## Configure and run

Set these variables in the service environment:

```sh
APP_ALLOWED_EMAIL=you@example.com
APP_SECRET_KEY=<at least 32 random characters>
APP_BASE_URL=https://YOUR_HOST
GOOGLE_CLIENT_SECRETS=/run/secrets/google-oauth-web.json
AUDIOBOOKS_FOLDER_ID=<Drive folder ID>
APP_DB_PATH=data/ruangdengar.sqlite3
APP_CACHE_PATH=data/media-cache
APP_CACHE_MAX_BYTES=32212254720
APP_CACHE_WARM_ENABLED=true
```

Create the secret with `openssl rand -hex 32`. Make sure the service user can read the OAuth file and write to the database and cache directories. Keep those paths private.

Build the frontend and start the service:

```sh
npm ci
npm run build
uv sync --extra web
APP_HOST=0.0.0.0 APP_PORT=8111 uv run ruangdengar-web
```

Configure the HTTPS proxy to forward requests to the Uvicorn port. Set trusted proxy headers for the proxy IP only. Use a process manager that restarts the service after failure and host reboot.

The first library scan starts after sign-in. A new scan after a completed scan lists Drive folders and files again, then refreshes file metadata and discovers added audiobooks. If a scan stops or the app restarts during a scan, the next scan resumes from saved folder and file checkpoints. Directory exclusions use paths already indexed by the app and remove matching library data when saved.

## Cache behavior

The cache stores fully downloaded audio tracks, verifies them against Drive metadata, and evicts least-recently-used files at the configured size limit. Background warming uses up to two workers. Set `APP_CACHE_WARM_ENABLED=false` to disable warming. Cache misses keep using authenticated Drive range streaming.

The cache resides on the app host. Caddy or another proxy cannot serve cached files directly unless it shares the cache volume and has a protected internal file-serving route. Current default setup streams cache hits through Python.

## Update

```sh
git pull
npm ci
npm run build
uv sync --extra web
# Restart the RuangDengar service with its configured environment.
```
