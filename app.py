from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import mysql.connector
import os
import json
from datetime import datetime
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

def load_kpi_config():
    try:
        with open('kpi_config.json') as f:
            return json.load(f)
    except Exception:
        return {}

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

def build_where(bulan, pic, divisi):
    clauses, params = [], []
    if bulan:
        clauses.append("DATE_FORMAT(o.tgl_omzet_realtime,'%Y-%m') = %s")
        params.append(bulan)
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

BASE = """
    FROM order_risepack o
    WHERE (o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)
"""

@app.route('/api/filters')
@login_required
def api_filters():
    pics = query("SELECT DISTINCT name AS PIC FROM order_risepack WHERE name IS NOT NULL AND name != '' ORDER BY name")
    divs = query("SELECT DISTINCT sub_division FROM tb_orders WHERE sub_division IS NOT NULL ORDER BY sub_division")
    return jsonify({
        'pics': [r['PIC'] for r in pics],
        'sub_divisions': [r['sub_division'] for r in divs]
    })

@app.route('/api/kpi')
@login_required
def api_kpi():
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(bulan, pic, divisi)

    sql = f"""
        SELECT
            COUNT(DISTINCT o.order_key) AS total_order,
            SUM(CASE WHEN o.status_deal='Deal' THEN o.total_harga ELSE 0 END) AS total_omzet,
            SUM(CASE WHEN o.status_deal='Deal' THEN o.modal_sales ELSE 0 END) AS total_modal,
            SUM(CASE WHEN o.status_deal='Deal' THEN (o.total_harga-o.modal_sales) ELSE 0 END) AS total_margin,
            COUNT(DISTINCT CASE WHEN o.sumber='Repeat Order' AND o.status_deal='Deal' THEN o.order_key END) AS total_repeat,
            COUNT(DISTINCT CASE WHEN o.sumber!='Repeat Order' AND o.status_deal='Deal' THEN o.order_key END) AS total_new,
            COUNT(DISTINCT CASE WHEN o.status_deal='Deal' THEN o.order_key END) AS total_deal
        {BASE} {cond}
    """
    row = query(sql, params)[0]
    omzet  = float(row['total_omzet']  or 0)
    modal  = float(row['total_modal']  or 0)
    margin = float(row['total_margin'] or 0)
    deal   = int(row['total_deal']     or 0)
    order  = int(row['total_order']    or 1)
    repeat = int(row['total_repeat']   or 0)
    return jsonify({
        'total_omzet':   omzet, 'total_modal': modal, 'total_margin': margin,
        'persen_margin': round(margin/omzet*100,1) if omzet else 0,
        'total_order':   order, 'total_deal': deal, 'total_repeat': repeat,
        'total_new':     int(row['total_new'] or 0),
        'closing_rate':  round(deal/order*100,1) if order else 0,
        'persen_repeat': round(repeat/deal*100,1) if deal else 0,
    })

@app.route('/api/trend-omzet')
@login_required
def api_trend_omzet():
    tahun  = request.args.get('tahun', str(datetime.now().year))
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(None, pic, divisi)

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
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(bulan, pic, divisi)

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
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(bulan, pic, divisi)

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
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(bulan, pic, divisi)

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
    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(bulan, pic, divisi)

    sql = f"""
        SELECT o.kategori_produksi, SUM(o.total_harga) AS omzet, COUNT(DISTINCT o.order_key) AS total
        {BASE}
        AND o.status_deal='Deal' AND o.kategori_produksi IS NOT NULL
        {cond}
        GROUP BY o.kategori_produksi ORDER BY omzet DESC
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

@app.route('/api/ring')
@login_required
def api_ring():
    """
    Ring Prioritas Customer.
    Ring 1 = total nilai >= ring1 threshold
    Ring 2 = total nilai >= ring2 threshold
    Ring 3 = di bawah ring2

    Nilai per customer = potensi (Follow Up, basis waktu_kontak)
                       + repeat  (Deal + Repeat Order, basis tgl_omzet_realtime)

    Status:
      'Repeat + Pipeline' jika punya keduanya
      'Follow Up'         jika hanya punya potensi
      'Repeat'            jika hanya punya repeat
    """
    cfg = load_kpi_config()
    th  = cfg.get('ring_thresholds', {'ring1': 100000000, 'ring2': 30000000})
    r1  = float(th.get('ring1', 100000000))
    r2  = float(th.get('ring2', 30000000))

    bulan  = request.args.get('bulan')
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')

    base_flag = "(o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)"

    # Bangun kondisi PIC dan divisi (sama untuk kedua query)
    pic_cond, pic_params = '', []
    if pic:
        pic_cond = ' AND o.name = %s'
        pic_params = [pic]

    div_cond, div_params = '', []
    if divisi:
        div_cond = ' AND o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)'
        div_params = [divisi]

    # Kondisi bulan untuk masing-masing basis tanggal
    bulan_kontak_cond, bulan_kontak_params = '', []
    if bulan:
        bulan_kontak_cond = " AND DATE_FORMAT(o.waktu_kontak, '%Y-%m') = %s"
        bulan_kontak_params = [bulan]

    bulan_omzet_cond, bulan_omzet_params = '', []
    if bulan:
        bulan_omzet_cond = " AND DATE_FORMAT(o.tgl_omzet_realtime, '%Y-%m') = %s"
        bulan_omzet_params = [bulan]

    # ── Query A: Potensi (Follow Up, basis waktu_kontak) ──────────────────────
    sql_a = f"""
        SELECT o.id_customer, o.nama, o.nama_instansi,
               SUM(o.total_harga) AS potensi
        FROM order_risepack o
        WHERE {base_flag}
          AND o.status_deal = 'Follow Up'
          {bulan_kontak_cond}{pic_cond}{div_cond}
        GROUP BY o.id_customer, o.nama, o.nama_instansi
    """
    params_a = bulan_kontak_params + pic_params + div_params
    rows_a = query(sql_a, params_a)

    # ── Query B: Repeat (Deal + Repeat Order, basis tgl_omzet_realtime) ───────
    sql_b = f"""
        SELECT o.id_customer, o.nama, o.nama_instansi,
               SUM(o.total_harga) AS repeat_val
        FROM order_risepack o
        WHERE {base_flag}
          AND o.status_deal = 'Deal'
          AND o.sumber = 'Repeat Order'
          {bulan_omzet_cond}{pic_cond}{div_cond}
        GROUP BY o.id_customer, o.nama, o.nama_instansi
    """
    params_b = bulan_omzet_params + pic_params + div_params
    rows_b = query(sql_b, params_b)

    # ── Gabungkan per id_customer ─────────────────────────────────────────────
    customers = {}

    for r in rows_a:
        cid = r['id_customer']
        if cid is None:
            continue
        if cid not in customers:
            nama_display = (r['nama_instansi'] or '').strip() or (r['nama'] or '').strip() or '(tanpa nama)'
            customers[cid] = {'nama': nama_display, 'potensi': 0.0, 'repeat': 0.0}
        customers[cid]['potensi'] += float(r['potensi'] or 0)

    for r in rows_b:
        cid = r['id_customer']
        if cid is None:
            continue
        if cid not in customers:
            nama_display = (r['nama_instansi'] or '').strip() or (r['nama'] or '').strip() or '(tanpa nama)'
            customers[cid] = {'nama': nama_display, 'potensi': 0.0, 'repeat': 0.0}
        customers[cid]['repeat'] += float(r['repeat_val'] or 0)

    # ── Hitung ring, status, total ────────────────────────────────────────────
    result = []
    summary = {'ring1': 0, 'ring2': 0, 'ring3': 0}

    for cid, c in customers.items():
        total = c['potensi'] + c['repeat']
        if total <= 0:
            continue

        ring = 1 if total >= r1 else (2 if total >= r2 else 3)

        if c['potensi'] > 0 and c['repeat'] > 0:
            status = 'Repeat + Pipeline'
        elif c['potensi'] > 0:
            status = 'Follow Up'
        else:
            status = 'Repeat'

        summary[f'ring{ring}'] += 1
        result.append({
            'id_customer': cid,
            'nama': c['nama'],
            'ring': ring,
            'status': status,
            'potensi': c['potensi'],
            'repeat': c['repeat'],
            'total': total,
        })

    result.sort(key=lambda x: x['total'], reverse=True)

    return jsonify({
        'data': result,
        'summary': summary,
        'thresholds': {'ring1': r1, 'ring2': r2},
    })

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
