import base64
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import requests
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

HOMES_FILE = "homes.json"

THINQ_API_KEY = "v6GFvkweNo7DK7yD3ylIZ9w52aKBU0eJ7wLXkSR3"
DEFAULT_SERVER = "https://api-kic.lgthinq.com"

# 서버가 스스로 갱신하는 주기 (3분)
REFRESH_INTERVAL_SEC = 180

# 디바이스 state 호출 timeout
HTTP_TIMEOUT_SEC = 12

CACHE = {
    "data": {"last_refresh": "-", "homes": []},
    "ts": 0.0,               # 마지막 성공 갱신 시각(time.time())
    "updating": False,       # 현재 갱신 중인지
    "last_error": None,      # 최근 갱신 에러(있으면 문자열)
    "last_success_iso": "-", # 마지막 성공 갱신 시각(ISO)
}
CACHE_LOCK = threading.Lock()

STOP_EVENT = threading.Event()


# ---------- Homes persistence ----------
def load_homes() -> List[Dict[str, Any]]:
    if not os.path.exists(HOMES_FILE):
        return []
    with open(HOMES_FILE, "r", encoding="utf-8") as f:
        obj = json.load(f) or {}
        return obj.get("homes", [])


def save_homes(homes: List[Dict[str, Any]]) -> None:
    with open(HOMES_FILE, "w", encoding="utf-8") as f:
        json.dump({"homes": homes}, f, ensure_ascii=False, indent=2)


# ---------- ThinQ headers helpers ----------
def urlsafe_b64_uuid4_22() -> str:
    u = uuid.uuid4()
    return base64.urlsafe_b64encode(u.bytes).decode("ascii").rstrip("=")  # 22 chars


def thinq_headers(pat: str, country: str, client_id: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {pat}",
        "x-message-id": urlsafe_b64_uuid4_22(),
        "x-country": country,
        "x-client-id": client_id,
        "x-api-key": THINQ_API_KEY,
        "Content-Type": "application/json",
    }


def guess_device_type(device_type: str) -> str:
    s = (device_type or "").upper()
    if "AIR" in s and "CONDITION" in s:
        return "AIR_CONDITIONER"
    if "REFRIG" in s:
        return "REFRIGERATOR"
    if "WASH" in s:
        return "WASHER"
    if "DRY" in s:
        return "DRYER"
    return "OTHER"


# ---------- ThinQ API calls ----------
def _raise_for_rate_limit_1314(resp: requests.Response) -> None:
    # ThinQ가 rate limit을 401로도 내릴 수 있어서 body를 확인
    if resp.status_code != 401:
        return
    try:
        j = resp.json() or {}
        err = (j.get("error") or {})
        code = str(err.get("code") or "")
        msg = str(err.get("message") or "")
        if code == "1314" or "Exceeded User API calls" in msg:
            raise RuntimeError("RATE_LIMIT_1314: Exceeded User API calls")
    except ValueError:
        # json 아님
        pass


def fetch_devices(server: str, pat: str, country: str, client_id: str) -> List[Dict[str, Any]]:
    url = f"{server}/devices"
    r = requests.get(url, headers=thinq_headers(pat, country, client_id), timeout=HTTP_TIMEOUT_SEC)
    _raise_for_rate_limit_1314(r)
    r.raise_for_status()
    return (r.json() or {}).get("response", [])


def fetch_state(server: str, pat: str, country: str, client_id: str, device_id: str) -> Dict[str, Any]:
    url = f"{server}/devices/{device_id}/state"
    r = requests.get(url, headers=thinq_headers(pat, country, client_id), timeout=HTTP_TIMEOUT_SEC)
    _raise_for_rate_limit_1314(r)
    r.raise_for_status()
    return (r.json() or {}).get("response", {})


# ---------- Summary ----------
def make_summary(dev_type: str, state: dict) -> str:
    try:
        if dev_type == "AIR_CONDITIONER":
            op = state.get("operation", {}).get("airConOperationMode", "—")
            t = state.get("temperature", {}).get("targetTemperature", "—")
            u = state.get("temperature", {}).get("unit", "")
            mode = state.get("airConJobMode", {}).get("currentJobMode", "—")
            wind = state.get("airFlow", {}).get("windStrength", "—")
            return f"{op} | Target {t}{u} | Mode {mode} | Wind {wind}"

        if dev_type == "REFRIGERATOR":
            temps = state.get("temperature", [])
            parts = []
            for x in temps:
                loc = x.get("locationName")
                val = x.get("targetTemperature")
                unit = x.get("unit", "")
                if loc and val is not None:
                    parts.append(f"{loc}:{val}{unit}")
            return " | ".join(parts) if parts else "—"

        if dev_type == "WASHER":
            s0 = state[0] if isinstance(state, list) and state else state
            cur = s0.get("runState", {}).get("currentState", "—")
            rem_h = s0.get("timer", {}).get("remainHour", 0)
            rem_m = s0.get("timer", {}).get("remainMinute", 0)
            return f"{cur} | Remain {rem_h:02d}:{rem_m:02d}"

        return "—"
    except Exception:
        return "—"


# ---------- Cache builder (slow) ----------
def build_status_slow() -> Dict[str, Any]:
    homes = load_homes()
    out_homes: List[Dict[str, Any]] = []

    for h in homes:
        home_name = h.get("home_name") or "—"
        home_id = h.get("home_id")
        server = h.get("server") or DEFAULT_SERVER
        pat = h.get("pat") or ""
        country = (h.get("country") or "KR").upper()
        client_id = h.get("client_id") or "team-dashboard"

        now_iso = datetime.now(timezone.utc).isoformat()

        # pat 없으면 바로 OFFLINE 처리
        if not pat:
            out_homes.append({
                "home_id": home_id,
                "home_name": home_name,
                "home_status": "OFFLINE",
                "updated_at": now_iso,
                "offline_count": 0,
                "total_devices": 0,
                "devices": [],
                "error": "missing PAT",
            })
            continue

        try:
            dev_list = fetch_devices(server, pat, country, client_id)

            devices_out = []
            offline_count = 0

            for d in dev_list:
                device_id = d.get("deviceId")
                info = d.get("deviceInfo", {}) or {}
                alias = info.get("alias") or info.get("modelName") or device_id or "—"
                dtype_raw = info.get("deviceType", "")
                dtype = guess_device_type(dtype_raw)

                online = True
                state_obj: Dict[str, Any] = {}
                try:
                    state_obj = fetch_state(server, pat, country, client_id, device_id)
                except Exception:
                    online = False
                    offline_count += 1

                devices_out.append({
                    "device_id": device_id,
                    "name": alias,
                    "type": dtype,
                    "online": online,
                    "raw_type": dtype_raw,
                    "state": state_obj,
                    "summary": make_summary(dtype, state_obj),
                })

            if len(devices_out) == 0:
                home_status = "OFFLINE"
            elif offline_count == 0:
                home_status = "ONLINE"
            elif offline_count < len(devices_out):
                home_status = "PARTIAL"
            else:
                home_status = "OFFLINE"

            out_homes.append({
                "home_id": home_id,
                "home_name": home_name,
                "home_status": home_status,
                "updated_at": now_iso,
                "offline_count": offline_count,
                "total_devices": len(devices_out),
                "devices": devices_out,
            })

        except Exception as e:
            # 집 단위 에러는 집에만 찍고, 전체 캐시는 "성공 갱신"으로 볼지 말지 고민인데,
            # 여기서는 "갱신은 성공(응답 생성)"으로 간주해서 last_refresh는 갱신되도록 둠.
            out_homes.append({
                "home_id": home_id,
                "home_name": home_name,
                "home_status": "OFFLINE",
                "updated_at": now_iso,
                "offline_count": 0,
                "total_devices": 0,
                "devices": [],
                "error": str(e),
            })

    return {
        "last_refresh": datetime.now(timezone.utc).isoformat(),
        "homes": out_homes,
    }


# ---------- Cache refresh (server scheduled / forced) ----------
def refresh_cache(force: bool = False) -> None:
    with CACHE_LOCK:
        if CACHE["updating"]:
            return
        CACHE["updating"] = True

    try:
        data = build_status_slow()
        now_iso = datetime.now(timezone.utc).isoformat()
        with CACHE_LOCK:
            CACHE["data"] = data
            CACHE["ts"] = time.time()
            CACHE["last_error"] = None
            CACHE["last_success_iso"] = now_iso
    except Exception as e:
        # 중요: 실패하면 "이전 캐시 유지"
        with CACHE_LOCK:
            CACHE["last_error"] = str(e)
    finally:
        with CACHE_LOCK:
            CACHE["updating"] = False


def start_background_refresher_once():
    """
    Flask debug reloader가 켜져 있으면 프로세스가 2번 떠서
    스레드가 2개 생길 수 있음.
    => WERKZEUG_RUN_MAIN 가 true 인 메인 프로세스에서만 실행.
    """
    if app.debug:
        if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            return

    def loop():
        # 시작 직후 1회
        refresh_cache(force=True)

        # 주기 갱신
        while not STOP_EVENT.is_set():
            STOP_EVENT.wait(REFRESH_INTERVAL_SEC)
            if STOP_EVENT.is_set():
                break
            refresh_cache()

    t = threading.Thread(target=loop, daemon=True)
    t.start()


# ---------- Routes ----------
@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/status")
def api_status():
    # ThinQ 직접 호출 금지. 캐시만 반환.
    with CACHE_LOCK:
        payload = dict(CACHE["data"])
        payload["_meta"] = {
            "cache_ts": CACHE["ts"],
            "updating": CACHE["updating"],
            "last_error": CACHE["last_error"],
            "refresh_interval_sec": REFRESH_INTERVAL_SEC,
            "last_success_iso": CACHE["last_success_iso"],
        }
    return jsonify(payload)


@app.post("/api/admin/homes")
def api_add_home():
    admin_key = os.environ.get("ADMIN_KEY", "")
    if admin_key and request.headers.get("x-admin-key") != admin_key:
        return jsonify({"error": "unauthorized"}), 401

    body = request.json or {}
    home_name = (body.get("home_name") or "").strip()
    pat = (body.get("pat") or "").strip()
    country = (body.get("country") or "KR").strip().upper()
    client_id = (body.get("client_id") or f"team-dashboard-{uuid.uuid4().hex[:8]}").strip()
    server = (body.get("server") or DEFAULT_SERVER).strip()

    if not home_name or not pat:
        return jsonify({"error": "home_name and pat required"}), 400

    homes = load_homes()
    home_id = uuid.uuid4().hex[:10]
    homes.append({
        "home_id": home_id,
        "home_name": home_name,
        "pat": pat,
        "country": country,
        "client_id": client_id,
        "server": server,
    })
    save_homes(homes)

    # 관리자 액션은 즉시 1회 강제 갱신
    threading.Thread(target=lambda: refresh_cache(force=True), daemon=True).start()

    return jsonify({"ok": True, "home_id": home_id})


@app.delete("/api/admin/homes/<home_id>")
def api_delete_home(home_id):
    admin_key = os.environ.get("ADMIN_KEY", "")
    if admin_key and request.headers.get("x-admin-key") != admin_key:
        return jsonify({"error": "unauthorized"}), 401

    homes = load_homes()
    before = len(homes)
    homes = [h for h in homes if h.get("home_id") != home_id]

    if len(homes) == before:
        return jsonify({"error": "home not found"}), 404

    save_homes(homes)

    # 즉시 1회 강제 갱신
    threading.Thread(target=lambda: refresh_cache(force=True), daemon=True).start()

    return jsonify({"ok": True, "deleted": home_id})


if __name__ == "__main__":
    # tmux로 오래 돌릴 거면 debug=False가 제일 안정적.
    # debug=True를 쓰면 reloader 때문에 프로세스 2개가 떠서 헷갈릴 수 있음.
    app.debug = False
    start_background_refresher_once()
    app.run(host="0.0.0.0", port=5000, debug=False)