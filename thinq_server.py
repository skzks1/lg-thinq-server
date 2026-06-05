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
import hashlib
import secrets
from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for
from functools import wraps
from aiohttp import ClientSession
from thinqconnect import ThinQApi
import random
import requests

# ── Firebase Admin SDK (FCM 푸시 알림) ────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, messaging as fb_messaging
    # FIREBASE_CREDENTIALS_JSON 환경변수에 서비스 계정 JSON 문자열을 넣어두세요
    # Render 대시보드 → Environment → FIREBASE_CREDENTIALS_JSON
    _fb_cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
    if _fb_cred_json and not firebase_admin._apps:
        _cred_dict = json.loads(_fb_cred_json)
        _cred = credentials.Certificate(_cred_dict)
        firebase_admin.initialize_app(_cred)
        FCM_ENABLED = True
    else:
        FCM_ENABLED = False
        print("[FCM] FIREBASE_CREDENTIALS_JSON 환경변수가 없습니다. FCM 비활성화.")
except ImportError:
    FCM_ENABLED = False
    print("[FCM] firebase-admin 패키지가 없습니다. pip install firebase-admin 후 재시작하세요.")

# ── FCM 토큰 저장소 (메모리 + 파일 백업) ──────────────────
FCM_TOKENS_FILE = "fcm_tokens.json"

def _load_fcm_tokens() -> dict:
    """{ token: { "label": str, "role": "user"|"admin" } }"""
    if not os.path.exists(FCM_TOKENS_FILE):
        return {}
    try:
        with open(FCM_TOKENS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_fcm_tokens(tokens: dict):
    with open(FCM_TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)

FCM_TOKENS: dict = _load_fcm_tokens()

def send_push_notification(title: str, body: str, role: str = "all"):
    """
    FCM 푸시 알림 전송.
    role: "all" | "user" | "admin"
    """
    if not FCM_ENABLED:
        print(f"[FCM 비활성화] 알림 전송 건너뜀: {title} / {body}")
        return

    targets = [
        token for token, info in FCM_TOKENS.items()
        if role == "all" or info.get("role") == role
    ]

    if not targets:
        print("[FCM] 등록된 토큰이 없습니다.")
        return

    success, fail = 0, 0
    for token in targets:
        try:
            msg = fb_messaging.Message(
                notification=fb_messaging.Notification(title=title, body=body),
                android=fb_messaging.AndroidConfig(priority="high"),
                apns=fb_messaging.APNSConfig(
                    payload=fb_messaging.APNSPayload(
                        aps=fb_messaging.Aps(sound="default")
                    )
                ),
                token=token,
            )
            fb_messaging.send(msg)
            success += 1
        except Exception as e:
            print(f"[FCM] 토큰 {token[:20]}... 전송 실패: {e}")
            fail += 1

    print(f"[FCM] 전송 완료: 성공 {success}, 실패 {fail}")

# ── 카카오 알림 (기존 유지) ────────────────────────────────
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
PUBLIC_SITE_URL = "https://lg-thinq-server.onrender.com"
CONFIG_FILE = "thinq_config.json"

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
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        CONFIG["pat"] = os.environ.get("THINQ_PAT", saved.get("pat", CONFIG["pat"]))
        CONFIG["client_id"] = os.environ.get("THINQ_CLIENT_ID", saved.get("client_id", CONFIG["client_id"]))
        CONFIG["country"] = saved.get("country", CONFIG["country"])
        CONFIG["devices"] = saved.get("devices", [])
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

def _public_device(device, state):
    info  = device.get("deviceInfo", {}) if isinstance(device, dict) else {}
    state = _first_state(state)
    values = " ".join(_deep_values(state))

    run_state = state.get("runState", {}).get("currentState") if isinstance(state.get("runState"), dict) else None

    running_words  = ["RUNNING", "WORKING", "WASHING", "RINSING", "SPINNING", "DRYING", "COOLING"]
    power_on_words = ["ON", "POWER_ON", "POWERON", "INITIAL", *running_words]

    is_running = any(word in values for word in running_words) or str(run_state).upper() in running_words
    power_on   = is_running or any(word in values for word in power_on_words) or str(run_state).upper() == "INITIAL"

    timer         = state.get("timer", {}) if isinstance(state.get("timer"), dict) else {}
    remain_hour   = timer.get("remainHour",   0) or 0
    remain_minute = timer.get("remainMinute", 0) or 0

    return {
        "id":            device.get("deviceId") or device.get("id"),
        "name":          info.get("alias")      or device.get("alias")     or "기기",
        "type":          _device_type(device),
        "model":         info.get("modelName")  or device.get("modelName") or "",
        "power":         "ON" if power_on else "OFF",
        "runState":      run_state or "-",
        "running":       is_running,
        "remainingText": f"{remain_hour}시간 {remain_minute}분" if remain_hour or remain_minute else "-",
    }

# ── 설정 조회/저장 ─────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
@admin_required
def get_config():
    return jsonify({
        "hasPat":  bool(CONFIG["pat"]),
        "clientId": CONFIG["client_id"],
        "country":  CONFIG["country"],
        "devices":  CONFIG.get("devices", []),
    })

# ── 기기 등록 ──────────────────────────────────────────────
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
        })
    else:
        devices.append({
            "deviceId": device_id,
            "name":  data.get("name",  "기기"),
            "type":  data.get("type",  "DEVICE"),
            "model": data.get("model", ""),
        })

    CONFIG["devices"] = devices
    save_config()
    return jsonify({"ok": True, "deviceId": device_id})

# ── 기기 삭제 ──────────────────────────────────────────────
@app.route("/api/devices/register/<device_id>", methods=["DELETE"])
@admin_required
def unregister_device(device_id):
    devices = CONFIG.get("devices", [])
    before  = len(devices)
    CONFIG["devices"] = [d for d in devices if d.get("deviceId") != device_id]
    save_config()
    return jsonify({"ok": True, "removed": before - len(CONFIG["devices"])})

# ── 등록된 기기 목록 ──────────────────────────────────────
@app.route("/api/devices/registered", methods=["GET"])
@admin_required
def get_registered_devices():
    return jsonify({"ok": True, "devices": CONFIG.get("devices", [])})

@app.route("/api/config", methods=["POST"])
@admin_required
def set_config():
    data = request.json or {}
    if "pat"     in data: CONFIG["pat"]       = data["pat"]
    if "clientId" in data: CONFIG["client_id"] = data["clientId"]
    if "country"  in data: CONFIG["country"]   = data["country"]
    if "devices"  in data: CONFIG["devices"]   = data["devices"]
    save_config()
    return jsonify({"ok": True})

# ── 클라이언트 등록 ────────────────────────────────────────
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

# ── FCM 토큰 등록 (앱에서 호출) ───────────────────────────
@app.route("/api/fcm/register", methods=["POST"])
def fcm_register():
    """
    앱 최초 실행 시 호출.
    Body: { "token": "FCM토큰", "role": "user"|"admin", "label": "기기 이름(선택)" }
    role은 앱에서 판단해서 넘겨주면 됩니다.
    """
    data  = request.json or {}
    token = data.get("token", "").strip()
    role  = data.get("role", "user")   # "user" | "admin"
    label = data.get("label", "")

    if not token:
        return jsonify({"error": "token이 없습니다"}), 400
    if role not in ("user", "admin"):
        return jsonify({"error": "role은 user 또는 admin이어야 합니다"}), 400

    FCM_TOKENS[token] = {"role": role, "label": label}
    _save_fcm_tokens(FCM_TOKENS)
    print(f"[FCM] 토큰 등록: role={role}, label={label}")
    return jsonify({"ok": True})

# ── FCM 토큰 삭제 (앱 로그아웃 시 호출) ──────────────────
@app.route("/api/fcm/unregister", methods=["POST"])
def fcm_unregister():
    data  = request.json or {}
    token = data.get("token", "").strip()
    if token in FCM_TOKENS:
        del FCM_TOKENS[token]
        _save_fcm_tokens(FCM_TOKENS)
    return jsonify({"ok": True})

# ── FCM 토큰 목록 조회 (관리자 전용) ──────────────────────
@app.route("/api/fcm/tokens", methods=["GET"])
@admin_required
def fcm_token_list():
    summary = [
        {"token": t[:20] + "...", "role": info.get("role"), "label": info.get("label")}
        for t, info in FCM_TOKENS.items()
    ]
    return jsonify({"ok": True, "count": len(FCM_TOKENS), "tokens": summary})

# ── 알림 테스트 API (카카오 + FCM 동시) ───────────────────
@app.route("/api/notify/kakao", methods=["POST"])
@admin_required
def notify_kakao():
    data        = request.json or {}
    message     = data.get("message", "테스트 알림")
    device_type = data.get("device_type", "")
    tip         = get_tip(device_type)
    full_message = message + tip

    errors = []

    # 카카오 (기존)
    try:
        send_kakao_message(full_message)
    except Exception as e:
        errors.append(f"카카오: {e}")

    # FCM 푸시 (신규) - role 지정 없으면 전체 전송
    role = data.get("role", "all")  # "all" | "user" | "admin"
    try:
        send_push_notification(title="기숙사 세탁실", body=message, role=role)
    except Exception as e:
        errors.append(f"FCM: {e}")

    if errors:
        return jsonify({"ok": False, "errors": errors}), 500
    return jsonify({"ok": True})

# ── FCM만 단독 알림 (앱 전용) ─────────────────────────────
@app.route("/api/notify/push", methods=["POST"])
@admin_required
def notify_push():
    """
    Body: { "title": str, "body": str, "role": "all"|"user"|"admin" }
    """
    data  = request.json or {}
    title = data.get("title", "기숙사 세탁실")
    body  = data.get("body",  "알림")
    role  = data.get("role",  "all")

    try:
        send_push_notification(title=title, body=body, role=role)
        return jsonify({"ok": True, "fcm_enabled": FCM_ENABLED})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── 기기 목록 ──────────────────────────────────────────────
@app.route("/api/devices", methods=["GET"])
@admin_required
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
@admin_required
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

# ── 공개 상태 조회 ──────────────────────────────────────────
@app.route("/api/public/status", methods=["GET"])
def public_status():
    registered = CONFIG.get("devices", [])
    if not registered:
        return jsonify({"ok": True, "devices": [], "notice": "등록된 기기가 없습니다"})
    if not CONFIG["pat"] or not CONFIG["client_id"]:
        return jsonify({"error": "관리자 설정이 필요합니다"}), 400

    registered_ids = {d.get("deviceId") for d in registered if d.get("deviceId")}

    async def _do():
        async with ClientSession() as session:
            api = get_api(session)
            all_devices = await api.async_get_device_list()
            if isinstance(all_devices, dict):
                all_devices = (all_devices.get("devices") or all_devices.get("items")
                               or all_devices.get("item") or all_devices.get("data") or [])

            filtered = [d for d in (all_devices or [])
                        if (d.get("deviceId") or d.get("id")) in registered_ids]

            found_ids = {d.get("deviceId") or d.get("id") for d in filtered}
            for reg in registered:
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

# ── AI 건의사항 필터 ───────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def ai_filter_suggestion(content: str) -> dict:
    """
    Claude API로 건의사항 내용 검사.
    반환: { "ok": bool, "reason": str }
    """
    if not ANTHROPIC_API_KEY:
        # API 키 없으면 필터 건너뜀
        return {"ok": True, "reason": ""}

    prompt = f"""당신은 기숙사 건의사항 필터링 시스템입니다.
아래 건의사항이 진짜 건의/불편사항인지, 장난/욕설/무의미한 내용인지 판단하세요.

건의사항: "{content}"

다음 기준으로 판단하세요:
- 장난: 의미없는 반복 문자(ㅋㅋㅋ, ㅎㅎ, aaa 등), 테스트 문구, 무의미한 내용
- 욕설/비하: 욕설, 특정인 비하, 혐오 표현
- 스팸: 광고, 외부 링크, 관련 없는 내용
- 정상: 시설 불편, 개선 요청, 규칙 제안 등 실질적 내용

JSON 형식으로만 답하세요 (다른 말 금지):
{{"ok": true/false, "reason": "차단 이유 (정상이면 빈 문자열)"}}"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=10,
        )
        text = res.json()["content"][0]["text"].strip()
        # JSON 파싱
        import re
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            return {"ok": bool(result.get("ok", True)), "reason": result.get("reason", "")}
    except Exception as e:
        print(f"[AI 필터] 오류: {e}")

    # 필터 실패 시 통과
    return {"ok": True, "reason": ""}

# ── 건의사항 ───────────────────────────────────────────────
SUGGESTIONS_FILE = "suggestions.json"

def _load_suggestions() -> list:
    if not os.path.exists(SUGGESTIONS_FILE):
        return []
    try:
        with open(SUGGESTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _save_suggestions(suggestions: list):
    with open(SUGGESTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(suggestions, f, ensure_ascii=False, indent=2)

BLOCKED_FILE = "blocked_suggestions.json"

def _load_blocked() -> list:
    if not os.path.exists(BLOCKED_FILE):
        return []
    try:
        with open(BLOCKED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _save_blocked(data: list):
    with open(BLOCKED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route("/api/suggestions/blocked", methods=["GET"])
@admin_required
def get_blocked():
    return jsonify({"ok": True, "blocked": _load_blocked()})

@app.route("/api/suggestions", methods=["POST"])
def submit_suggestion():
    """앱에서 건의사항 제출 (인증 불필요)"""
    data    = request.json or {}
    content = data.get("content", "").strip()
    room    = data.get("room", "-")

    if not content:
        return jsonify({"error": "내용을 입력해주세요"}), 400

    # 최소 글자 수
    if len(content) < 5:
        return jsonify({"ok": False, "blocked": True, "reason": "너무 짧은 내용입니다. 5자 이상 입력해주세요."}), 400

    # AI 필터링
    filter_result = ai_filter_suggestion(content)
    if not filter_result["ok"]:
        print(f"[건의사항 차단] {room}호: {content[:30]}... / 이유: {filter_result['reason']}")
        # 차단 내역 저장
        blocked = _load_blocked()
        blocked.insert(0, {
            "id":      str(uuid.uuid4())[:8],
            "room":    room,
            "content": content,
            "reason":  filter_result["reason"],
            "created_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        _save_blocked(blocked)
        return jsonify({
            "ok": False,
            "blocked": True,
            "reason": filter_result["reason"] or "부적절한 내용으로 전송이 차단되었습니다.",
        }), 400

    suggestions = _load_suggestions()
    suggestions.insert(0, {
        "id":      str(uuid.uuid4())[:8],
        "room":    room,
        "content": content,
        "read":    False,
        "created_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    _save_suggestions(suggestions)
    return jsonify({"ok": True})

@app.route("/api/suggestions", methods=["GET"])
@admin_required
def get_suggestions():
    """관리자: 건의사항 목록 조회"""
    suggestions = _load_suggestions()
    return jsonify({"ok": True, "suggestions": suggestions})

@app.route("/api/suggestions/<sid>/read", methods=["POST"])
@admin_required
def mark_suggestion_read(sid):
    """관리자: 읽음 처리"""
    suggestions = _load_suggestions()
    for s in suggestions:
        if s.get("id") == sid:
            s["read"] = True
            break
    _save_suggestions(suggestions)
    return jsonify({"ok": True})

@app.route("/api/suggestions/<sid>", methods=["DELETE"])
@admin_required
def delete_suggestion(sid):
    """관리자: 건의사항 삭제"""
    suggestions = _load_suggestions()
    suggestions = [s for s in suggestions if s.get("id") != sid]
    _save_suggestions(suggestions)
    return jsonify({"ok": True})

# ── 실행 ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print(" LG ThinQ 대시보드 서버 시작")
    print(f" 공개 상태조회: {PUBLIC_SITE_URL}")
    print(f" 관리자 사이트: {PUBLIC_SITE_URL}/admin")
    print(f" FCM 알림: {'활성화' if FCM_ENABLED else '비활성화'}")
    print("=" * 50)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)