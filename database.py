import sqlite3
import os
import json
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'vending.db')

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        confirmed_at TEXT DEFAULT NULL,
        FOREIGN KEY (product_id) REFERENCES products(id)
    )''')
    
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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT DEFAULT NULL
    )''')
    
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
        max_uses INTEGER DEFAULT 0,  -- 0 = 무제한
        used_count INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
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
    conn = get_conn()
    c = conn.cursor()
    for k in key_list:
        c.execute('INSERT INTO keys (product_id, key_value) VALUES (?, ?)', (product_id, k.strip()))
    conn.commit()
    conn.close()

def get_available_key(product_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM keys WHERE product_id=? AND used=0 ORDER BY id LIMIT 1', (product_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

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
def set_vending_message(channel_id, message_id):
    """Save vending machine message location"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vending_messages (
        channel_id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL
    )''')
    c.execute('INSERT OR REPLACE INTO vending_messages (channel_id, message_id) VALUES (?, ?)',
              (str(channel_id), str(message_id)))
    conn.commit()
    conn.close()

def get_vending_message(channel_id):
    """Get vending machine message for a channel"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vending_messages (
        channel_id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL
    )''')
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
        message_id TEXT NOT NULL
    )''')
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

def deduct_balance(user_id, amount, trans_type='purchase', description=''):
    """Deduct balance from user"""
    user = get_user(user_id)
    if not user or user['balance'] < amount:
        return False
    new_balance = user['balance'] - amount
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE users SET balance=? WHERE user_id=?', (new_balance, str(user_id)))
    c.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
              (str(user_id), -amount, trans_type, description))
    conn.commit()
    conn.close()
    return True

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
def create_coupon(code, discount_amount, max_uses=0):
    """쿠폰 생성 - 코드 중복 시 -1 반환"""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('INSERT INTO coupons (code, discount_amount, max_uses) VALUES (?, ?, ?)',
                  (code.strip().upper(), int(discount_amount), int(max_uses)))
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

def use_coupon(cid):
    """쿠폰 사용 처리 (사용 횟수 +1)"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE coupons SET used_count=used_count+1 WHERE id=?', (cid,))
    conn.commit()
    conn.close()

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
