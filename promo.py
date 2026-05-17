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
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ─────────────────────────── KONFIGURACIJA ───────────────────────────────────

EMAIL_SENDER   = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"

GITHUB_TOKEN = "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"
GITHUB_REPO  = "webb987-rgb/PromoAM"
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

# ── Višestruke lokacije po gradu za kompletno pokrivanje ─────────────────────
CITY_MULTI_COORDS = {
    "Beograd": [
        (44.8178, 20.4569),  # centar
        (44.7866, 20.4489),  # Voždovac
        (44.8525, 20.3914),  # Zemun
        (44.8010, 20.5132),  # Zvezdara
        (44.8650, 20.6432),  # Pančevo granica
        (44.7700, 20.3900),  # Rakovica
        (44.8300, 20.5800),  # Palilula sever
    ],
    "Novi Sad": [
        (45.2671, 19.8335),  # centar
        (45.2500, 19.8100),  # Liman
        (45.2850, 19.8600),  # Salajka
        (45.2400, 19.8700),  # Adice
        (45.2900, 19.7900),  # Futog granica
    ],
    "Nis": [
        (43.3209, 21.8958),  # centar
        (43.3050, 21.8800),  # Pantelej
        (43.3350, 21.9150),  # Palilula
        (43.3100, 21.9300),  # Niška Banja
        (43.2950, 21.8700),  # Crveni Krst
    ],
    "Kragujevac": [
        (44.0128, 20.9114),  # centar
        (44.0000, 20.8900),  # Aerodrom
        (44.0300, 20.9300),  # Points north
        (43.9900, 20.9400),  # Points south
    ],
}

# Primarna koordinata (prva u listi) za svaki grad
CITY_COORDS = {k: v[0] for k, v in CITY_MULTI_COORDS.items()}

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
    .city-progress { background:#fff; border-radius:8px; padding:12px 16px;
                     margin:4px 0; box-shadow:0 1px 4px rgba(0,0,0,0.06);
                     font-size:0.9rem; }
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
            r = requests.get(url, headers=GITHUB_HEADERS, timeout=15)
        else:
            r = requests.put(url, headers=GITHUB_HEADERS,
                             json=payload, timeout=15)
        return r
    except Exception as e:
        return None

def github_read(path: str) -> str | None:
    """Čita fajl sa GitHub-a, vraća string sadržaj ili None."""
    r = _gh_api("GET", path)
    if r and r.status_code == 200:
        content = r.json().get("content", "")
        return base64.b64decode(content).decode("utf-8")
    return None

def github_write(path: str, content: str, message: str = "update") -> bool:
    """Upisuje fajl na GitHub (create ili update)."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    # Dohvati sha za update
    r = _gh_api("GET", path)
    sha = r.json().get("sha") if r and r.status_code == 200 else None
    payload = {
        "message": message,
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
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

# ─────────────────────────── GITHUB PERSISTENTNA BAZA ───────────────────────

def save_scan_github(df: pd.DataFrame):
    """Čuva scan rezultate na GitHub."""
    content = df.to_json(orient="records", force_ascii=False)
    ok = github_write("scan_baza_item.json", content, "scan: update results")
    if not ok:
        df.to_json(SCAN_FILE, orient="records", force_ascii=False)

def load_scan_github() -> pd.DataFrame:
    """Učitava scan sa GitHub-a, fallback na lokalni fajl."""
    content = github_read("scan_baza_item.json")
    if content:
        try:
            return pd.read_json(content)  # type: ignore
        except Exception:
            pass
    if SCAN_FILE.exists():
        try:
            return pd.read_json(SCAN_FILE, orient="records")
        except Exception:
            pass
    return pd.DataFrame()

def scan_meta_github() -> str | None:
    """Vraća datum poslednje izmene scan fajla sa GitHub-a."""
    r = _gh_api("GET", "scan_baza_item.json")
    if r and r.status_code == 200:
        # GitHub ne vraća mtime direktno, koristimo commit info
        try:
            commits_url = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path=scan_baza_item.json&per_page=1"
            cr = requests.get(commits_url, headers=GITHUB_HEADERS, timeout=10)
            if cr.status_code == 200 and cr.json():
                ts = cr.json()[0]["commit"]["committer"]["date"]
                dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local_dt = dt.astimezone()
                return local_dt.strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            pass
        return "dostupan"
    if SCAN_FILE.exists():
        mtime = SCAN_FILE.stat().st_mtime
        return datetime.datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S")
    return None

def save_amm_github(df: pd.DataFrame):
    """Čuva AMM bazu na GitHub + lokalno."""
    content = df.to_csv(index=False)
    github_write("amm_baza.csv", content, "amm: update")
    df.to_csv(AMM_FILE, index=False)

def load_amm_github() -> pd.DataFrame:
    """Učitava AMM sa GitHub-a, fallback lokalno."""
    content = github_read("amm_baza.csv")
    if content:
        try:
            from io import StringIO
            df = pd.read_csv(StringIO(content))
            for c in AMM_COLS:
                if c not in df.columns:
                    df[c] = ""
            df.to_csv(AMM_FILE, index=False)
            return df
        except Exception:
            pass
    return load_amm_local()

def load_amm_local() -> pd.DataFrame:
    if AMM_FILE.exists():
        df = pd.read_csv(AMM_FILE)
        for c in AMM_COLS:
            if c not in df.columns:
                df[c] = ""
        return df
    return pd.DataFrame(columns=AMM_COLS)

def save_alert_log_github(df: pd.DataFrame):
    content = df.to_csv(index=False)
    github_write("alert_log.csv", content, "alert: log update")
    df.to_csv(ALERT_FILE, index=False)

def load_alert_log_github() -> pd.DataFrame:
    content = github_read("alert_log.csv")
    if content:
        try:
            from io import StringIO
            df = pd.read_csv(StringIO(content))
            for c in ALERT_COLS:
                if c not in df.columns:
                    df[c] = ""
            df.to_csv(ALERT_FILE, index=False)
            return df
        except Exception:
            pass
    return load_alert_log_local()

def load_alert_log_local() -> pd.DataFrame:
    if ALERT_FILE.exists():
        df = pd.read_csv(ALERT_FILE)
        for c in ALERT_COLS:
            if c not in df.columns:
                df[c] = ""
        return df
    return pd.DataFrame(columns=ALERT_COLS)

def append_alert_log(rows: list):
    df_new = pd.DataFrame(rows)
    existing = load_alert_log_github()
    merged = pd.concat([existing, df_new], ignore_index=True)
    save_alert_log_github(merged)

# Aliasi za kompatibilnost
def load_amm() -> pd.DataFrame:
    return load_amm_github()

def save_amm(df: pd.DataFrame):
    save_amm_github(df)

def load_alert_log() -> pd.DataFrame:
    return load_alert_log_github()

def save_scan(df: pd.DataFrame):
    save_scan_github(df)

def load_scan() -> pd.DataFrame:
    return load_scan_github()

def scan_meta() -> str | None:
    return scan_meta_github()

# ─────────────────────────── KEEP-ALIVE PING ─────────────────────────────────

def _keepalive_loop():
    """Pinga sopstvenu Streamlit stranicu svakih 5 minuta da ne zaspi."""
    time.sleep(30)
    while True:
        try:
            requests.get("http://localhost:8501/_stcore/health", timeout=10)
        except Exception:
            pass
        time.sleep(270)  # 4.5 minuta

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

# ─────────────────────────── FETCH AKCIJA (PARALELNO) ────────────────────────

_fetch_log_lock = threading.Lock()
_throttle_until = 0.0
_throttle_lock  = threading.Lock()

def _log_fetch(msg: str):
    try:
        with _fetch_log_lock:
            with open("_fetch_debug.log", "a", encoding="utf-8") as f:
                f.write(msg + "\n")
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
            time.sleep(random.uniform(0.3, 1.2))  # jitter pre svakog requesta
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
    """
    Šalje JEDAN request na assortment API.
    Ako bilo koji artikal ima discounted_price < base_price,
    restoran se označava sa najvećim pronađenim procentom popusta.
    """
    if stop_event.is_set():
        return slug, "-"

    ts = make_thread_session()
    assortment_url = (
        f"https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment"
    )

    try:
        r = ts.get(assortment_url, timeout=12)
        if r.status_code != 200:
            _log_fetch(f"{slug} → {r.status_code}")
            return slug, "-"
        data = r.json()
    except Exception as e:
        _log_fetch(f"{slug} → EXC {e}")
        return slug, "-"

    max_pct = 0
    for item in data.get("items", []):
        if stop_event.is_set():
            return slug, "-"
        base_price       = item.get("base_price")
        discounted_price = item.get("discounted_price")
        if (
            base_price is not None
            and discounted_price is not None
            and isinstance(base_price, (int, float))
            and isinstance(discounted_price, (int, float))
            and base_price > 0
            and discounted_price < base_price
        ):
            pct = int(round((1 - discounted_price / base_price) * 100))
            if pct > max_pct:
                max_pct = pct

    if max_pct > 0:
        _log_fetch(f"{slug} → sniženje do {max_pct}%")
        return slug, f"• Sniženje do {max_pct}%"

    return slug, "-"


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
        is_wp = (disc.get("has_wolt_plus") or (disc.get("banner") or {}).get("show_wolt_plus", False) or (disc.get("conditions") or {}).get("has_wolt_plus") == True)

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
        is_wp = (disc.get("has_wolt_plus") or (disc.get("banner") or {}).get("show_wolt_plus", False) or (disc.get("conditions") or {}).get("has_wolt_plus") == True)
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


def _safe_price(val) -> float:
    if isinstance(val, (int, float)):
        return float(val) / 100
    return 0.0

# ─────────────────────────── FETCH GRAD (VIŠESTRUKE LOKACIJE) ────────────────

# Globalni counter za praćenje progresa po gradovima
_city_progress = {}  # {city_display: {"found": int, "total": int, "status": str}}
_city_progress_lock = threading.Lock()

def _update_city_progress(city_display: str, found: int = None, total: int = None, status: str = None):
    with _city_progress_lock:
        if city_display not in _city_progress:
            _city_progress[city_display] = {"found": 0, "total": 0, "status": "čekanje..."}
        if found is not None:
            _city_progress[city_display]["found"] = found
        if total is not None:
            _city_progress[city_display]["total"] = total
        if status is not None:
            _city_progress[city_display]["status"] = status

def fetch_city(city_display: str, status_placeholder, stop_event: threading.Event) -> list[dict]:
    city_key  = display_to_key(city_display)
    city_slug = CITY_SLUG_MAP.get(city_key)
    multi_coords = CITY_MULTI_COORDS.get(city_key, [CITY_COORDS.get(city_key, (44.8178, 20.4569))])
    primary_lat, primary_lon = multi_coords[0]

    if not city_slug:
        status_placeholder.error(f"❌ Nepoznat grad: '{city_display}'")
        return []

    restaurants = {}
    _update_city_progress(city_display, found=0, total=0, status="Učitavam listu restorana...")
    _write_status_file()

    # ── Faza 1: Skupljamo sve restorane sa VIŠE lokacija ─────────────────────
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

    # ── Faza 2: Paralelno fetchovanje akcija ─────────────────────────────────
    slugs = list(restaurants.keys())
    total = len(slugs)
    completed = 0
    _update_city_progress(city_display, total=total, found=total,
                          status=f"⚡ Učitavam akcije (0/{total})...")
    _write_status_file()

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {
            executor.submit(
                _fetch_one,
                slug,
                primary_lat,
                primary_lon,
                restaurants[slug]["_feed_akcije"],
                stop_event,
            ): slug
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
            if completed % 10 == 0 or completed == total:
                _update_city_progress(city_display,
                                      status=f"⚡ Akcije: {completed}/{total} restorana")
                _write_status_file()

    for r in restaurants.values():
        r.pop("_feed_akcije", None)

    _update_city_progress(city_display, status=f"✅ Završen! {len(restaurants)} restorana")
    _write_status_file()

    return list(restaurants.values())


def _write_status_file():
    """Upisuje progres svih gradova u fajl za prikaz u UI-u."""
    with _city_progress_lock:
        data = dict(_city_progress)
    try:
        Path("_scan_city_progress.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def scan_all_cities(selected_cities: list[str], status_placeholder, stop_event: threading.Event) -> pd.DataFrame:
    # Reset progresa
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
    """
    No Promo Scan – skenira samo restorane koji u prethodnom skenu NISU imali akcije.
    Ne prikuplja novu listu restorana, koristi već poznate slugove iz prev_df.
    Rezultat merguje sa prev_df: ažurira akcije za skenirane, čuva stare za preskočene.
    """
    with _city_progress_lock:
        _city_progress.clear()

    # Filtriraj prev_df na izabrane gradove i restorane bez akcija
    no_promo = prev_df[
        (prev_df["grad"].isin(selected_cities)) &
        (prev_df["akcije"] == "-")
    ].copy()

    # Za gradove koji nisu u selected_cities – zadrži ih nepromenjene
    other = prev_df[~prev_df["grad"].isin(selected_cities)].copy()

    # Za izabrane gradove koji su imali akcije – zadrži ih nepromenjene
    had_promo = prev_df[
        (prev_df["grad"].isin(selected_cities)) &
        (prev_df["akcije"] != "-")
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

        _update_city_progress(city, found=total, total=total,
                              status=f"⚡ Skeniranje akcija (0/{total})...")
        _write_status_file()

        slug_to_row = {row["slug"]: row.to_dict() for _, row in city_subset.iterrows()} if "slug" in city_subset.columns else {}

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
            futures = {
                executor.submit(
                    _fetch_one, slug, primary_lat, primary_lon, [], stop_event
                ): slug
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

    # Merge: updated + had_promo + other
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

    log.info(f"[Scheduler] Pokrenuo sken za gradove: {cfg['cities']}")
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
        return

    if df.empty:
        log.warning("[Scheduler] Sken vratio prazan rezultat.")
        return

    save_scan(df)
    log.info(f"[Scheduler] Sken završen, {len(df)} restorana.")

    amm_df = load_amm()
    if amm_df.empty:
        return

    df["naziv_norm"] = df["naziv"].apply(normalize)
    merged = df.merge(
        amm_df[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
        left_on="naziv_norm", right_on="restaurant_norm", how="inner"
    )

    def should_alert(row):
        return filter_akcije_for_email(row["akcije"]) != "-"

    merged["_alert"] = merged.apply(should_alert, axis=1)
    sa_akcijama = merged[merged["_alert"]].copy()

    sent_log = []
    for (am_name, am_email_addr), grp in sa_akcijama.groupby(["am_name", "am_email"]):
        alerts = [
            {"naziv": row["naziv"], "grad": row["grad"],
             "akcije": row["akcije"], "link": row.get("link", "")}
            for _, row in grp.iterrows()
        ]
        ok = send_alert_email(am_email_addr, am_name, alerts)
        if ok:
            for a in alerts:
                sent_log.append({
                    "timestamp": local_now(), "city": a["grad"],
                    "restaurant_display": a["naziv"], "am_name": am_name,
                    "am_email": am_email_addr, "akcije": a["akcije"],
                })

    if sent_log:
        append_alert_log(sent_log)


def _scheduler_loop():
    import logging
    log = logging.getLogger("scheduler")
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

# ─────────────────────────── UI ──────────────────────────────────────────────

st.title("🏷️ Promo Monitor – Item Level")
st.caption("Skenira item-level popuste: ulazi u svaki restoran i proverava da li ima makar jedan snižen proizvod.")

tab_scan, tab_amm, tab_alert, tab_stats, tab_sched, tab_debug = st.tabs([
    "🔍 Scan & Rezultati",
    "👥 AMM Baza",
    "📧 Pošalji Alert",
    "📈 Statistika",
    "⏰ Auto-Scheduler",
    "🔧 Debug API",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: SCAN
# ══════════════════════════════════════════════════════════════════════════════
with tab_scan:
    st.markdown("### 🔍 Scan")

    selected_cities = st.multiselect(
        "📍 Gradovi za skeniranje:",
        options=CITIES,
        default=CITIES,
        key="selected_cities",
    )

    # Info o prethodnom skenu i broju restorana bez akcija
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
        run_scan = st.button(
            "▶️ Full Scan", type="primary",
            use_container_width=True,
            disabled=not selected_cities or st.session_state.scan_running,
            help="Skenira sve restorane u izabranim gradovima od nule.",
        )
    with col_btn2:
        run_nopromo = st.button(
            f"🔍 No Promo Scan ({no_promo_count})",
            use_container_width=True,
            disabled=not selected_cities or st.session_state.scan_running or not nopromo_available or no_promo_count == 0,
            help=f"Skenira samo {no_promo_count} restorana koji prošli put NISU imali akcije. Brže!",
        )
    with col_stop:
        stop_scan = st.button(
            "⏹️ Zaustavi",
            use_container_width=True,
            disabled=not st.session_state.scan_running,
            type="secondary",
        )
    with col_info:
        if st.session_state.last_scan:
            st.info(f"⏱️ Poslednji scan: **{st.session_state.last_scan}** | "
                    f"Ukupno restorana: **{len(st.session_state.df_wolt)}**")
        if not selected_cities:
            st.warning("Izaberi bar jedan grad.")

    # Dugme za učitavanje prethodnog skena sa GitHub-a
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
        st.warning("⏹️ Zaustavljanje... čeka se da threadovi završe.")

    if run_nopromo and selected_cities and not st.session_state.scan_running and nopromo_available:
        cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
        if cookie:
            session.headers["Cookie"] = cookie
        elif "Cookie" in session.headers:
            del session.headers["Cookie"]

        st.session_state.scan_stop_event = threading.Event()
        st.session_state.scan_running = True
        st.session_state.scan_mode = "nopromo"
        st.session_state.scan_start_time = time.time()

        _cities_snap = list(selected_cities)
        _stop_ev_snap = st.session_state.scan_stop_event
        _prev_df_snap = st.session_state.df_wolt.copy()

        Path("_scan_done.txt").unlink(missing_ok=True)
        Path("_scan_result.json").unlink(missing_ok=True)
        Path("_scan_status.txt").write_text("🔍 No Promo Scan – priprema...")

        with _city_progress_lock:
            _city_progress.clear()
            for _c in _cities_snap:
                _cnt = len(_prev_df_snap[(_prev_df_snap["grad"] == _c) & (_prev_df_snap["akcije"] == "-")])
                _city_progress[_c] = {"found": _cnt, "total": _cnt, "status": f"⏳ Čeka na red... ({_cnt} res.)"}
        _write_status_file()

        _cookie_snap = st.session_state.get("wolt_cookie", "") or WOLT_COOKIE or ""
        Path("_scan_cookie.txt").write_text(_cookie_snap)

        def _run_nopromo_bg():
            Path("_scan_status.txt").write_text("🔍 No Promo Scan u toku...")
            result = scan_nopromo_cities(_cities_snap, _prev_df_snap, _stop_ev_snap)
            if result is not None and not result.empty:
                result.to_json("_scan_result.json", orient="records", force_ascii=False)
            Path("_scan_done.txt").write_text("1")
            Path("_scan_status.txt").write_text("✅ No Promo Scan završen!")

        bg = threading.Thread(target=_run_nopromo_bg, daemon=True)
        bg.start()
        st.rerun()

    if run_scan and selected_cities and not st.session_state.scan_running:
        cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
        if cookie:
            session.headers["Cookie"] = cookie
        elif "Cookie" in session.headers:
            del session.headers["Cookie"]

        st.session_state.scan_stop_event = threading.Event()
        st.session_state.scan_running = True
        st.session_state.scan_mode = "full"
        st.session_state.scan_start_time = time.time()

        _cities_snap = list(selected_cities)
        _stop_ev_snap = st.session_state.scan_stop_event

        Path("_scan_done.txt").unlink(missing_ok=True)
        Path("_scan_result.json").unlink(missing_ok=True)
        Path("_scan_status.txt").write_text("🔄 Priprema skena...")

        # Inicijalizuj progress SAMO za izabrane gradove, pre starta threada
        with _city_progress_lock:
            _city_progress.clear()
            for _c in _cities_snap:
                _city_progress[_c] = {"found": 0, "total": 0, "status": "⏳ Čeka na red..."}
        _write_status_file()

        _cookie_snap = st.session_state.get("wolt_cookie", "") or WOLT_COOKIE or ""
        Path("_scan_cookie.txt").write_text(_cookie_snap)

        def _run_scan_bg():
            class LivePH:
                def info(self, msg, *a, **k): Path("_scan_status.txt").write_text(str(msg))
                def warning(self, msg, *a, **k): Path("_scan_status.txt").write_text("⚠️ " + str(msg))
                def success(self, msg, *a, **k): Path("_scan_status.txt").write_text("✅ " + str(msg))
                def error(self, msg, *a, **k): Path("_scan_status.txt").write_text("❌ " + str(msg))
                def empty(self, *a, **k): pass

            result = scan_all_cities(_cities_snap, LivePH(), _stop_ev_snap)
            if result is not None and not result.empty:
                result.to_json("_scan_result.json", orient="records", force_ascii=False)
            Path("_scan_done.txt").write_text("1")
            Path("_scan_status.txt").write_text("✅ Sken završen!")

        bg = threading.Thread(target=_run_scan_bg, daemon=True)
        bg.start()
        st.rerun()

    # ── Prikaz statusa sa per-grad progress box-ovima ────────────────────────
    scan_done_flag = Path("_scan_done.txt").exists()

    if st.session_state.scan_running and not scan_done_flag:
        elapsed = time.time() - (st.session_state.scan_start_time or time.time())
        m2, s2 = divmod(int(elapsed), 60)

        try:
            status_msg = Path("_scan_status.txt").read_text()
        except Exception:
            status_msg = "🔄 Skeniranje..."

        st.markdown(f"### 🔄 Skeniranje u toku — {m2:02d}:{s2:02d}")

        # Per-grad progress kartice
        try:
            city_prog = json.loads(Path("_scan_city_progress.json").read_text(encoding="utf-8"))
        except Exception:
            city_prog = {}

        if city_prog:
            cols = st.columns(len(city_prog))
            for i, (city_name, info) in enumerate(city_prog.items()):
                with cols[i]:
                    found  = info.get("found", 0)
                    total  = info.get("total", 0)
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

        time.sleep(2)
        st.rerun()

    # Prikaz rezultata kad scan završi
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
            save_scan(df_result)
            m, s = divmod(int(scan_duration), 60)
            scan_mode_done = st.session_state.get("scan_mode", "full")
            if scan_mode_done == "nopromo":
                newly_found = len(df_result[df_result["akcije"] != "-"])
                st.success(
                    f"✅ No Promo Scan završen za **{m:02d}:{s:02d}**! "
                    f"Od prethodno preskočenih, **{newly_found}** restorana sada ima akcije."
                )
            else:
                st.success(
                    f"✅ Full Scan završen za **{m:02d}:{s:02d}**! "
                    f"Pronađeno **{len(df_result)}** restorana, "
                    f"**{len(df_result[df_result['akcije'] != '-'])}** sa akcijama."
                )
            st.rerun()
        else:
            if _stop_ev.is_set():
                st.warning("⏹️ Scan je zaustavljen pre završetka.")
            else:
                st.error("❌ Scan nije vratio podatke. Proveri cookie u Debug tabu.")

    df = st.session_state.df_wolt
    if not df.empty:
        st.markdown("---")

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        total          = len(df)
        sa_akcijama    = len(df[df["akcije"] != "-"])
        otvoreni       = len(df[df["status"] == "Otvoren"])
        novi           = len(df[df["novo"] == "Da"])
        sa_wolt_plus   = len(df[df["akcije"].str.contains("[Wolt+]", na=False, regex=False)])
        sa_snizenjem   = len(df[df["akcije"].str.contains("[Sniženje", na=False, regex=False)])

        for col, val, lbl in [
            (k1, total,        "Ukupno restorana"),
            (k2, sa_akcijama,  "Ima akciju"),
            (k3, sa_snizenjem, "🏷️ Sniženi artikli"),
            (k4, sa_wolt_plus, "Wolt+ akcije"),
            (k5, otvoreni,     "Trenutno otvoreno"),
            (k6, novi,         "Novi restorani"),
        ]:
            with col:
                st.markdown(f"""
                <div class='kpi'>
                  <div class='kpi-val'>{val}</div>
                  <div class='kpi-lbl'>{lbl}</div>
                </div>""", unsafe_allow_html=True)

        # Per-grad summary
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
                <div style='background:#fff;border-radius:10px;padding:12px 16px;
                            box-shadow:0 2px 8px rgba(0,0,0,0.07);
                            border-top:3px solid #009de0;text-align:center'>
                  <div style='font-weight:800;color:#009de0;font-size:1rem'>{row["grad"]}</div>
                  <div style='font-size:1.6rem;font-weight:900'>{int(row["Restorana"])}</div>
                  <div style='font-size:0.75rem;color:#888'>restorana</div>
                  <div style='margin-top:4px;font-size:0.85rem;color:#27ae60'>
                    {int(row["Sa_akcijama"])} akcija ({pct}%)
                  </div>
                  <div style='font-size:0.75rem;color:#555'>
                    {int(row["Otvoreni"])} otvorenih
                  </div>
                </div>
                """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        fc1, fc2, fc3, fc4 = st.columns(4)
        with fc1:
            grad_filter = st.multiselect("Grad:", CITIES, default=CITIES, key="scan_grad")
        with fc2:
            samo_akcije = st.checkbox("📌 Samo sa akcijama", value=False, key="scan_akcije")
        with fc3:
            samo_novi = st.checkbox("🆕 Samo NOVI", value=False, key="scan_novi")
        with fc4:
            search = st.text_input("🔎 Pretraži naziv:", key="scan_search")

        fc5, fc6, fc7 = st.columns(3)
        with fc5:
            samo_wolt_plus = st.checkbox("💙 Samo sa Wolt+ akcijama", value=False, key="scan_wolt_plus")
        with fc6:
            samo_otvoreni = st.checkbox("🟢 Samo otvoreni", value=False, key="scan_otvoreni")
        with fc7:
            samo_snizeni = st.checkbox("🏷️ Samo sa sniženim artiklima", value=False, key="scan_snizeni")

        sve_akcije_tekst = sorted(set(
            line.lstrip("• ").strip()
            for akcije_cell in df["akcije"]
            if akcije_cell != "-"
            for line in akcije_cell.split("\n")
            if line.strip() and line.strip() != "-"
        ))
        akcija_filter = st.multiselect(
            "🎯 Filtriraj po akciji:",
            options=sve_akcije_tekst,
            default=[],
            placeholder="Sve akcije – ili izaberi specifičnu...",
            key="scan_akcija_filter"
        )

        fdf = df[df["grad"].isin(grad_filter)]
        if samo_akcije:
            fdf = fdf[fdf["akcije"] != "-"]
        if samo_novi:
            fdf = fdf[fdf["novo"] == "Da"]
        if samo_otvoreni:
            fdf = fdf[fdf["status"] == "Otvoren"]
        if search.strip():
            fdf = fdf[fdf["naziv"].str.contains(search.strip(), case=False, na=False)]
        if akcija_filter:
            mask = fdf["akcije"].apply(
                lambda cell: any(a in cell for a in akcija_filter) if cell != "-" else False
            )
            fdf = fdf[mask]
        if samo_wolt_plus:
            fdf = fdf[fdf["akcije"].str.contains("[Wolt+]", na=False, regex=False)]
        if samo_snizeni:
            fdf = fdf[fdf["akcije"].str.contains("[Sniženje", na=False, regex=False)]

        total_fdf  = len(fdf)
        sa_ak      = len(fdf[fdf["akcije"] != "-"])
        sa_wplus   = len(fdf[fdf["akcije"].str.contains("[Wolt+]", na=False, regex=False)])
        sa_sniz    = len(fdf[fdf["akcije"].str.contains("[Sniženje", na=False, regex=False)])
        sa_novi_f  = len(fdf[fdf["novo"] == "Da"])
        sa_otv     = len(fdf[fdf["status"] == "Otvoren"])

        cnt1, cnt2, cnt3, cnt4, cnt5, cnt6 = st.columns(6)
        for col, val, lbl, color in [
            (cnt1, total_fdf, "Prikazano",        "#009de0"),
            (cnt2, sa_ak,     "Sa akcijama",      "#27ae60"),
            (cnt3, sa_sniz,   "🏷️ Sniženi artik.","#e67e22"),
            (cnt4, sa_wplus,  "Wolt+",            "#8e44ad"),
            (cnt5, sa_novi_f, "Novi",             "#e74c3c"),
            (cnt6, sa_otv,    "Otvoreni",         "#2ecc71"),
        ]:
            with col:
                st.markdown(f"""
                <div class='kpi' style='padding:10px 8px;border-top:3px solid {color}'>
                  <div class='kpi-val' style='font-size:1.6rem;color:{color}'>{val}</div>
                  <div class='kpi-lbl'>{lbl}</div>
                </div>""", unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        display_cols = ["grad", "naziv", "status", "ocena", "dostava", "novo", "akcije", "link"]
        display_cols = [c for c in display_cols if c in fdf.columns]

        st.dataframe(
            fdf[display_cols].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
            height=480,
            column_config={
                "grad":    st.column_config.TextColumn("Grad"),
                "naziv":   st.column_config.TextColumn("Restoran"),
                "status":  st.column_config.TextColumn("Status"),
                "ocena":   st.column_config.TextColumn("Ocena"),
                "dostava": st.column_config.TextColumn("Dostava"),
                "novo":    st.column_config.TextColumn("Novi"),
                "akcije":  st.column_config.TextColumn("Akcije", width="large"),
                "link":    st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
            },
        )

        csv = fdf[display_cols].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Preuzmi CSV", csv, "scan.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: AMM BAZA
# ══════════════════════════════════════════════════════════════════════════════
with tab_amm:
    st.markdown("### 👥 Baza Account Managera")
    st.caption("Definiši koji AM je zadužen za koji restoran. Čuva se na GitHub-u (`amm_baza.csv`).")

    amm_df  = load_amm()
    df_wolt = st.session_state.df_wolt

    st.markdown("#### ➕ Dodaj / ažuriraj")

    rest_options = sorted(df_wolt["naziv"].dropna().unique().tolist()) if not df_wolt.empty else []

    a1, a2 = st.columns([2, 1])
    with a1:
        sel_rest = st.selectbox("Restoran (iz poslednjeg scana):",
                                ["-- Odaberi --"] + rest_options, key="amm_sel")
    with a2:
        man_rest = st.text_input("Ili upiši ručno:", placeholder="npr. KFC", key="amm_man")

    final_rest = man_rest.strip() if man_rest.strip() else (
        sel_rest if sel_rest != "-- Odaberi --" else ""
    )

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        amm_city  = st.selectbox("Grad:", ["-- Svi --"] + CITIES, key="amm_city_sel")
    with b2:
        amm_name  = st.text_input("Ime AM-a:", placeholder="Marko M.", key="amm_name")
    with b3:
        amm_email = st.text_input("Email AM-a:", placeholder="marko@firma.com", key="amm_email")
    with b4:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("💾 Sačuvaj", use_container_width=True, key="amm_save"):
            if not final_rest:
                st.error("Izaberi ili upiši naziv restorana.")
            elif not amm_name or not amm_email:
                st.error("Upiši ime i email AM-a.")
            else:
                norm     = normalize(final_rest)
                city_val = "" if amm_city == "-- Svi --" else amm_city
                new_row  = {
                    "restaurant_norm":    norm,
                    "restaurant_display": final_rest,
                    "city":               city_val,
                    "am_name":            amm_name,
                    "am_email":           amm_email,
                }
                mask = (amm_df["restaurant_norm"] == norm) & (amm_df["city"] == city_val)
                if mask.any():
                    amm_df.loc[mask, ["restaurant_display", "am_name", "am_email"]] = [
                        final_rest, amm_name, amm_email
                    ]
                else:
                    amm_df = pd.concat([amm_df, pd.DataFrame([new_row])], ignore_index=True)
                save_amm(amm_df)
                st.success(f"✅ Sačuvano na GitHub-u: **{final_rest}** → {amm_name} ({amm_email})")
                st.rerun()

    st.markdown("---")
    st.markdown("#### 📋 Trenutna baza")
    st.caption("💾 Podaci se čuvaju na GitHub-u i ne brišu se pri gašenju Streamlit-a.")

    if amm_df.empty:
        st.info("Baza je prazna. Dodaj prvi restoran gore.")
    else:
        am_opts = ["Svi"] + sorted(amm_df["am_name"].dropna().unique().tolist())
        am_filt = st.selectbox("Filtriraj po AM-u:", am_opts, key="amm_view_filt")
        view    = amm_df if am_filt == "Svi" else amm_df[amm_df["am_name"] == am_filt]

        edited = st.data_editor(
            view.reset_index(drop=True),
            use_container_width=True,
            num_rows="dynamic",
            hide_index=True,
            column_config={
                "restaurant_norm":    st.column_config.TextColumn("Norm naziv", disabled=True),
                "restaurant_display": st.column_config.TextColumn("Restoran"),
                "city":               st.column_config.TextColumn("Grad"),
                "am_name":            st.column_config.TextColumn("Ime AM-a"),
                "am_email":           st.column_config.TextColumn("Email AM-a"),
            },
            key="amm_editor",
        )
        if st.button("💾 Sačuvaj izmene u tabeli", key="amm_save_tbl"):
            if am_filt == "Svi":
                save_amm(edited)
            else:
                rest_df = amm_df[amm_df["am_name"] != am_filt]
                save_amm(pd.concat([rest_df, edited], ignore_index=True))
            st.success("✅ Baza ažurirana na GitHub-u!")
            st.rerun()

    st.markdown("---")
    st.markdown("#### 📤 Export restorana za dodelu AM-ova")

    if df_wolt.empty:
        st.info("Pokreni scan prvo.")
    else:
        export_df = df_wolt[["grad", "naziv"]].copy()
        export_df["restaurant_display"] = export_df["naziv"]
        export_df["city"]     = export_df["grad"]
        export_df["am_name"]  = ""
        export_df["am_email"] = ""
        export_df = export_df[["restaurant_display", "city", "am_name", "am_email"]].drop_duplicates()

        grad_exp = st.multiselect("Filtriraj po gradu (za export):", CITIES, default=CITIES, key="amm_export_grad")
        export_filtered = export_df[export_df["city"].isin(grad_exp)]
        st.caption(f"Restorana za export: **{len(export_filtered)}**")
        csv_out = export_filtered.to_csv(index=False).encode("utf-8")
        st.download_button("📥 Preuzmi listu restorana (CSV)", csv_out, "restorani_za_amm.csv", "text/csv")

    st.markdown("---")
    st.markdown("#### 📥 Bulk import CSV")
    uploaded = st.file_uploader("CSV fajl:", type="csv", key="amm_upload")
    if uploaded:
        try:
            new_df = pd.read_csv(uploaded)
            new_df["restaurant_norm"] = new_df["restaurant_display"].apply(normalize)
            merged = pd.concat([amm_df, new_df], ignore_index=True).drop_duplicates(
                subset=["restaurant_norm", "city"], keep="last"
            )
            save_amm(merged)
            st.success(f"✅ Importovano {len(new_df)} redova. Ukupno: {len(merged)}.")
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
        st.warning("⚠️ Nema scan podataka. Pokreni scan u prvom tabu.")
    elif amm_df.empty:
        st.warning("⚠️ AMM baza je prazna. Dodaj restorane u drugom tabu.")
    else:
        st.markdown("#### 🔍 Pregled – partneri sa akcijama")

        df_wolt["naziv_norm"]     = df_wolt["naziv"].apply(normalize)
        amm_df["restaurant_norm"] = amm_df["restaurant_norm"].apply(str)

        merged = df_wolt.merge(
            amm_df[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
            left_on="naziv_norm", right_on="restaurant_norm", how="inner"
        )

        def should_alert(row):
            return filter_akcije_for_email(row["akcije"]) != "-"

        merged["_alert"] = merged.apply(should_alert, axis=1)
        sa_akcijama = merged[merged["_alert"]].copy()

        if sa_akcijama.empty:
            st.info("✅ Nijedan partner iz AMM baze trenutno nema relevantne akcije.")
        else:
            sa_akcijama["akcije_email"] = sa_akcijama["akcije"].apply(filter_akcije_for_email)

            af1, af2 = st.columns(2)
            with af1:
                grad_filt_a = st.multiselect("Grad:", CITIES, default=CITIES, key="alert_grad")
            with af2:
                am_filt_a = st.multiselect(
                    "AM:",
                    sorted(sa_akcijama["am_name"].dropna().unique().tolist()),
                    default=sorted(sa_akcijama["am_name"].dropna().unique().tolist()),
                    key="alert_am",
                )

            preview = sa_akcijama[
                (sa_akcijama["grad"].isin(grad_filt_a)) &
                (sa_akcijama["am_name"].isin(am_filt_a))
            ]

            st.caption(
                f"Partnera za alert: **{len(preview)}** | "
                f"AM-ova: **{preview['am_name'].nunique()}** | "
                f"Ukupno akcija: **{len(preview[preview['akcije_email'] != '-'])}**"
            )

            preview_cols = ["grad", "naziv", "am_name", "am_email", "akcije_email", "link"]
            preview_cols = [c for c in preview_cols if c in preview.columns]

            st.dataframe(
                preview[preview_cols].reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
                height=350,
                column_config={
                    "grad":         st.column_config.TextColumn("Grad"),
                    "naziv":        st.column_config.TextColumn("Restoran"),
                    "am_name":      st.column_config.TextColumn("AM"),
                    "am_email":     st.column_config.TextColumn("Email"),
                    "akcije_email": st.column_config.TextColumn("Akcije (u emailu)", width="large"),
                    "link":         st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
                },
            )

            st.markdown("---")
            st.markdown("#### 📤 Pošalji mailove")

            if st.button("🚀 Pošalji alertove", type="primary"):
                am_groups     = preview.groupby(["am_name", "am_email"])
                sent_log      = []
                success_count = 0

                for (am_name, am_email_addr), grp in am_groups:
                    alerts = [
                        {"naziv": row["naziv"], "grad": row["grad"],
                         "akcije": row["akcije"], "link": row.get("link", "")}
                        for _, row in grp.iterrows()
                    ]
                    ok = send_alert_email(am_email_addr, am_name, alerts)
                    if ok:
                        success_count += 1
                        st.success(f"✅ Mail poslat: **{am_name}** ({am_email_addr}) – {len(alerts)} partnera")
                        for a in alerts:
                            sent_log.append({
                                "timestamp": local_now(), "city": a["grad"],
                                "restaurant_display": a["naziv"], "am_name": am_name,
                                "am_email": am_email_addr, "akcije": a["akcije"],
                            })
                    else:
                        st.error(f"❌ Greška pri slanju: {am_name} ({am_email_addr})")

                if sent_log:
                    append_alert_log(sent_log)
                st.markdown(f"**Završeno:** {success_count}/{am_groups.ngroups} mailova poslato.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: STATISTIKA
# ══════════════════════════════════════════════════════════════════════════════
with tab_stats:
    st.markdown("### 📈 Statistika alerta po Account Manageru")

    log_df = load_alert_log()

    if log_df.empty:
        st.info("Još nema poslatih alerta. Statistika će se pojaviti posle prvog slanja.")
    else:
        log_df["timestamp"] = pd.to_datetime(log_df["timestamp"], errors="coerce")
        min_d = log_df["timestamp"].min().date()
        max_d = log_df["timestamp"].max().date()
        s1, s2 = st.columns(2)
        with s1: date_from = st.date_input("Od:", min_d, key="s_from")
        with s2: date_to   = st.date_input("Do:", max_d, key="s_to")

        flog = log_df[
            (log_df["timestamp"].dt.date >= date_from) &
            (log_df["timestamp"].dt.date <= date_to)
        ]

        if flog.empty:
            st.warning("Nema podataka za izabrani period.")
        else:
            k1, k2, k3, k4 = st.columns(4)
            for col, val, lbl, color in [
                (k1, len(flog),                            "Ukupno alerta",    "#009de0"),
                (k2, flog["am_name"].nunique(),            "AM-ova",           "#8e44ad"),
                (k3, flog["restaurant_display"].nunique(), "Restorana",        "#27ae60"),
                (k4, flog["timestamp"].dt.date.nunique(),  "Dana sa alertima", "#e67e22"),
            ]:
                with col:
                    st.markdown(f"""
                    <div class='kpi' style='border-top:4px solid {color}'>
                      <div class='kpi-val' style='color:{color}'>{val}</div>
                      <div class='kpi-lbl'>{lbl}</div>
                    </div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("#### 👤 Pregled po AM-u")

            am_stats = (
                flog.groupby(["am_name", "am_email"])
                .agg(
                    Slanja        =("timestamp", lambda x: x.dt.date.nunique()),
                    Restorana     =("restaurant_display", "nunique"),
                    Ukupno_alerta =("restaurant_display", "count"),
                    Poslednji     =("timestamp", "max"),
                )
                .reset_index()
                .rename(columns={"am_name": "AM", "am_email": "Email"})
                .sort_values("Ukupno_alerta", ascending=False)
            )
            am_stats["Poslednji"] = am_stats["Poslednji"].dt.strftime("%d.%m.%Y %H:%M")
            st.dataframe(am_stats, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.markdown("#### 🗂️ Detaljan log")

            am_log_sel = st.selectbox(
                "Filtriraj po AM-u:",
                ["Svi"] + sorted(flog["am_name"].dropna().unique().tolist()),
                key="log_am_sel"
            )
            log_view = flog if am_log_sel == "Svi" else flog[flog["am_name"] == am_log_sel]
            log_view = log_view.sort_values("timestamp", ascending=False).copy()
            log_view["timestamp"] = log_view["timestamp"].dt.strftime("%d.%m.%Y %H:%M")

            st.dataframe(log_view, use_container_width=True, hide_index=True, height=400)
            csv_exp = log_view.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Eksportuj log", csv_exp, "alert_log.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5: AUTO-SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════
with tab_sched:
    st.markdown("### ⏰ Automatski dnevni sken i slanje")
    st.info(
        "Definiši vreme i gradove – skripta će se svaki dan sama pokrenuti, "
        "skenirati sve gradove i poslati mailove AM-ovima čiji partneri imaju akcije."
    )

    cfg = load_scheduler_config()

    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        sched_enabled = st.toggle("✅ Uključi automatsko slanje", value=cfg.get("enabled", False), key="sched_on")
    with sc2:
        sched_hour = st.number_input("Sat (0–23):", min_value=0, max_value=23,
                                      value=cfg.get("hour", 8), key="sched_hour")
    with sc3:
        sched_min = st.number_input("Minut (0–59):", min_value=0, max_value=59,
                                     value=cfg.get("minute", 0), key="sched_min")

    sched_cities = st.multiselect(
        "Gradovi za automatski sken:",
        options=CITIES,
        default=cfg.get("cities", CITIES),
        key="sched_cities"
    )

    if st.button("💾 Sačuvaj podešavanja schedulera", type="primary"):
        new_cfg = {
            "enabled": sched_enabled,
            "hour":    int(sched_hour),
            "minute":  int(sched_min),
            "cities":  sched_cities,
        }
        save_scheduler_config(new_cfg)
        st.success(
            f"✅ Sačuvano! Automatski sken {'UKLJUČEN' if sched_enabled else 'ISKLJUČEN'} "
            f"– pokreće se svaki dan u **{int(sched_hour):02d}:{int(sched_min):02d}**."
        )

    st.markdown("---")
    st.markdown("#### 🧪 Test – pokreni ručno odmah")

    sched_running = st.session_state.get("sched_running", False)
    sched_done    = st.session_state.get("sched_done", False)

    if st.button("▶️ Pokreni test sken + slanje sada", key="sched_test", disabled=sched_running):
        st.session_state["sched_running"] = True
        st.session_state["sched_done"]    = False

        def _run_sched_bg():
            run_scheduled_scan_and_send()
            st.session_state["sched_running"] = False
            st.session_state["sched_done"]    = True

        threading.Thread(target=_run_sched_bg, daemon=True).start()
        st.rerun()

    if sched_running:
        st.info("🔄 Automatski sken u toku... stranica se osvežava svakih 3s.")
        time.sleep(3)
        st.rerun()

    if sched_done:
        st.session_state["sched_done"] = False
        st.success("✅ Test završen. Proveri statistiku i log.")

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

    # ── GitHub status ──────────────────────────────────────────────────────
    st.markdown("#### 🐙 GitHub Sync Status")
    gh_col1, gh_col2, gh_col3 = st.columns(3)
    with gh_col1:
        r_test = _gh_api("GET", "amm_baza.csv")
        if r_test and r_test.status_code == 200:
            st.success("✅ GitHub konekcija OK")
        else:
            st.error(f"❌ GitHub greška: {r_test.status_code if r_test else 'timeout'}")
    with gh_col2:
        if st.button("🔄 Sync AMM sa GitHub-a", key="sync_amm"):
            amm_fresh = load_amm_github()
            st.success(f"✅ AMM synced: {len(amm_fresh)} redova")
    with gh_col3:
        if st.button("🔄 Sync Alert log sa GitHub-a", key="sync_log"):
            log_fresh = load_alert_log_github()
            st.success(f"✅ Log synced: {len(log_fresh)} redova")

    st.info(f"📁 GitHub repo: `{GITHUB_REPO}` | branch: `{GITHUB_BRANCH}`")

    st.markdown("---")
    st.markdown("#### 🍪 Cookie")
    st.markdown("""
Cookie je potreban da API vraćao akcije restorana.  
**Kako ga nabaviti:** Otvori bilo koji restoran → F12 → Network tab → 
klikni na `dynamic?lat=` request → Request Headers → kopiraj celu vrednost `Cookie:` polja.  
Cookie traje ~24h.
    """)

    saved_cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
    new_cookie = st.text_area(
        "Cookie string:",
        value=saved_cookie,
        height=100,
        placeholder="ravelinDeviceId=...; __woltUid=...; ...",
        key="cookie_input"
    )
    if st.button("💾 Sačuvaj cookie i primeni", key="save_cookie"):
        st.session_state["wolt_cookie"] = new_cookie
        session.headers["Cookie"] = new_cookie
        st.success("✅ Cookie sačuvan i primenjen.")

    if "wolt_cookie" in st.session_state and st.session_state["wolt_cookie"]:
        session.headers["Cookie"] = st.session_state["wolt_cookie"]

    st.markdown("---")
    st.markdown("#### 📍 Lokacije po gradu (višestruko skeniranje)")
    st.info("Svaki grad se skenira sa više geografskih tačaka da bi se pokrili svi restorani.")
    for city_key, coords_list in CITY_MULTI_COORDS.items():
        city_disp = CITY_DISPLAY.get(city_key, city_key)
        with st.expander(f"📍 {city_disp} – {len(coords_list)} lokacija"):
            for i, (lat, lon) in enumerate(coords_list):
                st.markdown(f"  **Lok. {i+1}:** lat={lat}, lon={lon}")

    st.markdown("---")
    st.markdown("#### ⚙️ Podešavanja fetcha")
    st.info(f"Trenutni broj paralelnih radnika: **{FETCH_WORKERS}**.")

    st.markdown("---")
    st.markdown("#### 🚫 Filtrirane akcije iz emaila")
    for p in EMAIL_IGNORE_PROMOS:
        st.markdown(f"- `{p}`")

    st.markdown("---")
    st.markdown("### 🔬 Sirovi API odgovor za restoran")

    dc1, dc2 = st.columns([2, 1])
    with dc1:
        debug_slug = st.text_input("Slug restorana:", placeholder="npr. mcdonalds-nis", key="debug_slug")
    with dc2:
        debug_city_display = st.selectbox("Grad:", CITIES, key="debug_city")

    if st.button("🔍 Dohvati sirovi JSON", key="debug_fetch") and debug_slug:
        debug_city_key = display_to_key(debug_city_display)
        lat, lon  = CITY_COORDS.get(debug_city_key, (44.8178, 20.4569))
        city_slug = CITY_SLUG_MAP.get(debug_city_key, "belgrade")

        st.info(f"Koristim: ključ=`{debug_city_key}`, slug=`{city_slug}`, lat={lat}, lon={lon}")
        st.markdown("---")

        st.markdown("#### 1️⃣ Dynamic endpoint")
        dyn_url = (
            f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{debug_slug}/dynamic/"
            f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery"
        )
        dyn_data, dyn_status = wolt_get(dyn_url)
        if dyn_data:
            with st.expander("Pun JSON (dynamic)", expanded=True):
                st.json(dyn_data)
            st.markdown("**Parsed akcije:**")
            parsed = _parse_dynamic(dyn_data)
            for p in parsed:
                st.write(p)
            if not parsed:
                st.warning("Nema parsiranih akcija.")
        else:
            st.warning(f"Dynamic endpoint nije vratio podatke. HTTP status: {dyn_status}")

    st.markdown("---")
    st.markdown("### 📋 Fetch Debug Log")
    col_log1, col_log2 = st.columns([1, 1])
    with col_log1:
        if st.button("🔄 Osveži log", key="refresh_log"):
            st.rerun()
    with col_log2:
        if st.button("🗑️ Obriši log", key="clear_log"):
            Path("_fetch_debug.log").unlink(missing_ok=True)
            st.success("Log obrisan.")
    try:
        log_content = Path("_fetch_debug.log").read_text(encoding="utf-8")
        if log_content.strip():
            lines = log_content.strip().split("\n")
            st.markdown(f"**{len(lines)} linija u logu**")
            auth_fails = [l for l in lines if "auth fail" in l]
            no_akcija  = [l for l in lines if "NEMA akcija" in l]
            errors     = [l for l in lines if "EXC" in l or "→ 4" in l or "→ 5" in l]
            if auth_fails:
                st.error(f"🔐 **{len(auth_fails)} auth grešaka (401/403)** — cookie možda istekao!")
                with st.expander(f"Auth greške ({len(auth_fails)})"):
                    st.text("\n".join(auth_fails[:50]))
            if no_akcija:
                st.warning(f"🔍 **{len(no_akcija)} restorana sa 200 ali bez akcija**")
                with st.expander(f"Bez akcija ({len(no_akcija)})"):
                    st.text("\n".join(no_akcija[:100]))
            if errors:
                st.warning(f"⚠️ **{len(errors)} ostalih grešaka**")
                with st.expander(f"Ostale greške ({len(errors)})"):
                    st.text("\n".join(errors[:50]))
            if not auth_fails and not no_akcija and not errors:
                st.success("✅ Nema grešaka u logu!")
        else:
            st.info("Log je prazan. Pokreni sken pa osvježi.")
    except FileNotFoundError:
        st.info("Log fajl ne postoji. Pokreni sken pa osvježi.")
