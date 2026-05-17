import re
import io
import base64
import time
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

# ─────────────────────────── KONFIGURACIJA ───────────────────────────────────

EMAIL_SENDER   = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"   # Gmail App Password

CITY_KEYS    = ["Beograd", "Novi Sad", "Nis", "Kragujevac"]
CITY_DISPLAY = {
    "Beograd":    "Beograd",
    "Novi Sad":   "Novi Sad",
    "Nis":        "Niš",
    "Kragujevac": "Kragujevac",
}
CITIES = [CITY_DISPLAY[k] for k in CITY_KEYS]

FETCH_WORKERS = 4          # Smanjen sa 8 → 4: manje simultanih konekcija
RATE_LIMIT    = 1.2        # Max req/s globalno (1 req svakih ~0.83s)
MAX_RETRIES   = 3          # Broj retry pokušaja na 429

EMAIL_IGNORE_PROMOS = [
    "0 din delivery fee for 14 days",
    "0 din delivery fee",
    "free delivery for 14 days",
    "besplatna dostava 14 dana",
    "besplatna dostava",
]

AMM_COLS   = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]
ALERT_COLS = ["timestamp", "city", "restaurant_display", "am_name", "am_email", "akcije"]

# ─────────────────────────── GITHUB INTEGRACIJA ──────────────────────────────

def _gh_headers() -> dict:
    token = st.secrets["github"]["token"]
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

def _gh_repo() -> str:
    return st.secrets["github"]["repo"]

def gh_read(path: str) -> tuple:
    url = f"https://api.github.com/repos/{_gh_repo()}/contents/{path}"
    r = requests.get(url, headers=_gh_headers(), timeout=15)
    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    return None, None

def gh_write(path: str, content_str: str, sha, message: str = "update") -> bool:
    url = f"https://api.github.com/repos/{_gh_repo()}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
    return r.status_code in (200, 201)

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
    /* Debug log stilovi */
    .log-line-ok    { color: #27ae60; font-family: monospace; font-size: 12px; }
    .log-line-warn  { color: #e67e22; font-family: monospace; font-size: 12px; }
    .log-line-err   { color: #e74c3c; font-family: monospace; font-size: 12px; }
    .log-line-info  { color: #3498db; font-family: monospace; font-size: 12px; }
    .log-stat-box { background:#1a1a2e; color:#e0e0e0; border-radius:10px;
                    padding:16px 20px; font-family:monospace; font-size:13px;
                    margin-bottom:12px; white-space:pre-wrap; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────── HELPERS ─────────────────────────────────────────

def normalize(name: str) -> str:
    return re.sub(r"[^\w]", "", str(name).lower())

def beograd_now() -> datetime.datetime:
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Belgrade")
    except Exception:
        import pytz
        tz = pytz.timezone("Europe/Belgrade")
    return datetime.datetime.now(tz)

def local_now() -> str:
    return beograd_now().strftime("%Y-%m-%d %H:%M:%S")

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

# ─────────────────────────── PERMANENTNA BAZA SKENA (GitHub) ────────────────

def save_scan(df: pd.DataFrame):
    try:
        json_str = df.to_json(orient="records", force_ascii=False)
        _, sha = gh_read("scan_baza_item.json")
        gh_write("scan_baza_item.json", json_str, sha, "scan: update results")
    except Exception as e:
        st.warning(f"⚠️ Nije uspelo čuvanje skena na GitHub: {e}")

def load_scan() -> pd.DataFrame:
    try:
        content, _ = gh_read("scan_baza_item.json")
        if content and content.strip() and content.strip() != "[]":
            return pd.read_json(io.StringIO(content), orient="records")
    except Exception:
        pass
    return pd.DataFrame()

def scan_meta() -> str:
    try:
        url = f"https://api.github.com/repos/{_gh_repo()}/contents/scan_baza_item.json"
        r = requests.get(url, headers=_gh_headers(), timeout=10)
        if r.status_code == 200:
            ts = r.json().get("last_modified") or r.json().get("sha", "")[:7]
            return ts if ts else "postoji"
    except Exception:
        pass
    return None

# ─────────────────────────── AMM BAZA (GitHub) ───────────────────────────────

def load_amm() -> pd.DataFrame:
    try:
        content, _ = gh_read("amm_baza.csv")
        if content and content.strip():
            df = pd.read_csv(io.StringIO(content))
            for c in AMM_COLS:
                if c not in df.columns:
                    df[c] = ""
            return df
    except Exception:
        pass
    return pd.DataFrame(columns=AMM_COLS)

def save_amm(df: pd.DataFrame):
    try:
        csv_str = df.to_csv(index=False)
        _, sha = gh_read("amm_baza.csv")
        ok = gh_write("amm_baza.csv", csv_str, sha, "amm: update baza")
        if not ok:
            st.warning("⚠️ Nije uspelo čuvanje AMM baze na GitHub.")
    except Exception as e:
        st.warning(f"⚠️ GitHub greška pri čuvanju AMM: {e}")

# ─────────────────────────── ALERT LOG (GitHub) ──────────────────────────────

def load_alert_log() -> pd.DataFrame:
    try:
        content, _ = gh_read("alert_log.csv")
        if content and content.strip():
            df = pd.read_csv(io.StringIO(content))
            for c in ALERT_COLS:
                if c not in df.columns:
                    df[c] = ""
            return df
    except Exception:
        pass
    return pd.DataFrame(columns=ALERT_COLS)

def append_alert_log(rows: list):
    try:
        existing = load_alert_log()
        df_new   = pd.DataFrame(rows)
        merged   = pd.concat([existing, df_new], ignore_index=True)
        csv_str  = merged.to_csv(index=False)
        _, sha   = gh_read("alert_log.csv")
        gh_write("alert_log.csv", csv_str, sha, "alert: append log")
    except Exception as e:
        st.warning(f"⚠️ GitHub greška pri čuvanju alert loga: {e}")

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

CITY_COORDS = {
    "Beograd":    (44.8178, 20.4569),
    "Novi Sad":   (45.2671, 19.8335),
    "Nis":        (43.3209, 21.8958),
    "Kragujevac": (44.0128, 20.9114),
}

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

# ─────────────────────────── ENHANCED DEBUG LOG ──────────────────────────────

_fetch_log_lock = threading.Lock()

# Statistike za live prikaz
_stats_lock = threading.Lock()
_stats = {
    "total_requests": 0,
    "success_200":    0,
    "error_429":      0,
    "error_401_403":  0,
    "error_other":    0,
    "retries":        0,
    "exceptions":     0,
    "throttle_waits": 0,
    "total_wait_429": 0.0,
    "scan_start":     0.0,
}

def _reset_stats():
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0 if isinstance(_stats[k], int) else 0.0
        _stats["scan_start"] = time.time()

def _inc_stat(key, val=1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + val

def _get_stats_snapshot() -> dict:
    with _stats_lock:
        return dict(_stats)

def _log_fetch(msg: str, level: str = "INFO"):
    """
    Upisuje u debug log sa timestampom i nivoom.
    level: INFO | OK | WARN | ERROR
    """
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{level:5s}] {msg}"
    try:
        with _fetch_log_lock:
            with open("_fetch_debug.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

# ─────────────────────────── RATE LIMITER & THROTTLE ─────────────────────────

_throttle_until = 0.0
_throttle_lock  = threading.Lock()

_token_lock      = threading.Lock()
_token_last_time = 0.0

def _acquire_token():
    """
    Token bucket rate limiter sa jitter-om.
    Čeka slobodan slot pre slanja requesta.
    Jitter (0–150ms) sprečava da svi threadovi krenu istovremeno.
    """
    global _token_last_time
    interval = 1.0 / RATE_LIMIT
    with _token_lock:
        now  = time.time()
        wait = _token_last_time + interval - now
        if wait > 0:
            time.sleep(wait)
        # Jitter: nasumičan delay da razbije burst
        jitter = random.uniform(0, 0.15)
        time.sleep(jitter)
        _token_last_time = time.time()

def _wait_throttle():
    """Čeka ako je globalni throttle aktivan (npr. posle 429)."""
    now = time.time()
    with _throttle_lock:
        wait = _throttle_until - now
    if wait > 0:
        _log_fetch(f"Throttle čekanje {wait:.1f}s...", "WARN")
        _inc_stat("throttle_waits")
        time.sleep(wait)

def _set_throttle(seconds: float):
    """Postavlja globalni throttle za sve threadove."""
    with _throttle_lock:
        global _throttle_until
        _throttle_until = max(_throttle_until, time.time() + seconds)

# ─────────────────────────── FETCH SA RETRY ──────────────────────────────────

def _fetch_url(ts, url: str, label: str, stop_event) -> tuple:
    """
    Fetches a single URL sa exponential backoff na 429.
    - MAX_RETRIES pokušaja
    - Na 429: čeka Retry-After header ili 2^attempt sekundi
    - Jitter u token bucket-u sprečava burst
    """
    if stop_event.is_set():
        return None, 0

    # Čekaj throttle pre prvog pokušaja
    _wait_throttle()
    _acquire_token()
    _inc_stat("total_requests")

    for attempt in range(MAX_RETRIES):
        if stop_event.is_set():
            return None, 0
        try:
            r = ts.get(url, timeout=10)

            if r.status_code == 200:
                _inc_stat("success_200")
                if attempt > 0:
                    _log_fetch(f"{label} → 200 (posle {attempt} retry)", "OK")
                return r.json(), 200

            if r.status_code in (401, 403):
                _inc_stat("error_401_403")
                _log_fetch(f"{label} → {r.status_code} AUTH FAIL — cookie možda istekao!", "ERROR")
                return None, r.status_code

            if r.status_code == 429:
                _inc_stat("error_429")
                # Pokušaj da pročitamo Retry-After header
                retry_after_hdr = r.headers.get("Retry-After", "")
                if retry_after_hdr:
                    try:
                        base_wait = float(retry_after_hdr)
                    except ValueError:
                        base_wait = 2 ** (attempt + 1)
                else:
                    base_wait = 2 ** (attempt + 1)  # 2s, 4s, 8s

                # Dodaj jitter da svi threadovi ne krenu istovremeno
                wait = min(base_wait + random.uniform(0, 1.0), 30.0)
                _inc_stat("total_wait_429", wait)

                _log_fetch(
                    f"{label} → 429 (pokušaj {attempt+1}/{MAX_RETRIES}) "
                    f"Retry-After={retry_after_hdr or 'N/A'} čekam {wait:.1f}s",
                    "WARN"
                )

                # Blokiraj SVE threadove na ovaj period
                _set_throttle(wait)
                time.sleep(wait)

                # Novi token nakon čekanja
                _wait_throttle()
                _acquire_token()
                _inc_stat("retries")
                continue

            # Drugi HTTP grešci (5xx, itd.)
            _inc_stat("error_other")
            _log_fetch(f"{label} → HTTP {r.status_code}", "WARN")
            return None, r.status_code

        except requests.exceptions.Timeout:
            _inc_stat("exceptions")
            _log_fetch(f"{label} → TIMEOUT (pokušaj {attempt+1}/{MAX_RETRIES})", "WARN")
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5)
                _acquire_token()
                _inc_stat("retries")
                continue
            return None, -1

        except Exception as e:
            _inc_stat("exceptions")
            _log_fetch(f"{label} → EXCEPTION: {type(e).__name__}: {e}", "ERROR")
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.0)
                _acquire_token()
                _inc_stat("retries")
                continue
            return None, -1

    # Iscrpljeni svi pokušaji
    _log_fetch(f"{label} → ISCRPLJENI SVI POKUŠAJI ({MAX_RETRIES}x)", "ERROR")
    return None, 429


def _fetch_one(slug: str, lat: float, lon: float, feed_akcije: list, stop_event: threading.Event) -> tuple[str, str]:
    if stop_event.is_set():
        return slug, "-"

    ts = make_thread_session()

    dyn_url = (
        f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic/"
        f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery"
    )

    akcije_str = "-"
    dyn_data, status = _fetch_url(ts, dyn_url, f"DYN {slug}", stop_event)

    if dyn_data:
        try:
            parsed   = _parse_dynamic_with_item_discount(dyn_data)
            combined = list(dict.fromkeys(feed_akcije + parsed))
            akcije_str = "\n".join(combined) if combined else "-"
            if akcije_str == "-":
                _log_fetch(f"DYN {slug} → 200 ali NEMA akcija (parsed={len(parsed)}, feed={len(feed_akcije)})", "INFO")
        except Exception as e:
            _log_fetch(f"DYN {slug} → PARSE EXCEPTION: {e}", "ERROR")
    elif feed_akcije:
        akcije_str = "\n".join(feed_akcije)
        _log_fetch(f"DYN {slug} → status={status} fallback na feed_akcije ({len(feed_akcije)})", "WARN")
    else:
        if status not in (401, 403):  # auth greške su već logovane
            _log_fetch(f"DYN {slug} → status={status} nema podataka", "WARN")

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


def _safe_price(val) -> float:
    if isinstance(val, (int, float)):
        return float(val) / 100
    return 0.0

# ─────────────────────────── FETCH GRAD ──────────────────────────────────────

def fetch_city(city_display: str, status_placeholder, stop_event: threading.Event) -> list[dict]:
    city_key  = display_to_key(city_display)
    city_slug = CITY_SLUG_MAP.get(city_key)
    coords    = CITY_COORDS.get(city_key)

    if not city_slug or not coords:
        status_placeholder.error(f"❌ Nepoznat grad: '{city_display}'")
        return []

    lat, lon = coords
    restaurants = {}
    skip = 0

    status_placeholder.info(f"🔍 Učitavam listu restorana za **{city_display}**...")
    _log_fetch(f"=== POČETAK SKENA: {city_display} lat={lat} lon={lon} ===", "INFO")

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
        status_placeholder.info(
            f"🚴 **{city_display}**: str. {page_num+1} – +{new_this_page} novih "
            f"(ukupno {len(restaurants)})"
        )

        if items_in_response == 0:
            break

        skip += 40
        time.sleep(0.3)  # Povećano sa 0.1 → 0.3 za stabilniju paginaciju

    if not restaurants or stop_event.is_set():
        if not restaurants:
            status_placeholder.warning(f"⚠️ **{city_display}**: nije pronađen nijedan restoran.")
        return []

    _log_fetch(f"{city_display}: pronađeno {len(restaurants)} restorana, počinjem fetch akcija...", "INFO")

    slugs = list(restaurants.keys())
    total = len(slugs)
    completed = 0

    status_placeholder.info(f"⚡ Učitavam akcije za **{city_display}** ({total} restorana)...")

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {
            executor.submit(
                _fetch_one,
                slug,
                lat,
                lon,
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
            except Exception as e:
                _log_fetch(f"Future exception: {e}", "ERROR")

            completed += 1
            if completed % 10 == 0 or completed == total:
                snap = _get_stats_snapshot()
                status_placeholder.info(
                    f"⚡ **{city_display}**: {completed}/{total} | "
                    f"✅{snap['success_200']} ⚠️429:{snap['error_429']} "
                    f"🔐401/403:{snap['error_401_403']} 🔁retry:{snap['retries']}"
                )

    for r in restaurants.values():
        r.pop("_feed_akcije", None)

    snap = _get_stats_snapshot()
    elapsed = time.time() - snap["scan_start"] if snap["scan_start"] else 0
    _log_fetch(
        f"=== {city_display} ZAVRŠEN: {len(restaurants)} restorana, "
        f"req={snap['total_requests']} ok={snap['success_200']} "
        f"429={snap['error_429']} auth={snap['error_401_403']} "
        f"retry={snap['retries']} exc={snap['exceptions']} "
        f"wait429={snap['total_wait_429']:.1f}s elapsed={elapsed:.0f}s ===",
        "INFO"
    )

    return list(restaurants.values())


def _write_city_status(all_cities: list, city_states: dict):
    import json
    Path("_scan_city_status.json").write_text(
        json.dumps({"cities": all_cities, "states": city_states}, ensure_ascii=False)
    )

def scan_all_cities(selected_cities: list[str], status_placeholder, stop_event: threading.Event) -> pd.DataFrame:
    import json
    _reset_stats()
    all_rows    = []
    city_states = {c: {"status": "⏳ čeka...", "done": 0, "total": 0} for c in selected_cities}
    _write_city_status(selected_cities, city_states)

    for i, city in enumerate(selected_cities):
        if stop_event.is_set():
            break

        city_states[city]["status"] = "🔍 učitavam listu..."
        _write_city_status(selected_cities, city_states)

        class CityPH:
            def __init__(self, city_name):
                self.city = city_name
            def _update(self, msg):
                import re
                m = re.search(r"(\d+)/(\d+)", str(msg))
                if m:
                    city_states[self.city]["done"]  = int(m.group(1))
                    city_states[self.city]["total"] = int(m.group(2))
                city_states[self.city]["status"] = str(msg)
                _write_city_status(selected_cities, city_states)
                status_placeholder.info(str(msg))
            def info(self, msg, *a, **k):    self._update(msg)
            def warning(self, msg, *a, **k): self._update(f"⚠️ {msg}")
            def success(self, msg, *a, **k): self._update(msg)
            def error(self, msg, *a, **k):   self._update(f"❌ {msg}")
            def empty(self, *a, **k):        pass

        try:
            rows = fetch_city(city, CityPH(city), stop_event)
            all_rows.extend(rows)
            if not stop_event.is_set():
                city_states[city]["status"] = f"✅ završen ({len(rows)} restorana)"
                city_states[city]["done"]   = len(rows)
                city_states[city]["total"]  = len(rows)
                _write_city_status(selected_cities, city_states)
        except Exception as e:
            city_states[city]["status"] = f"❌ greška: {e}"
            _write_city_status(selected_cities, city_states)
            import traceback
            st.error(traceback.format_exc())

        if i < len(selected_cities) - 1 and not stop_event.is_set():
            time.sleep(0.5)

    status_placeholder.empty()
    Path("_scan_city_status.json").unlink(missing_ok=True)
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

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

        today_str = beograd_now().strftime("%d.%m.%Y")

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

def load_scheduler_config() -> dict:
    import json
    try:
        content, _ = gh_read("scheduler_config.json")
        if content:
            return json.loads(content)
    except Exception:
        pass
    return {"enabled": False, "hour": 8, "minute": 0, "cities": CITIES}

def save_scheduler_config(cfg: dict):
    import json
    try:
        cfg_str = json.dumps(cfg)
        _, sha  = gh_read("scheduler_config.json")
        gh_write("scheduler_config.json", cfg_str, sha, "scheduler: update config")
    except Exception as e:
        st.warning(f"⚠️ GitHub greška pri čuvanju scheduler config: {e}")

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
        log.warning("[Scheduler] AMM baza prazna, nema kome da se pošalje.")
        return

    df["naziv_norm"] = df["naziv"].apply(normalize)
    merged = df.merge(
        amm_df[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
        left_on="naziv_norm", right_on="restaurant_norm", how="inner"
    )

    def should_alert(row):
        filtered = filter_akcije_for_email(row["akcije"])
        return filtered != "-"

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
            log.info(f"[Scheduler] Mail poslat: {am_name} ({am_email_addr})")
            for a in alerts:
                sent_log.append({
                    "timestamp": local_now(), "city": a["grad"],
                    "restaurant_display": a["naziv"], "am_name": am_name,
                    "am_email": am_email_addr, "akcije": a["akcije"],
                })

    if sent_log:
        append_alert_log(sent_log)
    log.info(f"[Scheduler] Završeno. Poslato mailova: {len(sent_log)}")


def _should_trigger_scheduler() -> bool:
    try:
        cfg = load_scheduler_config()
        if not cfg.get("enabled"):
            return False

        now    = beograd_now().replace(tzinfo=None)
        h, m   = int(cfg["hour"]), int(cfg["minute"])
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff   = abs((now - target).total_seconds())

        if diff > 90:
            return False

        lock_file = Path("_scheduler_lock.txt")
        if lock_file.exists():
            try:
                lock_ts = float(lock_file.read_text().strip())
                if time.time() - lock_ts < 300:
                    return False
            except Exception:
                pass
        lock_file.write_text(str(time.time()))
        return True
    except Exception as e:
        try:
            Path("_scheduler_debug.txt").write_text(f"trigger error: {e}")
        except Exception:
            pass
        return False

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

if not st.session_state.get("scan_running") and _should_trigger_scheduler():
    cfg_sched = load_scheduler_config()
    st.session_state["_sched_trigger"] = cfg_sched.get("cities", CITIES)
    st.session_state["_sched_rerun"] = True

if "auto_loaded" not in st.session_state:
    st.session_state["auto_loaded"] = True
    try:
        prev_df = load_scan()
        if not prev_df.empty:
            st.session_state.df_wolt = prev_df
            st.session_state.last_scan = "GitHub (auto-učitan)"
    except Exception:
        pass

# ─────────────────────────── UI ──────────────────────────────────────────────

st.markdown(
    '<iframe src="javascript:setInterval(function(){fetch(window.location.href)},600000)" '
    'style="display:none"></iframe>',
    unsafe_allow_html=True,
)

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

    col_btn, col_stop, col_info = st.columns([1, 1, 3])
    with col_btn:
        run_scan = st.button(
            "▶️ Pokreni scan", type="primary",
            use_container_width=True,
            disabled=not selected_cities or st.session_state.scan_running,
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

    sched_trigger_cities = st.session_state.pop("_sched_trigger", None)
    if sched_trigger_cities and not st.session_state.scan_running:
        selected_cities = sched_trigger_cities
        run_scan        = True
        st.toast("⏰ Automatski sken pokrenut po rasporedu!", icon="🚀")

    if run_scan and selected_cities and not st.session_state.scan_running:
        cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
        if cookie:
            session.headers["Cookie"] = cookie
        elif "Cookie" in session.headers:
            del session.headers["Cookie"]

        st.session_state.scan_stop_event = threading.Event()
        st.session_state.scan_running = True
        st.session_state.scan_start_time = time.time()
        st.session_state["_scan_result"] = None
        st.session_state["_scan_done"] = False

        _cities_snap  = list(selected_cities)
        _stop_ev_snap = st.session_state.scan_stop_event
        _scan_t0      = time.time()

        Path("_scan_done.txt").unlink(missing_ok=True)
        Path("_scan_result.csv").unlink(missing_ok=True)
        Path("_scan_status.txt").write_text("🔄 Priprema skena...")
        # Briši stari log na početku novog skena
        Path("_fetch_debug.log").unlink(missing_ok=True)

        _cookie_snap = st.session_state.get("wolt_cookie", "") or WOLT_COOKIE or ""
        Path("_scan_cookie.txt").write_text(_cookie_snap)

        _is_sched_scan = bool(sched_trigger_cities)

        def _run_scan_bg():
            class LivePH:
                def info(self, msg, *a, **k):
                    Path("_scan_status.txt").write_text(str(msg))
                def warning(self, msg, *a, **k):
                    Path("_scan_status.txt").write_text("⚠️ " + str(msg))
                def success(self, msg, *a, **k):
                    Path("_scan_status.txt").write_text("✅ " + str(msg))
                def error(self, msg, *a, **k):
                    Path("_scan_status.txt").write_text("❌ " + str(msg))
                def empty(self, *a, **k):
                    pass
            result = scan_all_cities(_cities_snap, LivePH(), _stop_ev_snap)
            if result is not None and not result.empty:
                result.to_json("_scan_result.json", orient="records", force_ascii=False)
                if _is_sched_scan:
                    Path("_scan_send_mail.txt").write_text("1")
            _dur = int(time.time() - _scan_t0)
            Path("_scan_done.txt").write_text(str(_dur))
            _dm, _ds = divmod(_dur, 60)
            Path("_scan_status.txt").write_text(f"✅ Sken završen za {_dm}min {_ds}s!")

        bg = threading.Thread(target=_run_scan_bg, daemon=True)
        bg.start()
        st.rerun()

    scan_done_flag = Path("_scan_done.txt").exists()

    if st.session_state.scan_running and not scan_done_flag:
        import json
        elapsed = time.time() - (st.session_state.scan_start_time or time.time())
        m2, s2  = divmod(int(elapsed), 60)

        st.markdown(f"**🔄 Skeniranje u toku — {m2:02d}:{s2:02d}**")

        try:
            city_data = json.loads(Path("_scan_city_status.json").read_text())
            cities    = city_data.get("cities", [])
            states    = city_data.get("states", {})
            for c in cities:
                st_info = states.get(c, {})
                done    = st_info.get("done", 0)
                total   = st_info.get("total", 0)
                status  = st_info.get("status", "⏳ čeka...")
                pct = (done / total) if total > 0 else 0.0
                col_name, col_bar, col_cnt = st.columns([2, 5, 2])
                with col_name:
                    st.markdown(f"**{c}**")
                with col_bar:
                    st.progress(min(pct, 1.0))
                with col_cnt:
                    if total > 0:
                        st.markdown(f"`{done}/{total}`")
                    else:
                        st.markdown(f"`{status[:30]}`")
        except Exception:
            try:
                status_msg = Path("_scan_status.txt").read_text()
            except Exception:
                status_msg = "🔄 Skeniranje..."
            st.info(status_msg)

        time.sleep(1)
        st.rerun()

    if st.session_state.scan_running and scan_done_flag:
        try:
            _dur_sec = int(Path("_scan_done.txt").read_text().strip())
            _dm, _ds = divmod(_dur_sec, 60)
            st.toast(f"✅ Skeniranje završeno za {_dm}min {_ds}s!", icon="🏁")
        except Exception:
            pass
        Path("_scan_done.txt").unlink(missing_ok=True)
        st.session_state.scan_running = False
        if Path("_scan_send_mail.txt").exists():
            Path("_scan_send_mail.txt").unlink(missing_ok=True)
            threading.Thread(target=run_scheduled_scan_and_send, daemon=True).start()
            st.toast("📧 Slanje alertova AM-ovima u toku...", icon="📬")
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
            st.success(
                f"✅ Scan završen za **{m:02d}:{s:02d}**! "
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

        k1, k2, k3, k4, k5 = st.columns(5)
        total        = len(df)
        sa_akcijama  = len(df[df["akcije"] != "-"])
        otvoreni     = len(df[df["status"] == "Otvoren"])

        for col, val, lbl in [
            (k1, total,       "Ukupno restorana"),
            (k2, sa_akcijama, "Ima akciju"),
            (k3, len(df[df["akcije"].str.contains("[Wolt+]", na=False, regex=False)]), "Wolt+ akcije"),
            (k4, len(df[df["novo"] == "Da"]), "Novi"),
            (k5, otvoreni,    "Otvoreno"),
        ]:
            with col:
                st.markdown(f"""
                <div class='kpi'>
                  <div class='kpi-val'>{val}</div>
                  <div class='kpi-lbl'>{lbl}</div>
                </div>""", unsafe_allow_html=True)

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

        fc5, = st.columns(1)
        with fc5:
            samo_wolt_plus = st.checkbox("💙 Samo sa Wolt+ akcijama", value=False, key="scan_wolt_plus")

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
        if search.strip():
            fdf = fdf[fdf["naziv"].str.contains(search.strip(), case=False, na=False)]
        if akcija_filter:
            mask = fdf["akcije"].apply(
                lambda cell: any(a in cell for a in akcija_filter) if cell != "-" else False
            )
            fdf = fdf[mask]
        if samo_wolt_plus:
            fdf = fdf[fdf["akcije"].str.contains("[Wolt+]", na=False, regex=False)]

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
    st.caption("Definiši koji AM je zadužen za koji restoran. Čuva se u `amm_baza.csv`.")

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
                st.success(f"✅ Sačuvano: **{final_rest}** → {amm_name} ({amm_email})")
                st.rerun()

    st.markdown("---")
    st.markdown("#### 📋 Trenutna baza")

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
            st.success("✅ Baza ažurirana!")
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

        merged["_alert"] = merged.apply(
            lambda row: filter_akcije_for_email(row["akcije"]) != "-", axis=1
        )
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
                f"AM-ova: **{preview['am_name'].nunique()}**"
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
                                "timestamp":          local_now(),
                                "city":               a["grad"],
                                "restaurant_display": a["naziv"],
                                "am_name":            am_name,
                                "am_email":           am_email_addr,
                                "akcije":             a["akcije"],
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

    log = load_alert_log()

    if log.empty:
        st.info("Još nema poslatih alerta.")
    else:
        log["timestamp"] = pd.to_datetime(log["timestamp"], errors="coerce")
        min_d = log["timestamp"].min().date()
        max_d = log["timestamp"].max().date()
        s1, s2 = st.columns(2)
        with s1: date_from = st.date_input("Od:", min_d, key="s_from")
        with s2: date_to   = st.date_input("Do:", max_d, key="s_to")

        flog = log[
            (log["timestamp"].dt.date >= date_from) &
            (log["timestamp"].dt.date <= date_to)
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
                    Slanja=("timestamp", lambda x: x.dt.date.nunique()),
                    Restorana=("restaurant_display", "nunique"),
                    Ukupno_alerta=("restaurant_display", "count"),
                    Poslednji=("timestamp", "max"),
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
            f"– pokreće se u **{int(sched_hour):02d}:{int(sched_min):02d}**."
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
        st.info("🔄 Automatski sken u toku...")
        time.sleep(3)
        st.rerun()

    if sched_done:
        st.session_state["sched_done"] = False
        st.success("✅ Test završen. Proveri statistiku i log.")

    st.markdown("---")
    cfg_cur = load_scheduler_config()
    if cfg_cur.get("enabled"):
        now    = beograd_now()
        target = now.replace(hour=cfg_cur["hour"], minute=cfg_cur["minute"], second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        diff      = target - now
        total_sec = int(diff.total_seconds())
        h,   rem  = divmod(total_sec, 3600)
        m_r, s_r  = divmod(rem, 60)
        day_sec   = 24 * 3600
        elapsed   = day_sec - total_sec
        progress  = max(0.0, min(1.0, elapsed / day_sec))

        st.markdown(f"""
        <div style='background:#fff;border-radius:12px;padding:20px 24px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.07);text-align:center;margin-bottom:12px'>
          <div style='font-size:.85rem;color:#888;margin-bottom:6px'>⏱️ Sledeći automatski sken</div>
          <div style='font-size:2.6rem;font-weight:800;color:#009de0;letter-spacing:2px'>
            {h:02d}:{m_r:02d}:{s_r:02d}
          </div>
          <div style='font-size:.85rem;color:#555;margin-top:6px'>
            pokreće se u <b>{cfg_cur["hour"]:02d}:{cfg_cur["minute"]:02d}</b>
            &nbsp;|&nbsp; sada: <b>{now.strftime("%H:%M:%S")}</b>
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.progress(progress)
        st.caption(f"Gradovi: {', '.join(cfg_cur.get('cities', []))}")
        time.sleep(1)
        st.rerun()
    else:
        st.warning("⚠️ Automatski sken je isključen.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 6: DEBUG API  ← PROŠIRENI DEBUG LOG
# ══════════════════════════════════════════════════════════════════════════════
with tab_debug:
    st.markdown("### 🔧 Debug & Podešavanja")

    # ── Cookie ────────────────────────────────────────────────────────────────
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

    # ── Rate limit info ───────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### ⚙️ Podešavanja fetcha")
    st.info(
        f"Paralelni threadovi: **{FETCH_WORKERS}** | "
        f"Rate limit: **{RATE_LIMIT} req/s** (jedan request svakih {1/RATE_LIMIT:.2f}s) | "
        f"Max retry: **{MAX_RETRIES}x** sa exponential backoff\n\n"
        "📌 Ako dobijaš 429 → smanji `RATE_LIMIT` na `0.8` i `FETCH_WORKERS` na `3`.\n\n"
        "📌 Ako nema 429 → povećaj `RATE_LIMIT` na `1.5` za brže skeniranje."
    )

    # ══════════════════════════════════════════════════════════════════════════
    # ENHANCED DEBUG LOG
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown("### 📊 Debug Log – Live Statistike")

    # Kontrole
    dcol1, dcol2, dcol3, dcol4 = st.columns(4)
    with dcol1:
        if st.button("🔄 Osveži log", key="refresh_log"):
            st.rerun()
    with dcol2:
        if st.button("🗑️ Obriši log", key="clear_log"):
            Path("_fetch_debug.log").unlink(missing_ok=True)
            st.success("Log obrisan.")
            st.rerun()
    with dcol3:
        auto_refresh = st.checkbox("⏱️ Auto-refresh (3s)", value=False, key="log_auto_refresh")
    with dcol4:
        show_all = st.checkbox("📋 Prikaži sve linije", value=False, key="log_show_all")

    # Statistike iz _stats (in-memory, samo tokom aktivnog skena)
    snap = _get_stats_snapshot()
    if snap["total_requests"] > 0:
        elapsed_s = time.time() - snap["scan_start"] if snap["scan_start"] else 1
        req_per_s = snap["total_requests"] / elapsed_s if elapsed_s > 0 else 0
        success_pct = (snap["success_200"] / snap["total_requests"] * 100) if snap["total_requests"] > 0 else 0
        error_pct   = ((snap["error_429"] + snap["error_401_403"] + snap["error_other"]) / snap["total_requests"] * 100)

        st.markdown("#### 📈 Live statistike (trenutni/poslednji sken)")
        lc1, lc2, lc3, lc4, lc5, lc6 = st.columns(6)
        for col, val, lbl, color in [
            (lc1, snap["total_requests"],  "Ukupno req",    "#009de0"),
            (lc2, snap["success_200"],     "✅ Uspešnih",   "#27ae60"),
            (lc3, snap["error_429"],       "⚠️ 429 Too Many","#e67e22"),
            (lc4, snap["error_401_403"],   "🔐 401/403",    "#e74c3c"),
            (lc5, snap["retries"],         "🔁 Retry",      "#8e44ad"),
            (lc6, snap["exceptions"],      "💥 Exceptions", "#c0392b"),
        ]:
            with col:
                st.markdown(f"""
                <div class='kpi' style='padding:10px 8px;border-top:3px solid {color}'>
                  <div class='kpi-val' style='font-size:1.5rem;color:{color}'>{val}</div>
                  <div class='kpi-lbl'>{lbl}</div>
                </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # Progress bar uspešnosti
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            st.metric("Req/s", f"{req_per_s:.2f}", help="Prosečan broj requestova u sekundi")
        with sc2:
            st.metric("Uspešnost", f"{success_pct:.1f}%")
        with sc3:
            st.metric("Ukupno čekanje na 429", f"{snap['total_wait_429']:.1f}s")

        if snap["error_429"] > 0:
            st.error(
                f"🚨 **{snap['error_429']} puta dobijen 429!** "
                f"Ukupno {snap['total_wait_429']:.1f}s čekanja zbog rate limit-a. "
                f"Razmisli o smanjenju `RATE_LIMIT` na `0.8`."
            )
        elif snap["error_401_403"] > 0:
            st.error("🔐 **Auth greška (401/403)** – cookie je istekao! Unesi novi cookie gore.")
        else:
            st.success("✅ Nema 429 i auth grešaka u ovom skenu!")

    st.markdown("---")
    st.markdown("#### 📋 Fetch Log Fajl")

    try:
        log_content = Path("_fetch_debug.log").read_text(encoding="utf-8")
        if not log_content.strip():
            st.info("Log je prazan. Pokreni sken pa osvježi.")
        else:
            lines = log_content.strip().split("\n")

            # Grupisanje po tipu
            auth_fails  = [l for l in lines if "AUTH FAIL" in l or "401" in l or "403" in l]
            errors_429  = [l for l in lines if "429" in l]
            retries     = [l for l in lines if "retry" in l.lower() or "pokušaj" in l.lower()]
            exceptions_ = [l for l in lines if "EXCEPTION" in l or "TIMEOUT" in l or "ISCRPLJENI" in l]
            no_akcija   = [l for l in lines if "NEMA akcija" in l]
            ok_retry    = [l for l in lines if "200 (posle" in l]
            info_lines  = [l for l in lines if "[INFO ]" in l and "===" in l]  # samo sekcioni info

            # Summary box
            st.markdown(f"""
            <div class='log-stat-box'>
UKUPNO LINIJA U LOGU:  {len(lines)}
─────────────────────────────────────────
🔐 Auth greške (401/403):  {len(auth_fails)}
⚠️  Rate limit (429):      {len(errors_429)}
🔁 Retry pokušaji:         {len(retries)}
💥 Exceptions/Timeout:     {len(exceptions_)}
🔍 Bez akcija (200 ali -): {len(no_akcija)}
✅ Uspešno posle retry:    {len(ok_retry)}
            </div>
            """, unsafe_allow_html=True)

            # Sekcija za svaki tip greške
            if auth_fails:
                st.error(f"🔐 **AUTH GREŠKE ({len(auth_fails)})** — Cookie je verovatno istekao!")
                with st.expander(f"Prikaži auth greške ({len(auth_fails)} linija)", expanded=True):
                    st.code("\n".join(auth_fails), language=None)

            if errors_429:
                pct_429 = len(errors_429) / max(len(lines), 1) * 100
                st.warning(f"⚠️ **429 Too Many Requests ({len(errors_429)} puta, {pct_429:.1f}% svih logova)**")
                with st.expander(f"Prikaži 429 greške ({len(errors_429)} linija)"):
                    st.code("\n".join(errors_429[-50:]), language=None)  # poslednih 50
                    if len(errors_429) > 50:
                        st.caption(f"(prikazano poslednjih 50 od {len(errors_429)})")

            if ok_retry:
                st.success(f"✅ **{len(ok_retry)} requestova uspešno posle retry** (429 nije fatalan)")
                with st.expander(f"Uspešni retry ({len(ok_retry)} linija)"):
                    st.code("\n".join(ok_retry), language=None)

            if exceptions_:
                st.error(f"💥 **Exceptions/Timeout ({len(exceptions_)})**")
                with st.expander(f"Exceptions ({len(exceptions_)} linija)"):
                    st.code("\n".join(exceptions_), language=None)

            if no_akcija:
                with st.expander(f"🔍 Restorani bez akcija ({len(no_akcija)}) — normalno je"):
                    st.code("\n".join(no_akcija[:100]), language=None)
                    if len(no_akcija) > 100:
                        st.caption(f"(prikazano prvih 100 od {len(no_akcija)})")

            # Skan-sekcioni info (=== POČETAK / ZAVRŠEN ===)
            if info_lines:
                with st.expander("📌 Sken sekcije (start/end po gradu)"):
                    st.code("\n".join(info_lines), language=None)

            # Svi redovi (opciono)
            if show_all:
                st.markdown("#### 📄 Kompletni log")
                # Prikaži poslednjih 500 linija
                display_lines = lines[-500:] if len(lines) > 500 else lines
                st.code("\n".join(display_lines), language=None)
                if len(lines) > 500:
                    st.caption(f"(prikazano poslednjih 500 od {len(lines)} linija)")

            # Download dugme
            st.download_button(
                "📥 Preuzmi kompletni log",
                log_content.encode("utf-8"),
                "fetch_debug.log",
                "text/plain",
                key="dl_log"
            )

    except FileNotFoundError:
        st.info("Log fajl ne postoji. Pokreni sken pa osvježi.")

    # Auto-refresh
    if auto_refresh:
        time.sleep(3)
        st.rerun()

    # ── Filtrirane akcije iz emaila ───────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🚫 Filtrirane akcije iz emaila")
    st.caption("Ove akcije se NE šalju AM-u jer ih imaju maltene svi restorani.")
    for p in EMAIL_IGNORE_PROMOS:
        st.markdown(f"- `{p}`")

    # ── Dijagnostika gradova ──────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🗺️ Dijagnostika mapiranja gradova")
    diag_data = []
    for key in CITY_KEYS:
        disp  = CITY_DISPLAY.get(key, "?")
        slug  = CITY_SLUG_MAP.get(key, "NIJE NAĐEN ❌")
        coord = CITY_COORDS.get(key, "NIJE NAĐEN ❌")
        diag_data.append({"Ključ": key, "Prikaz": disp, "Slug": slug, "Koordinate": str(coord)})
    st.dataframe(pd.DataFrame(diag_data), use_container_width=True, hide_index=True)

    # ── Sirovi API debug ──────────────────────────────────────────────────────
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

        st.markdown("#### 1️⃣ Feed – badges & label")
        feed_url = f"https://restaurant-api.wolt.com/v3/venues/slug/{debug_slug}"
        feed_data, feed_status = wolt_get(feed_url)
        if feed_data:
            results    = feed_data.get("results", [{}])
            venue_info = results[0] if results else {}
            st.write(f"**badges:** {venue_info.get('badges', [])}")
            st.write(f"**label:** `{venue_info.get('label', '')}`")
            with st.expander("Pun JSON (v3/venues/slug)"):
                st.json(feed_data)
        else:
            st.warning(f"v3 endpoint nije vratio podatke. HTTP status: {feed_status}")

        st.markdown("---")
        st.markdown("#### 2️⃣ Dynamic endpoint")
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
        st.markdown("#### 3️⃣ Promotions endpoint")
        promo_url = (
            f"https://consumer-api.wolt.com/consumer-promotions/api/v1/venues/{debug_slug}/promotions"
            f"?lat={lat}&lon={lon}"
        )
        promo_data, promo_status = wolt_get(promo_url)
        if promo_data:
            with st.expander("Pun JSON (promotions)"):
                st.json(promo_data)
        else:
            st.warning(f"Promotions endpoint nije vratio podatke. HTTP status: {promo_status}")

        st.markdown("---")
        st.markdown("#### 4️⃣ Loyalty/Deals endpoint")
        loyalty_url = (
            f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{debug_slug}/loyalty/"
            f"?lat={lat}&lon={lon}"
        )
        loyalty_data, loyalty_status = wolt_get(loyalty_url)
        if loyalty_data:
            with st.expander("Pun JSON (loyalty)"):
                st.json(loyalty_data)
        else:
            st.warning(f"Loyalty endpoint nije vratio podatke. HTTP status: {loyalty_status}")
