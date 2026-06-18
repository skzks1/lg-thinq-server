"""
LG ThinQ Flask 서버
실행: python thinq_server.py
"""

import asyncio
import uuid
import os
import json
import hashlib
import secrets
import time
from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for
from flask_cors import CORS
from functools import wraps
from aiohttp import ClientSession
from thinqconnect import ThinQApi
import random
import requests

def send_kakao_message(message):
    access_token = "카카오토큰"
    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "template_object": json.dumps({
            "object_type": "text",
            "text": message,
            "link": {"web_url": "https://lg-thinq-server.onrender.com/"}
        })
    }
    requests.post(url, headers=headers, data=data)

KAKAO_ACCESS_TOKEN = "Sl_MVCaCwmlCPjtexNX5QCYaT26NrZgcAAAAAQoNDSEAAAGeRmyyNP8D-j8FVvr5"

WASHER_TIPS = [
    "세탁 후 바로 꺼내면 냄새를 줄일 수 있어요.",
    "수건은 따로 세탁하면 더 위생적이에요.",
    "세제는 너무 많이 넣지 않는 게 좋아요."
]
DRYER_TIPS = [
    "건조 후 바로 꺼내면 구김이 줄어들어요.",
    "먼지 필터를 자주 청소하면 효율이 좋아져요.",
    "과건조는 옷감 손상의 원인이 될 수 있어요."
]

def get_tip(device_type):
    if random.random() > 0.4:
        return ""
    if device_type == "washer":
        return f"\n\nTip 💡\n{random.choice(WASHER_TIPS)}"
    if device_type == "dryer":
        return f"\n\nTip 💡\n{random.choice(DRYER_TIPS)}"
    return ""

last_status = None
start_sent = False
almost_done_sent = False
complete_sent = False

app = Flask(__name__, static_folder=".")
CORS(app, resources={
    r"/api/public/*": {"origins": "*"}
})

PUBLIC_SITE_URL = "https://lg-thinq-server.onrender.com"
CONFIG_FILE = "thinq_config.json"

# ── 상태 캐시 (LG API 호출 횟수 초과 방지기능 보완) ──
# 층별 필터에 맞춤화된 캐시 격리를 위해 층별 전용 캐시 사전으로 세분화합니다.
_status_caches = {
    "":  {"data": None, "ts": 0, "ttl": 30},
    "2": {"data": None, "ts": 0, "ttl": 30},
    "3": {"data": None, "ts": 0, "ttl": 30},
}

# 기기 목록 캐시 (/api/devices)
_devices_cache = {
    "data": None,
    "ts": 0,
    "ttl": 60,
}

# 기기 개별 상태 캐시 (/api/devices/<id>/state)
_state_cache: dict = {}
_STATE_CACHE_TTL = 20

# 임시 API 통신 불가 시에도 기존 정상 가동 정보를 유지하기 위한 캐시 백업소
_last_known_states = {}

# ── 관리자 인증 ────────────────────────────────────────────
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
ADMIN_PASSWORD_HASH = None

def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def _get_admin_pw_hash() -> str:
    global ADMIN_PASSWORD_HASH
    if ADMIN_PASSWORD_HASH is None:
        raw = os.environ.get("ADMIN_PASSWORD", "admin1234")
        ADMIN_PASSWORD_HASH = _hash_pw(raw)
    return ADMIN_PASSWORD_HASH

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "관리자 인증이 필요합니다", "redirect": "/login"}), 401
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated

# ── 설정 ──────────────────────────────────────────────────
CONFIG = {
    "pat": os.environ.get("THINQ_PAT", ""),
    "client_id": os.environ.get("THINQ_CLIENT_ID", ""),
    "country": "KR",
    "devices": [],
}

def load_config():
    saved = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception as e:
            print(f"설정 파일을 불러오지 못했습니다: {e}")

    CONFIG["pat"] = os.environ.get("THINQ_PAT", "").strip() or saved.get("pat", CONFIG["pat"])
    CONFIG["client_id"] = os.environ.get("THINQ_CLIENT_ID", "").strip() or saved.get("client_id", CONFIG["client_id"])
    CONFIG["country"] = saved.get("country", CONFIG["country"])
    CONFIG["devices"] = saved.get("devices", [])

load_config()

# ── 헬퍼 ──────────────────────────────────────────────────
def get_api(session):
    return ThinQApi(
        session=session,
        access_token=CONFIG["pat"],
        country_code=CONFIG["country"],
        client_id=CONFIG["client_id"],
    )

def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ── HTML 서빙 ──────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "thinq_status.html")

@app.route("/oauth")
def oauth():
    return "카카오 인증 성공!"

@app.route("/login")
def login_page():
    if session.get("admin_logged_in"):
        return redirect(request.args.get("next", "/admin"))
    return send_from_directory(".", "thinq_login.html")

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.json or {}
    pw = data.get("password", "")
    if _hash_pw(pw) == _get_admin_pw_hash():
        session["admin_logged_in"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"error": "비밀번호가 올바르지 않습니다"}), 401

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/admin/check", methods=["GET"])
def admin_check():
    return jsonify({"loggedIn": bool(session.get("admin_logged_in"))})

@app.route("/setup")
@admin_required
def setup():
    return send_from_directory(".", "thinq_setup.html")

@app.route("/admin")
@admin_required
def admin():
    return send_from_directory(".", "thinq_dashboard.html")

def _first_state(state):
    if isinstance(state, list):
        merged = {}
        for item in state:
            if isinstance(item, dict):
                merged.update(item)
        return merged
    return state if isinstance(state, dict) else {}

def _deep_values(value):
    if value is None:
        return []
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(_deep_values(item))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_deep_values(item))
        return values
    return [str(value).upper()]

def _device_type(device):
    info = device.get("deviceInfo", {}) if isinstance(device, dict) else {}
    raw   = str(info.get("deviceType") or device.get("deviceType") or "").upper()
    alias = str(info.get("alias")      or device.get("alias")      or "").upper()
    model = str(info.get("modelName")  or device.get("modelName")  or "").upper()
    text  = f"{raw} {alias} {model}"
    if model.startswith("RH") or "DRYER" in text or "건조" in text:
        return "DRYER"
    if model.startswith("F") or "WASHER" in text or "WASH" in text or "세탁" in text:
        return "WASHER"
    return raw or "DEVICE"

def _find_deep_value(obj, target_key):
    """중첩된 사전/리스트 구조 내에서 특정 키의 값을 재귀적으로 검색합니다."""
    if isinstance(obj, dict):
        if target_key in obj:
            return obj[target_key]
        for value in obj.values():
            found = _find_deep_value(value, target_key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_deep_value(item, target_key)
            if found is not None:
                return found
    return None

def _public_device(device, state, reg_info=None):
    info  = device.get("deviceInfo", {}) if isinstance(device, dict) else {}
    state = _first_state(state)
    values = " ".join(_deep_values(state))

    # 중첩 구조 파싱을 위해 재귀 탐색 함수 활용 고도화
    run_state_dict = _find_deep_value(state, "runState")
    run_state = None
    if isinstance(run_state_dict, dict):
        run_state = run_state_dict.get("currentState")
    if not run_state:
        run_state = _find_deep_value(state, "currentState")

    running_words  = ["RUNNING","WORKING","WASHING","RINSING","SPINNING","DRYING","COOLING","PROCESSING","PROCESS"]
    power_on_words = ["ON","POWER_ON","POWERON","INITIAL",*running_words]

    is_running = any(word in values for word in running_words) or str(run_state).upper() in running_words
    power_on   = is_running or any(word in values for word in power_on_words) or str(run_state).upper() == "INITIAL"

    # 중첩 구조 속 타이머 찾기
    timer = _find_deep_value(state, "timer") or {}
    if not isinstance(timer, dict):
        timer = {}

    remain_hour   = int(timer.get("remainHour",   0) or 0)
    remain_minute = int(timer.get("remainMinute", 0) or 0)
    remain_second = int(timer.get("remainSecond", 0) or 0)

    # remainHour가 없고 relativeMinuteToStop만 있는 일부 모델 대비 예외 보완
    if remain_hour == 0 and remain_minute == 0:
        remain_minute = int(timer.get("relativeMinuteToStop", 0) or 0)

    remain_seconds = remain_hour * 3600 + remain_minute * 60 + remain_second

    floor = str((reg_info or {}).get("floor") or info.get("floor") or device.get("floor") or "3")

    return {
        "id":               device.get("deviceId") or device.get("id"),
        "name":             (reg_info or {}).get("name") or info.get("alias") or device.get("alias") or "기기",
        "type":             _device_type(device),
        "model":            info.get("modelName") or device.get("modelName") or "",
        "floor":            floor,
        "power":            "ON" if power_on else "OFF",
        "runState":         run_state or "-",
        "running":          is_running,
        "remainingSeconds": remain_seconds if is_running else 0,
        "remainingText":    f"{remain_hour}시간 {remain_minute}분" if (remain_hour or remain_minute) else "-",
    }

# ── 설정 조회/저장 ─────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
@admin_required
def get_config():
    return jsonify({
        "hasPat":   bool(CONFIG["pat"]),
        "clientId": CONFIG["client_id"],
        "country":  CONFIG["country"],
        "devices":  CONFIG.get("devices", []),
    })

@app.route("/api/admin/debug/config", methods=["GET"])
@admin_required
def debug_config():
    return jsonify({
        "pat_loaded": bool(CONFIG["pat"]),
        "pat_preview": CONFIG["pat"][:10] + "..." if CONFIG["pat"] else "(비어있음)",
        "client_id": CONFIG["client_id"],
        "country": CONFIG["country"],
        "device_count": len(CONFIG.get("devices", [])),
        "env_THINQ_PAT_exists": "THINQ_PAT" in os.environ,
        "env_THINQ_PAT_empty": os.environ.get("THINQ_PAT", "") == "",
    })

@app.route("/api/devices/register", methods=["POST"])
@admin_required
def register_device():
    data      = request.json or {}
    device_id = data.get("deviceId") or data.get("id")
    if not device_id:
        return jsonify({"error": "deviceId가 없습니다"}), 400

    devices  = CONFIG.get("devices", [])
    existing = next((d for d in devices if d.get("deviceId") == device_id), None)
    if existing:
        existing.update({
            "deviceId": device_id,
            "name":  data.get("name",  existing.get("name",  "기기")),
            "type":  data.get("type",  existing.get("type",  "DEVICE")),
            "model": data.get("model", existing.get("model", "")),
            "floor": str(data.get("floor", existing.get("floor", "3"))),
        })
    else:
        devices.append({
            "deviceId": device_id,
            "name":     data.get("name",  "기기"),
            "type":     data.get("type",  "DEVICE"),
            "model":    data.get("model", ""),
            "floor":    str(data.get("floor", "3")),
        })

    CONFIG["devices"] = devices
    save_config()
    for f in _status_caches:
        _status_caches[f]["data"] = None
    return jsonify({"ok": True, "deviceId": device_id})

@app.route("/api/devices/<device_id>/rename", methods=["PATCH"])
@admin_required
def rename_device(device_id):
    data = request.json or {}
    new_name = data.get("name", "").strip()
    if not new_name:
        return jsonify({"error": "이름을 입력해주세요"}), 400

    devices = CONFIG.get("devices", [])
    device = next((d for d in devices if d.get("deviceId") == device_id), None)
    if not device:
        return jsonify({"error": "기기를 찾을 수 없습니다"}), 404

    device["name"] = new_name
    save_config()
    for f in _status_caches:
        _status_caches[f]["data"] = None
    return jsonify({"ok": True, "deviceId": device_id, "name": new_name})

@app.route("/api/devices/<device_id>/floor", methods=["PATCH"])
@admin_required
def change_device_floor(device_id):
    data = request.json or {}
    new_floor = str(data.get("floor", "")).strip()
    if new_floor not in ("2", "3"):
        return jsonify({"error": "floor는 '2' 또는 '3'이어야 합니다"}), 400

    devices = CONFIG.get("devices", [])
    device = next((d for d in devices if d.get("deviceId") == device_id), None)
    if not device:
        return jsonify({"error": "기기를 찾을 수 없습니다"}), 404

    device["floor"] = new_floor
    save_config()
    for f in _status_caches:
        _status_caches[f]["data"] = None
    return jsonify({"ok": True, "deviceId": device_id, "floor": new_floor})

@app.route("/api/devices/<old_id>/replace-id", methods=["PATCH"])
@admin_required
def replace_device_id(old_id):
    data = request.json or {}
    new_id = str(data.get("newDeviceId", "")).strip()
    if not new_id:
        return jsonify({"error": "newDeviceId가 없습니다"}), 400

    devices = CONFIG.get("devices", [])

    if any(d.get("deviceId") == new_id for d in devices):
        return jsonify({"error": "이미 등록된 deviceId입니다"}), 409

    device = next((d for d in devices if d.get("deviceId") == old_id), None)
    if not device:
        return jsonify({"error": "기기를 찾을 수 없습니다"}), 404

    device["deviceId"] = new_id
    save_config()
    for f in _status_caches:
        _status_caches[f]["data"] = None
    return jsonify({"ok": True, "oldId": old_id, "newId": new_id})

@app.route("/api/devices/register/<device_id>", methods=["DELETE"])
@admin_required
def unregister_device(device_id):
    devices = CONFIG.get("devices", [])
    before  = len(devices)
    CONFIG["devices"] = [d for d in devices if d.get("deviceId") != device_id]
    save_config()
    for f in _status_caches:
        _status_caches[f]["data"] = None
    removed = before - len(CONFIG["devices"])
    return jsonify({"ok": True, "removed": removed})

@app.route("/api/devices/registered", methods=["GET"])
@admin_required
def get_registered_devices():
    return jsonify({"ok": True, "devices": CONFIG.get("devices", [])})

@app.route("/api/config", methods=["POST"])
@admin_required
def set_config():
    data = request.json or {}
    if "pat"      in data: CONFIG["pat"]       = data["pat"]
    if "clientId" in data: CONFIG["client_id"] = data["clientId"]
    if "country"  in data: CONFIG["country"]   = data["country"]
    if "devices"  in data: CONFIG["devices"]   = data["devices"]
    save_config()
    for f in _status_caches:
        _status_caches[f]["data"] = None
    return jsonify({"ok": True})

def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"설정을 저장하지 못했습니다: {e}")

@app.route("/api/client/register", methods=["POST"])
@admin_required
def register_client():
    if not CONFIG["pat"]:
        return jsonify({"error": "PAT가 설정되지 않았습니다"}), 400

    new_id = str(uuid.uuid4())

    async def _do():
        async with ClientSession() as session:
            api = ThinQApi(
                session=session,
                access_token=CONFIG["pat"],
                country_code=CONFIG["country"],
                client_id=new_id,
            )
            return await api.async_post_client_register({
                "type":         "MQTT",
                "service-code": "SVC202",
                "device-type":  "607",
            })

    try:
        run_async(_do())
        CONFIG["client_id"] = new_id
        save_config()
        return jsonify({"ok": True, "clientId": new_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/notify/kakao", methods=["POST"])
@admin_required
def notify_kakao():
    data        = request.json or {}
    message     = data.get("message", "테스트 알림")
    device_type = data.get("device_type", "")
    tip         = get_tip(device_type)
    full_message = message + tip
    try:
        send_kakao_message(full_message)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/devices", methods=["GET"])
@admin_required
def get_devices():
    pat_preview = CONFIG["pat"][:10] + "..." if CONFIG["pat"] else "(비어있음)"
    print(f"[DEBUG] 현재 PAT: {pat_preview}")
    print(f"[DEBUG] 현재 client_id: {CONFIG['client_id']}")
    print(f"[DEBUG] 환경변수 THINQ_PAT 존재: {'THINQ_PAT' in os.environ}")
    print(f"[DEBUG] 환경변수 THINQ_PAT 실제 값 존재 여부: {bool(os.environ.get('THINQ_PAT', '').strip())}")

    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "PAT와 Client ID를 먼저 설정하세요"}), 400

    now = time.time()
    if _devices_cache["data"] is not None and (now - _devices_cache["ts"]) < _devices_cache["ttl"]:
        return jsonify({"ok": True, "devices": _devices_cache["data"], "cached": True})

    async def _do():
        async with ClientSession() as session:
            return await get_api(session).async_get_device_list()

    try:
        result = run_async(_do())
        _devices_cache["data"] = result
        _devices_cache["ts"]   = now
        _devices_cache["ttl"]  = 60
        return jsonify({"ok": True, "devices": result})
    except Exception as e:
        err_str = str(e)
        print(f"[ERROR] 기기 탐색 에러 발생: {err_str}")
        
        if "1314" in err_str or "exceeded" in err_str.lower():
            _devices_cache["ttl"] = 120
            
        if _devices_cache["data"] is not None:
            return jsonify({
                "ok": True, 
                "devices": _devices_cache["data"], 
                "cached": True, 
                "stale": True,
                "notice": f"API 호출 한도 초과 또는 에러 — 이전 목록을 표시합니다. (에러: {err_str})"
            })
        return jsonify({"error": f"기기 탐색에 실패했습니다. (상세 원인: {err_str})"}), 500

@app.route("/api/devices/<device_id>/state", methods=["GET"])
@admin_required
def get_device_state(device_id):
    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "PAT와 Client ID를 먼저 설정하세요"}), 400

    now = time.time()
    cached = _state_cache.get(device_id)
    if cached and (now - cached["ts"]) < _STATE_CACHE_TTL:
        return jsonify({"ok": True, "state": cached["data"], "cached": True})

    async def _do():
        async with ClientSession() as session:
            return await get_api(session).async_get_device_status(device_id)

    try:
        result = run_async(_do())
        _state_cache[device_id] = {"data": result, "ts": now}
        _last_known_states[device_id] = result  # 정상 상태일 때 최종 정보 보존소 백업
        return jsonify({"ok": True, "state": result})
    except Exception as e:
        err_str = str(e)
        if "1314" in err_str or "exceeded" in err_str.lower():
            if cached:
                cached["ts"] = now
        # API 오류가 나더라도 보존소에 데이터가 있다면 백업하여 에러 응답 차단
        backup_state = _last_known_states.get(device_id)
        if backup_state:
            return jsonify({"ok": True, "state": backup_state, "cached": True, "stale": True})
        if cached:
            return jsonify({"ok": True, "state": cached["data"], "cached": True, "stale": True})
        return jsonify({"error": err_str}), 500

# ── 공개 상태 조회 (캐시 및 Rate Limit 차단용 층별 최적화 적용) ────────────────────────────
@app.route("/api/public/status", methods=["GET"])
def public_status():
    now = time.time()
    floor_filter = request.args.get("floor", "").strip()
    if floor_filter not in ("2", "3", ""):
        floor_filter = ""

    # 전체, 2층, 3층 등 별도로 쪼갠 층 전용 캐시 불러오기
    cache = _status_caches.get(floor_filter, {"data": None, "ts": 0, "ttl": 30})

    if cache["data"] is not None and (now - cache["ts"]) < cache["ttl"]:
        cached = dict(cache["data"])
        cached["cached"] = True
        cached["cacheAge"] = int(now - cache["ts"])
        return jsonify(cached)

    registered = CONFIG.get("devices", [])
    if not registered:
        return jsonify({"ok": True, "devices": [], "notice": "등록된 기기가 없습니다"})

    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "관리자 설정이 필요합니다"}), 400

    # API 낭비를 최소화하기 위해 API 호출 전 층이 매핑된 기기만 사전 선별합니다.
    target_reg = registered
    if floor_filter:
        target_reg = [d for d in registered if str(d.get("floor", "3")) == floor_filter]

    if not target_reg:
        return jsonify({"ok": True, "devices": [], "notice": f"{floor_filter}층에 등록된 기기가 없습니다"})

    target_reg_ids = {d.get("deviceId") for d in target_reg if d.get("deviceId")}
    target_registered_by_id = {d.get("deviceId"): d for d in target_reg if d.get("deviceId")}

    async def _do():
        async with ClientSession() as session:
            api         = get_api(session)
            all_devices = await api.async_get_device_list()

            if isinstance(all_devices, dict):
                all_devices = (all_devices.get("devices") or all_devices.get("items")
                               or all_devices.get("item") or all_devices.get("data") or [])

            # 선별된 ID에 속한 기기 리스트만 필터링
            filtered = [d for d in (all_devices or [])
                        if (d.get("deviceId") or d.get("id")) in target_reg_ids]

            found_ids = {d.get("deviceId") or d.get("id") for d in filtered}
            for reg in target_reg:
                rid = reg.get("deviceId")
                if rid and rid not in found_ids:
                    filtered.append({
                        "deviceId": rid,
                        "deviceInfo": {
                            "alias":      reg.get("name",  "기기"),
                            "modelName":  reg.get("model", ""),
                            "deviceType": reg.get("type",  "DEVICE"),
                        }
                    })

            public_devices = []
            for device in filtered:
                device_id = device.get("deviceId") or device.get("id")
                if not device_id:
                    continue
                try:
                    state = await api.async_get_device_status(device_id)
                    if state:
                        _last_known_states[device_id] = state  # 정상 상태 보존소 갱신
                except Exception as e:
                    print(f"[WARN] Failed to fetch device status for {device_id}: {e}")
                    # 조회 실패(API 1314 에러 등) 시, 기존 보존소 데이터로 fallback하여 화면이 리셋되지 않게 보호합니다.
                    state = _last_known_states.get(device_id, {})
                
                reg_info = target_registered_by_id.get(device_id)
                public_devices.append(_public_device(device, state, reg_info=reg_info))

            config_order = {d.get("deviceId"): i for i, d in enumerate(target_reg)}
            public_devices.sort(key=lambda d: config_order.get(d.get("id"), 999))

            return public_devices

    try:
        devices = run_async(_do())
        result  = {"ok": True, "devices": devices, "fetchedAt": now, "cached": False}
        
        cache["data"] = result
        cache["ts"]   = now
        cache["ttl"]  = 30
        
        return jsonify(result)
    except Exception as e:
        err_str = str(e)
        if "1314" in err_str or "exceeded" in err_str.lower():
            cache["ttl"] = 60
        
        stale = cache.get("data")
        if stale:
            result = dict(stale)
            result["cached"] = True
            result["stale"]  = True
            result["notice"] = "API 호출 한도 초과 — 잠시 후 자동으로 갱신됩니다"
            return jsonify(result)
        return jsonify({"error": err_str}), 500

# ── 캐시 강제 초기화 (관리자 전용) ────────────────────────
@app.route("/api/admin/cache/clear", methods=["GET", "POST"])
@admin_required
def clear_cache():
    for f in _status_caches:
        _status_caches[f]["data"] = None
        _status_caches[f]["ts"]   = 0
        _status_caches[f]["ttl"]  = 30
    _devices_cache["data"] = None
    _devices_cache["ts"]   = 0
    _devices_cache["ttl"]  = 60
    _state_cache.clear()
    _last_known_states.clear()
    return jsonify({"ok": True, "message": "모든 캐시가 초기화되었습니다"})

@app.route("/api/devices/<device_id>/control", methods=["POST"])
@admin_required
def control_device(device_id):
    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "PAT와 Client ID를 먼저 설정하세요"}), 400

    payload = request.json
    if not payload:
        return jsonify({"error": "payload가 없습니다"}), 400

    async def _do():
        async with ClientSession() as session:
            return await get_api(session).async_post_device_control(device_id, payload)

    try:
        result = run_async(_do())
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print("=" * 50)
    print(" LG ThinQ 대시보드 서버 시작")
    print(f" 공개 상태조회: {PUBLIC_SITE_URL}")
    print(f" 관리자 사이트: {PUBLIC_SITE_URL}/admin")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)