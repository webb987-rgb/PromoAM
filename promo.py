import re
import time
import json
import random
import datetime
import smtplib
import threading
import requests
import pandas as pd
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials

# ─────────────────────────── KONFIGURACIJA ───────────────────────────────────

EMAIL_SENDER   = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"

GSHEET_ID = st.secrets["GSHEET_ID"]

CITY_KEYS    = ["Beograd", "Novi Sad", "Nis", "Kragujevac"]
CITY_DISPLAY = {
    "Beograd":    "Beograd",
    "Novi Sad":   "Novi Sad",
    "Nis":        "Niš",
    "Kragujevac": "Kragujevac",
}
CITIES = [CITY_DISPLAY[k] for k in CITY_KEYS]

FETCH_WORKERS = 2

EMAIL_IGNORE_PROMOS = [
    "0 din delivery fee for 14 days",
    "0 din delivery fee",
    "free delivery for 14 days",
    "besplatna dostava 14 dana",
    "besplatna dostava",
]

AMM_COLS   = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]
ALERT_COLS = ["timestamp", "city", "restaurant_display", "am_name", "am_email", "akcije"]

SCAN_FILE           = Path("scan_baza_item.json")
SALES_FILE          = Path("sales_baza.json")
AMM_FILE            = Path("amm_baza.csv")
ALERT_FILE          = Path("alert_log.csv")
LOCK_FILE           = Path("_scan_running.lock")
SENT_NEW_REST_FILE  = Path("sent_new_restaurants.json")
ALERT_COOLDOWN_FILE = Path("alert_cooldown.json")
COOLDOWN_DAYS       = 7

# ── Višestruke lokacije po gradu ─────────────────────────────────────────────
CITY_MULTI_COORDS = {
    "Beograd": [
        # Zapad / Zemun / Novi Beograd
        (44.8610, 20.3450),  # Autoput za Novi Sad — Zemun sever
        (44.8395, 20.3662),  # Dobanovački put — Zemun jug
        (44.8251, 20.4102),  # Omladinskih brigada — Novi Beograd centar
        (44.8130, 20.4182),  # Bulevar Zorana Đinđića — Novi Beograd istok
        (44.8050, 20.3880),  # Nehruova — Novi Beograd zapad
        # Centar / Stari grad
        (44.8255, 20.4571),  # Skenderbegova — Savamala
        (44.8180, 20.4522),  # Kralja Petra — Stari grad
        (44.8160, 20.4735),  # Bulevar despota Stefana
        (44.8042, 20.4521),  # Savska — centar
        (44.8180, 20.4620),  # Francuska
        # Vračar / Zvezdara
        (44.8001, 20.4705),  # Katanićeva — Vračar
        (44.8145, 20.4990),  # Dragoslava Srejovića — Zvezdara
        (44.8080, 20.4905),  # Veljka Dugoševića
        (44.7932, 20.4800),  # Južni bulevar
        (44.8175, 20.5182),  # Višnjička
        # Palilula / Karaburma
        (44.8160, 20.4950),  # Višnjička 17b
        (44.8100, 20.5100),  # Mirijevska
        # Voždovac / Dedinje
        (44.7925, 20.4430),  # Bulevar vojvode Putnika
        (44.7920, 20.4350),  # Bulevar vojvode Mišića
        (44.7820, 20.4550),  # Šekspirova — Dedinje
        # Čukarica / Rakovica
        (44.7760, 20.4180),  # Požeška
        (44.7500, 20.4100),  # Slavonskih brigada — Čukarica
        # Banjica / Autokomanda
        (44.7870, 20.4660),  # Ustanička
        (44.7975, 20.4650),  # Avalska
        # Bežanijska kosa / Blok 45
        (44.8070, 20.4100),  # Jurija Gagarina
    ],
    "Novi Sad": [
        (45.2671, 19.8335), (45.2500, 19.8100), (45.2850, 19.8600),
        (45.2400, 19.8700), (45.2900, 19.7900),
    ],
    "Nis": [
        (43.3209, 21.8958), (43.3050, 21.8800), (43.3350, 21.9150),
        (43.3100, 21.9300), (43.2950, 21.8700),
    ],
    "Kragujevac": [
        (44.0128, 20.9114), (44.0000, 20.8900),
        (44.0300, 20.9300), (43.9900, 20.9400),
    ],
}

CITY_COORDS = {k: v[0] for k, v in CITY_MULTI_COORDS.items()}

def get_active_coords() -> dict:
    if "custom_coords" in st.session_state:
        return st.session_state["custom_coords"]
    return CITY_MULTI_COORDS

# ─────────────────────────── PAGE CONFIG ─────────────────────────────────────

st.set_page_config(page_title="Promo Monitor – Item Level", page_icon="🏷️", layout="wide")

st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background: #f7f8fc; }
    .kpi { background:#fff; border-radius:12px; padding:18px 24px;
           box-shadow:0 2px 8px rgba(0,0,0,0.07); text-align:center; }
    .kpi-val { font-size:2.2rem; font-weight:800; color:#009de0; }
    .kpi-lbl { font-size:.85rem; color:#888; margin-top:4px; }
    div[data-testid="stDataFrame"] thead th { background:#009de0!important; color:#fff!important; }
    .timer-box { font-size:1.1rem; font-weight:700; color:#009de0; padding:6px 16px;
                 background:#e8f6fd; border-radius:8px; display:inline-block; margin-bottom:8px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────── GOOGLE SHEETS API ───────────────────────────────

@st.cache_resource
def get_gsheet_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=scopes
    )
    return gspread.authorize(creds)

def get_sheet(tab_name: str):
    client = get_gsheet_client()
    sh = client.open_by_key(GSHEET_ID)
    try:
        return sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=tab_name, rows=1000, cols=20)


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
    t = text.lower().strip().lstrip("•").strip()
    t = t.replace("[wolt+]", "").strip()
    for ignored in EMAIL_IGNORE_PROMOS:
        if ignored.lower() in t:
            return True
    return False

def filter_akcije_for_email(akcije_str: str) -> str:
    if not akcije_str or akcije_str == "-":
        return "-"
    lines = [l for l in akcije_str.split("\n") if l.strip()]
    filtered = [l for l in lines if not is_ignored_promo(l)]
    return "\n".join(filtered) if filtered else "-"

# ─────────────────────────── GOOGLE SHEETS PERSISTENTNA BAZA ─────────────────
# Keširanje u session_state — svaki Sheet se čita JEDNOM po sesiji,
# ne pri svakom re-renderu stranice (sprečava 429 quota greške).

_CACHE_KEYS = {
    "amm":   "_cache_amm",
    "alert": "_cache_alert",
    "sales": "_cache_sales",
    "scan":  "_cache_scan",
}

def _cache_get(key: str):
    return st.session_state.get(_CACHE_KEYS[key])

def _cache_set(key: str, value):
    st.session_state[_CACHE_KEYS[key]] = value

# ── SCAN ──────────────────────────────────────────────────────────────────────

def save_scan_gsheet(df: pd.DataFrame):
    try:
        ws = get_sheet("scan_baza")
        ws.clear()
        if not df.empty:
            data = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
            ws.update(data)
    except Exception as e:
        st.warning(f"GSheet scan save greška: {e}")
    df.to_json(SCAN_FILE, orient="records", force_ascii=False)
    _cache_set("scan", df)

def load_scan_gsheet() -> pd.DataFrame:
    cached = _cache_get("scan")
    if cached is not None:
        return cached
    try:
        ws = get_sheet("scan_baza")
        data = ws.get_all_records()
        if data:
            df = pd.DataFrame(data)
            _cache_set("scan", df)
            return df
    except Exception as e:
        st.warning(f"GSheet scan load greška: {e}")
    if SCAN_FILE.exists():
        try:
            df = pd.read_json(SCAN_FILE, orient="records")
            _cache_set("scan", df)
            return df
        except Exception:
            pass
    return pd.DataFrame()

def scan_meta_gsheet() -> str | None:
    cached = _cache_get("scan")
    if cached is not None and not cached.empty:
        return "dostupan (keš)"
    if SCAN_FILE.exists():
        mtime = SCAN_FILE.stat().st_mtime
        return datetime.datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S")
    return None

# ── AMM ───────────────────────────────────────────────────────────────────────

def save_amm_gsheet(df: pd.DataFrame):
    try:
        ws = get_sheet("amm_baza")
        ws.clear()
        if df.empty:
            ws.update([AMM_COLS])
        else:
            data = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
            ws.update(data)
    except Exception as e:
        st.warning(f"GSheet AMM save greška: {e}")
    df.to_csv(AMM_FILE, index=False)
    _cache_set("amm", df)

def load_amm_gsheet() -> pd.DataFrame:
    cached = _cache_get("amm")
    if cached is not None:
        return cached
    try:
        ws = get_sheet("amm_baza")
        data = ws.get_all_records()
        if data:
            df = pd.DataFrame(data)
            for c in AMM_COLS:
                if c not in df.columns:
                    df[c] = ""
            df.to_csv(AMM_FILE, index=False)
            _cache_set("amm", df)
            return df
    except Exception as e:
        st.warning(f"GSheet AMM load greška: {e}")
    if AMM_FILE.exists():
        df = pd.read_csv(AMM_FILE)
        for c in AMM_COLS:
            if c not in df.columns:
                df[c] = ""
        _cache_set("amm", df)
        return df
    empty = pd.DataFrame(columns=AMM_COLS)
    _cache_set("amm", empty)
    return empty

# ── ALERT LOG ─────────────────────────────────────────────────────────────────

def save_alert_log_gsheet(df: pd.DataFrame):
    try:
        ws = get_sheet("alert_log")
        ws.clear()
        if df.empty:
            ws.update([ALERT_COLS])
        else:
            data = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
            ws.update(data)
    except Exception as e:
        st.warning(f"GSheet alert log save greška: {e}")
    df.to_csv(ALERT_FILE, index=False)
    _cache_set("alert", df)

def load_alert_log_gsheet() -> pd.DataFrame:
    cached = _cache_get("alert")
    if cached is not None:
        return cached
    try:
        ws = get_sheet("alert_log")
        data = ws.get_all_records()
        if data:
            df = pd.DataFrame(data)
            for c in ALERT_COLS:
                if c not in df.columns:
                    df[c] = ""
            df.to_csv(ALERT_FILE, index=False)
            _cache_set("alert", df)
            return df
    except Exception as e:
        st.warning(f"GSheet alert log load greška: {e}")
    if ALERT_FILE.exists():
        df = pd.read_csv(ALERT_FILE)
        for c in ALERT_COLS:
            if c not in df.columns:
                df[c] = ""
        _cache_set("alert", df)
        return df
    empty = pd.DataFrame(columns=ALERT_COLS)
    _cache_set("alert", empty)
    return empty

def append_alert_log_gsheet(rows: list):
    try:
        ws = get_sheet("alert_log")
        existing = ws.get_all_records()
        if not existing:
            ws.update([ALERT_COLS])
        new_rows = [[r.get(c, "") for c in ALERT_COLS] for r in rows]
        ws.append_rows(new_rows)
    except Exception as e:
        st.warning(f"GSheet alert append greška: {e}")
        
    # ISPRAVKA:
    existing_df = _cache_get("alert")
    if existing_df is None:
        existing_df = pd.DataFrame(columns=ALERT_COLS)
        
    merged = pd.concat([existing_df, pd.DataFrame(rows)], ignore_index=True)
    merged.to_csv(ALERT_FILE, index=False)
    _cache_set("alert", merged)

# ── SALES ─────────────────────────────────────────────────────────────────────

def load_sales_gsheet() -> dict:
    cached = _cache_get("sales")
    if cached is not None:
        return cached
    try:
        ws = get_sheet("sales_baza")
        data = ws.get_all_records()
        if data:
            result = {}
            for row in data:
                city = row.get("city", "")
                emails_str = row.get("emails", "")
                if city:
                    result[city] = [e.strip() for e in emails_str.split(",") if e.strip()]
            _cache_set("sales", result)
            return result
    except Exception as e:
        st.warning(f"GSheet sales load greška: {e}")
    if SALES_FILE.exists():
        try:
            data = json.loads(SALES_FILE.read_text(encoding="utf-8"))
            _cache_set("sales", data)
            return data
        except Exception:
            pass
    default = {city: [] for city in CITIES}
    _cache_set("sales", default)
    return default

def save_sales_gsheet(data: dict):
    try:
        ws = get_sheet("sales_baza")
        ws.clear()
        rows = [["city", "emails"]]
        for city, emails in data.items():
            rows.append([city, ", ".join(emails)])
        ws.update(rows)
    except Exception as e:
        st.warning(f"GSheet sales save greška: {e}")
    SALES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _cache_set("sales", data)

# ── Aliasi ────────────────────────────────────────────────────────────────────
def load_amm() -> pd.DataFrame:        return load_amm_gsheet()
def save_amm(df):                       save_amm_gsheet(df)
def load_alert_log() -> pd.DataFrame:  return load_alert_log_gsheet()
def append_alert_log(rows):             append_alert_log_gsheet(rows)
def save_scan(df):                      save_scan_gsheet(df)
def load_scan() -> pd.DataFrame:       return load_scan_gsheet()
def scan_meta() -> str | None:         return scan_meta_gsheet()
def load_sales() -> dict:              return load_sales_gsheet()
def save_sales(data):                  save_sales_gsheet(data)

# ─────────────────────────── SCAN LOCK ──────────────────────────────────────

def acquire_scan_lock() -> bool:
    """Pokušaj da zauzmeš lock. Vraća True ako uspešno, False ako je već zauzet."""
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < 10800:  # 3 sata
                return False
        except Exception:
            pass
    try:
        LOCK_FILE.write_text(str(time.time()))
        return True
    except Exception:
        return False

def release_scan_lock():
    LOCK_FILE.unlink(missing_ok=True)

def is_scan_locked() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
        return age < 10800
    except Exception:
        return False

# ─────────────────────────── DEDUPLICATION NOVIH RESTORANA ───────────────────

def load_sent_new_restaurants() -> set:
    if SENT_NEW_REST_FILE.exists():
        try:
            data = json.loads(SENT_NEW_REST_FILE.read_text(encoding="utf-8"))
            return set(data)
        except Exception:
            pass
    return set()

def save_sent_new_restaurants(slugs: set):
    try:
        SENT_NEW_REST_FILE.write_text(
            json.dumps(list(slugs), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass

# ─────────────────────────── AM ALERT COOLDOWN ───────────────────────────────

def load_alert_cooldown() -> dict:
    if ALERT_COOLDOWN_FILE.exists():
        try:
            return json.loads(ALERT_COOLDOWN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_alert_cooldown(cooldown: dict):
    try:
        tmp = Path(str(ALERT_COOLDOWN_FILE) + ".tmp")
        tmp.write_text(json.dumps(cooldown, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ALERT_COOLDOWN_FILE)
    except Exception:
        pass

def is_in_cooldown(am_email: str, restaurant_norm: str, cooldown: dict) -> bool:
    key = f"{am_email}|{restaurant_norm}"
    last_sent_str = cooldown.get(key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.date.fromisoformat(last_sent_str)
        return (datetime.date.today() - last_sent).days < COOLDOWN_DAYS
    except Exception:
        return False

def update_cooldown(am_email: str, restaurant_norm: str, cooldown: dict):
    key = f"{am_email}|{restaurant_norm}"
    cooldown[key] = datetime.date.today().isoformat()

# ─────────────────────────── KEEP-ALIVE PING ─────────────────────────────────

def _keepalive_loop():
    time.sleep(30)
    while True:
        try:
            requests.get("http://localhost:8501/_stcore/health", timeout=10)
        except Exception:
            pass
        time.sleep(270)

if "keepalive_started" not in st.session_state:
    t_ka = threading.Thread(target=_keepalive_loop, daemon=True)
    t_ka.start()
    st.session_state["keepalive_started"] = True

# ─────────────────────────── WOLT API & SESSION ──────────────────────────────

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

CITY_SLUG_MAP = {
    "Beograd":    "belgrade",
    "Novi Sad":   "novi-sad",
    "Nis":        "nis",
    "Kragujevac": "kragujevac",
}

def wolt_get(url: str) -> tuple:
    try:
        r = session.get(url, timeout=15)
        if r.status_code == 200:
            return r.json(), 200
        return None, r.status_code
    except Exception:
        return None, -1

def make_thread_session() -> requests.Session:
    s = requests.Session()
    for k, v in session.headers.items():
        s.headers[k] = v
    try:
        cookie_val = Path("_scan_cookie.txt").read_text().strip()
    except Exception:
        cookie_val = ""
    if not cookie_val:
        cookie_val = WOLT_COOKIE or ""
    if cookie_val:
        s.headers["Cookie"] = cookie_val
    return s

# ─────────────────────────── FETCH AKCIJA ────────────────────────────────────

_fetch_log_lock = threading.Lock()
_throttle_until = 0.0
_throttle_lock  = threading.Lock()

def _log_fetch(msg: str):
    try:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with _fetch_log_lock:
            with open("_fetch_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def _wait_throttle():
    now = time.time()
    with _throttle_lock:
        wait = _throttle_until - now
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

def _fetch_one(slug: str, lat: float, lon: float, feed_akcije: list, stop_event: threading.Event) -> tuple[str, str]:
    if stop_event.is_set():
        return slug, "-"
    ts = make_thread_session()
    time.sleep(random.uniform(1.0, 2.0))
    dyn_url = (
        f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic/"
        f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery"
    )
    akcije_str = "-"
    dyn_data, _ = _fetch_url(ts, dyn_url, f"DYN {slug}", stop_event)
    if dyn_data:
        try:
            parsed   = _parse_dynamic_with_item_discount(dyn_data)
            combined = list(dict.fromkeys(feed_akcije + parsed))
            akcije_str = "\n".join(combined) if combined else "-"
            if akcije_str == "-":
                _log_fetch(f"DYN {slug} → 200 ali NEMA akcija")
        except Exception as e:
            _log_fetch(f"DYN {slug} → parse EXC {e}")
    elif feed_akcije:
        akcije_str = "\n".join(feed_akcije)
    return slug, akcije_str

def _parse_dynamic_with_item_discount(data: dict) -> list:
    akcije = []
    seen = set()
    ignore_texts = {
        "prikaži detalje", "show details", "vidi sve", "see all",
        "detalji restorana", "restaurant details", "more", "još",
        "schedule order", "naruči", "see menu", "add {amount} more",
        "try for 30 days for free!", "get rsd0 delivery fee & more!",
    }

    def add(text, wolt_plus=False):
        t = (text or "").strip()
        if not t or len(t) <= 3 or t.lower() in ignore_texts:
            return
        prefix = "• [Wolt+] " if wolt_plus else "• "
        key = t.lower()
        if key not in seen:
            seen.add(key)
            akcije.append(f"{prefix}{t}")

    venue_raw = data.get("venue_raw") or {}
    for disc in venue_raw.get("discounts", []):
        if not isinstance(disc, dict):
            continue
        is_wp = (disc.get("has_wolt_plus") or
                 (disc.get("banner") or {}).get("show_wolt_plus", False) or
                 (disc.get("conditions") or {}).get("has_wolt_plus") == True)
        banner = disc.get("banner") or {}
        desc   = disc.get("description") or {}
        primary_text = banner.get("formatted_text") or desc.get("title") or ""
        add(primary_text, wolt_plus=is_wp)
        effects = disc.get("effects") or {}
        item_disc = effects.get("item_discount")
        if item_disc and isinstance(item_disc, dict):
            fraction = item_disc.get("fraction")
            if fraction and float(fraction) > 0:
                pct = int(round(float(fraction) * 100))
                fallback = primary_text or f"{pct}% popust na izabrane artikle"
                add(fallback, wolt_plus=is_wp)
        basket_disc = effects.get("basket_discount")
        if basket_disc and isinstance(basket_disc, dict):
            amount   = basket_disc.get("amount")
            fraction = basket_disc.get("fraction")
            if amount and int(amount) > 0:
                rsd = int(amount) // 100
                fallback = primary_text or f"{rsd} RSD popust na korpu"
                add(fallback, wolt_plus=is_wp)
            elif fraction and float(fraction) > 0:
                pct = int(round(float(fraction) * 100))
                fallback = primary_text or f"{pct}% popust na celu korpu"
                add(fallback, wolt_plus=is_wp)
        delivery_disc = effects.get("delivery_discount")
        if delivery_disc and isinstance(delivery_disc, dict):
            amount   = delivery_disc.get("amount")
            fraction = delivery_disc.get("fraction")
            if (amount is not None and int(amount) == 0) or (fraction and float(fraction) >= 1.0):
                fallback = primary_text or "Besplatna dostava"
                add(fallback, wolt_plus=is_wp)
            elif amount and int(amount) > 0:
                rsd = int(amount) // 100
                fallback = primary_text or f"{rsd} RSD popust na dostavu"
                add(fallback, wolt_plus=is_wp)
        free_items = effects.get("free_items")
        if free_items and isinstance(free_items, (dict, list)):
            fallback = primary_text or "Gratis artikal uz porudžbinu"
            add(fallback, wolt_plus=is_wp)

    venue = data.get("venue") or {}
    for ban in venue.get("banners", []):
        if not isinstance(ban, dict):
            continue
        is_wp = ban.get("show_wolt_plus", False)
        disc = ban.get("discount") or {}
        add(disc.get("formatted_text"), wolt_plus=is_wp)

    offer_assistant = venue.get("offer_assistant") or {}
    for tracker in offer_assistant.get("offer_trackers", []):
        if not isinstance(tracker, dict):
            continue
        is_wp = tracker.get("offer_type") == "wolt_plus" or tracker.get("show_wolt_plus", False)
        add(tracker.get("title"), wolt_plus=is_wp)

    return akcije

def _parse_dynamic(data: dict) -> list:
    akcije = set()
    ignore_texts = {
        "prikaži detalje", "show details", "vidi sve", "see all",
        "detalji restorana", "restaurant details", "more", "još",
        "schedule order", "naruči", "see menu", "add {amount} more",
        "try for 30 days for free!", "get rsd0 delivery fee & more!",
    }

    def add(text, wolt_plus=False):
        t = (text or "").strip()
        if not t or len(t) <= 3 or t.lower() in ignore_texts:
            return
        prefix = "• [Wolt+] " if wolt_plus else "• "
        akcije.add(f"{prefix}{t}")

    venue_raw = data.get("venue_raw") or {}
    for disc in venue_raw.get("discounts", []):
        if not isinstance(disc, dict):
            continue
        is_wp = (disc.get("has_wolt_plus") or
                 (disc.get("banner") or {}).get("show_wolt_plus", False) or
                 (disc.get("conditions") or {}).get("has_wolt_plus") == True)
        banner = disc.get("banner") or {}
        add(banner.get("formatted_text"), wolt_plus=is_wp)
        desc = disc.get("description") or {}
        add(desc.get("title"), wolt_plus=is_wp)

    venue = data.get("venue") or {}
    for banner in venue.get("banners", []):
        if not isinstance(banner, dict):
            continue
        is_wp = banner.get("show_wolt_plus", False)
        disc = banner.get("discount") or {}
        add(disc.get("formatted_text"), wolt_plus=is_wp)

    offer_assistant = venue.get("offer_assistant") or {}
    for tracker in offer_assistant.get("offer_trackers", []):
        if not isinstance(tracker, dict):
            continue
        is_wp = tracker.get("offer_type") == "wolt_plus" or tracker.get("show_wolt_plus", False)
        add(tracker.get("title"), wolt_plus=is_wp)

    return list(akcije)

# ─────────────────────────── FETCH GRAD ──────────────────────────────────────

_city_progress = {}
_city_progress_lock = threading.Lock()

def _update_city_progress(city_display: str, found: int = None, total: int = None, status: str = None):
    with _city_progress_lock:
        if city_display not in _city_progress:
            _city_progress[city_display] = {"found": 0, "total": 0, "status": "čekanje..."}
        if found is not None:
            _city_progress[city_display]["found"] = max(_city_progress[city_display]["found"], found)
        if total is not None:
            _city_progress[city_display]["total"] = max(_city_progress[city_display]["total"], total)
        if status is not None:
            _city_progress[city_display]["status"] = status

def _write_status_file():
    with _city_progress_lock:
        data = dict(_city_progress)
    try:
        tmp = Path("_scan_city_progress.json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(Path("_scan_city_progress.json"))
    except Exception:
        pass

def fetch_city(city_display: str, status_placeholder, stop_event: threading.Event) -> list[dict]:
    city_key  = display_to_key(city_display)
    city_slug = CITY_SLUG_MAP.get(city_key)
    multi_coords = get_active_coords().get(city_key, CITY_MULTI_COORDS.get(city_key, [CITY_COORDS.get(city_key, (44.8178, 20.4569))]))
    primary_lat, primary_lon = multi_coords[0]

    if not city_slug:
        status_placeholder.error(f"❌ Nepoznat grad: '{city_display}'")
        return []

    restaurants = {}
    _update_city_progress(city_display, found=0, total=0, status="Učitavam listu restorana...")
    _write_status_file()

    for loc_idx, (lat, lon) in enumerate(multi_coords):
        if stop_event.is_set():
            break
        loc_label = f"lok. {loc_idx+1}/{len(multi_coords)}"
        skip = 0
        for page_num in range(50):
            if stop_event.is_set():
                break
            count_before = len(restaurants)
            endpoint = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}"
            data, _status = wolt_get(endpoint)
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
                        status_obj = "Otvoren" if venue.get("online") else "Zatvoren"
                        rating   = venue.get("rating") or {}
                        r_score  = rating.get("score", "-") if isinstance(rating, dict) else "-"
                        est      = venue.get("estimate_range") or venue.get("estimate")
                        delivery = f"{est} min" if est else "-"
                        feed_akcije = []
                        novo_status = "Ne"
                        for badge in venue.get("badges", []):
                            txt = badge.get("text", "")
                            if txt:
                                if txt.lower() in ["novo", "new"]:
                                    novo_status = "Da"
                                else:
                                    feed_akcije.append(f"• {txt}")
                        label = venue.get("label", "")
                        if label:
                            if label.lower() in ["novo", "new"]:
                                novo_status = "Da"
                            else:
                                feed_akcije.append(f"• {label}")
                        restaurants[slug] = {
                            "grad":         city_display,
                            "naziv":        name,
                            "slug":         slug,
                            "status":       status_obj,
                            "ocena":        str(r_score),
                            "dostava":      delivery,
                            "novo":         novo_status,
                            "_feed_akcije": feed_akcije,
                            "akcije":       "-",
                            "link":         f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}",
                            "naziv_norm":   normalize(name),
                        }
            new_this_page = len(restaurants) - count_before
            _update_city_progress(city_display, found=len(restaurants),
                                  status=f"📍 {loc_label} | str.{page_num+1} +{new_this_page} (ukupno {len(restaurants)})")
            _write_status_file()
            if items_in_response == 0:
                break
            skip += 40
            time.sleep(random.uniform(0.5, 1.8))
        _update_city_progress(city_display, status=f"✅ Lokacija {loc_idx+1}/{len(multi_coords)} gotova ({len(restaurants)} ukupno)")
        _write_status_file()

    if not restaurants or stop_event.is_set():
        if not restaurants:
            _update_city_progress(city_display, status="⚠️ Nije pronađen nijedan restoran.")
        _write_status_file()
        return []

    slugs = list(restaurants.keys())
    total = len(slugs)
    completed = 0
    _update_city_progress(city_display, total=total, found=total,
                          status=f"⚡ Učitavam akcije (0/{total})...")
    _write_status_file()

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {
            executor.submit(
                _fetch_one, slug, primary_lat, primary_lon,
                restaurants[slug]["_feed_akcije"], stop_event,
            ): slug for slug in slugs
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
            if completed % 10 == 0 or completed == total:
                _update_city_progress(city_display, status=f"⚡ Akcije: {completed}/{total} restorana")
                _write_status_file()

    for r in restaurants.values():
        r.pop("_feed_akcije", None)

    _update_city_progress(city_display, status=f"✅ Završen! {len(restaurants)} restorana")
    _write_status_file()
    return list(restaurants.values())

def scan_all_cities(selected_cities: list[str], status_placeholder, stop_event: threading.Event) -> pd.DataFrame:
    with _city_progress_lock:
        _city_progress.clear()
    for city in selected_cities:
        _update_city_progress(city, found=0, total=0, status="⏳ Čeka na red...")
    _write_status_file()
    all_rows = []
    for i, city in enumerate(selected_cities):
        if stop_event.is_set():
            break
        try:
            rows = fetch_city(city, status_placeholder, stop_event)
            all_rows.extend(rows)
        except Exception as e:
            _update_city_progress(city, status=f"❌ Greška: {e}")
            _write_status_file()
        if i < len(selected_cities) - 1 and not stop_event.is_set():
            time.sleep(0.5)
    status_placeholder.empty()
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

def scan_nopromo_cities(selected_cities: list[str], prev_df: pd.DataFrame, stop_event: threading.Event) -> pd.DataFrame:
    with _city_progress_lock:
        _city_progress.clear()
    no_promo = prev_df[
        (prev_df["grad"].isin(selected_cities)) & (prev_df["akcije"] == "-")
    ].copy()
    other = prev_df[~prev_df["grad"].isin(selected_cities)].copy()
    had_promo = prev_df[
        (prev_df["grad"].isin(selected_cities)) & (prev_df["akcije"] != "-")
    ].copy()

    for city in selected_cities:
        city_count = len(no_promo[no_promo["grad"] == city])
        _update_city_progress(city, found=city_count, total=city_count,
                              status=f"⏳ Čeka na red... ({city_count} restorana za sken)")
    _write_status_file()

    updated_rows = []
    for city in selected_cities:
        if stop_event.is_set():
            break
        city_key = display_to_key(city)
        primary_lat, primary_lon = CITY_MULTI_COORDS.get(city_key, [(44.8178, 20.4569)])[0]
        city_subset = no_promo[no_promo["grad"] == city]
        slugs = city_subset["slug"].tolist() if "slug" in city_subset.columns else []
        total = len(slugs)
        completed = 0
        _update_city_progress(city, found=total, total=total, status=f"⚡ Skeniranje akcija (0/{total})...")
        _write_status_file()
        slug_to_row = {row["slug"]: row.to_dict() for _, row in city_subset.iterrows()} if "slug" in city_subset.columns else {}

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
            futures = {
                executor.submit(_fetch_one, slug, primary_lat, primary_lon, [], stop_event): slug
                for slug in slugs
            }
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
                if completed % 10 == 0 or completed == total:
                    _update_city_progress(city, status=f"⚡ Akcije: {completed}/{total}")
                    _write_status_file()
        _update_city_progress(city, status=f"✅ Završen! {total} restorana skeniran")
        _write_status_file()

    all_parts = []
    if updated_rows:
        all_parts.append(pd.DataFrame(updated_rows))
    if not had_promo.empty:
        all_parts.append(had_promo)
    if not other.empty:
        all_parts.append(other)
    if all_parts:
        return pd.concat(all_parts, ignore_index=True)
    return prev_df.copy()

# ─────────────────────────── EMAIL ───────────────────────────────────────────

def send_alert_email(am_email: str, am_name: str, alerts: list[dict]) -> bool:
    try:
        rows_html = ""
        for a in alerts:
            akcije_filtered = filter_akcije_for_email(a["akcije"])
            if akcije_filtered != "-":
                akcije_html = akcije_filtered.replace("\n", "<br>")
            else:
                akcije_html = "<span style='color:#aaa'>–</span>"
            link = a.get("link", "")
            if link:
                naziv_cell = f"<a href='{link}' style='color:#222;text-decoration:none;font-weight:600'>{a['naziv']}</a>"
            else:
                naziv_cell = f"<span style='font-weight:600'>{a['naziv']}</span>"
            rows_html += f"""
            <tr>
              <td style='padding:10px 14px;border-bottom:1px solid #eee'>{naziv_cell}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;color:#555'>{a['grad']}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;color:#333'>{akcije_html}</td>
            </tr>"""
        if not rows_html:
            return True
        today_str = datetime.date.today().strftime("%d.%m.%Y")
        html = f"""
        <html><body style='font-family:Arial,sans-serif;color:#222;max-width:720px;margin:auto'>
          <div style='background:#1a1a2e;padding:24px 32px;border-radius:12px 12px 0 0'>
            <h2 style='color:#fff;margin:0'>📊 Promo Monitor – {today_str}</h2>
          </div>
          <div style='background:#fff;padding:24px 32px;border-radius:0 0 12px 12px;
                      box-shadow:0 4px 16px rgba(0,0,0,0.08)'>
            <p>Zdravo <b>{am_name}</b>,</p>
            <p>Sledeći tvoji partneri imaju <b>aktivne promotivne akcije</b>:</p>
            <table style='border-collapse:collapse;width:100%;font-size:14px'>
              <thead>
                <tr style='background:#f0f4ff'>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Restoran</th>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Grad</th>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Akcije</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <p style='margin-top:20px;font-size:12px;color:#999'>
              Automatski izveštaj &bull; {local_now()}
            </p>
          </div>
        </body></html>"""
        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = am_email
        msg["Subject"] = f"📊 Promo izveštaj – {len(alerts)} partnera – {today_str}"
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, am_email, msg.as_string())
        return True
    except Exception as e:
        st.error(f"Email greška ({am_email}): {e}")
        return False

def send_sales_bulk_notification(to_email: str, grad: str, novi_restorani: list) -> bool:
    """Šalje JEDAN mail prodavcu sa svim novim restoranima u gradu."""
    try:
        rows_html = ""
        for r in novi_restorani:
            naziv = r.get("naziv", "")
            slug  = r.get("slug", "")
            grad_slug = (grad.lower()
                         .replace(" ", "-")
                         .replace("š", "s").replace("Š", "s")
                         .replace("ć", "c").replace("Ć", "c")
                         .replace("č", "c").replace("Č", "c")
                         .replace("đ", "dj").replace("Đ", "dj")
                         .replace("ž", "z").replace("Ž", "z"))
            wolt_link = f"https://wolt.com/sr/srb/{grad_slug}/restaurant/{slug}"
            rows_html += f"""
            <tr>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;font-weight:600'>{naziv}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee'>
                <a href='{wolt_link}' style='color:#009de0'>{wolt_link}</a>
              </td>
            </tr>"""

        today_str = datetime.date.today().strftime("%d.%m.%Y")
        html = f"""
        <html><body style='font-family:Arial,sans-serif;background:#f5f5f5;padding:20px'>
          <div style='max-width:680px;margin:auto;background:#fff;border-radius:12px;
                      padding:28px;box-shadow:0 2px 8px rgba(0,0,0,0.08)'>
            <div style='background:#009de0;color:#fff;padding:16px 24px;border-radius:8px;
                        margin-bottom:20px;font-size:1.2rem;font-weight:700'>
              🆕 Novi restorani na Woltu — {grad} — {today_str}
            </div>
            <p>Detektovani su novi restorani u gradu <b>{grad}</b> koji nemaju dodeljenog Account Managera:</p>
            <table style='border-collapse:collapse;width:100%;font-size:14px'>
              <thead>
                <tr style='background:#f0f4ff'>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Restoran</th>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Wolt link</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <p style='margin-top:20px;color:#666;font-size:0.9rem'>
              Ukupno novih restorana: <b>{len(novi_restorani)}</b><br>
              Molimo dodelite odgovornog AM-a što pre.
            </p>
            <p style='font-size:11px;color:#999;margin-top:20px'>
              Automatski izveštaj &bull; Promo Monitor &bull; {local_now()}
            </p>
          </div>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = to_email
        msg["Subject"] = f"🆕 {len(novi_restorani)} novi restoran(a) — {grad} — {today_str}"
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, to_email, msg.as_string())
        return True
    except Exception as e:
        _log_fetch(f"SALES BULK MAIL greška → {to_email}: {e}")
        return False

# ─────────────────────────── AUTO-SCHEDULER ──────────────────────────────────

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
    import logging
    log = logging.getLogger("scheduler")
    cfg = load_scheduler_config()
    if not cfg.get("enabled"):
        return

    # Ne pokreći ako je već aktivan scan
    if not acquire_scan_lock():
        log.warning("[Scheduler] Scan već u toku, preskačem zakazani sken.")
        return

    stop_ev = threading.Event()

    class NullPH:
        def info(self, *a, **kw): pass
        def warning(self, *a, **kw): pass
        def success(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def empty(self, *a, **kw): pass

    try:
        df = scan_all_cities(cfg["cities"], NullPH(), stop_ev)
    except Exception as e:
        log.error(f"[Scheduler] Greška pri skenu: {e}")
        release_scan_lock()
        return

    if df.empty:
        release_scan_lock()
        return

    save_scan(df)

    # ── SALES: bulk mail o novim restoranima ──────────────────────────────────
    sent_slugs   = load_sent_new_restaurants()
    amm_df_curr  = load_amm()
    sales_cfg    = load_sales()
    novi_df      = df[df["novo"] == "Da"].copy() if "novo" in df.columns else pd.DataFrame()

    if not novi_df.empty:
        novi_po_gradu  = {}
        new_sent_slugs = set(sent_slugs)
        for _, row in novi_df.iterrows():
            naziv = row.get("naziv", "")
            grad  = row.get("grad", "")
            slug  = row.get("slug", "")
            norm  = normalize(naziv)
            if slug in sent_slugs:
                continue
            has_am = False
            if not amm_df_curr.empty:
                has_am = not amm_df_curr[
                    (amm_df_curr["restaurant_norm"] == norm) & (amm_df_curr["city"] == grad)
                ].empty
            if not has_am:
                if grad not in novi_po_gradu:
                    novi_po_gradu[grad] = []
                novi_po_gradu[grad].append({"naziv": naziv, "slug": slug})
                new_sent_slugs.add(slug)
        for grad, restorani in novi_po_gradu.items():
            for email in sales_cfg.get(grad, []):
                ok = send_sales_bulk_notification(email, grad, restorani)
                if ok:
                    log.info(f"[Scheduler] Bulk sales mail → {email} ({grad}): {len(restorani)} restorana")
        save_sent_new_restaurants(new_sent_slugs)

    # ── AM ALERTOVI sa cooldown-om ─────────────────────────────────────────────
    if amm_df_curr.empty:
        release_scan_lock()
        return

    df["naziv_norm"] = df["naziv"].apply(normalize)
    merged = df.merge(
        amm_df_curr[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
        left_on="naziv_norm", right_on="restaurant_norm", how="inner"
    )

    cooldown = load_alert_cooldown()
    sent_log = []

    for (am_name, am_email_addr), grp in merged.groupby(["am_name", "am_email"]):
        alerts = []
        for _, row in grp.iterrows():
            akcije_filtered = filter_akcije_for_email(row["akcije"])
            if akcije_filtered == "-":
                continue
            rest_norm = normalize(row["naziv"])
            if is_in_cooldown(am_email_addr, rest_norm, cooldown):
                continue
            alerts.append({
                "naziv": row["naziv"],
                "grad":  row["grad"],
                "akcije": row["akcije"],
                "link":  row.get("link", ""),
                "norm":  rest_norm,
            })
        if not alerts:
            continue
        ok = send_alert_email(am_email_addr, am_name, alerts)
        if ok:
            for a in alerts:
                update_cooldown(am_email_addr, a["norm"], cooldown)
                sent_log.append({
                    "timestamp":          local_now(),
                    "city":               a["grad"],
                    "restaurant_display": a["naziv"],
                    "am_name":            am_name,
                    "am_email":           am_email_addr,
                    "akcije":             a["akcije"],
                })

    save_alert_cooldown(cooldown)
    if sent_log:
        append_alert_log(sent_log)

    release_scan_lock()

def _scheduler_loop():
    while True:
        cfg = load_scheduler_config()
        if cfg.get("enabled"):
            now = datetime.datetime.now()
            target = now.replace(hour=cfg["hour"], minute=cfg["minute"], second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            time.sleep(wait_sec)
            run_scheduled_scan_and_send()
        else:
            time.sleep(60)

if "scheduler_started" not in st.session_state:
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    st.session_state["scheduler_started"] = True

# ─────────────────────────── SESSION STATE ───────────────────────────────────

if "df_wolt" not in st.session_state:
    st.session_state.df_wolt = pd.DataFrame()
if "last_scan" not in st.session_state:
    st.session_state.last_scan = None
if "scan_stop_event" not in st.session_state:
    st.session_state.scan_stop_event = threading.Event()
if "scan_running" not in st.session_state:
    st.session_state.scan_running = False
if "scan_start_time" not in st.session_state:
    st.session_state.scan_start_time = None
if "scan_mode" not in st.session_state:
    st.session_state.scan_mode = "full"
if "scan_duration_last" not in st.session_state:
    st.session_state.scan_duration_last = None

# ─────────────────────────── UI ──────────────────────────────────────────────

st.title("🏷️ Promo Monitor – Item Level")
st.caption("Skenira item-level popuste: ulazi u svaki restoran i proverava da li ima makar jedan snižen proizvod.")

tab_scan, tab_amm, tab_alert, tab_stats, tab_sched, tab_debug, tab_reset = st.tabs([
    "🔍 Scan & Rezultati",
    "👥 AMM Baza",
    "📧 Pošalji Alert",
    "📈 Statistika",
    "⏰ Auto-Scheduler",
    "🔧 Debug API",
    "🗑️ Reset & Backup",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: SCAN
# ══════════════════════════════════════════════════════════════════════════════
with tab_scan:
    st.markdown("### 🔍 Scan")
    selected_cities = st.multiselect("📍 Gradovi za skeniranje:", options=CITIES, default=CITIES, key="selected_cities")

    prev_df_for_nopromo = st.session_state.df_wolt
    nopromo_available = not prev_df_for_nopromo.empty
    if nopromo_available:
        no_promo_count = len(prev_df_for_nopromo[
            prev_df_for_nopromo["grad"].isin(selected_cities) &
            (prev_df_for_nopromo["akcije"] == "-")
        ]) if selected_cities else 0
    else:
        no_promo_count = 0

    col_btn, col_btn2, col_stop, col_info = st.columns([1.2, 1.5, 0.9, 2.4])
    with col_btn:
        run_scan = st.button("▶️ Full Scan", type="primary", use_container_width=True,
                             disabled=not selected_cities or st.session_state.scan_running)
    with col_btn2:
        run_nopromo = st.button(f"🔍 No Promo Scan ({no_promo_count})", use_container_width=True,
                                disabled=not selected_cities or st.session_state.scan_running or not nopromo_available or no_promo_count == 0)
    with col_stop:
        stop_scan = st.button("⏹️ Zaustavi", use_container_width=True,
                              disabled=not st.session_state.scan_running, type="secondary")
    with col_info:
        if st.session_state.last_scan:
            st.info(f"⏱️ Poslednji scan: **{st.session_state.last_scan}** | Ukupno restorana: **{len(st.session_state.df_wolt)}**")
        if not selected_cities:
            st.warning("Izaberi bar jedan grad.")

    prev_meta = scan_meta()
    if prev_meta and not st.session_state.scan_running:
        load_col, _ = st.columns([2, 4])
        with load_col:
            if st.button(f"📂 Učitaj prethodni sken ({prev_meta})", use_container_width=True):
                prev_df = load_scan()
                if not prev_df.empty:
                    st.session_state.df_wolt = prev_df
                    st.session_state.last_scan = prev_meta
                    st.success(f"✅ Učitan prethodni sken – {len(prev_df)} restorana.")
                    st.rerun()
                else:
                    st.error("Greška pri učitavanju fajla.")

    if stop_scan and st.session_state.scan_running:
        st.session_state.scan_stop_event.set()
        st.warning("⏹️ Zaustavljanje...")

    if run_nopromo and selected_cities and not st.session_state.scan_running and nopromo_available:
        if not acquire_scan_lock():
            st.error("⛔ Scan je već aktivan (drugi korisnik ili zakazani sken). Pokušaj malo kasnije.")
        else:
            st.session_state.scan_stop_event = threading.Event()
            st.session_state.scan_running = True
            st.session_state.scan_mode = "nopromo"
            st.session_state.scan_start_time = time.time()
            _cities_snap = list(selected_cities)
            _stop_ev_snap = st.session_state.scan_stop_event
            _prev_df_snap = st.session_state.df_wolt.copy()
            Path("_scan_done.txt").unlink(missing_ok=True)
            Path("_scan_result.json").unlink(missing_ok=True)
            with _city_progress_lock:
                _city_progress.clear()
                for _c in _cities_snap:
                    _cnt = len(_prev_df_snap[(_prev_df_snap["grad"] == _c) & (_prev_df_snap["akcije"] == "-")])
                    _city_progress[_c] = {"found": _cnt, "total": _cnt, "status": f"⏳ Čeka na red... ({_cnt} res.)"}
            _write_status_file()

            def _run_nopromo_bg():
                try:
                    result = scan_nopromo_cities(_cities_snap, _prev_df_snap, _stop_ev_snap)
                    if result is not None and not result.empty:
                        result.to_json("_scan_result.json", orient="records", force_ascii=False)
                finally:
                    release_scan_lock()
                    Path("_scan_done.txt").write_text("1")

            threading.Thread(target=_run_nopromo_bg, daemon=True).start()
            st.rerun()

    if run_scan and selected_cities and not st.session_state.scan_running:
        if not acquire_scan_lock():
            st.error("⛔ Scan je već aktivan (drugi korisnik ili zakazani sken). Pokušaj malo kasnije.")
        else:
            st.session_state.scan_stop_event = threading.Event()
            st.session_state.scan_running = True
            st.session_state.scan_mode = "full"
            st.session_state.scan_start_time = time.time()
            _cities_snap = list(selected_cities)
            _stop_ev_snap = st.session_state.scan_stop_event
            Path("_scan_done.txt").unlink(missing_ok=True)
            Path("_scan_result.json").unlink(missing_ok=True)
            with _city_progress_lock:
                _city_progress.clear()
                for _c in _cities_snap:
                    _city_progress[_c] = {"found": 0, "total": 0, "status": "⏳ Čeka na red..."}
            _write_status_file()

            def _run_scan_bg():
                try:
                    class LivePH:
                        def info(self, msg, *a, **k): Path("_scan_status.txt").write_text(str(msg))
                        def warning(self, msg, *a, **k): Path("_scan_status.txt").write_text("⚠️ " + str(msg))
                        def success(self, msg, *a, **k): Path("_scan_status.txt").write_text("✅ " + str(msg))
                        def error(self, msg, *a, **k): Path("_scan_status.txt").write_text("❌ " + str(msg))
                        def empty(self, *a, **k): pass
                    result = scan_all_cities(_cities_snap, LivePH(), _stop_ev_snap)
                    if result is not None and not result.empty:
                        result.to_json("_scan_result.json", orient="records", force_ascii=False)
                finally:
                    release_scan_lock()
                    Path("_scan_done.txt").write_text("1")

            threading.Thread(target=_run_scan_bg, daemon=True).start()
            st.rerun()

    scan_done_flag = Path("_scan_done.txt").exists()

    if st.session_state.scan_running and not scan_done_flag:
        elapsed = time.time() - (st.session_state.scan_start_time or time.time())
        m2, s2 = divmod(int(elapsed), 60)
        st.markdown(f"### 🔄 Skeniranje u toku — {m2:02d}:{s2:02d}")
        city_prog = {}
        for _ in range(3):
            try:
                raw = Path("_scan_city_progress.json").read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed:
                    city_prog = parsed
                    break
            except Exception:
                time.sleep(0.05)
        if city_prog:
            st.session_state["_last_city_prog"] = city_prog
        else:
            city_prog = st.session_state.get("_last_city_prog", {})
        if city_prog:
            cols = st.columns(len(city_prog))
            for i, (city_name, info) in enumerate(city_prog.items()):
                with cols[i]:
                    found  = info.get("found", 0)
                    cstatus = info.get("status", "...")
                    is_done = "✅" in cstatus
                    color = "#27ae60" if is_done else "#009de0"
                    st.markdown(f"""
                    <div style='background:#fff;border-radius:10px;padding:14px 16px;
                                box-shadow:0 2px 8px rgba(0,0,0,0.08);
                                border-top:4px solid {color};margin-bottom:8px'>
                      <div style='font-weight:800;font-size:1.1rem;color:{color}'>{city_name}</div>
                      <div style='font-size:1.8rem;font-weight:900;color:#222'>{found}</div>
                      <div style='font-size:0.75rem;color:#888'>restorana pronađeno</div>
                      <div style='font-size:0.8rem;color:#555;margin-top:6px'>{cstatus}</div>
                    </div>
                    """, unsafe_allow_html=True)
        time.sleep(5)
        st.rerun()

    if st.session_state.scan_running and scan_done_flag:
        Path("_scan_done.txt").unlink(missing_ok=True)
        st.session_state.scan_running = False
        scan_duration = time.time() - (st.session_state.scan_start_time or time.time())
        _stop_ev = st.session_state.scan_stop_event
        try:
            df_result = pd.read_json("_scan_result.json", orient="records")
        except Exception:
            df_result = pd.DataFrame()
        if df_result is not None and not df_result.empty:
            st.session_state.df_wolt = df_result
            st.session_state.last_scan = local_now()
            st.session_state.scan_duration_last = scan_duration
            save_scan(df_result)
            # Obavesti sales o novim restoranima — bulk mail po gradu
            novi_df = df_result[df_result["novo"] == "Da"].copy() if "novo" in df_result.columns else pd.DataFrame()
            if not novi_df.empty:
                sent_slugs    = load_sent_new_restaurants()
                amm_check     = load_amm()
                sales_cfg     = load_sales()
                novi_po_gradu = {}
                new_sent_slugs = set(sent_slugs)
                for _, row in novi_df.iterrows():
                    naziv = row.get("naziv", "")
                    grad  = row.get("grad", "")
                    slug  = row.get("slug", "")
                    norm  = normalize(naziv)
                    if slug in sent_slugs:
                        continue
                    has_am = False
                    if not amm_check.empty:
                        has_am = not amm_check[
                            (amm_check["restaurant_norm"] == norm) & (amm_check["city"] == grad)
                        ].empty
                    if not has_am:
                        if grad not in novi_po_gradu:
                            novi_po_gradu[grad] = []
                        novi_po_gradu[grad].append({"naziv": naziv, "slug": slug})
                        new_sent_slugs.add(slug)
                notified = 0
                for grad_key, restorani in novi_po_gradu.items():
                    for email in sales_cfg.get(grad_key, []):
                        if send_sales_bulk_notification(email, grad_key, restorani):
                            notified += 1
                save_sent_new_restaurants(new_sent_slugs)
                if notified:
                    total_novi = sum(len(v) for v in novi_po_gradu.values())
                    st.info(f"📬 Poslato **{notified}** bulk obaveštenja sales agentima ({total_novi} novih restorana).")
            m, s = divmod(int(scan_duration), 60)
            scan_mode_done = st.session_state.get("scan_mode", "full")
            if scan_mode_done == "nopromo":
                newly_found = len(df_result[df_result["akcije"] != "-"])
                st.success(f"✅ No Promo Scan završen za **{m:02d}:{s:02d}**! Od prethodno preskočenih, **{newly_found}** restorana sada ima akcije.")
            else:
                st.success(f"✅ Full Scan završen za **{m:02d}:{s:02d}**! Pronađeno **{len(df_result)}** restorana, **{len(df_result[df_result['akcije'] != '-'])}** sa akcijama.")
            st.rerun()
        else:
            if _stop_ev.is_set():
                st.warning("⏹️ Scan je zaustavljen.")
            else:
                st.error("❌ Scan nije vratio podatke.")

    df = st.session_state.df_wolt
    if not df.empty:
        if st.session_state.scan_duration_last:
            m_t, s_t = divmod(int(st.session_state.scan_duration_last), 60)
            st.markdown(f"<div style='background:#e8f8f0;border-left:4px solid #27ae60;padding:8px 16px;border-radius:6px;margin-bottom:12px;font-size:0.95rem;color:#155724'>⏱️ Poslednji sken trajao: <strong>{m_t:02d}:{s_t:02d}</strong></div>", unsafe_allow_html=True)
        st.markdown("---")

        k1, k2, k3, k4, k5 = st.columns(5)
        total        = len(df)
        sa_akcijama  = len(df[df["akcije"] != "-"])
        otvoreni     = len(df[df["status"] == "Otvoren"])
        novi         = len(df[df["novo"] == "Da"])
        sa_wolt_plus = len(df[df["akcije"].apply(lambda c: bool(re.search(r'\[Wolt\+\]|Wolt\+|W\+', c, re.IGNORECASE)) if pd.notna(c) else False)])

        for col, val, lbl in [
            (k1, total, "Ukupno restorana"), (k2, sa_akcijama, "Ima akciju"),
            (k3, sa_wolt_plus, "💙 Wolt+ akcije"),
            (k4, otvoreni, "Trenutno otvoreno"), (k5, novi, "Novi restorani"),
        ]:
            with col:
                st.markdown(f"<div class='kpi'><div class='kpi-val'>{val}</div><div class='kpi-lbl'>{lbl}</div></div>", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        grad_summary = df.groupby("grad").agg(
            Restorana=("naziv", "count"),
            Sa_akcijama=("akcije", lambda x: (x != "-").sum()),
            Otvoreni=("status", lambda x: (x == "Otvoren").sum()),
        ).reset_index()
        gs_cols = st.columns(len(grad_summary))
        for i, row in grad_summary.iterrows():
            with gs_cols[i]:
                pct = int(row["Sa_akcijama"] / row["Restorana"] * 100) if row["Restorana"] > 0 else 0
                st.markdown(f"""
                <div style='background:#fff;border-radius:10px;padding:12px 16px;box-shadow:0 2px 8px rgba(0,0,0,0.07);border-top:3px solid #009de0;text-align:center'>
                  <div style='font-weight:800;color:#009de0;font-size:1rem'>{row["grad"]}</div>
                  <div style='font-size:1.6rem;font-weight:900'>{int(row["Restorana"])}</div>
                  <div style='font-size:0.75rem;color:#888'>restorana</div>
                  <div style='margin-top:4px;font-size:0.85rem;color:#27ae60'>{int(row["Sa_akcijama"])} akcija ({pct}%)</div>
                  <div style='font-size:0.75rem;color:#555'>{int(row["Otvoreni"])} otvorenih</div>
                </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Filteri — kompaktno u dva reda ───────────────────────────────────
        ff1, ff2, ff3, ff4, ff5, ff6 = st.columns([2, 1, 1, 1, 1, 2])
        with ff1: grad_filter = st.multiselect("📍 Grad:", CITIES, default=CITIES, key="scan_grad")
        with ff2: samo_akcije = st.checkbox("📌 Sa akcijama", value=False, key="scan_akcije")
        with ff3: samo_wolt_plus = st.checkbox("💙 Wolt+", value=False, key="scan_wolt_plus")
        with ff4: samo_pct_popust = st.checkbox("🔢 % popust", value=False, key="scan_pct_popust")
        with ff5: samo_otvoreni = st.checkbox("🟢 Otvoreni", value=False, key="scan_otvoreni")
        with ff6: search = st.text_input("🔎 Pretraži naziv:", key="scan_search", placeholder="naziv restorana...")

        ff7, ff8 = st.columns([1, 3])
        with ff7: samo_novi = st.checkbox("🆕 Samo novi restorani", value=False, key="scan_novi")
        with ff8:
            sve_akcije_tekst = sorted(set(
                line.lstrip("• ").strip()
                for akcije_cell in df["akcije"] if akcije_cell != "-"
                for line in akcije_cell.split("\n") if line.strip() and line.strip() != "-"
            ))
            akcija_filter = st.multiselect("🎯 Filtriraj po tipu akcije:", options=sve_akcije_tekst, default=[], key="scan_akcija_filter", placeholder="Sve akcije...")

        fdf = df[df["grad"].isin(grad_filter)]
        if samo_akcije: fdf = fdf[fdf["akcije"] != "-"]
        if samo_novi: fdf = fdf[fdf["novo"] == "Da"]
        if samo_otvoreni: fdf = fdf[fdf["status"] == "Otvoren"]
        if search.strip(): fdf = fdf[fdf["naziv"].str.contains(search.strip(), case=False, na=False)]
        if akcija_filter:
            fdf = fdf[fdf["akcije"].apply(lambda cell: any(a in cell for a in akcija_filter) if cell != "-" else False)]
        if samo_wolt_plus:
            fdf = fdf[fdf["akcije"].apply(lambda cell: bool(re.search(r'\[Wolt\+\]|Wolt\+|W\+', cell, re.IGNORECASE)) if cell != "-" else False)]
        if samo_pct_popust:
            fdf = fdf[fdf["akcije"].str.contains(r'\d+\s*%', na=False, regex=True)]

        display_cols = ["grad", "naziv", "status", "ocena", "dostava", "novo", "akcije", "link"]
        display_cols = [c for c in display_cols if c in fdf.columns]
        st.dataframe(fdf[display_cols].reset_index(drop=True), use_container_width=True, hide_index=True, height=480,
            column_config={
                "grad": st.column_config.TextColumn("Grad"), "naziv": st.column_config.TextColumn("Restoran"),
                "status": st.column_config.TextColumn("Status"), "ocena": st.column_config.TextColumn("Ocena"),
                "dostava": st.column_config.TextColumn("Dostava"), "novo": st.column_config.TextColumn("Novi"),
                "akcije": st.column_config.TextColumn("Akcije", width="large"),
                "link": st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
            })
        csv = fdf[display_cols].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Preuzmi CSV", csv, "scan.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: AMM BAZA
# ══════════════════════════════════════════════════════════════════════════════
with tab_amm:
    st.markdown("### 👥 Baza Account Managera")
    st.caption("Definiši koji AM je zadužen za koji restoran. Čuva se na Google Sheets-u (tab `amm_baza`).")

    amm_df  = load_amm()
    df_wolt = st.session_state.df_wolt

    st.markdown("---")
    st.markdown("#### 📬 Sales agenti po gradu")
    sales_data = load_sales()

    for city in CITIES:
        emails_current = sales_data.get(city, [])
        col_city, col_email, col_save = st.columns([1, 3, 1])
        with col_city: st.markdown(f"**{city}**")
        with col_email:
            new_emails_str = st.text_input(f"Email(ovi) za {city}:", value=", ".join(emails_current),
                                           placeholder="sales@firma.com", key=f"sales_email_{city}", label_visibility="collapsed")
        with col_save:
            if st.button("💾", key=f"sales_save_{city}"):
                parsed = [e.strip() for e in new_emails_str.split(",") if e.strip()]
                sales_data[city] = parsed
                save_sales(sales_data)
                st.success(f"✅ {city}: {len(parsed)} email(a) sačuvano.")
                st.rerun()

    st.markdown("---")
    st.markdown("#### ⚡ Bulk dodela")

    if df_wolt.empty:
        st.info("Pokreni scan prvo da bi se restorani prikazali.")
    else:
        bulk_am_opts = sorted(amm_df["am_name"].dropna().unique().tolist()) if not amm_df.empty else []
        if not bulk_am_opts:
            st.warning("Nema AM-ova u bazi. Dodaj AM-a ispod pa se vrati ovde.")
        else:
            b_col1, b_col2 = st.columns([1, 3])
            with b_col1: bulk_selected_am = st.selectbox("Izaberi AM:", bulk_am_opts, key="bulk_am_sel")
            with b_col2: bulk_grad = st.multiselect("Filtriraj po gradu:", CITIES, default=CITIES, key="bulk_grad_filt")

            if bulk_selected_am:
                am_row = amm_df[amm_df["am_name"] == bulk_selected_am].iloc[0]
                am_email_bulk = am_row["am_email"]
                bulk_df = df_wolt[df_wolt["grad"].isin(bulk_grad)][["naziv", "grad"]].drop_duplicates().copy()
                already = amm_df[amm_df["am_name"] == bulk_selected_am]["restaurant_display"].tolist()
                bulk_df["✅ Dodeli"] = bulk_df["naziv"].isin(already)
                edited_bulk = st.data_editor(bulk_df.reset_index(drop=True), use_container_width=True,
                    hide_index=True, height=400,
                    column_config={
                        "✅ Dodeli": st.column_config.CheckboxColumn("Dodeli ovom AM-u", default=False),
                        "naziv": st.column_config.TextColumn("Restoran", disabled=True),
                        "grad": st.column_config.TextColumn("Grad", disabled=True),
                    }, key="bulk_editor")

                if st.button("💾 Sačuvaj bulk dodelu", key="bulk_save"):
                    selected_rows = edited_bulk[edited_bulk["✅ Dodeli"] == True]
                    new_rows = []
                    for _, row in selected_rows.iterrows():
                        norm = normalize(row["naziv"])
                        city_v = row["grad"]
                        mask = (amm_df["restaurant_norm"] == norm) & (amm_df["city"] == city_v)
                        if mask.any():
                            amm_df.loc[mask, ["am_name", "am_email"]] = [bulk_selected_am, am_email_bulk]
                        else:
                            new_rows.append({"restaurant_norm": norm, "restaurant_display": row["naziv"],
                                            "city": city_v, "am_name": bulk_selected_am, "am_email": am_email_bulk})
                    if new_rows:
                        amm_df = pd.concat([amm_df, pd.DataFrame(new_rows)], ignore_index=True)
                    save_amm(amm_df)
                    st.success(f"✅ Dodeljeno {len(selected_rows)} restorana → {bulk_selected_am}")
                    st.rerun()

    st.markdown("---")
    st.markdown("#### ➕ Dodaj / ažuriraj pojedinačno")

    rest_options = sorted(df_wolt["naziv"].dropna().unique().tolist()) if not df_wolt.empty else []
    a1, a2 = st.columns([2, 1])
    with a1: sel_rest = st.selectbox("Restoran:", ["-- Odaberi --"] + rest_options, key="amm_sel")
    with a2: man_rest = st.text_input("Ili upiši ručno:", placeholder="npr. KFC", key="amm_man")

    final_rest = man_rest.strip() if man_rest.strip() else (sel_rest if sel_rest != "-- Odaberi --" else "")
    b1, b2, b3, b4 = st.columns(4)
    with b1: amm_city  = st.selectbox("Grad:", ["-- Svi --"] + CITIES, key="amm_city_sel")
    with b2: amm_name  = st.text_input("Ime AM-a:", key="amm_name")
    with b3: amm_email = st.text_input("Email AM-a:", key="amm_email")
    with b4:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("💾 Sačuvaj", use_container_width=True, key="amm_save"):
            if not final_rest:
                st.error("Izaberi ili upiši naziv restorana.")
            elif not amm_name or not amm_email:
                st.error("Upiši ime i email AM-a.")
            else:
                norm = normalize(final_rest)
                city_val = "" if amm_city == "-- Svi --" else amm_city
                mask = (amm_df["restaurant_norm"] == norm) & (amm_df["city"] == city_val)
                if mask.any():
                    amm_df.loc[mask, ["restaurant_display", "am_name", "am_email"]] = [final_rest, amm_name, amm_email]
                else:
                    amm_df = pd.concat([amm_df, pd.DataFrame([{"restaurant_norm": norm, "restaurant_display": final_rest,
                                        "city": city_val, "am_name": amm_name, "am_email": amm_email}])], ignore_index=True)
                save_amm(amm_df)
                st.success(f"✅ Sačuvano na Google Sheets: **{final_rest}** → {amm_name}")
                st.rerun()

    st.markdown("---")
    st.markdown("#### 📋 Trenutna baza")
    if amm_df.empty:
        st.info("Baza je prazna.")
    else:
        am_opts = ["Svi"] + sorted(amm_df["am_name"].dropna().unique().tolist())
        am_filt = st.selectbox("Filtriraj po AM-u:", am_opts, key="amm_view_filt")
        view = amm_df if am_filt == "Svi" else amm_df[amm_df["am_name"] == am_filt]
        edited = st.data_editor(view.reset_index(drop=True), use_container_width=True, num_rows="dynamic",
            hide_index=True, column_config={
                "restaurant_norm": st.column_config.TextColumn("Norm naziv", disabled=True),
                "restaurant_display": st.column_config.TextColumn("Restoran"),
                "city": st.column_config.TextColumn("Grad"),
                "am_name": st.column_config.TextColumn("Ime AM-a"),
                "am_email": st.column_config.TextColumn("Email AM-a"),
            }, key="amm_editor")
        if st.button("💾 Sačuvaj izmene", key="amm_save_tbl"):
            if am_filt == "Svi":
                save_amm(edited)
            else:
                rest_df = amm_df[amm_df["am_name"] != am_filt]
                save_amm(pd.concat([rest_df, edited], ignore_index=True))
            st.success("✅ Baza ažurirana na Google Sheets!")
            st.rerun()

    st.markdown("---")
    st.markdown("#### 📥 Bulk import CSV")
    uploaded = st.file_uploader("CSV fajl:", type="csv", key="amm_upload")
    if uploaded:
        try:
            new_df = pd.read_csv(uploaded)
            new_df["restaurant_norm"] = new_df["restaurant_display"].apply(normalize)
            merged_amm = pd.concat([amm_df, new_df], ignore_index=True).drop_duplicates(subset=["restaurant_norm", "city"], keep="last")
            save_amm(merged_amm)
            st.success(f"✅ Importovano {len(new_df)} redova.")
            st.rerun()
        except Exception as e:
            st.error(f"Greška: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: POŠALJI ALERT
# ══════════════════════════════════════════════════════════════════════════════
with tab_alert:
    st.markdown("### 📧 Pošalji Alert AM-ovima")
    df_wolt = st.session_state.df_wolt
    amm_df  = load_amm()

    if df_wolt.empty:
        st.warning("⚠️ Nema scan podataka.")
    elif amm_df.empty:
        st.warning("⚠️ AMM baza je prazna.")
    else:
        df_wolt["naziv_norm"] = df_wolt["naziv"].apply(normalize)
        merged = df_wolt.merge(
            amm_df[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
            left_on="naziv_norm", right_on="restaurant_norm", how="inner"
        )
        merged["_alert"] = merged.apply(lambda row: filter_akcije_for_email(row["akcije"]) != "-", axis=1)
        sa_akcijama = merged[merged["_alert"]].copy()

        if sa_akcijama.empty:
            st.info("✅ Nijedan partner trenutno nema relevantne akcije.")
        else:
            sa_akcijama["akcije_email"] = sa_akcijama["akcije"].apply(filter_akcije_for_email)
            af1, af2 = st.columns(2)
            with af1: grad_filt_a = st.multiselect("Grad:", CITIES, default=CITIES, key="alert_grad")
            with af2: am_filt_a = st.multiselect("AM:", sorted(sa_akcijama["am_name"].dropna().unique().tolist()),
                                                  default=sorted(sa_akcijama["am_name"].dropna().unique().tolist()), key="alert_am")
            preview = sa_akcijama[(sa_akcijama["grad"].isin(grad_filt_a)) & (sa_akcijama["am_name"].isin(am_filt_a))]
            st.caption(f"Partnera za alert: **{len(preview)}** | AM-ova: **{preview['am_name'].nunique()}**")
            preview_cols = ["grad", "naziv", "am_name", "am_email", "akcije_email", "link"]
            preview_cols = [c for c in preview_cols if c in preview.columns]
            st.dataframe(preview[preview_cols].reset_index(drop=True), use_container_width=True, hide_index=True, height=350,
                column_config={
                    "grad": st.column_config.TextColumn("Grad"), "naziv": st.column_config.TextColumn("Restoran"),
                    "am_name": st.column_config.TextColumn("AM"), "am_email": st.column_config.TextColumn("Email"),
                    "akcije_email": st.column_config.TextColumn("Akcije", width="large"),
                    "link": st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
                })
            st.markdown("---")
            if st.button("🚀 Pošalji alertove", type="primary"):
                cooldown = load_alert_cooldown()
                am_groups = preview.groupby(["am_name", "am_email"])
                sent_log = []
                success_count = 0
                skipped_count = 0
                for (am_name, am_email_addr), grp in am_groups:
                    alerts = []
                    for _, row in grp.iterrows():
                        rest_norm = normalize(row["naziv"])
                        if is_in_cooldown(am_email_addr, rest_norm, cooldown):
                            skipped_count += 1
                            continue
                        alerts.append({
                            "naziv": row["naziv"], "grad": row["grad"],
                            "akcije": row["akcije"], "link": row.get("link", ""),
                            "norm": rest_norm,
                        })
                    if not alerts:
                        continue
                    ok = send_alert_email(am_email_addr, am_name, alerts)
                    if ok:
                        success_count += 1
                        st.success(f"✅ Mail poslat: **{am_name}** – {len(alerts)} partnera")
                        for a in alerts:
                            update_cooldown(am_email_addr, a["norm"], cooldown)
                            sent_log.append({"timestamp": local_now(), "city": a["grad"],
                                            "restaurant_display": a["naziv"], "am_name": am_name,
                                            "am_email": am_email_addr, "akcije": a["akcije"]})
                    else:
                        st.error(f"❌ Greška: {am_name}")
                save_alert_cooldown(cooldown)
                if sent_log:
                    append_alert_log(sent_log)
                if skipped_count:
                    st.info(f"ℹ️ {skipped_count} partnera preskočeno (cooldown {COOLDOWN_DAYS} dana).")
                st.markdown(f"**Završeno:** {success_count}/{am_groups.ngroups} AM-ova kontaktirano.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: STATISTIKA
# ══════════════════════════════════════════════════════════════════════════════
with tab_stats:
    st.markdown("### 📈 Statistika alerta po Account Manageru")
    log_df = load_alert_log()
    if log_df.empty:
        st.info("Još nema poslatih alerta.")
    else:
        log_df["timestamp"] = pd.to_datetime(log_df["timestamp"], errors="coerce")
        min_d = log_df["timestamp"].min().date()
        max_d = log_df["timestamp"].max().date()
        s1, s2 = st.columns(2)
        with s1: date_from = st.date_input("Od:", min_d, key="s_from")
        with s2: date_to   = st.date_input("Do:", max_d, key="s_to")
        flog = log_df[(log_df["timestamp"].dt.date >= date_from) & (log_df["timestamp"].dt.date <= date_to)]
        if flog.empty:
            st.warning("Nema podataka za period.")
        else:
            k1, k2, k3, k4 = st.columns(4)
            for col, val, lbl, color in [
                (k1, len(flog), "Ukupno alerta", "#009de0"),
                (k2, flog["am_name"].nunique(), "AM-ova", "#8e44ad"),
                (k3, flog["restaurant_display"].nunique(), "Restorana", "#27ae60"),
                (k4, flog["timestamp"].dt.date.nunique(), "Dana sa alertima", "#e67e22"),
            ]:
                with col:
                    st.markdown(f"<div class='kpi' style='border-top:4px solid {color}'><div class='kpi-val' style='color:{color}'>{val}</div><div class='kpi-lbl'>{lbl}</div></div>", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            am_stats = (flog.groupby(["am_name", "am_email"]).agg(
                Slanja=("timestamp", lambda x: x.dt.date.nunique()),
                Restorana=("restaurant_display", "nunique"),
                Ukupno_alerta=("restaurant_display", "count"),
                Poslednji=("timestamp", "max"),
            ).reset_index().rename(columns={"am_name": "AM", "am_email": "Email"}).sort_values("Ukupno_alerta", ascending=False))
            am_stats["Poslednji"] = am_stats["Poslednji"].dt.strftime("%d.%m.%Y %H:%M")
            st.dataframe(am_stats, use_container_width=True, hide_index=True)

            st.markdown("---")
            am_log_sel = st.selectbox("Filtriraj po AM-u:", ["Svi"] + sorted(flog["am_name"].dropna().unique().tolist()), key="log_am_sel")
            log_view = flog if am_log_sel == "Svi" else flog[flog["am_name"] == am_log_sel]
            log_view = log_view.sort_values("timestamp", ascending=False).copy()
            log_view["timestamp"] = log_view["timestamp"].dt.strftime("%d.%m.%Y %H:%M")
            st.dataframe(log_view, use_container_width=True, hide_index=True, height=400)
            st.download_button("📥 Eksportuj log", log_view.to_csv(index=False).encode("utf-8"), "alert_log.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5: AUTO-SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════
with tab_sched:
    st.markdown("### ⏰ Automatski dnevni sken i slanje")
    cfg = load_scheduler_config()
    sc1, sc2, sc3 = st.columns(3)
    with sc1: sched_enabled = st.toggle("✅ Uključi automatsko slanje", value=cfg.get("enabled", False))
    with sc2: sched_hour = st.number_input("Sat (0–23):", min_value=0, max_value=23, value=cfg.get("hour", 8))
    with sc3: sched_min = st.number_input("Minut (0–59):", min_value=0, max_value=59, value=cfg.get("minute", 0))
    sched_cities = st.multiselect("Gradovi:", options=CITIES, default=cfg.get("cities", CITIES))
    if st.button("💾 Sačuvaj podešavanja", type="primary"):
        save_scheduler_config({"enabled": sched_enabled, "hour": int(sched_hour), "minute": int(sched_min), "cities": sched_cities})
        st.success(f"✅ Sačuvano! Automatski sken {'UKLJUČEN' if sched_enabled else 'ISKLJUČEN'} u **{int(sched_hour):02d}:{int(sched_min):02d}**.")

    st.markdown("---")
    st.markdown("#### 🧪 Test – pokreni ručno")
    sched_running = st.session_state.get("sched_running", False)
    sched_done    = st.session_state.get("sched_done", False)
    if st.button("▶️ Pokreni test sken + slanje sada", disabled=sched_running):
        st.session_state["sched_running"] = True
        st.session_state["sched_done"] = False
        def _run_sched_bg():
            run_scheduled_scan_and_send()
            st.session_state["sched_running"] = False
            st.session_state["sched_done"] = True
        threading.Thread(target=_run_sched_bg, daemon=True).start()
        st.rerun()
    if sched_running:
        st.info("🔄 U toku...")
        time.sleep(3)
        st.rerun()
    if sched_done:
        st.session_state["sched_done"] = False
        st.success("✅ Test završen.")

    st.markdown("---")
    cfg_cur = load_scheduler_config()
    if cfg_cur.get("enabled"):
        now = datetime.datetime.now()
        target = now.replace(hour=cfg_cur["hour"], minute=cfg_cur["minute"], second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        diff = target - now
        h, rem = divmod(int(diff.total_seconds()), 3600)
        m_rem = rem // 60
        st.success(f"🕐 Sledeći automatski sken za: **{h}h {m_rem}min** (u {cfg_cur['hour']:02d}:{cfg_cur['minute']:02d})")
    else:
        st.warning("Automatski sken je isključen.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 6: DEBUG API
# ══════════════════════════════════════════════════════════════════════════════
with tab_debug:
    st.markdown("### 🔧 Debug & Podešavanja")

    _debug_pass = st.text_input("🔑 Lozinka za pristup:", type="password", key="debug_pass_input")
    if _debug_pass != "zekapeka":
        st.warning("Unesite lozinku za pristup Debug tabu.")
        st.stop()

    st.markdown("#### 📊 Google Sheets Status")
    try:
        client = get_gsheet_client()
        sh = client.open_by_key(GSHEET_ID)
        st.success(f"✅ Google Sheets konekcija OK — Sheet: **{sh.title}**")
        tabs = [ws.title for ws in sh.worksheets()]
        st.info(f"Tabovi u Sheetu: {', '.join(tabs)}")
    except Exception as e:
        st.error(f"❌ Google Sheets greška: {e}")

    st.markdown("---")
    st.markdown("#### 📍 Lokacije po gradu")
    if "custom_coords" not in st.session_state:
        st.session_state["custom_coords"] = {k: list(v) for k, v in CITY_MULTI_COORDS.items()}

    for city_key in CITY_KEYS:
        city_disp = CITY_DISPLAY.get(city_key, city_key)
        coords_list = st.session_state["custom_coords"].get(city_key, [])
        with st.expander(f"📍 {city_disp} — {len(coords_list)} lokacija", expanded=False):
            coords_text = "\n".join(f"{lat}, {lon}" for lat, lon in coords_list)
            new_text = st.text_area(f"Koordinate za {city_disp}:", value=coords_text,
                                    height=max(120, len(coords_list) * 35 + 40),
                                    key=f"coords_input_{city_key}", label_visibility="collapsed")
            col_save_c, col_reset_c = st.columns(2)
            with col_save_c:
                if st.button("💾 Sačuvaj koordinate", key=f"save_coords_{city_key}"):
                    parsed_coords = []
                    errors = []
                    for i, line in enumerate(new_text.strip().split("\n")):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            parts = [p.strip().replace(",", ".") for p in line.replace(";", ",").split(",")]
                            if len(parts) >= 2:
                                lat_v, lon_v = float(parts[0]), float(parts[1])
                                if -90 <= lat_v <= 90 and -180 <= lon_v <= 180:
                                    parsed_coords.append((lat_v, lon_v))
                                else:
                                    errors.append(f"Red {i+1}: van opsega")
                            else:
                                errors.append(f"Red {i+1}: format nije `lat, lon`")
                        except ValueError:
                            errors.append(f"Red {i+1}: nije broj")
                    if errors:
                        for err in errors: st.error(err)
                    elif not parsed_coords:
                        st.error("Nema validnih koordinata.")
                    else:
                        st.session_state["custom_coords"][city_key] = parsed_coords
                        st.success(f"✅ {len(parsed_coords)} lokacija sačuvano.")
            with col_reset_c:
                if st.button("↩️ Vrati default", key=f"reset_coords_{city_key}"):
                    st.session_state["custom_coords"][city_key] = list(CITY_MULTI_COORDS[city_key])
                    st.success("↩️ Resetovano.")
                    st.rerun()

    st.markdown("---")
    st.markdown("### 🔬 Sirovi API odgovor")
    dc1, dc2 = st.columns([2, 1])
    with dc1: debug_slug = st.text_input("Slug restorana:", placeholder="npr. mcdonalds-nis", key="debug_slug")
    with dc2: debug_city_display = st.selectbox("Grad:", CITIES, key="debug_city")

    if st.button("🔍 Dohvati JSON", key="debug_fetch") and debug_slug:
        debug_city_key = display_to_key(debug_city_display)
        lat, lon  = CITY_COORDS.get(debug_city_key, (44.8178, 20.4569))
        city_slug = CITY_SLUG_MAP.get(debug_city_key, "belgrade")
        dyn_url = (f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{debug_slug}/dynamic/"
                   f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery")
        dyn_data, dyn_status = wolt_get(dyn_url)
        if dyn_data:
            with st.expander("Pun JSON", expanded=True):
                st.json(dyn_data)
            parsed = _parse_dynamic(dyn_data)
            st.markdown("**Parsed akcije:**")
            for p in parsed: st.write(p)
            if not parsed: st.warning("Nema parsiranih akcija.")
        else:
            st.warning(f"Nije vratio podatke. HTTP: {dyn_status}")

    st.markdown("---")
    st.markdown("### 📋 Fetch Debug Log")
    col_log1, col_log2 = st.columns(2)
    with col_log1:
        if st.button("🔄 Osveži log"): st.rerun()
    with col_log2:
        if st.button("🗑️ Obriši log"):
            Path("_fetch_debug.log").unlink(missing_ok=True)
            st.success("Log obrisan.")
    try:
        log_content = Path("_fetch_debug.log").read_text(encoding="utf-8")
        if log_content.strip():
            lines = log_content.strip().split("\n")
            st.code("\n".join(lines[-200:]), language=None)
        else:
            st.info("Log je prazan.")
    except FileNotFoundError:
        st.info("Log ne postoji.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 7: RESET & BACKUP
# ══════════════════════════════════════════════════════════════════════════════
with tab_reset:
    st.markdown("### 🗑️ Reset sistema")
    RESET_PASSWORD = "zekapeka"

    st.markdown("#### 💾 Backup")
    if st.button("📦 Kreiraj backup (CSV download)", key="backup_btn"):
        df_wolt_bk = st.session_state.df_wolt
        if not df_wolt_bk.empty:
            st.download_button("⬇️ Preuzmi scan CSV", df_wolt_bk.to_csv(index=False).encode("utf-8"),
                               file_name=f"scan_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                               mime="text/csv", key="backup_scan_dl")
        amm_bk = load_amm()
        if not amm_bk.empty:
            st.download_button("⬇️ Preuzmi AMM CSV", amm_bk.to_csv(index=False).encode("utf-8"),
                               file_name="amm_backup.csv", mime="text/csv", key="backup_amm_dl")

    st.markdown("---")
    st.markdown("#### ⚠️ Reset operacije")
    reset_pass = st.text_input("🔑 Lozinka:", type="password", key="reset_pass_input")
    pass_ok = reset_pass == RESET_PASSWORD

    r1, r2, r3, r4 = st.columns(4)
    with r1:
        st.markdown("**Reset logova**")
        if st.button("🗑️ Obriši logove", key="reset_logs", disabled=not pass_ok):
            Path("_fetch_debug.log").unlink(missing_ok=True)
            st.success("✅ Logovi obrisani.")
    with r2:
        st.markdown("**Reset AMM baze**")
        if st.button("🗑️ Obriši AMM bazu", key="reset_amm", disabled=not pass_ok):
            empty_amm = pd.DataFrame(columns=AMM_COLS)
            save_amm(empty_amm)
            st.success("✅ AMM baza obrisana.")
    with r3:
        st.markdown("**Reset scan rezultata**")
        if st.button("🗑️ Obriši scan", key="reset_scan", disabled=not pass_ok):
            st.session_state.df_wolt = pd.DataFrame()
            st.session_state.last_scan = None
            st.session_state.scan_duration_last = None
            SCAN_FILE.unlink(missing_ok=True)
            Path("_scan_result.json").unlink(missing_ok=True)
            save_scan_gsheet(pd.DataFrame())
            st.success("✅ Scan obrisan.")
            st.rerun()
    with r4:
        st.markdown("**Reset SVE**")
        if st.button("💥 RESET SVE", key="reset_all", type="primary", disabled=not pass_ok):
            st.session_state.df_wolt = pd.DataFrame()
            st.session_state.last_scan = None
            for f in ["_scan_result.json", "_scan_done.txt", "_scan_status.txt", "_scan_city_progress.json", "_fetch_debug.log"]:
                Path(f).unlink(missing_ok=True)
            SCAN_FILE.unlink(missing_ok=True)
            save_amm(pd.DataFrame(columns=AMM_COLS))
            save_alert_log_gsheet(pd.DataFrame(columns=ALERT_COLS))
            st.success("💥 Sve obrisano!")
            st.rerun()

    if reset_pass and not pass_ok:
        st.error("❌ Pogrešna lozinka.")
