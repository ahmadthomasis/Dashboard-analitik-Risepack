from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import mysql.connector
import os
import calendar
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

def shift_months(d, n):
    """Geser tanggal mundur n bulan, pertahankan tanggal (clamp ke akhir bulan)."""
    month = d.month - n
    year = d.year
    while month <= 0:
        month += 12
        year -= 1
    last = calendar.monthrange(year, month)[1]
    return d.replace(year=year, month=month, day=min(d.day, last))

def prev_range(tgl_dari, tgl_sampai):
    """Periode pembanding berbasis kalender:
    - rentang <= 1 bulan  -> bulan sebelumnya (tanggal sama)
    - rentang <= 1 quarter -> quarter sebelumnya
    - rentang lebih panjang -> tahun sebelumnya (year-over-year)
    """
    if not tgl_dari or not tgl_sampai:
        return None, None
    try:
        d1 = datetime.strptime(tgl_dari, '%Y-%m-%d').date()
        d2 = datetime.strptime(tgl_sampai, '%Y-%m-%d').date()
    except ValueError:
        return None, None
    days = (d2 - d1).days + 1
    n = 1 if days <= 31 else (3 if days <= 92 else 12)
    return shift_months(d1, n).isoformat(), shift_months(d2, n).isoformat()

def pct(cur, prev):
    """Persentase perubahan cur vs prev. None bila tak bisa dihitung."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)

def fmt_date(v):
    """Format tanggal aman untuk tipe apa pun (date/datetime/str/None)."""
    if v is None:
        return None
    if hasattr(v, 'strftime'):
        return v.strftime('%Y-%m-%d')
    return str(v)[:10]

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
    rows = query(sql, [tahun] + params)
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

# ─── Grafik: Produk (jenis_bahan) ────────────────────────────────
@app.route('/api/sales-by-bahan')
@login_required
def api_sales_by_bahan():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    sql = f"""
        SELECT o.jenis_bahan,
               COUNT(DISTINCT o.order_key) AS orders,
               SUM(o.total_harga) AS omzet
        {BASE}
        AND o.status_deal='Deal' AND o.jenis_bahan IS NOT NULL AND o.jenis_bahan!=''
        {cond}
        GROUP BY o.jenis_bahan ORDER BY omzet DESC
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

@app.route('/api/trend-bahan')
@login_required
def api_trend_bahan():
    tahun  = request.args.get('tahun', str(datetime.now().year))
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(None, None, pic, divisi)
    sql = f"""
        SELECT DATE_FORMAT(o.tgl_omzet_realtime,'%Y-%m') AS bulan,
               o.jenis_bahan, SUM(o.total_harga) AS omzet
        {BASE}
        AND o.status_deal='Deal' AND o.jenis_bahan IS NOT NULL AND o.jenis_bahan!=''
        AND YEAR(o.tgl_omzet_realtime) = %s
        {cond}
        GROUP BY bulan, o.jenis_bahan ORDER BY bulan
    """
    rows = query(sql, [tahun] + params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

# ─── Grafik: Sales by Margin (bucket margin %) ───────────────────
@app.route('/api/sales-by-margin')
@login_required
def api_sales_by_margin():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    sql = f"""
        SELECT
            CASE
                WHEN omzet <= 0 THEN 'Tidak diketahui'
                WHEN margin/omzet*100 < 10  THEN 'Low (<10%)'
                WHEN margin/omzet*100 <= 20 THEN 'Medium (10-20%)'
                ELSE 'High (>20%)'
            END AS bucket,
            COUNT(*) AS orders, SUM(omzet) AS omzet
        FROM (
            SELECT o.order_key,
                   SUM(o.total_harga) AS omzet,
                   SUM(o.total_harga - o.modal_sales) AS margin
            {BASE}
            AND o.status_deal='Deal'
            {cond}
            GROUP BY o.order_key
        ) t
        GROUP BY bucket
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])

# ─── Grafik: Persentase Margin bulanan (setahun) ─────────────────
@app.route('/api/margin-bulanan')
@login_required
def api_margin_bulanan():
    tahun  = request.args.get('tahun', str(datetime.now().year))
    pic    = request.args.get('pic')
    divisi = request.args.get('divisi')
    cond, params = build_where(None, None, pic, divisi)
    sql = f"""
        SELECT DATE_FORMAT(o.tgl_omzet_realtime,'%Y-%m') AS bulan,
               SUM(o.total_harga) AS omzet,
               SUM(o.total_harga - o.modal_sales) AS margin
        {BASE}
        AND o.status_deal='Deal'
        AND YEAR(o.tgl_omzet_realtime) = %s
        {cond}
        GROUP BY bulan ORDER BY bulan
    """
    rows = query(sql, [tahun] + params)
    out = []
    for r in rows:
        omzet = float(r['omzet'] or 0)
        margin = float(r['margin'] or 0)
        out.append({'bulan': r['bulan'],
                    'persen_margin': round(margin / omzet * 100, 1) if omzet else 0})
    return jsonify(out)

# ─── Grafik: Lifetime Value (frekuensi beli per customer) ────────
@app.route('/api/lifetime-value')
@login_required
def api_lifetime_value():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    sql = f"""
        SELECT
            CASE WHEN n >= 4 THEN '>=4x' WHEN n = 3 THEN '3x'
                 WHEN n = 2 THEN '2x' ELSE '1x' END AS bucket,
            COUNT(*) AS customers
        FROM (
            SELECT o.id_customer, COUNT(DISTINCT o.order_key) AS n
            {BASE}
            AND o.status_deal='Deal' AND o.id_customer IS NOT NULL
            {cond}
            GROUP BY o.id_customer
        ) t
        GROUP BY bucket
    """
    rows = query(sql, params)
    return jsonify([{**r, 'customers': int(r['customers'] or 0)} for r in rows])

# ─── Grafik: Kategori Omzet (bucket nilai order) ─────────────────
@app.route('/api/kategori-omzet')
@login_required
def api_kategori_omzet():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    sql = f"""
        SELECT
            CASE
                WHEN omzet > 100000000 THEN '>100 jt'
                WHEN omzet > 50000000  THEN '50-100 jt'
                WHEN omzet > 30000000  THEN '30-50 jt'
                ELSE '<30 jt'
            END AS bucket,
            COUNT(*) AS orders, SUM(omzet) AS omzet
        FROM (
            SELECT o.order_key, SUM(o.total_harga) AS omzet
            {BASE}
            AND o.status_deal='Deal'
            {cond}
            GROUP BY o.order_key
        ) t
        GROUP BY bucket
    """
    rows = query(sql, params)
    return jsonify([{**r, 'omzet': float(r['omzet'] or 0)} for r in rows])


# ─── Detail Order (tabel cek cepat, per baris produk) ────────────
@app.route('/api/detail')
@login_required
def api_detail():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    sql = f"""
        SELECT o.sko, o.nama, o.sumber,
               TRIM(CONCAT(COALESCE(o.jenis_bahan,''),' ',COALESCE(o.nama_brand,''))) AS nama_produk,
               o.tgl_omzet_realtime AS tanggal,
               o.jumlah_produk AS quantity,
               o.harga_produk AS harga_jual,
               o.modal_sales, o.total_harga
        {BASE}
        AND o.status_deal='Deal'
        {cond}
        ORDER BY o.tgl_omzet_realtime DESC
        LIMIT 2000
    """
    rows = query(sql, params)
    out = []
    for r in rows:
        qty   = float(r['quantity'] or 0)
        total = float(r['total_harga'] or 0)
        modal = float(r['modal_sales'] or 0)
        tgl = r['tanggal']
        out.append({
            'sko': r['sko'], 'nama': r['nama'], 'sumber': r['sumber'],
            'nama_produk': (r['nama_produk'] or '').strip(),
            'tanggal': tgl.strftime('%Y-%m-%d') if tgl else None,
            'quantity': int(qty),
            'harga_jual': float(r['harga_jual'] or 0),
            'harga_modal': round(modal / qty) if qty else 0,
            'total_harga': total,
            'persen_margin': round((total - modal) / total * 100, 1) if total else 0,
        })
    return jsonify(out)


# ─── Monitoring Potensi (kelengkapan input harga oleh sales) ─────
@app.route('/api/monitoring-potensi')
@login_required
def api_monitoring_potensi():
    """Customer baru via Online; potensi = SUM(total_harga). Belum diisi = 0.
    Difilter berdasarkan tgl_order (saat lead masuk), bukan tgl omzet,
    agar lead yang belum dihargai tetap muncul."""
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    clauses = ["o.sumber = 'Online'", "o.id_customer IS NOT NULL"]
    params = []
    if tgl_dari:
        clauses.append("o.tgl_order >= %s"); params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("o.tgl_order <= %s"); params.append(tgl_sampai + ' 23:59:59')
    if pic:
        clauses.append("o.name = %s"); params.append(pic)
    if divisi:
        clauses.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        params.append(divisi)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT o.id_customer,
               MAX(o.nama) AS nama,
               MAX(o.nama_instansi) AS instansi,
               MAX(o.name) AS pic,
               DATE_FORMAT(MIN(o.tgl_order),'%Y-%m-%d') AS tgl_masuk,
               COUNT(DISTINCT o.order_key) AS orders,
               SUM(o.total_harga) AS potensi
        FROM order_risepack o
        WHERE (o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL) AND {where}
        GROUP BY o.id_customer
        ORDER BY MIN(o.tgl_order) DESC
        LIMIT 3000
    """
    rows = query(sql, params)
    out = []
    for r in rows:
        potensi = float(r['potensi'] or 0)
        out.append({
            'nama': r['nama'], 'instansi': r['instansi'], 'pic': r['pic'],
            'tgl_masuk': fmt_date(r['tgl_masuk']),
            'orders': int(r['orders'] or 0),
            'potensi': potensi,
            'terisi': potensi > 0,
        })
    return jsonify(out)


# ─── DIAGNOSTIK SEMENTARA (hapus setelah dipakai) ────────────────
@app.route('/api/_viewdef')
@login_required
def api_viewdef():
    """Tampilkan definisi SQL sebuah view, untuk memahami logika leads/potensi
    aplikasi tanpa menebak. Pakai ?v=nama_view."""
    name = request.args.get('v', 'view_newleads')
    allowed = {'view_newleads', 'view_salesorder', 'view_customerprofile', 'view_ordercrm'}
    if name not in allowed:
        return jsonify({'error': 'view tidak diizinkan', 'pilihan': sorted(allowed)})
    rows = query(f"SHOW CREATE VIEW `{name}`")
    return jsonify(rows)


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
