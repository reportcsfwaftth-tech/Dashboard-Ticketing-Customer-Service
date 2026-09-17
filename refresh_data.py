#!/usr/bin/env python3
"""
Refresh the CS performance dashboard's data bundle.

Downloads the published Google Sheet (CSV export), cleans it using the same
rules validated for the Agustus 2026 dataset, computes rollup stats and a
few auto-generated insights, and writes data/bundle.json — the single file
the dashboard (index.html) fetches at load time.

Run manually:
    python3 scripts/refresh_data.py

Configure the source with the SOURCE_CSV_URL env var, or edit the default
below. This must be a Google Sheet published as CSV:
    File > Share > Publish to web > select the sheet > CSV > Publish
Make sure "Automatically republish when changes are made" is checked in
that dialog, otherwise edits to the sheet won't reach this script.
"""
import os
import re
import sys
import json
import io
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

# os.environ.get(..., default) only falls back when the key is ABSENT — a
# GitHub Actions repo variable that exists but is left blank still sets the
# key to "", so we explicitly treat blank as "use the default" too.
DEFAULT_SOURCE_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSADjWD_rJGxiuAkzKjQUKAgWTqm8cwv-OKQoAVWr5ZLRYx5iMKqkR93F686EnoPedpypDe6vCb2agg/pub?output=csv"
SOURCE_CSV_URL = os.environ.get("SOURCE_CSV_URL") or DEFAULT_SOURCE_CSV_URL

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bundle.json")

# Brand string (as it appears in the sheet) -> (Company, ServiceType).
# Anything not listed here is out of scope for this dashboard (other internal
# teams sharing the same tracker) and gets excluded + counted in meta.
BRAND_MAP = {
    "FTTH Starlite": ("Starlite", "FTTH"),
    "FTTH Viberlink": ("Viberlink", "FTTH"),
    "FWA IRA": ("IRA", "FWA"),
}
COMPANIES = ["Starlite", "Viberlink", "IRA"]

# Flexible column matching: several plausible header spellings per field,
# matched case-insensitively after stripping whitespace. First match wins.
COLUMN_CANDIDATES = {
    "date": ["date", "tanggal"],
    "name": ["full_name", "fullname", "agent", "nama"],
    "brand": ["brand"],
    "frt_text": ["first_response_time"],
    "art_text": ["response_time"],
    "rt_text": ["resolution_time"],
    "resolved": ["resolved_conversation", "resolved_conversations"],
    "messages": ["messages_sent"],
    "assigned": ["conversation_assigned", "conversations_assigned"],
    "source": ["source", "platform"],
}

DUR_RE = re.compile(r"^\s*(\d+)h\s*(\d+)m\s*(\d+)s\s*$", re.IGNORECASE)


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


def parse_duration(text):
    """Return (minutes:float|None, malformed:bool) from '00h 00m 53s' text.

    Overflow minutes/seconds (e.g. '00h 100m 52s') are common in this sheet
    (a spreadsheet formula bug that doesn't carry the overflow into the hour
    field). h*60 + m + s/60 is mathematically correct regardless of whether
    the overflow was carried, so we always compute it this way rather than
    trusting any pre-computed numeric duration column, which inherits the
    same bug and is not safe to trust as-is."""
    if pd.isna(text):
        return None, False
    m = DUR_RE.match(str(text))
    if not m:
        return None, True
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3))
    malformed = mi >= 60 or se >= 60
    return round(h * 60 + mi + se / 60, 3), malformed


def is_shared_account(name):
    n = str(name).strip()
    return bool(re.match(r"^(CS |HD )", n, re.IGNORECASE)) or ("Care(" in n) or ("Care (" in n)


def clean_name(name):
    n = re.sub(r"\s+", " ", str(name).strip())
    return n.rstrip(". ").strip()


def normalize_source(s):
    s = str(s).strip().lower()
    return "Qiscus" if s.startswith("q") else "Mekari"


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


def build_bundle(df):
    col = {k: find_column(df, v, required=(k not in ("messages", "assigned"))) for k, v in COLUMN_CANDIDATES.items()}

    work = pd.DataFrame({
        "date_raw": df[col["date"]],
        "name_raw": df[col["name"]],
        "brand_raw": df[col["brand"]],
        "frt_text": df[col["frt_text"]],
        "art_text": df[col["art_text"]],
        "rt_text": df[col["rt_text"]],
        "resolved": pd.to_numeric(df[col["resolved"]], errors="coerce"),
        "messages": pd.to_numeric(df[col["messages"]], errors="coerce") if col["messages"] else np.nan,
        "assigned": pd.to_numeric(df[col["assigned"]], errors="coerce") if col["assigned"] else np.nan,
        "source_raw": df[col["source"]],
    })

    work["date"] = pd.to_datetime(work["date_raw"], errors="coerce").dt.strftime("%Y-%m-%d")
    bad_dates = work["date"].isna().sum()
    if bad_dates:
        log(f"WARNING: dropping {bad_dates} row(s) with an unparseable date")
    work = work.dropna(subset=["date"]).copy()

    frt = work["frt_text"].apply(parse_duration)
    art = work["art_text"].apply(parse_duration)
    rt = work["rt_text"].apply(parse_duration)
    work["frt"], work["frt_mal"] = zip(*frt) if len(frt) else ([], [])
    work["art"], work["art_mal"] = zip(*art) if len(art) else ([], [])
    work["rt"], work["rt_mal"] = zip(*rt) if len(rt) else ([], [])
    work["malformed"] = work["frt_mal"] | work["art_mal"] | work["rt_mal"]

    mapped = work["brand_raw"].apply(lambda b: BRAND_MAP.get(str(b).strip(), (None, None)))
    work["company"] = mapped.apply(lambda t: t[0])
    work["service_type"] = mapped.apply(lambda t: t[1])
    work["in_scope"] = work["company"].notna()

    work["agent"] = work["name_raw"].apply(clean_name)
    work["is_shared"] = work["name_raw"].apply(is_shared_account)
    work["source_norm"] = work["source_raw"].apply(normalize_source)

    excluded = work.loc[~work["in_scope"]]
    excluded_brands = (
        excluded["brand_raw"].astype(str).str.strip().value_counts().to_dict()
    )
    source_variants = work["source_raw"].astype(str).str.strip().value_counts().to_dict()
    shared_names = sorted(
        work.loc[work["in_scope"] & work["is_shared"], "agent"].unique().tolist()
    )

    scope = work[work["in_scope"]].copy()

    rows = []
    for _, r in scope.iterrows():
        rows.append({
            "d": r["date"],
            "a": r["agent"],
            "sh": bool(r["is_shared"]),
            "c": r["company"],
            "st": r["service_type"],
            "src": r["source_norm"],
            "frt": None if pd.isna(r["frt"]) else round(float(r["frt"]), 2),
            "art": None if pd.isna(r["art"]) else round(float(r["art"]), 2),
            "rt": None if pd.isna(r["rt"]) else round(float(r["rt"]), 2),
            "res": None if pd.isna(r["resolved"]) else int(r["resolved"]),
            "msg": None if pd.isna(r["messages"]) else int(r["messages"]),
            "asg": None if pd.isna(r["assigned"]) else int(r["assigned"]),
            "mal": bool(r["malformed"]),
        })

    meta = {
        "excluded_brands": excluded_brands,
        "excluded_total": int(len(excluded)),
        "total_raw_rows": int(len(work)),
        "source_variants": source_variants,
        "malformed_count": int(scope["malformed"].sum()),
        "shared_names": shared_names,
    }

    insights = build_insights(scope)

    return rows, meta, insights


def median(a):
    a = [x for x in a if x is not None and not (isinstance(x, float) and np.isnan(x))]
    return float(np.median(a)) if a else None


ID_MONTHS = ["Jan","Feb","Mar","Apr","Mei","Jun","Jul","Agu","Sep","Okt","Nov","Des"]


def fmt_date_id(iso_date):
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{d.day} {ID_MONTHS[d.month-1]} {d.year}"
    except (ValueError, TypeError):
        return str(iso_date)


def fmt_hm(minutes):
    if minutes is None:
        return "–"
    total_sec = round(minutes * 60)
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}j {m}m"
    if m:
        return f"{m}m {s}d"
    return f"{s}d"


def build_insights(scope):
    """Re-derive the same kinds of findings written up manually for Agustus,
    but computed from whatever data is actually present — each rule is
    skipped (not faked) if the pattern it looks for isn't there."""
    insights = []
    if scope.empty or not set(scope["company"].unique()) & set(COMPANIES):
        return insights

    per_company = {}
    for c in COMPANIES:
        crows = scope[scope["company"] == c]
        if crows.empty:
            continue
        per_company[c] = {
            "resolved": float(crows["resolved"].sum(skipna=True)),
            "rt_med": median(crows["rt"].tolist()),
            "art_med": median(crows["art"].tolist()),
            "frt_med": median(crows["frt"].tolist()),
            "shared_share": float(crows["is_shared"].mean()) if len(crows) else 0.0,
            "n": len(crows),
        }
    if len(per_company) < 2:
        return insights

    total_resolved = sum(v["resolved"] for v in per_company.values()) or 1

    # 1) Biggest-volume company vs its own speed rank
    biggest = max(per_company, key=lambda c: per_company[c]["resolved"])
    rt_rank = sorted(per_company, key=lambda c: per_company[c]["rt_med"] if per_company[c]["rt_med"] is not None else 1e9)
    if rt_rank and rt_rank[-1] == biggest and len(rt_rank) > 1:
        vol_share = per_company[biggest]["resolved"] / total_resolved * 100
        fastest = rt_rank[0]
        insights.append({
            "tag": "watch", "tagLabel": "Perhatian",
            "html": (
                f"<b>{biggest}</b> menyumbang volume terbesar ({vol_share:.0f}% dari seluruh percakapan selesai) "
                f"tetapi median <b>RT</b>-nya justru paling lambat di antara brand yang ada: "
                f"<b>{fmt_hm(per_company[biggest]['rt_med'])}</b>, dibanding {fmt_hm(per_company[fastest]['rt_med'])} pada {fastest}. "
                f"Brand terbesar sekaligus paling lambat — kandidat utama untuk audit kapasitas/alur kerja."
            ),
        })

    # 2) Fastest company with a smaller volume share
    fastest = rt_rank[0] if rt_rank else None
    if fastest:
        vol_share_fast = per_company[fastest]["resolved"] / total_resolved * 100
        if vol_share_fast < (100 / len(per_company)):
            insights.append({
                "tag": "good", "tagLabel": "Sinyal Positif",
                "html": (
                    f"<b>{fastest}</b> mencatat resolution time tercepat (median {fmt_hm(per_company[fastest]['rt_med'])}) "
                    f"di antara brand yang ada meski volumenya hanya {vol_share_fast:.0f}% dari total. "
                    f"Pola kerja tim {fastest} layak dipelajari sebagai referensi efisiensi untuk brand lain."
                ),
            })

    # 3) Shared-account share disparity
    shares = {c: v["shared_share"] for c, v in per_company.items()}
    if len(shares) > 1:
        high_c = max(shares, key=shares.get)
        others_avg = np.mean([v for c, v in shares.items() if c != high_c])
        if shares[high_c] - others_avg > 0.15:
            insights.append({
                "tag": "note", "tagLabel": "Konteks Data",
                "html": (
                    f"<b>{shares[high_c]*100:.0f}%</b> baris data {high_c} berasal dari akun bersama/shared inbox, "
                    f"jauh di atas rata-rata brand lain (~{others_avg*100:.0f}%). Bandingkan angka {high_c} secara hati-hati — "
                    f"tabel ranking di bawah sudah mengecualikan akun bersama dari perbandingan individu."
                ),
            })

    # 4) Platform-switch anomaly days — detect per company, then group by
    # date so a same-day event affecting several brands reads as one finding
    # instead of one repetitive bullet per brand.
    flagged = {}  # date -> {companies:set, dom_day, rt_by_company:{}}
    for c in per_company:
        crows = scope[scope["company"] == c]
        dominant_overall = crows["source_norm"].mode()
        if dominant_overall.empty:
            continue
        dominant_overall = dominant_overall.iloc[0]
        daily = crows.groupby("date")
        day_rt = daily["rt"].median()
        if day_rt.dropna().empty:
            continue
        rt_p75 = day_rt.quantile(0.75)
        for d, g in daily:
            dom_day = g["source_norm"].mode()
            if dom_day.empty:
                continue
            dom_day = dom_day.iloc[0]
            share_other = (g["source_norm"] == dom_day).mean()
            if dom_day != dominant_overall and share_other >= 0.8 and day_rt.get(d, 0) >= rt_p75 and rt_p75 > 0:
                slot = flagged.setdefault(d, {"companies": [], "dom_day": dom_day, "usual": [], "rt": {}})
                slot["companies"].append(c)
                slot["usual"].append(dominant_overall)
                slot["rt"][c] = day_rt.get(d)
                break  # one flagged day per company is enough signal

    for d, info in sorted(flagged.items())[:2]:
        companies_str = " & ".join(info["companies"])
        usual_str = " / ".join(sorted(set(info["usual"])))
        rt_str = ", ".join(f"{c} {fmt_hm(v)}" for c, v in info["rt"].items())
        insights.append({
            "tag": "watch", "tagLabel": "Anomali",
            "html": (
                f"Pada <b>{fmt_date_id(d)}</b>, mayoritas transaksi <b>{companies_str}</b> tercatat lewat platform {info['dom_day']} "
                f"(biasanya didominasi {usual_str}) — bertepatan dengan median RT harian yang tinggi ({rt_str}). "
                f"Pola ini lebih mengarah ke gangguan/migrasi sistem pada hari tsb dan sebaiknya dikonfirmasi ke tim IT/Ops, "
                f"bukan langsung disimpulkan sebagai penurunan performa agent."
            ),
        })

    # 5) Qiscus / messages_sent bias note
    qiscus_rows = scope[scope["source_norm"] == "Qiscus"]
    if not qiscus_rows.empty:
        qiscus_share = qiscus_rows["resolved"].sum(skipna=True) / total_resolved * 100
        insights.append({
            "tag": "dq", "tagLabel": "Catatan Data",
            "html": (
                f"{qiscus_share:.0f}% percakapan selesai berjalan lewat <b>Qiscus</b>, platform yang tidak selalu mencatat "
                f"<code>messages_sent</code>. Metrik volume pesan (termasuk scatter plot) akan bias merepresentasikan agent Mekari; "
                f"gunakan <code>resolved_conversation</code> sebagai pembanding volume yang lebih adil antar platform."
            ),
        })

    return insights[:6]


def main():
    df = fetch_csv(SOURCE_CSV_URL)
    log(f"Loaded {len(df)} raw rows, columns: {list(df.columns)}")
    rows, meta, insights = build_bundle(df)
    log(f"In-scope rows: {len(rows)} / {meta['total_raw_rows']} ({meta['excluded_total']} excluded)")

    bundle = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sourceUrl": SOURCE_CSV_URL,
        "rows": rows,
        "meta": meta,
        "insights": insights,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(bundle, f, separators=(",", ":"), ensure_ascii=False)
    log(f"Wrote {OUT_PATH} ({os.path.getsize(OUT_PATH)} bytes)")


if __name__ == "__main__":
    main()
