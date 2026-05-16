import re
import random
import json
import time
import datetime
import smtplib
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

CITIES = ["Beograd", "Novi Sad", "Niš", "Kragujevac"]

AMM_FILE   = Path("amm_baza.csv")
AMM_COLS = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]

ALERT_FILE = Path("alert_log.csv")
ALERT_COLS = ["timestamp", "city", "restaurant_display", "am_name", "am_email", "akcije"]

# ─────────────────────────── PAGE CONFIG ─────────────────────────────────────

st.set_page_config(page_title="Promo Monitor", page_icon="🚴", layout="wide")

st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background: #f7f8fc; }
    .kpi { background:#fff; border-radius:12px; padding:18px 24px;
           box-shadow:0 2px 8px rgba(0,0,0,0.07); text-align:center; }
    .kpi-val { font-size:2.2rem; font-weight:800; color:#009de0; }
    .kpi-lbl { font-size:.85rem; color:#888; margin-top:4px; }
    div[data-testid="stDataFrame"] thead th { background:#009de0!important; color:#fff!important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────── HELPERS ─────────────────────────────────────────

def normalize(name: str) -> str:
    return re.sub(r"[^\w]", "", str(name).lower())

def local_now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

# ⚠️  COOKIE – osvežavaj kad akcije prestanu da se čitaju (traje ~24h)
# Kako: wolt.com → F12 → Network → klikni dynamic?lat= → Request Headers → kopiraj Cookie
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

# Perzistentna sesija da izbegnemo blokade
session = requests.Session()
session.headers.update(BROWSER_HEADERS)
if WOLT_COOKIE:
    session.headers["Cookie"] = WOLT_COOKIE

CITY_COORDS = {
    "Beograd":    (44.8178, 20.4569),
    "Novi Sad":   (45.2671, 19.8335),
    "Niš":        (43.3209, 21.8958),
    "Kragujevac": (44.0128, 20.9114),
}

CITY_SLUG_MAP = {
    "Beograd":    "belgrade",
    "Novi Sad":   "novi-sad",
    "Niš":        "nis",
    "Kragujevac": "kragujevac",
}

def wolt_get(url: str, extra_headers: dict = None) -> tuple:
    """Vraca (json_data, status_code). json_data je None ako nije 200."""
    try:
        hdrs = {}
        if extra_headers:
            hdrs.update(extra_headers)
        r = session.get(url, timeout=15, headers=hdrs)
        if r.status_code == 200:
            return r.json(), 200
        return None, r.status_code
    except Exception:
        return None, -1

def fetch_dynamic_discounts(slug: str, lat: float, lon: float) -> list:
    """
    Cita akcije iz dynamic endpointa.
    Struktura: venue_raw.discounts[i].banner.formatted_text
    """
    url = (
        f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic/"
        f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery"
    )
    # Kopiraj headere iz globalnog sessiona (ukljucujuci cookie)
    thread_session = requests.Session()
    thread_session.headers.update(dict(session.headers))

    data = None
    for attempt in range(2):  # max 2 pokusaja, retry se radi na visem nivou
        try:
            r = thread_session.get(url, timeout=15)
            if r.status_code != 200:
                return []
            data = r.json()
            venue_raw = data.get("venue_raw") or {}
            venue = data.get("venue") or {}
            has_data = (
                bool(venue_raw.get("discounts")) or
                bool(venue.get("banners")) or
                bool((venue.get("offer_assistant") or {}).get("offer_trackers"))
            )
            if has_data:
                break
            if attempt == 0:
                time.sleep(1.0)
        except Exception:
            if attempt == 0:
                time.sleep(0.5)
    if not data:
        return []

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

    # venue_raw.discounts[] - glavne kampanje
    venue_raw = data.get("venue_raw") or {}
    for disc in venue_raw.get("discounts", []):
        if not isinstance(disc, dict):
            continue
        is_wolt_plus = disc.get("has_wolt_plus") or (disc.get("banner") or {}).get("show_wolt_plus", False)
        banner = disc.get("banner") or {}
        add(banner.get("formatted_text"), wolt_plus=is_wolt_plus)
        desc = disc.get("description") or {}
        add(desc.get("title"), wolt_plus=is_wolt_plus)

    # venue.banners[] - vizuelni baneri
    venue = data.get("venue") or {}
    for banner in venue.get("banners", []):
        if not isinstance(banner, dict):
            continue
        is_wolt_plus = banner.get("show_wolt_plus", False)
        disc = banner.get("discount") or {}
        add(disc.get("formatted_text"), wolt_plus=is_wolt_plus)

    # venue.offer_assistant.offer_trackers[]
    offer_assistant = venue.get("offer_assistant") or {}
    for tracker in offer_assistant.get("offer_trackers", []):
        if not isinstance(tracker, dict):
            continue
        is_wolt_plus = tracker.get("offer_type") == "wolt_plus" or tracker.get("show_wolt_plus", False)
        add(tracker.get("title"), wolt_plus=is_wolt_plus)

    return list(akcije)

def fetch_menu_discounts(slug: str) -> dict:
    """
    Proverava meni restorana i vraca:
    {
      "has_discounts": True/False,
      "items": [{"name": "...", "price": 803, "original_price": 879}, ...]
    }
    Koristi assortment endpoint (isti kao Menu Scraper skripta).
    """
    url = f"https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment"
    thread_session = requests.Session()
    thread_session.headers.update(dict(session.headers))

    try:
        r = thread_session.get(url, timeout=15)
        if r.status_code != 200:
            return {"has_discounts": False, "items": []}
        data = r.json()
    except Exception:
        return {"has_discounts": False, "items": []}

    discounted = []
    for item in data.get("items", []):
        # Cene su u "coins" (centima) - delimo sa 100
        price = (item.get("base_price") or item.get("price") or 0) / 100
        # Originalna (precrtan) cena - moguca polja
        orig = (
            item.get("original_price") or
            item.get("strikethrough_price") or
            item.get("compare_at_price") or
            item.get("unit_price") or
            0
        ) / 100

        if orig > 0 and orig > price:
            discounted.append({
                "name": item.get("name", ""),
                "price": int(price),
                "original_price": int(orig),
                "discount_pct": round((1 - price / orig) * 100),
            })

    return {
        "has_discounts": len(discounted) > 0,
        "items": discounted,
    }

def fetch_city(city: str, status_placeholder) -> list[dict]:
    city_slug = CITY_SLUG_MAP.get(city, normalize(city).replace(" ", "-"))
    lat, lon  = CITY_COORDS.get(city, (44.8178, 20.4569))

    restaurants = {}
    skip = 0

    status_placeholder.info(f"🔍 Učitavam listu restorana za **{city}**...")

    for page_num in range(30):
        count_before = len(restaurants)
        page_has_data = False

        for endpoint in [
            f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}",
            f"https://restaurant-api.wolt.com/v1/pages/delivery?lat={lat}&lon={lon}&skip={skip}",
        ]:
            data, _status = wolt_get(endpoint)
            if not data:
                continue

            items_in_response = 0
            for section in data.get("sections", []):
                for item in section.get("items", []):
                    venue = item.get("venue")
                    if not venue: continue
                    
                    name = venue.get("name", "")
                    slug = venue.get("slug", "")
                    if not name or not slug: continue

                    items_in_response += 1  # broji sve stavke u odgovoru, i duplikate

                    if slug in restaurants: continue

                    status = "Otvoren" if venue.get("online") else "Zatvoren"
                    rating = venue.get("rating") or {}
                    rating_score = rating.get("score", "-") if isinstance(rating, dict) else "-"
                    est = venue.get("estimate_range") or venue.get("estimate")
                    delivery_time = f"{est} min" if est else "-"

                    # 1. Osnovni bedževi sa feed-a i "NOVO" logika
                    akcije_lista = []
                    novo_status = "Ne"

                    # Skupljamo APSOLUTNO SVE bedževe koji postoje na početnoj strani
                    badges = venue.get("badges", [])
                    for badge in badges:
                        txt = badge.get("text", "")
                        if txt:
                            if txt.lower() in ["novo", "new"]:
                                novo_status = "Da"
                            else:
                                akcije_lista.append(f"• {txt}") # Nema više if % in txt... hvata sve!

                    # Skupljamo i dodatne labele
                    label = venue.get("label", "")
                    if label:
                        if label.lower() in ["novo", "new"]:
                            novo_status = "Da"
                        else:
                            akcije_lista.append(f"• {label}")

                    restaurants[slug] = {
                        "grad":       city,
                        "naziv":      name,
                        "slug":       slug,
                        "status":     status,
                        "ocena":      str(rating_score),
                        "dostava":    delivery_time,
                        "novo":       novo_status,
                        "akcije_feed": akcije_lista,
                        "item_popusti": "?",
                        "link":       f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}",
                        "naziv_norm": normalize(name),
                    }

            if items_in_response > 0:
                page_has_data = True
                break  # uspešno dohvaćena stranica, ne treba drugi endpoint

        new_this_page = len(restaurants) - count_before
        status_placeholder.info(
            f"🚴 **{city}**: Stranica {page_num+1} – +{new_this_page} novih "
            f"(ukupno {len(restaurants)}). Tražim akcije..."
        )

        # Stajemo tek kad API vrati praznu stranicu (nema više restorana)
        if not page_has_data:
            break

        skip += 40
        time.sleep(0.3)

    if not restaurants:
        status_placeholder.warning(f"⚠️ **{city}**: nije pronađen nijedan restoran.")
        return []

    # 2. Kopanje po "Ponude i Pogodnosti" za svaki restoran (sa limitom od 5 radnika)
    slugs = list(restaurants.keys())
    total = len(slugs)
    progress_text = f"🎯 Učitavam detaljne akcije za {city}..."
    progress_bar = st.progress(0, text=progress_text)
    
    # Sekvencijalno sa retry - jedini nacin da pouzdano izvucemo sve akcije
    # Wolt rate-limituje paralelne zahteve, sekvencijalno je jedino sigurno
    completed = 0
    failed_slugs = []  # slugovi koji nisu dali podatke, pokusavamo ponovo na kraju

    for slug in slugs:
        dynamic_akcije = fetch_dynamic_discounts(slug, lat, lon)
        if not dynamic_akcije:
            failed_slugs.append(slug)
        sve_akcije = set(restaurants[slug]["akcije_feed"] + dynamic_akcije)
        restaurants[slug]["akcije"] = "\n".join(sorted(sve_akcije)) if sve_akcije else "-"
        completed += 1
        if completed % 10 == 0 or completed == total:
            progress_bar.progress(completed / total, text=f"{progress_text} ({completed}/{total})")
        time.sleep(random.uniform(0.5, 1.5))

    # Retry za failed slugove - cekamo malo pa pokusamo ponovo
    if failed_slugs:
        progress_bar.progress(1.0, text=f"🔄 Retry za {len(failed_slugs)} restorana...")
        time.sleep(5)  # duza pauza pre retry-a
        for slug in failed_slugs:
            dynamic_akcije = fetch_dynamic_discounts(slug, lat, lon)
            if dynamic_akcije:
                sve_akcije = set(restaurants[slug]["akcije_feed"] + dynamic_akcije)
                restaurants[slug]["akcije"] = "\n".join(sorted(sve_akcije)) if sve_akcije else "-"
            time.sleep(random.uniform(1.0, 2.0))

    progress_bar.empty()

    # 3. Item-level provera - sekvencijalno sa random pauzama
    progress_bar3 = st.progress(0, text=f"🏷️ Proveravam item popuste za {city}...")
    for i, slug in enumerate(slugs):
        try:
            result = fetch_menu_discounts(slug)
            restaurants[slug]["item_popusti"] = "Da" if result["has_discounts"] else "Ne"
        except Exception:
            restaurants[slug]["item_popusti"] = "Ne"
        progress_bar3.progress((i + 1) / total, text=f"🏷️ Item popusti... ({i+1}/{total})")
        time.sleep(random.uniform(0.3, 0.8))
    progress_bar3.empty()

    # Brisanje pomoćnog ključa pre nego što ga vratimo
    for r in restaurants.values():
        r.pop("akcije_feed", None)

    return list(restaurants.values())

def scan_all_cities(selected_cities: list[str], status_placeholder) -> pd.DataFrame:
    all_rows = []
    for i, city in enumerate(selected_cities):
        try:
            rows = fetch_city(city, status_placeholder)
            all_rows.extend(rows)
            status_placeholder.success(f"✅ {city} završen! ({len(rows)} restorana)")
        except Exception as e:
            status_placeholder.error(f"❌ Greška za {city}: {e}")
            import traceback
            st.error(traceback.format_exc())
        if i < len(selected_cities) - 1:
            time.sleep(1)
    status_placeholder.empty()
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

# ─────────────────────────── EMAIL ───────────────────────────────────────────

def send_alert_email(am_email: str, am_name: str, alerts: list[dict]) -> bool:
    try:
        rows_html = ""
        for a in alerts:
            akcije_html = a["akcije"].replace("\n", "<br>")
            rows_html += f"""
            <tr>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;font-weight:600'>{a['naziv']}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;color:#555'>{a['grad']}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;color:#009de0'>{akcije_html}</td>
            </tr>"""

        html = f"""
        <html><body style='font-family:Arial,sans-serif;color:#222;max-width:700px;margin:auto'>
          <div style='background:#009de0;padding:24px 32px;border-radius:12px 12px 0 0'>
            <h2 style='color:#fff;margin:0'>🚴 Wolt Promo Alert</h2>
          </div>
          <div style='background:#fff;padding:24px 32px;border-radius:0 0 12px 12px;
                      box-shadow:0 4px 16px rgba(0,0,0,0.08)'>
            <p>Pozdrav <b>{am_name}</b>,</p>
            <p>Sledeći tvoji partneri imaju <b>aktivne akcije na Wolt-u</b>:</p>
            <table style='border-collapse:collapse;width:100%;font-size:14px'>
              <thead>
                <tr style='background:#f0f8ff'>
                  <th style='padding:10px 14px;text-align:left;color:#009de0'>Restoran</th>
                  <th style='padding:10px 14px;text-align:left;color:#009de0'>Grad</th>
                  <th style='padding:10px 14px;text-align:left;color:#009de0'>Akcije</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <p style='margin-top:24px;font-size:12px;color:#aaa'>
              Automatski alert – Wolt Monitor &bull; {local_now()}
            </p>
          </div>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = am_email
        msg["Subject"] = f"🚴 Wolt akcije – {len(alerts)} partnera ({datetime.date.today().strftime('%d.%m.%Y')})"
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, am_email, msg.as_string())
        return True
    except Exception as e:
        st.error(f"Email greška ({am_email}): {e}")
        return False

# ─────────────────────────── SESSION STATE ───────────────────────────────────

if "df_wolt" not in st.session_state:
    st.session_state.df_wolt = pd.DataFrame()
if "last_scan" not in st.session_state:
    st.session_state.last_scan = None

# ─────────────────────────── UI ──────────────────────────────────────────────

st.title("🚴 Promo Monitor")
st.caption("Prati akcije Wolt partnera po gradovima i obaveštava Account Managere.")

tab_scan, tab_amm, tab_alert, tab_stats, tab_debug = st.tabs([
    "🔍 Scan & Rezultati",
    "👥 AMM Baza",
    "📧 Pošalji Alert",
    "📈 Statistika",
    "🔧 Debug API",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: SCAN
# ══════════════════════════════════════════════════════════════════════════════
with tab_scan:
    st.markdown("### 🔍 Wolt scan")

    selected_cities = st.multiselect(
        "📍 Gradovi za skeniranje:",
        options=CITIES,
        default=CITIES,
        key="selected_cities",
    )

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        run_scan = st.button(
            "▶️ Pokreni scan", type="primary",
            use_container_width=True,
            disabled=not selected_cities,
        )
    with col_info:
        if st.session_state.last_scan:
            st.info(f"⏱️ Poslednji scan: **{st.session_state.last_scan}** |  "
                    f"Ukupno restorana: **{len(st.session_state.df_wolt)}**")
        if not selected_cities:
            st.warning("Izaberi bar jedan grad.")

    if run_scan and selected_cities:
        # Primeni cookie ako je sačuvan
        cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
        if cookie:
            session.headers["Cookie"] = cookie
        elif "Cookie" in session.headers:
            del session.headers["Cookie"]

        ph = st.empty()
        with st.spinner("Skeniranje u toku (ovo može potrajati minut-dva zbog detaljnih akcija)..."):
            df = scan_all_cities(selected_cities, ph)
        if not df.empty:
            st.session_state.df_wolt = df
            st.session_state.last_scan = local_now()
            st.success(f"✅ Scan završen! Pronađeno **{len(df)}** restorana, "
                       f"od toga **{len(df[df['akcije'] != '-'])}** sa akcijama.")
        else:
            st.error("❌ Scan nije vratio podatke. Proveri internet konekciju.")

    df = st.session_state.df_wolt
    if not df.empty:
        st.markdown("---")

        k1, k2, k3, k4 = st.columns(4)
        total = len(df)
        sa_akcijama = len(df[df["akcije"] != "-"])
        otvoreni = len(df[df["status"] == "Otvoren"])
        novi = len(df[df["novo"] == "Da"])

        for col, val, lbl in [
            (k1, total,       "Ukupno restorana"),
            (k2, sa_akcijama, "Sa aktivnim akcijama"),
            (k3, otvoreni,    "Trenutno otvoreno"),
            (k4, novi,        "Novih restorana"),
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

        # Filter po tipu akcije
        sve_akcije_tekst = sorted(set(
            line.lstrip("• ").strip()
            for akcije_cell in df["akcije"]
            if akcije_cell != "-"
            for line in akcije_cell.split("\n")
            if line.strip() and line.strip() != "-"
        ))
        akcija_filter = st.multiselect(
            "🎯 Filtriraj po akciji (pretraži i izaberi):",
            options=sve_akcije_tekst,
            default=[],
            placeholder="Sve akcije – ili izaberi specifičnu...",
            key="scan_akcija_filter"
        )

        # Primeni sve filtere
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

        # ── Dinamički brojač po aktivnim filterima ──
        total_fdf    = len(fdf)
        sa_ak        = len(fdf[fdf["akcije"] != "-"])
        sa_item      = len(fdf[fdf["item_popusti"] == "Da"]) if "item_popusti" in fdf.columns else 0
        sa_wplus     = len(fdf[fdf["akcije"].str.contains("[Wolt+]", na=False, regex=False)])
        sa_novi_f    = len(fdf[fdf["novo"] == "Da"])
        sa_otv       = len(fdf[fdf["status"] == "Otvoren"])

        cnt1, cnt2, cnt3, cnt4, cnt5, cnt6 = st.columns(6)
        for col, val, lbl, color in [
            (cnt1, total_fdf, "Prikazano",        "#009de0"),
            (cnt2, sa_ak,     "Sa akcijama",      "#27ae60"),
            (cnt3, sa_item,   "Item popusti",     "#e67e22"),
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
                "item_popusti": st.column_config.TextColumn("🏷️ Item popusti"),
                "akcije":       st.column_config.TextColumn("Akcije", width="large"),
                "link":         st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
            },
        )

        csv = fdf[display_cols].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Preuzmi CSV", csv, "wolt_scan.csv", "text/csv")



# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: AMM BAZA
# ══════════════════════════════════════════════════════════════════════════════
with tab_amm:
    st.markdown("### 👥 Baza Account Managera")
    st.caption("Definiši koji AM je zadužen za koji restoran. Čuva se u `amm_baza.csv`.")

    amm_df = load_amm()
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
                norm = normalize(final_rest)
                city_val = "" if amm_city == "-- Svi --" else amm_city
                new_row = {
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
                    amm_df = pd.concat(
                        [amm_df, pd.DataFrame([new_row])], ignore_index=True
                    )
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
        view = amm_df if am_filt == "Svi" else amm_df[amm_df["am_name"] == am_filt]

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
        st.info("Pokreni scan prvo pa će se ovde pojaviti lista restorana.")
    else:
        export_df = df_wolt[["grad", "naziv"]].copy()
        export_df["restaurant_display"] = export_df["naziv"]
        export_df["city"] = export_df["grad"]
        export_df["am_name"] = ""
        export_df["am_email"] = ""
        export_df = export_df[["restaurant_display", "city", "am_name", "am_email"]].drop_duplicates()

        grad_exp = st.multiselect(
            "Filtriraj po gradu (za export):",
            CITIES, default=CITIES, key="amm_export_grad"
        )
        export_filtered = export_df[export_df["city"].isin(grad_exp)]
        st.caption(f"Restorana za export: **{len(export_filtered)}**")

        csv_out = export_filtered.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Preuzmi listu restorana (CSV)",
            csv_out,
            "restorani_za_amm.csv",
            "text/csv",
        )

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

        df_wolt["naziv_norm"] = df_wolt["naziv"].apply(normalize)
        amm_df["restaurant_norm"] = amm_df["restaurant_norm"].apply(str)

        merged = df_wolt.merge(
            amm_df[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
            left_on="naziv_norm", right_on="restaurant_norm", how="inner"
        )

        sa_akcijama = merged[merged["akcije"] != "-"].copy()

        if sa_akcijama.empty:
            st.info("✅ Nijedan partner iz AMM baze trenutno nema aktivne akcije na Wolt-u.")
        else:
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

            st.caption(f"Partnera sa akcijama: **{len(preview)}** | AM-ova: **{preview['am_name'].nunique()}**")

            st.dataframe(
                preview[["grad", "naziv", "am_name", "am_email", "akcije"]].reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
                height=350,
                column_config={
                    "grad":     st.column_config.TextColumn("Grad"),
                    "naziv":    st.column_config.TextColumn("Restoran"),
                    "am_name":  st.column_config.TextColumn("AM"),
                    "am_email": st.column_config.TextColumn("Email"),
                    "akcije":   st.column_config.TextColumn("Akcije", width="large"),
                },
            )

            st.markdown("---")

            st.markdown("#### 📤 Pošalji mailove")
            st.info("Svaki AM dobija jedan mail sa svim svojim partnerima koji imaju akcije.")

            if st.button("🚀 Pošalji alertove", type="primary"):
                am_groups = preview.groupby(["am_name", "am_email"])
                sent_log = []
                success_count = 0

                for (am_name, am_email), grp in am_groups:
                    alerts = [
                        {"naziv": row["naziv"], "grad": row["grad"], "akcije": row["akcije"]}
                        for _, row in grp.iterrows()
                    ]
                    ok = send_alert_email(am_email, am_name, alerts)
                    if ok:
                        success_count += 1
                        st.success(f"✅ Mail poslat: **{am_name}** ({am_email}) – {len(alerts)} partnera")
                        for a in alerts:
                            sent_log.append({
                                "timestamp":          local_now(),
                                "city":               a["grad"],
                                "restaurant_display": a["naziv"],
                                "am_name":            am_name,
                                "am_email":           am_email,
                                "akcije":             a["akcije"],
                            })
                    else:
                        st.error(f"❌ Greška pri slanju: {am_name} ({am_email})")

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
                (k1, len(flog),                           "Ukupno alerta",      "#009de0"),
                (k2, flog["am_name"].nunique(),           "AM-ova",             "#8e44ad"),
                (k3, flog["restaurant_display"].nunique(),"Restorana",          "#27ae60"),
                (k4, flog["timestamp"].dt.date.nunique(), "Dana sa alertima",   "#e67e22"),
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
# TAB 5: DEBUG API
# ══════════════════════════════════════════════════════════════════════════════
with tab_debug:
    st.markdown("### 🔧 Debug & Podešavanja")

    st.markdown("#### 🍪 Wolt Cookie")
    st.markdown("""
Cookie je potreban da bi `consumer-api.wolt.com` vraćao akcije restorana.  
**Kako ga nabaviti:** Otvori bilo koji restoran na wolt.com → F12 → Network tab → 
klikni na `dynamic?lat=` request → Request Headers → kopiraj celu vrednost `Cookie:` polja.  
Cookie traje ~24h, posle toga ga osvežiti.
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
        st.success("✅ Cookie sačuvan i primenjen za ovaj session.")

    # Uvek primeni cookie iz session_state na pocetku
    if "wolt_cookie" in st.session_state and st.session_state["wolt_cookie"]:
        session.headers["Cookie"] = st.session_state["wolt_cookie"]

    st.markdown("---")
    st.markdown("### 🔧 Debug – Sirovi API odgovor za restoran")
    st.info(
        "Unesi slug restorana (deo URL-a: `wolt.com/en/srb/nis/restaurant/**SLUG**`) "
        "i izaberi grad da vidimo šta API tačno vraća."
    )

    dc1, dc2 = st.columns([2, 1])
    with dc1:
        debug_slug = st.text_input("Slug restorana:", placeholder="npr. mcdonalds-nis", key="debug_slug")
    with dc2:
        debug_city = st.selectbox("Grad:", CITIES, key="debug_city")

    if st.button("🔍 Dohvati sirovi JSON", key="debug_fetch") and debug_slug:
        lat, lon = CITY_COORDS.get(debug_city, (44.8178, 20.4569))
        city_slug = CITY_SLUG_MAP.get(debug_city, "nis")

        st.markdown("---")

        # ── 1. Feed (listing) podaci ──────────────────────────────────────
        st.markdown("#### 1️⃣ Feed – badges & label (sa listing stranice)")
        feed_url = f"https://restaurant-api.wolt.com/v3/venues/slug/{debug_slug}"
        feed_data, feed_status = wolt_get(feed_url)
        if feed_data:
            # Izvuci badges i label direktno
            results = feed_data.get("results", [{}])
            venue_info = results[0] if results else {}
            badges = venue_info.get("badges", [])
            label  = venue_info.get("label", "")
            st.write(f"**badges:** {badges}")
            st.write(f"**label:** `{label}`")
            with st.expander("Pun JSON (v3/venues/slug)"):
                st.json(feed_data)
        else:
            st.warning(f"v3 endpoint nije vratio podatke. HTTP status: {feed_status}")

        st.markdown("---")

        # ── 2. Dynamic / Deals & Benefits ────────────────────────────────
        st.markdown("#### 2️⃣ Dynamic endpoint (Deals & Benefits)")
        dyn_url = (
            f"https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{debug_slug}/dynamic/"
            f"?lat={lat}&lon={lon}&selected_delivery_method=homedelivery"
        )
        dyn_data, dyn_status = wolt_get(dyn_url)
        if dyn_data:
            with st.expander("Pun JSON (dynamic)", expanded=True):
                st.json(dyn_data)

            # Pokazi sve kljuceve prvog nivoa
            st.markdown("**Ključevi na vrhu odgovora:**")
            st.write(list(dyn_data.keys()))

            # Traži sekcije koje sadrže promocije
            sections = dyn_data.get("sections", [])
            st.markdown(f"**Broj sekcija:** {len(sections)}")
            for i, sec in enumerate(sections):
                sec_name = sec.get("name") or sec.get("title") or sec.get("template") or f"sekcija_{i}"
                items = sec.get("items", [])
                st.markdown(f"- `{sec_name}` → {len(items)} stavki")
        else:
            st.warning(f"Dynamic endpoint nije vratio podatke. HTTP status: {dyn_status}")

        st.markdown("---")

        # ── 3. Promotions dedicated endpoint ─────────────────────────────
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

        # ── 4. Loyalty / deals sekcija ───────────────────────────────────
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

        st.markdown("---")

        # ── 5. Šta trenutna skripta pronalazi ────────────────────────────
        st.markdown("#### 5️⃣ Šta trenutna skripta pronalazi za ovaj restoran")
        found = fetch_dynamic_discounts(debug_slug, lat, lon)
        if found:
            for f_item in found:
                st.write(f_item)
        else:
            st.error("Skripta nije pronašla nijednu akciju.")
