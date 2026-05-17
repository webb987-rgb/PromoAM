import streamlit as st
import requests
import pandas as pd
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────── KONFIGURACIJA ───────────────────────────────────

# Iste lokacije koje si imao za višestruko pokrivanje grada
CITY_MULTI_COORDS = {
    "Beograd": [
        (44.8178, 20.4569), (44.7866, 20.4489), (44.8525, 20.3914),
        (44.8010, 20.5132), (44.8650, 20.6432), (44.7700, 20.3900), (44.8300, 20.5800),
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
        (44.0128, 20.9114), (44.0000, 20.8900), (44.0300, 20.9300), (43.9900, 20.9400),
    ],
}

CITY_SLUG_MAP = {
    "Beograd": "belgrade", "Novi Sad": "novi-sad", "Nis": "nis", "Kragujevac": "kragujevac",
}

FETCH_WORKERS = 15 # Povećali smo jer je assortment endpoint stabilan i brz

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

# ─────────────────────────── API FUNKCIJE ────────────────────────────────────

def get_assortment_promos(slug):
    """
    Brzi udarac na assortment endpoint. 
    Traži iteme gde je base_price (stara cena) veća od price (nove cene).
    """
    url = f"https://consumer-api.wolt.com/consumer-api/consumer-assortment/v1/venues/slug/{slug}/assortment"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return slug, []
        
        data = r.json()
        item_promos = []
        
        for item in data.get("items", []):
            stara_cena = item.get("base_price")
            nova_cena = item.get("price")
            
            if stara_cena and nova_cena and stara_cena > nova_cena:
                sc_rsd = stara_cena // 100
                nc_rsd = nova_cena // 100
                ime = item.get("name", "Nepoznat artikal")
                item_promos.append(f"• [Sniženje] {ime} ({sc_rsd} -> {nc_rsd} RSD)")
                
        return slug, item_promos
    except Exception:
        return slug, []

def scan_city(city_display, status_placeholder):
    city_key = city_display
    multi_coords = CITY_MULTI_COORDS.get(city_key)
    city_slug = CITY_SLUG_MAP.get(city_key)

    restaurants = {}
    
    status_placeholder.info(f"📍 Skeniram listu restorana za {city_display}...")
    
    # ── Faza 1: Brzi sken celog grada (i čitanje opštih promocija) ──
    for loc_idx, (lat, lon) in enumerate(multi_coords):
        skip = 0
        while True:
            url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
                if r.status_code != 200: break
                
                data = r.json()
                items_found = 0
                
                for section in data.get("sections", []):
                    for item in section.get("items", []):
                        venue = item.get("venue")
                        if not venue: continue
                        
                        slug = venue.get("slug")
                        name = venue.get("name")
                        if not slug or not name or slug in restaurants: continue
                        
                        items_found += 1
                        
                        # --- NOVO: Čitamo opšte promocije restorana odmah ovde ---
                        feed_akcije = []
                        for promo in venue.get("promotions", []):
                            txt = promo.get("text") or promo.get("title", "")
                            if txt: feed_akcije.append(f"• [Promo] {txt}")
                            
                        # Čitamo i klasične bedževe (npr. Wolt+)
                        for badge in venue.get("badges", []):
                            txt = badge.get("text", "")
                            if txt and txt.lower() not in ["novo", "new"]:
                                feed_akcije.append(f"• [Badge] {txt}")
                                
                        est = venue.get("estimate_range") or venue.get("estimate")
                        
                        restaurants[slug] = {
                            "grad": city_display,
                            "naziv": name,
                            "slug": slug,
                            "status": "Otvoren" if venue.get("online") else "Zatvoren",
                            "dostava": f"{est} min" if est else "-",
                            "opste_akcije": feed_akcije,  # Sačuvano iz feeda
                            "item_akcije": [],            # Popunićemo u fazi 2
                            "link": f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}"
                        }
                
                if items_found == 0:
                    break
                skip += 40
            except Exception:
                break

    if not restaurants:
        return []

    # ── Faza 2: Turbo Assortment Fetch za item-level sniženja ──
    slugs = list(restaurants.keys())
    total = len(slugs)
    
    status_placeholder.info(f"⚡ Tražim item-level popuste za {total} restorana u {city_display}...")
    
    completed = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {executor.submit(get_assortment_promos, slug): slug for slug in slugs}
        
        for future in as_completed(futures):
            try:
                slug, item_promos = future.result()
                restaurants[slug]["item_akcije"] = item_promos
            except Exception:
                pass
            
            completed += 1
            if completed % 20 == 0:
                status_placeholder.info(f"⚡ Skeniram menije: {completed} / {total} završeno...")

    # Spajamo akcije u jedan string za prikaz
    for r in restaurants.values():
        sve_akcije = r["opste_akcije"] + r["item_akcije"]
        # Filtriramo duplikate
        sve_akcije = list(dict.fromkeys(sve_akcije))
        r["akcije"] = "\n".join(sve_akcije) if sve_akcije else "-"
        
        # Brišemo pomoćne liste
        del r["opste_akcije"]
        del r["item_akcije"]

    status_placeholder.success(f"✅ {city_display} završen! ({total} restorana)")
    return list(restaurants.values())

# ─────────────────────────── STREAMLIT UI ────────────────────────────────────

st.set_page_config(page_title="Turbo Promo Monitor", page_icon="🚀", layout="wide")

st.title("🚀 Turbo Promo Monitor")
st.markdown("Hibridni engine: Povuče restorane i njihove opšte bedževe + munjevito prođe kroz sve menije za item-level popuste.")

cities = list(CITY_MULTI_COORDS.keys())
selected_cities = st.multiselect("📍 Izaberi gradove:", cities, default=["Beograd"])

if st.button("▶️ Pokreni Turbo Sken", type="primary", use_container_width=True):
    if not selected_cities:
        st.warning("Izaberi bar jedan grad.")
    else:
        st.session_state.scan_results = []
        status_box = st.empty()
        
        start_time = time.time()
        
        all_data = []
        for city in selected_cities:
            city_data = scan_city(city, status_box)
            all_data.extend(city_data)
            
        elapsed = time.time() - start_time
        
        if all_data:
            df = pd.DataFrame(all_data)
            st.session_state.scan_results = df
            status_box.success(f"🎉 Turbo sken završen za {elapsed:.1f} sekundi! Ukupno restorana: {len(df)}")
        else:
            status_box.error("Nije pronađen nijedan restoran.")

if "scan_results" in st.session_state and isinstance(st.session_state.scan_results, pd.DataFrame):
    df = st.session_state.scan_results
    
    st.markdown("---")
    
    sa_akcijama = len(df[df["akcije"] != "-"])
    otvoreni = len(df[df["status"] == "Otvoren"])
    item_popusti = len(df[df["akcije"].str.contains(r"\[Sniženje\]", na=False)])
    wolt_plus = len(df[df["akcije"].str.contains("Wolt+", case=False, na=False)])
    
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Ukupno restorana", len(df))
    c2.metric("🟢 Ima bilo kakvu akciju", sa_akcijama)
    c3.metric("🍔 Ima snižena jela", item_popusti)
    c4.metric("💙 Wolt+ akcije", wolt_plus)
    c5.metric("Trenutno otvoreno", otvoreni)

    st.markdown("<br>", unsafe_allow_html=True)
    
    # Filteri
    col_f1, col_f2, col_f3 = st.columns(3)
    samo_akcije = col_f1.checkbox("📌 Prikaži samo sa akcijama", value=True)
    samo_snizenja = col_f2.checkbox("🍔 Prikaži samo konkretna sniženja jela")
    search = col_f3.text_input("🔎 Pretraži restoran:")
    
    view_df = df.copy()
    if samo_akcije:
        view_df = view_df[view_df["akcije"] != "-"]
    if samo_snizenja:
        view_df = view_df[view_df["akcije"].str.contains(r"\[Sniženje\]", na=False)]
    if search:
        view_df = view_df[view_df["naziv"].str.contains(search, case=False, na=False)]
        
    st.dataframe(
        view_df[["grad", "naziv", "status", "dostava", "akcije", "link"]].reset_index(drop=True),
        use_container_width=True,
        hide_index=True,
        height=600,
        column_config={
            "akcije": st.column_config.TextColumn("Akcije & Sniženja", width="large"),
            "link": st.column_config.LinkColumn("Link", display_text="Otvori ↗"),
        }
    )
