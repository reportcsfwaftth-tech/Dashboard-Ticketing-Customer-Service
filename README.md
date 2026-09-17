# Dashboard KPI Tiket CS IRA — dashboard dengan data yang refresh otomatis

Dashboard KPI tiket customer service (Layer 1 & Layer 2, sheet "IRA") yang
datanya ditarik ulang otomatis dari Google Sheet sumbernya secara berkala,
lewat GitHub Actions — tidak perlu generate ulang HTML manual setiap ada
perubahan di spreadsheet.

## Cara kerjanya

```
Google Sheet, tab "IRA" (Publish to web → CSV)
        │  (dijadwalkan tiap 6 jam, atau tombol "Run workflow")
        ▼
scripts/refresh_data.py   → membersihkan data, hitung ulang KPI & leaderboard
        │
        ▼
data/bundle.json          → di-commit balik ke repo oleh GitHub Actions
        │
        ▼
index.html                → fetch('./data/bundle.json') saat halaman dibuka
```

`index.html` tidak menyimpan data di dalam file HTML-nya — ia selalu memuat
`data/bundle.json` saat dibuka, sehingga begitu file itu diperbarui oleh
GitHub Actions, dashboard yang live otomatis menampilkan angka terbaru tanpa
perlu redeploy manual.

## Setup sekali di awal

1. **Buat repository baru di GitHub** (Public — GitHub Pages gratis hanya
   untuk repo public, kecuali Anda punya GitHub Pro/Team/Enterprise).
2. **Upload seluruh isi folder ini** ke repo tsb (drag semua file lewat
   "Add file → Upload files" di web, atau lewat git — lihat bagian bawah).
   Struktur yang harus persis sama:
   ```
   index.html
   requirements.txt
   scripts/refresh_data.py
   .github/workflows/refresh-data.yml
   data/bundle.json          (isi awal sudah disertakan — akan ditimpa otomatis)
   ```
3. **Nyalakan GitHub Pages**: Settings → Pages → Source: *Deploy from a
   branch* → Branch: `main`, folder `/ (root)` → Save. Setelah 1–2 menit,
   URL live-nya muncul di halaman yang sama:
   `https://<username>.github.io/<nama-repo>/`
4. **(Opsional tapi disarankan) Set URL sumber lewat repo variable**, supaya
   tidak perlu edit kode kalau link Google Sheet-nya berubah:
   Settings → Secrets and variables → Actions → tab **Variables** → *New
   repository variable* → Name: `SOURCE_CSV_URL`, Value: link "Publish to
   web" CSV untuk tab **IRA** dari Google Sheet Anda (lihat langkah
   berikut). Kalau variable ini tidak diisi, script memakai URL default
   yang sudah tertanam di `scripts/refresh_data.py` (link yang sama yang
   Anda berikan sebelumnya).
5. **Pastikan tab "IRA" di Google Sheet sudah "Published to web" sebagai
   CSV**: di Google Sheets, File → Share → **Publish to web** → pilih sheet
   **IRA** (bukan "Seluruh dokumen") → format **Comma-separated values
   (.csv)** → Publish. **Centang "Automatically republish when changes are
   made"** — kalau opsi ini mati, editan baru di sheet tidak akan pernah
   sampai ke dashboard.
6. **Jalankan refresh pertama secara manual**: tab **Actions** di repo →
   pilih workflow **Refresh dashboard data** → **Run workflow**. Setelah
   selesai (biasanya <1 menit), cek `data/bundle.json` ter-commit dengan
   timestamp baru, lalu buka URL Pages-nya.

Setelah langkah di atas, workflow berjalan sendiri setiap 6 jam (bisa
diubah — lihat di bawah), dan setiap kali berhasil ambil data baru dari
sheet, dashboard yang live otomatis ikut ter-update.

## Mengubah jadwal refresh

Edit baris `cron` di `.github/workflows/refresh-data.yml`:
```yaml
schedule:
  - cron: "0 */6 * * *"   # tiap 6 jam. Contoh lain:
                          # "0 * * * *"     -> tiap jam
                          # "0 8 * * *"     -> tiap hari jam 08:00 UTC
```
GitHub Actions memakai UTC — WIB = UTC+7. Jadwal cron minimum yang dijamin
GitHub adalah setiap 5 menit, tapi jadwal terlalu sering bisa telat
dieksekusi saat traffic Actions sedang padat; tiap 6 jam sudah cukup untuk
data harian.

Anda juga bisa memicu refresh kapan saja tanpa menunggu jadwal: tab
**Actions → Refresh dashboard data → Run workflow**.

## Kalau kolom di Google Sheet Anda berbeda nama

Script ini dites terhadap struktur kolom sheet "IRA" seperti data Jul–Agu
2026 (`Unique Ticket ID, Stage, Ticket Priority, Source, Escalation to,
Assignee, Reporter, Distributor IRA, Area IRA, Status Pelanggan IRA,
Category Ticket, Created At, Updated At`, dst). Kalau sheet Anda memakai
nama kolom lain, edit bagian `COLUMN_CANDIDATES` di
`scripts/refresh_data.py` — tambahkan nama kolom Anda ke list yang sesuai.
Kalau ada kolom wajib (`Unique Ticket ID`, `Created At`, `Updated At`) yang
tidak ketemu, workflow akan gagal dengan pesan error yang menyebutkan kolom
apa yang dicari dan kolom apa saja yang benar-benar ada di sheet Anda — cek
log-nya di tab Actions.

Akun bersama/generik yang dikecualikan dari peringkat individu saat ini
(`SHARED_ASSIGNEE` / `SHARED_REPORTER` di script yang sama): `Helpdesk
NOC`, `CS IRA LB Satu`. Tambahkan/ubah daftar ini kalau ada akun bersama
baru di tim Anda.

## Menjalankan & mengetes secara lokal

```bash
pip install -r requirements.txt
export SOURCE_CSV_URL="https://.../pub?gid=...&single=true&output=csv"   # opsional, pakai default kalau kosong
python3 scripts/refresh_data.py
python3 -m http.server 8000     # lalu buka http://localhost:8000/
```
`index.html` memakai `fetch()`, jadi harus dibuka lewat server (seperti di
atas) — membuka file HTML-nya langsung (`file://…`) akan gagal memuat data
karena browser memblokir fetch lokal semacam itu.

## Privasi

Repo GitHub Pages gratis bersifat **public** — siapa pun yang tahu URL-nya
(termasuk `data/bundle.json` mentah) bisa mengaksesnya. Bundle ini hanya
berisi agregat (bukan detail tiket per baris seperti deskripsi/nomor
pelanggan), tapi tetap memuat nama-nama assignee & reporter. Kalau ini
sensitif, gunakan GitHub Pro/Team/Enterprise agar bisa memakai repo
**private** dengan Pages, atau tambahkan lapisan proteksi akses lain di
luar GitHub Pages.

## Struktur file

| Path | Isi |
|---|---|
| `index.html` | Dashboard-nya sendiri — HTML/CSS/JS satu file, tanpa data tertanam |
| `data/bundle.json` | KPI & agregat hasil bersih, di-generate ulang oleh workflow |
| `scripts/refresh_data.py` | Pipeline: download CSV tab IRA → bersihkan → hitung KPI, tren, leaderboard → tulis bundle.json |
| `.github/workflows/refresh-data.yml` | Jadwal & langkah otomatisasi di GitHub Actions |
| `requirements.txt` | Dependensi Python untuk `refresh_data.py` |
