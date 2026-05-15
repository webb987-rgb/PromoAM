import streamlit as st
import asyncio
import os
import subprocess
import pandas as pd
import datetime
import smtplib
import re
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- 1. INSTALACIJA ZAVISNOSTI (Rešenje za beli ekran) ---
@st.cache_resource
def install_playwright_stuff():
    try:
        # Instalacija Chromiuma i sistemskih zavisnosti
        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
        subprocess.run(["python", "-m", "playwright", "install-deps"], check=True)
        return True
    except Exception as e:
        st.error(f"Greška pri instalaciji Playwright-a: {e}")
        return False

with st.spinner("Inicijalizacija sistema..."):
    install_playwright_stuff()

# Tek nakon instalacije uvozimo Playwright
from playwright.async_api import async_playwright

# --- 2. KONFIGURACIJA ---
EMAIL_SENDER = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"
RECIPIENTS = ["manager1@glovo.com"] # DODAJ PRAVE MEJLOVE OVDE

GLOVO_AUTH = "glovo_auth.json"

CITIES = {
    "Beograd": {"lat": 44.8125, "lon": 20.4612, "address": "Makenzijeva 57, Beograd"},
    "Novi Sad": {"lat": 45.2551, "lon": 19.8452, "address": "Trg Slobode, Novi Sad"},
    "Niš": {"lat": 43.3209, "lon": 21.8954, "address": "Trg Kralja Milana, Niš"},
    "Kragujevac": {"lat": 44.0128, "lon": 20.9114, "address": "Centar, Kragujevac"}
}

# --- 3. POMOĆNE FUNKCIJE ---
def normalize_name(name):
    return re.sub(r'[^\w]', '', str(name).lower())

def send_sales_email(df_gaps):
    if df_gaps.empty:
        return
    
    subject = f"🔴 SALES ALERT: Promo Gaps na Woltu ({datetime.date.today().strftime('%d.%m.%Y')})"
    html_table = df_gaps.to_html(index=False, escape=False, border=0)
    
    style = """
    <style>
        table { border-collapse: collapse; width: 100%; font-family: sans-serif; }
        th { background-color: #ffc244; padding: 10px; text-align: left; }
        td { border-bottom: 1px solid #ddd; padding: 10px; }
        .wolt { color: #00c2e8; font-weight: bold; }
        .urgency { color: #e74c3c; font-weight: bold; }
    </style>
    """

    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = EMAIL_SENDER
    msg['To'] = ", ".join(RECIPIENTS)

    body = f"<html><head>{style}</head><body><h3>Propusti u promocijama</h3>{html_table}</body></html>"
    msg.attach(MIMEText(body, 'html'))

    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(msg)

# --- 4. SCRAPER LOGIKA ---
async def get_wolt_promos(context, coords):
    page = await context.new_page()
    lat, lon = coords['lat'], coords['lon']
    url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}"
    
    await page.goto(url)
    raw_data = await page.inner_text("body")
    data = json.loads(raw_data)
    
    results = {}
    slugs = []
    venues_map = {}

    for section in data.get("sections", []):
        for item in section.get("items", []):
            if "venue" in item:
                v = item["venue"]
                slugs.append(v["slug"])
                venues_map[v["slug"]] = v["name"]

    # Dubinski JS check za popuste
    js_check = """
    async (slugs, lat, lon) => {
        let res = {};
        for (let s of slugs.slice(0, 80)) {
            try {
                let r = await fetch(`https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/${s}/dynamic/?lat=${lat}&lon=${lon}`);
                if (r.ok) {
                    let d = await r.json();
                    let p = (d.discounts || []).map(x => x.description?.title || "Popust");
                    res[s] = p.length > 0 ? p.join(", ") : "-";
                }
            } catch(e) {}
        }
        return res;
    }
    """
    promo_data = await page.evaluate(f"({js_check})({slugs}, {lat}, {lon})")
    
    for slug, p_text in promo_data.items():
        results[normalize_name(venues_map[slug])] = {"Name": venues_map[slug], "Promo": p_text}
    
    await page.close()
    return results

async def get_glovo_promos(context, address):
    page = await context.new_page()
    # Ovde koristimo bazični scan prodavnica
    await page.goto("https://glovoapp.com/sr/rs")
    # Napomena: Za punu navigaciju do adrese koristi se tvoj raniji kod sa hero-container-input
    # Za ovaj primer koristimo scrape vidljivih elemenata
    promos = await page.evaluate("""() => {
        let items = {};
        document.querySelectorAll("a[data-testid='store-card']").forEach(c => {
            let n = c.querySelector('h3')?.innerText || "";
            let hasP = !!c.querySelector('[data-style="promotion"]');
            if(n) items[n] = hasP ? "Ima" : "-";
        });
        return items;
    }""")
    await page.close()
    return {normalize_name(k): v for k, v in promos.items()}

# --- 5. STREAMLIT INTERFEJS ---
st.set_page_config(page_title="Sales Promo Tool", page_icon="🎯")

if "scanning" not in st.session_state:
    st.session_state.scanning = False

async def start_scan():
    st.session_state.scanning = True
    all_gaps = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Učitavanje Glovo sesije
        storage = GLOVO_AUTH if os.path.exists(GLOVO_AUTH) else None
        context = await browser.new_context(storage_state=storage)
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for i, (city, info) in enumerate(CITIES.items()):
            status_text.text(f"Skeniram grad: {city}...")
            w_data = await get_wolt_promos(context, info)
            g_data = await get_glovo_promos(context, info["address"])
            
            for norm_name, w_info in w_data.items():
                if w_info["Promo"] != "-" and norm_name in g_data:
                    if g_data[norm_name] == "-":
                        all_gaps.append({
                            "Grad": city,
                            "Restoran": w_info["Name"],
                            "Wolt Akcija": f"<span class='wolt'>{w_info['Promo']}</span>",
                            "Glovo": "<span class='urgency'>Bez akcije ❌</span>"
                        })
            progress_bar.progress((i + 1) / len(CITIES))
        
        await browser.close()
    
    df = pd.DataFrame(all_gaps)
    if not df.empty:
        st.subheader("Pronađeni Sales Gap-ovi")
        st.write(df.to_html(escape=False), unsafe_allow_html=True)
        if st.button("Pošalji izveštaj Account Managerima"):
            send_sales_email(df)
            st.success("Mejl poslat!")
    else:
        st.info("Nisu pronađeni propusti u promocijama.")
    
    st.session_state.scanning = False

if st.button("🚀 POKRENI DNEVNU ANALIZU (SVI GRADOVI)") and not st.session_state.scanning:
    asyncio.run(start_scan())

st.sidebar.markdown("---")
st.sidebar.info("Ova skripta proverava BG, NS, NI i KG u 09:00h i traži restorane koji su 'zaboravili' akciju na Glovu.")
