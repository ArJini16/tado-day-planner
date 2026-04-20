from fastapi import FastAPI, HTTPException, Request
import yaml
import logging
import base64
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tado import TadoClient
from planner import DayPlanner

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

app = FastAPI()

# --------------------------------------------------------------------
# AUTH CONFIG
# --------------------------------------------------------------------
# Muss GENAU 32 Zeichen lang sein (=> 32 bytes key für AES-256)
AUTH_SECRET = "..."  # <-- 32 Zeichen!

DATE_FORMAT = "%Y.%m.%d-%H:%M:%S%z"  # yyyy.MM.dd-HH:mm:ss-Z
MAX_AGE_SECONDS = 10


def _short(s: str | None, n: int = 16) -> str:
    if not s:
        return "<none>"
    if len(s) <= n:
        return s
    return s[:n] + "..."


def _get_key_from_secret() -> bytes:
    if not isinstance(AUTH_SECRET, str):
        raise RuntimeError("AUTH_SECRET muss ein String sein")

    if len(AUTH_SECRET) != 32:
        raise RuntimeError(
            f"AUTH_SECRET muss GENAU 32 Zeichen haben (ist {len(AUTH_SECRET)})"
        )

    key = AUTH_SECRET.encode("utf-8")
    if len(key) != 32:
        raise RuntimeError(
            "AUTH_SECRET ergibt nicht exakt 32 bytes. Bitte nur ASCII verwenden."
        )

    return key


_AES_KEY = _get_key_from_secret()


async def require_auth(request: Request):
    """
    Muss am Anfang jedes Endpoints aufgerufen werden:
    - nur POST erlaubt
    - JSON Body muss { "token": "..." } enthalten
    - token ist AES-256-GCM verschlüsselte Zeit
    - Zeit darf nicht älter als 10 Sekunden sein
    """
    client_ip = request.client.host if request.client else "unknown"
    path = str(request.url.path)

    log.info("[AUTH] Request from=%s path=%s", client_ip, path)

    if request.method != "POST":
        log.warning("[AUTH] Reject: method=%s (only POST allowed)", request.method)
        raise HTTPException(status_code=405, detail="Only POST allowed")

    try:
        body = await request.json()
        log.info("[AUTH] JSON body parsed OK (keys=%s)", list(body.keys()))
    except Exception as e:
        log.warning("[AUTH] Reject: invalid JSON body (%s)", repr(e))
        raise HTTPException(status_code=400, detail="Body must be valid JSON")

    token_b64 = body.get("token")
    if not token_b64 or not isinstance(token_b64, str):
        log.warning("[AUTH] Reject: missing token field in JSON body")
        raise HTTPException(status_code=401, detail="Missing token in JSON body")

    log.info("[AUTH] token received (len=%d, preview=%s)", len(token_b64), _short(token_b64))

    # base64 decode
    try:
        raw = base64.b64decode(token_b64)
    except Exception as e:
        log.warning("[AUTH] Reject: token base64 decode failed (%s)", repr(e))
        raise HTTPException(status_code=401, detail="Invalid token base64")

    log.info("[AUTH] token base64 decoded bytes=%d", len(raw))

    # raw = iv(12) + ciphertext+tag
    if len(raw) < 12 + 16:
        log.warning("[AUTH] Reject: token too short (need >= 28 bytes)")
        raise HTTPException(status_code=401, detail="Invalid token length")

    iv = raw[:12]
    ct = raw[12:]

    log.info("[AUTH] iv_len=%d ct_len=%d", len(iv), len(ct))

    aesgcm = AESGCM(_AES_KEY)

    # decrypt
    try:
        pt = aesgcm.decrypt(iv, ct, None)
    except Exception as e:
        log.warning("[AUTH] Reject: token decrypt failed (%s)", repr(e))
        raise HTTPException(status_code=401, detail="Token decrypt failed")

    # plaintext = date string
    try:
        date_str = pt.decode("utf-8")
    except Exception as e:
        log.warning("[AUTH] Reject: plaintext not utf-8 (%s)", repr(e))
        raise HTTPException(status_code=401, detail="Token plaintext not utf-8")

    log.info("[AUTH] decrypted date_str=%s", date_str)

    # parse date
    try:
        dt = datetime.strptime(date_str, DATE_FORMAT)
    except Exception as e:
        log.warning(
            "[AUTH] Reject: date format invalid (%s), expected=%s, got=%s",
            repr(e),
            DATE_FORMAT,
            date_str,
        )
        raise HTTPException(
            status_code=401,
            detail=f"Token date format invalid (expected {DATE_FORMAT})",
        )

    now = datetime.now(timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)

    delta = (now - dt_utc).total_seconds()

    log.info(
        "[AUTH] now_utc=%s token_utc=%s delta=%.3fs max_age=%ss",
        now.isoformat(),
        dt_utc.isoformat(),
        delta,
        MAX_AGE_SECONDS,
    )

    if delta < 0:
        log.warning("[AUTH] Reject: token time is in the future (delta=%.3fs)", delta)
        raise HTTPException(status_code=401, detail="Token time is in the future")

    if delta > MAX_AGE_SECONDS:
        log.warning("[AUTH] Reject: token expired (delta=%.3fs)", delta)
        raise HTTPException(status_code=401, detail="Token expired")

    log.info("[AUTH] ✅ OK")


# --------------------------------------------------------------------
# APP
# --------------------------------------------------------------------

ZONES = {
    "Schlafzimmer": 1,
    "Bad": 2,
    "Arbeitszimmer": 3,
    "Küche": 4,
}

tado = TadoClient(1496844)
planner: DayPlanner | None = None

with open("plans.yaml") as f:
    PLANS = yaml.safe_load(f)["day_types"]


def _cleanup_planner():
    global planner
    if planner and not planner.is_alive():
        log.info("Cleaning up finished planner")
        planner = None


@app.post("/next-day/{day_type}")
async def next_day(day_type: str, now: bool = False, request: Request = None):
    await require_auth(request)

    global planner

    if day_type not in PLANS:
        raise HTTPException(404, "Unknown day type")

    _cleanup_planner()

    if planner:
        log.info("Stopping running planner")
        planner.abort()
        planner.join(timeout=1)

    planner = DayPlanner(tado, ZONES)
    planner.immediate = now

    planner.load_plan(PLANS[day_type])
    planner.start()

    log.info("Scheduled day type: %s (now=%s)", day_type, now)
    return {"status": "scheduled", "day_type": day_type, "now": now}


@app.post("/abort")
async def abort(request: Request):
    await require_auth(request)

    global planner

    if planner:
        log.info("Abort requested via API")
        planner.abort()
        planner.join(timeout=1)
        planner = None

    return {"status": "aborted"}


@app.post("/status")
async def status(request: Request):
    await require_auth(request)

    _cleanup_planner()

    if planner:
        return {
            "running": True,
            "finished": planner.finished,
            "immediate": planner.immediate,
        }

    return {"running": False}

