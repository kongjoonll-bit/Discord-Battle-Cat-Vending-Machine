import sqlite3
import os
import json
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'vending.db')

def get_conn():
    """SQLite 연결 생성 (동시성/안정성 강화)

    - timeout/busy_timeout: 봇 스레드와 Flask 스레드가 동시에 접근해도
      'database is locked' 로 죽지 않도록 대기 시간을 준다.
    - WAL: 읽기/쓰기 동시성 향상 (오래 켜두면 필수)
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
    except sqlite3.DatabaseError:
        pass
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    
    # Settings table
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    # Products table
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        description TEXT DEFAULT '',
        stock INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        category TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Add category column if not exists (migration for existing DBs)
    try:
        c.execute('ALTER TABLE products ADD COLUMN category TEXT DEFAULT \'\'')
    except:
        pass  # column already exists
    
    # Keys table
    c.execute('''CREATE TABLE IF NOT EXISTS keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        key_value TEXT NOT NULL,
        used INTEGER DEFAULT 0,
        order_id INTEGER DEFAULT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')
    
    # Orders table
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',  -- pending, confirmed, cancelled, completed
        key_id INTEGER DEFAULT NULL,
        reviewed INTEGER DEFAULT 0,  -- 후기 작성 여부 (구매당 1개 제한)
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        confirmed_at TEXT DEFAULT NULL,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')

    # 기존 DB 마이그레이션: 후기 작성 여부 컬럼 보장
    try:
        c.execute("ALTER TABLE orders ADD COLUMN reviewed INTEGER DEFAULT 0")
    except Exception:
        pass  # 이미 존재
    
    # Admins table
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL
    )''')
    
    # Users table (for balance/points)
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        balance INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Transactions table
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        amount INTEGER NOT NULL,
        type TEXT NOT NULL,  -- charge, purchase, admin_add, admin_remove
        description TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Charge requests table (for deposit matching)
    c.execute('''CREATE TABLE IF NOT EXISTS charge_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        depositor_name TEXT NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',  -- pending, completed, cancelled
        receipt_image_url TEXT DEFAULT '',  -- 수동 승인용 입금 스크린샷 첨부 이미지 URL
        decided_by TEXT DEFAULT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT DEFAULT NULL
    )''')

    # 기존 DB 마이그레이션: 증거이미지/결정자/리마인더 발송여부 컬럼 보장
    for _col in ('receipt_image_url TEXT DEFAULT \'\'', 'decided_by TEXT', 'reminder_sent INTEGER DEFAULT 0'):
        try:
            c.execute(f'ALTER TABLE charge_requests ADD COLUMN {_col}')
        except Exception:
            pass  # 이미 존재
    
    # Tickets table (for inquiry/support system)
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        status TEXT DEFAULT 'open',  -- open, closed
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        closed_at TEXT DEFAULT NULL
    )''')
    
    # Coupons table (할인 쿠폰/이벤트)
    c.execute('''CREATE TABLE IF NOT EXISTS coupons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        discount_amount INTEGER NOT NULL,
        discount_type TEXT DEFAULT 'amount',  -- amount = 정액, percent = 정률(%)
        max_uses INTEGER DEFAULT 0,  -- 0 = 무제한
        used_count INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    # 기존 DB 마이그레이션: 쿠폰 할인 방식 컬럼 보장
    try:
        c.execute("ALTER TABLE coupons ADD COLUMN discount_type TEXT DEFAULT 'amount'")
    except Exception:
        pass  # 이미 존재

    # 재고(입고) 로그 테이블 - '입고 로그가 안 뜨는' 문제 해결용
    c.execute('''CREATE TABLE IF NOT EXISTS stock_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        product_name TEXT NOT NULL,
        count INTEGER NOT NULL,
        total_stock INTEGER DEFAULT 0,
        source TEXT DEFAULT 'dashboard',  -- dashboard / command / bot
        actor TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    # 감사 로그 (관리자 행동 추적 - 보안)
    c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        actor_id TEXT DEFAULT '',
        actor_name TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    # 쿠폰 사용 기록 (누가 어떤 쿠폰으로 얼마 할인받았는지)
    c.execute('''CREATE TABLE IF NOT EXISTS coupon_uses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        coupon_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        user_id TEXT NOT NULL,
        order_id INTEGER DEFAULT NULL,
        discount INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    # 성능 인덱스 (데이터가 쌓여도 느려지지 않게)
    for _sql in (
        'CREATE INDEX IF NOT EXISTS idx_keys_product_used ON keys(product_id, used)',
        'CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)',
        'CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_charge_status ON charge_requests(status)',
        'CREATE INDEX IF NOT EXISTS idx_stock_logs_created ON stock_logs(created_at)',
    ):
        try:
            c.execute(_sql)
        except Exception:
            pass

    conn.commit()
    conn.close()

# ============ SETTINGS ============
def get_setting(key, default=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key=?', (key,))
    row = c.fetchone()
    conn.close()
    if row:
        return row['value']
    return default

def set_setting(key, value):
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
    conn.commit()
    conn.close()

def get_all_settings():
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM settings')
    rows = c.fetchall()
    conn.close()
    return {row['key']: row['value'] for row in rows}

# ============ PRODUCTS ============
def add_product(name, price, description='', stock=0, category=''):
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT INTO products (name, price, description, stock, category) VALUES (?, ?, ?, ?, ?)',
              (name, price, description, stock, category))
    conn.commit()
    pid = c.lastrowid
    conn.close()
    return pid

def get_products(active_only=False):
    conn = get_conn()
    c = conn.cursor()
    if active_only:
        c.execute('SELECT * FROM products WHERE active=1 ORDER BY id')
    else:
        c.execute('SELECT * FROM products ORDER BY id')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_categories():
    """Get all product categories (non-empty, unique)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT category FROM products WHERE category != '' ORDER BY category")
    rows = c.fetchall()
    conn.close()
    return [r['category'] for r in rows if r['category']]

def get_products_by_category(category, active_only=False):
    """Get products filtered by category"""
    conn = get_conn()
    c = conn.cursor()
    if active_only:
        c.execute('SELECT * FROM products WHERE active=1 AND category=? ORDER BY id', (category,))
    else:
        c.execute('SELECT * FROM products WHERE category=? ORDER BY id', (category,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_product(pid):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM products WHERE id=?', (pid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def update_product(pid, name=None, price=None, description=None, stock=None, active=None, category=None):
    conn = get_conn()
    c = conn.cursor()
    product = get_product(pid)
    if not product:
        conn.close()
        return False
    if name is not None: product['name'] = name
    if price is not None: product['price'] = price
    if description is not None: product['description'] = description
    if stock is not None: product['stock'] = stock
    if active is not None: product['active'] = active
    if category is not None: product['category'] = category
    c.execute('UPDATE products SET name=?, price=?, description=?, stock=?, active=?, category=? WHERE id=?',
              (product['name'], product['price'], product['description'], product['stock'], product['active'], product['category'], pid))
    conn.commit()
    conn.close()
    return True

def delete_product(pid):
    conn = get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM products WHERE id=?', (pid,))
    c.execute('DELETE FROM keys WHERE product_id=?', (pid,))
    conn.commit()
    conn.close()

# ============ KEYS ============
def add_keys(product_id, key_list):
    """키 여러 개 추가 (추가된 개수 반환)"""
    conn = get_conn()
    c = conn.cursor()
    added = 0
    for k in key_list:
        k = str(k).strip()
        if not k:
            continue
        c.execute('INSERT INTO keys (product_id, key_value) VALUES (?, ?)', (product_id, k))
        added += 1
    conn.commit()
    conn.close()
    return added

def get_available_key(product_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM keys WHERE product_id=? AND used=0 ORDER BY id LIMIT 1', (product_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def claim_key(product_id, order_id):
    """키를 원자적으로 선점(지급)한다 - 동시 구매 시 같은 키가 중복 지급되는 것 방지

    Returns: 선점한 키 dict 또는 None(재고 없음)
    """
    conn = get_conn()
    c = conn.cursor()
    try:
        for _ in range(5):
            c.execute('SELECT * FROM keys WHERE product_id=? AND used=0 ORDER BY id LIMIT 1', (product_id,))
            row = c.fetchone()
            if not row:
                return None
            c.execute('UPDATE keys SET used=1, order_id=? WHERE id=? AND used=0', (order_id, row['id']))
            conn.commit()
            if c.rowcount:
                return dict(row)
        return None
    finally:
        conn.close()

def release_key(key_id):
    """선점한 키를 다시 판매 가능 상태로 되돌린다 (결제 실패/환불 시)

    Returns: 되돌리기 성공 여부
    """
    if not key_id:
        return False
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE keys SET used=0, order_id=NULL WHERE id=?', (int(key_id),))
    ok = c.rowcount > 0
    conn.commit()
    conn.close()
    return ok

def get_keys(product_id=None, used=None):
    conn = get_conn()
    c = conn.cursor()
    if product_id and used is not None:
        c.execute('SELECT * FROM keys WHERE product_id=? AND used=? ORDER BY id DESC', (product_id, used))
    elif product_id:
        c.execute('SELECT * FROM keys WHERE product_id=? ORDER BY id DESC', (product_id,))
    elif used is not None:
        c.execute('SELECT * FROM keys WHERE used=? ORDER BY id DESC', (used,))
    else:
        c.execute('SELECT * FROM keys ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_key(key_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM keys WHERE id=?', (key_id,))
    conn.commit()
    conn.close()

def count_available_keys(product_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) as cnt FROM keys WHERE product_id=? AND used=0', (product_id,))
    row = c.fetchone()
    conn.close()
    return row['cnt'] if row else 0

# ============ ORDERS ============
def create_order(user_id, username, product_id, product_name, amount):
    conn = get_conn()
    c = conn.cursor()
    c.execute('''INSERT INTO orders (user_id, username, product_id, product_name, amount, status)
                 VALUES (?, ?, ?, ?, ?, 'pending')''',
              (user_id, username, product_id, product_name, amount))
    conn.commit()
    oid = c.lastrowid
    conn.close()
    return oid

def get_order(oid):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE id=?', (oid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def order_is_reviewed(oid):
    """해당 주문의 후기 작성 여부 확인 (1이면 이미 작성함)"""
    row = get_order(oid)
    if not row:
        return None
    return bool(row.get('reviewed', 0))

def mark_order_reviewed(oid):
    """해당 주문에 리뷰가 작성되었음을 기록"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE orders SET reviewed=1 WHERE id=?", (oid,))
    conn.commit()
    conn.close()

def get_orders(status=None, user_id=None, limit=100):
    conn = get_conn()
    c = conn.cursor()
    query = 'SELECT * FROM orders'
    conditions = []
    params = []
    if status:
        conditions.append('status=?')
        params.append(status)
    if user_id:
        conditions.append('user_id=?')
        params.append(user_id)
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_order_with_key(order_id):
    """주문과 함께 지급된 키를 조회 (관리자 패널용)"""
    order = get_order(order_id)
    if not order:
        return None
    key_val = None
    if order.get('key_id'):
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT key_value FROM keys WHERE id=?', (order['key_id'],))
        row = c.fetchone()
        conn.close()
        if row:
            key_val = row['key_value']
    order['key_value'] = key_val
    return order

def get_orders_with_keys(status=None, limit=200):
    """주문 목록과 함께 지급된 키를 조회 (관리자 패널용)"""
    conn = get_conn()
    c = conn.cursor()
    query = '''SELECT o.*, k.key_value 
               FROM orders o 
               LEFT JOIN keys k ON o.key_id = k.id'''
    conditions = []
    params = []
    if status:
        conditions.append('o.status=?')
        params.append(status)
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY o.id DESC LIMIT ?'
    params.append(limit)
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_pending_orders():
    return get_orders(status='pending')

def update_order_status(oid, status, key_id=None):
    conn = get_conn()
    c = conn.cursor()
    if key_id is not None:
        c.execute('UPDATE orders SET status=?, key_id=?, confirmed_at=CURRENT_TIMESTAMP WHERE id=?',
                  (status, key_id, oid))
    else:
        c.execute('UPDATE orders SET status=?, confirmed_at=CURRENT_TIMESTAMP WHERE id=?',
                  (status, oid))
    conn.commit()
    conn.close()

def get_order_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT status, COUNT(*) as cnt FROM orders GROUP BY status')
    rows = c.fetchall()
    conn.close()
    stats = {'pending': 0, 'confirmed': 0, 'cancelled': 0, 'completed': 0}
    for r in rows:
        stats[r['status']] = r['cnt']
    return stats

def get_total_revenue():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(amount), 0) as total FROM orders WHERE status IN ('confirmed', 'completed')")
    row = c.fetchone()
    conn.close()
    return row['total'] if row else 0

# ============ VENDING MACHINE ============
def set_vending_message(channel_id, message_id, content_hash=''):
    """자판기 메시지 위치 저장 (content_hash: 메시지 내용이 안 바뀌면 edit을 생략하기 위한 서명)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vending_messages (
        channel_id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        content_hash TEXT DEFAULT ''
    )''')
    # 기존 DB에 content_hash 컬럼 추가 (마이그레이션)
    try:
        c.execute("ALTER TABLE vending_messages ADD COLUMN content_hash TEXT DEFAULT ''")
    except Exception:
        pass  # 이미 존재
    c.execute('INSERT OR REPLACE INTO vending_messages (channel_id, message_id, content_hash) VALUES (?, ?, ?)',
              (str(channel_id), str(message_id), content_hash or ''))
    conn.commit()
    conn.close()

def get_vending_message(channel_id):
    """Get vending machine message for a channel"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vending_messages (
        channel_id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        content_hash TEXT DEFAULT ''
    )''')
    try:
        c.execute("ALTER TABLE vending_messages ADD COLUMN content_hash TEXT DEFAULT ''")
    except Exception:
        pass  # 이미 존재
    c.execute('SELECT * FROM vending_messages WHERE channel_id=?', (str(channel_id),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_vending_channels():
    """Get all channels with vending machines"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vending_messages (
        channel_id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL,
        content_hash TEXT DEFAULT ''
    )''')
    try:
        c.execute("ALTER TABLE vending_messages ADD COLUMN content_hash TEXT DEFAULT ''")
    except Exception:
        pass  # 이미 존재
    c.execute('SELECT * FROM vending_messages')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ============ USERS (POINTS) ============
def get_or_create_user(user_id, username):
    """Get or create a user"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id=?', (str(user_id),))
    row = c.fetchone()
    if not row:
        c.execute('INSERT INTO users (user_id, username) VALUES (?, ?)', (str(user_id), username))
        conn.commit()
        c.execute('SELECT * FROM users WHERE user_id=?', (str(user_id),))
        row = c.fetchone()
    conn.close()
    return dict(row)

def get_user(user_id):
    """Get a user"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id=?', (str(user_id),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_balance(user_id):
    """Get user balance"""
    user = get_user(user_id)
    return user['balance'] if user else 0

def add_balance(user_id, username, amount, trans_type='admin_add', description=''):
    """Add balance to user"""
    user = get_or_create_user(user_id, username)
    new_balance = user['balance'] + amount
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE users SET balance=?, username=? WHERE user_id=?', (new_balance, username, str(user_id)))
    c.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
              (str(user_id), amount, trans_type, description))
    conn.commit()
    conn.close()
    return new_balance

def deduct_balance(user_id, amount, trans_type='purchase', description='', username=None):
    """잔액 차감 (원자적)

    - 예전 구현은 '잔액 조회 → 계산 → 덮어쓰기' 라서 동시에 두 번 결제하면
      잔액이 마이너스가 되거나 한 번만 차감되는 문제가 있었다.
    - 이제는 조건부 UPDATE 로 한 번에 처리하고, 실제 차감 여부를 rowcount 로 확인한다.
    - 0원 결제(무료 쿠폰 등)도 실패하지 않고 거래내역만 남긴다.
    """
    amount = int(amount)
    if amount < 0:
        return False

    user = get_user(user_id)
    if not user and username:
        get_or_create_user(user_id, username)

    conn = get_conn()
    c = conn.cursor()
    if amount == 0:
        c.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, 0, ?, ?)',
                  (str(user_id), trans_type, description))
        conn.commit()
        conn.close()
        return True

    c.execute('UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?',
              (amount, str(user_id), amount))
    if c.rowcount == 0:
        conn.rollback()
        conn.close()
        return False
    c.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
              (str(user_id), -amount, trans_type, description))
    conn.commit()
    conn.close()
    return True

def add_balance_all(amount, trans_type='admin_add', description=''):
    """모든 등록 유저에게 동일한 포인트 지급 (각 유저별 거래 내역 기록)

    SQL 증분 연산으로 처리해 동시 결제/충전과 충돌하지 않게 한다.

    Returns: 지급된 유저 수
    """
    amount = int(amount)
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT user_id FROM users')
    user_ids = [str(r['user_id']) for r in c.fetchall()]
    for uid in user_ids:
        c.execute('UPDATE users SET balance = balance + ? WHERE user_id=?', (amount, uid))
        c.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
                  (uid, amount, trans_type, description))
    conn.commit()
    conn.close()
    return len(user_ids)

def deduct_balance_all(amount, trans_type='admin_remove', description=''):
    """모든 등록 유저에게서 동일한 포인트 차감 (잔액이 0 미만으로는 내려가지 않음)

    Returns: (차감된 유저 수, 실제 총 차감 포인트)
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM users')
    rows = c.fetchall()
    affected = 0
    total_deducted = 0
    for row in rows:
        deducted = min(amount, row['balance'])
        if deducted <= 0:
            continue
        new_balance = row['balance'] - deducted
        c.execute('UPDATE users SET balance=? WHERE user_id=?', (new_balance, str(row['user_id'])))
        c.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
                  (str(row['user_id']), -deducted, trans_type, description))
        affected += 1
        total_deducted += deducted
    conn.commit()
    conn.close()
    return affected, total_deducted

def get_users():
    """Get all users"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM users ORDER BY balance DESC')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_user_transactions(user_id, limit=20):
    """Get user transactions"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT ?', (str(user_id), limit))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_total_balance():
    """Get total balance across all users"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT COALESCE(SUM(balance), 0) as total FROM users')
    row = c.fetchone()
    conn.close()
    return row['total'] if row else 0

# ============ CHARGE REQUESTS ============
def create_charge_request(user_id, username, depositor_name, amount):
    """Create a charge request"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT INTO charge_requests (user_id, username, depositor_name, amount) VALUES (?, ?, ?, ?)',
              (str(user_id), username, depositor_name, amount))
    conn.commit()
    rid = c.lastrowid
    conn.close()
    return rid

def get_pending_charge_requests():
    """Get all pending charge requests"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM charge_requests WHERE status=? ORDER BY id', ('pending',))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_charge_request(rid):
    """Get a charge request by id"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM charge_requests WHERE id=?', (rid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def complete_charge_request(rid):
    """Mark a charge request as completed"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE charge_requests SET status=?, completed_at=CURRENT_TIMESTAMP WHERE id=?', ('completed', rid))
    conn.commit()
    conn.close()

def cancel_charge_request(rid):
    """Mark a charge request as cancelled"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE charge_requests SET status=? WHERE id=?', ('cancelled', rid))
    conn.commit()
    conn.close()

def set_charge_receipt_image(rid, image_url):
    """충전 요청에 입금 증거 스크린샷 이미지 URL 저장"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE charge_requests SET receipt_image_url=? WHERE id=?', (image_url, rid))
    conn.commit()
    conn.close()

def get_pending_charge_requests_by_user(user_id):
    """특정 유저의 대기 중인 충전 요청 (가장 오래된 것 우선, 최대 5개)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM charge_requests WHERE user_id=? AND status=? ORDER BY id ASC LIMIT 5',
              (str(user_id), 'pending'))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_recent_charge_requests(limit=20):
    """최근 충전 요청 목록 (대시보드 진단용, 최신순)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM charge_requests ORDER BY id DESC LIMIT ?', (int(limit),))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_charge_requests_needing_reminder(minutes=3, expire_minutes=30, limit=20):
    """자동 충전 리마인더가 필요한 대기 충전 요청 조회.

    조건: 아직 pending + 리마인더 미발송 + 신청 후 {minutes}분 경과 + 유효 시간({expire_minutes}분) 이내
    (created_at/completed_at은 SQLite CURRENT_TIMESTAMP = UTC 기준)
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        f"""SELECT * FROM charge_requests
            WHERE status='pending' AND reminder_sent=0
              AND datetime(created_at, '+{int(minutes)} minutes') <= datetime('now')
              AND created_at >= datetime('now', '-{int(expire_minutes)} minutes')
            ORDER BY id ASC LIMIT {int(limit)}"""
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def mark_charge_reminder_sent(rid):
    """충전 요청에 리마인더 DM 발송 완료 표시 (중복 발송 방지)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE charge_requests SET reminder_sent=1 WHERE id=?', (int(rid),))
    conn.commit()
    conn.close()

def get_user_charge_requests(user_id, limit=10):
    """Get charge requests for a user"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM charge_requests WHERE user_id=? ORDER BY id DESC LIMIT ?', (str(user_id), limit))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ============ ADMINS ============
def add_admin(user_id, username):
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO admins (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def remove_admin(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM admins WHERE user_id=?', (user_id,))
    conn.commit()
    conn.close()

def get_admins():
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM admins')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def is_admin(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM admins WHERE user_id=?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

# ============ TICKETS (INQUIRY SYSTEM) ============
def create_ticket(user_id, username, channel_id):
    """Create a new ticket - only 1 open ticket per user"""
    # Check if user already has an open ticket
    existing = get_open_ticket_by_user(user_id)
    if existing:
        return -1
    
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT INTO tickets (user_id, username, channel_id) VALUES (?, ?, ?)',
              (str(user_id), username, str(channel_id)))
    conn.commit()
    tid = c.lastrowid
    conn.close()
    return tid

def get_ticket(tid):
    """Get a ticket by id"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM tickets WHERE id=?', (tid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_ticket_by_channel(channel_id):
    """Get a ticket by channel id"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM tickets WHERE channel_id=?', (str(channel_id),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_open_ticket_by_user(user_id):
    """Get open ticket for a user"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM tickets WHERE user_id=? AND status=?', (str(user_id), 'open'))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_open_tickets():
    """Get all open tickets"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM tickets WHERE status=? ORDER BY id', ('open',))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def close_ticket(tid):
    """Close a ticket"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE tickets SET status=?, closed_at=CURRENT_TIMESTAMP WHERE id=?', ('closed', tid))
    conn.commit()
    conn.close()

# ============ COUPONS (할인 쿠폰) ============
def create_coupon(code, discount_amount, max_uses=0, discount_type='amount'):
    """쿠폰 생성 - 코드 중복 시 -1 반환

    discount_type: 'amount' = 정액(원), 'percent' = 정률(%)
    """
    conn = get_conn()
    c = conn.cursor()
    dtype = 'percent' if str(discount_type).lower() == 'percent' else 'amount'
    try:
        c.execute('INSERT INTO coupons (code, discount_amount, max_uses, discount_type) VALUES (?, ?, ?, ?)',
                  (str(code).strip().upper(), int(discount_amount), int(max_uses), dtype))
        conn.commit()
        cid = c.lastrowid
    except sqlite3.IntegrityError:
        cid = -1  # 중복 코드
    conn.close()
    return cid

def get_coupons(active_only=False):
    """쿠폰 목록 조회"""
    conn = get_conn()
    c = conn.cursor()
    if active_only:
        c.execute('SELECT * FROM coupons WHERE active=1 ORDER BY id DESC')
    else:
        c.execute('SELECT * FROM coupons ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_coupon(cid):
    """쿠폰 단건 조회"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM coupons WHERE id=?', (cid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_coupon_by_code(code):
    """코드로 쿠폰 조회 (유효성 검사 포함: 활성화 + 사용횟수)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM coupons WHERE code=?', (str(code).strip().upper(),))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    coupon = dict(row)
    if not coupon['active']:
        return None
    if coupon['max_uses'] > 0 and coupon['used_count'] >= coupon['max_uses']:
        return None
    return coupon

def coupon_discount_amount(coupon, price):
    """쿠폰으로 할인될 금액 계산 (가격보다 클 수 없음)"""
    price = max(0, int(price))
    if not coupon:
        return 0
    try:
        value = int(coupon.get('discount_amount') or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return 0
    if str(coupon.get('discount_type') or 'amount').lower() == 'percent':
        value = min(value, 100)
        return price * value // 100
    return min(price, value)

def coupon_final_price(coupon, price):
    """쿠폰 적용 후 최종 결제 금액 (0원 가능)"""
    return max(0, max(0, int(price)) - coupon_discount_amount(coupon, price))

def use_coupon(cid, user_id=None, order_id=None, discount=0):
    """쿠폰 사용 처리 (원자적, 사용 횟수 +1)

    max_uses 제한을 SQL 조건으로 검사하므로 동시 사용에도 초과 사용되지 않는다.
    Returns: 사용 성공 여부
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute('''UPDATE coupons SET used_count = used_count + 1
                 WHERE id=? AND active=1 AND (max_uses=0 OR used_count < max_uses)''', (cid,))
    ok = c.rowcount > 0
    if ok:
        try:
            c.execute('SELECT code FROM coupons WHERE id=?', (cid,))
            row = c.fetchone()
            code = row['code'] if row else ''
            c.execute('''INSERT INTO coupon_uses (coupon_id, code, user_id, order_id, discount)
                         VALUES (?, ?, ?, ?, ?)''',
                      (cid, code, str(user_id or ''), order_id, int(discount or 0)))
        except Exception:
            pass
    conn.commit()
    conn.close()
    return ok

def get_coupon_uses(limit=50):
    """쿠폰 사용 기록 (최신순)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM coupon_uses ORDER BY id DESC LIMIT ?', (int(limit),))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_coupon(cid):
    """쿠폰 삭제"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM coupons WHERE id=?', (cid,))
    conn.commit()
    conn.close()

def toggle_coupon(cid):
    """쿠폰 활성화/비활성화 토글"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE coupons SET active = CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?', (cid,))
    conn.commit()
    conn.close()


# ============ SALE (전체 할인 이벤트) ============
def get_sale():
    """현재 진행 중인 전체 할인 설정

    Returns:
        {'active': bool, 'type': 'percent'|'amount', 'value': int,
         'label': str, 'ends_at': str}
    """
    active_raw = str(get_setting('sale_active', 'false') or '').strip().lower()
    active = active_raw in ('1', 'true', 'yes', 'on', 'y')

    sale_type = str(get_setting('sale_type', 'percent') or 'percent').strip().lower()
    if sale_type not in ('percent', 'amount'):
        sale_type = 'percent'

    try:
        value = int(float(str(get_setting('sale_value', '0') or '0').strip() or 0))
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    if sale_type == 'percent':
        value = min(value, 100)

    label = get_setting('sale_label', '') or ''
    ends_at = str(get_setting('sale_ends_at', '') or '').strip()

    # 종료 시각이 지났으면 자동 종료
    if active and ends_at:
        try:
            from datetime import datetime as _dt
            end = None
            for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M'):
                try:
                    end = _dt.strptime(ends_at, fmt)
                    break
                except ValueError:
                    continue
            if end and _dt.now() > end:
                active = False
        except Exception:
            pass

    return {
        'active': bool(active and value > 0),
        'type': sale_type,
        'value': value,
        'label': label,
        'ends_at': ends_at,
    }

def set_sale(active=True, sale_type='percent', value=0, label='', ends_at=''):
    """전체 할인 설정 저장"""
    set_setting('sale_active', 'true' if active else 'false')
    set_setting('sale_type', 'percent' if str(sale_type).lower() == 'percent' else 'amount')
    set_setting('sale_value', str(int(value)))
    set_setting('sale_label', str(label or ''))
    set_setting('sale_ends_at', str(ends_at or ''))
    return get_sale()

def stop_sale():
    """전체 할인 종료"""
    set_setting('sale_active', 'false')
    return get_sale()

def apply_sale(price):
    """전체 할인 적용 → (할인 적용 가격, 할인 금액)"""
    price = max(0, int(price))
    sale = get_sale()
    if not sale['active']:
        return price, 0
    if sale['type'] == 'percent':
        discount = price * sale['value'] // 100
    else:
        discount = min(price, sale['value'])
    return max(0, price - discount), discount

def sale_summary():
    """할인 안내 문구 (할인 중이 아니면 빈 문자열)"""
    sale = get_sale()
    if not sale['active']:
        return ''
    unit = '%' if sale['type'] == 'percent' else '원'
    if sale['label']:
        text = f'🔥 {sale["label"]} ({sale["value"]}{unit} 할인)'
    else:
        text = f'🔥 전체 {sale["value"]}{unit} 할인 진행중'
    if sale['ends_at']:
        text += f' · ~{sale["ends_at"]}'
    return text


def sale_label_text():
    """할인 짧은 라벨 (할인 중이 아니면 빈 문자열)"""
    sale = get_sale()
    if not sale['active']:
        return ''
    unit = '%' if sale['type'] == 'percent' else '원'
    return str(sale['label'] or f"{sale['value']}{unit} 할인")


# ============ STOCK LOGS (입고 기록) ============
def log_stock(product_id, product_name, count, total_stock=0, source='dashboard', actor=''):
    """입고 기록 저장 (대시보드/명령어 공통)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''INSERT INTO stock_logs (product_id, product_name, count, total_stock, source, actor)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (product_id, str(product_name)[:100], int(count), int(total_stock),
               str(source)[:20], str(actor)[:100]))
    conn.commit()
    lid = c.lastrowid
    conn.close()
    return lid

def restock_product(product_id, count, source='dashboard', actor=''):
    """상품 입고: 재고 증가 + 입고 로그 기록

    Returns:
        {'product': dict, 'count': int, 'total_stock': int} 또는 None
    """
    count = int(count)
    if count <= 0:
        return None
    product = get_product(product_id)
    if not product:
        return None
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE products SET stock = stock + ? WHERE id=?', (count, product_id))
    conn.commit()
    conn.close()

    product = get_product(product_id)
    log_stock(product_id, product['name'], count, product['stock'], source=source, actor=actor)
    return {'product': product, 'count': count, 'total_stock': product['stock']}

def get_stock_logs(limit=50):
    """입고 기록 조회 (최신순)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM stock_logs ORDER BY id DESC LIMIT ?', (int(limit),))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_low_stock_products(threshold=3, active_only=True):
    """재고가 threshold 이하인 상품 목록 (재고 부족 알림용)"""
    conn = get_conn()
    c = conn.cursor()
    if active_only:
        c.execute('SELECT * FROM products WHERE active=1 AND stock <= ? ORDER BY stock ASC', (int(threshold),))
    else:
        c.execute('SELECT * FROM products WHERE stock <= ? ORDER BY stock ASC', (int(threshold),))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============ AUDIT LOGS (관리자 행동 기록) ============
def add_audit_log(action, actor_id='', actor_name='', detail=''):
    """관리자 행동(입고/할인/포인트 지급 등) 기록"""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''INSERT INTO audit_logs (action, actor_id, actor_name, detail)
                     VALUES (?, ?, ?, ?)''',
                  (str(action)[:60], str(actor_id)[:40], str(actor_name)[:100], str(detail)[:1000]))
        conn.commit()
    except Exception:
        pass
    conn.close()

def get_audit_logs(limit=50):
    """감사 로그 조회 (최신순)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?', (int(limit),))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============ DM NOTICE TARGETS (전체 공지) ============
def get_notice_targets():
    """DM 공지 발송 대상 (등록된 유저 ID 목록)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT user_id, username FROM users ORDER BY rowid')
    rows = c.fetchall()
    conn.close()
    return [{'user_id': str(r['user_id']), 'username': r['username']} for r in rows]

def count_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) AS cnt FROM users')
    row = c.fetchone()
    conn.close()
    return row['cnt'] if row else 0


# ============ MAINTENANCE (오래된 데이터 정리) ============
def purge_old_data(days=90):
    """오래된 로그/완료된 충전 요청 정리 (DB가 계속 커지는 것 방지)

    Returns: 삭제된 행 수
    """
    days = max(1, int(days))
    removed = 0
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM stock_logs WHERE created_at < datetime('now', ?)", (f'-{days} days',))
        removed += c.rowcount or 0
        c.execute("DELETE FROM audit_logs WHERE created_at < datetime('now', ?)", (f'-{days} days',))
        removed += c.rowcount or 0
        c.execute("DELETE FROM coupon_uses WHERE created_at < datetime('now', ?)", (f'-{days} days',))
        removed += c.rowcount or 0
        c.execute("DELETE FROM charge_requests WHERE status!='pending' AND created_at < datetime('now', ?)",
                  (f'-{days} days',))
        removed += c.rowcount or 0
        c.execute("DELETE FROM transactions WHERE created_at < datetime('now', ?)", (f'-{days * 3} days',))
        removed += c.rowcount or 0
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()
    return removed

