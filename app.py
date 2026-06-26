from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import mysql.connector
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'risepack-dashboard-secret-2025')

DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'database': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'connection_timeout': 60,
    'autocommit': True,
}

def query(sql, params=None):
    conn = mysql.connector.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params or ())
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        try: conn.close()
        except: pass

USERS = {
    os.getenv('MANAGER_EMAIL', 'manager@risepack.id'): os.getenv('MANAGER_PASSWORD', 'risepack2025')
}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if USERS.get(email) == password:
            session['user'] = email
            return redirect(url_for('index'))
        error = 'Email atau password salah.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('dashboard.html')

# ─── Helpers filter ──────────────────────────────────────────────
def build_where(tgl_dari, tgl_sampai, pic, divisi):
    """WHERE tambahan berbasis rentang tanggal (range), PIC, dan divisi."""
    clauses, params = [], []
    if tgl_dari:
        clauses.append("DATE(o.tgl_omzet_realtime) >= %s")
        params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("DATE(o.tgl_omzet_realtime) <= %s")
        params.append(tgl_sampai)
    if pic:
        clauses.append("o.name = %s")
        params.append(pic)
    if divisi:
        clauses.append("""o.order_key IN (
            SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s
        )""")
        params.append(divisi)
    cond = (' AND ' + ' AND '.join(clauses)) if clauses else ''
    return cond, params

def get_args():
    return (
        request.args.get('tgl_dari'),
        request.args.get('tgl_sampai'),
        request.args.get('pic'),
        request.args.get('divisi'),
    )

def prev_range(tgl_dari, tgl_sampai):
    """Periode pembanding: durasi sama persis tepat sebelum rentang terpilih."""
    if not tgl_dari or not tgl_sampai:
        return None, None
    try:
        d1 = datetime.strptime(tgl_dari, '%Y-%m-%d').date()
        d2 = datetime.strptime(tgl_sampai, '%Y-%m-%d').date()
    except ValueError:
        return None, None
    length = (d2 - d1).days + 1
    p2 = d1 - timedelta(days=1)
    p1 = p2 - timedelta(days=length - 1)
    return p1.isoformat(), p2.isoformat()

def pct(cur, prev):
    """Persentase perubahan cur vs prev. None bila tak bisa dihitung."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)

BASE = """
    FROM order_risepack o
    WHERE (o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)
"""

# ─── API ─────────────────────────────────────────────────────────
@app.route('/api/filters')
@login_required
def api_filters():
    pics = query("SELECT DISTINCT name AS PIC FROM order_risepack WHERE name IS NOT NULL AND name != '' ORDER BY name")
    divs = query("SELECT DISTINCT sub_division FROM tb_orders WHERE sub_division IS NOT NULL ORDER BY sub_division")
    return jsonify({
        'pics': [r['PIC'] for r in pics],
        'sub_divisions': [r['sub_division'] for r in divs]
    })

def kpi_metrics(cond, params):
    """Hitung seluruh metrik KPI untuk satu kondisi WHERE."""
    sql = f"""
        SELECT
            COUNT(DISTINCT o.order_key) AS total_order,
            COUNT(DISTINCT CASE WHEN o.status_deal='Deal' THEN o.order_key END) AS total_deal,
            SUM(CASE WHEN o.status_deal='Deal' THEN o.total_harga ELSE 0 END) AS total_omzet,
            SUM(CASE WHEN o.status_deal='Deal' THEN o.modal_sales ELSE 0 END) AS total_modal,
            SUM(CASE WHEN o.status_deal='Deal' THEN (o.total_harga-o.modal_sales) ELSE 0 END) AS total_margin,
            COUNT(DISTINCT CASE WHEN o.sumber='Repeat Order' AND o.status_deal='Deal' THEN o.order_key END) AS total_repeat,
            COUNT(DISTINCT CASE WHEN o.sumber!='Repeat Order' AND o.status_deal='Deal' THEN o.order_key END) AS total_new,
            COUNT(DISTINCT CASE WHEN o.sumber!='Repeat Order' THEN o.order_key END) AS new_order,
            SUM(CASE WHEN o.sumber='Repeat Order' AND o.status_deal='Deal' THEN o.total_harga ELSE 0 END) AS repeat_omzet
        {BASE} {cond}
    """
    r = query(sql, params)[0]
    omzet     = float(r['total_omzet']  or 0)
    modal     = float(r['total_modal']  or 0)
    margin    = float(r['total_margin'] or 0)
    deal      = int(r['total_deal']     or 0)
    order     = int(r['total_order']    or 0)
    repeat    = int(r['total_repeat']   or 0)
    new       = int(r['total_new']      or 0)
    new_order = int(r['new_order']      or 0)
    rep_omzet = float(r['repeat_omzet'] or 0)
    return {
        'total_omzet': omzet, 'total_modal': modal, 'total_margin': margin,
        'persen_margin': round(margin / omzet * 100, 1) if omzet else 0,
        'total_order': order, 'total_deal': deal,
        'total_repeat': repeat, 'total_new': new,
        'closing_rate': round(deal / order * 100, 1) if order else 0,
        'persen_repeat': round(repeat / deal * 100, 1) if deal else 0,
        'avg_purchase': round(omzet / deal) if deal else 0,
        'repeat_omzet': rep_omzet,
        'persen_repeat_omzet': round(rep_omzet / omzet * 100, 1) if omzet else 0,
        'closing_rate_new': round(new / new_order * 100, 1) if new_order else 0,
    }

@app.route('/api/kpi')
@login_required
def api_kpi():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    cur = kpi_metrics(cond, params)

    # Perbandingan periode sebelumnya (durasi sama)
    delta = {}
    p1, p2 = prev_range(tgl_dari, tgl_sampai)
    if p1 and p2:
        pcond, pparams = build_where(p1, p2, pic, divisi)
        prev = kpi_metrics(pcond, pparams)
        for k in ['total_omzet', 'total_modal', 'total_margin', 'total_order',
                  'total_deal', 'total_repeat', 'total_new', 'closing_rate',
                  'avg_purchase', 'repeat_omzet', 'closing_rate_new', 'persen_repeat']:
            delta[k] = pct(cur[k], prev[k])

    cur['delta'] = delta
    cur['prev_range'] = {'dari': p1, 'sampai': p2} if p1 else None
    return jsonify(cur)

@app.route('/api/trend-omzet')
@login_required
def api_trend_omzet():
    tahun  = request.args.get('tahun', str(datetime.now().year))
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(None, None, pic, divisi)

    sql = f"""
        SELECT DATE_FORMAT(o.tgl_omzet_realtime,'%Y-%m') AS bulan,
               o.kategori_produksi, SUM(o.total_harga) AS omzet
        {BASE}
        AND o.status_deal='Deal'
        AND YEAR(o.tgl_omzet_realtime) = %s
        {cond}
        GROUP BY bulan, o.kategori_produksi ORDER BY bulan
    """
    rows = query(sql, params + [tahun])
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

@app.route('/api/top-sales')
@login_required
def api_top_sales():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)

    sql = f"""
        SELECT o.name AS PIC,
               COUNT(DISTINCT o.order_key) AS total_order,
               SUM(o.total_harga) AS total_omzet,
               SUM(o.total_harga-o.modal_sales) AS total_margin
        {BASE}
        AND o.status_deal='Deal' AND o.name IS NOT NULL AND o.name!=''
        {cond}
        GROUP BY o.name ORDER BY total_omzet DESC LIMIT 10
    """
    rows = query(sql, params)
    return jsonify([{**r, 'total_omzet': float(r['total_omzet'] or 0), 'total_margin': float(r['total_margin'] or 0)} for r in rows])

@app.route('/api/sales-by-sumber')
@login_required
def api_sales_by_sumber():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)

    sql = f"""
        SELECT o.sumber, COUNT(DISTINCT o.order_key) AS total, SUM(o.total_harga) AS omzet
        {BASE}
        AND o.status_deal='Deal'
        {cond}
        GROUP BY o.sumber ORDER BY omzet DESC
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

@app.route('/api/produksi')
@login_required
def api_produksi():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)

    sql = f"""
        SELECT
            COUNT(CASE WHEN o.status_order='Berjalan' THEN 1 END) AS berjalan,
            COUNT(CASE WHEN o.status_order='Selesai Produksi' THEN 1 END) AS tuntas,
            COUNT(CASE WHEN o.status_order='Belum SPK' THEN 1 END) AS belum_spk
        {BASE}
        AND o.status_deal='Deal'
        {cond}
    """
    return jsonify(query(sql, params)[0])

@app.route('/api/kategori')
@login_required
def api_kategori():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)

    sql = f"""
        SELECT o.kategori_produksi, SUM(o.total_harga) AS omzet, COUNT(DISTINCT o.order_key) AS total
        {BASE}
        AND o.status_deal='Deal' AND o.kategori_produksi IS NOT NULL
        {cond}
        GROUP BY o.kategori_produksi ORDER BY omzet DESC
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
