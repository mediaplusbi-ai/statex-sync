"""
statex_to_supabase.py
─────────────────────
1. Calls the Statex Export API
2. Downloads each creative image
3. Uploads to Supabase Storage
4. Upserts rows into the Supabase table
"""

import os, re, csv, time, json, urllib.request, urllib.error, gzip
from datetime import date, timedelta
from pathlib import Path

# ── Config from environment variables ────────────────────────────────────────
STATEX_TOKEN   = os.environ["STATEX_TOKEN"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]          # https://xxxx.supabase.co
SUPABASE_KEY   = os.environ["SUPABASE_SERVICE_KEY"]  # service_role key
BUCKET         = "statex_outdoor_adview"
TABLE          = "statex_rows"

BASE_URL       = "https://backend.statexmonitoring.com/api/v1"

COLUMNS = [
    "creative_id",
    "creative_link",
    "brand",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def statex_headers():
    return {"Authorization": f"Bearer {STATEX_TOKEN}",
            "Content-Type": "application/json"}

def supabase_headers(extra={}):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    h.update(extra)
    return h

def http(method, url, headers, body=None, raw=False):
    data = (json.dumps(body).encode() if isinstance(body, dict)
            else body if isinstance(body, bytes)
            else None)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as r:
        return r.read() if raw else json.loads(r.read().decode())

# ── Step 1: Export from Statex ────────────────────────────────────────────────

def fetch_statex_csv():
    today = date.today()
    start = str(today - timedelta(days=7))
    end   = str(today)

    payload = {
        "format": "csv",
        "date_range": {"start": start, "end": end},
        "columns": COLUMNS,
    }

    print(f"Submitting Statex export ({start} → {end}) …")
    result = http("POST", f"{BASE_URL}/export",
                  statex_headers(), body=payload)
    export_id = result["id"]
    print(f"  id: {export_id}")

    print("  Polling", end="", flush=True)
    while True:
        rec = http("GET", f"{BASE_URL}/export/{export_id}", statex_headers())
        print(f" {rec['status']}", end="", flush=True)
        if rec["status"] == "completed":
            print()
            return rec["file_url"]
        if rec["status"] == "error":
            raise RuntimeError("Statex export failed")
        time.sleep(5)

def download_csv(file_url):
    print("Downloading CSV …")
    raw = http("GET", file_url, {}, raw=True)

    # Decompress if gzip encoded
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)

    path = Path("/tmp/statex_export.csv")
    path.write_bytes(raw)
    return path

# ── Step 2: Upload image to Supabase Storage ─────────────────────────────────

def upload_image(creative_id, source_url):
    """Downloads image from Statex, uploads to Storage, returns public URL."""
    source_url = source_url.strip()
    if not source_url:
        return None

    # Derive extension
    ext = Path(source_url.split("?")[0]).suffix or ".jpg"
    storage_path = f"{creative_id}{ext}"

    # Check if already exists
    check_url = (f"{SUPABASE_URL}/storage/v1/object/info/public/"
                 f"{BUCKET}/{storage_path}")
    try:
        http("GET", check_url, supabase_headers())
        # Already exists — return public URL
        return (f"{SUPABASE_URL}/storage/v1/object/public/"
                f"{BUCKET}/{storage_path}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    # Download from Statex
    try:
        img_bytes = http("GET", source_url, {}, raw=True)
    except Exception as exc:
        print(f"  ⚠ Could not fetch image {source_url}: {exc}")
        return None

    # Upload to Supabase Storage
    upload_url = (f"{SUPABASE_URL}/storage/v1/object/"
                  f"{BUCKET}/{storage_path}")
    content_type = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    headers = supabase_headers({"Content-Type": content_type})
    http("POST", upload_url, headers, body=img_bytes)

    public_url = (f"{SUPABASE_URL}/storage/v1/object/public/"
                  f"{BUCKET}/{storage_path}")
    print(f"  ↑ Uploaded {storage_path}")
    return public_url

# ── Step 3: Upsert rows into Supabase ────────────────────────────────────────

def upsert_rows(rows):
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"
    headers = supabase_headers({
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        http("POST", url, headers, body=batch)
        print(f"  Upserted rows {i+1}–{i+len(batch)}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    file_url = fetch_statex_csv()
    csv_path = download_csv(file_url)
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        print(f"CSV headers: {reader.fieldnames}")  # ← add this
        for row in reader:
            creative_id = (row.get("creative_id") or "").strip()
            link        = (row.get("creative_link") or "").strip().replace("\n", "")
            if not creative_id:
                continue
            image_url = upload_image(creative_id, link) if link else None
            rows.append({
                "id":            creative_id,
                "brand":         row.get("brand", "").strip(),
                "link_to_image": link,
                "image_url":     image_url,
            })
    print(f"\nUpserting {len(rows)} rows …")
    upsert_rows(rows)
    print("✓ Done.")
if __name__ == "__main__":
    main()
