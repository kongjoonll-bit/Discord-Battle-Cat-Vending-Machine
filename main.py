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

# Create Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nyanko-vending-secret-key-change-me')

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
    """핑 대상 URL 조회: 설정의 dashboard_url 우선, 없으면 Render 외부 URL(RENDER_EXTERNAL_URL) 자동 사용"""
    url = db.get_setting('dashboard_url', '')
    if not url:
        url = os.environ.get('RENDER_EXTERNAL_URL', '')
    return url.strip() if url else ''

def ping_keeper():
    """5분마다 서비스에 핑 전송 (Render 무료 플랜 슬립 방지)"""
    while True:
        try:
            url = _get_ping_url()
            if url:
                resp = requests.get(url.rstrip('/') + '/ping', timeout=10)
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
        
        # Send deposit log to deposit log channel
        embed_log = discord.Embed(
            title="💰 입금 확인!",
            description=f"**{charge['username']}** 님의 입금이 확인되었습니다!",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed_log.add_field(name="입금자명", value=charge['depositor_name'], inline=True)
        embed_log.add_field(name="입금 금액", value=f"{amount:,}원", inline=True)
        embed_log.add_field(name="충전 포인트", value=f"{amount:,}포인트", inline=True)
        embed_log.add_field(name="현재 잔액", value=f"{new_balance:,}포인트", inline=True)
        send_to_discord_channel('deposit_log_channel_id', embed=embed_log)
        
        # Notify admin channel (always for tracking)
        send_to_discord_channel('admin_channel_id',
            content=f"💰 **충전 완료!**\n{charge['username']} 님이 {amount:,}포인트 충전\n입금자명: {charge['depositor_name']}")
        
        return  # Deposit processed, don't continue to unallocated
    
    # If no matching charge request found with both amount+name, send unallocated log
    logger.info(f"No matching charge request for {amount:,}원 (depositor: {depositor})")
    _record_webhook_log('warn', f"미할당 입금: {amount:,}원 (입금자: {depositor or '파싱실패'}) - 금액/입금자명 일치 대기요청 없음")
    
    # Send deposit log to deposit log channel (unmatched)
    embed_log = discord.Embed(
        title="💰 입금 감지 (미할당)",
        description=f"금액: {amount:,}원\n내용: {text[:150]}",
        color=discord.Color.orange(),
        timestamp=datetime.now()
    )
    if depositor:
        embed_log.add_field(name="예상 입금자", value=depositor, inline=True)
    send_to_discord_channel('deposit_log_channel_id', embed=embed_log)
    
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
        password = request.form.get('password', '')
        admin_password = db.get_setting('admin_password', 'zizer731!!')
        if password == admin_password:
            # 영구 세션으로 설정 - 로그아웃하지 않는 이상 로그인 유지
            session.permanent = True
            session['admin_logged_in'] = True
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='비밀번호가 올바르지 않습니다.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
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
    _base = (settings.get('dashboard_url', '')
             or os.environ.get('RENDER_EXTERNAL_URL', '')
             or f"http://localhost:{os.environ.get('PORT', '5001')}").rstrip('/')
    webhook_full_url = f"{_base}/api/deposit-webhook?key={webhook_key}"

    # Bot status
    bot_status = 'online' if bot.is_ready() else 'offline'

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
        charge_expire_minutes=charge_expire_minutes
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
    return jsonify({'success': True})

@app.route('/api/products/<int:pid>', methods=['DELETE'])
@login_required
def api_delete_product(pid):
    db.delete_product(pid)
    return jsonify({'success': True})

# --- Keys ---
@app.route('/api/products/<int:pid>/keys', methods=['POST'])
@login_required
def api_add_keys(pid):
    data = request.json
    keys_text = data.get('keys', '')
    key_list = [k.strip() for k in keys_text.split('\n') if k.strip()]
    if not key_list:
        return jsonify({'error': '키를 입력해주세요.'}), 400
    db.add_keys(pid, key_list)
    
    # 입고 채널에 공지 전송 + 자판기 새로고침 (봇이 실행 중일 때만)
    product = db.get_product(pid)
    if product and bot.is_ready():
        try:
            # 봇의 이벤트 루프에서 안전하게 실행 (스레드 간 호출)
            asyncio.run_coroutine_threadsafe(bot.send_stock_notice(product, len(key_list)), bot.loop)
            asyncio.run_coroutine_threadsafe(bot.refresh_vending_machines(), bot.loop)
        except Exception as e:
            logger.error(f"Stock notice error: {e}")
    
    return jsonify({'success': True, 'count': len(key_list)})

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
    return jsonify(db.get_all_settings())

@app.route('/api/settings', methods=['POST'])
@login_required
def api_update_settings():
    data = request.json
    for key, value in data.items():
        db.set_setting(key, value)
    return jsonify({'success': True})

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
    data = request.json
    amount = data.get('amount', 0)
    username = data.get('username', '')
    if amount <= 0:
        return jsonify({'error': '금액을 확인해주세요.'}), 400
    new_balance = db.add_balance(user_id, username, amount, 'admin_add', '대시보드에서 포인트 지급')
    return jsonify({'success': True, 'balance': new_balance})

@app.route('/api/users/<user_id>/points/deduct', methods=['POST'])
@login_required
def api_deduct_points(user_id):
    data = request.json
    amount = data.get('amount', 0)
    if amount <= 0:
        return jsonify({'error': '금액을 확인해주세요.'}), 400
    success = db.deduct_balance(user_id, amount, 'admin_remove', '대시보드에서 포인트 차감')
    if not success:
        return jsonify({'error': '잔액 부족'}), 400
    new_balance = db.get_balance(user_id)
    return jsonify({'success': True, 'balance': new_balance})

# --- Coupons (쿠폰/할인 이벤트) ---
@app.route('/api/coupons', methods=['GET'])
@login_required
def api_get_coupons():
    return jsonify(db.get_coupons())

@app.route('/api/coupons', methods=['POST'])
@login_required
def api_create_coupon():
    data = request.json
    code = data.get('code', '').strip()
    discount_amount = data.get('discount_amount', 0)
    max_uses = data.get('max_uses', 0)
    if not code:
        return jsonify({'error': '쿠폰 코드를 입력해주세요.'}), 400
    try:
        discount_amount = int(discount_amount)
        max_uses = int(max_uses)
    except (TypeError, ValueError):
        return jsonify({'error': '할인 금액과 사용 횟수는 숫자로 입력해주세요.'}), 400
    if discount_amount <= 0:
        return jsonify({'error': '할인 금액은 0보다 커야 합니다.'}), 400
    if max_uses < 0:
        return jsonify({'error': '최대 사용 횟수는 0 이상이어야 합니다.'}), 400
    cid = db.create_coupon(code, discount_amount, max_uses)
    if cid == -1:
        return jsonify({'error': '이미 존재하는 쿠폰 코드입니다.'}), 400
    return jsonify({'success': True, 'id': cid})

@app.route('/api/coupons/<int:cid>', methods=['DELETE'])
@login_required
def api_delete_coupon(cid):
    db.delete_coupon(cid)
    return jsonify({'success': True})

@app.route('/api/coupons/<int:cid>/toggle', methods=['POST'])
@login_required
def api_toggle_coupon(cid):
    db.toggle_coupon(cid)
    coupon = db.get_coupon(cid)
    return jsonify({'success': True, 'active': coupon['active'] if coupon else 0})

# ============ 입금 웹훅 (Pushbullet 없이 폰 앱이 은행 알림을 직접 전송) ============

# ---- 웹훅 진단 로그 (대시보드 표시용 최근 50건 링버퍼) ----
import collections
_webhook_log_lock = threading.Lock()
_webhook_log = collections.deque(maxlen=50)

def _record_webhook_log(level, message):
    """웹훅 수신/파싱/매칭 결과를 링버퍼에 기록 (대시보드 진단용)"""
    try:
        with _webhook_log_lock:
            _webhook_log.appendleft({
                'time': datetime.now().strftime('%m-%d %H:%M:%S'),
                'level': str(level),
                'message': str(message)[:300],
            })
    except Exception:
        pass

# 충전 요청 유효 시간 기본값 (분) - 설정키: charge_expire_minutes
DEFAULT_CHARGE_EXPIRE_MINUTES = 30

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

    # JSON 파싱 (Content-Type 헤더 없이 보내도 강제로 JSON 파싱 - Tasker 설정 단순화)
    data = request.get_json(silent=True, force=True) or {}
    title = str(data.get('title') or data.get('titleText') or '')
    body = str(data.get('body') or data.get('text') or data.get('message') or data.get('content') or '')
    package = str(data.get('package') or data.get('package_name') or data.get('app') or data.get('pkg') or '')

    # JSON이 아니라 raw 텍스트로 보내는 경우도 허용
    if not body and not title:
        body = request.get_data(as_text=True) or ''
    if not body and not title:
        return jsonify({'success': False, 'error': 'empty notification'}), 400

    logger.info(f"[WEBHOOK] 폰 알림 수신: pkg={package}, title={title[:60]}, body={body[:80]}")
    _record_webhook_log('recv', f"수신: pkg={package or '없음'} | {title[:40]} | {body[:70]}")
    try:
        handled, depositor, amount = toss_monitor.process(title, body)
    except Exception as e:
        logger.error(f"[WEBHOOK] 입금 처리 오류: {e}")
        _record_webhook_log('error', f"처리 중 오류: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    if handled:
        return jsonify({'success': True, 'deposit_detected': True,
                        'depositor': depositor, 'amount': amount})
    _record_webhook_log('warn', "⚠️ 알림을 받았지만 입금으로 인식하지 못했습니다. (대시보드 '웹훅 테스트'로 확인)")
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