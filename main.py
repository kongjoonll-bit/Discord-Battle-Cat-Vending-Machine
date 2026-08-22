import os
import logging
import threading
import time
import asyncio
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
import requests

import discord
import database as db
from bot import VendingBot
from pushbullet_monitor import PushbulletMonitor

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize database
db.init_db()

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
pushbullet_monitor = PushbulletMonitor()

# ============ PING KEEPER (24h) ============
def ping_keeper():
    """Send ping every 24 hours to keep the service alive on Render"""
    while True:
        try:
            dashboard_url = db.get_setting('dashboard_url', '')
            if dashboard_url:
                resp = requests.get(dashboard_url + '/ping', timeout=10)
                logger.info(f"Ping sent to {dashboard_url}: {resp.status_code}")
            else:
                logger.info("Dashboard URL not set, skipping ping")
        except Exception as e:
            logger.error(f"Ping error: {e}")
        time.sleep(24 * 60 * 60)  # 24 hours

# ============ DEPOSIT HANDLER ============
def handle_deposit(amount, text, depositor=None):
    """Handle detected deposit from Pushbullet - auto point award when amount AND depositor name match"""
    logger.info(f"Processing deposit: {amount}원, depositor: {depositor}, text: {text[:100]}")
    
    # Check if charge request is expired (5 min)
    pending_charges = db.get_pending_charge_requests()
    from datetime import datetime, timedelta
    # SQLite CURRENT_TIMESTAMP는 UTC로 저장되므로 UTC 기준으로 비교해야 함
    now = datetime.utcnow()
    
    # Find matching pending charge request first
    for charge in pending_charges:
        # Check expiry (5 minutes)
        try:
            created_at = datetime.strptime(charge['created_at'], '%Y-%m-%d %H:%M:%S')
            if now - created_at > timedelta(minutes=5):
                db.cancel_charge_request(charge['id'])
                logger.info(f"Charge request #{charge['id']} expired")
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
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            embed = discord.Embed(
                title="✅ 포인트 충전 완료!",
                description=f"{amount:,}포인트가 충전되었습니다!",
                color=discord.Color.green()
            )
            embed.add_field(name="입금자명", value=charge['depositor_name'], inline=True)
            embed.add_field(name="충전 금액", value=f"{amount:,}포인트", inline=True)
            embed.add_field(name="현재 잔액", value=f"{new_balance:,}포인트", inline=True)
            embed.set_footer(text="냥코 KEY 자판기 🐱")
            
            loop.run_until_complete(bot.send_dm(charge['user_id'], embed=embed))
            loop.close()
        except Exception as e:
            logger.error(f"Charge DM error: {e}")
        
        # Send deposit log to deposit log channel
        deposit_log_channel_id = db.get_setting('deposit_log_channel_id', '')
        if deposit_log_channel_id and bot.is_ready():
            try:
                channel = bot.get_channel(int(deposit_log_channel_id))
                if channel:
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
                    asyncio.run_coroutine_threadsafe(channel.send(embed=embed_log), bot.loop)
            except Exception as e:
                logger.error(f"Deposit log channel error: {e}")
        
        # Notify admin channel (always for tracking)
        admin_channel_id = db.get_setting('admin_channel_id', '')
        if admin_channel_id and bot.is_ready():
            try:
                channel = bot.get_channel(int(admin_channel_id))
                if channel:
                    asyncio.run_coroutine_threadsafe(
                        channel.send(f"💰 **충전 완료!**\n{charge['username']} 님이 {amount:,}포인트 충전\n입금자명: {charge['depositor_name']}"),
                        bot.loop
                    )
            except Exception as e:
                logger.error(f"Admin channel notify error: {e}")
        
        return  # Deposit processed, don't continue to unallocated
    
    # If no matching charge request found with both amount+name, send unallocated log
    logger.info(f"No matching charge request for {amount:,}원 (depositor: {depositor})")
    
    # Send deposit log to deposit log channel (unmatched)
    deposit_log_channel_id = db.get_setting('deposit_log_channel_id', '')
    if deposit_log_channel_id and bot.is_ready():
        try:
            channel = bot.get_channel(int(deposit_log_channel_id))
            if channel:
                embed_log = discord.Embed(
                    title="💰 입금 감지 (미할당)",
                    description=f"금액: {amount:,}원\n내용: {text[:150]}",
                    color=discord.Color.orange(),
                    timestamp=datetime.now()
                )
                if depositor:
                    embed_log.add_field(name="예상 입금자", value=depositor, inline=True)
                asyncio.run_coroutine_threadsafe(channel.send(embed=embed_log), bot.loop)
        except Exception as e:
            logger.error(f"Deposit log channel error: {e}")
    
    # Notify admin channel about unallocated deposit
    admin_channel_id = db.get_setting('admin_channel_id', '')
    if admin_channel_id and bot.is_ready():
        try:
            channel = bot.get_channel(int(admin_channel_id))
            if channel:
                asyncio.run_coroutine_threadsafe(
                    channel.send(f"💰 **입금 감지됨 (미할당)**\n금액: {amount:,}원\n내용: {text[:100]}\n\n대시보드에서 유저에게 포인트를 지급해주세요!"),
                    bot.loop
                )
        except Exception as e:
            logger.error(f"Admin channel notify error: {e}")

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
    settings = db.get_all_settings()
    
    # Bot status
    bot_status = 'online' if bot.is_ready() else 'offline'
    
    return render_template('dashboard.html',
        stats=stats,
        total_revenue=total_revenue,
        products=products,
        pending_orders=pending_orders,
        settings=settings,
        bot_status=bot_status,
        bot_name=settings.get('bot_name', '냥코 KEY 자판기')
    )

# ============ API ROUTES ============

# --- Notice (공지) ---
@app.route('/api/notice/send', methods=['POST'])
@login_required
def api_send_notice():
    """공지 채널에 임베드 공지 전송"""
    data = request.json
    title = data.get('title', '').strip()
    content = data.get('content', '').strip()
    color = data.get('color', '')
    
    if not title or not content:
        return jsonify({'error': '제목과 내용을 입력해주세요.'}), 400
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success, msg = loop.run_until_complete(bot.send_notice(title, content, color))
        loop.close()
        return jsonify({'success': success, 'message': msg})
    except Exception as e:
        logger.error(f"Send notice error: {e}")
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
    
    # 입고 채널에 공지 전송 (봇이 실행 중일 때만)
    product = db.get_product(pid)
    if product and bot.is_ready():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(bot.send_stock_notice(product, len(key_list)))
            loop.close()
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

# --- Pushbullet test ---
@app.route('/api/pushbullet/test', methods=['POST'])
@login_required
def api_test_pushbullet():
    success, msg = pushbullet_monitor.test_connection()
    return jsonify({'success': success, 'message': msg})

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

def start_pushbullet():
    """Start Pushbullet monitor"""
    # Load Pushbullet token from .env into database if not already set
    env_token = os.environ.get('PUSHBULLET_TOKEN', '')
    if env_token:
        db.set_setting('pushbullet_token', env_token)
        logger.info("Pushbullet token loaded from .env")
    
    pushbullet_monitor.on_deposit_detected = handle_deposit
    pushbullet_monitor.start()
    logger.info("Pushbullet monitor started (WebSocket 실시간 감지)")

def start_ping_keeper():
    """Start 24h ping keeper"""
    thread = threading.Thread(target=ping_keeper, daemon=True)
    thread.start()
    logger.info("Ping keeper started (24h interval)")

# ============ STARTUP (runs on import for gunicorn) ============
# Start bot
start_bot()

# Start pushbullet monitor
start_pushbullet()

# Start ping keeper
start_ping_keeper()

# ============ MAIN ============
if __name__ == '__main__':
    # Run Flask app
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)