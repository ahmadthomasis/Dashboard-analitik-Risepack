# Dashboard Analitik Risepack

Dashboard custom pengganti Looker Studio, dibangun dengan Python Flask + Chart.js.

## Struktur File

```
risepack-dashboard/
├── app.py              ← Backend utama (Python Flask)
├── requirements.txt    ← Daftar library Python
├── Procfile            ← Konfigurasi untuk Railway/Render
├── .env.example        ← Template konfigurasi (JANGAN hapus)
├── .env                ← Konfigurasi asli (JANGAN di-commit ke GitHub)
└── templates/
    ├── login.html      ← Halaman login
    └── dashboard.html  ← Halaman dashboard utama
```

## Setup Awal (sudah dilakukan developer)

1. Clone repository ini
2. Copy `.env.example` → `.env`
3. Isi kredensial database dan password login di `.env`
4. Deploy ke Railway

## Menambah Fitur Baru

Semua pengembangan dilakukan via Claude Project.
Setelah Claude tulis code → push ke GitHub → Railway otomatis deploy ulang.
