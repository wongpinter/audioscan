# RuangDengar

RuangDengar plays your audiobooks straight from Google Drive. Just connect your Drive, pick a folder, and listen. Private server cache, sleep timer. Your books never leave your Drive.

RuangDengar is a private, mobile-first audiobook player. It streams audio from your configured Drive folder and syncs playback progress and library details across your devices.

## Listen with RuangDengar

- Connect one Google account and choose the audiobook folder for your library.
- Stream tracks and chapters from Drive with byte-range requests.
- Resume listening across devices with saved progress.
- Browse by artist, album, or folder. Search, sort, and filter your books.
- Save favorites, ratings, tags, playlists, and listening history.
- Install RuangDengar from its HTTPS site on your phone.
- Keep recently played books in a private, size-limited server cache. Cache misses stream from Drive.
- Set a sleep timer for 15, 30, 45, or 60 minutes, or stop at the end of the current track.

## Set up the player

RuangDengar needs Python 3.11+, Node.js, a Google OAuth **Web application** client, and a Drive folder with your audiobooks.

1. Enable the Google Drive API and create a Web application OAuth client. Add `https://YOUR_HOST/auth/callback` as its authorized redirect URI.
2. Set these server environment values:

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

Generate the secret with `openssl rand -hex 32`. Keep OAuth credentials, database, and cache private to the server.

3. Build and start RuangDengar:

```sh
npm ci
npm run build
uv sync --extra web
uv run ruangdengar-web
```

Run the service behind an HTTPS reverse proxy. Open its URL on your phone and choose **Add to Home Screen**.

RuangDengar allows the configured Google email and scans only the configured Drive folder. It uses Drive's read-only access. The server cache stores copies of fully warmed or fully requested audio files, up to its configured limit. Google Drive remains the source library; cache files stay on the app server. Cache hits still pass through the app server.

See [Deployment](docs/DEPLOYMENT.md) for service setup. For standalone library metadata scans, see [Google Drive CLI scanning](docs/CLI.md).

## Development

```sh
uv sync --extra dev --extra gdrive --extra web
npm ci
npm test
npm run build
uv run pytest
uv run ruff check .
uv run mypy src
```

The web frontend uses React and Vite. Its production assets live in `src/audioscan/static/`.

## License

MIT
