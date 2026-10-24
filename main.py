import os
import sys

# Windows 콘솔(cp949)에서 이모지/한글 출력 크래시 방지
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import logging
import threading
import time
import asyncio
import re
import secrets
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
import requests

import discord
import database as db
from bot import VendingBot
from toss_deposit import TossDepositMonitor

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize database
db.init_db()

# 오타 자동 교정: 저장된 자판기 제목의 '냥코대전생' → '냥코대전쟁'
_saved_title = db.get_setting('vending_title', '')
if _saved_title and '냥코대전생' in _saved_title:
    db.set_setting('vending_title', _saved_title.replace('냥코대전생', '냥코대전쟁'))
    logger.info(f"자판기 제목 오타 자동 교정: {_saved_title} -> {db.get_setting('vending_title', '')}")

# 대시보드 URL 자동 정규화: '/dashboard' 경로로 저장된 값을 루트 URL로 교정 (ping/웹훅 404 방지)
_saved_dash_url = (db.get_setting('dashboard_url', '') or '').strip().rstrip('/')
if _saved_dash_url.endswith('/dashboard'):
    db.set_setting('dashboard_url', _saved_dash_url[:-len('/dashboard')])
    logger.info(f"대시보드 URL 자동 정규화: /dashboard 제거 -> {db.get_setting('dashboard_url', '')}")

# Create Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nyanko-vending-secret-key-change-me')
# 세션 쿠키 보안 강화 (HTTPS 환경에서 CSRF/세션 탈취 방지)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_HTTPONLY=True,
)

# ============ 보안: 비밀번호 해시 / CSRF / 로그인 시도 제한 ============
import hashlib as _hashlib
import hmac as _hmac

_PASSWORD_ITERATIONS = 120000
# 대시보드로 절대 노출되면 안 되는 설정 키 (마스킹 대상)
_SENSITIVE_SETTINGS = {
    'discord_token', 'admin_password', 'admin_password_hash',
    'admin_session_token', 'webhook_key',
}


def hash_password(password):
    """PBKDF2-SHA256 해시 (salt 포함) 생성"""
    salt = secrets.token_hex(16)
    digest = _hashlib.pbkdf2_hmac(
        'sha256', str(password).encode('utf-8'), salt.encode('utf-8'), _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password, stored):
    """해시 검증 (형식이 다르면 False)"""
    try:
        algo, iterations, salt, digest = str(stored).split('$', 3)
        if algo != 'pbkdf2_sha256':
            return False
        calc = _hashlib.pbkdf2_hmac(
            'sha256', str(password).encode('utf-8'), salt.encode('utf-8'), int(iterations))
        return _hmac.compare_digest(calc.hex(), digest)
    except (TypeError, ValueError, AttributeError):
        return False


def set_admin_password(password):
    """관리자 비밀번호를 해시로 저장하고 평문은 제거"""
    db.set_setting('admin_password_hash', hash_password(password))
    db.set_setting('admin_password', '')
    logger.info("[SECURITY] 관리자 비밀번호가 해시로 저장되었습니다.")


def check_admin_password(password):
    """비밀번호 확인 - 저장된 해시 우선, 없으면 예전 평문 설정과 비교 후 자동 해시 전환"""
    stored_hash = db.get_setting('admin_password_hash', '')
    if stored_hash:
        return verify_password(password, stored_hash)
    plain = str(db.get_setting('admin_password', 'zizer731!!') or '')
    if plain and _hmac.compare_digest(str(password), plain):
        set_admin_password(password)
        return True
    return False


def mask_secret(value):
    """민감 설정값 마스킹 (앞 4자 + 뒤 4자만 노출)"""
    value = str(value or '')
    if not value:
        return ''
    if len(value) <= 8:
        return '••••••••'
    return f"{value[:4]}••••••••{value[-4:]}"


def _settings_for_client():
    """대시보드에 내려줄 설정 (민감값은 마스킹)"""
    settings = db.get_all_settings()
    for key in _SENSITIVE_SETTINGS:
        if settings.get(key):
            settings[key] = mask_secret(settings[key])
    return settings


def csrf_token():
    """세션 CSRF 토큰 (없으면 생성)"""
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['csrf_token'] = token
    return token


# 웹훅/핑은 외부(Tasker/Render)에서 호출하므로 CSRF 예외 (자체 키/서명으로 보호)
_CSRF_EXEMPT_ENDPOINTS = {
    'api_deposit_webhook', 'api_test_deposit_webhook', 'ping',
}


@app.before_request
def _csrf_protect():
    """상태 변경 요청(POST/PUT/PATCH/DELETE)에 CSRF 토큰 검증"""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    if request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return None
    if not session.get('admin_logged_in'):
        return None  # 로그인 라우트는 자체적으로 토큰 검증
    expected = session.get('csrf_token')
    payload = request.get_json(silent=True) or {}
    provided = (request.headers.get('X-CSRF-Token')
                or request.form.get('csrf_token')
                or payload.get('csrf_token'))
    if not expected or not provided or not _hmac.compare_digest(str(expected), str(provided)):
        logger.warning(f"[SECURITY] CSRF 검증 실패: {request.path} ({request.remote_addr})")
        return jsonify({'success': False, 'error': 'CSRF 토큰이 유효하지 않습니다. 페이지를 새로고침 후 다시 시도해주세요.'}), 403
    return None


@app.context_processor
def _inject_csrf():
    return {'csrf_token': csrf_token()}


# 로그인 무차별 대입 방지 (IP당 5회 / 5분)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300
_login_attempts = {}
_login_lock = threading.Lock()


def _login_blocked(ip):
    now = time.time()
    with _login_lock:
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _login_attempts[ip] = attempts
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            return True, int(_LOGIN_WINDOW_SECONDS - (now - attempts[0]))
        return False, 0


def _record_login_failure(ip):
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


def _clear_login_failures(ip):
    with _login_lock:
        _login_attempts.pop(ip, None)


def _log_coro_result(future):
    """백그라운드 코루틴 예외를 로그로 남김 (조용한 실패 방지)"""
    try:
        future.result()
    except Exception as e:
        logger.error(f"[BOT] 백그라운드 작업 오류: {e}")


def _run_bot_coro(coro_factory, on_done=None):
    """봇 이벤트 루프에서 코루틴 실행 (Flask 스레드 → 봇 루프, 스레드 안전)

    Returns: concurrent.futures.Future 또는 None(봇 오프라인)
    """
    loop = getattr(bot, 'loop', None)
    if not bot.is_ready() or loop is None or loop.is_closed():
        logger.warning("[BOT] 봇이 오프라인이라 요청을 실행할 수 없습니다.")
        return None
    try:
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
    except Exception as e:
        logger.error(f"[BOT] 코루틴 예약 실패: {e}")
        return None
    future.add_done_callback(on_done or _log_coro_result)
    return future


def _schedule_vending_refresh(force=False):
    """가격/할인/재고 변경 시 자판기 메시지 갱신"""
    return _run_bot_coro(lambda: bot.refresh_vending_machines(force_edit=force))


def _admin_actor():
    """감사 로그용 관리자 식별자"""
    return f"dashboard:{request.remote_addr or 'unknown'}"

# ============ PERMANENT SESSION (로그인 유지) ============
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=365)  # 1년간 로그인 유지
import secrets
def _generate_session_token():
    """세션 토큰 생성"""
    return secrets.token_hex(32)

def _validate_session_token(token):
    """세션 토큰 검증"""
    if not token:
        return False
    saved_token = db.get_setting('admin_session_token', '')
    return saved_token == token

# Global bot instance
bot = VendingBot()
# 토스 입금 알림 전용 모니터 (Tasker 웹훅 전용 — Pushbullet 사용 안 함)
# 콜백(handle_deposit)은 아래 DEPOSIT HANDLER 정의 후 연결됨
toss_monitor = TossDepositMonitor()

# ============ PING KEEPER (5분마다) ============
def _get_ping_url():
    """핑 대상 URL 조회: 설정의 dashboard_url 우선, 없으면 Render 외부 URL(RENDER_EXTERNAL_URL) 자동 사용

    설정값에 /dashboard 경로가 포함돼 있어도 /ping 404가 나지 않도록 루트 경로로 정규화한다.
    """
    url = db.get_setting('dashboard_url', '')
    if not url:
        url = os.environ.get('RENDER_EXTERNAL_URL', '')
    url = (url or '').strip().rstrip('/')
    if url.endswith('/dashboard'):
        url = url[:-len('/dashboard')]
    return url

def ping_keeper():
    """5분마다 서비스에 핑 전송 (Render 무료 플랜 슬립 방지)"""
    while True:
        try:
            url = _get_ping_url()
            if url:
                resp = requests.get(url + '/ping', timeout=10)
                logger.info(f"[PING] {url}/ping -> {resp.status_code} (5분 후 재전송)")
            else:
                logger.warning("[PING] 핑 URL 미설정! 대시보드 설정에서 '대시보드 URL'을 저장하거나 RENDER_EXTERNAL_URL 환경변수가 필요합니다.")
        except Exception as e:
            logger.warning(f"[PING] error: {e}")
        time.sleep(300)  # 5분

# ============ SAFE DISCORD SENDERS (로그 전송 실패 시 반드시 기록) ============
def send_to_discord_channel(setting_key, embed=None, content=None):
    """설정된 채널로 메시지 전송 (봇 이벤트 루프에서 안전 실행 + 모든 실패 원인 로깅)
    
    기존 문제 수정:
    - get_channel 캐시 미스 시 fetch_channel로 재시도
    - run_coroutine_threadsafe 결과 확인 → 전송 실패 사유를 터미널 로그에 기록
    - 채널 ID 미설정/봇 미준비 시에도 경고 로그 남김 (조용히 실패하지 않음)
    """
    cid = db.get_setting(setting_key, '')
    if not cid:
        logger.warning(f"[DISCORD] '{setting_key}' 채널 ID가 미설정이라 전송 생략! (대시보드 설정에서 채널 ID를 저장하세요)")
        return False
    if not bot.is_ready():
        logger.warning(f"[DISCORD] 봇이 아직 준비되지 않아 '{setting_key}' 채널 전송 생략")
        return False

    async def _do_send():
        try:
            channel = bot.get_channel(int(cid))
            if not channel:
                channel = await bot.fetch_channel(int(cid))
        except discord.NotFound:
            logger.error(f"[DISCORD] '{setting_key}' 채널(ID:{cid})을 찾을 수 없습니다! 채널 ID를 확인하세요.")
            return False
        except discord.Forbidden:
            logger.error(f"[DISCORD] '{setting_key}' 채널(ID:{cid}) 접근 권한이 없습니다! 봇 권한을 확인하세요.")
            return False
        except Exception as e:
            logger.error(f"[DISCORD] '{setting_key}' 채널 조회 오류: {e}")
            return False

        if not channel:
            logger.error(f"[DISCORD] '{setting_key}' 채널(ID:{cid})을 찾을 수 없습니다!")
            return False

        try:
            if embed and content:
                await channel.send(content=content, embed=embed)
            elif embed:
                await channel.send(embed=embed)
            elif content:
                await channel.send(content)
            logger.info(f"[DISCORD] '{setting_key}' 채널로 로그 전송 성공")
            return True
        except Exception as e:
            logger.error(f"[DISCORD] '{setting_key}' 채널 전송 오류: {e}")
            return False

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), bot.loop)
        return bool(future.result(timeout=20))
    except Exception as e:
        logger.error(f"[DISCORD] '{setting_key}' 전송 스케줄 오류: {e}")
        return False


def send_dm_safe(user_id, embed=None, content=None):
    """DM 전송 (봇 이벤트 루프에서 안전하게 실행 - 기존 별도 루프 방식의 버그 수정)"""
    if not bot.is_ready():
        logger.warning("[DISCORD] 봇이 아직 준비되지 않아 DM 전송 생략")
        return False

    async def _do_send():
        return await bot.send_dm(user_id, content=content, embed=embed)

    try:
        future = asyncio.run_coroutine_threadsafe(_do_send(), bot.loop)
        ok = future.result(timeout=20)
        if not ok:
            logger.warning(f"[DISCORD] DM 전송 실패 (유저 ID: {user_id}) - DM 차단 또는 잘못된 ID일 수 있음")
        return bool(ok)
    except Exception as e:
        logger.error(f"[DISCORD] DM 전송 오류 (유저 ID: {user_id}): {e}")
        return False

# ============ DEPOSIT HANDLER ============
def handle_deposit(amount, text, depositor=None):
    """Handle detected deposit - auto point award when amount AND depositor name match"""
    logger.info(f"Processing deposit: {amount}원, depositor: {depositor}, text: {text[:100]}")
    _record_webhook_log('deposit', f"입금 감지: {amount:,}원 / 입금자: {depositor or '파싱실패'}")
    
    expire_minutes = _get_charge_expire_minutes()
    pending_charges = db.get_pending_charge_requests()
    from datetime import datetime, timedelta
    # SQLite CURRENT_TIMESTAMP는 UTC로 저장되므로 UTC 기준으로 비교해야 함
    now = datetime.utcnow()
    
    # Find matching pending charge request first
    for charge in pending_charges:
        # Check expiry (설정값, 기본 30분)
        try:
            created_at = datetime.strptime(charge['created_at'], '%Y-%m-%d %H:%M:%S')
            if now - created_at > timedelta(minutes=expire_minutes):
                db.cancel_charge_request(charge['id'])
                logger.info(f"Charge request #{charge['id']} expired ({expire_minutes}분 경과)")
                _record_webhook_log('info', f"충전요청 #{charge['id']} 만료 취소 ({expire_minutes}분 경과, {charge['amount']:,}원)")
                continue
        except:
            pass
        
        # AMOUNT must match
        if charge['amount'] != amount:
            logger.debug(f"Amount mismatch: request={charge['amount']}, deposit={amount}")
            continue
        
        request_depositor = charge['depositor_name'].lower().strip()
        text_lower = text.lower()
        
        # 입금자명 정규화: 숫자, "입금", "원" 등 불필요한 단어 제거
        # 예: "2000원 입금 공예준" → "공예준"
        import re as _re
        request_depositor_clean = _re.sub(r'[\d,]+원?', '', request_depositor)
        request_depositor_clean = _re.sub(r'입금|충전|계좌|이체|송금', '', request_depositor_clean).strip()
        
        # DEPOSITOR NAME must match - flexible matching:
        # 1. Exact extraction match (e.g., depositor="홍길동" matches request)
        # 2. Depositor name appears in notification text (e.g., "입금홍길동" contains "홍길동")
        # 3. Text contains the depositor name (e.g., "입금자: 홍길동", "홍길동님 입금")
        # 4. Cleaned depositor name (숫자/입금 제거) appears in text
        depositor_matched = False
        
        if request_depositor:
            if depositor and depositor.lower().strip() == request_depositor:
                depositor_matched = True
            elif request_depositor in text_lower:
                depositor_matched = True
            elif request_depositor_clean and request_depositor_clean in text_lower:
                depositor_matched = True
            elif depositor and request_depositor_clean and depositor.lower().strip() == request_depositor_clean:
                depositor_matched = True
        
        if not depositor_matched:
            logger.info(f"Depositor name mismatch: request depositor={charge['depositor_name']}, extracted depositor={depositor}, text contains?={request_depositor in text_lower}")
            continue
        
        # BOTH amount AND depositor name matched - automatically award points
        logger.info(f"✅ MATCHED! Charge request #{charge['id']} - amount: {amount:,}원, depositor: {charge['depositor_name']}")
        _record_webhook_log('match', f"자동 충전 성공! 요청 #{charge['id']} ({charge['username']}) {amount:,}원 / 입금자 {charge['depositor_name']}")
        
        # Add points to user (AUTOMATIC)
        new_balance = db.add_balance(
            charge['user_id'],
            charge['username'],
            amount,
            trans_type='charge',
            description=f"입금 충전 ({charge['depositor_name']}, {amount:,}원)"
        )
        
        # Complete charge request
        db.complete_charge_request(charge['id'])
        
        # Notify user via DM with auto charge confirmation
        embed = discord.Embed(
            title="✅ 포인트 충전 완료!",
            description=f"{amount:,}포인트가 충전되었습니다!",
            color=discord.Color.green()
        )
        embed.add_field(name="입금자명", value=charge['depositor_name'], inline=True)
        embed.add_field(name="충전 금액", value=f"{amount:,}포인트", inline=True)
        embed.add_field(name="현재 잔액", value=f"{new_balance:,}포인트", inline=True)
        embed.set_footer(text="냥코 KEY 자판기 🐱")
        send_dm_safe(charge['user_id'], embed=embed)
        
        # (입금 로그는 _record_webhook_log가 디코 deposit_log_channel_id로 자동 미러링)
        
        # Notify admin channel (always for tracking)
        send_to_discord_channel('admin_channel_id',
            content=f"💰 **충전 완료!**\n{charge['username']} 님이 {amount:,}포인트 충전\n입금자명: {charge['depositor_name']}")
        
        return  # Deposit processed, don't continue to unallocated
    
    # If no matching charge request found with both amount+name, send unallocated log
    logger.info(f"No matching charge request for {amount:,}원 (depositor: {depositor})")
    _record_webhook_log('warn', f"미할당 입금: {amount:,}원 (입금자: {depositor or '파싱실패'}) - 금액/입금자명 일치 대기요청 없음")
    
    # (입금 로그는 _record_webhook_log가 디코 deposit_log_channel_id로 자동 미러링)
    
    # Notify admin channel about unallocated deposit
    send_to_discord_channel('admin_channel_id',
        content=f"💰 **입금 감지됨 (미할당)**\n금액: {amount:,}원\n내용: {text[:100]}\n\n대시보드에서 유저에게 포인트를 지급해주세요!")

# 토스 입금 모니터에 입금 처리 콜백 연결
toss_monitor.on_deposit = handle_deposit

# ============ AUTH DECORATOR ============
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('login'))
        # 세션 영구 유지 설정 (로그아웃 전까지 로그인 유지)
        session.permanent = True
        return f(*args, **kwargs)
    return decorated

# ============ ROUTES ============

@app.route('/')
def index():
    if session.get('admin_logged_in'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        blocked, retry_after = _login_blocked(ip)
        if blocked:
            logger.warning(f"[SECURITY] 로그인 시도 초과 차단: {ip}")
            return render_template('login.html',
                                   error=f'로그인 시도가 너무 많습니다. {retry_after}초 후 다시 시도해주세요.'), 429

        # 로그인 폼 CSRF 검증
        form_token = request.form.get('csrf_token', '')
        session_token = session.get('csrf_token', '')
        if not form_token or not session_token or not _hmac.compare_digest(form_token, session_token):
            logger.warning(f"[SECURITY] 로그인 CSRF 검증 실패: {ip}")
            return render_template('login.html',
                                   error='세션이 만료되었습니다. 페이지를 새로고침 후 다시 시도해주세요.'), 400

        password = request.form.get('password', '')
        if check_admin_password(password):
            _clear_login_failures(ip)
            # 세션 고정 공격 방지: 로그인 성공 시 세션 재발급
            session.clear()
            session.permanent = True
            session['admin_logged_in'] = True
            session['csrf_token'] = secrets.token_hex(32)
            session['login_ip'] = ip
            db.add_audit_log('admin_login', '', f'ip:{ip}', '대시보드 로그인 성공')
            return redirect(url_for('dashboard'))

        _record_login_failure(ip)
        remaining = max(0, _LOGIN_MAX_ATTEMPTS - len(_login_attempts.get(ip, [])))
        logger.warning(f"[SECURITY] 로그인 실패: {ip} (남은 시도 {remaining}회)")
        return render_template('login.html',
                               error=f'비밀번호가 올바르지 않습니다. (남은 시도 {remaining}회)')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})

# ============ DASHBOARD ============

@app.route('/dashboard')
@login_required
def dashboard():
    stats = db.get_order_stats()
    total_revenue = db.get_total_revenue()
    products = db.get_products()
    pending_orders = db.get_pending_orders()
    # 웹훅 키 보장 (없으면 생성) - 설정 화면에서 항상 확인 가능하게
    webhook_key = _get_or_create_webhook_key()
    settings = db.get_all_settings()
    # 자동입금 확인 현황 (대시보드 표시용)
    pending_charges = db.get_recent_charge_requests(10)
    charge_expire_minutes = _get_charge_expire_minutes()

    # 웹훅 전체 URL 만들기: 대시보드 URL → Render 외부 URL → 로컬 순
    # (dashboard_url에 /dashboard 경로가 포함돼 있으면 정규화 - _get_ping_url 이 루트로 변환)
    _base = _get_ping_url() or f"http://localhost:{os.environ.get('PORT', '5001')}"
    webhook_full_url = f"{_base}/api/deposit-webhook?key={webhook_key}"

    # Bot status
    bot_status = 'online' if bot.is_ready() else 'offline'

    # 할인 이벤트 / 입고 기록 / 감사 로그 (대시보드 초기 렌더링용)
    sale = db.get_sale()
    sale_summary = db.sale_summary()
    stock_logs = db.get_stock_logs(20)
    audit_logs = db.get_audit_logs(20)

    return render_template('dashboard.html',
        stats=stats,
        total_revenue=total_revenue,
        products=products,
        pending_orders=pending_orders,
        settings=settings,
        bot_status=bot_status,
        bot_name=settings.get('bot_name', '냥코 KEY 자판기'),
        webhook_full_url=webhook_full_url,
        pending_charges=pending_charges,
        charge_expire_minutes=charge_expire_minutes,
        sale=sale,
        sale_summary=sale_summary,
        stock_logs=stock_logs,
        audit_logs=audit_logs
    )

# ============ API ROUTES ============

# --- Notice (공지) ---
@app.route('/api/notice/send', methods=['POST'])
@login_required
def api_send_notice():
    """공지 채널에 임베드 공지 전송 (이미지 파일 지원)"""
    # JSON 또는 multipart/form-data 모두 지원
    if request.is_json:
        data = request.json
        title = data.get('title', '').strip()
        content = data.get('content', '').strip()
        color = data.get('color', '')
        image_file = None
    else:
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        color = request.form.get('color', '').strip()
        image_file = None
        temp_path = None
        if 'image' in request.files:
            f = request.files['image']
            if f and f.filename:
                import tempfile
                ext = os.path.splitext(f.filename)[1] or '.png'
                fd, temp_path = tempfile.mkstemp(suffix=ext)
                f.save(temp_path)
                image_file = temp_path
    
    if not title or not content:
        return jsonify({'error': '제목과 내용을 입력해주세요.'}), 400
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success, msg = loop.run_until_complete(bot.send_notice(title, content, color, image_file))
        loop.close()
        
        # 임시 파일 삭제
        if not request.is_json and temp_path:
            try:
                os.remove(temp_path)
            except:
                pass
        
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        logger.error(f"Send notice error: {e}")
        if not request.is_json and temp_path:
            try:
                os.remove(temp_path)
            except:
                pass
        return jsonify({'success': False, 'message': str(e)}), 500

# --- Products ---
@app.route('/api/products', methods=['GET'])
@login_required
def api_get_products():
    products = db.get_products()
    for p in products:
        p['available_keys'] = db.count_available_keys(p['id'])
    return jsonify(products)

@app.route('/api/products', methods=['POST'])
@login_required
def api_add_product():
    data = request.json
    name = data.get('name', '').strip()
    price = data.get('price', 0)
    description = data.get('description', '')
    category = data.get('category', '').strip()
    if not name or price <= 0:
        return jsonify({'error': '상품명과 가격을 확인해주세요.'}), 400
    pid = db.add_product(name, price, description, category=category)
    db.add_audit_log('product_add', '', _admin_actor(),
                     f"{name} / {int(price):,}원 / 카테고리: {category or '없음'}")
    _schedule_vending_refresh(force=True)
    return jsonify({'success': True, 'id': pid})

@app.route('/api/products/<int:pid>', methods=['PUT'])
@login_required
def api_update_product(pid):
    data = request.json
    db.update_product(
        pid,
        name=data.get('name'),
        price=data.get('price'),
        description=data.get('description'),
        active=data.get('active'),
        category=data.get('category', '')
    )
    db.add_audit_log('product_update', '', _admin_actor(),
                     f"ID {pid} / {data.get('name', '')} / {data.get('price', '')}원")
    _schedule_vending_refresh(force=True)
    return jsonify({'success': True})

@app.route('/api/products/<int:pid>', methods=['DELETE'])
@login_required
def api_delete_product(pid):
    product = db.get_product(pid)
    db.delete_product(pid)
    db.add_audit_log('product_delete', '', _admin_actor(),
                     f"ID {pid} / {(product or {}).get('name', '알 수 없음')}")
    _schedule_vending_refresh(force=True)
    return jsonify({'success': True})

# --- Keys ---
@app.route('/api/products/<int:pid>/keys', methods=['POST'])
@login_required
def api_add_keys(pid):
    data = request.json or {}
    keys_text = data.get('keys', '')
    key_list = [k.strip() for k in keys_text.split('\n') if k.strip()]
    if not key_list:
        return jsonify({'error': '키를 입력해주세요.'}), 400
    db.add_keys(pid, key_list)

    product = db.get_product(pid)
    total_stock = db.count_available_keys(pid)
    if product:
        # 입고 기록 + 감사 로그 (대시보드/명령어 공통 추적)
        db.log_stock(pid, product['name'], len(key_list), total_stock,
                     source='dashboard', actor=_admin_actor())
        db.add_audit_log('stock_add', '', _admin_actor(),
                         f"{product['name']} +{len(key_list)}개 (총 {total_stock}개)")

        # 입고 채널 공지 + 자판기 새로고침 (봇이 실행 중일 때만)
        if bot.is_ready():
            _run_bot_coro(lambda: bot.send_stock_notice(product, len(key_list), total_stock))
            _schedule_vending_refresh(force=True)

    return jsonify({'success': True, 'count': len(key_list), 'total_stock': total_stock})

@app.route('/api/keys', methods=['GET'])
@login_required
def api_get_keys():
    product_id = request.args.get('product_id', type=int)
    used = request.args.get('used', type=int)
    keys = db.get_keys(product_id=product_id, used=used)
    return jsonify(keys)

@app.route('/api/keys/<int:kid>', methods=['DELETE'])
@login_required
def api_delete_key(kid):
    db.delete_key(kid)
    return jsonify({'success': True})

# --- Orders ---
@app.route('/api/orders', methods=['GET'])
@login_required
def api_get_orders():
    status = request.args.get('status')
    include_keys = request.args.get('include_keys', 'false').lower() == 'true'
    if include_keys:
        orders = db.get_orders_with_keys(status=status, limit=200)
    else:
        orders = db.get_orders(status=status, limit=200)
    return jsonify(orders)

@app.route('/api/orders/<int:oid>/key', methods=['GET'])
@login_required
def api_get_order_key(oid):
    order = db.get_order_with_key(oid)
    if not order:
        return jsonify({'error': '주문을 찾을 수 없습니다.'}), 404
    return jsonify(order)

@app.route('/api/orders/<int:oid>/confirm', methods=['POST'])
@login_required
def api_confirm_order(oid):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success, msg = loop.run_until_complete(bot.confirm_order(oid))
        loop.close()
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        logger.error(f"Confirm order error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/orders/<int:oid>/cancel', methods=['POST'])
@login_required
def api_cancel_order(oid):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success, msg = loop.run_until_complete(bot.cancel_order(oid))
        loop.close()
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        logger.error(f"Cancel order error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

# --- Settings ---
@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    # 민감값(봇 토큰/비밀번호)은 마스킹해서 반환
    return jsonify(_settings_for_client())

@app.route('/api/settings', methods=['POST'])
@login_required
def api_update_settings():
    data = request.json or {}
    updated = []
    password_changed = False
    for key, value in data.items():
        if key == 'admin_password':
            value = str(value or '').strip()
            # 빈 값/마스킹된 값은 "변경 안 함"으로 처리
            if not value or '•' in value:
                continue
            set_admin_password(value)
            password_changed = True
            updated.append(key)
            continue
        if key in _SENSITIVE_SETTINGS:
            # 빈 값/마스킹된 값은 "변경 안 함" (실수로 토큰이 지워지는 것 방지)
            if not str(value or '').strip() or '•' in str(value):
                continue
        db.set_setting(key, value)
        updated.append(key)

    if updated:
        detail = ', '.join(updated)[:500]
        if password_changed:
            detail += ' (비밀번호 변경)'
        db.add_audit_log('settings_update', '', _admin_actor(), detail)
        logger.info(f"[DASHBOARD] 설정 변경: {detail}")
        # 할인/문구/카테고리 설정이 자판기에 즉시 반영되도록 갱신
        _schedule_vending_refresh(force=True)
    return jsonify({'success': True, 'updated': updated})

# --- Admins ---
@app.route('/api/admins', methods=['GET'])
@login_required
def api_get_admins():
    return jsonify(db.get_admins())

@app.route('/api/admins', methods=['POST'])
@login_required
def api_add_admin():
    data = request.json
    user_id = data.get('user_id', '').strip()
    username = data.get('username', '').strip()
    if not user_id:
        return jsonify({'error': '유저 ID를 입력해주세요.'}), 400
    db.add_admin(user_id, username or user_id)
    return jsonify({'success': True})

@app.route('/api/admins/<user_id>', methods=['DELETE'])
@login_required
def api_remove_admin(user_id):
    db.remove_admin(user_id)
    return jsonify({'success': True})

# --- Users (Points) ---
@app.route('/api/users', methods=['GET'])
@login_required
def api_get_users():
    users = db.get_users()
    for u in users:
        u['transactions'] = db.get_user_transactions(u['user_id'], limit=5)
    return jsonify(users)

@app.route('/api/users/<user_id>/points', methods=['POST'])
@login_required
def api_give_points(user_id):
    data = request.json or {}
    try:
        amount = int(data.get('amount', 0))
    except (TypeError, ValueError):
        return jsonify({'error': '금액을 확인해주세요.'}), 400
    username = data.get('username', '')
    if amount <= 0:
        return jsonify({'error': '금액을 확인해주세요.'}), 400
    new_balance = db.add_balance(user_id, username, amount, 'admin_add', '대시보드에서 포인트 지급')
    db.add_audit_log('grant_points', user_id, _admin_actor(),
                     f"+{int(amount):,}P → {username or user_id} (잔액 {new_balance:,}P)")
    return jsonify({'success': True, 'balance': new_balance})

@app.route('/api/users/<user_id>/points/deduct', methods=['POST'])
@login_required
def api_deduct_points(user_id):
    data = request.json or {}
    try:
        amount = int(data.get('amount', 0))
    except (TypeError, ValueError):
        return jsonify({'error': '금액을 확인해주세요.'}), 400
    if amount <= 0:
        return jsonify({'error': '금액을 확인해주세요.'}), 400
    success = db.deduct_balance(user_id, amount, 'admin_remove', '대시보드에서 포인트 차감')
    if not success:
        return jsonify({'error': '잔액 부족'}), 400
    new_balance = db.get_balance(user_id)
    db.add_audit_log('deduct_points', user_id, _admin_actor(),
                     f"-{int(amount):,}P → {user_id} (잔액 {new_balance:,}P)")
    return jsonify({'success': True, 'balance': new_balance})

# --- Coupons (쿠폰/할인 이벤트) ---
@app.route('/api/coupons', methods=['GET'])
@login_required
def api_get_coupons():
    return jsonify(db.get_coupons())

@app.route('/api/coupons', methods=['POST'])
@login_required
def api_create_coupon():
    data = request.json or {}
    code = data.get('code', '').strip()
    discount_amount = data.get('discount_amount', 0)
    max_uses = data.get('max_uses', 0)
    discount_type = str(data.get('discount_type', 'amount') or 'amount').lower()
    if discount_type not in ('amount', 'percent'):
        discount_type = 'amount'
    if not code:
        return jsonify({'error': '쿠폰 코드를 입력해주세요.'}), 400
    try:
        discount_amount = int(discount_amount)
        max_uses = int(max_uses)
    except (TypeError, ValueError):
        return jsonify({'error': '할인 금액과 사용 횟수는 숫자로 입력해주세요.'}), 400
    if discount_amount <= 0:
        return jsonify({'error': '할인 금액은 0보다 커야 합니다.'}), 400
    if discount_type == 'percent' and discount_amount > 100:
        return jsonify({'error': '할인율은 100% 이하여야 합니다.'}), 400
    if max_uses < 0:
        return jsonify({'error': '최대 사용 횟수는 0 이상이어야 합니다.'}), 400
    cid = db.create_coupon(code, discount_amount, max_uses, discount_type=discount_type)
    if cid == -1:
        return jsonify({'error': '이미 존재하는 쿠폰 코드입니다.'}), 400
    unit = '%' if discount_type == 'percent' else '원'
    db.add_audit_log('coupon_create', '', _admin_actor(),
                     f"{code.upper()} ({discount_amount}{unit} 할인, 최대 {max_uses or '무제한'}회)")
    return jsonify({'success': True, 'id': cid})

@app.route('/api/coupons/<int:cid>', methods=['DELETE'])
@login_required
def api_delete_coupon(cid):
    coupon = db.get_coupon(cid)
    db.delete_coupon(cid)
    db.add_audit_log('coupon_delete', '', _admin_actor(),
                     (coupon or {}).get('code', f'ID {cid}'))
    return jsonify({'success': True})

@app.route('/api/coupons/<int:cid>/toggle', methods=['POST'])
@login_required
def api_toggle_coupon(cid):
    db.toggle_coupon(cid)
    coupon = db.get_coupon(cid)
    active = coupon['active'] if coupon else 0
    if coupon:
        db.add_audit_log('coupon_toggle', '', _admin_actor(),
                         f"{coupon['code']} → {'활성화' if active else '비활성화'}")
    return jsonify({'success': True, 'active': active})

# --- 전체 할인 (Sale / 할인 이벤트) ---
@app.route('/api/sale', methods=['GET'])
@login_required
def api_get_sale():
    sale = db.get_sale()
    sale['summary'] = db.sale_summary()
    return jsonify(sale)

@app.route('/api/sale', methods=['POST'])
@login_required
def api_update_sale():
    """전체 할인 시작/종료 (모든 상품에 적용, 쿠폰과 중복 적용 가능)"""
    data = request.json or {}
    if data.get('stop') or data.get('active') is False:
        sale = db.stop_sale()
        db.add_audit_log('sale_stop', '', _admin_actor(), '전체 할인 종료')
        logger.info("[DASHBOARD] 전체 할인 종료")
        _schedule_vending_refresh(force=True)
        return jsonify({'success': True, 'sale': sale, 'summary': db.sale_summary()})

    sale_type = str(data.get('type', 'percent') or 'percent').lower()
    if sale_type not in ('percent', 'amount'):
        return jsonify({'error': '할인 유형이 올바르지 않습니다.'}), 400
    try:
        value = int(data.get('value', 0))
    except (TypeError, ValueError):
        return jsonify({'error': '할인 값은 숫자로 입력해주세요.'}), 400
    if sale_type == 'percent':
        if not 1 <= value <= 100:
            return jsonify({'error': '할인율은 1~100% 사이여야 합니다.'}), 400
    elif value <= 0:
        return jsonify({'error': '할인 금액은 0보다 커야 합니다.'}), 400

    label = str(data.get('label', '') or '').strip()[:60]
    ends_at = str(data.get('ends_at', '') or '').strip()
    if ends_at:
        valid = False
        for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M'):
            try:
                datetime.strptime(ends_at, fmt)
                valid = True
                break
            except ValueError:
                continue
        if not valid:
            return jsonify({'error': '종료 시각 형식이 올바르지 않습니다. (예: 2026-01-31 23:59)'}), 400

    sale = db.set_sale(True, sale_type, value, label, ends_at)
    unit = '%' if sale_type == 'percent' else '원'
    db.add_audit_log('sale_start', '', _admin_actor(),
                     f"{value}{unit} 할인 시작 {label} {ends_at}".strip())
    logger.info(f"[DASHBOARD] 전체 할인 시작: {value}{unit} {label} {ends_at}")
    _schedule_vending_refresh(force=True)
    return jsonify({'success': True, 'sale': sale, 'summary': db.sale_summary()})

# --- 입고 기록 (Stock logs) ---
@app.route('/api/stock-logs', methods=['GET'])
@login_required
def api_get_stock_logs():
    limit = request.args.get('limit', 50, type=int) or 50
    return jsonify(db.get_stock_logs(min(max(limit, 1), 500)))

# --- 감사 로그 (Audit logs) ---
@app.route('/api/audit-logs', methods=['GET'])
@login_required
def api_get_audit_logs():
    limit = request.args.get('limit', 100, type=int) or 100
    return jsonify(db.get_audit_logs(min(max(limit, 1), 500)))

# --- DM 전체 공지 (유저 전원에게 DM) ---
@app.route('/api/notice/dm', methods=['POST'])
@login_required
def api_send_dm_notice():
    data = request.json or {}
    title = str(data.get('title', '') or '').strip()
    content = str(data.get('content', '') or '').strip()
    color = str(data.get('color', '') or '#3498db').strip()
    if not content:
        return jsonify({'success': False, 'message': '공지 내용을 입력해주세요.'}), 400
    if not bot.is_ready():
        return jsonify({'success': False, 'message': '봇이 오프라인입니다. 잠시 후 다시 시도해주세요.'}), 503
    targets = db.get_notice_targets()
    if not targets:
        return jsonify({'success': False, 'message': 'DM 공지를 받을 유저가 없습니다.'}), 400

    actor = _admin_actor()
    total_targets = len(targets)

    def _worker():
        """DM 발송은 오래 걸리므로 별도 스레드에서 실행 (요청 타임아웃 방지)"""
        try:
            future = _run_bot_coro(
                lambda: bot.send_dm_broadcast(title or '📢 공지', content, color))
            if future is None:
                logger.error("[NOTICE] 봇 오프라인으로 DM 공지를 시작하지 못했습니다.")
                return
            sent, failed, total = future.result(timeout=3600)
            logger.info(f"[NOTICE] DM 공지 완료: 성공 {sent} / 실패 {failed} / 대상 {total}")
            db.add_audit_log('notice_dm', '', actor,
                             f"성공 {sent} / 실패 {failed} / 대상 {total} - {title or '(제목 없음)'}")
        except Exception as e:
            logger.error(f"[NOTICE] DM 공지 실패: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    logger.info(f"[DASHBOARD] DM 공지 시작: {total_targets}명 / {title or '(제목 없음)'}")
    return jsonify({
        'success': True,
        'message': f'{total_targets}명에게 DM 공지 발송을 시작했습니다. (완료까지 시간이 걸릴 수 있습니다)',
        'total': total_targets,
    })

# ============ 입금 웹훅 (Pushbullet 없이 폰 앱이 은행 알림을 직접 전송) ============

# ---- 웹훅 진단 로그 (대시보드 표시용 최근 50건 링버퍼) ----
import collections
_webhook_log_lock = threading.Lock()
_webhook_log = collections.deque(maxlen=50)

# 디코 입금 로그 채널로 미러링할 레벨 (매 알림마다 오는 'recv'와 잡담성 'info'는 제외)
_DISCORD_MIRROR_LEVELS = {'deposit', 'match', 'warn', 'error'}
_DISCORD_MIRROR_META = {
    'deposit': ('📥 입금 감지', discord.Color.blue()),
    'match': ('✅ 자동 충전 성공', discord.Color.green()),
    'warn': ('⚠️ 입금 처리 주의', discord.Color.orange()),
    'error': ('❌ 입금 처리 오류', discord.Color.red()),
}

def _record_webhook_log(level, message):
    """웹훅 수신/파싱/매칭 결과를 링버퍼에 기록 (대시보드 진단용)

    중요 레벨(deposit/match/warn/error)은 설정된 디코 입금 로그 채널
    ('deposit_log_channel_id')로도 자동 미러링된다.
    """
    try:
        with _webhook_log_lock:
            _webhook_log.appendleft({
                'time': datetime.now().strftime('%m-%d %H:%M:%S'),
                'level': str(level),
                'message': str(message)[:300],
            })
    except Exception:
        pass

    # 디코 입금 로그 채널 미러링 (채널 미설정/봇 미준비 시에는 조용히 스킵)
    try:
        if level in _DISCORD_MIRROR_LEVELS and bot.is_ready():
            title, color = _DISCORD_MIRROR_META.get(level, ('📥 입금 로그', discord.Color.blue()))
            embed = discord.Embed(
                title=title,
                description=str(message)[:1000],
                color=color,
                timestamp=datetime.now()
            )
            embed.set_footer(text="냥코 KEY 자판기 🐱 | 입금 로그")
            send_to_discord_channel('deposit_log_channel_id', embed=embed)
    except Exception:
        pass

# 충전 요청 유효 시간 기본값 (분) - 설정키: charge_expire_minutes
DEFAULT_CHARGE_EXPIRE_MINUTES = 30

# Tasker 변수 패턴 (이 형식이 그대로 오면 변수 치환이 안 된 것)
_TASKER_VAR_RE = re.compile(r'%[A-Za-z0-9_]+')

def _detect_unresolved_tasker_vars(title, body):
    """'%ntitle', '%ntext', '%evtprm1'처럼 Tasker 변수가 치환되지 않고 그대로 도착했는지 감지"""
    found = []
    for part in (title, body):
        if not part:
            continue
        for m in _TASKER_VAR_RE.finditer(part):
            v = m.group(0)
            if v not in found:
                found.append(v)
    return found

def _get_charge_expire_minutes():
    """충전 요청 유효 시간(분) 조회. 대시보드 설정에서 변경 가능 (1~1440분)"""
    try:
        val = int(str(db.get_setting('charge_expire_minutes', '')).strip() or DEFAULT_CHARGE_EXPIRE_MINUTES)
    except (TypeError, ValueError):
        val = DEFAULT_CHARGE_EXPIRE_MINUTES
    return max(1, min(val, 1440))

DEPOSIT_WEBHOOK_SETTING_KEY = 'deposit_webhook_key'

def _get_or_create_webhook_key():
    """입금 웹훅 인증 키 조회.

    우선순위: 환경변수(DEPOSIT_WEBHOOK_KEY) > DB 저장값 > 자동 생성.
    환경변수를 지정하면 DB 값과 무관하게 항상 그 키를 사용한다.
    """
    env_key = os.environ.get('DEPOSIT_WEBHOOK_KEY', '').strip()
    if env_key:
        # 환경변수가 있으면 그 값과 DB를 동기화 (Render 재배포 후에도 안전)
        if db.get_setting(DEPOSIT_WEBHOOK_SETTING_KEY, '') != env_key:
            db.set_setting(DEPOSIT_WEBHOOK_SETTING_KEY, env_key)
        return env_key

    saved = db.get_setting(DEPOSIT_WEBHOOK_SETTING_KEY, '')
    if saved:
        return saved

    new_key = secrets.token_hex(16)
    db.set_setting(DEPOSIT_WEBHOOK_SETTING_KEY, new_key)
    logger.info(f"[WEBHOOK] 입금 웹훅 인증 키 자동 생성됨: {new_key}")
    return new_key

@app.route('/api/deposit-webhook', methods=['GET', 'POST'])
def api_deposit_webhook():
    """폰의 Tasker/MacroDroid가 토스 은행 알림을 직접 POST하는 엔드포인트.

    Body(JSON): {"title": "...", "body": "...", "package": "..."}
    인증: ?key=... 또는 헤더 X-Webhook-Key (대시보드 설정의 웹훅 URL과 일치해야 함)
    처리는 toss_deposit.py (토스 전용 파서) → handle_deposit (자동 충전 매칭) 순서.
    """
    if request.method == 'GET':
        # 폰 앱에서 연결 테스트용
        key = request.args.get('key') or request.headers.get('X-Webhook-Key', '')
        if key != _get_or_create_webhook_key():
            return jsonify({'success': False, 'error': 'invalid key'}), 403
        return jsonify({'success': True, 'message': '입금 웹훅 연결 정상! 이 URL과 키를 MacroDroid에 설정하세요.'})

    key = request.args.get('key') or request.headers.get('X-Webhook-Key', '')
    if key != _get_or_create_webhook_key():
        logger.warning(f"[WEBHOOK] 인증 실패한 입금 웹훅 호출 차단 (IP: {request.remote_addr})")
        _record_webhook_log('error', "❌ 인증 실패(키 불일치)! Tasker/MacroDroid에 저장된 URL이 오래되었습니다. 대시보드 설정의 웹훅 URL을 다시 복사해 넣으세요.")
        return jsonify({'success': False, 'error': 'invalid key'}), 403

    # JSON(form/query도 허용) 파싱 (Content-Type 헤더 없이 보내도 강제로 JSON 파싱 - Tasker 설정 단순화)
    data = request.get_json(silent=True, force=True) or {}
    title = str(data.get('title') or data.get('titleText')
                or request.form.get('title') or request.args.get('title') or '')
    body = str(data.get('body') or data.get('text') or data.get('message') or data.get('content')
               or request.form.get('body') or request.form.get('text') or request.form.get('message')
               or request.args.get('body') or request.args.get('text') or request.args.get('message') or '')
    package = str(data.get('package') or data.get('package_name') or data.get('app') or data.get('pkg')
                  or request.form.get('package') or request.args.get('package') or '')

    # JSON이 아니라 raw 텍스트로 보내는 경우도 허용
    if not body and not title:
        body = request.get_data(as_text=True) or ''
    if not body and not title:
        return jsonify({'success': False, 'error': 'empty notification'}), 400

    logger.info(f"[WEBHOOK] 폰 알림 수신: pkg={package}, title={title[:60]}, body={body[:80]}")
    _record_webhook_log('recv', f"수신: pkg={package or '없음'} | {title[:40]} | {body[:70]}")

    # Tasker 변수가 치환되지 않고 그대로 도착한 경우 → 즉시 안내 (가장 흔한 원인)
    unresolved = _detect_unresolved_tasker_vars(title, body)
    if unresolved:
        msg = ("❌ Tasker 변수가 그대로 전송됨: " + ", ".join(unresolved) +
               " → 알림 내용이 아니라 변수 이름만 도착했습니다. "
               "Tasker의 Notification 프로필 진입 변수는 %evtprm1(제목)/%evtprm2(내용)/%evtprm3(앱)을 사용하세요 (%ntitle/%ntext는 쓰지 마세요).")
        _record_webhook_log('error', msg)
        logger.warning(msg)
        return jsonify({'success': False, 'error': 'tasker_var_not_substituted',
                        'vars': unresolved, 'message': msg}), 200

    try:
        handled, depositor, amount = toss_monitor.process(title, body)
    except Exception as e:
        logger.error(f"[WEBHOOK] 입금 처리 오류: {e}")
        _record_webhook_log('error', f"처리 중 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    if handled:
        return jsonify({'success': True, 'deposit_detected': True,
                        'depositor': depositor, 'amount': amount})
    _record_webhook_log('warn', "⚠️ 알림을 받았지만 입금으로 인식하지 못했습니다. (대시보드 '입금 로그' 탭의 웹훅 테스트로 확인)")
    return jsonify({'success': True, 'deposit_detected': False})


@app.route('/api/deposit-webhook/test', methods=['POST'])
@login_required
def api_test_deposit_webhook():
    """웹훅 파서 + 대기 충전요청 매칭 시뮬레이션 (포인트 지급 없음 - 진단 전용)"""
    data = request.get_json(silent=True, force=True) or {}
    title = str(data.get('title') or '').strip()
    body = str(data.get('body') or '').strip()
    package = str(data.get('package') or '').strip()
    if not title and not body:
        return jsonify({'success': False, 'message': '제목 또는 내용을 입력해주세요.'}), 400

    # 테스트 입력에 Tasker 변수가 그대로 오면 안내
    unresolved = _detect_unresolved_tasker_vars(title, body)
    if unresolved:
        return jsonify({'success': False, 'message': (
            "입력값에 Tasker 변수(" + ", ".join(unresolved) +
            ")가 그대로 있습니다. 실제 토스 알림 문구를 입력해주세요. "
            "(변수 문제가 아니라 '알림 내용'으로 테스트해야 합니다)"
        )}), 200

    try:
        parsed = toss_monitor.test_parse(title, body)
    except Exception as e:
        return jsonify({'success': False, 'message': f'파싱 중 오류: {e}'}), 500

    expire_minutes = _get_charge_expire_minutes()
    result = {
        'parsed': parsed,
        'expire_minutes': expire_minutes,
        'pending_count': 0,
        'matched': None,
        'match_hint': '',
    }

    # 만료되지 않은 대기 요청만 대상
    from datetime import timedelta as _td
    now = datetime.utcnow()
    active = []
    for charge in db.get_pending_charge_requests():
        try:
            created_at = datetime.strptime(charge['created_at'], '%Y-%m-%d %H:%M:%S')
            if now - created_at > _td(minutes=expire_minutes):
                continue
        except Exception:
            pass
        active.append(charge)
    result['pending_count'] = len(active)

    if parsed.get('parsed_ok'):
        amount = parsed['amount']
        depositor = (parsed.get('depositor') or '').lower().strip()
        text_lower = parsed.get('text', '').lower()
        for charge in active:
            if charge['amount'] != amount:
                continue
            req_name = charge['depositor_name'].lower().strip()
            req_clean = re.sub(r'[\d,]+원?', '', req_name)
            req_clean = re.sub(r'입금|충전|계좌|이체|송금', '', req_clean).strip()
            if (depositor and depositor == req_name) or req_name in text_lower or (req_clean and req_clean in text_lower):
                result['matched'] = {
                    'request_id': charge['id'],
                    'username': charge['username'],
                    'depositor_name': charge['depositor_name'],
                    'amount': charge['amount'],
                }
                break
        if result['matched']:
            result['match_hint'] = f"✅ 실제 입금이라면 요청 #{result['matched']['request_id']}({result['matched']['username']})에 자동 충전됩니다."
        elif not active:
            result['match_hint'] = "⚠️ 유효한 대기 충전 요청이 없습니다. 유저가 먼저 '충전 신청'을 해야 자동 충전이 됩니다."
        else:
            result['match_hint'] = "⚠️ 금액+입금자명이 둘 다 일치하는 대기 요청이 없습니다. 충전 신청 금액과 입금자명이 알림과 정확히 같아야 합니다."
    else:
        result['match_hint'] = "❌ 알림에서 금액/입금자명을 추출하지 못했습니다. 알림 텍스트에 금액(원)과 입금자명이 포함되어야 합니다."

    return jsonify({'success': True, **result})


@app.route('/api/deposit-webhook/logs', methods=['GET'])
@login_required
def api_deposit_webhook_logs():
    """최근 웹훅 수신/처리 로그 (고장 진단용)"""
    with _webhook_log_lock:
        logs = list(_webhook_log)
    return jsonify({'success': True, 'logs': logs})


@app.route('/api/deposit-webhook/logs/clear', methods=['POST'])
@login_required
def api_clear_deposit_webhook_logs():
    """웹훅 수신 로그 전체 삭제"""
    with _webhook_log_lock:
        _webhook_log.clear()
    logger.info("[WEBHOOK] 관리자가 웹훅 로그 전체 삭제 실행")
    return jsonify({'success': True, 'message': '로그가 삭제되었습니다.'})


# --- 셀프 진단 (봇이 안 뜰 때 / IP 차단 여부 확인) ---
@app.route('/api/net-diag', methods=['GET'])
@login_required
def api_net_diag():
    """서버의 아웃바운드 IP와 봇 연결 상태를 돌려준다.

    Cloudflare 1015(디스코드 로그인 IP 차단)는 이 IP 단위로 발생하므로,
    차단이 의심될 때 현재 IP를 확인하고 호스팅/프록시를 바꾸는 데 사용한다.
    """
    outbound_ip = ''
    try:
        outbound_ip = requests.get('https://api.ipify.org', timeout=5).text.strip()
    except Exception as e:
        logger.warning(f"[DIAG] 아웃바운드 IP 조회 실패: {e}")
    return jsonify({
        'outbound_ip': outbound_ip,
        'proxy': getattr(bot, 'proxy_label', '') or '',
        'bot_ready': bot.is_ready() if bot else False,
        'bot_user': str(bot.user) if getattr(bot, 'user', None) else '',
        'rate_limited': bot._is_rate_limited() if bot else False,
    })


# --- Stats ---
@app.route('/api/stats', methods=['GET'])
@login_required
def api_get_stats():
    stats = db.get_order_stats()
    stats['total_revenue'] = db.get_total_revenue()
    stats['total_products'] = len(db.get_products())
    stats['total_keys'] = len(db.get_keys(used=0))
    return jsonify(stats)

# ============ STARTUP ============
def start_bot():
    """Start the Discord bot"""
    token = os.environ.get('DISCORD_TOKEN', db.get_setting('discord_token', ''))
    if not token:
        logger.warning("Discord token not set. Bot will not start.")
        return
    
    # Start bot in thread
    bot.run_bot(token)
    logger.info("Bot thread started")

def start_ping_keeper():
    """Start 24h ping keeper"""
    thread = threading.Thread(target=ping_keeper, daemon=True)
    thread.start()
    logger.info(f"Ping keeper 시작됨 (5분 간격 | 핑 URL: {_get_ping_url() or '미설정'})")

# ============ STARTUP (runs on import for gunicorn) ============
# Start bot
start_bot()

# Start ping keeper
start_ping_keeper()

# ============ MAIN ============
if __name__ == '__main__':
    # Run Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)