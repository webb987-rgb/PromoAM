import asyncio
import datetime
import os
import re
import smtplib
import pandas as pd
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright

# ================= KONFIGURACIJA =================
EMAIL_SENDER = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo"
# Dodaj ovde prave mejlove tvojih Account Managera
RECIPIENTS = ["am_beograd@glovo.com", "am_nis@glovo.com"] 

GLOVO_AUTH = "glovo_auth.json" # Mora postojati u istom folderu
WOLT_AUTH = "wolt_auth.json"   # Opciono

CITIES = {
    "Beograd": {"lat": 44.8125, "lon": 20.4612, "address": "Makenzijeva 57, Beograd"},
    "Novi Sad": {"lat": 45.2551, "lon": 19.8452, "address": "Trg Slobode, Novi Sad"},
    "Niš": {"lat": 43.3209, "lon": 21.8954, "address": "Trg Kralja Milana, Niš"},
    "Kragujevac": {"lat": 44.0128, "lon": 20.9114, "address": "Centar, Kragujevac"}
}

# ================= POMOĆNE FUNKCIJE =================
def normalize_name(name):
    # Uklanja razmake i specijalne karaktere radi lakšeg poređenja
    return re.sub(r'[^\w]', '', str(name).lower())

def send_sales_email(df_gaps):
    if df_gaps.empty:
        print("Nema pronađenih promo propusta za danas.")
        return

    subject = f"🔴 SALES ALERT: Promo Gaps na Woltu ({datetime.date.today().strftime('%d.%m.%Y')})"
    
    # Kreiranje HTML tabele za mejl
    html_table = df_gaps.to_html(index=False, border=0, classes='promo-table')
    
    # Moderniji stil za mejl
    style = """
    <style>
        .promo-table { border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; }
        .promo-table td, .promo-table th { border: 1px solid #ddd; padding: 12px; }
        .promo-table th { background-color: #ffc244; color: black; text-align: left; }
        .promo-table tr:nth-child(even) { background-color: #f2f2f2; }
        .wolt-promo { color: #00c2e8; font-weight: bold; }
        .urgency { color: #e74c3c; font-weight: bold; }
    </style>
    """

    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = ", ".join(RECIPIENTS)
    msg['Subject'] = subject

    body = f"""
    <html>
    <head>{style}</head>
    <body>
        <h2>Izveštaj o propuštenim akcijama</h2>
        <p>Sledeći restorani imaju aktivne promocije na <b>Wolt-u</b>, dok su na <b>Glovo</b> platformi bez akcije:</p>
        {html_table}
        <br>
        <p><i>Ovaj izveštaj je generisan automatski. Proverite uslove pre kontaktiranja partnera.</i></p>
    </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print("Mejl uspešno poslat.")
    except Exception as e:
        print(f"Greška pri slanju mejla: {e}")

# ================= SCRAPERI =================

async def scrape_wolt_city(context, city_name, coords):
    """Munjevito izvlačenje Wolt promocija putem API-ja"""
    page = await context.new_page()
    lat, lon = coords['lat'], coords['lon']
    
    # 1. Fetch osnovnog feed-a
    feed_url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}"
    await page.goto(feed_url)
    content = await page.inner_text("body")
    
    import json
    data = json.loads(content)
    venues = []
    for section in data.get("sections", []):
        for item in section.get("items", []):
            if "venue" in item:
                venues.append(item["venue"])
    
    # 2. JS Requester za dubinsku provere akcija (iz tvog originalnog koda)
    promo_results = {}
    slugs = [v["slug"] for v in venues[:100]] # Prvih 100 restorana radi stabilnosti
    
    js_promo_check = """
    async (slugs, lat, lon) => {
        let results = {};
        for (let slug of slugs) {
            try {
                let url = `https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/${slug}/dynamic/?lat=${lat}&lon=${lon}`;
                let r = await fetch(url);
                if (r.ok) {
                    let d = await r.json();
                    let p = [];
                    (d.discounts || []).forEach(x => p.push(x.description?.title || "Popust"));
                    results[slug] = p.join(", ");
                }
            } catch(e) {}
        }
        return results;
    }
    """
    api_promos = await page.evaluate(f"({js_promo_check})({slugs}, {lat}, {lon})")
    
    final_data = {}
    for v in venues:
        name_norm = normalize_name(v["name"])
        promo_text = api_promos.get(v["slug"], "-")
        # Ako API nije vratio, proveri da li je bilo šta u bazičnom feed-u
        if promo_text == "-" and "Wolt+" in str(v): promo_text = "Wolt+ Exclusive"
        
        final_data[name_norm] = {"Name": v["name"], "Promo": promo_text}
    
    await page.close()
    return final_data

async def scrape_glovo_city(context, address):
    """Izvlačenje Glovo promocija koristeći login sesiju"""
    page = await context.new_page()
    try:
        await page.goto("https://glovoapp.com/sr/rs", timeout=30000)
        # Navigacija do adrese (tvoj originalni metod)
        # ... (Ovde ide tvoj kod za unos adrese i smart scroll)
        
        # Pojednostavljena logika za potrebe sales izveštaja
        # Vraćamo rečnik: { "restoran_norm": "Ima/Nema" }
        promos = await page.evaluate("""() => {
            let res = {};
            document.querySelectorAll("a[data-testid='store-card']").forEach(card => {
                let name = card.querySelector('h3')?.innerText || "";
                let hasPromo = !!card.querySelector('[data-style="promotion"]');
                if(name) res[name] = hasPromo ? "Ima" : "-";
            });
            return res;
        }""")
        
        final_glovo = {normalize_name(k): v for k, v in promos.items()}
        return final_glovo
    except:
        return {}
    finally:
        await page.close()

# ================= GLAVNI PROCES =================

async def main():
    async with async_playwright() as p:
        # Pokrećemo browser sa tvojim podešavanjima
        browser = await p.chromium.launch(headless=True)
        
        # Učitavamo tvoje sesije (auth fajlove)
        glovo_context = await browser.new_context(storage_state=GLOVO_AUTH if os.path.exists(GLOVO_AUTH) else None)
        wolt_context = await browser.new_context(storage_state=WOLT_AUTH if os.path.exists(WOLT_AUTH) else None)
        
        all_gaps = []

        for city, info in CITIES.items():
            print(f"🔄 Proveravam {city}...")
            
            wolt_res = await scrape_wolt_city(wolt_context, city, info)
            glovo_res = await scrape_glovo_city(glovo_context, info["address"])
            
            # Poređenje podataka
            for norm_name, w_info in wolt_res.items():
                if w_info["Promo"] != "-" and norm_name in glovo_res:
                    if glovo_res[norm_name] == "-":
                        all_gaps.append({
                            "Grad": city,
                            "Restoran": w_info["Name"],
                            "Wolt Akcija": f"<span class='wolt-promo'>{w_info['Promo']}</span>",
                            "Glovo Status": "<span class='urgency'>NEMA AKCIJU</span>"
                        })
        
        # Slanje rezultata
        if all_gaps:
            df = pd.DataFrame(all_gaps)
            send_sales_email(df)
        else:
            print("Nema propusta u promocijama.")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())