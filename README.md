# tado_day_planner

A self-hosted FastAPI service that applies time-based heating schedules to
[tado°](https://www.tado.com/) thermostat zones, one "day type" at a time.

You define **day types** (e.g. `workday`, `homeoffice`, `away`, `free`) in a
YAML file — each one a list of per-room, per-time temperatures — and trigger
the one you want for the next day via an authenticated HTTP call. The service
then walks through the timeline and sets manual overlays on the correct
tado° zones at the correct times.

Ideal glue layer between a home automation dashboard, a NFC tag, a voice
assistant, or a bedside button — and your heating.

---

## Features

- **YAML-defined day types** — describe each scenario once, reuse daily.
- **Per-room, per-time temperatures** — including frost protection (`temp: 0`).
- **Smart target date** — scheduling after 05:00 plans for *tomorrow*; before
  05:00 it plans for *today* (so a late-night trigger still works).
- **`now=true` mode** — apply all events immediately instead of waiting
  (useful for testing or "I changed my mind" scenarios).
- **Single active plan** — starting a new plan cleanly aborts the running one.
- **tado° OAuth2 device flow** — one-time browser confirmation on first start,
  automatic refresh afterwards. Tokens persisted to disk.
- **AES-256-GCM authenticated endpoints** — short-lived (10 s) encrypted
  timestamp tokens, no username/password, no bearer token on the wire.
- **Dockerized** — single container, single volume for tokens.

---

## Architecture

```
┌──────────────┐   encrypted     ┌──────────────────┐   OAuth2   ┌──────────┐
│  Your client │ ──────────────▶ │  tado_day_planner │ ─────────▶ │  tado°   │
│ (dashboard,  │   time token    │  (FastAPI +      │            │  API v2  │
│  NFC, cron…) │                 │   scheduler)     │            │          │
└──────────────┘                 └──────────────────┘            └──────────┘
                                         │
                                         ▼
                                   plans.yaml
                                   data/tokens.json
```

- `app.py` — FastAPI app, auth middleware, endpoint routing
- `planner.py` — background thread that walks through the timeline
- `tado.py` — tado° API client (OAuth2 device flow + overlay calls)
- `plans.yaml` — your day types

---

## Requirements

- A tado° account with at least one heating zone
- Your tado° **home ID** (visible in the tado° web app URL or via the API)
- Docker + Docker Compose (recommended), or Python 3.12 directly
- A tado° OAuth2 **client ID** — the official one is currently distributed by
  tado° support; fill it into `src/tado.py`

---

## Configuration

Before first run you need to configure four things.

### 1. `AUTH_SECRET` (in `src/app.py`)

A **32-character ASCII string** used as the AES-256 key for endpoint
authentication. Generate one, for example:

```bash
python -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))"
```

> ⚠️ **Do not commit your real secret.** For production use, move
> `AUTH_SECRET` into an environment variable and read it via `os.environ`.

### 2. `CLIENT_ID` (in `src/tado.py`)

The OAuth2 client ID for the tado° device flow. See the tado° developer /
support resources for the current value.

### 3. `home_id` (in `src/app.py`)

Change the literal in:

```python
tado = TadoClient(1496844)   # <-- replace with your home ID
```

### 4. Zone mapping (in `src/app.py`)

Map your tado° zone IDs to human-readable room names:

```python
ZONES = {
    "Schlafzimmer":  1,
    "Bad":           2,
    "Arbeitszimmer": 3,
    "Küche":         4,
}
```

The keys here must match the room names used in `plans.yaml`.

---

## Defining day types

`src/plans.yaml` holds all day types. Each one lists rooms, and each room
lists `time` → `temp` transitions for that day.

```yaml
day_types:
  homeoffice:
    rooms:
      Arbeitszimmer:
        - { time: "00:00", temp: 18.0 }
        - { time: "05:30", temp: 24.0 }   # pre-heat before workday
        - { time: "09:30", temp: 20.0 }
        - { time: "17:00", temp: 18.0 }
      Schlafzimmer:
        - { time: "00:00", temp: 0 }      # 0 = frost protection / heating OFF
        - { time: "10:00", temp: 16.0 }
```

**Rules:**

- `time` is `HH:MM` in `Europe/Berlin` (hard-coded in `planner.py`).
- `temp: 0` triggers frost protection (heating OFF with manual overlay).
- Every other value is set as a manual temperature overlay in °C.
- Events are sorted globally by time and applied in order.

---

## Running with Docker Compose

```bash
git clone https://github.com/YOUR_USER/tado_day_planner.git
cd tado_day_planner

# edit src/app.py, src/tado.py, src/plans.yaml as described above

docker compose up -d --build
docker compose logs -f
```

On first start, the container logs will print a tado° authorization URL and a
user code — open it in a browser and approve. Tokens are then written to
`./data/tokens.json` and reused across restarts.

The service listens on **http://localhost:7171** by default (see
`docker-compose.yml` to change the host port).

---

## API

All endpoints are `POST` only and require an encrypted time token in the JSON
body:

```json
{ "token": "<base64 AES-256-GCM ciphertext>" }
```

### Token format

The token is built as:

1. Take the current time as `yyyy.MM.dd-HH:mm:ssZ`
   (e.g. `2026.04.20-18:30:00+0200`).
2. AES-256-GCM encrypt the UTF-8 bytes of that string using `AUTH_SECRET`
   as the key, with a random 12-byte IV.
3. Concatenate `IV || ciphertext || tag` → base64 encode.
4. Send as the `token` field of the JSON body.

The server decrypts, parses the timestamp, and rejects anything older than
**10 seconds** or in the future. This means every request needs a fresh
token — replay attacks have a 10-second window at most.

### Endpoints

#### `POST /next-day/{day_type}?now={bool}`

Schedule a day type. By default it plans for the *next* day (or today if it's
before 05:00). With `?now=true`, all events are applied **immediately** in
sequence, without waiting.

```bash
curl -X POST "http://localhost:7171/next-day/homeoffice" \
     -H "Content-Type: application/json" \
     -d '{"token":"<...>"}'
```

Response:

```json
{ "status": "scheduled", "day_type": "homeoffice", "now": false }
```

Starting a new plan while one is running cleanly aborts the previous one.

#### `POST /abort`

Cancel the currently running plan. Already-applied overlays stay in place —
this only stops future events.

#### `POST /status`

Returns whether a plan is currently active:

```json
{ "running": true, "finished": false, "immediate": false }
```

or simply:

```json
{ "running": false }
```

---

## Example client (Python)

```python
import base64, json, os, requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET = os.environ["tado_day_planner_SECRET"]  # 32 chars
BASE   = "http://localhost:7171"

def make_token() -> str:
    now = datetime.now(timezone.utc).astimezone()
    ts  = now.strftime("%Y.%m.%d-%H:%M:%S%z")
    aes = AESGCM(SECRET.encode())
    iv  = os.urandom(12)
    ct  = aes.encrypt(iv, ts.encode(), None)
    return base64.b64encode(iv + ct).decode()

r = requests.post(
    f"{BASE}/next-day/homeoffice",
    json={"token": make_token()},
    timeout=10,
)
print(r.status_code, r.json())
```

---

## Security notes

- The service is designed to sit on a **trusted network** (LAN / VPN / reverse
  proxy). The auth scheme prevents replay and trivial tampering, but there is
  no user management, rate limiting, or TLS built in. Put it behind a reverse
  proxy (Caddy, nginx, Traefik) if you expose it beyond localhost.
- `AUTH_SECRET` and `CLIENT_ID` should not be committed — move them to
  environment variables for real deployments.
- `data/tokens.json` contains your tado° refresh token. Treat it like a
  password.

---

## Troubleshooting

**"No tokens at startup → device flow required"**
First-run message. Open the URL from the logs and confirm in your browser.

**`Token expired` / `Token time is in the future`**
Your client's clock drifts more than 10 seconds from the server's. Sync both
via NTP, or widen `MAX_AGE_SECONDS` in `app.py`.

**`AUTH_SECRET muss GENAU 32 Zeichen haben`**
The secret must be exactly 32 ASCII characters (= 32 bytes for AES-256).

**Zones not heating**
Check `ZONES` in `app.py` — the numeric IDs must match the actual zone IDs in
your tado° home. Verify with `GET /api/v2/homes/{home_id}/zones` against the
tado° API.

---

## Development (without Docker)

```bash
cd src
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# token file expected at /data/tokens.json by default — override via symlink
# or run as root in a container. For local dev, simplest is to adjust
# TOKEN_FILE in tado.py.

uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

---

## License

This project is licensed under the **GNU Affero General Public License v3.0
or later (AGPL-3.0-or-later)**.

In short: you may use, modify, and redistribute this software freely, **but
any modified version you distribute or run as a network service must also be
released under the AGPL** and its source code made available to its users.
See the [LICENSE](./LICENSE) file for the full text, or
<https://www.gnu.org/licenses/agpl-3.0.html>.

---

## Disclaimer

This project controls physical heating equipment via an unofficial use of the
tado° API. It is not affiliated with, endorsed by, or supported by tado° GmbH.
Use at your own risk — misconfigured schedules may lead to uncomfortable
temperatures, frozen pipes, or unnecessary energy consumption. Always test
new day types with `now=true` before relying on them overnight.
