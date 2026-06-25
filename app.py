from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import mysql.connector
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'risepack-dashboard-secret-2025')

def get_db():
    return mysql.connector.connect(
        host=os.getenv('DB_HOST'),
        port=int(os.getenv('DB_PORT', 3306)),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        connection_timeout=60,
        autocommit=True,
    )

def query(sql, params=None):
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params or ())
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        conn.close()

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

def build_where(bulan, pic):
    clauses, params = [], []
    if bulan:
        clauses.append("DATE_FORMAT(tgl_omzet_realtime,'%Y-%m') = %s")
        params.append(bulan)
    if pic:
        clauses.append("name = %s")
        params.append(pic)
    cond = (' AND ' + ' AND '.join(clauses)) if clauses else ''
    return cond, params

@app.route('/api/filters')
@login_required
def api_filters():
    pics = query("SELECT DISTINCT name AS PIC FROM order_risepack WHERE name IS NOT NULL AND name != '' ORDER BY name")
    return jsonify({
        'pics': [r['PIC'] for r in pics],
        'sub_divisions': []
    })

@app.route('/api/kpi')
@login_required
def api_kpi():
    bulan = request.args.get('bulan')
    pic   = request.args.get('pic')
    cond, params = build_where(bulan, pic)

    sql = f"""
        SELECT
            COUNT(DISTINCT order_key) AS total_order,
            SUM(CASE WHEN status_deal='Deal' THEN total_harga ELSE 0 END) AS total_omzet,
            SUM(CASE WHEN status_deal='Deal' THEN modal_sales ELSE 0 END) AS total_modal,
            SUM(CASE WHEN status_deal='Deal' THEN (total_harga - modal_sales) ELSE 0 END) AS total_margin,
            COUNT(DISTINCT CASE WHEN sumber='Repeat Order' AND status_deal='Deal' THEN order_key END) AS total_repeat,
            COUNT(DISTINCT CASE WHEN sumber != 'Repeat Order' AND status_deal='Deal' THEN order_key END) AS total_new,
            COUNT(DISTINCT CASE WHEN status_deal='Deal' THEN order_key END) AS total_deal
        FROM order_risepack
        WHERE (flag_dummy != 'dummy' OR flag_dummy IS NULL)
        {cond}
    """
    row = query(sql, params)[0]
    omzet  = float(row['total_omzet']  or 0)
    modal  = float(row['total_modal']  or 0)
    margin = float(row['total_margin'] or 0)
    deal   = int(row['total_deal']     or 0)
    order  = int(row['total_order']    or 1)
    repeat = int(row['total_repeat']   or 0)
    return jsonify({
        'total_omzet':   omzet,
        'total_modal':   modal,
        'total_margin':  margin,
        'persen_margin': round(margin / omzet * 100, 1) if omzet else 0,
        'total_order':   order,
        'total_deal':    deal,
        'total_repeat':  repeat,
        'total_new':     int(row['total_new'] or 0),
        'closing_rate':  round(deal / order * 100, 1) if order else 0,
        'persen_repeat': round(repeat / deal * 100, 1) if deal else 0,
    })

@app.route('/api/trend-omzet')
@login_required
def api_trend_omzet():
    tahun = request.args.get('tahun', str(datetime.now().year))
    pic   = request.args.get('pic')
    extra, params = ["AND YEAR(tgl_omzet_realtime) = %s"], [tahun]
    if pic:
        extra.append("AND name = %s")
        params.append(pic)
    sql = f"""
        SELECT DATE_FORMAT(tgl_omzet_realtime,'%Y-%m') AS bulan,
               kategori_produksi, SUM(total_harga) AS omzet
        FROM order_risepack
        WHERE status_deal='Deal' AND (flag_dummy != 'dummy' OR flag_dummy IS NULL)
          {' '.join(extra)}
        GROUP BY bulan, kategori_produksi ORDER BY bulan
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

@app.route('/api/top-sales')
@login_required
def api_top_sales():
    bulan = request.args.get('bulan')
    pic   = request.args.get('pic')
    cond, params = build_where(bulan, pic)
    sql = f"""
        SELECT name AS PIC,
               COUNT(DISTINCT order_key) AS total_order,
               SUM(total_harga) AS total_omzet,
               SUM(total_harga - modal_sales) AS total_margin
        FROM order_risepack
        WHERE status_deal='Deal' AND name IS NOT NULL AND name != ''
          AND (flag_dummy != 'dummy' OR flag_dummy IS NULL)
          {cond}
        GROUP BY name ORDER BY total_omzet DESC LIMIT 10
    """
    rows = query(sql, params)
    return jsonify([{**r, 'total_omzet': float(r['total_omzet'] or 0), 'total_margin': float(r['total_margin'] or 0)} for r in rows])

@app.route('/api/sales-by-sumber')
@login_required
def api_sales_by_sumber():
    bulan = request.args.get('bulan')
    pic   = request.args.get('pic')
    cond, params = build_where(bulan, pic)
    sql = f"""
        SELECT sumber, COUNT(DISTINCT order_key) AS total, SUM(total_harga) AS omzet
        FROM order_risepack
        WHERE status_deal='Deal' AND (flag_dummy != 'dummy' OR flag_dummy IS NULL)
          {cond}
        GROUP BY sumber ORDER BY omzet DESC
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

@app.route('/api/produksi')
@login_required
def api_produksi():
    bulan = request.args.get('bulan')
    pic   = request.args.get('pic')
    cond, params = build_where(bulan, pic)
    sql = f"""
        SELECT
            COUNT(CASE WHEN status_order='Berjalan' THEN 1 END) AS berjalan,
            COUNT(CASE WHEN status_order='Selesai Produksi' THEN 1 END) AS tuntas,
            COUNT(CASE WHEN status_order='Belum SPK' THEN 1 END) AS belum_spk
        FROM order_risepack
        WHERE status_deal='Deal' AND (flag_dummy != 'dummy' OR flag_dummy IS NULL)
          {cond}
    """
    return jsonify(query(sql, params)[0])

@app.route('/api/kategori')
@login_required
def api_kategori():
    bulan = request.args.get('bulan')
    pic   = request.args.get('pic')
    cond, params = build_where(bulan, pic)
    sql = f"""
        SELECT kategori_produksi, SUM(total_harga) AS omzet, COUNT(DISTINCT order_key) AS total
        FROM order_risepack
        WHERE status_deal='Deal' AND kategori_produksi IS NOT NULL
          AND (flag_dummy != 'dummy' OR flag_dummy IS NULL)
          {cond}
        GROUP BY kategori_produksi ORDER BY omzet DESC
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
