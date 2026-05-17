import re
import time
import json
import random
import base64
import datetime
import smtplib
import threading
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ─────────────────────────── KONFIGURACIJA ───────────────────────────────────

EMAIL_SENDER   = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"

GITHUB_TOKEN  = "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"
GITHUB_REPO   = "webb987-rgb/PromoAM"
GITHUB_BRANCH = "main"

CITY_KEYS    = ["Beograd", "Novi Sad", "Nis", "Kragujevac"]
CITY_DISPLAY = {
    "Beograd":    "Beograd",
    "Novi Sad":   "Novi Sad",
    "Nis":        "Niš",
    "Kragujevac": "Kragujevac",
}
CITIES = [CITY_DISPLAY[k] for k in CITY_KEYS]

FETCH_WORKERS = 4

RESET_PASSWORD = "zekapeka"

EMAIL_IGNORE_PROMOS = [
    "0 din delivery fee for 14 days",
    "0 din delivery fee",
    "free delivery for 14 days",
    "besplatna dostava 14 dana",
    "besplatna dostava",
]

AMM_FILE   = Path("amm_baza.csv")
AMM_COLS   = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]
ALERT_FILE = Path("alert_log.csv")
ALERT_COLS = ["timestamp", "city", "restaurant_display", "am_name", "am_email", "akcije"]
SCAN_FILE  = Path("scan_baza_item.json")

CITY_MULTI_COORDS = {
    "Beograd": [
        (44.8178, 20.4569),
        (44.7866, 20.4489),
        (44.8525, 20.3914),
        (44.8010, 20.5132),
        (44.7700, 20.3900),
        (44.8300, 20.5800),
    ],
    "Novi Sad": [
        (45.2671, 19.8335),
        (45.2500, 19.8100),
        (45.2850, 19.8600),
        (45.2400, 19.8700),
    ],
    "Nis": [
        (43.3209, 21.8958),
        (43.3050, 21.8800),
        (43.3350, 21.9150),
        (43.3100, 21.9300),
    ],
    "Kragujevac": [
        (44.0128, 20.9114),
        (44.0000, 20.8900),
        (44.0300, 20.9300),
        (43.9900, 20.9400),
    ],
}
CITY_COORDS = {k: v[0] for k, v in CITY_MULTI_COORDS.items()}

CITY_SLUG_MAP = {
    "Beograd":    "belgrade",
    "Novi Sad":   "novi-sad",
    "Nis":        "nis",
    "Kragujevac": "kragujevac",
}

# ─────────────────────────── PAGE CONFIG ─────────────────────────────────────

st.set_page_config(page_title="Promo Monitor", page_icon="🏷️", layout="wide")

st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background:#f7f8fc; }
    .kpi { background:#fff; border-radius:12px; padding:18px 24px;
           box-shadow:0 2px 8px rgba(0,0,0,0.07); text-align:center; }
    .kpi-val { font-size:2.2rem; font-weight:800; color:#009de0; }
    .kpi-lbl { font-size:.85rem; color:#888; margin-top:4px; }
    div[data-testid="stDataFrame"] thead th { background:#009de0!important; color:#fff!important; }
    .reset-zone { background:#fff5f5; border:2px solid #e74c3c;
                  border-radius:12px; padding:20px 24px; margin-top:8px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────── GITHUB API ──────────────────────────────────────

GITHUB_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json",
}

def _gh_api(method: str, path: str, payload: dict = None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    try:
        if method == "GET":
            return requests.get(url, headers=GITHUB_HEADERS, timeout=15)
        return requests.put(url, headers=GITHUB_HEADERS, json=payload, timeout=15)
    except Exception:
        return None

def _gh_delete(path: str) -> bool:
    r = _gh_api("GET", path)
    if not r or r.status_code != 200:
        return False
    sha = r.json().get("sha")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r2 = requests.delete(url, headers=GITHUB_HEADERS,
                             json={"message": f"reset: delete {path}", "sha": sha,
                                   "branch": GITHUB_BRANCH}, timeout=15)
        return r2.status_code in (200, 204)
    except Exception:
        return False

def github_read(path: str) -> str | None:
    r = _gh_api("GET", path)
    if r and r.status_code == 200:
        return base64.b64decode(r.json().get("content", "")).decode("utf-8")
    return None

def github_write(path: str, content: str, message: str = "update") -> bool:
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    r = _gh_api("GET", path)
    sha = r.json().get("sha") if r and r.status_code == 200 else None
    payload = {"message": message, "content": encoded, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    r2 = _gh_api("PUT", path, payload)
    return r2 is not None and r2.status_code in (200, 201)

# ─────────────────────────── HELPERS ─────────────────────────────────────────

def normalize(name: str) -> str:
    return re.sub(r"[^\w]", "", str(name).lower())

def local_now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def display_to_key(display_name: str) -> str:
    for key, disp in CITY_DISPLAY.items():
        if disp == display_name or key == display_name:
            return key
    norm = normalize(display_name)
    for key in CITY_KEYS:
        if normalize(key) == norm:
            return key
    return display_name

def is_ignored_promo(text: str) -> bool:
    t = text.lower().strip().lstrip("•").strip().replace("[wolt+]", "").strip()
    return any(i.lower() in t for i in EMAIL_IGNORE_PROMOS)

def filter_akcije_for_email(akcije_str: str) -> str:
    if not akcije_str or akcije_str == "-":
        return "-"
    lines = [l for l in akcije_str.split("\n") if l.strip()]
    filtered = [l for l in lines if not is_ignored_promo(l)]
    return "\n".join(filtered) if filtered else "-"

# ─────────────────────────── GITHUB PERSISTENTNA BAZA ───────────────────────

def save_scan(df: pd.DataFrame):
    github_write("scan_baza_item.json", df.to_json(orient="records", force_ascii=False), "scan: update")

def load_scan() -> pd.DataFrame:
    content = github_read("scan_baza_item.json")
    if content:
        try:
            return pd.read_json(content)
        except Exception:
            pass
    return pd.DataFrame()

def scan_meta() -> str | None:
    r = _gh_api("GET", "scan_baza_item.json")
    if r and r.status_code == 200:
        try:
            cr = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/commits?path=scan_baza_item.json&per_page=1",
                headers=GITHUB_HEADERS, timeout=10)
            if cr.status_code == 200 and cr.json():
                ts = cr.json()[0]["commit"]["committer"]["date"]
                dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                return dt.strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            pass
        return "dostupan"
    return None

def save_amm(df: pd.DataFrame):
    github_write("amm_baza.csv", df.to_csv(index=False), "amm: update")
    df.to_csv(AMM_FILE, index=False)

def load_amm() -> pd.DataFrame:
    content = github_read("amm_baza.csv")
    if content:
        try:
            from io import StringIO
            df = pd.read_csv(StringIO(content))
            for c in AMM_COLS:
                if c not in df.columns:
                    df[c] = ""
            return df
        except Exception:
            pass
    if AMM_FILE.exists():
        df = pd.read_csv(AMM_FILE)
        for c in AMM_COLS:
            if c not in df.columns:
                df[c] = ""
        return df
    return pd.DataFrame(columns=AMM_COLS)

def save_alert_log(df: pd.DataFrame):
    github_write("alert_log.csv", df.to_csv(index=False), "alert: log update")
    df.to_csv(ALERT_FILE, index=False)

def load_alert_log() -> pd.DataFrame:
    content = github_read("alert_log.csv")
    if content:
        try:
            from io import StringIO
            df = pd.read_csv(StringIO(content))
            for c in ALERT_COLS:
                if c not in df.columns:
                    df[c] = ""
            return df
        except Exception:
            pass
    if ALERT_FILE.exists():
        df = pd.read_csv(ALERT_FILE)
        for c in ALERT_COLS:
            if c not in df.columns:
                df[c] = ""
        return df
    return pd.DataFrame(columns=ALERT_COLS)

def append_alert_log(rows: list):
    df_new = pd.DataFrame(rows)
    existing = load_alert_log()
    merged = pd.concat([existing, df_new], ignore_index=True)
    save_alert_log(merged)

# ─────────────────────────── SYSTEM RESET ────────────────────────────────────

def system_reset() -> list[str]:
    """
    Briše SVE podatke: GitHub fajlove + lokalne fajlove + session state.
    Vraća listu poruka o uspešnosti svake operacije.
    """
    log = []

    # GitHub fajlovi
    for gh_path in ["scan_baza_item.json", "amm_baza.csv", "alert_log.csv", "scheduler_config.json"]:
        ok = _gh_delete(gh_path)
        log.append(("✅" if ok else "⚠️") + f" GitHub: {gh_path} {'obrisan' if ok else 'nije nađen/greška'}")

    # Lokalni fajlovi
    for local_path in [SCAN_FILE, AMM_FILE, ALERT_FILE,
                        Path("scheduler_config.json"),
                        Path("_fetch_debug.log"),
                        Path("_scan_cookie.txt"),
                        Path("_scan_city_progress.json")]:
        if local_path.exists():
            local_path.unlink(missing_ok=True)
            log.append(f"✅ Lokalno: {local_path.name} obrisan")

    # Session state reset
    keys_to_clear = ["df_wolt", "last_scan", "scan_running", "scan_start_time",
                     "scan_mode", "wolt_cookie"]
    for k in keys_to_clear:
        if k in st.session_state:
            del st.session_state[k]
    log.append("✅ Session state resetovan")

    return log

# ─────────────────────────── KEEP-ALIVE ──────────────────────────────────────

if "keepalive_started" not in st.session_state:
    def _ka():
        time.sleep(30)
        while True:
            try:
                requests.get("http://localhost:8501/_stcore/health", timeout=10)
            except Exception:
                pass
            time.sleep(270)
    threading.Thread(target=_ka, daemon=True).start()
    st.session_state["keepalive_started"] = True

# ─────────────────────────── WOLT SESSION ────────────────────────────────────

WOLT_COOKIE = ""

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://wolt.com",
    "Referer": "https://wolt.com/en/srb/",
    "W-PlatformType": "Web",
    "W-Wolt-Session-Id": "wolt-monitor-session",
}

session = requests.Session()
session.headers.update(BROWSER_HEADERS)
if WOLT_COOKIE:
    session.headers["Cookie"] = WOLT_COOKIE

def wolt_get(url: str) -> tuple:
    try:
        r = session.get(url, timeout=15)
        return (r.json(), 200) if r.status_code == 200 else (None, r.status_code)
    except Exception:
        return None, -1

def make_thread_session() -> requests.Session:
    s = requests.Session()
    for k, v in session.headers.items():
        s.headers[k] = v
    try:
        cookie_val = Path("_scan_cookie.txt").read_text().strip()
    except Exception:
        cookie_val = WOLT_COOKIE or ""
    if cookie_val:
        s.headers["Cookie"] = cookie_val
    return s

# ─────────────────────────── FETCH ───────────────────────────────────────────

_fetch_log_lock = threading.Lock()
_throttle_until = 0.0
_throttle_lock  = threading.Lock()

def _log_fetch(msg: str):
    try:
        with _fetch_log_lock:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            with open("_fetch_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def _wait_throttle():
    with _throttle_lock:
        wait = _throttle_until - time.time()
    if wait > 0:
        time.sleep(wait)

def _set_throttle(seconds: float):
    with _throttle_lock:
        global _throttle_until
        _throttle_until = max(_throttle_until, time.time() + seconds)

def _fetch_url(ts, url: str, label: str, stop_event) -> tuple:
    for attempt in range(4):
        if stop_event.is_set():
            return None, 0
        _wait_throttle()
        try:
            time.sleep(random.uniform(0.3, 1.2))
            r = ts.get(url, timeout=10)
            if r.status_code == 200:
                return r.json(), 200
            if r.status_code in (401, 403):
                _log_fetch(f"{label} → {r.status_code} (auth fail)")
                return None, r.status_code
            if r.status_code == 429:
                wait = 2 + 2 ** attempt
                _set_throttle(wait)
                _log_fetch(f"{label} → 429 retry {attempt} (throttle {wait:.0f}s)")
                continue
            _log_fetch(f"{label} → {r.status_code}")
            return None, r.status_code
        except Exception as e:
            _log_fetch(f"{label} → EXC {e}")
            if attempt < 3:
                time.sleep(0.5)
    return None, -1

def _fetch_one(slug: str, lat: float, lon: float, feed_akcije: list,
               stop_event: threading.Event) -> tuple[str, str]:
    if stop_event.is_set():
        return slug, "-"
    ts = make_thread_session()
    dyn_url = (f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic/"
               f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery")
    dyn_data, _ = _fetch_url(ts, dyn_url, f"DYN {slug}", stop_event)
    if dyn_data:
        try:
            parsed = _parse_dynamic_with_item_discount(dyn_data)
            combined = list(dict.fromkeys(feed_akcije + parsed))
            return slug, "\n".join(combined) if combined else "-"
        except Exception as e:
            _log_fetch(f"DYN {slug} → parse EXC {e}")
    elif feed_akcije:
        return slug, "\n".join(feed_akcije)
    return slug, "-"

def _parse_dynamic_with_item_discount(data: dict) -> list:
    akcije, seen = [], set()
    ignore_texts = {
        "prikaži detalje","show details","vidi sve","see all",
        "detalji restorana","restaurant details","more","još",
        "schedule order","naruči","see menu","add {amount} more",
        "try for 30 days for free!","get rsd0 delivery fee & more!",
    }
    def add(text, wolt_plus=False):
        t = (text or "").strip()
        if not t or len(t) <= 3 or t.lower() in ignore_texts:
            return
        key = t.lower()
        if key not in seen:
            seen.add(key)
            akcije.append(f"• {'[Wolt+] ' if wolt_plus else ''}{t}")

    venue_raw = data.get("venue_raw") or {}
    for disc in venue_raw.get("discounts", []):
        if not isinstance(disc, dict):
            continue
        is_wp = (disc.get("has_wolt_plus") or
                 (disc.get("banner") or {}).get("show_wolt_plus", False) or
                 (disc.get("conditions") or {}).get("has_wolt_plus") == True)
        banner = disc.get("banner") or {}
        desc   = disc.get("description") or {}
        primary = banner.get("formatted_text") or desc.get("title") or ""
        add(primary, is_wp)
        effects = disc.get("effects") or {}
        for eff_key, handler in [
            ("item_discount",    lambda e, p, w: add(p or f"{int(round(float(e.get('fraction',0))*100))}% popust na izabrane artikle", w) if e.get("fraction") and float(e["fraction"])>0 else None),
            ("basket_discount",  lambda e, p, w: add(p or (f"{int(e['amount'])//100} RSD popust na korpu" if e.get("amount") and int(e["amount"])>0 else f"{int(round(float(e.get('fraction',0))*100))}% popust na korpu"), w) if e.get("amount") or e.get("fraction") else None),
            ("free_items",       lambda e, p, w: add(p or "Gratis artikal uz porudžbinu", w)),
        ]:
            eff = effects.get(eff_key)
            if eff and isinstance(eff, (dict, list)):
                handler(eff if isinstance(eff, dict) else {}, primary, is_wp)
        dd = effects.get("delivery_discount")
        if dd and isinstance(dd, dict):
            amt = dd.get("amount"); frac = dd.get("fraction")
            if (amt is not None and int(amt)==0) or (frac and float(frac)>=1.0):
                add(primary or "Besplatna dostava", is_wp)
            elif amt and int(amt)>0:
                add(primary or f"{int(amt)//100} RSD popust na dostavu", is_wp)

    venue = data.get("venue") or {}
    for ban in venue.get("banners", []):
        if isinstance(ban, dict):
            add((ban.get("discount") or {}).get("formatted_text"), ban.get("show_wolt_plus", False))
    for tracker in (venue.get("offer_assistant") or {}).get("offer_trackers", []):
        if isinstance(tracker, dict):
            add(tracker.get("title"),
                tracker.get("offer_type") == "wolt_plus" or tracker.get("show_wolt_plus", False))
    return akcije

def _parse_dynamic(data: dict) -> list:
    akcije = set()
    ignore_texts = {
        "prikaži detalje","show details","vidi sve","see all",
        "detalji restorana","restaurant details","more","još",
        "schedule order","naruči","see menu","add {amount} more",
        "try for 30 days for free!","get rsd0 delivery fee & more!",
    }
    def add(text, wolt_plus=False):
        t = (text or "").strip()
        if not t or len(t) <= 3 or t.lower() in ignore_texts:
            return
        akcije.add(f"• {'[Wolt+] ' if wolt_plus else ''}{t}")
    venue_raw = data.get("venue_raw") or {}
    for disc in venue_raw.get("discounts", []):
        if not isinstance(disc, dict):
            continue
        is_wp = (disc.get("has_wolt_plus") or
                 (disc.get("banner") or {}).get("show_wolt_plus", False))
        add((disc.get("banner") or {}).get("formatted_text"), is_wp)
        add((disc.get("description") or {}).get("title"), is_wp)
    venue = data.get("venue") or {}
    for b in venue.get("banners", []):
        if isinstance(b, dict):
            add((b.get("discount") or {}).get("formatted_text"), b.get("show_wolt_plus", False))
    for t in (venue.get("offer_assistant") or {}).get("offer_trackers", []):
        if isinstance(t, dict):
            add(t.get("title"), t.get("offer_type")=="wolt_plus" or t.get("show_wolt_plus", False))
    return list(akcije)

# ─────────────────────────── CITY PROGRESS ───────────────────────────────────

_city_progress      = {}
_city_progress_lock = threading.Lock()

def _update_cp(city, found=None, total=None, status=None):
    with _city_progress_lock:
        if city not in _city_progress:
            _city_progress[city] = {"found": 0, "total": 0, "status": "čekanje..."}
        if found is not None:  _city_progress[city]["found"]  = found
        if total is not None:  _city_progress[city]["total"]  = total
        if status is not None: _city_progress[city]["status"] = status

def _write_cp():
    with _city_progress_lock:
        data = dict(_city_progress)
    try:
        Path("_scan_city_progress.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# ─────────────────────────── FETCH CITY ──────────────────────────────────────

def fetch_city(city_display: str, stop_event: threading.Event) -> list[dict]:
    city_key     = display_to_key(city_display)
    city_slug    = CITY_SLUG_MAP.get(city_key)
    multi_coords = CITY_MULTI_COORDS.get(city_key, [CITY_COORDS.get(city_key, (44.8178, 20.4569))])
    primary_lat, primary_lon = multi_coords[0]
    if not city_slug:
        return []

    restaurants = {}
    _update_cp(city_display, found=0, total=0, status="Učitavam listu...")
    _write_cp()

    for loc_idx, (lat, lon) in enumerate(multi_coords):
        if stop_event.is_set():
            break
        skip = 0
        for page_num in range(50):
            if stop_event.is_set():
                break
            count_before = len(restaurants)
            data, _ = wolt_get(f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}")
            items_in_response = 0
            if data:
                for section in data.get("sections", []):
                    for item in section.get("items", []):
                        venue = item.get("venue")
                        if not venue:
                            continue
                        name = venue.get("name", "")
                        slug = venue.get("slug", "")
                        if not name or not slug or slug in restaurants:
                            continue
                        items_in_response += 1
                        rating  = venue.get("rating") or {}
                        est     = venue.get("estimate_range") or venue.get("estimate")
                        feed_ak = []
                        novo    = "Ne"
                        for badge in venue.get("badges", []):
                            txt = badge.get("text", "")
                            if txt.lower() in ["novo", "new"]:
                                novo = "Da"
                            elif txt:
                                feed_ak.append(f"• {txt}")
                        lbl = venue.get("label", "")
                        if lbl.lower() in ["novo", "new"]:
                            novo = "Da"
                        elif lbl:
                            feed_ak.append(f"• {lbl}")
                        restaurants[slug] = {
                            "grad": city_display, "naziv": name, "slug": slug,
                            "status": "Otvoren" if venue.get("online") else "Zatvoren",
                            "ocena": str(rating.get("score", "-") if isinstance(rating, dict) else "-"),
                            "dostava": f"{est} min" if est else "-",
                            "novo": novo, "_feed_akcije": feed_ak, "akcije": "-",
                            "link": f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}",
                            "naziv_norm": normalize(name),
                        }
            new_count = len(restaurants) - count_before
            _update_cp(city_display, found=len(restaurants),
                       status=f"📍 lok.{loc_idx+1}/{len(multi_coords)} str.{page_num+1} +{new_count} (ukupno {len(restaurants)})")
            _write_cp()
            if items_in_response == 0:
                break
            skip += 40
            time.sleep(random.uniform(0.5, 1.5))

    if not restaurants or stop_event.is_set():
        if not restaurants:
            _update_cp(city_display, status="⚠️ Nije pronađen nijedan restoran.")
        _write_cp()
        return []

    slugs    = list(restaurants.keys())
    total    = len(slugs)
    completed = 0
    _update_cp(city_display, total=total, status=f"⚡ Akcije 0/{total}...")
    _write_cp()

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_one, slug, primary_lat, primary_lon,
                            restaurants[slug]["_feed_akcije"], stop_event): slug
            for slug in slugs
        }
        for future in as_completed(futures):
            if stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                slug, akcije_str = future.result()
                restaurants[slug]["akcije"] = akcije_str
            except Exception:
                pass
            completed += 1
            if completed % 15 == 0 or completed == total:
                _update_cp(city_display, status=f"⚡ Akcije {completed}/{total}")
                _write_cp()

    for r in restaurants.values():
        r.pop("_feed_akcije", None)
    _update_cp(city_display, status=f"✅ Završen! {len(restaurants)} restorana")
    _write_cp()
    return list(restaurants.values())

def scan_all_cities(selected_cities: list[str], stop_event: threading.Event) -> pd.DataFrame:
    with _city_progress_lock:
        _city_progress.clear()
    for city in selected_cities:
        _update_cp(city, found=0, total=0, status="⏳ Čeka...")
    _write_cp()
    all_rows = []
    for i, city in enumerate(selected_cities):
        if stop_event.is_set():
            break
        try:
            rows = fetch_city(city, stop_event)
            all_rows.extend(rows)
        except Exception as e:
            _update_cp(city, status=f"❌ Greška: {e}")
            _write_cp()
        if i < len(selected_cities) - 1 and not stop_event.is_set():
            time.sleep(0.5)
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

def scan_nopromo_cities(selected_cities: list[str], prev_df: pd.DataFrame,
                        stop_event: threading.Event) -> pd.DataFrame:
    with _city_progress_lock:
        _city_progress.clear()
    no_promo  = prev_df[(prev_df["grad"].isin(selected_cities)) & (prev_df["akcije"] == "-")].copy()
    had_promo = prev_df[(prev_df["grad"].isin(selected_cities)) & (prev_df["akcije"] != "-")].copy()
    other     = prev_df[~prev_df["grad"].isin(selected_cities)].copy()

    for city in selected_cities:
        cnt = len(no_promo[no_promo["grad"] == city])
        _update_cp(city, found=cnt, total=cnt, status=f"⏳ Čeka... ({cnt} res.)")
    _write_cp()

    updated_rows = []
    for city in selected_cities:
        if stop_event.is_set():
            break
        city_key = display_to_key(city)
        lat, lon = CITY_MULTI_COORDS.get(city_key, [(44.8178, 20.4569)])[0]
        subset   = no_promo[no_promo["grad"] == city]
        slugs    = subset["slug"].tolist() if "slug" in subset.columns else []
        total    = len(slugs)
        completed = 0
        slug_to_row = {row["slug"]: row.to_dict() for _, row in subset.iterrows()} if "slug" in subset.columns else {}
        _update_cp(city, found=total, total=total, status=f"⚡ 0/{total}...")
        _write_cp()
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
            futures = {executor.submit(_fetch_one, slug, lat, lon, [], stop_event): slug for slug in slugs}
            for future in as_completed(futures):
                if stop_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    slug, akcije_str = future.result()
                    row = dict(slug_to_row.get(slug, {}))
                    row["akcije"] = akcije_str
                    updated_rows.append(row)
                except Exception:
                    pass
                completed += 1
                if completed % 15 == 0 or completed == total:
                    _update_cp(city, status=f"⚡ {completed}/{total}")
                    _write_cp()
        _update_cp(city, status=f"✅ {total} skeniran")
        _write_cp()

    parts = []
    if updated_rows: parts.append(pd.DataFrame(updated_rows))
    if not had_promo.empty: parts.append(had_promo)
    if not other.empty: parts.append(other)
    return pd.concat(parts, ignore_index=True) if parts else prev_df.copy()

# ─────────────────────────── EMAIL ───────────────────────────────────────────

def send_alert_email(am_email: str, am_name: str, alerts: list[dict]) -> bool:
    try:
        rows_html = ""
        for a in alerts:
            af = filter_akcije_for_email(a["akcije"])
            ah = af.replace("\n", "<br>") if af != "-" else "<span style='color:#aaa'>–</span>"
            lnk = a.get("link", "")
            nc = (f"<a href='{lnk}' style='color:#222;text-decoration:none;font-weight:600'>{a['naziv']}</a>"
                  if lnk else f"<b>{a['naziv']}</b>")
            rows_html += f"<tr><td style='padding:10px 14px;border-bottom:1px solid #eee'>{nc}</td><td style='padding:10px 14px;border-bottom:1px solid #eee;color:#555'>{a['grad']}</td><td style='padding:10px 14px;border-bottom:1px solid #eee;color:#333'>{ah}</td></tr>"
        if not rows_html:
            return True
        today = datetime.date.today().strftime("%d.%m.%Y")
        html = f"""<html><body style='font-family:Arial,sans-serif;color:#222;max-width:720px;margin:auto'>
          <div style='background:#1a1a2e;padding:24px 32px;border-radius:12px 12px 0 0'>
            <h2 style='color:#fff;margin:0'>📊 Promo Monitor – {today}</h2></div>
          <div style='background:#fff;padding:24px 32px;border-radius:0 0 12px 12px;box-shadow:0 4px 16px rgba(0,0,0,0.08)'>
            <p>Zdravo <b>{am_name}</b>,</p>
            <p>Sledeći tvoji partneri imaju <b>aktivne promotivne akcije</b>:</p>
            <table style='border-collapse:collapse;width:100%;font-size:14px'>
              <thead><tr style='background:#f0f4ff'>
                <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Restoran</th>
                <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Grad</th>
                <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Akcije</th>
              </tr></thead><tbody>{rows_html}</tbody></table>
            <p style='margin-top:20px;font-size:12px;color:#999'>Automatski izveštaj &bull; {local_now()}</p>
          </div></body></html>"""
        msg = MIMEMultipart("alternative")
        msg["From"] = EMAIL_SENDER; msg["To"] = am_email
        msg["Subject"] = f"📊 Promo izveštaj – {len(alerts)} partnera – {today}"
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls(); srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, am_email, msg.as_string())
        return True
    except Exception as e:
        st.error(f"Email greška ({am_email}): {e}")
        return False

# ─────────────────────────── SCHEDULER ───────────────────────────────────────

SCHEDULER_FILE = Path("scheduler_config.json")

def load_scheduler_config() -> dict:
    if SCHEDULER_FILE.exists():
        try:
            return json.loads(SCHEDULER_FILE.read_text())
        except Exception:
            pass
    return {"enabled": False, "hour": 8, "minute": 0, "cities": CITIES}

def save_scheduler_config(cfg: dict):
    SCHEDULER_FILE.write_text(json.dumps(cfg))

def run_scheduled_scan_and_send():
    cfg = load_scheduler_config()
    if not cfg.get("enabled"):
        return
    stop_ev = threading.Event()
    df = scan_all_cities(cfg["cities"], stop_ev)
    if df.empty:
        return
    save_scan(df)
    amm_df = load_amm()
    if amm_df.empty:
        return
    df["naziv_norm"] = df["naziv"].apply(normalize)
    merged = df.merge(amm_df[["restaurant_norm","restaurant_display","city","am_name","am_email"]],
                      left_on="naziv_norm", right_on="restaurant_norm", how="inner")
    merged["_alert"] = merged.apply(lambda r: filter_akcije_for_email(r["akcije"]) != "-", axis=1)
    sa_ak = merged[merged["_alert"]].copy()
    sent_log = []
    for (am_name, am_email_addr), grp in sa_ak.groupby(["am_name","am_email"]):
        alerts = [{"naziv":r["naziv"],"grad":r["grad"],"akcije":r["akcije"],"link":r.get("link","")}
                  for _,r in grp.iterrows()]
        if send_alert_email(am_email_addr, am_name, alerts):
            for a in alerts:
                sent_log.append({"timestamp":local_now(),"city":a["grad"],"restaurant_display":a["naziv"],
                                 "am_name":am_name,"am_email":am_email_addr,"akcije":a["akcije"]})
    if sent_log:
        append_alert_log(sent_log)

if "scheduler_started" not in st.session_state:
    def _sched_loop():
        while True:
            cfg = load_scheduler_config()
            if cfg.get("enabled"):
                now = datetime.datetime.now()
                target = now.replace(hour=cfg["hour"], minute=cfg["minute"], second=0, microsecond=0)
                if now >= target:
                    target += datetime.timedelta(days=1)
                time.sleep((target - now).total_seconds())
                run_scheduled_scan_and_send()
            else:
                time.sleep(60)
    threading.Thread(target=_sched_loop, daemon=True).start()
    st.session_state["scheduler_started"] = True

# ─────────────────────────── SESSION STATE ───────────────────────────────────

for _k, _v in [("df_wolt", pd.DataFrame()), ("last_scan", None),
               ("scan_stop_event", threading.Event()), ("scan_running", False),
               ("scan_start_time", None), ("scan_mode", "full")]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ─────────────────────────── UI ──────────────────────────────────────────────

st.title("🏷️ Promo Monitor – Item Level")

tab_scan, tab_amm, tab_alert, tab_stats, tab_sched, tab_debug = st.tabs([
    "🔍 Scan & Rezultati", "👥 AMM Baza", "📧 Pošalji Alert",
    "📈 Statistika", "⏰ Auto-Scheduler", "🔧 Debug API",
])

# ══════════════════════════ TAB 1: SCAN ══════════════════════════════════════
with tab_scan:
    st.markdown("### 🔍 Scan")

    selected_cities = st.multiselect("📍 Gradovi:", options=CITIES, default=CITIES, key="selected_cities")

    prev_df_np = st.session_state.df_wolt
    nopromo_available = not prev_df_np.empty
    no_promo_count = (len(prev_df_np[(prev_df_np["grad"].isin(selected_cities)) & (prev_df_np["akcije"] == "-")])
                      if nopromo_available and selected_cities else 0)

    col_btn, col_btn2, col_stop, col_info = st.columns([1.2, 1.5, 0.9, 2.4])
    with col_btn:
        run_scan = st.button("▶️ Full Scan", type="primary", use_container_width=True,
                             disabled=not selected_cities or st.session_state.scan_running)
    with col_btn2:
        run_nopromo = st.button(f"🔍 No Promo ({no_promo_count})", use_container_width=True,
                                disabled=not selected_cities or st.session_state.scan_running
                                or not nopromo_available or no_promo_count == 0)
    with col_stop:
        stop_scan = st.button("⏹️ Stop", use_container_width=True,
                              disabled=not st.session_state.scan_running, type="secondary")
    with col_info:
        if st.session_state.last_scan:
            st.info(f"⏱️ Scan: **{st.session_state.last_scan}** | **{len(st.session_state.df_wolt)}** restorana")

    prev_meta = scan_meta()
    if prev_meta and not st.session_state.scan_running:
        if st.button(f"📂 Učitaj prethodni sken ({prev_meta})"):
            prev_df = load_scan()
            if not prev_df.empty:
                st.session_state.df_wolt = prev_df
                st.session_state.last_scan = prev_meta
                st.rerun()

    if stop_scan and st.session_state.scan_running:
        st.session_state.scan_stop_event.set()
        st.warning("⏹️ Zaustavljanje...")

    # ── Pokretanje Full Scan ──────────────────────────────────────────────────
    if run_scan and selected_cities and not st.session_state.scan_running:
        cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
        if cookie: session.headers["Cookie"] = cookie
        elif "Cookie" in session.headers: del session.headers["Cookie"]
        st.session_state.scan_stop_event = threading.Event()
        st.session_state.scan_running    = True
        st.session_state.scan_mode       = "full"
        st.session_state.scan_start_time = time.time()
        _cities_snap  = list(selected_cities)
        _stop_ev_snap = st.session_state.scan_stop_event
        Path("_scan_done.txt").unlink(missing_ok=True)
        Path("_scan_result.json").unlink(missing_ok=True)
        Path("_scan_cookie.txt").write_text(st.session_state.get("wolt_cookie","") or WOLT_COOKIE or "")
        with _city_progress_lock:
            _city_progress.clear()
            for _c in _cities_snap:
                _city_progress[_c] = {"found":0,"total":0,"status":"⏳ Čeka..."}
        _write_cp()
        def _run_full_bg():
            try:
                result = scan_all_cities(_cities_snap, _stop_ev_snap)
                if result is not None and not result.empty:
                    result.to_json("_scan_result.json", orient="records", force_ascii=False)
                else:
                    Path("_scan_result.json").write_text("[]")
            except Exception as e:
                Path("_scan_error.txt").write_text(str(e))
                Path("_scan_result.json").write_text("[]")
            finally:
                Path("_scan_done.txt").write_text(str(int(time.time() - st.session_state.scan_start_time or 0)))
        threading.Thread(target=_run_full_bg, daemon=True).start()
        st.rerun()

    # ── Pokretanje No Promo Scan ──────────────────────────────────────────────
    if run_nopromo and selected_cities and not st.session_state.scan_running and nopromo_available:
        cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
        if cookie: session.headers["Cookie"] = cookie
        elif "Cookie" in session.headers: del session.headers["Cookie"]
        st.session_state.scan_stop_event = threading.Event()
        st.session_state.scan_running    = True
        st.session_state.scan_mode       = "nopromo"
        st.session_state.scan_start_time = time.time()
        _cities_snap  = list(selected_cities)
        _stop_ev_snap = st.session_state.scan_stop_event
        _prev_snap    = st.session_state.df_wolt.copy()
        Path("_scan_done.txt").unlink(missing_ok=True)
        Path("_scan_result.json").unlink(missing_ok=True)
        Path("_scan_cookie.txt").write_text(st.session_state.get("wolt_cookie","") or WOLT_COOKIE or "")
        with _city_progress_lock:
            _city_progress.clear()
            for _c in _cities_snap:
                _cnt = len(_prev_snap[(_prev_snap["grad"]==_c)&(_prev_snap["akcije"]=="-")])
                _city_progress[_c] = {"found":_cnt,"total":_cnt,"status":f"⏳ ({_cnt} res.)"}
        _write_cp()
        def _run_nopromo_bg():
            try:
                result = scan_nopromo_cities(_cities_snap, _prev_snap, _stop_ev_snap)
                if result is not None and not result.empty:
                    result.to_json("_scan_result.json", orient="records", force_ascii=False)
                else:
                    Path("_scan_result.json").write_text("[]")
            except Exception as e:
                Path("_scan_error.txt").write_text(str(e))
                Path("_scan_result.json").write_text("[]")
            finally:
                Path("_scan_done.txt").write_text("1")
        threading.Thread(target=_run_nopromo_bg, daemon=True).start()
        st.rerun()

    # ── Prikaz toka skena BEZ punog rerun-a ──────────────────────────────────
    scan_done_flag = Path("_scan_done.txt").exists()

    if st.session_state.scan_running and not scan_done_flag:
        # Autorefresh samo progress sekcije — osvežava svake 3s
        # Koristimo st_autorefresh koji ne trepće ceo ekran
        st_autorefresh(interval=3000, limit=None, key="scan_autorefresh")

        elapsed = time.time() - (st.session_state.scan_start_time or time.time())
        m2, s2  = divmod(int(elapsed), 60)
        st.markdown(f"### 🔄 Skeniranje u toku — `{m2:02d}:{s2:02d}`")

        try:
            city_prog = json.loads(Path("_scan_city_progress.json").read_text(encoding="utf-8"))
        except Exception:
            city_prog = {}

        if city_prog:
            cols = st.columns(len(city_prog))
            for i, (city_name, info) in enumerate(city_prog.items()):
                with cols[i]:
                    found   = info.get("found", 0)
                    total_c = info.get("total", 0)
                    cstatus = info.get("status", "...")
                    is_done = "✅" in cstatus
                    color   = "#27ae60" if is_done else "#009de0"
                    pct     = min(found / total_c, 1.0) if total_c > 0 else 0.0
                    st.markdown(f"""
                    <div style='background:#fff;border-radius:10px;padding:14px 16px;
                                box-shadow:0 2px 8px rgba(0,0,0,0.08);
                                border-top:4px solid {color};margin-bottom:8px'>
                      <div style='font-weight:800;font-size:1rem;color:{color}'>{city_name}</div>
                      <div style='font-size:1.8rem;font-weight:900;color:#222'>{found}</div>
                      <div style='font-size:0.75rem;color:#888'>restorana</div>
                      <div style='background:#eee;border-radius:4px;height:6px;margin:8px 0'>
                        <div style='background:{color};border-radius:4px;height:6px;width:{int(pct*100)}%'></div>
                      </div>
                      <div style='font-size:0.78rem;color:#555'>{cstatus[:60]}</div>
                    </div>""", unsafe_allow_html=True)

    # ── Finalizacija kad scan završi ──────────────────────────────────────────
    if st.session_state.scan_running and scan_done_flag:
        Path("_scan_done.txt").unlink(missing_ok=True)
        st.session_state.scan_running = False
        scan_duration = time.time() - (st.session_state.scan_start_time or time.time())

        # Prikaži grešku iz bg threada ako postoji
        if Path("_scan_error.txt").exists():
            st.error(f"❌ Greška: {Path('_scan_error.txt').read_text()}")
            Path("_scan_error.txt").unlink(missing_ok=True)

        try:
            df_result = pd.read_json("_scan_result.json", orient="records")
        except Exception:
            df_result = pd.DataFrame()

        if df_result is not None and not df_result.empty:
            st.session_state.df_wolt = df_result
            st.session_state.last_scan = local_now()
            save_scan(df_result)
            m, s = divmod(int(scan_duration), 60)
            if st.session_state.get("scan_mode") == "nopromo":
                st.success(f"✅ No Promo Scan završen za {m:02d}:{s:02d}! "
                           f"{len(df_result[df_result['akcije'] != '-'])} restorana sada ima akcije.")
            else:
                st.success(f"✅ Full Scan završen za {m:02d}:{s:02d}! "
                           f"{len(df_result)} restorana, "
                           f"{len(df_result[df_result['akcije'] != '-'])} sa akcijama.")
            st.rerun()
        else:
            if st.session_state.scan_stop_event.is_set():
                st.warning("⏹️ Scan zaustavljen.")
            else:
                st.error("❌ Scan nije vratio podatke. Proveri cookie u Debug tabu.")

    # ── Prikaz rezultata ──────────────────────────────────────────────────────
    df = st.session_state.df_wolt
    if not df.empty:
        st.markdown("---")
        k1, k2, k3, k4, k5 = st.columns(5)
        for col, val, lbl in [
            (k1, len(df),                                                              "Ukupno"),
            (k2, len(df[df["akcije"] != "-"]),                                         "Ima akciju"),
            (k3, len(df[df["akcije"].str.contains("[Wolt+]", na=False, regex=False)]), "Wolt+"),
            (k4, len(df[df["status"] == "Otvoren"]),                                   "Otvoreno"),
            (k5, len(df[df["novo"] == "Da"]),                                          "Novi"),
        ]:
            with col:
                st.markdown(f"<div class='kpi'><div class='kpi-val'>{val}</div>"
                            f"<div class='kpi-lbl'>{lbl}</div></div>", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        grad_summary = df.groupby("grad").agg(
            Restorana=("naziv","count"),
            Sa_akcijama=("akcije", lambda x: (x!="-").sum()),
            Otvoreni=("status", lambda x: (x=="Otvoren").sum()),
        ).reset_index()
        gs_cols = st.columns(len(grad_summary))
        for i, row in grad_summary.iterrows():
            with gs_cols[i]:
                pct = int(row["Sa_akcijama"]/row["Restorana"]*100) if row["Restorana"]>0 else 0
                st.markdown(f"""<div style='background:#fff;border-radius:10px;padding:12px 16px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.07);border-top:3px solid #009de0;text-align:center'>
                  <div style='font-weight:800;color:#009de0'>{row["grad"]}</div>
                  <div style='font-size:1.6rem;font-weight:900'>{int(row["Restorana"])}</div>
                  <div style='font-size:0.75rem;color:#888'>restorana</div>
                  <div style='font-size:0.85rem;color:#27ae60'>{int(row["Sa_akcijama"])} akcija ({pct}%)</div>
                  <div style='font-size:0.75rem;color:#555'>{int(row["Otvoreni"])} otvorenih</div>
                </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        fc1, fc2, fc3, fc4 = st.columns(4)
        with fc1: grad_filter  = st.multiselect("Grad:", CITIES, default=CITIES, key="scan_grad")
        with fc2: samo_akcije  = st.checkbox("📌 Samo sa akcijama", key="scan_akcije")
        with fc3: samo_novi    = st.checkbox("🆕 Samo NOVI", key="scan_novi")
        with fc4: search       = st.text_input("🔎 Pretraži:", key="scan_search")
        fc5, fc6 = st.columns(2)
        with fc5: samo_wolt_plus = st.checkbox("💙 Samo Wolt+", key="scan_wolt_plus")
        with fc6: samo_otvoreni  = st.checkbox("🟢 Samo otvoreni", key="scan_otvoreni")

        fdf = df[df["grad"].isin(grad_filter)]
        if samo_akcije:    fdf = fdf[fdf["akcije"] != "-"]
        if samo_novi:      fdf = fdf[fdf["novo"] == "Da"]
        if samo_otvoreni:  fdf = fdf[fdf["status"] == "Otvoren"]
        if search.strip(): fdf = fdf[fdf["naziv"].str.contains(search.strip(), case=False, na=False)]
        if samo_wolt_plus: fdf = fdf[fdf["akcije"].str.contains("[Wolt+]", na=False, regex=False)]

        display_cols = [c for c in ["grad","naziv","status","ocena","dostava","novo","akcije","link"] if c in fdf.columns]
        st.dataframe(fdf[display_cols].reset_index(drop=True), use_container_width=True, hide_index=True, height=480,
            column_config={
                "grad":st.column_config.TextColumn("Grad"), "naziv":st.column_config.TextColumn("Restoran"),
                "status":st.column_config.TextColumn("Status"), "ocena":st.column_config.TextColumn("Ocena"),
                "dostava":st.column_config.TextColumn("Dostava"), "novo":st.column_config.TextColumn("Novi"),
                "akcije":st.column_config.TextColumn("Akcije", width="large"),
                "link":st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
            })
        st.download_button("📥 CSV", fdf[display_cols].to_csv(index=False).encode("utf-8"), "scan.csv", "text/csv")

# ══════════════════════════ TAB 2: AMM ═══════════════════════════════════════
with tab_amm:
    st.markdown("### 👥 Baza Account Managera")
    amm_df  = load_amm()
    df_wolt = st.session_state.df_wolt
    rest_options = sorted(df_wolt["naziv"].dropna().unique().tolist()) if not df_wolt.empty else []

    a1, a2 = st.columns([2,1])
    with a1: sel_rest = st.selectbox("Restoran:", ["-- Odaberi --"] + rest_options, key="amm_sel")
    with a2: man_rest = st.text_input("Ili upiši ručno:", key="amm_man")
    final_rest = man_rest.strip() if man_rest.strip() else (sel_rest if sel_rest != "-- Odaberi --" else "")

    b1, b2, b3, b4 = st.columns(4)
    with b1: amm_city  = st.selectbox("Grad:", ["-- Svi --"] + CITIES, key="amm_city_sel")
    with b2: amm_name  = st.text_input("Ime AM-a:", key="amm_name")
    with b3: amm_email_inp = st.text_input("Email AM-a:", key="amm_email")
    with b4:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("💾 Sačuvaj", use_container_width=True, key="amm_save"):
            if not final_rest: st.error("Izaberi restoran.")
            elif not amm_name or not amm_email_inp: st.error("Upiši ime i email.")
            else:
                norm = normalize(final_rest)
                city_val = "" if amm_city == "-- Svi --" else amm_city
                mask = (amm_df["restaurant_norm"]==norm) & (amm_df["city"]==city_val)
                if mask.any():
                    amm_df.loc[mask, ["restaurant_display","am_name","am_email"]] = [final_rest, amm_name, amm_email_inp]
                else:
                    amm_df = pd.concat([amm_df, pd.DataFrame([{"restaurant_norm":norm,"restaurant_display":final_rest,
                        "city":city_val,"am_name":amm_name,"am_email":amm_email_inp}])], ignore_index=True)
                save_amm(amm_df)
                st.success(f"✅ Sačuvano: **{final_rest}** → {amm_name}")
                st.rerun()

    st.markdown("---")
    if amm_df.empty:
        st.info("Baza je prazna.")
    else:
        am_filt = st.selectbox("Filtriraj po AM-u:", ["Svi"] + sorted(amm_df["am_name"].dropna().unique().tolist()), key="amm_view_filt")
        view = amm_df if am_filt == "Svi" else amm_df[amm_df["am_name"]==am_filt]
        edited = st.data_editor(view.reset_index(drop=True), use_container_width=True, num_rows="dynamic", hide_index=True,
            column_config={"restaurant_norm":st.column_config.TextColumn("Norm naziv", disabled=True),
                           "restaurant_display":st.column_config.TextColumn("Restoran"),
                           "city":st.column_config.TextColumn("Grad"),
                           "am_name":st.column_config.TextColumn("Ime AM-a"),
                           "am_email":st.column_config.TextColumn("Email AM-a")}, key="amm_editor")
        if st.button("💾 Sačuvaj izmene", key="amm_save_tbl"):
            save_amm(edited if am_filt=="Svi" else pd.concat([amm_df[amm_df["am_name"]!=am_filt], edited], ignore_index=True))
            st.success("✅ Ažurirano!"); st.rerun()

    st.markdown("---")
    if not df_wolt.empty:
        grad_exp = st.multiselect("Grad za export:", CITIES, default=CITIES, key="amm_export_grad")
        exp = df_wolt[["grad","naziv"]].copy()
        exp["restaurant_display"]=exp["naziv"]; exp["city"]=exp["grad"]; exp["am_name"]=""; exp["am_email"]=""
        exp = exp[["restaurant_display","city","am_name","am_email"]].drop_duplicates()
        exp = exp[exp["city"].isin(grad_exp)]
        st.download_button("📥 Lista restorana za AM dodelu (CSV)", exp.to_csv(index=False).encode("utf-8"), "restorani.csv","text/csv")

    st.markdown("---")
    st.markdown("#### 📥 Bulk import CSV")
    uploaded = st.file_uploader("CSV:", type="csv", key="amm_upload")
    if uploaded:
        try:
            ndf = pd.read_csv(uploaded)
            ndf["restaurant_norm"] = ndf["restaurant_display"].apply(normalize)
            merged2 = pd.concat([amm_df, ndf], ignore_index=True).drop_duplicates(subset=["restaurant_norm","city"], keep="last")
            save_amm(merged2)
            st.success(f"✅ Importovano {len(ndf)} redova."); st.rerun()
        except Exception as e:
            st.error(f"Greška: {e}")

# ══════════════════════════ TAB 3: ALERT ═════════════════════════════════════
with tab_alert:
    st.markdown("### 📧 Pošalji Alert AM-ovima")
    df_wolt = st.session_state.df_wolt
    amm_df  = load_amm()
    if df_wolt.empty:
        st.warning("⚠️ Pokreni scan prvo.")
    elif amm_df.empty:
        st.warning("⚠️ AMM baza je prazna.")
    else:
        df_wolt["naziv_norm"] = df_wolt["naziv"].apply(normalize)
        amm_df["restaurant_norm"] = amm_df["restaurant_norm"].apply(str)
        merged = df_wolt.merge(amm_df[["restaurant_norm","restaurant_display","city","am_name","am_email"]],
                               left_on="naziv_norm", right_on="restaurant_norm", how="inner")
        merged["_alert"] = merged.apply(lambda r: filter_akcije_for_email(r["akcije"])!="-", axis=1)
        sa_akcijama = merged[merged["_alert"]].copy()
        if sa_akcijama.empty:
            st.info("✅ Nema relevantnih akcija za AM-ove.")
        else:
            sa_akcijama["akcije_email"] = sa_akcijama["akcije"].apply(filter_akcije_for_email)
            af1, af2 = st.columns(2)
            with af1: grad_filt_a = st.multiselect("Grad:", CITIES, default=CITIES, key="alert_grad")
            with af2:
                am_list = sorted(sa_akcijama["am_name"].dropna().unique().tolist())
                am_filt_a = st.multiselect("AM:", am_list, default=am_list, key="alert_am")
            preview = sa_akcijama[(sa_akcijama["grad"].isin(grad_filt_a)) & (sa_akcijama["am_name"].isin(am_filt_a))]
            st.caption(f"Partnera: **{len(preview)}** | AM-ova: **{preview['am_name'].nunique()}**")
            pc = [c for c in ["grad","naziv","am_name","am_email","akcije_email","link"] if c in preview.columns]
            st.dataframe(preview[pc].reset_index(drop=True), use_container_width=True, hide_index=True, height=350,
                column_config={"grad":st.column_config.TextColumn("Grad"),"naziv":st.column_config.TextColumn("Restoran"),
                    "am_name":st.column_config.TextColumn("AM"),"am_email":st.column_config.TextColumn("Email"),
                    "akcije_email":st.column_config.TextColumn("Akcije",width="large"),
                    "link":st.column_config.LinkColumn("Link",display_text="Otvori ↗")})
            st.markdown("---")
            if st.button("🚀 Pošalji alertove", type="primary"):
                sent_log = []; ok_count = 0
                for (am_name, am_email_addr), grp in preview.groupby(["am_name","am_email"]):
                    alerts = [{"naziv":r["naziv"],"grad":r["grad"],"akcije":r["akcije"],"link":r.get("link","")} for _,r in grp.iterrows()]
                    if send_alert_email(am_email_addr, am_name, alerts):
                        ok_count += 1
                        st.success(f"✅ {am_name} ({am_email_addr})")
                        for a in alerts:
                            sent_log.append({"timestamp":local_now(),"city":a["grad"],"restaurant_display":a["naziv"],
                                             "am_name":am_name,"am_email":am_email_addr,"akcije":a["akcije"]})
                    else:
                        st.error(f"❌ {am_name}")
                if sent_log: append_alert_log(sent_log)
                st.markdown(f"**Završeno: {ok_count} mailova.**")

# ══════════════════════════ TAB 4: STATISTIKA ════════════════════════════════
with tab_stats:
    st.markdown("### 📈 Statistika")
    log_df = load_alert_log()
    if log_df.empty:
        st.info("Još nema poslatih alerta.")
    else:
        log_df["timestamp"] = pd.to_datetime(log_df["timestamp"], errors="coerce")
        min_d = log_df["timestamp"].min().date(); max_d = log_df["timestamp"].max().date()
        s1, s2 = st.columns(2)
        with s1: date_from = st.date_input("Od:", min_d, key="s_from")
        with s2: date_to   = st.date_input("Do:", max_d, key="s_to")
        flog = log_df[(log_df["timestamp"].dt.date >= date_from) & (log_df["timestamp"].dt.date <= date_to)]
        if not flog.empty:
            k1,k2,k3,k4 = st.columns(4)
            for col,val,lbl,color in [
                (k1,len(flog),"Alerta","#009de0"), (k2,flog["am_name"].nunique(),"AM-ova","#8e44ad"),
                (k3,flog["restaurant_display"].nunique(),"Restorana","#27ae60"),
                (k4,flog["timestamp"].dt.date.nunique(),"Dana","#e67e22")]:
                with col: st.markdown(f"<div class='kpi' style='border-top:4px solid {color}'><div class='kpi-val' style='color:{color}'>{val}</div><div class='kpi-lbl'>{lbl}</div></div>", unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)
            am_stats = (flog.groupby(["am_name","am_email"]).agg(
                Slanja=("timestamp",lambda x:x.dt.date.nunique()),
                Restorana=("restaurant_display","nunique"),
                Ukupno=("restaurant_display","count"),
                Poslednji=("timestamp","max")).reset_index()
                .rename(columns={"am_name":"AM","am_email":"Email"}).sort_values("Ukupno",ascending=False))
            am_stats["Poslednji"] = am_stats["Poslednji"].dt.strftime("%d.%m.%Y %H:%M")
            st.dataframe(am_stats, use_container_width=True, hide_index=True)
            am_log_sel = st.selectbox("Filtriraj:", ["Svi"]+sorted(flog["am_name"].dropna().unique().tolist()), key="log_am_sel")
            lv = flog if am_log_sel=="Svi" else flog[flog["am_name"]==am_log_sel]
            lv = lv.sort_values("timestamp",ascending=False).copy()
            lv["timestamp"] = lv["timestamp"].dt.strftime("%d.%m.%Y %H:%M")
            st.dataframe(lv, use_container_width=True, hide_index=True, height=400)
            st.download_button("📥 Eksportuj log", lv.to_csv(index=False).encode("utf-8"), "alert_log.csv","text/csv")

# ══════════════════════════ TAB 5: SCHEDULER ═════════════════════════════════
with tab_sched:
    st.markdown("### ⏰ Automatski sken i slanje")
    cfg = load_scheduler_config()
    sc1,sc2,sc3 = st.columns(3)
    with sc1: sched_enabled = st.toggle("✅ Uključi", value=cfg.get("enabled",False), key="sched_on")
    with sc2: sched_hour    = st.number_input("Sat (0–23):", 0, 23, cfg.get("hour",8), key="sched_hour")
    with sc3: sched_min     = st.number_input("Minut:", 0, 59, cfg.get("minute",0), key="sched_min")
    sched_cities = st.multiselect("Gradovi:", CITIES, default=cfg.get("cities",CITIES), key="sched_cities")
    if st.button("💾 Sačuvaj", type="primary"):
        save_scheduler_config({"enabled":sched_enabled,"hour":int(sched_hour),"minute":int(sched_min),"cities":sched_cities})
        st.success(f"✅ {'UKLJUČEN' if sched_enabled else 'ISKLJUČEN'} u {int(sched_hour):02d}:{int(sched_min):02d}")
    st.markdown("---")
    if st.button("▶️ Pokreni test sken sada", key="sched_test", disabled=st.session_state.get("sched_running",False)):
        st.session_state["sched_running"] = True
        def _sched_bg(): run_scheduled_scan_and_send(); st.session_state["sched_running"]=False; st.session_state["sched_done"]=True
        threading.Thread(target=_sched_bg, daemon=True).start(); st.rerun()
    if st.session_state.get("sched_running"): st.info("🔄 Sken u toku...")
    if st.session_state.get("sched_done"): st.session_state["sched_done"]=False; st.success("✅ Test završen.")
    if cfg.get("enabled"):
        now = datetime.datetime.now()
        tgt = now.replace(hour=cfg["hour"],minute=cfg["minute"],second=0,microsecond=0)
        if now >= tgt: tgt += datetime.timedelta(days=1)
        h,rem = divmod(int((tgt-now).total_seconds()),3600); mr = rem//60
        st.success(f"🕐 Sledeći sken za: **{h}h {mr}min** (u {cfg['hour']:02d}:{cfg['minute']:02d})")
    else:
        st.warning("Automatski sken je isključen.")

# ══════════════════════════ TAB 6: DEBUG ═════════════════════════════════════
with tab_debug:
    st.markdown("### 🔧 Debug & Podešavanja")

    # ── GitHub status ─────────────────────────────────────────────────────────
    st.markdown("#### 🐙 GitHub Status")
    gh_col1, gh_col2 = st.columns(2)
    with gh_col1:
        r_test = _gh_api("GET", "amm_baza.csv")
        if r_test and r_test.status_code == 200: st.success("✅ GitHub konekcija OK")
        else: st.error(f"❌ GitHub greška: {r_test.status_code if r_test else 'timeout'}")
    with gh_col2:
        st.info(f"📁 Repo: `{GITHUB_REPO}` | branch: `{GITHUB_BRANCH}`")

    # ── Cookie ────────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🍪 Cookie")
    saved_cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
    new_cookie = st.text_area("Cookie string:", value=saved_cookie, height=100,
                               placeholder="ravelinDeviceId=...; __woltUid=...; ...", key="cookie_input")
    if st.button("💾 Sačuvaj cookie", key="save_cookie"):
        st.session_state["wolt_cookie"] = new_cookie
        session.headers["Cookie"] = new_cookie
        st.success("✅ Cookie primenjen.")
    if "wolt_cookie" in st.session_state and st.session_state["wolt_cookie"]:
        session.headers["Cookie"] = st.session_state["wolt_cookie"]

    # ── Fetch Debug Log ───────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 📋 Fetch Debug Log")
    dl1, dl2 = st.columns(2)
    with dl1:
        if st.button("🔄 Osveži", key="refresh_log"): st.rerun()
    with dl2:
        if st.button("🗑️ Obriši log", key="clear_log"):
            Path("_fetch_debug.log").unlink(missing_ok=True); st.success("Log obrisan.")
    try:
        log_content = Path("_fetch_debug.log").read_text(encoding="utf-8")
        lines = log_content.strip().split("\n") if log_content.strip() else []
        if lines:
            auth_fails = [l for l in lines if "auth fail" in l]
            errors_429  = [l for l in lines if "429" in l]
            exc_lines   = [l for l in lines if "EXC" in l]
            if auth_fails: st.error(f"🔐 {len(auth_fails)} auth grešaka — cookie istekao!")
            if errors_429: st.warning(f"⚠️ {len(errors_429)}x 429 Too Many Requests")
            if exc_lines:  st.warning(f"💥 {len(exc_lines)} exception-a")
            if not auth_fails and not errors_429 and not exc_lines: st.success("✅ Nema grešaka!")
            with st.expander(f"Ceo log ({len(lines)} linija)"):
                st.code("\n".join(lines[-200:]), language=None)
            st.download_button("📥 Preuzmi log", log_content.encode("utf-8"), "fetch_debug.log", "text/plain")
        else:
            st.info("Log je prazan. Pokreni sken pa osvježi.")
    except FileNotFoundError:
        st.info("Log fajl ne postoji.")

    # ── Raw API debug ─────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🔬 Sirovi API za restoran")
    dc1, dc2 = st.columns([2,1])
    with dc1: debug_slug = st.text_input("Slug:", placeholder="mcdonalds-nis", key="debug_slug")
    with dc2: debug_city_d = st.selectbox("Grad:", CITIES, key="debug_city")
    if st.button("🔍 Dohvati JSON", key="debug_fetch") and debug_slug:
        dck = display_to_key(debug_city_d)
        dlat, dlon = CITY_COORDS.get(dck, (44.8178,20.4569))
        dyn_url = (f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{debug_slug}/dynamic/"
                   f"?lat={dlat}&lon={dlon}&selected_delivery_method=homedelivery")
        dyn_data, dyn_status = wolt_get(dyn_url)
        if dyn_data:
            with st.expander("JSON (dynamic)", expanded=True): st.json(dyn_data)
            parsed = _parse_dynamic(dyn_data)
            st.markdown("**Parsed akcije:**")
            for p in parsed: st.write(p)
            if not parsed: st.warning("Nema akcija.")
        else:
            st.warning(f"HTTP {dyn_status}")

    # ══════════════════════════════════════════════════════════════════════════
    # SYSTEM RESET
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("### 🔴 System Reset")
    st.markdown("""
    <div class='reset-zone'>
    <b>⚠️ OPASNA ZONA</b> — Ovo briše sve podatke: scan rezultate, AMM bazu, alert log, scheduler config.
    Operacija je nepovratna.
    </div>
    """, unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    reset_pw = st.text_input("🔑 Upiši lozinku za reset:", type="password", key="reset_pw_input")

    if "reset_confirmed" not in st.session_state:
        st.session_state["reset_confirmed"] = False

    col_reset1, col_reset2, _ = st.columns([1, 1, 3])

    with col_reset1:
        check_btn = st.button("🔓 Proveri lozinku", key="reset_check")
    with col_reset2:
        do_reset  = st.button("💥 RESETUJ SVE", type="primary", key="reset_execute",
                              disabled=not st.session_state["reset_confirmed"])

    if check_btn:
        if reset_pw == RESET_PASSWORD:
            st.session_state["reset_confirmed"] = True
            st.success("✅ Lozinka tačna. Klikni **RESETUJ SVE** da potvrdiš.")
        else:
            st.session_state["reset_confirmed"] = False
            st.error("❌ Pogrešna lozinka.")

    if do_reset and st.session_state["reset_confirmed"]:
        with st.spinner("🗑️ Brišem sve podatke..."):
            reset_log = system_reset()

        st.session_state["reset_confirmed"] = False

        st.success("✅ Reset završen!")
        for msg in reset_log:
            if "✅" in msg:
                st.success(msg, icon=None)
            else:
                st.warning(msg)

        st.balloons()
        time.sleep(2)
        st.rerun()
