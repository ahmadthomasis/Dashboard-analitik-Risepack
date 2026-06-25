from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import mysql.connector
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'risepack-dashboard-secret-2025')

# ─── Database Connection ───────────────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(
        host=os.getenv('DB_HOST'),
        port=int(os.getenv('DB_PORT', 3306)),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        connection_timeout=30
    )

def query(sql, params=None):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(sql, params or ())
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

# ─── Auth ──────────────────────────────────────────────────────────────────────
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

# ─── Pages ─────────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    return render_template('dashboard.html')

# ─── API: Filter Options ───────────────────────────────────────────────────────
@app.route('/api/filters')
@login_required
def api_filters():
    pics     = query("SELECT DISTINCT PIC FROM view_salesorder WHERE PIC IS NOT NULL ORDER BY PIC")
    divs     = query("SELECT DISTINCT sub_division FROM view_salesorder WHERE sub_division IS NOT NULL ORDER BY sub_division")
    return jsonify({
        'pics': [r['PIC'] for r in pics],
        'sub_divisions': [r['sub_division'] for r in divs]
    })

# ─── API: KPI Cards ────────────────────────────────────────────────────────────
@app.route('/api/kpi')
@login_required
def api_kpi():
    bulan  = request.args.get('bulan')   # format: YYYY-MM
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')

    where, params = build_where(bulan, pic, divisi)

    sql = f"""
        SELECT
            COUNT(DISTINCT order_key)                                        AS total_order,
            SUM(CASE WHEN status_deal='Deal' THEN total_harga ELSE 0 END)    AS total_omzet,
            SUM(CASE WHEN status_deal='Deal' THEN modal_sales ELSE 0 END)    AS total_modal,
            SUM(CASE WHEN status_deal='Deal' THEN (total_harga - modal_sales) ELSE 0 END) AS total_margin,
            COUNT(DISTINCT CASE WHEN sumber='Repeat Order' THEN order_key END) AS total_repeat,
            COUNT(DISTINCT CASE WHEN sumber != 'Repeat Order' THEN order_key END) AS total_new,
            COUNT(DISTINCT CASE WHEN status_deal='Deal' THEN order_key END)  AS total_deal
        FROM view_salesorder
        {where}
    """
    row = query(sql, params)[0]

    omzet  = row['total_omzet']  or 0
    modal  = row['total_modal']  or 0
    margin = row['total_margin'] or 0
    deal   = row['total_deal']   or 0
    order  = row['total_order']  or 1

    return jsonify({
        'total_omzet':      omzet,
        'total_modal':      modal,
        'total_margin':     margin,
        'persen_margin':    round(margin / omzet * 100, 1) if omzet else 0,
        'total_order':      row['total_order'],
        'total_deal':       deal,
        'total_repeat':     row['total_repeat'] or 0,
        'total_new':        row['total_new'] or 0,
        'closing_rate':     round(deal / order * 100, 1) if order else 0,
        'persen_repeat':    round((row['total_repeat'] or 0) / deal * 100, 1) if deal else 0,
    })

# ─── API: Trend Omzet Bulanan ──────────────────────────────────────────────────
@app.route('/api/trend-omzet')
@login_required
def api_trend_omzet():
    tahun  = request.args.get('tahun', str(datetime.now().year))
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')

    extra = []
    params = [tahun]
    if pic:    extra.append("AND PIC = %s");         params.append(pic)
    if divisi: extra.append("AND sub_division = %s"); params.append(divisi)

    sql = f"""
        SELECT
            DATE_FORMAT(tgl_omzet_realtime, '%Y-%m') AS bulan,
            kategori_produksi,
            SUM(total_harga) AS omzet
        FROM view_salesorder
        WHERE status_deal = 'Deal'
          AND YEAR(tgl_omzet_realtime) = %s
          {' '.join(extra)}
        GROUP BY bulan, kategori_produksi
        ORDER BY bulan
    """
    rows = query(sql, params)
    return jsonify(rows)

# ─── API: Top Sales Rep ────────────────────────────────────────────────────────
@app.route('/api/top-sales')
@login_required
def api_top_sales():
    bulan  = request.args.get('bulan')
    divisi = request.args.get('divisi')
    where, params = build_where(bulan, None, divisi)

    sql = f"""
        SELECT
            PIC,
            COUNT(DISTINCT order_key)  AS total_order,
            SUM(total_harga)           AS total_omzet,
            SUM(total_harga - modal_sales) AS total_margin
        FROM view_salesorder
        WHERE status_deal = 'Deal' AND PIC IS NOT NULL
        {where.replace('WHERE','AND') if where else ''}
        GROUP BY PIC
        ORDER BY total_omzet DESC
        LIMIT 10
    """
    return jsonify(query(sql, params))

# ─── API: Sales by Sumber ──────────────────────────────────────────────────────
@app.route('/api/sales-by-sumber')
@login_required
def api_sales_by_sumber():
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    where, params = build_where(bulan, pic, divisi)

    sql = f"""
        SELECT sumber, COUNT(DISTINCT order_key) AS total, SUM(total_harga) AS omzet
        FROM view_salesorder
        WHERE status_deal = 'Deal'
        {where.replace('WHERE','AND') if where else ''}
        GROUP BY sumber ORDER BY omzet DESC
    """
    return jsonify(query(sql, params))

# ─── API: Status Produksi ──────────────────────────────────────────────────────
@app.route('/api/produksi')
@login_required
def api_produksi():
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    where, params = build_where(bulan, pic, divisi)

    sql = f"""
        SELECT
            COUNT(CASE WHEN status_produksi='Berjalan'  THEN 1 END) AS berjalan,
            COUNT(CASE WHEN status_produksi='Tuntas'    THEN 1 END) AS tuntas,
            COUNT(CASE WHEN status_produksi='Belum SPK' THEN 1 END) AS belum_spk
        FROM view_salesorder
        WHERE status_deal = 'Deal'
        {where.replace('WHERE','AND') if where else ''}
    """
    return jsonify(query(sql, params)[0])

# ─── API: Kategori Produk ──────────────────────────────────────────────────────
@app.route('/api/kategori')
@login_required
def api_kategori():
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    where, params = build_where(bulan, pic, divisi)

    sql = f"""
        SELECT kategori_produksi, SUM(total_harga) AS omzet, COUNT(DISTINCT order_key) AS total
        FROM view_salesorder
        WHERE status_deal = 'Deal' AND kategori_produksi IS NOT NULL
        {where.replace('WHERE','AND') if where else ''}
        GROUP BY kategori_produksi ORDER BY omzet DESC
    """
    return jsonify(query(sql, params))

# ─── Helper ────────────────────────────────────────────────────────────────────
def build_where(bulan, pic, divisi):
    clauses, params = [], []
    if bulan:
        clauses.append("DATE_FORMAT(tgl_omzet_realtime,'%Y-%m') = %s")
        params.append(bulan)
    if pic:
        clauses.append("PIC = %s")
        params.append(pic)
    if divisi:
        clauses.append("sub_division = %s")
        params.append(divisi)
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    return where, params

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
