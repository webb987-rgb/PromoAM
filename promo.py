import re
import json
import time
import datetime
import smtplib
import urllib.parse
import urllib.request

import pandas as pd
import streamlit as st
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ─────────────────────────── KONFIGURACIJA ───────────────────────────────────

EMAIL_SENDER   = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"   # Gmail App Password

CITIES = ["Beograd", "Novi Sad", "Niš", "Kragujevac"]

AMM_FILE   = Path("amm_baza.csv")
AMM_COLS   = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]

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
    .promo-badge { display:inline-block; background:#fff3cd; color:#856404;
                   border-radius:6px; padding:2px 10px; font-size:.8rem;
                   margin:2px; border:1px solid #ffc10740; }
    .no-promo { color:#bbb; font-style:italic; }
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

# ─────────────────────────── WOLT API ────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "sr,en;q=0.9",
}

def geocode(city: str) -> tuple[float, float] | None:
    """Nominatim geocoding – vraća (lat, lon) ili None."""
    url = (
        "https://nominatim.openstreetmap.org/search"
        f"?q={urllib.parse.quote(city + ', Serbia')}&format=json&limit=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WoltMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def wolt_fetch(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def extract_discounts(item: dict) -> str:
    """Izvlači sve akcije iz jednog venue stavke iz Wolt feed API odgovora."""
    seen, res = set(), []

    def _scan(obj):
        if isinstance(obj, dict):
            # Wolt discount blokovi
            for key in ("discounts", "badges", "label"):
                val = obj.get(key)
                if isinstance(val, list):
                    for d in val:
                        _read_discount(d)
                elif isinstance(val, dict):
                    _read_discount(val)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    _scan(v)
        elif isinstance(obj, list):
            for i in obj:
                _scan(i)

    def _read_discount(d):
        if not isinstance(d, dict):
            return
        # traži title na svim poznatim mestima
        candidates = [
            d.get("title", ""),
            (d.get("description") or {}).get("title", ""),
            (d.get("banner") or {}).get("formatted_text", ""),
            (d.get("condition_item_badge") or {}).get("text", ""),
            d.get("text", ""),
            d.get("label", ""),
        ]
        for t in candidates:
            t = str(t or "").strip()
            if len(t) < 2:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            display = t if len(t) <= 80 else t[:77] + "..."
            res.append(f"• {display}")

    _scan(item)

    # Fallback: regex na JSON dump-u
    raw = json.dumps(item)
    for pct in re.findall(r'(\d{1,3}\s*%\s*(?:off|popust|discount))', raw, re.I):
        k = pct.lower()
        if k not in seen:
            seen.add(k)
            res.append(f"• {pct.strip()}")
    for amt in re.findall(r'(\d{2,5})\s*(?:rsd|din)', raw, re.I):
        if int(amt) > 10:
            k = f"{amt} rsd"
            if k not in seen:
                seen.add(k)
                res.append(f"• {amt} RSD popust")

    return "\n".join(res) if res else "-"


def fetch_city(city: str, status_placeholder) -> list[dict]:
    """Skenira jedan grad i vraća listu restorana sa akcijama."""

    # Mapa grad → Wolt URL slug (onako kako Wolt koristi u linkovima)
    CITY_SLUG_MAP = {
        "Beograd":    "belgrade",
        "Novi Sad":   "novi-sad",
        "Niš":        "nis",
        "Kragujevac": "kragujevac",
    }
    city_slug = CITY_SLUG_MAP.get(city, normalize(city).replace(" ", "-"))

    status_placeholder.info(f"📍 Geocodiram **{city}**...")
    coords = geocode(city)
    if not coords:
        status_placeholder.error(f"❌ Ne mogu da nađem koordinate za {city}")
        return []

    lat, lon = coords
    restaurants = {}
    skip = 0
    page_size = 40
    max_pages = 30

    status_placeholder.info(f"🔍 Učitavam restorane za **{city}**...")

    for page_num in range(max_pages):
        for endpoint in [
            f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}",
            f"https://restaurant-api.wolt.com/v1/pages/delivery?lat={lat}&lon={lon}&skip={skip}",
        ]:
            data = wolt_fetch(endpoint)
            if not data:
                continue

            items_found = 0
            for section in data.get("sections", []):
                for item in section.get("items", []):
                    venue = item.get("venue")
                    if not venue:
                        continue
                    name = venue.get("name", "")
                    slug = venue.get("slug", "")
                    if not name or not slug or slug in restaurants:
                        continue

                    status = "Otvoren" if venue.get("online") else "Zatvoren"
                    rating = venue.get("rating", {}) or {}
                    rating_score = rating.get("score", "-") if isinstance(rating, dict) else "-"

                    est = venue.get("estimate_range") or venue.get("estimate")
                    delivery_time = f"{est} min" if est else "-"

                    akcije = extract_discounts(item)

                    restaurants[slug] = {
                        "grad":           city,
                        "naziv":          name,
                        "slug":           slug,
                        "status":         status,
                        "ocena":          str(rating_score),
                        "dostava":        delivery_time,
                        "akcije":         akcije,
                        "link":           f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}",
                        "naziv_norm":     normalize(name),
                    }
                    items_found += 1

            if items_found > 0:
                break  # uspešan endpoint, ne probaj drugi

        count = len(restaurants)
        status_placeholder.info(f"🚴 **{city}**: {count} restorana učitano (stranica {page_num + 1})")

        if items_found < 10:
            break  # poslednja stranica

        skip += page_size
        time.sleep(0.3)

    return list(restaurants.values())


def scan_all_cities(progress_placeholder) -> pd.DataFrame:
    all_rows = []
    for i, city in enumerate(CITIES):
        rows = fetch_city(city, progress_placeholder)
        all_rows.extend(rows)
        progress_placeholder.info(f"✅ {city}: {len(rows)} restorana | Ukupno: {len(all_rows)}")
        if i < len(CITIES) - 1:
            time.sleep(1)
    progress_placeholder.empty()
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

# ─────────────────────────── EMAIL ───────────────────────────────────────────

def send_alert_email(am_email: str, am_name: str, alerts: list[dict]) -> bool:
    """
    alerts = lista: {naziv, grad, akcije}
    """
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

tab_scan, tab_amm, tab_alert, tab_stats = st.tabs([
    "🔍 Scan & Rezultati",
    "👥 AMM Baza",
    "📧 Pošalji Alert",
    "📈 Statistika",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: SCAN
# ══════════════════════════════════════════════════════════════════════════════
with tab_scan:
    st.markdown("### 🔍 Wolt scan")
    st.markdown(f"Gradovi: **{', '.join(CITIES)}**")

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        run_scan = st.button("▶️ Pokreni scan", type="primary", use_container_width=True)
    with col_info:
        if st.session_state.last_scan:
            st.info(f"⏱️ Poslednji scan: **{st.session_state.last_scan}**  |  "
                    f"Ukupno restorana: **{len(st.session_state.df_wolt)}**")

    if run_scan:
        ph = st.empty()
        with st.spinner("Skeniranje u toku..."):
            df = scan_all_cities(ph)
        if not df.empty:
            st.session_state.df_wolt = df
            st.session_state.last_scan = local_now()
            st.success(f"✅ Scan završen! Pronađeno **{len(df)}** restorana.")
        else:
            st.error("❌ Scan nije vratio podatke. Proveri internet konekciju.")

    df = st.session_state.df_wolt
    if not df.empty:
        st.markdown("---")

        # KPI red
        k1, k2, k3, k4 = st.columns(4)
        total = len(df)
        sa_akcijama = len(df[df["akcije"] != "-"])
        otvoreni = len(df[df["status"] == "Otvoren"])
        gradovi = df["grad"].nunique()

        for col, val, lbl in [
            (k1, total,       "Ukupno restorana"),
            (k2, sa_akcijama, "Sa aktivnim akcijama"),
            (k3, otvoreni,    "Trenutno otvoreno"),
            (k4, gradovi,     "Gradova"),
        ]:
            with col:
                st.markdown(f"""
                <div class='kpi'>
                  <div class='kpi-val'>{val}</div>
                  <div class='kpi-lbl'>{lbl}</div>
                </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # Filteri
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            grad_filter = st.multiselect("Grad:", CITIES, default=CITIES, key="scan_grad")
        with fc2:
            samo_akcije = st.checkbox("📌 Samo sa akcijama", value=False, key="scan_akcije")
        with fc3:
            search = st.text_input("🔎 Pretraži naziv:", key="scan_search")

        fdf = df[df["grad"].isin(grad_filter)]
        if samo_akcije:
            fdf = fdf[fdf["akcije"] != "-"]
        if search.strip():
            fdf = fdf[fdf["naziv"].str.contains(search.strip(), case=False, na=False)]

        st.caption(f"Prikazano: **{len(fdf)}** restorana")

        display_cols = ["grad", "naziv", "status", "ocena", "dostava", "akcije", "link"]
        st.dataframe(
            fdf[display_cols].reset_index(drop=True),
            use_container_width=True,
            hide_index=True,
            height=520,
            column_config={
                "grad":    st.column_config.TextColumn("Grad"),
                "naziv":   st.column_config.TextColumn("Restoran"),
                "status":  st.column_config.TextColumn("Status"),
                "ocena":   st.column_config.TextColumn("Ocena"),
                "dostava": st.column_config.TextColumn("Dostava"),
                "akcije":  st.column_config.TextColumn("Akcije", width="large"),
                "link":    st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
            },
        )

        # Eksport
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

    # ── Dodavanje ─────────────────────────────────────────────────────────────
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

    # ── Prikaz i editovanje ───────────────────────────────────────────────────
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

    # ── Export restorana za bulk popunjavanje ────────────────────────────────
    st.markdown("---")
    st.markdown("#### 📤 Export restorana za dodelu AM-ova")
    st.caption("Preuzmi CSV sa svim restoranima iz poslednjeg scana, popuni kolone `am_name` i `am_email` u Excelu, pa uvezi nazad.")

    if df_wolt.empty:
        st.info("Pokreni scan prvo pa će se ovde pojaviti lista restorana.")
    else:
        # Pravljenje export CSV-a sa praznim AM kolonama
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
            help="Otvori u Excelu, popuni am_name i am_email, pa uvezi nazad dole."
        )

    # ── Bulk CSV import ───────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 📥 Bulk import CSV")
    st.caption("Kolone: `restaurant_display, city, am_name, am_email`")
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
        # ── Preview: koji partneri imaju akcije ──────────────────────────────
        st.markdown("#### 🔍 Pregled – partneri sa akcijama")

        df_wolt["naziv_norm"] = df_wolt["naziv"].apply(normalize)
        amm_df["restaurant_norm"] = amm_df["restaurant_norm"].apply(str)

        # Spoji scan sa AMM bazom
        merged = df_wolt.merge(
            amm_df[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
            left_on="naziv_norm", right_on="restaurant_norm", how="inner"
        )

        # Filtriramo samo one koji imaju akcije
        sa_akcijama = merged[merged["akcije"] != "-"].copy()

        if sa_akcijama.empty:
            st.info("✅ Nijedan partner iz AMM baze trenutno nema aktivne akcije na Wolt-u.")
        else:
            # Filtri
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

            # ── Slanje maila ─────────────────────────────────────────────────
            st.markdown("#### 📤 Pošalji mailove")
            st.info("Svaki AM dobija jedan mail sa svim svojim partnerima koji imaju akcije.")

            if st.button("🚀 Pošalji alertove", type="primary", use_container_width=False):
                # Grupiši po AM-u
                am_groups = preview.groupby(["am_name", "am_email"])
                sent_log = []
                results_col = st.empty()
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

        # Date filter
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
            # KPI
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

            # Tabela po AM-u
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

            # Detaljan log
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
