from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import mysql.connector
import os
import calendar
import json
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
        'omzet_new': omzet - rep_omzet,
        'persen_repeat_omzet': round(rep_omzet / omzet * 100, 1) if omzet else 0,
        'closing_rate_new': round(new / new_order * 100, 1) if new_order else 0,
    }

def new_funnel(tgl_dari, tgl_sampai, pic, divisi):
    """Corong customer baru = ONLINE leads (konsisten dgn Monitoring Potensi 71).
    Difilter waktu_kontak, grain per lead (sko_key).
      qualified_new = jumlah leads online
      total_new     = leads online yang Deal (new customer)
      omzet_new     = omzet dari leads online yang Deal
      closing_rate_new = total_new / qualified_new
    """
    clauses = ["(o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)", "o.sumber = 'Online'"]
    params = []
    if tgl_dari:
        clauses.append("o.waktu_kontak >= %s"); params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("o.waktu_kontak <= %s"); params.append(tgl_sampai + ' 23:59:59')
    if pic:
        clauses.append("o.name = %s"); params.append(pic)
    if divisi:
        clauses.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        params.append(divisi)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT
            COUNT(DISTINCT o.sko_key) AS qualified,
            COUNT(DISTINCT CASE WHEN o.status_deal='Deal' THEN o.sko_key END) AS deal_new,
            SUM(CASE WHEN o.status_deal='Deal' THEN o.total_harga ELSE 0 END) AS omzet_new
        FROM order_risepack o
        WHERE {where}
    """
    r = query(sql, params)[0]
    q = int(r['qualified'] or 0)
    d = int(r['deal_new'] or 0)
    om = float(r['omzet_new'] or 0)
    return {
        'qualified_new': q,
        'total_new': d,
        'omzet_new': om,
        'closing_rate_new': round(d / q * 100, 1) if q else 0,
    }

@app.route('/api/kpi')
@login_required
def api_kpi():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    cur = kpi_metrics(cond, params)
    omzet_new_cur = cur['omzet_new']                          # Omzet New = non-repeat (by tgl omzet)
    cur.update(new_funnel(tgl_dari, tgl_sampai, pic, divisi)) # corong online utk total_new/closing_rate_new/qualified_new
    cur['omzet_new'] = omzet_new_cur                          # kembalikan ke definisi non-repeat

    # Perbandingan periode sebelumnya (durasi sama)
    delta = {}
    p1, p2 = prev_range(tgl_dari, tgl_sampai)
    if p1 and p2:
        pcond, pparams = build_where(p1, p2, pic, divisi)
        prev = kpi_metrics(pcond, pparams)
        omzet_new_prev = prev['omzet_new']
        prev.update(new_funnel(p1, p2, pic, divisi))
        prev['omzet_new'] = omzet_new_prev
        for k in ['total_omzet', 'total_modal', 'total_margin', 'total_order',
                  'total_deal', 'total_repeat', 'total_new', 'closing_rate',
                  'avg_purchase', 'repeat_omzet', 'omzet_new', 'closing_rate_new', 'persen_repeat']:
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
        SELECT o.sko, o.sko_key, o.jenis_bahan, o.nama, o.sumber,
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

    # Nama produk asli dari tb_produksis (cocok per sko_key + jenis_bahan, fallback per sko_key)
    keys = list({r['sko_key'] for r in rows if r.get('sko_key')})
    pr_pair, pr_sko = {}, {}
    if keys:
        ph = ','.join(['%s'] * len(keys))
        for p in query(f"SELECT sko_key, jenis_bahan, nama_produk FROM tb_produksis "
                       f"WHERE sko_key IN ({ph}) AND nama_produk IS NOT NULL AND nama_produk <> ''", keys):
            pr_sko.setdefault(p['sko_key'], p['nama_produk'])
            pr_pair[(p['sko_key'], (p['jenis_bahan'] or '').strip())] = p['nama_produk']

    out = []
    for r in rows:
        qty   = float(r['quantity'] or 0)
        total = float(r['total_harga'] or 0)
        modal = float(r['modal_sales'] or 0)
        tgl = r['tanggal']
        jb = (r.get('jenis_bahan') or '').strip()
        nm = pr_pair.get((r['sko_key'], jb)) or pr_sko.get(r['sko_key']) or (r['nama_produk'] or '').strip()
        out.append({
            'sko': r['sko'], 'nama': r['nama'], 'sumber': r['sumber'],
            'nama_produk': nm,
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
    clauses = ["(o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)", "o.sumber = 'Online'"]
    params = []
    if tgl_dari:
        clauses.append("o.waktu_kontak >= %s"); params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("o.waktu_kontak <= %s"); params.append(tgl_sampai + ' 23:59:59')
    if pic:
        clauses.append("o.name = %s"); params.append(pic)
    if divisi:
        clauses.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        params.append(divisi)
    where = " AND ".join(clauses)
    # Pakai order_risepack (tabel cepat, tanpa join) — sudah berisi waktu_kontak & total_harga
    sql = f"""
        SELECT DATE_FORMAT(o.waktu_kontak,'%Y-%m-%d') AS tgl_kontak,
               o.nama AS nama,
               o.nama_instansi AS instansi,
               o.name AS pic,
               o.status_deal AS status_deal,
               o.total_harga AS potensi
        FROM order_risepack o
        WHERE {where}
        ORDER BY o.waktu_kontak DESC
        LIMIT 3000
    """
    rows = query(sql, params)
    out = []
    for r in rows:
        potensi = float(r['potensi'] or 0)
        out.append({
            'tgl_kontak': r['tgl_kontak'],
            'nama': r['nama'], 'instansi': r['instansi'], 'pic': r['pic'],
            'status_deal': r['status_deal'],
            'potensi': potensi,
            'terisi': potensi > 0,
        })
    return jsonify(out)


# ─── Customer: Deal New vs Repeat (pivot per customer) ───────────
@app.route('/api/deal-new-repeat')
@login_required
def api_deal_new_repeat():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    sql = f"""
        SELECT MAX(o.nama) AS nama,
               MAX(o.nama_instansi) AS instansi,
               SUM(CASE WHEN o.sumber='Repeat Order' THEN o.total_harga ELSE 0 END) AS omzet_repeat,
               SUM(CASE WHEN o.sumber<>'Repeat Order' THEN o.total_harga ELSE 0 END) AS omzet_new
        {BASE}
        AND o.status_deal='Deal' AND o.id_customer IS NOT NULL
        {cond}
        GROUP BY o.id_customer
        ORDER BY SUM(o.total_harga) DESC
        LIMIT 1000
    """
    rows = query(sql, params)
    return jsonify([{
        'nama': r['nama'], 'instansi': r['instansi'],
        'omzet_new': float(r['omzet_new'] or 0),
        'omzet_repeat': float(r['omzet_repeat'] or 0),
    } for r in rows])

# ─── Customer: Journey (grading) per customer ────────────────────
@app.route('/api/journey')
@login_required
def api_journey():
    """Customer Follow Up (belum order). Filter waktu_kontak, nilai = potensi."""
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    clauses = ["(o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)",
               "o.status_deal = 'Follow Up'", "o.id_customer IS NOT NULL"]
    params = []
    if tgl_dari:
        clauses.append("o.waktu_kontak >= %s"); params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("o.waktu_kontak <= %s"); params.append(tgl_sampai + ' 23:59:59')
    if pic:
        clauses.append("o.name = %s"); params.append(pic)
    if divisi:
        clauses.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        params.append(divisi)
    where = " AND ".join(clauses)
    sql = f"""
        SELECT MAX(o.nama) AS nama,
               MAX(o.nama_instansi) AS instansi,
               MAX(o.name) AS pic,
               DATE_FORMAT(MAX(o.waktu_kontak),'%Y-%m-%d') AS tgl_kontak,
               COUNT(DISTINCT o.order_key) AS orders,
               SUM(o.total_harga) AS potensi
        FROM order_risepack o
        WHERE {where}
        GROUP BY o.id_customer
        ORDER BY SUM(o.total_harga) DESC
        LIMIT 1500
    """
    rows = query(sql, params)
    return jsonify([{
        'nama': r['nama'], 'instansi': r['instansi'], 'pic': r['pic'],
        'tgl_kontak': r['tgl_kontak'],
        'orders': int(r['orders'] or 0),
        'potensi': float(r['potensi'] or 0),
    } for r in rows])

# ─── Customer: Achievement SKO 10x ───────────────────────────────
@app.route('/api/sko-achievement')
@login_required
def api_sko_achievement():
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    sql = f"""
        SELECT MAX(o.nama) AS nama,
               COUNT(DISTINCT o.sko_key) AS jml
        {BASE}
        AND o.status_deal='Deal' AND o.id_customer IS NOT NULL
        {cond}
        GROUP BY o.id_customer
        ORDER BY jml DESC
        LIMIT 2000
    """
    rows = query(sql, params)
    return jsonify([{'nama': r['nama'], 'jml': int(r['jml'] or 0)} for r in rows])


# ─── Bonus Achievement Sales ─────────────────────────────────────
@app.route('/api/bonus')
@login_required
def api_bonus():
    """Bonus per SKO. Bonus = margin x rate (Repeat 5% / New-Online 7% / lainnya 0).
    Denda = bonus x faktor telat (1-7hr 25%, 8-14hr 50%, >14hr 100%, tidak telat 0).
    Net = bonus - denda. Difilter tgl_pelunasan (order yang sudah lunas)."""
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    clauses = ["(o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)",
               "o.status_deal = 'Deal'", "inv.tanggal_pelunasan IS NOT NULL"]
    params = []
    if pic:
        clauses.append("o.name = %s"); params.append(pic)
    if divisi:
        clauses.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        params.append(divisi)
    if tgl_dari:
        clauses.append("inv.tanggal_pelunasan >= %s"); params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("inv.tanggal_pelunasan <= %s"); params.append(tgl_sampai)
    where = " AND ".join(clauses)
    # tanggal pelunasan & jatuh tempo asli ada di invoices (lewat invoice_details), sesuai view_salesorder
    sql = f"""
        SELECT MAX(o.nama) AS nama, o.sko, MAX(o.sumber) AS sumber, MAX(o.name) AS pic,
               MAX(TRIM(CONCAT(COALESCE(o.jenis_bahan,''),' ',COALESCE(o.nama_brand,'')))) AS nama_produk,
               MAX(o.total_harga) AS total_harga, MAX(o.modal_sales) AS modal_sales,
               DATE_FORMAT(MAX(inv.tanggal_pelunasan),'%Y-%m-%d') AS tgl_pelunasan,
               DATE_FORMAT(MAX(inv.tanggal_jatuh_tempo),'%Y-%m-%d') AS tgl_jatuh_tempo,
               DATEDIFF(MAX(inv.tanggal_pelunasan), MAX(inv.tanggal_jatuh_tempo)) AS hari_telat
        FROM order_risepack o
        JOIN invoice_details idt ON o.sko = idt.kode_order
        JOIN invoices inv ON idt.invoice_key = inv.invoice_key
        WHERE {where}
        GROUP BY o.sko_key, o.sko
        ORDER BY MAX(inv.tanggal_pelunasan) DESC
        LIMIT 3000
    """
    rows = query(sql, params)
    out = []
    for r in rows:
        margin = float(r['total_harga'] or 0) - float(r['modal_sales'] or 0)
        sumber = r['sumber'] or ''
        rate = 0.025 if sumber == 'Repeat Order' else (0.05 if sumber in ('Online', 'Online Lintas') else 0.0)
        bonus = margin * rate
        h = r['hari_telat']
        h = int(h) if h is not None else None
        if h is None or h <= 0:
            mult = 0.0
        elif h <= 7:
            mult = 0.25
        elif h <= 14:
            mult = 0.50
        else:
            mult = 1.0
        denda = bonus * mult
        out.append({
            'nama': r['nama'], 'sko': r['sko'], 'sumber': sumber, 'pic': r['pic'],
            'nama_produk': (r['nama_produk'] or '').strip(),
            'tgl_pelunasan': r['tgl_pelunasan'], 'tgl_jatuh_tempo': r['tgl_jatuh_tempo'],
            'hari_telat': h if h is not None else 0,
            'margin': round(margin), 'bonus': round(bonus),
            'denda': round(denda), 'net': round(bonus - denda),
        })
    return jsonify(out)


# ─── KPI Sales (OKR scorecard) ───────────────────────────────────
def load_kpi_config():
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kpi_config.json')
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'scoring_bands': [[95, 6], [80, 5], [60, 4], [40, 3], [20, 2], [10, 1]],
                'labels': [[0, '-']], 'kpi': [], 'umbrella_manual': {}}

def quarter_start(d):
    return d.replace(month=((d.month - 1) // 3) * 3 + 1, day=1)

def months_between(d1, d2):
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) + 1

def score_from_ach(ach, bands):
    for thr, sc in bands:
        if ach >= thr:
            return sc
    return 0

def sum_months(cfg_map, d1, d2, nmonths, default=0):
    """Jumlahkan nilai per-bulan dari config (YYYY-MM) sepanjang rentang."""
    if not (d1 and d2):
        return default
    tot, y, mo = 0, d1.year, d1.month
    for _ in range(nmonths):
        tot += cfg_map.get(f"{y:04d}-{mo:02d}", default) or 0
        mo += 1
        if mo > 12:
            mo = 1; y += 1
    return tot

def sales_omzet_target(pic, d1, d2, nmonths, cfg):
    """Target omzet sales per bulan dari tb_target_sales (yang diset manager di app).
    Dijumlahkan sepanjang rentang. Fallback ke config bila belum diset di app."""
    months = []
    if d1 and d2:
        y, mo = d1.year, d1.month
        for _ in range(nmonths):
            months.append(f"{y:04d}-{mo:02d}")
            mo += 1
            if mo > 12:
                mo = 1; y += 1
    db_total = 0.0
    if months:
        ph = ','.join(['%s'] * len(months))
        sql = (f"SELECT COALESCE(SUM(ts.target_revenue),0) v "
               f"FROM tb_target_sales ts JOIN users u ON ts.user_id = u.id "
               f"WHERE u.name = %s AND ts.deleted_at IS NULL "
               f"AND CONCAT(ts.year,'-',LPAD(ts.month,2,'0')) IN ({ph})")
        db_total = float(query(sql, [pic] + months)[0]['v'] or 0)
    if db_total > 0:
        return db_total
    return (cfg.get('fungsi_omzet_target', {}).get(pic, 0) or 0) * nmonths

def potensi_total(tgl_dari, tgl_sampai, pic, divisi):
    """Total nilai potensi (total_harga) dari lead Online, by waktu_kontak."""
    clauses = ["(o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)", "o.sumber = 'Online'"]
    params = []
    if tgl_dari:
        clauses.append("o.waktu_kontak >= %s"); params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("o.waktu_kontak <= %s"); params.append(tgl_sampai + ' 23:59:59')
    if pic:
        clauses.append("o.name = %s"); params.append(pic)
    if divisi:
        clauses.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        params.append(divisi)
    sql = f"SELECT SUM(o.total_harga) v FROM order_risepack o WHERE {' AND '.join(clauses)}"
    return float(query(sql, params)[0]['v'] or 0)

def qualified_leads_count(tgl_dari, tgl_sampai, divisi):
    """Jumlah qualified leads (tipe_kontak 'Bukan Sampah') semua sumber, by waktu_kontak."""
    clauses = ["(o.flag_dummy != 'dummy' OR o.flag_dummy IS NULL)", "o.tipe_kontak = 'Bukan Sampah'"]
    params = []
    if tgl_dari:
        clauses.append("o.waktu_kontak >= %s"); params.append(tgl_dari)
    if tgl_sampai:
        clauses.append("o.waktu_kontak <= %s"); params.append(tgl_sampai + ' 23:59:59')
    if divisi:
        clauses.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        params.append(divisi)
    sql = f"SELECT COUNT(DISTINCT o.sko_key) v FROM order_risepack o WHERE {' AND '.join(clauses)}"
    return int(query(sql, params)[0]['v'] or 0)

@app.route('/api/kpi-score')
@login_required
def api_kpi_score():
    cfg = load_kpi_config()
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    pic = None  # KPI Divisi selalu level tim — abaikan filter PIC (per orang pakai tab Fungsi Sales)
    def pdate(s):
        try: return datetime.strptime(s, '%Y-%m-%d').date()
        except Exception: return None
    d1, d2 = pdate(tgl_dari), pdate(tgl_sampai)
    nmonths = months_between(d1, d2) if (d1 and d2) else 1
    bands = cfg.get('scoring_bands', [])

    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    m = kpi_metrics(cond, params)
    nf = new_funnel(tgl_dari, tgl_sampai, pic, divisi)

    pot = potensi_total(tgl_dari, tgl_sampai, pic, divisi)
    default_omzet_t = next((k['target'] for k in cfg.get('kpi', []) if k['id'] == 'omzet'), 2500000000)
    omzet_target_eff = sum_months(cfg.get('omzet_target', {}), d1, d2, nmonths, default_omzet_t) if (d1 and d2) else default_omzet_t
    umbrella_val = sum_months(cfg.get('umbrella_manual', {}), d1, d2, nmonths, 0)

    rows, total_w, total_ach_w = [], 0.0, 0.0
    for k in cfg.get('kpi', []):
        basis, target, w = k['basis'], k['target'], k['weight'] / 100.0
        target_eff = target
        if basis == 'omzet_monthly':
            actual = m['total_omzet']; target_eff = omzet_target_eff
        elif basis == 'repeat_vs_target':
            actual = m['repeat_omzet']; target_eff = (target / 100.0) * omzet_target_eff
        elif basis == 'closing_rate_new':
            actual = nf['closing_rate_new']
        elif basis == 'umbrella_manual':
            actual = umbrella_val; target_eff = target * nmonths
        elif basis == 'potensi_total':
            actual = pot; target_eff = target * nmonths
        else:
            actual = 0
        ach = min(round(actual / target_eff * 100, 1), 100.0) if target_eff else 0
        sc = score_from_ach(ach, bands)
        weighted = round(sc * w, 2)
        total_w += weighted
        total_ach_w += ach * w
        rows.append({'id': k['id'], 'name': k['name'], 'weight': k['weight'],
                     'target': target_eff, 'unit': k.get('unit', ''), 'note': k.get('note', ''),
                     'actual': round(actual, 1) if isinstance(actual, float) else actual,
                     'ach': ach, 'score': sc, 'weighted': weighted})

    total_ach = round(total_ach_w, 1)
    label = next((lb for thr, lb in cfg.get('labels', []) if total_ach >= thr), '-')
    return jsonify({'rows': rows, 'total_kpi': round(total_w, 2),
                    'total_ach': total_ach, 'label': label, 'months': nmonths})

@app.route('/api/kpi-fungsi')
@login_required
def api_kpi_fungsi():
    """KPI per fungsi sales (individu). Sales dipilih lewat filter PIC."""
    cfg = load_kpi_config()
    tgl_dari, tgl_sampai, pic, divisi = get_args()
    sales_list = cfg.get('fungsi_sales', [])
    if not pic or pic not in sales_list:
        return jsonify({'valid': False, 'sales_list': sales_list})

    def pdate(s):
        try: return datetime.strptime(s, '%Y-%m-%d').date()
        except Exception: return None
    d1, d2 = pdate(tgl_dari), pdate(tgl_sampai)
    nmonths = months_between(d1, d2) if (d1 and d2) else 1
    bands = cfg.get('scoring_bands', [])

    cond, params = build_where(tgl_dari, tgl_sampai, pic, divisi)
    m = kpi_metrics(cond, params)
    nf = new_funnel(tgl_dari, tgl_sampai, pic, divisi)

    omzet_target_eff = sales_omzet_target(pic, d1, d2, nmonths, cfg)
    umbrella_val = sum_months(cfg.get('fungsi_umbrella', {}).get(pic, {}), d1, d2, nmonths, 0)

    rows, total_w, total_ach_w = [], 0.0, 0.0
    for k in cfg.get('fungsi_kpi', []):
        basis, target, w = k['basis'], k['target'], k['weight'] / 100.0
        target_eff = target
        if basis == 'omzet_sales':
            actual = m['total_omzet']; target_eff = omzet_target_eff
        elif basis == 'repeat_sales':
            actual = m['repeat_omzet']; target_eff = (target / 100.0) * omzet_target_eff
        elif basis == 'closing_rate_new':
            actual = nf['closing_rate_new']
        elif basis == 'gross_margin':
            actual = m['persen_margin']
        elif basis == 'umbrella_sales':
            actual = umbrella_val; target_eff = target * nmonths
        else:
            actual = 0
        ach = min(round(actual / target_eff * 100, 1), 100.0) if target_eff else 0
        sc = score_from_ach(ach, bands)
        weighted = round(sc * w, 2)
        total_w += weighted
        total_ach_w += ach * w
        rows.append({'id': k['id'], 'name': k['name'], 'weight': k['weight'],
                     'target': target_eff, 'unit': k.get('unit', ''), 'note': k.get('note', ''),
                     'actual': round(actual, 1) if isinstance(actual, float) else actual,
                     'ach': ach, 'score': sc, 'weighted': weighted})

    total_ach = round(total_ach_w, 1)
    label = next((lb for thr, lb in cfg.get('labels', []) if total_ach >= thr), '-')
    return jsonify({'valid': True, 'sales': pic, 'rows': rows, 'total_kpi': round(total_w, 2),
                    'total_ach': total_ach, 'label': label, 'months': nmonths})

@app.route('/api/kpi-marketing')
@login_required
def api_kpi_marketing():
    """KPI fungsi Marketing — team-wide (tidak per PIC). ROI pakai belanja iklan manual."""
    cfg = load_kpi_config()
    tgl_dari, tgl_sampai, _pic, divisi = get_args()

    def pdate(s):
        try: return datetime.strptime(s, '%Y-%m-%d').date()
        except Exception: return None
    d1, d2 = pdate(tgl_dari), pdate(tgl_sampai)
    nmonths = months_between(d1, d2) if (d1 and d2) else 1
    bands = cfg.get('scoring_bands', [])

    cond, params = build_where(tgl_dari, tgl_sampai, None, divisi)
    m = kpi_metrics(cond, params)
    omzet_new = m['omzet_new']  # non-repeat (konsisten dgn Ringkasan & tabel Customer)
    potensi = potensi_total(tgl_dari, tgl_sampai, None, divisi)
    qleads = qualified_leads_count(tgl_dari, tgl_sampai, divisi)
    adspend = sum_months(cfg.get('marketing_adspend', {}), d1, d2, nmonths, 0)

    rows, total_w, total_ach_w = [], 0.0, 0.0
    for k in cfg.get('marketing_kpi', []):
        basis, target, w = k['basis'], k['target'], k['weight'] / 100.0
        target_eff = target
        if basis == 'potensi_total':
            actual = potensi; target_eff = target * nmonths
        elif basis == 'qualified_leads':
            actual = qleads; target_eff = target * nmonths
        elif basis == 'roi_ads':
            actual = round(omzet_new / adspend, 1) if adspend > 0 else 0
        else:
            actual = 0
        ach = min(round(actual / target_eff * 100, 1), 100.0) if target_eff else 0
        sc = score_from_ach(ach, bands)
        weighted = round(sc * w, 2)
        total_w += weighted
        total_ach_w += ach * w
        rows.append({'id': k['id'], 'name': k['name'], 'weight': k['weight'],
                     'target': target_eff, 'unit': k.get('unit', ''), 'note': k.get('note', ''),
                     'actual': round(actual, 1) if isinstance(actual, float) else actual,
                     'ach': ach, 'score': sc, 'weighted': weighted})

    total_ach = round(total_ach_w, 1)
    label = next((lb for thr, lb in cfg.get('labels', []) if total_ach >= thr), '-')
    return jsonify({'rows': rows, 'total_kpi': round(total_w, 2), 'total_ach': total_ach,
                    'label': label, 'months': nmonths,
                    'adspend': adspend, 'omzet_new': omzet_new})

@app.route('/api/delivery')
@login_required
def api_delivery():
    """In Full Delivery — universe SAMA dengan On Time (proyek FAW, filter tgl_deadline).
       Qty dipesan: SUM(jumlah_produk) view. Qty dikirim: tb_surat_jalan_detail (kode_order=sko).
       In Full = qty dikirim >= qty dipesan."""
    tgl_dari, tgl_sampai, pic, divisi = get_args()

    # 1) FAW = universe (filter periode by tgl_deadline) — identik dengan /api/ontime
    fcond, fp = ["f.sko_key IS NOT NULL", "f.tgl_deadline IS NOT NULL"], []
    if tgl_dari:  fcond.append("f.tgl_deadline >= %s"); fp.append(tgl_dari)
    if tgl_sampai: fcond.append("f.tgl_deadline <= %s"); fp.append(tgl_sampai)
    faw = query(f"SELECT DISTINCT f.sko_key FROM tb_faws f WHERE {' AND '.join(fcond)}", fp)
    keys = [r['sko_key'] for r in faw]
    if not keys:
        return jsonify({'in_full': {'total': 0, 'in_full': 0, 'kurang': 0, 'belum': 0, 'pct': None},
                        'trend': [], 'by_type': [], 'by_vendor': [], 'rows': []})

    ph = ','.join(['%s'] * len(keys))
    spks = query(f"SELECT sko_key, MAX(vendor_ve) AS vendor FROM tb_spks "
                 f"WHERE sko_key IN ({ph}) GROUP BY sko_key", keys)
    sv_map = {r['sko_key']: r['vendor'] for r in spks}
    prod = query(f"SELECT sko_key, MAX(nama_produk) AS produk FROM tb_produksis "
                 f"WHERE sko_key IN ({ph}) AND nama_produk IS NOT NULL AND nama_produk <> '' "
                 f"GROUP BY sko_key", keys)
    pr_map = {r['sko_key']: r['produk'] for r in prod}

    vcond = [f"o.sko_key IN ({ph})"]; vp = list(keys)
    if pic:
        vcond.append("o.name = %s"); vp.append(pic)
    if divisi:
        vcond.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        vp.append(divisi)
    view = query(f"""
        SELECT o.sko_key, MAX(o.sko) AS sko, MAX(o.name) AS pic, MAX(o.jenis_bahan) AS jenis,
               MAX(TRIM(CONCAT(COALESCE(o.jenis_bahan,''),' ',COALESCE(o.nama_brand,'')))) AS produk,
               SUM(o.jumlah_produk) AS qty_dipesan
        FROM order_risepack o WHERE {' AND '.join(vcond)} GROUP BY o.sko_key
    """, vp)
    v_map = {v['sko_key']: v for v in view}

    sj = query("""
        SELECT sjd.kode_order AS sko, SUM(sjd.quantity) AS qty_dikirim, MAX(s.delivery_date) AS tgl_kirim
        FROM tb_surat_jalan_detail sjd
        JOIN tb_surat_jalan s ON s.surat_jalan_key = sjd.surat_jalan_key
        WHERE sjd.kode_order IS NOT NULL AND sjd.kode_order <> '-'
        GROUP BY sjd.kode_order
    """)
    sj_map = {r['sko']: r for r in sj}

    filtered = bool(pic or divisi)
    scope = list(v_map.keys()) if filtered else keys

    def to_date(v):
        if v is None: return None
        if isinstance(v, datetime): return v.date()
        if hasattr(v, 'year') and hasattr(v, 'month') and hasattr(v, 'day'): return v
        try: return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
        except Exception: return None

    if_total = if_full = if_kurang = if_belum = 0
    trend = {}
    by_type = {}
    by_vendor = {}
    rows = []
    for k in scope:
        v = v_map.get(k, {})
        sko = v.get('sko')
        qd = float(v.get('qty_dipesan') or 0)
        sjr = sj_map.get(sko) if sko else None
        qk = float(sjr['qty_dikirim']) if (sjr and sjr['qty_dikirim'] is not None) else None
        tk = to_date(sjr['tgl_kirim']) if sjr else None

        if_total += 1
        if qk is None or qk <= 0:
            st = 'Belum Dikirim'; if_belum += 1
        elif qd <= 0 or qk >= qd:
            st = 'In Full'; if_full += 1
        else:
            st = 'Kurang'; if_kurang += 1

        jn = (v.get('jenis') or '(lain)').strip() or '(lain)'
        bt = by_type.setdefault(jn, {'jenis': jn, 'total': 0, 'in_full': 0, 'kurang': 0, 'belum': 0})
        vn = (sv_map.get(k) or '(tanpa vendor)').strip() or '(tanpa vendor)'
        bv = by_vendor.setdefault(vn, {'vendor': vn, 'total': 0, 'in_full': 0, 'kurang': 0, 'belum': 0})
        bt['total'] += 1; bv['total'] += 1
        if st == 'In Full':
            bt['in_full'] += 1; bv['in_full'] += 1
        elif st == 'Kurang':
            bt['kurang'] += 1; bv['kurang'] += 1
        else:
            bt['belum'] += 1; bv['belum'] += 1

        if tk is not None:
            bl = tk.strftime('%Y-%m')
            t = trend.setdefault(bl, {'dikirim': 0, 'if_full': 0})
            t['dikirim'] += 1
            if st == 'In Full':
                t['if_full'] += 1

        rows.append({
            'sko': sko, 'pic': v.get('pic'), 'produk': pr_map.get(k) or v.get('produk'), 'vendor': sv_map.get(k),
            'qty_dipesan': qd, 'qty_dikirim': qk,
            'kurang': (qd - qk) if (qk is not None and qd > qk) else None,
            'tgl_kirim': fmt_date(sjr['tgl_kirim']) if sjr else None,
            'if_status': st,
        })

    trend_list = [{'bulan': b, 'if_pct': round(v['if_full'] / v['dikirim'] * 100, 1) if v['dikirim'] else None,
                   'dikirim': v['dikirim']} for b, v in sorted(trend.items())]
    by_type_list = sorted(by_type.values(), key=lambda x: -x['total'])[:12]
    by_vendor_list = sorted(by_vendor.values(), key=lambda x: -x['total'])[:12]

    return jsonify({
        'in_full': {'total': if_total, 'in_full': if_full, 'kurang': if_kurang, 'belum': if_belum,
                    'pct': round(if_full / if_total * 100, 1) if if_total else None},
        'trend': trend_list,
        'by_type': by_type_list,
        'by_vendor': by_vendor_list,
        'rows': rows[:3000],
    })

@app.route('/api/ontime')
@login_required
def api_ontime():
    """On Time PRODUKSI — meniru halaman 'Ontime' di app.
       Universe : proyek FAW (SKO ber-deadline), difilter berdasar tgl_deadline dalam periode.
       Deadline : tb_faws.tgl_deadline (produksi).
       Selesai  : tb_spks.tgl_selesai_all.
       On time  = tgl_selesai_all <= tgl_deadline. Belum selesai & lewat deadline = telat."""
    tgl_dari, tgl_sampai, pic, divisi = get_args()

    # 1) FAW = universe + deadline produksi (filter periode by tgl_deadline)
    fcond, fparams = ["f.sko_key IS NOT NULL", "f.tgl_deadline IS NOT NULL"], []
    if tgl_dari:  fcond.append("f.tgl_deadline >= %s"); fparams.append(tgl_dari)
    if tgl_sampai: fcond.append("f.tgl_deadline <= %s"); fparams.append(tgl_sampai)
    faw = query(f"""
        SELECT f.sko_key, MIN(f.tgl_deadline) AS deadline
        FROM tb_faws f
        WHERE {' AND '.join(fcond)}
        GROUP BY f.sko_key
    """, fparams)
    keys = [r['sko_key'] for r in faw]
    if not keys:
        return jsonify({'total': 0, 'ontime': 0, 'telat': 0, 'belum': 0,
                        'pct': None, 'avg_delay': None, 'trend': [], 'rows': []})

    ph = ','.join(['%s'] * len(keys))
    # 2) tb_spks = tanggal selesai (tgl_selesai_all) + vendor (vendor_ve) per sko_key
    spks = query(f"SELECT sko_key, MAX(tgl_selesai_all) AS tgl_selesai, MAX(vendor_ve) AS vendor "
                 f"FROM tb_spks WHERE sko_key IN ({ph}) GROUP BY sko_key", keys)
    sp_map = {r['sko_key']: r for r in spks}
    prod = query(f"SELECT sko_key, MAX(nama_produk) AS produk FROM tb_produksis "
                 f"WHERE sko_key IN ({ph}) AND nama_produk IS NOT NULL AND nama_produk <> '' "
                 f"GROUP BY sko_key", keys)
    pr_map = {r['sko_key']: r['produk'] for r in prod}

    # 3) label (SKO, produk, PIC) dari view + filter pic/divisi
    vcond = [f"o.sko_key IN ({ph})"]
    vparams = list(keys)
    if pic:
        vcond.append("o.name = %s"); vparams.append(pic)
    if divisi:
        vcond.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        vparams.append(divisi)
    try:
        view = query(f"""
            SELECT o.sko_key, MAX(o.sko) AS sko, MAX(o.name) AS pic,
                   MAX(o.jenis_bahan) AS jenis,
                   MAX(TRIM(CONCAT(COALESCE(o.jenis_bahan,''),' ',COALESCE(o.nama_brand,'')))) AS produk
            FROM order_risepack o
            WHERE {' AND '.join(vcond)}
            GROUP BY o.sko_key
        """, vparams)
    except Exception as e:
        return jsonify({'_error': str(e), 'step': 'view'}), 200
    v_map = {r['sko_key']: r for r in view}
    filtered = bool(pic or divisi)

    today = datetime.now().date()
    def to_date(v):
        if v is None: return None
        if isinstance(v, datetime): return v.date()
        if hasattr(v, 'year') and hasattr(v, 'month') and hasattr(v, 'day'): return v
        try: return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
        except Exception: return None

    total = ontime = telat = belum = 0
    delay_sum = delay_n = 0
    trend = {}
    by_type = {}
    by_vendor = {}
    rows = []
    for f in faw:
        k = f['sko_key']
        if filtered and k not in v_map:
            continue
        sp = sp_map.get(k) or {}
        dl = to_date(f['deadline'])
        sel = to_date(sp.get('tgl_selesai'))
        v = v_map.get(k, {})
        total += 1
        counted = True
        if sel is not None:
            if sel <= dl:
                status = 'On Time'; ontime += 1
            else:
                status = 'Terlambat'; telat += 1
                delay_sum += (sel - dl).days; delay_n += 1
        else:
            if dl is not None and dl < today:
                status = 'Terlambat (belum selesai)'; telat += 1
            else:
                status = 'Belum Jatuh Tempo'; belum += 1; counted = False

        jn = (v.get('jenis') or '(lain)').strip() or '(lain)'
        bt = by_type.setdefault(jn, {'jenis': jn, 'total': 0, 'ontime': 0, 'telat': 0, 'belum': 0})
        vn = (sp.get('vendor') or '(tanpa vendor)').strip() or '(tanpa vendor)'
        bv = by_vendor.setdefault(vn, {'vendor': vn, 'total': 0, 'ontime': 0, 'telat': 0, 'belum': 0})
        bt['total'] += 1; bv['total'] += 1
        if status == 'On Time':
            bt['ontime'] += 1; bv['ontime'] += 1
        elif status.startswith('Terlambat'):
            bt['telat'] += 1; bv['telat'] += 1
        else:
            bt['belum'] += 1; bv['belum'] += 1
        if dl is not None and counted:
            b = dl.strftime('%Y-%m')
            t = trend.setdefault(b, {'n': 0, 'ot': 0})
            t['n'] += 1
            if status == 'On Time':
                t['ot'] += 1
        rows.append({
            'sko': v.get('sko'), 'pic': v.get('pic'), 'produk': pr_map.get(k) or v.get('produk'),
            'vendor': sp.get('vendor'),
            'deadline': fmt_date(f['deadline']), 'tgl_selesai': fmt_date(sp.get('tgl_selesai')),
            'status': status,
            'delay': ((sel - dl).days if (sel and dl and sel > dl) else None),
        })

    trend_list = [{'bulan': b, 'ot_pct': round(v['ot'] / v['n'] * 100, 1) if v['n'] else None,
                   'n': v['n']} for b, v in sorted(trend.items())]

    by_type_list = sorted(by_type.values(), key=lambda x: -x['total'])[:12]
    by_vendor_list = sorted(by_vendor.values(), key=lambda x: -x['total'])[:12]

    return jsonify({
        'total': total, 'ontime': ontime, 'telat': telat, 'belum': belum,
        'pct': round(ontime / (ontime + telat) * 100, 1) if (ontime + telat) else None,
        'avg_delay': round(delay_sum / delay_n, 1) if delay_n else None,
        'trend': trend_list,
        'by_type': by_type_list,
        'by_vendor': by_vendor_list,
        'rows': rows[:3000],
    })

@app.route('/api/overview')
@login_required
def api_overview():
    """Produksi Overview: total pcs per jenis + SPK (tuntas/berjalan).
       Universe = proyek FAW (deadline dalam periode). Selesai = tb_spks.tgl_selesai_all."""
    tgl_dari, tgl_sampai, pic, divisi = get_args()

    fcond, fp = ["f.sko_key IS NOT NULL", "f.tgl_deadline IS NOT NULL"], []
    if tgl_dari:  fcond.append("f.tgl_deadline >= %s"); fp.append(tgl_dari)
    if tgl_sampai: fcond.append("f.tgl_deadline <= %s"); fp.append(tgl_sampai)
    faw = query(f"SELECT DISTINCT f.sko_key FROM tb_faws f WHERE {' AND '.join(fcond)}", fp)
    keys = [r['sko_key'] for r in faw]
    if not keys:
        return jsonify({'total_pcs': 0, 'by_jenis': [],
                        'spk': {'total': 0, 'tuntas': 0, 'berjalan': 0}})

    ph = ','.join(['%s'] * len(keys))
    spks = query(f"SELECT sko_key, MAX(tgl_selesai_all) AS sel FROM tb_spks "
                 f"WHERE sko_key IN ({ph}) GROUP BY sko_key", keys)
    sp_map = {r['sko_key']: r['sel'] for r in spks}

    vcond = [f"o.sko_key IN ({ph})"]
    vp = list(keys)
    if pic:
        vcond.append("o.name = %s"); vp.append(pic)
    if divisi:
        vcond.append("o.order_key IN (SELECT DISTINCT order_key FROM tb_orders WHERE sub_division = %s)")
        vp.append(divisi)
    view = query(f"""
        SELECT o.sko_key, MAX(o.jenis_bahan) AS jenis, SUM(o.jumlah_produk) AS pcs
        FROM order_risepack o WHERE {' AND '.join(vcond)} GROUP BY o.sko_key
    """, vp)
    pcs_by_key = {v['sko_key']: float(v['pcs'] or 0) for v in view}
    jenis_by_key = {v['sko_key']: ((v['jenis'] or '(lain)').strip() or '(lain)') for v in view}

    scope = list(pcs_by_key.keys()) if (pic or divisi) else keys
    by_jenis = {}
    total_pcs = spk_total = tuntas = berjalan = 0
    for k in scope:
        spk_total += 1
        if sp_map.get(k) is not None:
            tuntas += 1
        else:
            berjalan += 1
        pcs = pcs_by_key.get(k, 0.0)
        total_pcs += pcs
        jn = jenis_by_key.get(k, '(lain)')
        by_jenis[jn] = by_jenis.get(jn, 0.0) + pcs

    by_jenis_list = sorted(({'jenis': k, 'pcs': v} for k, v in by_jenis.items()),
                           key=lambda x: -x['pcs'])
    return jsonify({
        'total_pcs': total_pcs, 'by_jenis': by_jenis_list,
        'spk': {'total': spk_total, 'tuntas': tuntas, 'berjalan': berjalan},
    })


# ─── Reject Finance (sumber: Google Sheet published CSV) ─────────
_REJECT_CACHE = {'ts': 0.0, 'rows': None, 'url': None}

def _fetch_reject_rows(url):
    import time, urllib.request, csv, io
    now = time.time()
    if (_REJECT_CACHE['rows'] is not None and _REJECT_CACHE['url'] == url
            and now - _REJECT_CACHE['ts'] < 120):
        return _REJECT_CACHE['rows']
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode('utf-8', errors='replace')
    rows = list(csv.DictReader(io.StringIO(text)))
    _REJECT_CACHE.update(ts=now, rows=rows, url=url)
    return rows

def _num(s):
    s = str(s or '').replace('Rp', '').replace(',', '').replace(' ', '').strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0

_RJ_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7,
              'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
              'mei': 5, 'agu': 8, 'ags': 8, 'agt': 8, 'okt': 10, 'des': 12}

def _reject_date(s):
    """Parse 'DD Mon YYYY' (mis. '05 Jan 2026' / '20 May 2026') -> date."""
    p = str(s or '').strip().split()
    if len(p) < 3:
        return None
    try:
        m = _RJ_MONTHS.get(p[1][:3].lower())
        if not m:
            return None
        return datetime(int(p[2]), m, int(p[0])).date()
    except Exception:
        return None

def _pdate(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except Exception:
        return None

@app.route('/api/reject')
@login_required
def api_reject():
    cfg = load_kpi_config()
    url = cfg.get('reject_csv_url')
    if not url:
        return jsonify({'_error': 'URL CSV reject belum diisi di kpi_config.json (reject_csv_url).'}), 200
    try:
        raw = _fetch_reject_rows(url)
    except Exception as e:
        return jsonify({'_error': f'Gagal membaca Google Sheet: {e}'}), 200

    tgl_dari, tgl_sampai, _pic, _div = get_args()
    d1, d2 = _pdate(tgl_dari), _pdate(tgl_sampai)

    total = 0.0
    count = 0
    by_pj, by_pic, by_jenis = {}, {}, {}
    rows = []
    for r in raw:
        debit = _num(r.get('Debit'))
        if debit <= 0:
            continue
        # Filter periode berdasar kolom Tanggal (lewati yang di luar rentang; yg tak terbaca tetap ikut)
        rd = _reject_date(r.get('Tanggal'))
        if rd is not None:
            if d1 and rd < d1:
                continue
            if d2 and rd > d2:
                continue
        pj = (r.get('Penanggung Jawab') or '').strip() or '(kosong)'
        pic = (r.get('PIC') or '').strip() or '(kosong)'
        jn = (r.get('Jenis Reject') or '').strip() or '(kosong)'
        total += debit
        count += 1
        by_pj[pj] = by_pj.get(pj, 0.0) + debit
        by_pic[pic] = by_pic.get(pic, 0.0) + debit
        by_jenis[jn] = by_jenis.get(jn, 0.0) + debit
        rows.append({
            'tanggal': (r.get('Tanggal') or '').strip(),
            'kode_order': (r.get('Kode Order') or '').strip(),
            'produk': (r.get('Nama Produk') or '').strip(),
            'jenis': jn, 'pj': pj, 'pic': pic, 'debit': debit,
        })

    def tolist(d, n=None):
        lst = sorted(({'label': k, 'cost': v} for k, v in d.items()), key=lambda x: -x['cost'])
        return lst[:n] if n else lst

    rows.sort(key=lambda x: -x['debit'])
    return jsonify({
        'total_cost': total, 'count': count,
        'by_pj': tolist(by_pj),
        'by_pic': tolist(by_pic, 12),
        'by_jenis': tolist(by_jenis, 12),
        'rows': rows[:3000],
    })


# ─── Financial Statement (P&L PT BBI dari Google Sheet published CSV) ───
_FIN_CACHE = {'ts': 0.0, 'rows': None, 'url': None}

def _fetch_csv_grid(url):
    import time, urllib.request, csv, io
    now = time.time()
    if _FIN_CACHE['rows'] is not None and _FIN_CACHE['url'] == url and now - _FIN_CACHE['ts'] < 120:
        return _FIN_CACHE['rows']
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode('utf-8', errors='replace')
    rows = list(csv.reader(io.StringIO(text)))
    _FIN_CACHE.update(ts=now, rows=rows, url=url)
    return rows

@app.route('/api/financial')
@login_required
def api_financial():
    cfg = load_kpi_config()
    url = cfg.get('financial_csv_url')
    if not url:
        return jsonify({'_error': 'financial_csv_url belum diisi di kpi_config.json.'}), 200
    try:
        grid = _fetch_csv_grid(url)
    except Exception as e:
        return jsonify({'_error': f'Gagal membaca Google Sheet P&L: {e}'}), 200

    def cell(r, i):
        return r[i].strip() if r is not None and i < len(r) else ''

    MONTHS = [('January', 'Jan'), ('February', 'Feb'), ('March', 'Mar'), ('April', 'Apr'),
              ('May', 'Mei'), ('June', 'Jun'), ('July', 'Jul'), ('August', 'Agu'),
              ('September', 'Sep'), ('October', 'Okt'), ('November', 'Nov'), ('December', 'Des')]

    # 1) baris header bulan + sub-header Invoice/PO
    hdr = sub = None
    for i, r in enumerate(grid):
        if any(str(c).strip().startswith('January 2026') for c in r):
            hdr = r
            for j in range(i, min(i + 4, len(grid))):
                if any(str(c).strip() == 'Invoice' for c in grid[j]):
                    sub = grid[j]
                    break
            break
    if hdr is None or sub is None:
        return jsonify({'_error': 'Header bulan / Invoice-PO tidak ditemukan di sheet.'}), 200

    # 2) map bulan -> (kolom Invoice, kolom PO) — pakai kemunculan pertama yg sub-header-nya Invoice
    mcol = {}
    for c, cv in enumerate(hdr):
        cvs = str(cv).strip()
        for en, ab in MONTHS:
            if cvs.startswith(en + ' 2026') and ab not in mcol and cell(sub, c) == 'Invoice':
                mcol[ab] = (c, c + 1)

    # 3) baris metrik (cari berdasar label — tahan geser baris)
    def find(label):
        for r in grid:
            if cell(r, 0) == label or cell(r, 1) == label:
                return r
        return None
    r_rev = find('Total dari Revenue')
    r_cogs = find('Total dari Cost of Sales')
    r_ebitda = find('EBITDA')
    r_net = find('EBIT')
    miss = [n for n, r in [('Total dari Revenue', r_rev), ('Total dari Cost of Sales', r_cogs),
                           ('EBITDA', r_ebitda), ('EBIT', r_net)] if r is None]
    if miss:
        return jsonify({'_error': 'Baris tidak ditemukan: ' + ', '.join(miss)}), 200

    def blk(col):
        rev = _num(cell(r_rev, col)); cogs = _num(cell(r_cogs, col))
        gp = rev - cogs; eb = _num(cell(r_ebitda, col)); net = _num(cell(r_net, col))
        return {'revenue': rev, 'cogs': cogs, 'gross_profit': gp, 'ebitda': eb, 'net_profit': net,
                'gpm': round(gp / rev * 100, 1) if rev else None,
                'npm': round(net / rev * 100, 1) if rev else None}

    months = []
    for en, ab in MONTHS:
        if ab not in mcol:
            continue
        ci, cp = mcol[ab]
        months.append({'bulan': ab, 'invoice': blk(ci), 'po': blk(cp)})

    def totals(which):
        t = {'revenue': 0.0, 'cogs': 0.0, 'gross_profit': 0.0, 'ebitda': 0.0, 'net_profit': 0.0}
        for x in months:
            for k in t:
                t[k] += x[which][k]
        t['gpm'] = round(t['gross_profit'] / t['revenue'] * 100, 1) if t['revenue'] else None
        t['npm'] = round(t['net_profit'] / t['revenue'] * 100, 1) if t['revenue'] else None
        return t

    return jsonify({'months': months,
                    'total_invoice': totals('invoice'),
                    'total_po': totals('po'),
                    'targets': cfg.get('financial_targets', {})})


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
