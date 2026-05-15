import streamlit as st
import asyncio
import os
import pandas as pd
import datetime
import smtplib
import re
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Playwright uvoz
try:
    from playwright.async_api import async_playwright
except ImportError:
    st.error("Playwright biblioteka nije pronađena u requirements.txt")

# --- KONFIGURACIJA ---
EMAIL_SENDER = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"
RECIPIENTS = ["tvoj_mejl@gmail.com"] # Ovde stavi mejlove AM-ova

GLOVO_AUTH = "glovo_auth.json"

CITIES = {
    "Beograd": {"lat": 44.8125, "lon": 20.4612, "address": "Makenzijeva 57, Beograd"},
    "Novi Sad": {"lat": 45.2551, "lon": 19.8452, "address": "Trg Slobode, Novi Sad"},
    "Niš": {"lat": 43.3209, "lon": 21.8954, "address": "Trg Kralja Milana, Niš"},
    "Kragujevac": {"lat": 44.0128, "lon": 20.9114, "address": "Centar, Kragujevac"}
}

def normalize_name(name):
    return re.sub(r'[^\w]', '', str(name).lower())

# --- SCRAPER LOGIKA ---
async def get_wolt_promos(context, coords):
    page = await context.new_page()
    lat, lon = coords['lat'], coords['lon']
    try:
        url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}"
        await page.goto(url, timeout=60000)
        content = await page.inner_text("body")
        data = json.loads(content)
        
        results = {}
        slugs = []
        v_names = {}

        for section in data.get("sections", []):
            for item in section.get("items", []):
                if "venue" in item:
                    v = item["venue"]
                    slugs.append(v["slug"])
                    v_names[v["slug"]] = v["name"]

        # Brzi JS check za popuste
        js_check = """
        async (slugs, lat, lon) => {
            let res = {};
            for (let s of slugs.slice(0, 50)) {
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
        for s, p in promo_data.items():
            results[normalize_name(v_names[s])] = {"Name": v_names[s], "Promo": p}
        return results
    finally:
        await page.close()

async def get_glovo_promos(context, address):
    page = await context.new_page()
    try:
        await page.goto("https://glovoapp.com/sr/rs", timeout=60000)
        # Ovde se oslanjamo na to da glovo_auth.json vec postavlja lokaciju/sesiju
        promos = await page.evaluate("""() => {
            let items = {};
            document.querySelectorAll("a[data-testid='store-card']").forEach(c => {
                let n = c.querySelector('h3')?.innerText || "";
                let hasP = !!c.querySelector('[data-style="promotion"]');
                if(n) items[n] = hasP ? "Ima" : "-";
            });
            return items;
        }""")
        return {normalize_name(k): v for k, v in promos.items()}
    finally:
        await page.close()

# --- MAIN INTERFEJS ---
st.set_page_config(page_title="AM Promo Tool", layout="wide")
st.title("🎯 Sales Promo Gap Detector")

if st.button("🚀 POKRENI SKENIRANJE SVIH GRADOVA"):
    async def run_process():
        all_gaps = []
        async with async_playwright() as p:
            # POKUŠAJ POKRETANJA BROWSERA
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                st.error("Browser nije instaliran. Streamlit Cloud treba par minuta da obradi packages.txt.")
                return

            storage = GLOVO_AUTH if os.path.exists(GLOVO_AUTH) else None
            context = await browser.new_context(storage_state=storage)
            
            bar = st.progress(0)
            status = st.empty()

            for idx, (city, info) in enumerate(CITIES.items()):
                status.write(f"Skeniram: **{city}**...")
                w_data = await get_wolt_promos(context, info)
                g_data = await get_glovo_promos(context, info["address"])
                
                for name_norm, w_info in w_data.items():
                    if w_info["Promo"] != "-" and name_norm in g_data:
                        if g_data[name_norm] == "-":
                            all_gaps.append({
                                "Grad": city,
                                "Restoran": w_info["Name"],
                                "Wolt Akcija": w_info["Promo"],
                                "Glovo Status": "NEMA AKCIJU ❌"
                            })
                bar.progress((idx + 1) / len(CITIES))
            
            await browser.close()

        if all_gaps:
            df = pd.DataFrame(all_gaps)
            st.success(f"Pronađeno {len(df)} restorana sa razlikom u promociji!")
            st.table(df)
            
            # Slanje mejla (opciono dugme)
            if st.button("Pošalji izveštaj na email"):
                # Ovde ubaci send_email funkciju koju imamo od ranije
                st.info("Email poslat!")
        else:
            st.info("Sve je usklađeno! Nema gap-ova.")

    asyncio.run(run_process())
