import json
import time
import requests
import logging
from pathlib import Path

log = logging.getLogger("tado")

API_BASE = "https://my.tado.com/api/v2"

# Offizieller Auth Flow laut Tado Support
DEVICE_AUTH_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
CLIENT_ID = "..."

TOKEN_FILE = Path("/data/tokens.json")


class TadoAuthError(RuntimeError):
    pass


class TadoClient:
    def __init__(self, home_id: int):
        self.home_id = home_id

        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0
        self.token_type: str | None = None

        self._load_tokens()

        # ✅ Beim Start validieren: Access Token testen -> Refresh -> Device Flow
        self._ensure_authenticated_startup()

    # ---------------- Token File ----------------

    def _has_tokens(self) -> bool:
        return bool(self.access_token and self.refresh_token)

    def _load_tokens(self):
        if not TOKEN_FILE.exists():
            log.warning("No token file found at %s", TOKEN_FILE)
            return

        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))

        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        self.token_type = data.get("token_type")

        self.expires_at = float(data.get("expires_at", 0))

        if "expires_in" in data and not data.get("expires_at"):
            self.expires_at = time.time() + float(data["expires_in"]) - 30

        if self._has_tokens():
            log.info("Tokens loaded from %s", TOKEN_FILE)
        else:
            log.warning("Token file incomplete: %s", TOKEN_FILE)

    def _save_tokens(self, token_data: dict):
        expires_in = token_data.get("expires_in")
        if expires_in is not None:
            token_data["expires_at"] = time.time() + float(expires_in) - 30

        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        tmp.replace(TOKEN_FILE)

        self.access_token = token_data.get("access_token")
        self.refresh_token = token_data.get("refresh_token")
        self.token_type = token_data.get("token_type")
        self.expires_at = float(token_data.get("expires_at", 0))

        log.info("Tokens saved to %s", TOKEN_FILE)

    # ---------------- API Test ----------------

    def _test_access_token(self) -> bool:
        """
        True wenn Access Token funktioniert.
        """
        if not self.access_token:
            return False

        try:
            r = requests.get(
                f"{API_BASE}/me",
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=10,
            )

            if r.status_code == 200:
                log.info("Access token is valid (GET /me OK)")
                return True

            log.warning("Access token invalid: HTTP %s (%s)", r.status_code, r.text[:200])
            return False

        except Exception as e:
            log.warning("Access token test failed: %s", e)
            return False

    # ---------------- Device Auth Flow ----------------

    def _device_auth_flow(self):
        """
        Wird nur gestartet wenn keine Tokens gehen.
        User muss 1x im Browser bestätigen.
        """
        log.warning("Starting DEVICE AUTH flow (one-time browser confirm required)")

        r = requests.post(
            DEVICE_AUTH_URL,
            params={
                "client_id": CLIENT_ID,
                "scope": "offline_access",
            },
            timeout=10,
        )
        r.raise_for_status()
        auth = r.json()

        url = auth.get("verification_uri_complete")
        if not url:
            url = f"{auth['verification_uri']}?user_code={auth['user_code']}"

        log.warning("====================================================")
        log.warning("TADO AUTH REQUIRED")
        log.warning("Open this URL in a browser and confirm:")
        log.warning("%s", url)
        log.warning("User code: %s", auth["user_code"])
        log.warning("====================================================")

        interval = int(auth.get("interval", 5))
        timeout_s = int(auth.get("expires_in", 300))
        start = time.time()

        while time.time() - start < timeout_s:
            rr = requests.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": auth["device_code"],
                },
                timeout=10,
            )

            if rr.status_code == 200:
                token_data = rr.json()
                self._save_tokens(token_data)
                log.warning("✅ Device authorization successful")
                return

            try:
                err = rr.json()
            except Exception:
                err = {"raw": rr.text}

            if err.get("error") == "authorization_pending":
                time.sleep(interval)
                continue

            raise TadoAuthError(f"Device authorization failed: HTTP {rr.status_code}: {err}")

        raise TadoAuthError("Device authorization timeout - please restart and try again")

    # ---------------- Refresh ----------------

    def _refresh(self):
        if not self.refresh_token:
            raise TadoAuthError("No refresh_token available - device auth needed")

        log.info("Refreshing access token...")

        r = requests.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=10,
        )

        if not r.ok:
            raise TadoAuthError(f"Refresh failed: HTTP {r.status_code}: {r.text[:300]}")

        token_data = r.json()
        self._save_tokens(token_data)

    def _refresh_if_needed(self):
        if not self.access_token:
            raise TadoAuthError("No access_token available")

        if self.expires_at and time.time() < self.expires_at:
            return

        self._refresh()

    def _headers(self):
        self._refresh_if_needed()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    # ✅ Startup ensure logic
    def _ensure_authenticated_startup(self):
        """
        Beim Start:
        1) wenn keine Tokens -> Device Flow
        2) wenn Tokens da -> Access testen
           - wenn invalid -> Refresh -> nochmal testen
           - wenn Refresh fail -> Device Flow
        """
        if not self._has_tokens():
            log.warning("No tokens at startup -> device flow required")
            self._device_auth_flow()
            return

        # Tokens da: Test access
        if self._test_access_token():
            return

        # Access fail: versuch refresh
        try:
            log.warning("Access token invalid -> trying refresh...")
            self._refresh()
        except Exception as e:
            log.warning("Refresh failed at startup: %s", e)
            log.warning("Falling back to device flow...")
            self._device_auth_flow()
            return

        # Nach Refresh nochmal testen
        if not self._test_access_token():
            log.warning("Token still invalid after refresh -> device flow needed")
            self._device_auth_flow()

    # ---------------- Overlay Calls ----------------

    def _put_overlay(self, zone_id: int, payload: dict):
        url = f"{API_BASE}/homes/{self.home_id}/zones/{zone_id}/overlay"

        r = requests.put(url, headers=self._headers(), json=payload, timeout=10)

        if r.status_code == 401:
            log.warning("401 Unauthorized -> refresh & retry (zone=%s)", zone_id)
            self._refresh()
            r = requests.put(url, headers=self._headers(), json=payload, timeout=10)

        if not r.ok:
            raise RuntimeError(f"Tado overlay failed: HTTP {r.status_code}: {r.text[:300]}")

    def set_manual_temperature(self, zone_id: int, temperature: float):
        payload = {
            "setting": {
                "type": "HEATING",
                "power": "ON",
                "temperature": {"celsius": temperature},
            },
            "termination": {"type": "MANUAL"},
        }
        self._put_overlay(zone_id, payload)

    def set_frost_protection(self, zone_id: int):
        payload = {
            "setting": {"type": "HEATING", "power": "OFF"},
            "termination": {"type": "MANUAL"},
        }
        self._put_overlay(zone_id, payload)
