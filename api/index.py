"""
Archive Manila — Vercel + Turso persistent database.

ROOT CAUSE OF THE BUG:
  Vercel serverless = many isolated instances, each with their own empty /tmp.
  Order saved on instance A is invisible to instance B, C, D...

THE FIX: Turso (free cloud SQLite, persistent across ALL instances/devices).

SETUP (2 minutes):
  1. Sign up free at https://turso.tech
  2. Create a database named "archive-manila"
  3. Run in Turso CLI:  turso db tokens create archive-manila
  4. Add to Vercel Environment Variables:
       TURSO_URL   = libsql://archive-manila-YOURNAME.turso.io
       TURSO_TOKEN = your-token-here
       SECRET_KEY  = any-random-string
  5. Redeploy. Done — all devices share one real database.
"""

import os, sqlite3, uuid, json, time, base64, threading
from datetime import datetime
from functools import wraps
from urllib.request import urlopen, Request
from urllib.error import URLError

from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session, g)
from werkzeug.security import generate_password_hash, check_password_hash

# ── Config ─────────────────────────────────────────────────────────────────────
HERE     = os.path.dirname(os.path.abspath(__file__))
ROOT     = os.path.dirname(HERE)
TMPL_DIR = os.path.join(ROOT, 'templates')
STAT_DIR = os.path.join(ROOT, 'static')

IS_VERCEL   = bool(os.environ.get('VERCEL'))
TURSO_URL   = os.environ.get('TURSO_URL', '').rstrip('/')
TURSO_TOKEN = os.environ.get('TURSO_TOKEN', '')
USE_TURSO   = bool(TURSO_URL and TURSO_TOKEN)

LOCAL_DB_PATH = '/tmp/archive.db' if IS_VERCEL else os.path.join(ROOT, 'instance', 'archive.db')

app = Flask(__name__, template_folder=TMPL_DIR, static_folder=STAT_DIR)
app.secret_key = os.environ.get('SECRET_KEY', 'archive-manila-secret-2024')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# ══════════════════════════════════════════════════════════════════════════════
#  TURSO HTTP DATABASE LAYER
#  Uses Turso's HTTP pipeline API — no extra packages needed, just urllib.
# ══════════════════════════════════════════════════════════════════════════════

def _turso_execute(statements):
    """
    Send a list of {"sql": ..., "args": [...]} to Turso HTTP pipeline.
    Returns list of raw result objects.
    """
    url = TURSO_URL.replace('libsql://', 'https://') + '/v2/pipeline'
    requests_payload = []
    for s in statements:
        args = []
        for v in (s.get('args') or []):
            if v is None:
                args.append({'type': 'null'})
            elif isinstance(v, (int, float)):
                args.append({'type': 'integer' if isinstance(v, int) else 'float', 'value': str(v)})
            else:
                args.append({'type': 'text', 'value': str(v)})
        requests_payload.append({
            'type': 'execute',
            'stmt': {'sql': s['sql'], 'args': args}
        })
    requests_payload.append({'type': 'close'})

    body = json.dumps({'requests': requests_payload}).encode()
    req  = Request(url, data=body, headers={
        'Authorization': f'Bearer {TURSO_TOKEN}',
        'Content-Type':  'application/json',
    })
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())['results']


def _parse_rows(result):
    """Convert a Turso result into a list of dict-like rows."""
    if result.get('type') == 'error':
        msg = result.get('error', {}).get('message', 'Unknown Turso error')
        raise RuntimeError(f'Turso error: {msg}')
    rs   = result.get('response', {}).get('result', {})
    cols = [c['name'] for c in rs.get('cols', [])]
    rows = []
    for r in rs.get('rows', []):
        vals = []
        for v in r:
            t = v.get('type')
            if t == 'null':
                vals.append(None)
            elif t in ('integer',):
                vals.append(int(v['value']))
            elif t == 'float':
                vals.append(float(v['value']))
            else:
                vals.append(v.get('value'))
        rows.append(dict(zip(cols, vals)))
    return rows


class TursoCursor:
    """Mimics sqlite3 cursor/connection for use in Flask route handlers."""
    def __init__(self):
        self._rows    = []
        self.lastrowid = None

    def execute(self, sql, params=None):
        results = _turso_execute([{'sql': sql, 'args': list(params or [])}])
        self._rows = _parse_rows(results[0])
        rs = results[0].get('response', {}).get('result', {})
        self.lastrowid = rs.get('last_insert_rowid')
        return self

    def executemany(self, sql, seq_of_params):
        stmts = [{'sql': sql, 'args': list(p)} for p in seq_of_params]
        if stmts:
            _turso_execute(stmts)
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def commit(self):
        pass  # Turso auto-commits every statement

    def close(self):
        pass

    # Allow dict-style access for COUNT(*) results: db.execute(...).fetchone()[0]
    def __getitem__(self, key):
        row = self.fetchone()
        if row is None:
            return None
        if isinstance(key, int):
            return list(row.values())[key]
        return row[key]

    def keys(self):
        row = self.fetchone()
        return list(row.keys()) if row else []


# ── get_db: returns TursoCursor (cloud) or sqlite3 connection (local) ──────────
def get_db():
    if 'db' not in g:
        if USE_TURSO:
            g.db = TursoCursor()
        else:
            if not IS_VERCEL:
                os.makedirs(os.path.dirname(LOCAL_DB_PATH), exist_ok=True)
            conn = sqlite3.connect(LOCAL_DB_PATH)
            conn.row_factory = sqlite3.Row
            g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db and not USE_TURSO:
        try: db.close()
        except: pass


# ── Schema setup (runs once per cold start) ────────────────────────────────────
_schema_done = False
_schema_lock = threading.Lock()

TABLES = [
    '''CREATE TABLE IF NOT EXISTS products (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        name              TEXT    NOT NULL,
        price             REAL    NOT NULL,
        stock             INTEGER NOT NULL DEFAULT 0,
        description       TEXT,
        short_description TEXT,
        category          TEXT    DEFAULT 'Uncategorized',
        image             TEXT,
        image_back        TEXT,
        created_at        TEXT    DEFAULT (datetime('now'))
    )''',
    '''CREATE TABLE IF NOT EXISTS orders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number    TEXT UNIQUE NOT NULL,
        buyer_name      TEXT NOT NULL,
        buyer_phone     TEXT NOT NULL,
        buyer_address   TEXT NOT NULL,
        buyer_notes     TEXT,
        shipping_option TEXT NOT NULL,
        shipping_fee    REAL DEFAULT 0,
        subtotal        REAL NOT NULL,
        total           REAL NOT NULL,
        payment_proof   TEXT,
        status          TEXT DEFAULT 'Pending Verification',
        created_at      TEXT DEFAULT (datetime('now')),
        updated_at      TEXT DEFAULT (datetime('now'))
    )''',
    '''CREATE TABLE IF NOT EXISTS order_items (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id      INTEGER NOT NULL,
        product_id    INTEGER,
        product_name  TEXT    NOT NULL,
        product_price REAL    NOT NULL,
        quantity      INTEGER NOT NULL,
        image         TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS order_status_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id   INTEGER NOT NULL,
        status     TEXT    NOT NULL,
        note       TEXT,
        changed_at TEXT    DEFAULT (datetime('now'))
    )''',
    '''CREATE TABLE IF NOT EXISTS admin_users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )''',
]

SAMPLES = [
    ('Long Sleeve',      150, 10, 'Classic long sleeve shirt perfect for layering.',           'Essential wardrobe piece', 'Tops'),
    ('Thrift Store Tee', 300,  8, 'Vintage graphic tee from thrift collections.',             'Rare vintage find',        'Tops'),
    ('Ripped Jeans',     265,  5, 'Distressed denim jeans with raw hem details.',             'Street style staple',      'Bottoms'),
    ('Red Hoodie',       300, 12, 'A cozy zip-up hoodie with kangaroo pocket.',              'Cozy zip-up hoodie',       'Hoodies'),
    ('Purple Hoodie',    540,  7, 'Bold purple hoodie, oversized fit.',                       'Oversized streetwear',     'Hoodies'),
    ('GEAR Hoodie',      700,  4, 'Limited streetwear graphic hoodie by GEAR brand.',         'Limited edition drop',     'Hoodies'),
    ('LA Cap',           450, 15, 'Structured snapback cap with LA embroidery.',             'Streetwear essential',     'Accessories'),
    ('Nike Shorts',      350,  9, 'Authentic Nike athletic shorts, breathable mesh lining.', 'Athletic & street',        'Bottoms'),
]

def _setup_schema():
    if USE_TURSO:
        db = TursoCursor()
        for sql in TABLES:
            db.execute(sql)
        # Admin user
        h = generate_password_hash('admin123')
        db.execute('INSERT OR IGNORE INTO admin_users (username, password_hash) VALUES (?,?)', ('admin', h))
        db.execute('UPDATE admin_users SET password_hash=? WHERE username=?', (h, 'admin'))
        # Seed if empty
        result = db.execute('SELECT COUNT(*) as cnt FROM products').fetchone()
        if not result or int(result.get('cnt', 0) or 0) == 0:
            db.executemany(
                'INSERT INTO products (name,price,stock,description,short_description,category) VALUES (?,?,?,?,?,?)',
                SAMPLES)
    else:
        if not IS_VERCEL:
            os.makedirs(os.path.dirname(LOCAL_DB_PATH), exist_ok=True)
        db = sqlite3.connect(LOCAL_DB_PATH)
        db.row_factory = sqlite3.Row
        for sql in TABLES:
            db.execute(sql)
        # Migration for existing local DBs
        try:
            db.execute('ALTER TABLE products ADD COLUMN image_back TEXT')
        except Exception:
            pass
        h = generate_password_hash('admin123')
        db.execute('INSERT OR IGNORE INTO admin_users (username, password_hash) VALUES (?,?)', ('admin', h))
        db.execute('UPDATE admin_users SET password_hash=? WHERE username=?', (h, 'admin'))
        cnt = db.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        if not cnt:
            db.executemany(
                'INSERT INTO products (name,price,stock,description,short_description,category) VALUES (?,?,?,?,?,?)',
                SAMPLES)
        db.commit()
        db.close()

def ensure_schema():
    global _schema_done
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        _setup_schema()
        _schema_done = True

@app.before_request
def before_request():
    ensure_schema()


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def row_to_dict(row):
    if row is None: return None
    if isinstance(row, dict): return dict(row)
    return dict(zip(row.keys(), tuple(row)))

@app.template_filter('fmt_dt')
def fmt_dt(value, fmt='%b %d, %Y %I:%M %p'):
    if value is None: return ''
    if isinstance(value, str):
        for f in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
            try: value = datetime.strptime(value, f); break
            except ValueError: continue
        else: return str(value)[:16]
    return value.strftime(fmt)

@app.template_filter('fmt_date')
def fmt_date(v): return fmt_dt(v, '%b %d, %Y')

def img_src(value):
    if not value: return None
    if value.startswith('data:'): return value
    return '/static/uploads/' + value

app.jinja_env.globals['img_src'] = img_src

def save_image(file_obj):
    if not file_obj or not file_obj.filename: return None
    ext = file_obj.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_EXT: return None
    mime = 'image/jpeg' if ext in ('jpg','jpeg') else f'image/{ext}'
    raw = file_obj.read()
    return f'data:{mime};base64,{base64.b64encode(raw).decode()}' if raw else None

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if not session.get('admin_logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error':'Not authenticated','redirect':'/admin/login'}), 401
            return redirect(url_for('admin_login'))
        return f(*a, **kw)
    return dec

def gen_order_number():
    return 'AM-' + datetime.now().strftime('%Y%m%d') + '-' + str(uuid.uuid4())[:6].upper()

_notif, _nlock = [], threading.Lock()
def push_event(data):
    with _nlock:
        _notif.append(data)
        if len(_notif) > 100: _notif[:] = _notif[-50:]


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    db = get_db()
    products   = db.execute('SELECT * FROM products ORDER BY created_at DESC').fetchall()
    categories = db.execute('SELECT DISTINCT category FROM products').fetchall()
    cats = [c['category'] for c in categories] if categories else []
    return render_template('index.html', products=products, categories=cats)

@app.route('/product/<int:pid>')
def product_detail(pid):
    p = get_db().execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not p: return redirect(url_for('index'))
    return render_template('product_detail.html', product=p)

@app.route('/checkout')
def checkout(): return render_template('checkout.html')

@app.route('/my-orders')
def my_orders(): return render_template('my_orders.html')

@app.route('/order-confirmation/<order_number>')
def order_confirmation(order_number):
    db    = get_db()
    order = db.execute('SELECT * FROM orders WHERE order_number=?', (order_number,)).fetchone()
    if not order: return redirect(url_for('index'))
    items = db.execute('SELECT * FROM order_items WHERE order_id=?', (order['id'],)).fetchall()
    return render_template('order_confirmation.html', order=order, items=items)

@app.route('/track-order')
def track_order(): return render_template('track_order.html')


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/products')
def api_products():
    db  = get_db()
    cat = request.args.get('category')
    if cat:
        rows = db.execute('SELECT * FROM products WHERE category=? ORDER BY created_at DESC', (cat,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM products ORDER BY created_at DESC').fetchall()
    return jsonify([row_to_dict(r) for r in rows])

@app.route('/api/products/<int:pid>')
def api_product(pid):
    row = get_db().execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not row: return jsonify({'error':'Not found'}), 404
    return jsonify(row_to_dict(row))

@app.route('/api/checkout', methods=['POST'])
def api_checkout():
    data = request.form
    for field in ['buyer_name','buyer_phone','buyer_address']:
        if not data.get(field,'').strip():
            return jsonify({'error': f'{field} is required'}), 400
    try:
        items = json.loads(data.get('cart_items','[]'))
    except Exception:
        return jsonify({'error':'Invalid cart data'}), 400
    if not items:
        return jsonify({'error':'Cart is empty'}), 400
    proof = save_image(request.files.get('payment_proof'))
    if not proof:
        return jsonify({'error':'Payment proof required (JPG/PNG/GIF)'}), 400

    subtotal     = sum(i['price'] * i['quantity'] for i in items)
    order_number = gen_order_number()
    db           = get_db()

    cur = db.execute(
        '''INSERT INTO orders
           (order_number,buyer_name,buyer_phone,buyer_address,buyer_notes,
            shipping_option,shipping_fee,subtotal,total,payment_proof,status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (order_number, data['buyer_name'].strip(), data['buyer_phone'].strip(),
         data['buyer_address'].strip(), data.get('buyer_notes','').strip(),
         'standard', 0, subtotal, subtotal, proof, 'Pending Verification'))

    oid = cur.lastrowid
    for item in items:
        db.execute(
            'INSERT INTO order_items (order_id,product_id,product_name,product_price,quantity,image) VALUES (?,?,?,?,?,?)',
            (oid, item.get('id'), item['name'], item['price'], item['quantity'], item.get('image')))
        if item.get('id'):
            db.execute('UPDATE products SET stock=MAX(0,stock-?) WHERE id=?',
                       (item['quantity'], item['id']))
    db.execute('INSERT INTO order_status_history (order_id,status,note) VALUES (?,?,?)',
               (oid,'Pending Verification','Order submitted by buyer'))
    db.commit()
    push_event({'type':'new_order','order_number':order_number,
                'buyer_name':data['buyer_name'].strip(),'total':subtotal,
                'timestamp':datetime.now().isoformat()})
    return jsonify({'success':True,'order_number':order_number})

@app.route('/api/track-order', methods=['POST'])
def api_track_order():
    body         = request.json or {}
    order_number = body.get('order_number','').strip().upper()
    phone        = body.get('phone','').strip()
    if not order_number or not phone:
        return jsonify({'error':'Order number and phone are required'}), 400
    db    = get_db()
    order = db.execute('SELECT * FROM orders WHERE order_number=? AND buyer_phone=?',
                       (order_number, phone)).fetchone()
    if not order:
        return jsonify({'error':'Order not found. Check your order number and phone.'}), 404
    items   = db.execute('SELECT * FROM order_items WHERE order_id=?', (order['id'],)).fetchall()
    history = db.execute('SELECT * FROM order_status_history WHERE order_id=? ORDER BY changed_at',
                         (order['id'],)).fetchall()
    return jsonify({'order':row_to_dict(order),
                    'items':[row_to_dict(i) for i in items],
                    'history':[row_to_dict(h) for h in history]})

@app.route('/api/my-orders', methods=['POST'])
def api_my_orders():
    phone = (request.json or {}).get('phone','').strip()
    if not phone:
        return jsonify({'error':'Phone number is required'}), 400
    db     = get_db()
    orders = db.execute('SELECT * FROM orders WHERE buyer_phone=? ORDER BY created_at DESC',
                        (phone,)).fetchall()
    result = []
    for o in orders:
        items = db.execute('SELECT * FROM order_items WHERE order_id=?', (o['id'],)).fetchall()
        od = row_to_dict(o)
        od['items'] = [row_to_dict(i) for i in items]
        result.append(od)
    return jsonify({'orders':result})


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        u     = request.form.get('username','')
        p     = request.form.get('password','')
        admin = get_db().execute('SELECT * FROM admin_users WHERE username=?', (u,)).fetchone()
        if admin and check_password_hash(admin['password_hash'], p):
            session['admin_logged_in'] = True
            session['admin_username']  = u
            return redirect(url_for('admin_dashboard'))
        return render_template('admin/login.html', error='Invalid credentials')
    return render_template('admin/login.html')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))

def _count(db, sql):
    row = db.execute(sql).fetchone()
    if row is None: return 0
    v = row[0] if not isinstance(row, dict) else list(row.values())[0]
    return int(v or 0)

@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    db = get_db()
    stats = {
        'total_orders':   _count(db, "SELECT COUNT(*) FROM orders"),
        'pending':        _count(db, "SELECT COUNT(*) FROM orders WHERE status='Pending Verification'"),
        'verified':       _count(db, "SELECT COUNT(*) FROM orders WHERE status='Verified'"),
        'shipped':        _count(db, "SELECT COUNT(*) FROM orders WHERE status='Shipped'"),
        'completed':      _count(db, "SELECT COUNT(*) FROM orders WHERE status='Completed'"),
        'total_revenue':  _count(db, "SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Rejected'"),
        'total_products': _count(db, "SELECT COUNT(*) FROM products"),
    }
    recent = db.execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT 10').fetchall()
    return render_template('admin/dashboard.html', stats=stats, recent_orders=recent)

@app.route('/admin/orders')
@admin_required
def admin_orders():
    db = get_db()
    sf = request.args.get('status','')
    if sf:
        orders = db.execute('SELECT * FROM orders WHERE status=? ORDER BY created_at DESC', (sf,)).fetchall()
    else:
        orders = db.execute('SELECT * FROM orders ORDER BY created_at DESC').fetchall()
    return render_template('admin/orders.html', orders=orders, status_filter=sf)

@app.route('/admin/orders/<int:oid>')
@admin_required
def admin_order_detail(oid):
    db    = get_db()
    order = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order: return redirect(url_for('admin_orders'))
    items   = db.execute('SELECT * FROM order_items WHERE order_id=?', (oid,)).fetchall()
    history = db.execute('SELECT * FROM order_status_history WHERE order_id=? ORDER BY changed_at', (oid,)).fetchall()
    return render_template('admin/order_detail.html', order=order, items=items, history=history)

@app.route('/admin/products')
@admin_required
def admin_products():
    products = get_db().execute('SELECT * FROM products ORDER BY created_at DESC').fetchall()
    return render_template('admin/products.html', products=products)

@app.route('/api/admin-poll-notifications')
@admin_required
def api_admin_poll():
    with _nlock:
        events = list(_notif)
        _notif.clear()
    return jsonify({'events':events,'timestamp':int(time.time()*1000)})

@app.route('/api/admin/orders/<int:oid>/status', methods=['POST'])
@admin_required
def api_update_order_status(oid):
    body   = request.json or {}
    status = body.get('status')
    note   = body.get('note','')
    valid  = ['Pending Verification','Verified','Rejected','Preparing','Shipped','Completed']
    if status not in valid:
        return jsonify({'error':'Invalid status'}), 400
    db    = get_db()
    order = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order: return jsonify({'error':'Order not found'}), 404
    db.execute("UPDATE orders SET status=?, updated_at=datetime('now') WHERE id=?", (status, oid))
    db.execute('INSERT INTO order_status_history (order_id,status,note) VALUES (?,?,?)',
               (oid, status, note or f'Status updated to {status} by admin'))
    db.commit()
    push_event({'type':'status_update','order_number':order['order_number'],
                'order_id':oid,'new_status':status,'timestamp':datetime.now().isoformat()})
    return jsonify({'success':True,'status':status})

@app.route('/api/admin/products', methods=['POST'])
@admin_required
def api_add_product():
    data = request.form
    for f in ['name','price','stock']:
        if not data.get(f,'').strip():
            return jsonify({'error':f'{f} is required'}), 400
    img      = save_image(request.files.get('image'))
    img_back = save_image(request.files.get('image_back'))
    get_db().execute(
        'INSERT INTO products (name,price,stock,description,short_description,category,image,image_back) VALUES (?,?,?,?,?,?,?,?)',
        (data['name'].strip(), float(data['price']), int(data['stock']),
         data.get('description',''), data.get('short_description',''),
         data.get('category','Uncategorized'), img, img_back))
    get_db().commit()
    return jsonify({'success':True})

@app.route('/api/admin/products/<int:pid>/edit', methods=['POST'])
@admin_required
def api_edit_product(pid):
    db = get_db()
    p  = db.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not p: return jsonify({'error':'Product not found'}), 404
    data     = request.form
    img      = save_image(request.files.get('image'))      or p['image']
    img_back = save_image(request.files.get('image_back')) or p['image_back']
    db.execute(
        'UPDATE products SET name=?,price=?,stock=?,description=?,short_description=?,category=?,image=?,image_back=? WHERE id=?',
        (data.get('name',p['name']), float(data.get('price',p['price'])),
         int(data.get('stock',p['stock'])), data.get('description',p['description'] or ''),
         data.get('short_description',p['short_description'] or ''),
         data.get('category',p['category']), img, img_back, pid))
    db.commit()
    return jsonify({'success':True})

@app.route('/api/admin/products/<int:pid>/delete', methods=['POST'])
@admin_required
def api_delete_product(pid):
    db = get_db()
    if not db.execute('SELECT id FROM products WHERE id=?', (pid,)).fetchone():
        return jsonify({'error':'Product not found'}), 404
    db.execute('DELETE FROM products WHERE id=?', (pid,))
    db.commit()
    return jsonify({'success':True})

# ── Local dev static file serving ─────────────────────────────────────────────
if not IS_VERCEL:
    from flask import send_from_directory
    _UPL = os.path.join(ROOT, 'static', 'uploads')
    os.makedirs(_UPL, exist_ok=True)

    @app.route('/static/uploads/<filename>')
    def uploaded_file(filename):
        return send_from_directory(_UPL, filename)

if __name__ == '__main__':
    ensure_schema()
    app.run(debug=True, port=5000)
