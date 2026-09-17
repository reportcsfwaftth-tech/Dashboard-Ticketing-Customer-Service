#!/usr/bin/env python3
"""
Refresh the CS Ticket (IRA L1 & L2) dashboard's data bundle.

Downloads the published Google Sheet tab (CSV export), cleans it using the
same rules validated for the Jul-Aug 2026 dataset, computes rollup KPIs
(volume trend, resolution time, category/priority/source/area breakdowns,
assignee & reporter leaderboards) and writes data/bundle.json — the single
file the dashboard (index.html) fetches at load time.

Run manually:
    python3 scripts/refresh_data.py

Configure the source with the SOURCE_CSV_URL env var, or edit the default
below. This must be the "Publish to web" CSV link for the specific IRA tab:
    Google Sheets > File > Share > Publish to web > select sheet "IRA" >
    Comma-separated values (.csv) > Publish
Make sure "Automatically republish when changes are made" is checked,
otherwise edits to the sheet won't reach this script.
"""
import os
import re
import sys
import io
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, date, timezone

import requests
import pandas as pd

DEFAULT_SOURCE_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRxd_CY-nEurQsEkI_lLqgBO6mUPjr5T1EHuJPw9hmD4a5XlkEcdD2Q4gYU3IwnLODeSbJkd9rqcJzx/"
    "pub?gid=237809633&single=true&output=csv"
)
# os.environ.get(..., default) only falls back when the key is ABSENT — a
# GitHub Actions repo variable that exists but is left blank still sets the
# key to "", so blank is explicitly treated as "use the default" too.
SOURCE_CSV_URL = os.environ.get("SOURCE_CSV_URL") or DEFAULT_SOURCE_CSV_URL

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bundle.json")

# Flexible column matching: a few plausible header spellings per field,
# matched case-insensitively after stripping whitespace. First match wins.
COLUMN_CANDIDATES = {
    "ticket_id": ["unique ticket id", "ticket id"],
    "stage": ["stage"],
    "priority": ["ticket priority", "priority"],
    "source": ["source"],
    "escalation_to": ["escalation to"],
    "assignee": ["assignee"],
    "reporter": ["reporter"],
    "distributor": ["distributor ira", "distributor"],
    "area": ["area ira", "area"],
    "status_pelanggan": ["status pelanggan ira", "status pelanggan"],
    "category": ["category ticket", "category"],
    "subcategory": ["sub category ira", "sub category"],
    "brand_cpe": ["brand cpe"],
    "jarak_bts": ["jarak bts"],
    "created_at": ["created at"],
    "updated_at": ["updated at"],
}

SHARED_ASSIGNEE = {"Helpdesk NOC", "CS IRA LB Satu"}
SHARED_REPORTER = {"CS IRA LB Satu"}


def log(msg):
    print(f"[refresh_data] {msg}", file=sys.stderr)


def find_column(df, candidates, required=True):
    norm = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand in norm:
            return norm[cand]
    if required:
        raise SystemExit(
            f"Could not find a column matching any of {candidates!r}.\n"
            f"Columns actually present in the sheet: {list(df.columns)!r}\n"
            f"Fix COLUMN_CANDIDATES in scripts/refresh_data.py to match your sheet's headers."
        )
    return None


def fetch_csv(url):
    log(f"Downloading {url}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    text = resp.text
    if "<html" in text[:200].lower():
        raise SystemExit(
            "The URL returned an HTML page, not CSV — the sheet is probably not "
            "published, or 'Publish to web' needs re-enabling for this tab."
        )
    return pd.read_csv(io.StringIO(text))


def norm_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def parse_dt(v):
    s = norm_str(v)
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def norm_category(v):
    v = norm_str(v) or "Tidak diisi"
    return re.sub(r"^\d+\.\s*", "", v).strip()


def norm_status(v):
    v = norm_str(v)
    if v is None:
        return "Tidak diisi"
    s = v.strip().lower()
    s = re.sub(r"^status:\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    aktif_variants = {"aktif", "active", "akif", "altif", "aktiuf", "aktfi"}
    if s in aktif_variants:
        return "Aktif"
    if "isolir" in s or "siolir" in s:
        return "Isolir"
    if "pengiriman" in s:
        return "Dalam/Menunggu Pengiriman"
    if "pembayaran" in s and ("pertama" in s or "peratama" in s):
        return "Menunggu Pembayaran Pertama"
    if "diterima" in s:
        return "Diterima Pelanggan"
    if "potensial" in s:
        return "Pelanggan Potensial"
    if "dismantle" in s:
        return "Dismantle"
    if "dibatalkan" in s:
        return "Dibatalkan"
    if "tidak tercover" in s:
        return "Tidak Tercover"
    if s == "-":
        return "Tidak diisi"
    if s.startswith("pt ") or s.startswith("cv ") or s.startswith("dist"):
        return "Tidak diisi"  # data-entry error: a distributor name pasted into this field
    if s == "offline":
        return "Offline"
    return v.strip().title()


def clean_rows(df):
    col = {k: find_column(df, v, required=False) for k, v in COLUMN_CANDIDATES.items()}
    missing_required = [k for k in ("ticket_id", "created_at", "updated_at") if not col[k]]
    if missing_required:
        raise SystemExit(f"Missing required column(s): {missing_required}. Columns present: {list(df.columns)!r}")

    clean = []
    for _, r in df.iterrows():
        tid = norm_str(r[col["ticket_id"]])
        if not tid:
            continue
        created = parse_dt(r[col["created_at"]])
        updated = parse_dt(r[col["updated_at"]]) if col["updated_at"] else None
        dur_h = None
        if created and updated:
            delta = (updated - created).total_seconds() / 3600.0
            if delta >= 0:
                dur_h = round(delta, 3)
        clean.append({
            "id": tid,
            "priority": norm_str(r[col["priority"]]) if col["priority"] else None or "Tidak diisi",
            "source": (norm_str(r[col["source"]]) if col["source"] else None) or "Tidak diisi",
            "escalation_to": (norm_str(r[col["escalation_to"]]) if col["escalation_to"] else None) or "Tidak diisi",
            "assignee": norm_str(r[col["assignee"]]) if col["assignee"] else None,
            "reporter": norm_str(r[col["reporter"]]) if col["reporter"] else None,
            "distributor": (norm_str(r[col["distributor"]]) if col["distributor"] else None) or "Tidak diisi",
            "area": (norm_str(r[col["area"]]) if col["area"] else None) or "Tidak diisi",
            "status_pelanggan": norm_status(r[col["status_pelanggan"]]) if col["status_pelanggan"] else "Tidak diisi",
            "category": norm_category(r[col["category"]]) if col["category"] else "Tidak diisi",
            "created_date": created.date().isoformat() if created else None,
            "dur_h": dur_h,
        })
    return clean


def dist(clean, field, other_label="Lainnya", top=8):
    c = Counter(r[field] for r in clean)
    total = sum(c.values()) or 1
    items = c.most_common()
    head = items[:top]
    rest_items = items[top:]
    out = [{"label": k, "count": v, "pct": round(v / total * 100, 1)} for k, v in head]
    rest = sum(v for _, v in rest_items)
    if rest > 0:
        merged = False
        for entry in out:
            if entry["label"] == other_label:
                entry["count"] += rest
                entry["pct"] = round(entry["count"] / total * 100, 1)
                merged = True
                break
        if not merged:
            out.append({"label": other_label, "count": rest, "pct": round(rest / total * 100, 1)})
    return out


def isoweek(d):
    dt = date.fromisoformat(d)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def leaderboard(clean, field, exclude, min_n=3, top=10):
    grp = defaultdict(list)
    for r in clean:
        v = r[field]
        if not v or v in exclude:
            continue
        grp[v].append(r["dur_h"])
    board = []
    for k, durs in grp.items():
        durs2 = [d for d in durs if d is not None]
        board.append({
            "name": k,
            "count": len(durs),
            "median_h": round(statistics.median(durs2), 2) if len(durs2) >= min_n else None,
            "mean_h": round(statistics.mean(durs2), 2) if len(durs2) >= min_n else None,
        })
    board.sort(key=lambda x: -x["count"])
    return board[:top]


def build_bundle(clean):
    n = len(clean)
    if n == 0:
        raise SystemExit("No usable rows found after cleaning — check the sheet/columns.")

    dated = [r for r in clean if r["created_date"]]
    daily = Counter(r["created_date"] for r in dated)
    days = sorted(daily.keys())
    daily_trend = [{"date": d, "count": daily[d]} for d in days]

    weekly = Counter(isoweek(r["created_date"]) for r in dated)
    weeks = sorted(weekly.keys())
    weekly_trend = [{"week": w, "count": weekly[w]} for w in weeks]
    if weekly_trend:
        # Flag the last week as partial if it doesn't run through a Sunday
        # that's on/after the last observed date's own week-end.
        last_date = date.fromisoformat(days[-1])
        _, _, last_dow = last_date.isocalendar()
        if last_dow < 7:
            weekly_trend[-1]["partial"] = True

    durs = [r["dur_h"] for r in clean if r["dur_h"] is not None]
    res_overall = {
        "median_h": round(statistics.median(durs), 2) if durs else None,
        "mean_h": round(statistics.mean(durs), 2) if durs else None,
        "p90_h": round(statistics.quantiles(durs, n=10)[8], 2) if len(durs) >= 10 else None,
        "min_h": round(min(durs), 2) if durs else None,
        "max_h": round(max(durs), 2) if durs else None,
        "n": len(durs),
    }

    cat_durs = defaultdict(list)
    for r in clean:
        if r["dur_h"] is not None:
            cat_durs[r["category"]].append(r["dur_h"])
    cat_res = [
        {"label": k, "median_h": round(statistics.median(v), 2), "mean_h": round(statistics.mean(v), 2), "n": len(v)}
        for k, v in cat_durs.items() if len(v) >= 5
    ]
    cat_res.sort(key=lambda x: -x["n"])

    pri_durs = defaultdict(list)
    for r in clean:
        if r["dur_h"] is not None:
            pri_durs[r["priority"] or "Tidak diisi"].append(r["dur_h"])
    pri_res = [
        {"label": k, "median_h": round(statistics.median(v), 2), "mean_h": round(statistics.mean(v), 2), "n": len(v)}
        for k, v in pri_durs.items()
    ]
    pri_res.sort(key=lambda x: -x["n"])

    months = sorted(set(d[:7] for d in days)) if days else []
    mom = []
    for m in months:
        sub = [r for r in clean if r["created_date"] and r["created_date"][:7] == m]
        d = [r["dur_h"] for r in sub if r["dur_h"] is not None]
        mom.append({"month": m, "count": len(sub), "median_h": round(statistics.median(d), 2) if d else None})

    bundle = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sourceUrl": SOURCE_CSV_URL,
        "meta": {
            "total_tickets": n,
            "date_start": days[0] if days else None,
            "date_end": days[-1] if days else None,
            "generated_at": date.today().isoformat(),
            "source_name": "Dashboard AI - Data Ticket Customer Service L1 & L2 (sheet: IRA)",
        },
        "daily_trend": daily_trend,
        "weekly_trend": weekly_trend,
        "month_over_month": mom,
        "resolution": {"overall": res_overall, "by_category": cat_res, "by_priority": pri_res},
        "dist": {
            "priority": dist(clean, "priority", top=6),
            "source": dist(clean, "source", top=6),
            "escalation_to": dist(clean, "escalation_to", top=6),
            "category": dist(clean, "category", top=8),
            "area": dist(clean, "area", top=8),
            "status_pelanggan": dist(clean, "status_pelanggan", top=6),
        },
        "leaderboards": {
            "assignee": leaderboard(clean, "assignee", SHARED_ASSIGNEE),
            "reporter": leaderboard(clean, "reporter", SHARED_REPORTER),
            "excluded_assignee_count": sum(1 for r in clean if r["assignee"] in SHARED_ASSIGNEE),
            "excluded_reporter_count": sum(1 for r in clean if r["reporter"] in SHARED_REPORTER),
            "excluded_assignee_names": sorted(SHARED_ASSIGNEE),
            "excluded_reporter_names": sorted(SHARED_REPORTER),
        },
        "distributor_top": dist(clean, "distributor", top=10),
    }
    return bundle


def main():
    df = fetch_csv(SOURCE_CSV_URL)
    log(f"Loaded {len(df)} raw rows, columns: {list(df.columns)}")
    clean = clean_rows(df)
    log(f"Usable rows after cleaning: {len(clean)} / {len(df)}")
    bundle = build_bundle(clean)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Wrote {OUT_PATH} ({os.path.getsize(OUT_PATH)} bytes)")


if __name__ == "__main__":
    main()
