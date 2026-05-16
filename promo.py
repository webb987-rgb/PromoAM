import re
import time
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

FETCH_WORKERS = 60   # povećano sa 10 → 60 (6x brže); ako dobijaš 429, smanji na 40

EMAIL_IGNORE_PROMOS = []  # Nema filtera – prikazujemo SVE akcije

AMM_FILE   = Path("amm_baza.csv")
AMM_COLS   = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]

ALERT_FILE = Path("alert_log.csv")
ALERT_COLS = ["timestamp", "city", "restaurant_display", "am_name", "am_email", "akcije"]

# Fajl u kome čuvamo rezultate poslednjeg skena – permanentna baza
SCAN_FILE  = Path("scan_baza_item.csv")

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
    """Vraća sve akcije bez ikakvog filtera."""
    if not akcije_str or akcije_str == "-":
        return "-"
    lines = [l for l in akcije_str.split("\n") if l.strip()]
    return "\n".join(lines) if lines else "-"

# ─────────────────────────── PERMANENTNA BAZA SKENA ─────────────────────────

def save_scan(df: pd.DataFrame):
    """Čuva rezultate skena u CSV fajl (permanentna baza)."""
    df.to_csv(SCAN_FILE, index=False)

def load_scan() -> pd.DataFrame:
    """Učitava prethodni sken iz CSV fajla."""
    if SCAN_FILE.exists():
        try:
            df = pd.read_csv(SCAN_FILE)
            return df
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()

def scan_meta() -> str:
    """Vraća datum/vreme poslednjeg sačuvanog skena."""
    if SCAN_FILE.exists():
        mtime = SCAN_FILE.stat().st_mtime
        return datetime.datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M:%S")
    return None

# ─────────────────────────── AMM BAZA ────────────────────────────────────────

def load_amm() -> pd.DataFrame:
    if AMM_FILE.exists():
        df = pd.read_csv(AMM_FILE)
        for c in AMM_COLS:
            if c not in df.columns:
                df[c] = ""
        return df
    return pd.DataFrame(columns=AMM_COLS)

def save_amm(df: pd.DataFrame):
    df.to_csv(AMM_FILE, index=False)

# ─────────────────────────── ALERT LOG ───────────────────────────────────────

def load_alert_log() -> pd.DataFrame:
    if ALERT_FILE.exists():
        df = pd.read_csv(ALERT_FILE)
        for c in ALERT_COLS:
            if c not in df.columns:
                df[c] = ""
        return df
    return pd.DataFrame(columns=ALERT_COLS)

def append_alert_log(rows: list):
    df_new = pd.DataFrame(rows)
    if ALERT_FILE.exists():
        pd.concat([pd.read_csv(ALERT_FILE), df_new], ignore_index=True).to_csv(ALERT_FILE, index=False)
    else:
        df_new.to_csv(ALERT_FILE, index=False)

# ─────────────────────────── WOLT API & SESSION ──────────────────────────────

WOLT_COOKIE = ""  # <-- UNESI COOKIE OVDE

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
    cookie_val = st.session_state.get("wolt_cookie", "")
    if cookie_val:
        s.headers["Cookie"] = cookie_val
    elif WOLT_COOKIE:
        s.headers["Cookie"] = WOLT_COOKIE
    return s

# ─────────────────────────── FETCH AKCIJA (PARALELNO) ────────────────────────

def _parse_dynamic(data: dict) -> tuple[list, bool]:
    """
    Parsira dynamic endpoint i vraća (lista_akcija, ima_item_popust).

    Čita ISKLJUČIVO formatted_text iz dve lokacije:
      1. venue.banners[N].discount.formatted_text
      2. venue_raw.discounts[N].banner.formatted_text

    Svaki pronađeni tekst = jedna akcija. Duplikati se preskačaju.
    ima_item_popust = True ako bilo koja akcija sadrži "discount on selected".
    """
    akcije = []
    seen   = set()
    ima_item_popust = False

    def add(text, wolt_plus=False):
        t = (text or "").strip()
        if not t:
            return
        prefix = "• [Wolt+] " if wolt_plus else "• "
        key = t.lower()
        if key not in seen:
            seen.add(key)
            akcije.append(f"{prefix}{t}")
            if "discount on selected" in key or "popust na izabrane" in key:
                nonlocal ima_item_popust
                ima_item_popust = True

    # ── 1. venue.banners[N].discount.formatted_text ───────────────────────────
    venue = data.get("venue") or {}
    for ban in venue.get("banners", []):
        if not isinstance(ban, dict):
            continue
        is_wp = bool(ban.get("show_wolt_plus"))
        disc  = ban.get("discount") or {}
        add((disc.get("formatted_text") or "").strip(), wolt_plus=is_wp)

    # ── 2. venue_raw.discounts[N].banner.formatted_text ──────────────────────
    venue_raw = data.get("venue_raw") or {}
    for disc in venue_raw.get("discounts", []):
        if not isinstance(disc, dict):
            continue
        is_wp = bool(
            disc.get("has_wolt_plus") or
            (disc.get("banner") or {}).get("show_wolt_plus") or
            (disc.get("conditions") or {}).get("has_wolt_plus")
        )
        banner = disc.get("banner") or {}
        add((banner.get("formatted_text") or "").strip(), wolt_plus=is_wp)

    return akcije, ima_item_popust


def _fetch_one(slug: str, lat: float, lon: float, feed_akcije: list,
               stop_event: threading.Event) -> tuple[str, str, str]:
    """
    Fetchuje akcije za jedan restoran ISKLJUČIVO iz dynamic endpointa.
    feed_akcije parametar se ignoriše – koristi se samo dynamic API.
    """
    if stop_event.is_set():
        return slug, "-", "Ne"

    ts = make_thread_session()
    akcije_str   = "-"
    item_popusti = "Ne"

    # ── Dynamic endpoint – jedini izvor akcija ────────────────────────────────
    dyn_url = (
        f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic/"
        f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery"
    )
    for attempt in range(3):
        if stop_event.is_set():
            return slug, "-", "Ne"
        try:
            r = ts.get(dyn_url, timeout=8)
            if r.status_code == 200:
                parsed, ima_item = _parse_dynamic(r.json())
                if ima_item:
                    item_popusti = "Da"
                akcije_str = "\n".join(parsed) if parsed else "-"
                break
            elif r.status_code == 429:
                time.sleep(2 ** attempt)
            else:
                break
        except Exception:
            if attempt < 2:
                time.sleep(0.3)

    return slug, akcije_str, item_popusti


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

    # ── Paginacija – SAMO restaurants endpoint (delivery je duplikat) ─────────
    # Preskačemo delivery endpoint koji vraćao 0 dodatnih – to je bio problem br. 1
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

                    novo_status = "Ne"
                    for badge in venue.get("badges", []):
                        if badge.get("text", "").lower() in ["novo", "new"] or badge.get("label", "").lower() in ["novo", "new"]:
                            novo_status = "Da"

                    restaurants[slug] = {
                        "grad":           city_display,
                        "naziv":          name,
                        "slug":           slug,
                        "status":         status_obj,
                        "ocena":          str(r_score),
                        "dostava":        delivery,
                        "novo":           novo_status,
                        "_feed_akcije":   [],
                        "_feed_has_promo": False,
                        "item_popusti":   "Ne",
                        "akcije":         "-",
                        "link":           f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}",
                        "naziv_norm":     normalize(name),
                    }

        new_this_page = len(restaurants) - count_before
        status_placeholder.info(
            f"🚴 **{city_display}**: str. {page_num+1} – +{new_this_page} novih "
            f"(ukupno {len(restaurants)})"
        )

        if items_in_response == 0:
            break  # nema više stranica

        skip += 40
        time.sleep(0.05)

    if not restaurants or stop_event.is_set():
        if not restaurants:
            status_placeholder.warning(f"⚠️ **{city_display}**: nije pronađen nijedan restoran.")
        return []

    # ── Paralelno fetchovanje akcija ──────────────────────────────────────────
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
                slug, akcije_str, item_pop = future.result()
                restaurants[slug]["akcije"]       = akcije_str
                restaurants[slug]["item_popusti"] = item_pop
            except Exception:
                pass

            completed += 1
            if completed % 10 == 0 or completed == total:
                status_placeholder.info(
                    f"⚡ **{city_display}**: {completed}/{total} restorana obrađeno..."
                )

    for r in restaurants.values():
        r.pop("_feed_akcije", None)

    return list(restaurants.values())


def scan_all_cities(selected_cities: list[str], status_placeholder, stop_event: threading.Event) -> pd.DataFrame:
    all_rows = []
    for i, city in enumerate(selected_cities):
        if stop_event.is_set():
            break
        try:
            rows = fetch_city(city, status_placeholder, stop_event)
            all_rows.extend(rows)
            if not stop_event.is_set():
                status_placeholder.success(f"✅ {city} završen! ({len(rows)} restorana)")
        except Exception as e:
            status_placeholder.error(f"❌ Greška za {city}: {e}")
            import traceback
            st.error(traceback.format_exc())
        if i < len(selected_cities) - 1 and not stop_event.is_set():
            time.sleep(0.5)
    status_placeholder.empty()
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

# ─────────────────────────── EMAIL ───────────────────────────────────────────

def send_alert_email(am_email: str, am_name: str, alerts: list[dict]) -> bool:
    """
    Šalje alert email AM-u.
    - Item popust badge je u koloni Akcije (desno), ne u imenu restorana
    - Naziv restorana je klikabilan link
    - Filtrirane su masovne promo akcije
    """
    try:
        rows_html = ""
        for a in alerts:
            akcije_filtered = filter_akcije_for_email(a["akcije"])

            # Item popust badge ide U kolonu Akcije, ne uz naziv
            item_badge = ""
            if a.get("item_popusti") == "Da":
                item_badge = (
                    "<div style='margin-top:6px'>"
                    "<span style='background:#fff3cd;color:#856404;padding:2px 8px;"
                    "border-radius:12px;font-size:11px;font-weight:700'>🏷️ ITEM POPUSTI</span>"
                    "</div>"
                )

            # Akcije tekst
            if akcije_filtered != "-":
                akcije_html = akcije_filtered.replace("\n", "<br>")
            else:
                akcije_html = "<span style='color:#aaa'>–</span>"

            # Sve akcije + item badge zajedno u jednoj ćeliji
            akcije_cell = f"{akcije_html}{item_badge}"

            link = a.get("link", "")
            if link:
                naziv_cell = f"<a href='{link}' style='color:#222;text-decoration:none;font-weight:600'>{a['naziv']}</a>"
            else:
                naziv_cell = f"<span style='font-weight:600'>{a['naziv']}</span>"

            rows_html += f"""
            <tr>
              <td style='padding:10px 14px;border-bottom:1px solid #eee'>{naziv_cell}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;color:#555'>{a['grad']}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;color:#333'>{akcije_cell}</td>
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
    import json
    if SCHEDULER_FILE.exists():
        try:
            return json.loads(SCHEDULER_FILE.read_text())
        except Exception:
            pass
    return {"enabled": False, "hour": 8, "minute": 0, "cities": CITIES}

def save_scheduler_config(cfg: dict):
    import json
    SCHEDULER_FILE.write_text(json.dumps(cfg))

def run_scheduled_scan_and_send():
    """
    Pokreće sken i šalje mailove – koristi se iz scheduler threada.
    Radi tiho u pozadini bez Streamlit UI elemenata.
    """
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

    # Pošalji mailove
    amm_df = load_amm()
    if amm_df.empty:
        log.warning("[Scheduler] AMM baza prazna, nema kome da se pošalje.")
        return

    df["naziv_norm"] = df["naziv"].apply(normalize)
    merged = df.merge(
        amm_df[["restaurant_norm", "restaurant_display", "am_name", "am_email"]],
        left_on="naziv_norm", right_on="restaurant_norm", how="inner"
    )

    def should_alert(row):
        has_akcije       = str(row.get("akcije", "-")).strip() not in ("", "-")
        has_item_popusti = str(row.get("item_popusti", "Ne")) == "Da"
        return bool(has_akcije or has_item_popusti)

    merged["_alert"] = merged.apply(should_alert, axis=1)
    sa_akcijama = merged[merged["_alert"]].copy()

    sent_log = []
    for (am_name, am_email_addr), grp in sa_akcijama.groupby(["am_name", "am_email"]):
        alerts = [
            {
                "naziv":        row["naziv"],
                "grad":         row["grad"],
                "akcije":       row["akcije"],
                "item_popusti": row.get("item_popusti", "Ne"),
                "link":         row.get("link", ""),
            }
            for _, row in grp.iterrows()
        ]
        ok = send_alert_email(am_email_addr, am_name, alerts)
        if ok:
            log.info(f"[Scheduler] Mail poslat: {am_name} ({am_email_addr})")
            for a in alerts:
                sent_log.append({
                    "timestamp": local_now(),
                    "city": a["grad"],
                    "restaurant_display": a["naziv"],
                    "am_name": am_name,
                    "am_email": am_email_addr,
                    "akcije": a["akcije"],
                })

    if sent_log:
        append_alert_log(sent_log)
    log.info(f"[Scheduler] Završeno. Poslato mailova: {len(sent_log)}")


def _scheduler_loop():
    """Stalni pozadinski thread koji čeka pravo vreme i pali sken."""
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
            log.info(f"[Scheduler] Sledeći sken za {wait_sec/3600:.1f}h")
            time.sleep(wait_sec)
            run_scheduled_scan_and_send()
        else:
            time.sleep(60)  # Proveri ponovo za minutu


# Pokretanje scheduler threada jednom po sesiji
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

# ─────────────────────────── UI ──────────────────────────────────────────────

st.title("🏷️ Promo Monitor – Item Level")
st.caption("Skenira item-level popuste: ulazi u svaki restoran i proverava da li ima makar jedan snižen proizvod. Nema dynamic akcija – samo čisto item skeniranje.")

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

    # Dugme za učitavanje prethodnog skena
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

    # Zaustavljanje
    if stop_scan and st.session_state.scan_running:
        st.session_state.scan_stop_event.set()
        st.warning("⏹️ Zaustavljanje... čeka se da threadovi završe.")

    # Pokretanje skena
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

        _cities_snap = list(selected_cities)
        _stop_ev_snap = st.session_state.scan_stop_event

        # Očisti stare fajlove pre novog skena
        Path("_scan_done.txt").unlink(missing_ok=True)
        Path("_scan_result.csv").unlink(missing_ok=True)
        Path("_scan_status.txt").write_text("🔄 Priprema skena...")

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
            # Čuvamo rezultat na disk – session_state nije dostupan iz threada
            if result is not None and not result.empty:
                result.to_csv("_scan_result.csv", index=False)
            Path("_scan_done.txt").write_text("1")
            Path("_scan_status.txt").write_text("✅ Sken završen!")

        bg = threading.Thread(target=_run_scan_bg, daemon=True)
        bg.start()
        st.rerun()

    # Prikaz statusa dok scan traje
    scan_done_flag = Path("_scan_done.txt").exists()

    if st.session_state.scan_running and not scan_done_flag:
        elapsed = time.time() - (st.session_state.scan_start_time or time.time())
        m2, s2 = divmod(int(elapsed), 60)
        try:
            status_msg = Path("_scan_status.txt").read_text()
        except Exception:
            status_msg = "🔄 Skeniranje..."
        st.info(f"🔄 **{m2:02d}:{s2:02d}** | {status_msg}")
        time.sleep(2)
        st.rerun()

    # Prikaz rezultata kad scan završi
    if st.session_state.scan_running and scan_done_flag:
        Path("_scan_done.txt").unlink(missing_ok=True)
        st.session_state.scan_running = False
        scan_duration = time.time() - (st.session_state.scan_start_time or time.time())
        _stop_ev = st.session_state.scan_stop_event

        try:
            df_result = pd.read_csv("_scan_result.csv")
        except Exception:
            df_result = pd.DataFrame()

        if df_result is not None and not df_result.empty:
            st.session_state.df_wolt = df_result
            st.session_state.last_scan = local_now()
            save_scan(df_result)
            m, s = divmod(int(scan_duration), 60)
            sa_item = len(df_result[df_result["item_popusti"] == "Da"]) if "item_popusti" in df_result.columns else 0
            st.success(
                f"✅ Scan završen za **{m:02d}:{s:02d}**! "
                f"Pronađeno **{len(df_result)}** restorana, "
                f"**{len(df_result[df_result['akcije'] != '-'])}** sa akcijama, "
                f"**{sa_item}** sa item popustima."
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
        sa_item_kpi  = len(df[df["item_popusti"] == "Da"]) if "item_popusti" in df.columns else 0
        bilo_sta     = len(df[(df["akcije"] != "-") | (df.get("item_popusti", pd.Series(dtype=str)) == "Da")])
        otvoreni     = len(df[df["status"] == "Otvoren"])
        novi         = len(df[df["novo"] == "Da"])

        for col, val, lbl in [
            (k1, total,       "Ukupno restorana"),
            (k2, bilo_sta,    "Ima akciju (ukupno)"),
            (k3, sa_akcijama, "Tekstualne akcije"),
            (k4, sa_item_kpi, "Item popusti 🏷️"),
            (k5, otvoreni,    "Trenutno otvoreno"),
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

        fc5, fc6 = st.columns(2)
        with fc5:
            samo_item_pop = st.checkbox("🏷️ Samo sa item popustima", value=False, key="scan_item_pop")
        with fc6:
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
        if samo_item_pop and "item_popusti" in fdf.columns:
            fdf = fdf[fdf["item_popusti"] == "Da"]
        if samo_wolt_plus:
            fdf = fdf[fdf["akcije"].str.contains("[Wolt+]", na=False, regex=False)]

        total_fdf = len(fdf)
        sa_ak     = len(fdf[fdf["akcije"] != "-"])
        sa_item   = len(fdf[fdf["item_popusti"] == "Da"]) if "item_popusti" in fdf.columns else 0
        sa_wplus  = len(fdf[fdf["akcije"].str.contains("[Wolt+]", na=False, regex=False)])
        sa_novi_f = len(fdf[fdf["novo"] == "Da"])
        sa_otv    = len(fdf[fdf["status"] == "Otvoren"])

        cnt1, cnt2, cnt3, cnt4, cnt5, cnt6 = st.columns(6)
        for col, val, lbl, color in [
            (cnt1, total_fdf, "Prikazano",   "#009de0"),
            (cnt2, sa_ak,     "Sa akcijama", "#27ae60"),
            (cnt3, sa_item,   "Item pop.",   "#e67e22"),
            (cnt4, sa_wplus,  "Wolt+",       "#8e44ad"),
            (cnt5, sa_novi_f, "Novi",        "#e74c3c"),
            (cnt6, sa_otv,    "Otvoreni",    "#2ecc71"),
        ]:
            with col:
                st.markdown(f"""
                <div class='kpi' style='padding:10px 8px;border-top:3px solid {color}'>
                  <div class='kpi-val' style='font-size:1.6rem;color:{color}'>{val}</div>
                  <div class='kpi-lbl'>{lbl}</div>
                </div>""", unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        display_cols = ["grad", "naziv", "status", "ocena", "dostava", "novo", "item_popusti", "akcije", "link"]
        display_cols = [c for c in display_cols if c in fdf.columns]

        st.dataframe(
            fdf[display_cols].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
            height=480,
            column_config={
                "grad":         st.column_config.TextColumn("Grad"),
                "naziv":        st.column_config.TextColumn("Restoran"),
                "status":       st.column_config.TextColumn("Status"),
                "ocena":        st.column_config.TextColumn("Ocena"),
                "dostava":      st.column_config.TextColumn("Dostava"),
                "novo":         st.column_config.TextColumn("Novi"),
                "item_popusti": st.column_config.TextColumn("🏷️ Item pop."),
                "akcije":       st.column_config.TextColumn("Akcije", width="large"),
                "link":         st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
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

        df_wolt["naziv_norm"]         = df_wolt["naziv"].apply(normalize)
        amm_df["restaurant_norm"]     = amm_df["restaurant_norm"].apply(str)

        merged = df_wolt.merge(
            amm_df[["restaurant_norm", "restaurant_display", "am_name", "am_email"]],
            left_on="naziv_norm", right_on="restaurant_norm", how="inner"
        )

        def should_alert(row):
            has_akcije       = str(row.get("akcije", "-")).strip() not in ("", "-")
            has_item_popusti = str(row.get("item_popusti", "Ne")) == "Da"
            return bool(has_akcije or has_item_popusti)

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
                f"Sa item popustima: **{len(preview[preview['item_popusti']=='Da'])}**"
            )

            # Preview kolone – bez posebne item_popusti kolone (sve je u akcije_email)
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

            st.info(
                "💡 Kolona **Akcije (u emailu)** prikazuje šta će AM videti. "
                "🏷️ ITEM POPUSTI badge se prikazuje u emailu unutar kolone Akcije."
            )

            st.markdown("---")
            st.markdown("#### 📤 Pošalji mailove")
            st.info("Svaki AM dobija jedan mail sa svim svojim partnerima koji imaju relevantne akcije.")

            if st.button("🚀 Pošalji alertove", type="primary"):
                am_groups    = preview.groupby(["am_name", "am_email"])
                sent_log     = []
                success_count = 0

                for (am_name, am_email_addr), grp in am_groups:
                    alerts = [
                        {
                            "naziv":        row["naziv"],
                            "grad":         row["grad"],
                            "akcije":       row["akcije"],
                            "item_popusti": row.get("item_popusti", "Ne"),
                            "link":         row.get("link", ""),
                        }
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
        st.info("Još nema poslatih alerta. Statistika će se pojaviti posle prvog slanja.")
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
    st.caption("Radi isto kao automatski sken ali se pokreće odmah. Korisno za testiranje.")

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

    # Prikaz sledećeg raspoređenog pokretanja
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
    st.markdown("#### ⚙️ Podešavanja fetcha")
    st.info(f"Trenutni broj paralelnih radnika: **{FETCH_WORKERS}**. "
            "Ako dobijaš 429 greške, smanji `FETCH_WORKERS` u kodu (npr. na 40).")

    st.markdown("---")
    st.markdown("#### 🚫 Filtrirane akcije iz emaila")
    st.caption("Ove akcije se NE šalju AM-u jer ih imaju maltene svi restorani.")
    for p in EMAIL_IGNORE_PROMOS:
        st.markdown(f"- `{p}`")

    st.markdown("---")
    st.markdown("#### 🗺️ Dijagnostika mapiranja gradova")
    diag_data = []
    for key in CITY_KEYS:
        disp  = CITY_DISPLAY.get(key, "?")
        slug  = CITY_SLUG_MAP.get(key, "NIJE NAĐEN ❌")
        coord = CITY_COORDS.get(key, "NIJE NAĐEN ❌")
        diag_data.append({"Ključ": key, "Prikaz": disp, "Slug": slug, "Koordinate": str(coord)})
    st.dataframe(pd.DataFrame(diag_data), use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("### 🔬 Sirovi API odgovor za restoran")
    st.info("Unesi slug restorana i izaberi grad da vidiš šta API tačno vraća.")

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
            st.markdown("**Parsed akcije (full parser):**")
            parsed, ima_item = _parse_dynamic(dyn_data)
            for p in parsed:
                st.write(p)
            if not parsed:
                st.warning("Nema parsiranih akcija.")
            st.markdown(f"**Item discount u dynamic:** `{ima_item}`")
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
