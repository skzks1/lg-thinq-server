"""
LG ThinQ Flask 서버
실행: python thinq_server.py
공개 접속: https://injedormitory.dothome.co.kr
관리자 접속: https://injedormitory.dothome.co.kr/admin
"""

import asyncio
import uuid
import os
import json
from flask import Flask, jsonify, request, send_from_directory
from aiohttp import ClientSession
from thinqconnect import ThinQApi
import random
import requests
import json


def send_kakao_message(message):
    access_token = "카카오토큰"

    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    data = {
        "template_object": json.dumps({
            "object_type": "text",
            "text": message,
            "link": {
                "web_url": "https://lg-thinq-server.onrender.com/"
            }
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
PUBLIC_SITE_URL = "https://injedormitory.dothome.co.kr"
CONFIG_FILE = "thinq_config.json"

# ── 설정 ──────────────────────────────────────────────────
CONFIG = {
    "pat":       os.environ.get("THINQ_PAT", ""),
    "client_id": os.environ.get("THINQ_CLIENT_ID", ""),
    "country":   "KR",
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        CONFIG["pat"] = os.environ.get("THINQ_PAT", saved.get("pat", CONFIG["pat"]))
        CONFIG["client_id"] = os.environ.get("THINQ_CLIENT_ID", saved.get("client_id", CONFIG["client_id"]))
        CONFIG["country"] = saved.get("country", CONFIG["country"])
    except Exception as e:
        print(f"설정 파일을 불러오지 못했습니다: {e}")

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)

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

@app.route("/admin")
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
    raw = str(info.get("deviceType") or device.get("deviceType") or "").upper()
    alias = str(info.get("alias") or device.get("alias") or "").upper()
    model = str(info.get("modelName") or device.get("modelName") or "").upper()
    text = f"{raw} {alias} {model}"
    if model.startswith("RH") or "DRYER" in text or "건조" in text:
        return "DRYER"
    if model.startswith("F") or "WASHER" in text or "WASH" in text or "세탁" in text:
        return "WASHER"
    return raw or "DEVICE"

def _public_device(device, state):
    info = device.get("deviceInfo", {}) if isinstance(device, dict) else {}
    state = _first_state(state)
    values = " ".join(_deep_values(state))
    run_state = state.get("runState", {}).get("currentState") if isinstance(state.get("runState"), dict) else None
    running_words = ["RUNNING", "WORKING", "WASHING", "RINSING", "SPINNING", "DRYING", "COOLING"]
    power_on_words = ["ON", "POWER_ON", "POWERON", "INITIAL", *running_words]
    is_running = any(word in values for word in running_words) or str(run_state).upper() in running_words
    power_on = is_running or any(word in values for word in power_on_words) or str(run_state).upper() == "INITIAL"
    timer = state.get("timer", {}) if isinstance(state.get("timer"), dict) else {}
    remain_hour = timer.get("remainHour", 0) or 0
    remain_minute = timer.get("remainMinute", 0) or 0
    return {
        "id": device.get("deviceId") or device.get("id"),
        "name": info.get("alias") or device.get("alias") or "기기",
        "type": _device_type(device),
        "model": info.get("modelName") or device.get("modelName") or "",
        "power": "ON" if power_on else "OFF",
        "runState": run_state or "-",
        "running": is_running,
        "remainingText": f"{remain_hour}시간 {remain_minute}분" if remain_hour or remain_minute else "-",
    }

# ── 설정 조회/저장 ─────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "hasPat":   bool(CONFIG["pat"]),
        "clientId": CONFIG["client_id"],
        "country":  CONFIG["country"],
    })

@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.json or {}
    if "pat"      in data: CONFIG["pat"]       = data["pat"]
    if "clientId" in data: CONFIG["client_id"] = data["clientId"]
    if "country"  in data: CONFIG["country"]   = data["country"]
    save_config()
    return jsonify({"ok": True})

# ── 클라이언트 등록 ────────────────────────────────────────
@app.route("/api/client/register", methods=["POST"])
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
                "type": "MQTT",
                "service-code": "SVC202",
                "device-type": "607",
            })

    try:
        run_async(_do())
        CONFIG["client_id"] = new_id
        save_config()
        return jsonify({"ok": True, "clientId": new_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── 기기 목록 ──────────────────────────────────────────────
@app.route("/api/devices", methods=["GET"])
def get_devices():
    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "PAT와 Client ID를 먼저 설정하세요"}), 400

    async def _do():
        async with ClientSession() as session:
            return await get_api(session).async_get_device_list()

    try:
        result = run_async(_do())
        return jsonify({"ok": True, "devices": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── 기기 상태 조회 ─────────────────────────────────────────
@app.route("/api/devices/<device_id>/state", methods=["GET"])
def get_device_state(device_id):
    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "PAT와 Client ID를 먼저 설정하세요"}), 400

    async def _do():
        async with ClientSession() as session:
            return await get_api(session).async_get_device_status(device_id)

    try:
        result = run_async(_do())
        return jsonify({"ok": True, "state": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── 공개 상태 조회 ─────────────────────────────────────────
@app.route("/api/public/status", methods=["GET"])
def public_status():
    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "관리자 설정이 필요합니다"}), 400

    async def _do():
        async with ClientSession() as session:
            api = get_api(session)
            devices = await api.async_get_device_list()
            if isinstance(devices, dict):
                devices = devices.get("devices") or devices.get("items") or devices.get("item") or devices.get("data") or []
            public_devices = []
            for device in devices or []:
                device_id = device.get("deviceId") or device.get("id")
                if not device_id:
                    continue
                try:
                    state = await api.async_get_device_status(device_id)
                except Exception:
                    state = {}
                public_devices.append(_public_device(device, state))
            return public_devices

    try:
        return jsonify({"ok": True, "devices": run_async(_do())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── 기기 제어 ──────────────────────────────────────────────
@app.route("/api/devices/<device_id>/control", methods=["POST"])
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

# ── 실행 ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  LG ThinQ 대시보드 서버 시작")
    print(f"  공개 상태조회: {PUBLIC_SITE_URL}")
    print(f"  관리자 사이트: {PUBLIC_SITE_URL}/admin")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)


import requests
import json

def send_kakao_message():

    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

    headers = {
        "Authorization": f"Bearer {KAKAO_ACCESS_TOKEN}"
    }

    data = {
        "template_object": json.dumps({
            "object_type": "text",
            "text": "🧺 세탁 알림 테스트입니다!",
            "link": {
                "web_url": "https://lg-thinq-server.onrender.com"
            }
        })
    }

    response = requests.post(url, headers=headers, data=data)

    print(response.text)