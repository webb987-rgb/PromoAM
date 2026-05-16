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
EMAIL_PASSWORD = "sdehqzbnqefjlomo"

CITY_KEYS    = ["Beograd", "Novi Sad", "Nis", "Kragujevac"]
CITY_DISPLAY = {
    "Beograd":    "Beograd",
    "Novi Sad":   "Novi Sad",
    "Nis":        "Niš",
    "Kragujevac": "Kragujevac",
}
CITIES = [CITY_DISPLAY[k] for k in CITY_KEYS]

FETCH_WORKERS = 10

AMM_FILE  = Path("amm_baza.csv")
AMM_COLS  = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]
SCAN_FILE = Path("scan_baza_item.csv")

# ─────────────────────────── PAGE CONFIG ─────────────────────────────────────

st.set_page_config(page_title="Promo Monitor – ITEM ONLY", page_icon="🏷️", layout="wide")

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

# ─────────────────────────── PERMANENTNA BAZA ────────────────────────────────

def save_scan(df: pd.DataFrame):
    df.to_csv(SCAN_FILE, index=False)

def load_scan() -> pd.DataFrame:
    if SCAN_FILE.exists():
        try:
            return pd.read_csv(SCAN_FILE)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()

def scan_meta() -> str:
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

# ─────────────────────────── WOLT API ────────────────────────────────────────

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

def wolt_get(url: str) -> tuple:
    try:
        r = session.get(url, timeout=15)
        if r.status_code == 200:
            return r.json(), 200
        return None, r.status_code
    except Exception:
        return None, -1

# ─────────────────────────── ITEM-ONLY FETCH ─────────────────────────────────

def _has_item_discounts(data: dict) -> bool:
    for item in data.get("items", []):
        price = (item.get("base_price") or item.get("price") or 0) / 100
        orig = (
            item.get("original_price") or
            item.get("strikethrough_price") or
            item.get("compare_at_price") or
            item.get("unit_price") or 0
        ) / 100
        if orig > 0 and orig > price:
            return True
    return False


def _fetch_one_item(slug: str, stop_event: threading.Event) -> tuple[str, str]:
    """
    Jedan API poziv po restoranu – samo assortment endpoint.
    Vraća (slug, "Da"/"Ne").
    """
    if stop_event.is_set():
        return slug, "Ne"

    ts = make_thread_session()
    ass_url = (
        f"https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment"
    )

    for attempt in range(2):
        if stop_event.is_set():
            return slug, "Ne"
        try:
            r = ts.get(ass_url, timeout=12)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return slug, "Da" if _has_item_discounts(r.json()) else "Ne"
            break
        except Exception:
            if attempt < 1:
                time.sleep(0.3)

    return slug, "Ne"

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

    # Paginacija – samo restaurants endpoint
    for page_num in range(50):
        if stop_event.is_set():
            break

        endpoint = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}"
        data, _ = wolt_get(endpoint)
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
                    restaurants[slug] = {
                        "grad":       city_display,
                        "naziv":      name,
                        "slug":       slug,
                        "naziv_norm": normalize(name),
                        "akcija":     "Ne",
                        "link":       f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}",
                    }

        status_placeholder.info(
            f"🚴 **{city_display}**: str. {page_num+1} – "
            f"+{len(restaurants) - (len(restaurants) - items_in_response)} novih "
            f"(ukupno {len(restaurants)})"
        )

        if items_in_response == 0:
            break

        skip += 40
        time.sleep(0.2)

    if not restaurants or stop_event.is_set():
        return []

    # Paralelno fetchovanje – samo jedan API poziv po restoranu
    slugs = list(restaurants.keys())
    total = len(slugs)
    progress_bar = st.progress(0, text=f"⚡ Čekiram item popuste za {city_display} ({total} restorana)...")
    completed = 0

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_one_item, slug, stop_event): slug
            for slug in slugs
        }
        for future in as_completed(futures):
            if stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            try:
                slug, result = future.result()
                restaurants[slug]["akcija"] = result
            except Exception:
                pass

            completed += 1
            if completed % 5 == 0 or completed == total:
                progress_bar.progress(
                    min(completed / total, 1.0),
                    text=f"⚡ {city_display}: {completed}/{total} obrađeno..."
                )

    progress_bar.empty()
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
                sa = sum(1 for r in rows if r["akcija"] == "Da")
                status_placeholder.success(f"✅ {city}: {len(rows)} restorana, {sa} sa item akcijama")
        except Exception as e:
            status_placeholder.error(f"❌ Greška za {city}: {e}")
        if i < len(selected_cities) - 1 and not stop_event.is_set():
            time.sleep(0.5)
    status_placeholder.empty()
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

# ─────────────────────────── EMAIL ───────────────────────────────────────────

def send_alert_email(am_email: str, am_name: str, alerts: list[dict]) -> bool:
    try:
        rows_html = ""
        for a in alerts:
            link = a.get("link", "")
            if link:
                naziv_cell = f"<a href='{link}' style='color:#222;text-decoration:none;font-weight:600'>{a['naziv']}</a>"
            else:
                naziv_cell = f"<span style='font-weight:600'>{a['naziv']}</span>"

            rows_html += f"""
            <tr>
              <td style='padding:10px 14px;border-bottom:1px solid #eee'>{naziv_cell}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;color:#555'>{a['grad']}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;text-align:center'>
                <span style='background:#fff3cd;color:#856404;padding:3px 12px;border-radius:12px;font-weight:700;font-size:13px'>🏷️ DA</span>
              </td>
            </tr>"""

        if not rows_html:
            return True

        today_str = datetime.date.today().strftime("%d.%m.%Y")
        html = f"""
        <html><body style='font-family:Arial,sans-serif;color:#222;max-width:720px;margin:auto'>
          <div style='background:#1a1a2e;padding:24px 32px;border-radius:12px 12px 0 0'>
            <h2 style='color:#fff;margin:0'>🏷️ Item Popusti – {today_str}</h2>
          </div>
          <div style='background:#fff;padding:24px 32px;border-radius:0 0 12px 12px;
                      box-shadow:0 4px 16px rgba(0,0,0,0.08)'>
            <p>Zdravo <b>{am_name}</b>,</p>
            <p>Sledeći tvoji partneri imaju <b>aktivne item popuste u meniju</b>:</p>
            <table style='border-collapse:collapse;width:100%;font-size:14px'>
              <thead>
                <tr style='background:#f0f4ff'>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Restoran</th>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Grad</th>
                  <th style='padding:10px 14px;text-align:center;color:#1a1a2e;border-bottom:2px solid #dde'>Item akcija</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <p style='margin-top:20px;font-size:12px;color:#999'>
              Automatski izveštaj &bull; {local_now()} &bull; Klikni na naziv restorana da vidiš meni.
            </p>
          </div>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = am_email
        msg["Subject"] = f"🏷️ Item popusti – {len(alerts)} partnera – {today_str}"
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
if "scan_stop_event" not in st.session_state:
    st.session_state.scan_stop_event = threading.Event()
if "scan_running" not in st.session_state:
    st.session_state.scan_running = False
if "scan_start_time" not in st.session_state:
    st.session_state.scan_start_time = None

# ─────────────────────────── UI ──────────────────────────────────────────────

st.title("🏷️ Promo Monitor – Item Only")
st.caption("Brzi sken: samo proverava da li restoran ima item popuste u meniju (DA/NE). Jedan API poziv po restoranu.")

tab_scan, tab_alert, tab_debug = st.tabs([
    "🔍 Scan & Rezultati",
    "📧 Pošalji Alert",
    "🔧 Debug",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: SCAN
# ══════════════════════════════════════════════════════════════════════════════
with tab_scan:
    st.markdown("### 🔍 Scan")
    st.info("Svaki restoran se proverava **jednim API pozivom** – samo assortment endpoint. Nema dynamic, nema text akcija.")

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
        )
    with col_info:
        if st.session_state.scan_running and st.session_state.scan_start_time:
            elapsed = time.time() - st.session_state.scan_start_time
            m, s = divmod(int(elapsed), 60)
            st.markdown(f"<div class='timer-box'>⏱️ Skeniranje traje: {m:02d}:{s:02d}</div>", unsafe_allow_html=True)
        elif st.session_state.last_scan:
            st.info(f"⏱️ Poslednji scan: **{st.session_state.last_scan}** | "
                    f"Ukupno: **{len(st.session_state.df_wolt)}** restorana")

    # Učitaj prethodni sken
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

    if stop_scan and st.session_state.scan_running:
        st.session_state.scan_stop_event.set()
        st.session_state.scan_running = False
        st.warning("⏹️ Skeniranje zaustavljeno.")

    if run_scan and selected_cities and not st.session_state.scan_running:
        cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
        if cookie:
            session.headers["Cookie"] = cookie
        elif "Cookie" in session.headers:
            del session.headers["Cookie"]

        st.session_state.scan_stop_event = threading.Event()
        st.session_state.scan_running = True
        st.session_state.scan_start_time = time.time()

        ph = st.empty()
        df = scan_all_cities(selected_cities, ph, st.session_state.scan_stop_event)

        scan_duration = time.time() - st.session_state.scan_start_time
        st.session_state.scan_running = False

        if not df.empty:
            st.session_state.df_wolt = df
            st.session_state.last_scan = local_now()
            save_scan(df)
            m, s = divmod(int(scan_duration), 60)
            sa = len(df[df["akcija"] == "Da"])
            st.success(
                f"✅ Scan završen za **{m:02d}:{s:02d}**! "
                f"**{len(df)}** restorana, od toga **{sa}** sa item akcijama."
            )
        else:
            if st.session_state.scan_stop_event.is_set():
                st.warning("⏹️ Scan je zaustavljen pre završetka.")
            else:
                st.error("❌ Scan nije vratio podatke. Proveri cookie u Debug tabu.")

    df = st.session_state.df_wolt
    if not df.empty:
        st.markdown("---")

        total    = len(df)
        sa_ak    = len(df[df["akcija"] == "Da"])
        bez_ak   = total - sa_ak

        k1, k2, k3 = st.columns(3)
        for col, val, lbl, color in [
            (k1, total,  "Ukupno restorana", "#009de0"),
            (k2, sa_ak,  "Sa item akcijama", "#27ae60"),
            (k3, bez_ak, "Bez akcija",       "#aaa"),
        ]:
            with col:
                st.markdown(f"""
                <div class='kpi' style='border-top:4px solid {color}'>
                  <div class='kpi-val' style='color:{color}'>{val}</div>
                  <div class='kpi-lbl'>{lbl}</div>
                </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            grad_filter = st.multiselect("Grad:", CITIES, default=CITIES, key="scan_grad")
        with fc2:
            samo_akcije = st.checkbox("📌 Samo sa akcijama", value=False, key="scan_akcije")
        with fc3:
            search = st.text_input("🔎 Pretraži naziv:", key="scan_search")

        fdf = df[df["grad"].isin(grad_filter)]
        if samo_akcije:
            fdf = fdf[fdf["akcija"] == "Da"]
        if search.strip():
            fdf = fdf[fdf["naziv"].str.contains(search.strip(), case=False, na=False)]

        display_cols = ["grad", "naziv", "akcija", "link"]
        display_cols = [c for c in display_cols if c in fdf.columns]

        st.dataframe(
            fdf[display_cols].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
            height=500,
            column_config={
                "grad":   st.column_config.TextColumn("Grad"),
                "naziv":  st.column_config.TextColumn("Restoran"),
                "akcija": st.column_config.TextColumn("🏷️ Item akcija"),
                "link":   st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
            },
        )

        csv = fdf[display_cols].to_csv(index=False).encode("utf-8")
        st.download_button("📥 Preuzmi CSV", csv, "scan_item.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: POŠALJI ALERT
# ══════════════════════════════════════════════════════════════════════════════
with tab_alert:
    st.markdown("### 📧 Pošalji Alert AM-ovima")

    df_wolt = st.session_state.df_wolt
    amm_df  = load_amm()

    if df_wolt.empty:
        st.warning("⚠️ Nema scan podataka. Pokreni scan u prvom tabu.")
    elif amm_df.empty:
        st.warning("⚠️ AMM baza je prazna. Dodaj restorane u `amm_baza.csv` ili u promo_6.py.")
    else:
        df_wolt["naziv_norm"] = df_wolt["naziv"].apply(normalize)

        merged = df_wolt[df_wolt["akcija"] == "Da"].merge(
            amm_df[["restaurant_norm", "city", "am_name", "am_email"]],
            left_on="naziv_norm", right_on="restaurant_norm", how="inner"
        )

        if merged.empty:
            st.info("✅ Nijedan partner iz AMM baze trenutno nema item akcije.")
        else:
            af1, af2 = st.columns(2)
            with af1:
                grad_filt = st.multiselect("Grad:", CITIES, default=CITIES, key="alert_grad")
            with af2:
                am_filt = st.multiselect(
                    "AM:",
                    sorted(merged["am_name"].dropna().unique().tolist()),
                    default=sorted(merged["am_name"].dropna().unique().tolist()),
                    key="alert_am",
                )

            preview = merged[
                (merged["grad"].isin(grad_filt)) &
                (merged["am_name"].isin(am_filt))
            ]

            st.caption(
                f"Partnera za alert: **{len(preview)}** | AM-ova: **{preview['am_name'].nunique()}**"
            )

            preview_cols = ["grad", "naziv", "am_name", "am_email", "link"]
            preview_cols = [c for c in preview_cols if c in preview.columns]

            st.dataframe(
                preview[preview_cols].reset_index(drop=True),
                use_container_width=True,
                hide_index=True,
                height=300,
                column_config={
                    "grad":     st.column_config.TextColumn("Grad"),
                    "naziv":    st.column_config.TextColumn("Restoran"),
                    "am_name":  st.column_config.TextColumn("AM"),
                    "am_email": st.column_config.TextColumn("Email"),
                    "link":     st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
                },
            )

            st.markdown("---")
            if st.button("🚀 Pošalji alertove", type="primary"):
                success_count = 0
                for (am_name, am_email_addr), grp in preview.groupby(["am_name", "am_email"]):
                    alerts = [
                        {"naziv": row["naziv"], "grad": row["grad"], "link": row.get("link", "")}
                        for _, row in grp.iterrows()
                    ]
                    ok = send_alert_email(am_email_addr, am_name, alerts)
                    if ok:
                        success_count += 1
                        st.success(f"✅ {am_name} ({am_email_addr}) – {len(alerts)} partnera")
                    else:
                        st.error(f"❌ Greška: {am_name}")
                st.markdown(f"**Završeno:** {success_count} mailova poslato.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: DEBUG
# ══════════════════════════════════════════════════════════════════════════════
with tab_debug:
    st.markdown("### 🔧 Debug & Cookie")

    saved_cookie = st.session_state.get("wolt_cookie", WOLT_COOKIE)
    new_cookie = st.text_area(
        "Cookie string:",
        value=saved_cookie,
        height=100,
        placeholder="ravelinDeviceId=...; __woltUid=...; ...",
        key="cookie_input"
    )
    if st.button("💾 Sačuvaj cookie", key="save_cookie"):
        st.session_state["wolt_cookie"] = new_cookie
        session.headers["Cookie"] = new_cookie
        st.success("✅ Cookie sačuvan.")

    if "wolt_cookie" in st.session_state and st.session_state["wolt_cookie"]:
        session.headers["Cookie"] = st.session_state["wolt_cookie"]

    st.markdown("---")
    st.markdown("### 🔬 Test jednog restorana")

    debug_slug = st.text_input("Slug:", placeholder="npr. mcdonalds-nis", key="debug_slug")
    if st.button("🔍 Proveri item popuste", key="debug_fetch") and debug_slug:
        ass_url = f"https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{debug_slug}/assortment"
        data, status = wolt_get(ass_url)
        if data:
            result = _has_item_discounts(data)
            if result:
                st.success(f"✅ **DA** – restoran ima item popuste.")
            else:
                st.warning(f"❌ **NE** – nema item popusta.")
            with st.expander("Pun JSON odgovor"):
                st.json(data)
        else:
            st.error(f"API nije vratio podatke. HTTP status: {status}")
