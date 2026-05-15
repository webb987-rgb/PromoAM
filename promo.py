import asyncio
import datetime
import os
import platform
import subprocess
import re
import threading
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import plotly.express as px          
import plotly.graph_objects as go    
import random
import urllib.parse
from io import BytesIO
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import time
import streamlit as st
import sys

try:
    import schedule
except ImportError:
    schedule = None

# TIMEZONE SETUP
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Belgrade")
except Exception:
    LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=2))

def local_time():
    return datetime.datetime.now(LOCAL_TZ)

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Streamlit Page Config
st.set_page_config(page_title="Delivery Monitor", page_icon="🍔", layout="wide")

# ================= MODERN CSS DESIGN =================
st.markdown("""
<style>
    /* Live Counters Style */
    .live-card {
        display: flex; gap: 20px; background: #f8f9fa; padding: 15px; border-radius: 12px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.05);
    }
    .wolt-card {
        flex: 1; text-align: center; background: white; padding: 15px; border-radius: 10px; border-left: 6px solid #00c2e8; box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    .glovo-card {
        flex: 1; text-align: center; background: white; padding: 15px; border-radius: 10px; border-left: 6px solid #ffc244; box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    .metric-value { font-size: 32px; font-weight: bold; margin: 0; }
    .metric-title { font-size: 14px; color: #666; margin: 0; text-transform: uppercase; letter-spacing: 1px;}
    
    /* Modern Dashboard KPI blocks */
    .kpi-wrapper { display: flex; gap: 15px; margin-bottom: 20px; flex-wrap: wrap; }
    .kpi-card { 
        flex: 1; background: #ffffff; padding: 20px; border-radius: 12px; 
        box-shadow: 0 4px 15px rgba(0,0,0,0.04); border: 1px solid #f0f2f6; 
        text-align: center; transition: transform 0.2s, box-shadow 0.2s;
    }
    .kpi-card:hover { transform: translateY(-5px); box-shadow: 0 8px 25px rgba(0,0,0,0.1); }
    .kpi-title { font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 5px; font-weight: 700;}
    .kpi-value { font-size: 36px; font-weight: 800; color: #2c3e50; margin: 0; line-height: 1.1;}
    .kpi-wolt { border-bottom: 4px solid #00c2e8; }
    .kpi-glovo { border-bottom: 4px solid #ffc244; }

    /* Tabs Style */
    .stTabs [data-baseweb="tab-list"] { gap: 10px; }
    .stTabs [data-baseweb="tab"] { height: 50px; white-space: pre-wrap; background-color: #f0f2f6; border-radius: 8px 8px 0px 0px; padding: 10px 20px; color: #4f4f4f;}
    .stTabs [aria-selected="true"] { background-color: #e0e5ec !important; font-weight: bold; border-bottom: 3px solid #ff4b4b;}
</style>
""", unsafe_allow_html=True)
# ======================================================

# ================= WINDOWS & PLAYWRIGHT FIX =================
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

@st.cache_resource
def install_playwright():
    import os
    os.system("playwright install chromium")

install_playwright()

# ================= GLOBAL SETTINGS =================
EMAIL_SENDER = "webb987@gmail.com"
EMAIL_PASSWORD = "sdehqzbnqefjlomo" 

OUTPUT_DIR = Path.cwd() / "reports"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = OUTPUT_DIR / "delivery_history.csv"

ERRORS_DIR = Path.cwd() / "errors"
ERRORS_DIR.mkdir(parents=True, exist_ok=True)

# ── AMM BAZA i ALERT LOG ──────────────────────────────────────────────────────
AMM_FILE   = Path.cwd() / "amm_baza.csv"          # ko je AM za koji restoran
ALERT_FILE = Path.cwd() / "alert_log.csv"          # log poslatih mailova

# Gradovi koji se automatski skeniraju u 09:00
AUTO_CITIES = ["Beograd", "Novi Sad", "Niš", "Kragujevac"]

# ── AMM CSV helpers ───────────────────────────────────────────────────────────
AMM_COLUMNS = ["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]

def load_amm() -> pd.DataFrame:
    if AMM_FILE.exists():
        df = pd.read_csv(AMM_FILE)
        for c in AMM_COLUMNS:
            if c not in df.columns: df[c] = ""
        return df
    return pd.DataFrame(columns=AMM_COLUMNS)

def save_amm(df: pd.DataFrame):
    df.to_csv(AMM_FILE, index=False)

# ── ALERT LOG helpers ─────────────────────────────────────────────────────────
ALERT_COLUMNS = ["timestamp", "city", "restaurant_display", "am_name", "am_email",
                 "wolt_promo", "glovo_promo", "alert_type"]

def load_alert_log() -> pd.DataFrame:
    if ALERT_FILE.exists():
        df = pd.read_csv(ALERT_FILE)
        for c in ALERT_COLUMNS:
            if c not in df.columns: df[c] = ""
        return df
    return pd.DataFrame(columns=ALERT_COLUMNS)

def append_alert_log(rows: list):
    """Dodaje listu diktova u alert_log.csv"""
    df_new = pd.DataFrame(rows, columns=ALERT_COLUMNS)
    if ALERT_FILE.exists():
        df_existing = pd.read_csv(ALERT_FILE)
        pd.concat([df_existing, df_new], ignore_index=True).to_csv(ALERT_FILE, index=False)
    else:
        df_new.to_csv(ALERT_FILE, index=False)

GLOVO_AUTH_FILE = "glovo_auth.json"
WOLT_AUTH_FILE = "wolt_auth.json"

def timestamp(): return local_time().strftime("%Y%m%d_%H%M%S")
def format_time_short(): return local_time().strftime("%H:%M")
def log_msg(msg, placeholder=None):
    print(msg)
    if placeholder: placeholder.text(msg)

# ---------------- LIVE COUNTER UI ----------------
def refresh_live_ui(ph, wolt_count, glovo_count, address, custom_text=None):
    txt = custom_text if custom_text else f"📍 Currently scanning: <b>{address}</b>"
    html = f"""
    <div class="live-card">
        <div class="wolt-card">
            <p class="metric-title">🚲 Wolt</p>
            <p class="metric-value" style="color: #00c2e8;">{wolt_count}</p>
        </div>
        <div class="glovo-card">
            <p class="metric-title">🍔 Glovo</p>
            <p class="metric-value" style="color: #ffc244;">{glovo_count}</p>
        </div>
    </div>
    <p style="text-align: center; color: #666; font-size: 14px;">{txt}</p>
    """
    ph.markdown(html, unsafe_allow_html=True)

# ---------------- CYRILLIC & EMAIL SUPPORT ----------------
def cyrillic_to_latin(text):
    if not text: return ""
    mapa = { 'а':'a', 'б':'b', 'в':'v', 'г':'g', 'д':'d', 'ђ':'dj', 'е':'e', 'ж':'z', 'з':'z', 'и':'i', 'ј':'j', 'к':'k', 'л':'l', 'љ':'lj', 'м':'m', 'н':'n', 'њ':'nj', 'о':'o', 'п':'p', 'р':'r', 'с':'s', 'т':'t', 'ћ':'c', 'у':'u', 'ф':'f', 'х':'h', 'ц':'c', 'ч':'c', 'џ':'dz', 'ш':'s', 'А':'A', 'Б':'B', 'В':'V', 'Г':'G', 'Д':'D', 'Ђ':'Dj', 'Е':'E', 'Ж':'Z', 'З':'Z', 'И':'I', 'Ј':'J', 'К':'K', 'Л':'L', 'Љ':'Lj', 'М':'M', 'Н':'N', 'Њ':'Nj', 'О':'O', 'П':'P', 'Р':'R', 'С':'S', 'Т':'T', 'Ћ':'C', 'У':'U', 'Ф':'F', 'Х':'H', 'Ц':'C', 'Ч':'C', 'Џ':'Dz', 'Ш':'S' }
    for k, v in mapa.items(): text = text.replace(k, v)
    return text

def send_email(pdf_paths, recipients_str, log_ph=None):
    recipients_list = [e.strip() for e in recipients_str.split(",") if e.strip()]
    if not recipients_list: return
    try:
        log_msg(f"[SYSTEM] Sending email to: {', '.join(recipients_list)}...", log_ph)
        for recipient in recipients_list:
            msg = MIMEMultipart()
            msg['From'] = EMAIL_SENDER
            msg['To'] = recipient
            msg['Subject'] = f"Delivery Reports - {local_time().strftime('%d.%m. u %H:%M')}"
            body = "Hello,\n\nAttached are the summary and detailed restaurant status reports.\n\nThe system has successfully completed the cycle."
            msg.attach(MIMEText(body, 'plain'))
            for pdf_path in pdf_paths:
                with open(pdf_path, "rb") as attachment:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(attachment.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f"attachment; filename= {os.path.basename(pdf_path)}")
                    msg.attach(part)
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipient, msg.as_string())
            server.quit()
        log_msg("[SUCCESS] All emails sent!", log_ph)
    except Exception as e: log_msg(f"[ERROR] Email failed: {e}", log_ph)

# ── PROMO ALERT: šalje mail AM-u ako restoran ima promo na Woltu a ne na Glovu ──
def send_promo_alert(am_email: str, am_name: str, alerts: list, log_ph=None):
    """
    alerts = lista diktova: {restaurant_display, city, wolt_promo, glovo_promo}
    """
    if not alerts or not am_email.strip():
        return
    try:
        rows_html = ""
        for a in alerts:
            rows_html += f"""
            <tr>
              <td style='padding:8px 12px; border-bottom:1px solid #eee;'><b>{a['restaurant_display']}</b></td>
              <td style='padding:8px 12px; border-bottom:1px solid #eee; color:#555;'>{a['city']}</td>
              <td style='padding:8px 12px; border-bottom:1px solid #eee; color:#00c2e8;'>{a['wolt_promo'].replace(chr(10),'<br>')}</td>
              <td style='padding:8px 12px; border-bottom:1px solid #eee; color:#e74c3c;'>Nema promocije</td>
            </tr>"""

        body_html = f"""
        <html><body style='font-family:Arial,sans-serif; color:#222;'>
        <h2 style='color:#2c3e50;'>🔔 Promo Alert – Wolt vs Glovo</h2>
        <p>Pozdrav <b>{am_name}</b>,</p>
        <p>Sledeći restorani imaju <b>aktivnu promociju na Wolt-u</b>, ali <b>nemaju promociju na Glovu</b>:</p>
        <table style='border-collapse:collapse; width:100%; font-size:14px;'>
          <thead>
            <tr style='background:#f0f2f6;'>
              <th style='padding:10px 12px; text-align:left;'>Restoran</th>
              <th style='padding:10px 12px; text-align:left;'>Grad</th>
              <th style='padding:10px 12px; text-align:left;'>Wolt promo</th>
              <th style='padding:10px 12px; text-align:left;'>Glovo promo</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        <p style='margin-top:20px; font-size:12px; color:#888;'>
          Automatski alert – Delivery Monitor &bull; {local_time().strftime('%d.%m.%Y u %H:%M')}
        </p>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg['From']    = EMAIL_SENDER
        msg['To']      = am_email
        msg['Subject'] = f"⚠️ Promo Alert: {len(alerts)} restoran(a) ima promo na Woltu bez Glova – {local_time().strftime('%d.%m.%Y')}"
        msg.attach(MIMEText(body_html, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, am_email, msg.as_string())
        server.quit()
        log_msg(f"[PROMO ALERT] Mail poslat na {am_email} ({len(alerts)} restorana)", log_ph)
    except Exception as e:
        log_msg(f"[PROMO ALERT ERROR] {e}", log_ph)


def run_promo_comparison_and_alert(df_scan: pd.DataFrame, log_ph=None) -> list:
    """
    Prima scaniran DataFrame, učitava AMM bazu, pronalazi nepodudaranja
    i šalje mailove. Vraća listu poslatih alerta (za prikaz u UI-ju).
    """
    amm = load_amm()
    if amm.empty or df_scan.empty:
        log_msg("[PROMO ALERT] Baza AMM-ova je prazna ili nema scan podataka.", log_ph)
        return []

    sent_alerts = []

    # Grupisanje po gradu, pa po normalizovanom imenu
    df_scan["_norm"] = df_scan["Name"].apply(normalize_name)
    cities = df_scan["Address"].unique()

    # AM → lista alerta (da šaljemo jedan mail po AM-u)
    am_alerts: dict = {}

    for city in cities:
        df_city = df_scan[df_scan["Address"] == city]
        wolt_df  = df_city[df_city["Platform"] == "Wolt"]
        glovo_df = df_city[df_city["Platform"] == "Glovo"]

        glovo_norms = set(glovo_df["_norm"].tolist())
        glovo_promo_norms = set(
            glovo_df[glovo_df["Promo"] != "-"]["_norm"].tolist()
        )

        for _, wrow in wolt_df.iterrows():
            norm = wrow["_norm"]
            wolt_promo = wrow.get("Promo", "-")
            if not wolt_promo or wolt_promo == "-":
                continue  # Wolt nema promo, preskačemo

            # Je li restoran uopšte na Glovu?
            if norm not in glovo_norms:
                alert_type = "wolt_only"  # restoran uopšte nije na Glovu
            elif norm not in glovo_promo_norms:
                alert_type = "wolt_promo_glovo_no_promo"
            else:
                continue  # ima promo i na Glovu, OK

            # Pronađi AM iz baze
            amm_match = amm[amm["restaurant_norm"] == norm]
            if amm_match.empty:
                # Pokušaj fuzzy po city
                amm_match = amm[(amm["restaurant_norm"] == norm) | 
                                (amm["restaurant_display"].apply(normalize_name) == norm)]
            if amm_match.empty:
                continue  # restoran nije u AMM bazi

            row_am = amm_match.iloc[0]
            am_email = str(row_am["am_email"]).strip()
            am_name  = str(row_am["am_name"]).strip()
            if not am_email:
                continue

            alert_item = {
                "restaurant_display": wrow["Name"],
                "city": city,
                "wolt_promo": wolt_promo,
                "glovo_promo": "-" if alert_type == "wolt_only" else "Nema",
                "alert_type": alert_type,
                "am_name": am_name,
                "am_email": am_email,
            }

            am_alerts.setdefault(am_email, {"am_name": am_name, "items": []})
            am_alerts[am_email]["items"].append(alert_item)

    log_rows = []
    ts = local_time().strftime("%Y-%m-%d %H:%M:%S")
    for am_email, payload in am_alerts.items():
        send_promo_alert(am_email, payload["am_name"], payload["items"], log_ph)
        for item in payload["items"]:
            log_rows.append({
                "timestamp": ts,
                "city": item["city"],
                "restaurant_display": item["restaurant_display"],
                "am_name": payload["am_name"],
                "am_email": am_email,
                "wolt_promo": item["wolt_promo"],
                "glovo_promo": item["glovo_promo"],
                "alert_type": item["alert_type"],
            })
            sent_alerts.append(item)

    if log_rows:
        append_alert_log(log_rows)

    if not sent_alerts:
        log_msg("[PROMO ALERT] Nema novih nepodudaranja.", log_ph)
    return sent_alerts


# ── AUTOMATSKI DNEVNI SCAN U 09:00 ───────────────────────────────────────────
_scheduler_started = False

def _auto_daily_job():
    """Pokreće scan + alert u pozadini, bez Streamlit konteksta."""
    import asyncio as _asyncio

    class _FakePH:
        def text(self, msg): print(f"[AUTO-SCAN] {msg}")

    ph = _FakePH()
    log_msg("[AUTO-SCAN] Pokrećem dnevni scan u 09:00...", ph)

    addresses = [cyrillic_to_latin(c) for c in AUTO_CITIES]

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    try:
        df_s, _, _, _ = loop.run_until_complete(
            scan_process(addresses, ph, None, {"Wolt": 0, "Glovo": 0},
                         generate_pdf=False, recipient_email="", debug_mode=False)
        )
        if not df_s.empty:
            df_s.to_csv(OUTPUT_DIR / f"Detaljno_{timestamp()}.csv", index=False)
            run_promo_comparison_and_alert(df_s, ph)
            log_msg("[AUTO-SCAN] Završen. Alertovi poslati.", ph)
    except Exception as e:
        log_msg(f"[AUTO-SCAN ERROR] {e}", ph)
    finally:
        loop.close()

def start_scheduler():
    global _scheduler_started
    if _scheduler_started or schedule is None:
        return
    _scheduler_started = True
    if schedule:
        schedule.every().day.at("09:00").do(_auto_daily_job)
    def _run():
        while True:
            if schedule: schedule.run_pending()
            time.sleep(30)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("[SCHEDULER] Pokrenut. Dnevni scan u 09:00.")

start_scheduler()

# ---------------- HISTORY ----------------
def save_to_history(df):
    time_now = format_time_short()
    date_now = local_time().strftime("%Y-%m-%d")
    history_data = []
    addresses = df["Address"].unique()
    for adr in addresses:
        for plat in ["Wolt", "Glovo"]:
            sub = df[(df["Address"] == adr) & (df["Platform"] == plat)]
            if sub.empty: continue
            
            opened = len(sub[sub["Status"] == "Open"])
            closed = len(sub[sub["Status"] == "Closed"])
            
            avg_time = sub["Time_Num"].dropna().mean()
            avg_time = 0 if pd.isna(avg_time) else round(avg_time, 1)
            
            promo_count = len(sub[sub["Promo"] != "-"])
            
            history_data.append({ 
                "Date": date_now, "Time": time_now, 
                "Address": adr, "Platform": plat, 
                "Open": opened, "Closed": closed,
                "Avg_Time": avg_time, "Promo_Count": promo_count
            })
            
    df_new = pd.DataFrame(history_data)
    file_str = str(HISTORY_FILE)
    if os.path.exists(file_str):
        df_combined = pd.concat([pd.read_csv(file_str), df_new], ignore_index=True)
    else: df_combined = df_new
    try: df_combined.to_csv(file_str, index=False)
    except: pass
    st.session_state.df_history = df_combined
    return df_combined

# ================= MODERN UI CHARTS (PLOTLY) =================
def create_status_chart_ui(df_sub, title):
    wolt_o = len(df_sub[(df_sub["Platform"] == "Wolt") & (df_sub["Status"] == "Open")])
    wolt_z = len(df_sub[(df_sub["Platform"] == "Wolt") & (df_sub["Status"] == "Closed")])
    glovo_o = len(df_sub[(df_sub["Platform"] == "Glovo") & (df_sub["Status"] == "Open")])
    glovo_z = len(df_sub[(df_sub["Platform"] == "Glovo") & (df_sub["Status"] == "Closed")])
    
    data = [
        {"Category": "Total", "Platform": "Wolt", "Count": wolt_o+wolt_z},
        {"Category": "Open", "Platform": "Wolt", "Count": wolt_o},
        {"Category": "Closed", "Platform": "Wolt", "Count": wolt_z},
        {"Category": "Total", "Platform": "Glovo", "Count": glovo_o+glovo_z},
        {"Category": "Open", "Platform": "Glovo", "Count": glovo_o},
        {"Category": "Closed", "Platform": "Glovo", "Count": glovo_z},
    ]
    fig = px.bar(data, x="Category", y="Count", color="Platform", barmode="group",
                 color_discrete_map={"Wolt": "#00c2e8", "Glovo": "#ffc244"}, text="Count", title=title)
    fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", title_font_size=18)
    fig.update_traces(textposition='outside', textfont_size=14, textfont_weight="bold")
    return fig

def create_delivery_time_chart_ui(df_sub, title):
    wolt_df = df_sub[(df_sub["Platform"] == "Wolt") & (df_sub["Time_Num"].notna())]
    glovo_df = df_sub[(df_sub["Platform"] == "Glovo") & (df_sub["Time_Num"].notna())]
    w_avg = wolt_df["Time_Num"].dropna().mean() if not wolt_df["Time_Num"].dropna().empty else 0
    w_avg = 0 if pd.isna(w_avg) else round(w_avg, 1)
    g_avg = glovo_df["Time_Num"].dropna().mean() if not glovo_df["Time_Num"].dropna().empty else 0
    g_avg = 0 if pd.isna(g_avg) else round(g_avg, 1)
    
    data = [{"Platform": "Wolt", "Time": w_avg}, {"Platform": "Glovo", "Time": g_avg}]
    fig = px.bar(data, x="Platform", y="Time", color="Platform", 
                 color_discrete_map={"Wolt": "#00c2e8", "Glovo": "#ffc244"}, text="Time", title=title)
    fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", yaxis_title="Average time (min)", title_font_size=18)
    fig.update_traces(texttemplate='%{text} min', textposition='outside', textfont_size=14, textfont_weight="bold")
    return fig

def create_promo_chart_ui(df_sub, selected_promos, title):
    wolt_count = 0
    glovo_count = 0
    if selected_promos:
        for _, row in df_sub.iterrows():
            restaurant_promos = []
            if pd.notna(row['Promo']) and row['Promo'] != "-":
                restaurant_promos = [a.replace("• ", "").strip() for a in str(row['Promo']).split('\n') if a.strip()]
            if any(promo in selected_promos for promo in restaurant_promos):
                if row['Platform'] == 'Wolt': wolt_count += 1
                elif row['Platform'] == 'Glovo': glovo_count += 1
                
    data = [{"Platform": "Wolt", "Count": wolt_count}, {"Platform": "Glovo", "Count": glovo_count}]
    fig = px.bar(data, x="Platform", y="Count", color="Platform", 
                 color_discrete_map={"Wolt": "#00c2e8", "Glovo": "#ffc244"}, text="Count", title=title)
    fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", yaxis_title="Number of restaurants", title_font_size=18)
    fig.update_traces(textposition='outside', textfont_size=14, textfont_weight="bold")
    return fig

def create_timeline_chart_ui(df_hist, address=None, custom_title=None, metric="Open", ylabel="Number of open restaurants"):
    df_sub = df_hist.copy()
    if metric not in df_sub.columns: df_sub[metric] = 0

    if address:
        df_sub = df_sub[df_sub["Address"] == address]
        title = custom_title if custom_title else f'History - {address.upper()}'
    else:
        if not df_sub.empty and 'Platform' in df_sub.columns:
            if 'Date' in df_sub.columns and 'Time' in df_sub.columns:
                if metric == "Avg_Time": df_sub = df_sub.groupby(["Date", "Time", "Platform"])[metric].mean().reset_index()
                else: df_sub = df_sub.groupby(["Date", "Time", "Platform"])[metric].sum().reset_index()
        title = custom_title if custom_title else 'Summary History'
        
    if len(df_sub) == 0: return go.Figure().update_layout(title="No history data", plot_bgcolor="rgba(0,0,0,0)")

    df_sub["Real_Datetime"] = pd.to_datetime(df_sub["Date"] + " " + df_sub["Time"])
    df_sub = df_sub.sort_values(by="Real_Datetime")
    
    fig = px.line(df_sub, x="Real_Datetime", y=metric, color="Platform", markers=True,
                  color_discrete_map={"Wolt": "#00c2e8", "Glovo": "#ffc244"}, title=title)
    fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", 
                      xaxis_title="", yaxis_title=ylabel, hovermode="x unified", title_font_size=18)
    fig.update_xaxes(tickformat="%d.%m. u %H:%M") 
    fig.update_traces(line=dict(width=3), marker=dict(size=8))
    return fig

# ---------------- DATA EXTRACTION ----------------
def remove_accents(text):
    if not text: return ""
    for k, v in {'č':'c', 'ć':'c', 'ž':'z', 'š':'s', 'đ':'dj', 'Č':'C', 'Ć':'C', 'Ž':'Z', 'Š':'S', 'Đ':'Dj'}.items(): text = text.replace(k, v)
    return text

def extract_name(text):
    if not text: return ""
    for line in str(text).split('\n'):
        line = line.strip()
        if not line or '%' in line or ("min" in line.lower() and re.search(r'\d+', line.lower())): continue
        if any(x in line.lower() for x in ["rsd", "din", "promo", "novo", "new", "odlično", "besplatna dostava", "artikli", "narudžb", "popust", "off", "discount"]): continue
        if len(line) >= 2: return line
    return ""

def analyze_status(text):
    t = text.lower()
    if any(x in t for x in ["uskoro se zatvara", "closing soon", "zatvara se za", "closes in"]): return "Open"
    if any(k in t for k in ["samo preuzimanje", "samo za preuzimanje", "pickup only", "dostava nije dostupna", "dostava trenutno nije", "samo licno preuzimanje", "zatvoreno", "zakažite", "zakaži", "zakazi", "nedostupno", "otvara se", "otvara", "closed", "schedule"]): return "Closed"
    return "Open"

def extract_rating(text, plat):
    try:
        clean_text = re.sub(r'<[^>]+>', ' ', str(text)).lower()
        if plat == "Glovo":
            for p in re.findall(r'(\d{1,3})\s*%', clean_text):
                if int(p) >= 60: return p + "%"
        elif plat == "Wolt":
            m = re.search(r'\b([5-9][.,][0-9]|10[.,]0)\b', clean_text)
            if m: return m.group(1).replace(',', '.')
    except: pass
    return "-"

def extract_delivery_time(text):
    try:
        clean = re.sub(r'<[^>]+>', ' ', str(text)).lower()
        m1 = re.search(r'(\d{1,3})\s*[-–]\s*(\d{1,3})\s*(?:min|m|\')', clean)
        if m1 and int(m1.group(1)) < 120 and int(m1.group(2)) < 120: return f"{m1.group(1)}-{m1.group(2)} min", (int(m1.group(1)) + int(m1.group(2))) / 2.0
        m2 = re.search(r'\b(\d{1,3})\s*(?:min|m|\')', clean)
        if m2 and int(m2.group(1)) < 120: return f"{m2.group(1)} min", float(m2.group(1))
    except: pass
    return "-", np.nan

# ================= REKURZIVNO ČUPANJE JSON TEKSTOVA I PAMETNI PROMO EXTRACT =================
def get_all_json_strings(obj):
    """Prolazi kroz ceo JSON rekurzivno i skuplja SVE tekstove (zanemaruje gole brojeve)"""
    if isinstance(obj, dict):
        return " ".join(get_all_json_strings(v) for v in obj.values() if v is not None)
    elif isinstance(obj, list):
        return " ".join(get_all_json_strings(i) for i in obj if i is not None)
    elif isinstance(obj, str):
        return obj
    return ""

def extract_promo(text, html_content, plat):
    clean_text = (str(text) + " \n " + str(html_content)).lower()
    clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
    
    # Samo izbacujemo tacke i zareze izmedju cifara (npr 1.000 -> 1000) da bi regex lakse uhvatio prave cene
    clean_numbers = re.sub(r'(?<=\d)[.,](?=\d)', '', clean_text)
    
    promos, seen, res = [], set(), []

    # GLOVO HTML TAGOVI
    if plat == "Glovo" and html_content:
        glovo_tags = re.findall(r'data-style="promotion"[^>]*>([^<]+)<', str(html_content))
        for gp in glovo_tags:
            promos.append(gp.strip())
    
    # FREE DELIVERY
    if any(x in clean_text for x in ["besplatna dostava", "free delivery", "dostava 0", "0 rsd dostava", "delivery 0", "besplatna"]):
        promos.append("Free delivery")
        
    # 1+1 FREE
    if any(x in clean_text for x in ["1+1", "1 + 1", "buy 1 get 1"]):
        promos.append("1+1 Free")
        
    if plat == "Wolt":
        # ODVOJENI FILTER 1: Procenat popusta (npr. "20% discount on selected items")
        for pm in re.findall(r'(\d{1,3}\s*%)', clean_text):
            promos.append(f"{pm.strip()} discount")
            
        # ODVOJENI FILTER 2: Fiksni RSD/DIN popusti
        # Strogo mora da sadrzi rec off, popust, discount ili ustedi - da bi izbegli slucajno vadjenje 50000 sa API-ja
        rsd_patterns = [
            r'(?:rsd|din)\s*(\d{2,5})\s*(?:off|popust|discount)',
            r'(\d{2,5})\s*(?:rsd|din)\s*(?:off|popust|discount)',
            r'(?:uštedi|save|popust)\s*(\d{2,5})\s*(?:rsd|din)'
        ]
        for pat in rsd_patterns:
            for match in re.findall(pat, clean_numbers):
                if int(match) > 10: promos.append(f"{match} RSD discount")
                
    else:
        # Glovo logic
        for pm in re.findall(r'(\d{1,2}\s*%)\s*(?:popust|off|discount|-)', clean_text):
            promos.append(f"{pm.strip()} discount")
        for rm in re.findall(r'(\d{2,5})\s*(?:rsd|din)', clean_numbers):
            if int(rm) > 10: promos.append(f"{rm} RSD discount")
            
    if "wolt+" in clean_text: promos.append("Wolt+")
    if "prime" in clean_text: promos.append("Prime")
        
    for a in promos:
        ac = a[0].upper() + a[1:]
        if ac not in seen:
            seen.add(ac)
            res.append(f"• {ac}")
            
    return "\n".join(res) if res else "-"

def normalize_name(name): return re.sub(r'[^\w]', '', str(name).lower())

def extract_wolt_discounts_from_api(data):
    """
    Direktno čita 'discounts' array iz Wolt dynamic API odgovora i vraća
    listu svih akcija kao čitljive stringove (naslov + opis).
    Ovo je preciznije od regex-a jer čita tačne naslove koje Wolt šalje.
    """
    res = []
    seen = set()

    try:
        # discounts mogu biti na top nivou ili unutar venue_raw
        sources = []
        if isinstance(data, dict):
            if "discounts" in data:
                sources.append(data["discounts"])
            venue_raw = data.get("venue_raw", {})
            if isinstance(venue_raw, dict) and "discounts" in venue_raw:
                sources.append(venue_raw["discounts"])

        for discount_list in sources:
            if not isinstance(discount_list, list):
                continue
            for disc in discount_list:
                if not isinstance(disc, dict):
                    continue

                # Pokušavamo da nađemo naslov – može biti na više mesta
                title = ""
                # 1. description.title (standardno)
                desc = disc.get("description", {})
                if isinstance(desc, dict):
                    title = desc.get("title", "") or ""
                # 2. banner.formatted_text ako nema description.title
                if not title:
                    banner = disc.get("banner", {})
                    if isinstance(banner, dict):
                        title = banner.get("formatted_text", "") or ""
                # 3. condition_item_badge.text kao fallback
                if not title:
                    cib = disc.get("condition_item_badge", {})
                    if isinstance(cib, dict):
                        title = cib.get("text", "") or ""

                title = str(title).strip()
                if not title or title.lower() in seen:
                    continue

                # Preskačemo interne Wolt+ / Prime kampanje koje nisu prave akcije
                lower_title = title.lower()
                if any(x in lower_title for x in ["wolt+", "prime", "loyalty"]):
                    res.append(f"• Wolt+")
                    seen.add("wolt+")
                    continue

                seen.add(title.lower())
                # Skraćujemo preduge naslove na 80 znakova
                display = title if len(title) <= 80 else title[:77] + "..."
                res.append(f"• {display}")

    except Exception:
        pass

    return "\n".join(res) if res else None

# ---------------- SPARTAN MODE: FAKE PIXEL ----------------
TINY_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'

async def smart_diet_mode(route):
    if route.request.resource_type in ["image", "media"]:
        await route.fulfill(status=200, content_type="image/png", body=TINY_PNG)
    else:
        await route.continue_()

# ---------------- SMART SCROLLING (SAMO ZA GLOVO UI) ----------------
async def smart_scroll_and_extract(page, plat, address, log_ph=None, live_ph=None, live_state=None):
    results_dict = {}
    prev_count = 0
    attempts_at_bottom = 0
    
    while True:
        data = await page.evaluate('''() => {
            let rez = [];
            document.querySelectorAll("a:has(h3), a[data-testid='store-card'], .store-card a").forEach(c => {
                let link = c.href;
                if (!link.includes('/dostava') && !link.includes('/category')) { rez.push({link: link, text: c.innerText, html: c.innerHTML}); }
            });
            return rez;
        }''')

        for item in data:
            link = item['link']
            if not link or link in results_dict: continue
            
            text = item['text']
            html_content = item.get('html', '')
            all_text = text + " " + html_content
            
            name = remove_accents(extract_name(text))
            if len(name) < 2: continue
            
            rating = extract_rating(all_text, plat)
            time_str, time_num = extract_delivery_time(all_text)
            promo_str = extract_promo(text, html_content, plat)
            
            is_new = False
            t_low = text.strip().lower()
            is_new = t_low.endswith('new') or t_low.endswith('novo') or bool(re.search(r'•.*?new\b', t_low)) or (rating == "Novo" or rating == "New")

            results_dict[link] = {
                "Address": address, "Platform": plat, "Name": name, "Rating": rating,
                "Delivery Time": time_str, "Promo": promo_str, "Status": analyze_status(all_text),
                "Time_Num": time_num, "Is_New": is_new, "Link": link
            }

        current = len(results_dict)
        if current > prev_count:
            log_msg(f"[{plat.upper()} - {address}] Loaded {current} restaurants...", log_ph)
            
            if live_ph and live_state is not None:
                live_state[plat] = current
                refresh_live_ui(live_ph, live_state["Wolt"], live_state["Glovo"], address)
                
            prev_count = current
            attempts_at_bottom = 0
            
        await page.evaluate("window.scrollBy(0, 500);")
        await asyncio.sleep(0.5)
        
        h = await page.evaluate("document.body.scrollHeight")
        s = await page.evaluate("window.scrollY + window.innerHeight")
        
        if s >= h - 50:
            attempts_at_bottom += 1
            await asyncio.sleep(1.5)
            if attempts_at_bottom >= 5: break 
        
    return list(results_dict.values())


# ---------------- SCRAPERS ----------------

# HIBRIDNI API SCRAPER ZA WOLT (Ultra-brz JS konkurentni API requester)
async def scrape_wolt_api(context_wolt, address, log_ph=None, live_ph=None, live_state=None, error_screenshots=None, debug_mode=False):
    results_dict = {}
    page = None
    try:
        import urllib.request, json
        log_msg(f"[WOLT] Krenuo hibridni API. Geocodiranje adrese: {address}...", log_ph)
        
        custom_agent = 'DeliveryMonitorApp/6.0 (wolt_scraper)'
        try:
            geo_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(address + ', Serbia')}&format=json&limit=1&addressdetails=1"
            req = urllib.request.Request(geo_url, headers={'User-Agent': custom_agent})
            with urllib.request.urlopen(req) as response:
                geo_data = json.loads(response.read().decode())
            
            if not geo_data:
                await asyncio.sleep(1) 
                geo_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(address)}&format=json&limit=1&addressdetails=1"
                req = urllib.request.Request(geo_url, headers={'User-Agent': custom_agent})
                with urllib.request.urlopen(req) as response:
                    geo_data = json.loads(response.read().decode())
        except Exception as e:
            log_msg(f"[WOLT ERROR] Greška pri traženju koordinata: {e}", log_ph)
            return []
            
        if not geo_data:
            log_msg(f"[WOLT ERROR] Ne mogu da nadjem koordinate za: {address}", log_ph)
            return []
            
        lat = geo_data[0]["lat"]
        lon = geo_data[0]["lon"]
        
        city_raw = geo_data[0].get("address", {}).get("city", geo_data[0].get("address", {}).get("town", "srb"))
        city_slug = normalize_name(cyrillic_to_latin(city_raw))
        
        log_msg(f"[WOLT] Koordinate: {lat}, {lon} (Grad: {city_slug}). Ulazim u browser...", log_ph)
        
        page = await context_wolt.new_page()
        await page.goto("https://wolt.com/sr/srb")
        await asyncio.sleep(2)

        # Paginovano učitavanje SVIH restorana iz feed API-ja
        # Wolt vraća max ~20-30 po stranici, koristimo 'skip' parametar da prođemo sve
        log_msg(f"[WOLT] Učitavam sve restorane (paginacija)...", log_ph)
        all_items = []
        skip = 0
        page_size = 40  # Wolt interno ograničava, ali tražimo što više
        max_pages = 30  # Sigurnosni limit (30 * 40 = 1200 restorana, više nego dovoljno)

        for _ in range(max_pages):
            batch = await page.evaluate(f"""async () => {{
                try {{
                    // Probamo oba endpoint-a, noviji /restaurants ima bolju paginaciju
                    let endpoints = [
                        "https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}",
                        "https://restaurant-api.wolt.com/v1/pages/delivery?lat={lat}&lon={lon}&skip={skip}"
                    ];
                    for (let url of endpoints) {{
                        let res = await fetch(url);
                        if (res.ok) {{
                            let d = await res.json();
                            let items = [];
                            for (let section of (d.sections || [])) {{
                                for (let item of (section.items || [])) {{
                                    if (item.venue) items.push(item);
                                }}
                            }}
                            if (items.length > 0) return items;
                        }}
                    }}
                    return [];
                }} catch(e) {{ return []; }}
            }}""")

            if not batch:
                break  # Nema više restorana

            all_items.extend(batch)
            log_msg(f"[WOLT] Stranica {skip//page_size + 1}: +{len(batch)} restorana (ukupno {len(all_items)})", log_ph)

            if len(batch) < 10:
                break  # Poslednja stranica ima malo stavki, gotovi smo

            skip += page_size
            await asyncio.sleep(0.3)  # Mala pauza između stranica

        if not all_items:
            log_msg(f"[WOLT ERROR] Wolt je blokirao ili vratio prazan feed.", log_ph)
            return []

        log_msg(f"[WOLT] Ukupno učitano {len(all_items)} restorana iz feed-a.", log_ph)

        for item in all_items:
            venue = item.get("venue")
            if not venue: continue

            name = venue.get("name")
            slug = venue.get("slug")
            if not name or not slug: continue

            link_target = item.get("link", {}).get("target", "")
            if link_target.startswith("http"): link = link_target
            elif link_target.startswith("/"): link = f"https://wolt.com{link_target}"
            else: link = f"https://wolt.com/sr/srb/{city_slug}/restaurant/{slug}"

            if slug in results_dict: continue  # dedup po slug-u (ispravan kljuc)

            status = "Open" if venue.get("online") else "Closed"
            rating_score = venue.get("rating", {}).get("score")
            rating = str(rating_score) if rating_score else "-"

            est_range = venue.get("estimate_range")
            est_minutes = venue.get("estimate")

            time_num = np.nan
            time_str = "-"
            if est_range:
                time_str = f"{est_range} min"
                try:
                    parts = str(est_range).split('-')
                    time_num = (int(parts[0]) + int(parts[1])) / 2.0
                except: pass
            elif est_minutes:
                time_str = f"{est_minutes} min"
                time_num = float(est_minutes)

            feed_payload = get_all_json_strings(item).lower()
            is_new = "new" in feed_payload or "novo" in feed_payload or "new!" in feed_payload
            promo_initial = extract_promo(feed_payload, "", "Wolt")

            results_dict[slug] = {
                "Address": address, "Platform": "Wolt", "Name": remove_accents(name), "Rating": rating,
                "Delivery Time": time_str, "Promo": promo_initial, "Status": status,
                "Time_Num": time_num, "Is_New": is_new, "Link": link
            }
                
        if live_ph and live_state is not None:
            live_state["Wolt"] = len(results_dict)
            refresh_live_ui(live_ph, live_state["Wolt"], live_state["Glovo"], address, custom_text=f"📍 Skinuto {len(results_dict)} Wolt restorana. Pripremam munjevito skeniranje akcija...")

        log_msg(f"[WOLT - {address}] Skinuto {len(results_dict)} restorana. Pokrećem munjeviti JS requester za popuste...", log_ph)

        # 3. KORAK: MUNJEVITI KONKURENTNI JS POZIVI (Ovo smanjuje vreme sa 40 na ~4 sekunde)
        js_fetch_promos = """
        async ([slugs, lat, lon]) => {
            let results = {};
            let concurrencyLimit = 20; // povecano na 20 istovremenih upita
            let i = 0;

            async function fetchWithTimeout(url, timeoutMs) {
                let controller = new AbortController();
                let timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    let res = await fetch(url, { signal: controller.signal });
                    clearTimeout(timer);
                    return res;
                } catch(e) {
                    clearTimeout(timer);
                    throw e;
                }
            }
            
            async function fetchSlug(slug) {
                let retries = 3;
                let delay = 500;
                let dynamicData = null;

                // --- KORAK A: Dynamic endpoint (venue-level discounts) ---
                while (retries > 0) {
                    try {
                        let url = `https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/${slug}/dynamic/?lat=${lat}&lon=${lon}&selected_delivery_method=homedelivery`;
                        let res = await fetchWithTimeout(url, 8000);
                        if (res.ok) {
                            dynamicData = await res.json();
                            break;
                        } else if (res.status === 429) {
                            await new Promise(r => setTimeout(r, delay));
                            delay *= 2;
                            retries--;
                        } else { break; }
                    } catch(e) {
                        retries--;
                        if (retries > 0) await new Promise(r => setTimeout(r, 300));
                    }
                }

                // --- KORAK B: Menu endpoint (item-level discounts - precrtane cene) ---
                // Ovo hvata restorane koji imaju popuste na PROIZVODE ali ne i na venue nivou
                let itemDiscountInfo = { has_item_discounts: false, item_discount_pct: null };
                try {
                    let menuUrl = `https://restaurant-api.wolt.com/v4/venues/slug/${slug}/menu?unit_prices=true`;
                    let menuRes = await fetchWithTimeout(menuUrl, 8000);
                    if (menuRes.ok) {
                        let menuData = await menuRes.json();
                        // Prolazimo kroz sve kategorije i proizvode
                        let categories = menuData.categories || [];
                        let discountedItems = 0;
                        let totalItems = 0;
                        let maxDiscountPct = 0;
                        for (let cat of categories) {
                            for (let item of (cat.items || [])) {
                                totalItems++;
                                // original_price postoji i veci je od price = precrtana cena = popust
                                let price = item.baseprice || item.price || 0;
                                let origPrice = item.original_price || 0;
                                if (origPrice > 0 && origPrice > price && price > 0) {
                                    discountedItems++;
                                    let pct = Math.round((1 - price / origPrice) * 100);
                                    if (pct > maxDiscountPct) maxDiscountPct = pct;
                                }
                            }
                        }
                        if (discountedItems > 0) {
                            itemDiscountInfo.has_item_discounts = true;
                            itemDiscountInfo.discounted_items = discountedItems;
                            itemDiscountInfo.total_items = totalItems;
                            itemDiscountInfo.max_discount_pct = maxDiscountPct;
                        }
                    }
                } catch(e) { /* menu fetch neobavezan, nastavljamo */ }

                results[slug] = {
                    dynamic: dynamicData,
                    item_discount: itemDiscountInfo
                };
            }

            let workers = [];
            for (let j = 0; j < concurrencyLimit; j++) {
                workers.push((async () => {
                    while (i < slugs.length) {
                        let currentIndex = i++;
                        await fetchSlug(slugs[currentIndex]);
                    }
                })());
            }
            
            // Cekamo da svi 'radnici' zavrse
            await Promise.all(workers);
            return results;
        }
        """

        slugs = list(results_dict.keys())
        
        # SVE restorane šaljemo odjednom u Browser. JavaScript će ih sam pametno isprocesuirati!
        all_promo_data = await page.evaluate(js_fetch_promos, [slugs, lat, lon])
        
        for slug, combined in all_promo_data.items():
            if not combined:
                continue

            data = combined.get("dynamic") if isinstance(combined, dict) else combined
            item_discount = combined.get("item_discount", {}) if isinstance(combined, dict) else {}

            full_payload = get_all_json_strings(data).lower() if data else ""

            # KORAK 1: Direktno čitanje iz 'discounts' arraya (venue-level)
            api_promos = extract_wolt_discounts_from_api(data) if data else None

            # KORAK 2: Stari regex kao dopuna
            promo_regex = extract_promo(full_payload, "", "Wolt") if full_payload else "-"

            # KORAK 3: Item-level popusti (precrtane cene u meniju)
            item_promo = None
            if item_discount and item_discount.get("has_item_discounts"):
                n = item_discount.get("discounted_items", 0)
                pct = item_discount.get("max_discount_pct", 0)
                if pct > 0:
                    item_promo = f"• Popust do {pct}% na {n} proizvoda u meniju"
                else:
                    item_promo = f"• Popust na {n} proizvoda u meniju"

            # KORAK 4: Spajamo sve - API naslovi imaju prioritet
            existing_promo = results_dict[slug]["Promo"]
            final_promos_ordered = []
            seen_promos = set()

            for source in [api_promos, item_promo, promo_regex if not api_promos else None, existing_promo]:
                if not source or source == "-":
                    continue
                for p in source.split('\n'):
                    p = p.strip()
                    if p and p not in seen_promos:
                        if api_promos and source == promo_regex and re.search(r'\d+\s*%\s*discount', p, re.IGNORECASE):
                            continue  # preskoci genericki regex % ako imamo API naslov
                        seen_promos.add(p)
                        final_promos_ordered.append(p)

            if final_promos_ordered:
                results_dict[slug]["Promo"] = "\n".join(final_promos_ordered)

            if data and ("new" in full_payload or "novo" in full_payload):
                results_dict[slug]["Is_New"] = True

        # Čišćenje slug-ova iz tabele
        for v in results_dict.values():
            v.pop("Slug", None)

        log_msg(f"[WOLT] Gotovo. Skenirano {len(results_dict)} restorana.", log_ph)
        if live_ph and live_state is not None:
             refresh_live_ui(live_ph, live_state["Wolt"], live_state["Glovo"], address)

        return list(results_dict.values())
        
    except Exception as e:
        log_msg(f"[WOLT API ERROR] {e}", log_ph)
        return []
    finally:
        if page: await page.close()


async def scrape_glovo(context_glovo, address, log_ph=None, live_ph=None, live_state=None, error_screenshots=None, debug_mode=False):
    page = None
    try:
        page = await context_glovo.new_page()
        
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.set_default_timeout(15000) 
        
        await page.goto("https://glovoapp.com/sr/rs", wait_until="domcontentloaded")
        
        try:
            accept_btn = page.locator("button", has_text=re.compile(r"Accept All|Prihvati sve", re.IGNORECASE)).first
            await accept_btn.wait_for(state="visible", timeout=4000)
            await accept_btn.click()
            await asyncio.sleep(1.5)
        except: pass
        
        stranica_tekst = await page.content()
        if "Oh, no!" in stranica_tekst or "It looks like there's a problem" in stranica_tekst:
            log_msg(f"[GLOVO BLOCKED] {address}.", log_ph)
            if error_screenshots is not None and debug_mode:
                try:
                    err_path = str(ERRORS_DIR / f"Glovo_SoftBan_{remove_accents(address).replace(' ', '_')}_{timestamp()}.png")
                    await page.screenshot(path=err_path)
                    error_screenshots.append(err_path)
                except: pass
            return []
        
        try:
            hero_input = page.locator("#hero-container-input")
            await hero_input.wait_for(state="visible", timeout=5000)
            await hero_input.click()
            search = page.get_by_role("searchbox")
            await search.fill(address)
            
            dropdown_item = page.locator("div[data-actionable='true'][role='button']").first
            await dropdown_item.wait_for(state="visible", timeout=8000)
            await dropdown_item.click()
            
        except PlaywrightTimeoutError:
            log_msg(f"[GLOVO] Changing address in header to: {address}", log_ph)
            try:
                header_btn = page.locator('header div[role="button"]').first
                await header_btn.wait_for(state="visible", timeout=6000)
                await header_btn.click()
                
                await asyncio.sleep(1.5)
                search_modal = page.get_by_role("searchbox").last
                await search_modal.wait_for(state="visible", timeout=6000)
                await search_modal.click()
                await search_modal.fill(address)
                
                await asyncio.sleep(2.5)
                dropdown_item = page.locator("div[data-actionable='true'][role='button']").first
                await dropdown_item.wait_for(state="visible", timeout=8000)
                await dropdown_item.click()
            except PlaywrightTimeoutError:
                log_msg(f"[GLOVO ABORT] Cannot change address for {address}.", log_ph)
                if error_screenshots is not None and debug_mode:
                    try:
                        err_path = str(ERRORS_DIR / f"Glovo_Nav_Error_{remove_accents(address).replace(' ', '_')}_{timestamp()}.png")
                        await page.screenshot(path=err_path)
                        error_screenshots.append(err_path)
                    except: pass
                return []

        try:
            btn_drugo = page.locator("button:has-text('Drugo')")
            await btn_drugo.wait_for(state="visible", timeout=4000)
            await btn_drugo.click()
        except PlaywrightTimeoutError: pass
        
        try:
            btn_potvrdi = page.locator("button:has-text('Potvrdi adresu')")
            await btn_potvrdi.wait_for(state="visible", timeout=4000)
            await btn_potvrdi.click()
        except PlaywrightTimeoutError: pass
        
        await asyncio.sleep(6)
        
        try:
            btn_pocetna = page.locator("text='Idi na početnu stranicu'")
            if await btn_pocetna.count() > 0 and await btn_pocetna.first.is_visible(timeout=4000):
                await btn_pocetna.first.click()
                await asyncio.sleep(6)
        except: pass
        
        try:
            kat_link = page.get_by_role("link", name=re.compile(r"Restorani|Hrana|Food|Restaurants", re.I)).first
            await kat_link.wait_for(state="visible", timeout=6000)
            await kat_link.click()
        except PlaywrightTimeoutError: pass
        
        try:
            await page.wait_for_selector("a[data-testid='store-card'], .store-card a", timeout=15000)
        except PlaywrightTimeoutError:
            log_msg(f"[GLOVO WARNING] Restorani nisu ucitani na vreme za {address}.", log_ph)
        
        page.set_default_timeout(60000) 
        rez = await smart_scroll_and_extract(page, "Glovo", address, log_ph, live_ph, live_state)
        
        if len(rez) < 5 and debug_mode:
            if error_screenshots is not None:
                err_path = str(ERRORS_DIR / f"Glovo_Warning_{remove_accents(address).replace(' ', '_')}_{timestamp()}.png")
                try:
                    await page.screenshot(path=err_path)
                    error_screenshots.append(err_path)
                except: pass

        return rez
    except Exception as e: 
        log_msg(f"[GLOVO ERROR] {e}", log_ph)
        if page and error_screenshots is not None and debug_mode:
            try:
                err_path = str(ERRORS_DIR / f"Glovo_Error_{remove_accents(address).replace(' ', '_')}_{timestamp()}.png")
                await page.screenshot(path=err_path)
                error_screenshots.append(err_path)
            except: pass
        return []
    finally:
        if page: await page.close()


# ---------------- SEQUENTIAL SCAN PROCESS ----------------
async def scan_process(addresses, log_ph, live_ph, live_state, generate_pdf=False, recipient_email="", debug_mode=False):
    all_data = []
    error_screenshots = [] 
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage", 
                "--no-sandbox"             
            ]
        ) 
        
        wa = {
            "permissions": ['geolocation'],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
            
        if os.path.exists(WOLT_AUTH_FILE):
            wa["storage_state"] = WOLT_AUTH_FILE
            
        ga = {
            "permissions": ['geolocation'],
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9,sr;q=0.8"}
        }
        
        if debug_mode:
            ga["record_video_dir"] = str(ERRORS_DIR)
            ga["record_video_size"] = {"width": 1280, "height": 720}

        if os.path.exists(GLOVO_AUTH_FILE):
            log_msg("🔐 GLOVO: Loaded VIP pass.", log_ph)
            ga["storage_state"] = GLOVO_AUTH_FILE
            
        for i, adr in enumerate(addresses):
            live_state["Wolt"] = 0
            live_state["Glovo"] = 0
            refresh_live_ui(live_ph, 0, 0, adr)
            
            if i > 0:
                log_msg("⏳ Pausing for 5 seconds between addresses...", log_ph)
                await asyncio.sleep(5)
                
            log_msg(f"\n[SYSTEM] Starting scan for: {adr}", log_ph)
            
            log_msg("📱 Scrolling GLOVO...", log_ph)
            context_glovo = await browser.new_context(**ga)
            await context_glovo.route("**/*", smart_diet_mode)
            r_glovo = await scrape_glovo(context_glovo, adr, log_ph, live_ph, live_state, error_screenshots, debug_mode)
            all_data.extend(r_glovo)
            await context_glovo.close() 
            
            log_msg("🚲 Calling WOLT API...", log_ph)
            context_wolt = await browser.new_context(**wa)
            r_wolt = await scrape_wolt_api(context_wolt, adr, log_ph, live_ph, live_state, error_screenshots, debug_mode)
            all_data.extend(r_wolt)
            await context_wolt.close() 
                
        await browser.close()
            
    if all_data:
        df_s = pd.DataFrame(all_data)
        df_h = save_to_history(df_s)
        
        pdf_files = []
        if generate_pdf:
            log_msg("Generating PDF reports...", log_ph)
            try:
                zbirni = napravi_zbirni_pdf(df_s, df_h) 
                if zbirni: pdf_files.append(zbirni)
                
                for adr in df_s["Address"].unique():
                    df_sub = df_s[df_s["Address"] == adr]
                    p_file = napravi_pdf_za_adresu(df_sub, adr, df_h) 
                    if p_file: pdf_files.append(p_file)
            except NameError:
                log_msg("[WARNING] PDF functions are missing from the code context.", log_ph)
                
            if recipient_email.strip() and pdf_files:
                log_msg(f"Sending reports to: {recipient_email}", log_ph)
                send_email(pdf_files, recipient_email, log_ph)
        else:
            log_msg("Scan complete. PDF option is disabled.", log_ph)
            
        return df_s, df_h, pdf_files, error_screenshots
    return pd.DataFrame(), pd.DataFrame(), [], error_screenshots

# ================= STREAMLIT UI =================
if 'is_running' not in st.session_state: st.session_state.is_running = False
if 'last_run' not in st.session_state: st.session_state.last_run = 0
if 'df_all' not in st.session_state: st.session_state.df_all = pd.DataFrame()
if 'pdf_files' not in st.session_state: st.session_state.pdf_files = []
if 'error_screenshots' not in st.session_state: st.session_state.error_screenshots = []
if 'loaded_history' not in st.session_state: st.session_state.loaded_history = False
if 'last_alerts' not in st.session_state: st.session_state.last_alerts = []

if 'df_history' not in st.session_state: 
    if os.path.exists(HISTORY_FILE): st.session_state.df_history = pd.read_csv(HISTORY_FILE)
    else: st.session_state.df_history = pd.DataFrame()

st.title("🍔 Delivery Monitor (Wolt & Glovo)")
with st.sidebar:
    st.header("⚙️ Settings")
    
    address_1 = st.text_input("📍 Address 1 (Required):", value="", placeholder="Makenzijeva 57, Belgrade")
    address_2 = st.text_input("📍 Address 2 (Optional):", value="", placeholder="Somborska 5, Niš")
    
    auto_refresh = st.checkbox("🔄 Auto-refresh", value=False)
    sleep_interval = st.number_input("⏱️ Interval (min):", min_value=1, value=60, disabled=not auto_refresh)
    
    generate_pdf = st.checkbox("📄 Generate PDF reports", value=False)
    email_input = st.text_input("📧 Send to email:", placeholder="your@email.com") if generate_pdf else ""

    st.markdown("---")
    debug_mode = st.checkbox("🛠️ Enable Debug Mode (Video/HTML Logs)", value=False)
    st.markdown("---")
    
    c1, c2 = st.columns(2)
    with c1:
        if st.button("▶️ START", type="primary", use_container_width=True): 
            st.session_state.is_running = True
            st.session_state.loaded_history = False
            st.session_state.last_run = 0
            st.rerun()
    with c2:
        if st.button("⏹️ STOP", use_container_width=True): 
            st.session_state.is_running = False
            st.rerun()

    st.markdown("---")
    st.header("📂 Scan Archive")
    history_files = sorted(list(OUTPUT_DIR.glob("Detaljno_*.csv")), reverse=True)
    options = {"--- Choose old report ---": None}
    for f in history_files:
        name = f.stem.replace("Detaljno_", "")
        try: options[datetime.datetime.strptime(name, "%Y%m%d_%H%M%S").strftime("%d.%m.%Y u %H:%M:%S")] = f
        except: options[name] = f

    selected_file = st.selectbox("Previous scans:", list(options.keys()), label_visibility="collapsed")
    col_load, col_del = st.columns(2)
    with col_load:
        if st.button("📂 Load", use_container_width=True) and options[selected_file]:
            st.session_state.df_all = pd.read_csv(options[selected_file])
            st.session_state.is_running = False
            st.session_state.loaded_history = True
            st.session_state.last_run = os.path.getmtime(options[selected_file])
            st.rerun()
    with col_del:
        if st.button("🗑️ Delete", type="secondary", use_container_width=True) and options[selected_file]:
            os.remove(options[selected_file])
            if st.session_state.loaded_history: st.session_state.df_all = pd.DataFrame(); st.session_state.loaded_history = False
            st.rerun()

    st.markdown("---")
    st.header("⚠️ System Reset")
    with st.expander("Danger Zone (Delete Everything)"):
        st.warning("This deletes ALL old reports and history!")
        reset_pass = st.text_input("Password:", type="password", key="reset_pass")
        if st.button("🚨 DELETE ALL", use_container_width=True):
            if reset_pass == "zekapeka":
                if os.path.exists(HISTORY_FILE): os.remove(HISTORY_FILE)
                st.session_state.df_history = pd.DataFrame()
                
                for f in OUTPUT_DIR.glob("Detaljno_*.csv"):
                    try: os.remove(f)
                    except: pass
                for f in OUTPUT_DIR.glob("*.pdf"):
                    try: os.remove(f)
                    except: pass
                
                st.session_state.df_all = pd.DataFrame()
                st.session_state.loaded_history = False
                st.session_state.is_running = False
                
                st.success("✅ System successfully reset!")
                time.sleep(1.5)
                st.rerun()
            else:
                st.error("❌ Incorrect password!")

# ================= MAIN INTERFACE (TABS & LOADING) =================
if st.session_state.is_running or st.session_state.loaded_history:

    if st.session_state.is_running:
        list_addresses = [cyrillic_to_latin(a.strip()) for a in [address_1, address_2] if a.strip()]
        if not list_addresses: 
            st.warning("⚠️ Enter at least the first address to scan!"); st.session_state.is_running = False; st.rerun()

        now = time.time()
        if now - st.session_state.last_run >= sleep_interval * 60 or st.session_state.last_run == 0:
            
            with st.spinner('🔄 Script is searching for restaurants, please wait...'):
                live_ui_ph = st.empty() 
                sl = st.empty() 
                live_state = {"Wolt": 0, "Glovo": 0}
                
                df, hi, pdf, err_imgs = asyncio.run(scan_process(list_addresses, sl, live_ui_ph, live_state, generate_pdf, email_input, debug_mode))
                
                if not df.empty:
                    df.to_csv(OUTPUT_DIR / f"Detaljno_{timestamp()}.csv", index=False)

                live_ui_ph.empty()
                st.session_state.df_all, st.session_state.df_history, st.session_state.pdf_files, st.session_state.error_screenshots, st.session_state.last_run = df, hi, pdf, err_imgs, time.time()
                sl.empty()
            st.rerun()

    df = st.session_state.df_all
    if not df.empty:
        for col in ["Time_Num", "Delivery Time", "Rating", "Is_New"]:
            if col not in df.columns: df[col] = False if col == "Is_New" else (np.nan if "Num" in col else "-")

        if st.session_state.loaded_history: st.info("📂 **Viewing archived report.**")
        else: st.success(f"✅ Scan completed at: {datetime.datetime.fromtimestamp(st.session_state.last_run, LOCAL_TZ).strftime('%H:%M:%S')}")
        
        tab_dash, tab_list, tab_compare, tab_promo, tab_amm, tab_alert, tab_stats = st.tabs([
            "📊 Dashboard", "🔍 Restaurant List", "⚖️ Comparison", "🎁 Promos & Discounts",
            "👥 AMM Baza", "🔔 Promo Alert", "📈 AM Statistika"
        ])

        unique_addresses = list(df["Address"].unique())
        
        with tab_dash:
            for adr in unique_addresses:
                st.markdown(f"<h3 style='color: #2c3e50;'>📍 {adr.upper()}</h3>", unsafe_allow_html=True)
                sd = df[df["Address"] == adr]
                
                w_total = len(sd[sd["Platform"] == "Wolt"])
                w_open = len(sd[(sd["Platform"] == "Wolt") & (sd["Status"] == "Open")])
                g_total = len(sd[sd["Platform"] == "Glovo"])
                g_open = len(sd[(sd["Platform"] == "Glovo") & (sd["Status"] == "Open")])
                
                html_kpi = f"""
                <div class="kpi-wrapper">
                    <div class="kpi-card kpi-wolt">
                        <div class="kpi-title">Wolt Total</div>
                        <div class="kpi-value">{w_total}</div>
                    </div>
                    <div class="kpi-card kpi-wolt">
                        <div class="kpi-title">Wolt Open</div>
                        <div class="kpi-value" style="color: #27ae60;">{w_open}</div>
                    </div>
                    <div class="kpi-card kpi-glovo">
                        <div class="kpi-title">Glovo Total</div>
                        <div class="kpi-value">{g_total}</div>
                    </div>
                    <div class="kpi-card kpi-glovo">
                        <div class="kpi-title">Glovo Open</div>
                        <div class="kpi-value" style="color: #27ae60;">{g_open}</div>
                    </div>
                </div>
                """
                st.markdown(html_kpi, unsafe_allow_html=True)

            st.markdown("---")
            chart_addr = st.selectbox("📍 Filter Charts:", ["All addresses"] + unique_addresses, index=1 if len(unique_addresses) == 1 else 0)
            c_df = df if chart_addr == "All addresses" else df[df["Address"] == chart_addr]
            
            ca, cb = st.columns(2)
            with ca: st.plotly_chart(create_status_chart_ui(c_df, "Status Comparison"), use_container_width=True)
            with cb: st.plotly_chart(create_delivery_time_chart_ui(c_df, "Average Delivery Time"), use_container_width=True)

            st.markdown("---")
            st.markdown("##### 📅 Activity History")
            hist_df = st.session_state.df_history.copy()
            
            if not hist_df.empty:
                c_h = hist_df if chart_addr == "All addresses" else hist_df[hist_df["Address"] == chart_addr]
                if not c_h.empty and 'Date' in c_h.columns and 'Time' in c_h.columns:
                    c_h['Datetime'] = pd.to_datetime(c_h['Date'] + ' ' + c_h['Time'])
                    min_d = c_h['Datetime'].min().date()
                    max_d = c_h['Datetime'].max().date()
                    
                    c_dt1, c_dt2, c_dt3, c_dt4 = st.columns(4)
                    with c_dt1: start_date = st.date_input("From date:", min_d, min_value=min_d, max_value=max_d)
                    with c_dt2: start_time = st.time_input("From time:", datetime.time(0, 0))
                    with c_dt3: end_date = st.date_input("To date:", max_d, min_value=min_d, max_value=max_d)
                    with c_dt4: end_time = st.time_input("To time:", datetime.time(23, 59))
                    
                    start_dt = pd.to_datetime(datetime.datetime.combine(start_date, start_time))
                    end_dt = pd.to_datetime(datetime.datetime.combine(end_date, end_time))
                    mask = (c_h['Datetime'] >= start_dt) & (c_h['Datetime'] <= end_dt)
                    chart_hist = c_h.loc[mask].copy()
                    
                    st.plotly_chart(create_timeline_chart_ui(chart_hist, None, "History: Open restaurants", metric="Open", ylabel="Open restaurants"), use_container_width=True)
                    
                    ch1, ch2 = st.columns(2)
                    with ch1: st.plotly_chart(create_timeline_chart_ui(chart_hist, None, "History: Avg delivery time", metric="Avg_Time", ylabel="Time (min)"), use_container_width=True)
                    with ch2: st.plotly_chart(create_timeline_chart_ui(chart_hist, None, "History: Restaurants on promo", metric="Promo_Count", ylabel="Promo count"), use_container_width=True)
                else: st.info("No history for selected address.")
            else: st.info("No historical data.")

        with tab_list:
            f1, f2, f3 = st.columns(3)
            with f1: fa = st.multiselect("📍 Address", df["Address"].unique(), df["Address"].unique())
            with f2: fp = st.multiselect("📱 Platform", df["Platform"].unique(), df["Platform"].unique())
            with f3: fs = st.multiselect("🚦 Status", ["Open", "Closed"], ["Open", "Closed"])
            c_filt1, c_filt2 = st.columns(2)
            with c_filt1: filt_new = st.checkbox("✨ Show only NEW restaurants")
            with c_filt2: filt_promo = st.checkbox("🔥 Show only restaurants ON PROMO")
            
            f_df = df[(df["Address"].isin(fa)) & (df["Platform"].isin(fp)) & (df["Status"].isin(fs))]
            if filt_new: f_df = f_df[f_df["Is_New"].isin([True, 'True', 'true', 1])]
            if filt_promo: f_df = f_df[f_df["Promo"] != "-"]

            disp_df = f_df.copy()
            disp_df["Badge"] = disp_df["Is_New"].apply(lambda x: "✨ NEW" if x in [True, 'True', 'true', 1] else "")
            disp_df = disp_df.drop(columns=['Naziv_Norm', 'Time_Num', 'Is_New'], errors='ignore')
            cols = ["Address", "Platform", "Name", "Status", "Rating", "Delivery Time", "Promo", "Badge", "Link"]
            disp_df = disp_df[cols]

            def style_rows(row):
                styles = [''] * len(row)
                styles[row.index.get_loc('Status')] = 'color: #27ae60; font-weight: bold;' if row['Status'] == 'Open' else 'color: #e74c3c; font-weight: bold;'
                if row['Promo'] != '-': styles[row.index.get_loc('Promo')] = 'color: #8e44ad; font-weight: bold;'
                return styles

            st.dataframe(disp_df.style.apply(style_rows, axis=1), use_container_width=True, hide_index=True, height=800, column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open on site"), "Promo": st.column_config.TextColumn("Promo", width="large")})

        with tab_compare:
            c_up1, c_up2 = st.columns(2)
            with c_up1: filter_wolt_up = st.multiselect("🚦 Wolt filter:", ["Open", "Closed"], default=["Open", "Closed"], key="fw")
            with c_up2: filter_glovo_up = st.multiselect("🚦 Glovo filter:", ["Open", "Closed"], default=["Open", "Closed"], key="fg")

            df['Name_Norm'] = df['Name'].apply(normalize_name)
            compare_data = []
            for adr in unique_addresses:
                df_adr = df[df['Address'] == adr]
                common = set(df_adr[df_adr['Platform'] == 'Wolt']['Name_Norm']).intersection(set(df_adr[df_adr['Platform'] == 'Glovo']['Name_Norm']))
                for norm_name in common:
                    w_row = df_adr[(df_adr['Platform'] == 'Wolt') & (df_adr['Name_Norm'] == norm_name)].iloc[0]
                    g_row = df_adr[(df_adr['Platform'] == 'Glovo') & (df_adr['Name_Norm'] == norm_name)].iloc[0]
                    compare_data.append({
                        "Address": adr, "Name (Wolt)": w_row['Name'], "Status Wolt": w_row['Status'], "Time Wolt": w_row['Delivery Time'], "Link Wolt": w_row['Link'], "Name (Glovo)": g_row['Name'], "Status Glovo": g_row['Status'], "Time Glovo": g_row['Delivery Time'], "Link Glovo": g_row['Link']
                    })
            
            if compare_data:
                df_compare = pd.DataFrame(compare_data)
                df_compare = df_compare[(df_compare['Status Wolt'].isin(filter_wolt_up)) & (df_compare['Status Glovo'].isin(filter_glovo_up))]
                if not df_compare.empty: st.dataframe(df_compare.style.map(lambda val: f'color: {"#27ae60" if val=="Open" else "#e74c3c"}; font-weight: bold;', subset=['Status Wolt', 'Status Glovo']), use_container_width=True, hide_index=True, height=800, column_config={"Link Wolt": st.column_config.LinkColumn("Link Wolt", display_text="Open Wolt"), "Link Glovo": st.column_config.LinkColumn("Link Glovo", display_text="Open Glovo")})
                else: st.info("No restaurants match these filters.")
            else: st.info("No common restaurants found on both platforms.")

        with tab_promo:
            unique_promos = set()
            for promo_str in c_df['Promo']:
                if pd.notna(promo_str) and str(promo_str) != "-":
                    for a in str(promo_str).split('\n'):
                        cl = a.replace("• ", "").strip()
                        if cl: unique_promos.add(cl)
            unique_promos = sorted(list(unique_promos))
            selected_promos = st.multiselect("Select promos to show on chart:", unique_promos, default=unique_promos)
            st.plotly_chart(create_promo_chart_ui(c_df, selected_promos, "Number of restaurants with selected promos"), use_container_width=True)

        # ══════════════════════════════════════════════════════════════════════
        # TAB: AMM BAZA
        # ══════════════════════════════════════════════════════════════════════
        with tab_amm:
            st.markdown("### 👥 Baza Account Managera")
            st.caption("Ovde unosite koji AM je zadužen za koji restoran. Podaci se čuvaju u `amm_baza.csv`.")

            amm_df = load_amm()

            # ── Dodavanje novog unosa ─────────────────────────────────────────
            st.markdown("#### ➕ Dodaj / Ažuriraj restoran")
            
            # Dropdown sa imenima restorana iz poslednjeg scana
            all_restaurant_names = sorted(df["Name"].dropna().unique().tolist())
            col_r1, col_r2 = st.columns([2, 1])
            with col_r1:
                selected_restaurant = st.selectbox("Restoran (iz poslednjeg scana):", ["-- Odaberi --"] + all_restaurant_names, key="amm_sel_rest")
            with col_r2:
                manual_restaurant = st.text_input("Ili upiši ručno:", key="amm_manual_rest", placeholder="npr. McDonald's")

            restaurant_final = manual_restaurant.strip() if manual_restaurant.strip() else (selected_restaurant if selected_restaurant != "-- Odaberi --" else "")
            
            # City iz scana
            city_options = sorted(df["Address"].dropna().unique().tolist())
            col_c1, col_c2, col_c3, col_c4 = st.columns(4)
            with col_c1: amm_city     = st.selectbox("Grad:", ["-- Svi --"] + city_options, key="amm_city")
            with col_c2: amm_am_name  = st.text_input("Ime AM-a:", key="amm_am_name",  placeholder="Marko Marković")
            with col_c3: amm_am_email = st.text_input("Email AM-a:", key="amm_am_email", placeholder="marko@firma.com")

            with col_c4:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("💾 Sačuvaj", use_container_width=True, key="amm_save_btn"):
                    if not restaurant_final:
                        st.error("Izaberi ili upiši naziv restorana.")
                    elif not amm_am_name or not amm_am_email:
                        st.error("Upiši ime i email AM-a.")
                    else:
                        norm = normalize_name(restaurant_final)
                        city_val = "" if amm_city == "-- Svi --" else amm_city
                        new_row = pd.DataFrame([{
                            "restaurant_norm":    norm,
                            "restaurant_display": restaurant_final,
                            "city":               city_val,
                            "am_name":            amm_am_name,
                            "am_email":           amm_am_email,
                        }])
                        # Ažuriraj ako postoji isti norm+city, inače dodaj
                        mask = (amm_df["restaurant_norm"] == norm) & (amm_df["city"] == city_val)
                        if mask.any():
                            amm_df.loc[mask, ["restaurant_display","am_name","am_email"]] = [restaurant_final, amm_am_name, amm_am_email]
                        else:
                            amm_df = pd.concat([amm_df, new_row], ignore_index=True)
                        save_amm(amm_df)
                        st.success(f"✅ Sačuvano: **{restaurant_final}** → {amm_am_name} ({amm_am_email})")
                        st.rerun()

            st.markdown("---")
            st.markdown("#### 📋 Trenutna baza AMM-ova")

            if amm_df.empty:
                st.info("Baza je prazna. Dodaj first restoran gore.")
            else:
                # Filter po AM-u
                am_filter_opts = ["Svi"] + sorted(amm_df["am_name"].dropna().unique().tolist())
                am_filter = st.selectbox("Filtriraj po AM-u:", am_filter_opts, key="amm_filter")
                view_amm = amm_df if am_filter == "Svi" else amm_df[amm_df["am_name"] == am_filter]

                # Editable tabela
                edited_amm = st.data_editor(
                    view_amm.reset_index(drop=True),
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
                    key="amm_editor"
                )
                if st.button("💾 Sačuvaj izmene u tabeli", key="amm_save_table"):
                    # Merge edita nazad u amm_df (samo vidljivi redovi)
                    if am_filter == "Svi":
                        save_amm(edited_amm)
                    else:
                        amm_df_rest = amm_df[amm_df["am_name"] != am_filter]
                        save_amm(pd.concat([amm_df_rest, edited_amm], ignore_index=True))
                    st.success("✅ Baza AMM-ova ažurirana!")
                    st.rerun()

                # Brzi bulk import iz CSV-a
                st.markdown("---")
                st.markdown("#### 📥 Bulk import (CSV)")
                st.caption("CSV mora imati kolone: `restaurant_display, city, am_name, am_email`")
                uploaded_amm = st.file_uploader("Učitaj CSV:", type="csv", key="amm_upload")
                if uploaded_amm:
                    try:
                        new_amm = pd.read_csv(uploaded_amm)
                        new_amm["restaurant_norm"] = new_amm["restaurant_display"].apply(normalize_name)
                        merged = pd.concat([amm_df, new_amm], ignore_index=True).drop_duplicates(subset=["restaurant_norm","city"], keep="last")
                        save_amm(merged)
                        st.success(f"✅ Importovano {len(new_amm)} redova. Ukupno: {len(merged)}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Greška pri importu: {e}")

        # ══════════════════════════════════════════════════════════════════════
        # TAB: PROMO ALERT
        # ══════════════════════════════════════════════════════════════════════
        with tab_alert:
            st.markdown("### 🔔 Promo Alert – Wolt vs Glovo")
            st.markdown("""
            Ovaj tab poredi promocije iz poslednjeg scana:  
            - Pronalazi restorane koji **imaju promo na Woltu** ali **nemaju na Glovu**  
            - Šalje **email odgovornom AM-u** (iz AMM baze)  
            - Dnevni automatski scan pokreće se u **09:00**
            """)

            # ── Preview nepodudaranja ─────────────────────────────────────────
            st.markdown("#### 🔍 Preview trenutnog scana")
            if df.empty:
                st.info("Pokreni scan pa se vrati ovde.")
            else:
                df["_norm"] = df["Name"].apply(normalize_name)
                mismatch_rows = []
                for city_a in df["Address"].unique():
                    df_c = df[df["Address"] == city_a]
                    wolt_p  = df_c[(df_c["Platform"] == "Wolt") & (df_c["Promo"] != "-")]
                    glovo_norms = set(df_c[df_c["Platform"] == "Glovo"]["_norm"])
                    glovo_promo_norms = set(df_c[(df_c["Platform"] == "Glovo") & (df_c["Promo"] != "-")]["_norm"])
                    for _, wr in wolt_p.iterrows():
                        n = wr["_norm"]
                        status = "✅ Ima na Glovu" if n in glovo_promo_norms else ("⚠️ Nema promo na Glovu" if n in glovo_norms else "❌ Nije na Glovu")
                        amm_match = load_amm()
                        am_info = "-"
                        if not amm_match.empty:
                            r = amm_match[amm_match["restaurant_norm"] == n]
                            if not r.empty: am_info = f"{r.iloc[0]['am_name']} ({r.iloc[0]['am_email']})"
                        mismatch_rows.append({
                            "Grad": city_a, "Restoran": wr["Name"],
                            "Wolt Promo": wr["Promo"].replace("\n", " | "),
                            "Glovo Status": status, "AM": am_info
                        })

                if mismatch_rows:
                    df_mismatch = pd.DataFrame(mismatch_rows)
                    filt_only_bad = st.checkbox("📌 Prikaži samo problematične (bez promo na Glovu)", value=True, key="alert_filt")
                    if filt_only_bad:
                        df_mismatch = df_mismatch[~df_mismatch["Glovo Status"].str.startswith("✅")]

                    def _color_status(val):
                        if val.startswith("✅"): return "color: #27ae60; font-weight:bold"
                        if val.startswith("⚠️"): return "color: #e67e22; font-weight:bold"
                        return "color: #e74c3c; font-weight:bold"

                    st.dataframe(
                        df_mismatch.style.applymap(_color_status, subset=["Glovo Status"]),
                        use_container_width=True, hide_index=True, height=400
                    )
                    st.caption(f"Ukupno restorana sa Wolt promom: **{len(mismatch_rows)}** | Problematičnih: **{len(df_mismatch)}**")
                else:
                    st.success("✅ Nema restorana sa Wolt promom bez odgovarajućeg promo na Glovu!")

            st.markdown("---")
            st.markdown("#### 📧 Ručno pokretanje alerta")
            col_a1, col_a2 = st.columns([3, 1])
            with col_a1:
                st.info("🕘 Automatski alert se šalje svaki dan u **09:00** (zajedno sa dnevnim scanom gradova: Beograd, Novi Sad, Niš, Kragujevac).")
            with col_a2:
                if st.button("🚀 Pošalji alert ODMAH", type="primary", use_container_width=True, key="manual_alert_btn"):
                    if df.empty:
                        st.error("Nema scan podataka. Pokreni scan prvo.")
                    else:
                        with st.spinner("Šaljem alertove AM-ovima..."):
                            log_ph_alert = st.empty()
                            sent = run_promo_comparison_and_alert(df, log_ph_alert)
                            st.session_state.last_alerts = sent
                        if sent:
                            st.success(f"✅ Poslato {len(sent)} alerta!")
                        else:
                            st.info("Nema nepodudaranja ili AMM baza nema pokrivenih restorana.")
                        st.rerun()

            if st.session_state.get("last_alerts"):
                st.markdown("#### ✅ Poslednji poslati alerti")
                st.dataframe(pd.DataFrame(st.session_state.last_alerts), use_container_width=True, hide_index=True)

        # ══════════════════════════════════════════════════════════════════════
        # TAB: AM STATISTIKA
        # ══════════════════════════════════════════════════════════════════════
        with tab_stats:
            st.markdown("### 📈 Statistika po Account Manageru")
            alert_log = load_alert_log()

            if alert_log.empty:
                st.info("Još nema poslatih alerta. Statistika će se pojaviti posle prvog slanja.")
            else:
                alert_log["timestamp"] = pd.to_datetime(alert_log["timestamp"], errors="coerce")

                # ── Filter po datumu ─────────────────────────────────────────
                min_d = alert_log["timestamp"].min().date()
                max_d = alert_log["timestamp"].max().date()
                sc1, sc2 = st.columns(2)
                with sc1: date_from = st.date_input("Od:", min_d, min_value=min_d, max_value=max_d, key="stats_from")
                with sc2: date_to   = st.date_input("Do:", max_d, min_value=min_d, max_value=max_d, key="stats_to")
                mask = (alert_log["timestamp"].dt.date >= date_from) & (alert_log["timestamp"].dt.date <= date_to)
                filtered_log = alert_log[mask]

                if filtered_log.empty:
                    st.warning("Nema podataka za izabrani period.")
                else:
                    # ── KPI kartice ───────────────────────────────────────────
                    total_alerts = len(filtered_log)
                    unique_ams   = filtered_log["am_name"].nunique()
                    unique_rests = filtered_log["restaurant_display"].nunique()
                    unique_days  = filtered_log["timestamp"].dt.date.nunique()

                    k1, k2, k3, k4 = st.columns(4)
                    for col, label, val, color in [
                        (k1, "Ukupno alerta",    total_alerts,  "#e74c3c"),
                        (k2, "AM-ova obavešteno", unique_ams,   "#8e44ad"),
                        (k3, "Restorana",         unique_rests,  "#2980b9"),
                        (k4, "Dana sa alertima",  unique_days,   "#27ae60"),
                    ]:
                        with col:
                            st.markdown(f"""
                            <div class="kpi-card" style="border-bottom:4px solid {color};">
                                <div class="kpi-title">{label}</div>
                                <div class="kpi-value" style="color:{color};">{val}</div>
                            </div>""", unsafe_allow_html=True)

                    st.markdown("---")

                    # ── Tabela po AM-u ────────────────────────────────────────
                    st.markdown("#### 👤 Pregled po AM-u")
                    am_stats = (filtered_log
                        .groupby(["am_name", "am_email"])
                        .agg(
                            Broj_slanja   =("timestamp", lambda x: x.dt.date.nunique()),
                            Broj_restorana=("restaurant_display", "nunique"),
                            Ukupno_alerta =("restaurant_display", "count"),
                            Gradovi       =("city", lambda x: ", ".join(sorted(x.dropna().unique()))),
                            Poslednji_alert=("timestamp", "max"),
                        )
                        .reset_index()
                        .rename(columns={"am_name":"AM", "am_email":"Email"})
                        .sort_values("Ukupno_alerta", ascending=False)
                    )
                    am_stats["Poslednji_alert"] = am_stats["Poslednji_alert"].dt.strftime("%d.%m.%Y %H:%M")
                    st.dataframe(am_stats, use_container_width=True, hide_index=True)

                    # ── Grafikon: broj alerta po AM-u ─────────────────────────
                    fig_am = px.bar(
                        am_stats, x="AM", y="Ukupno_alerta",
                        color="AM", text="Ukupno_alerta",
                        title="Broj poslatih alerta po AM-u",
                        color_discrete_sequence=px.colors.qualitative.Set2
                    )
                    fig_am.update_layout(showlegend=False, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
                    fig_am.update_traces(textposition="outside")
                    st.plotly_chart(fig_am, use_container_width=True)

                    st.markdown("---")

                    # ── Timeline ─────────────────────────────────────────────
                    st.markdown("#### 📅 Timeline alerta")
                    daily = (filtered_log
                        .groupby([filtered_log["timestamp"].dt.date.rename("Datum"), "am_name"])
                        .size().reset_index(name="Broj alerta")
                    )
                    daily["Datum"] = pd.to_datetime(daily["Datum"])
                    fig_tl = px.line(daily, x="Datum", y="Broj alerta", color="am_name",
                                     markers=True, title="Dnevni alerti po AM-u",
                                     color_discrete_sequence=px.colors.qualitative.Set1)
                    fig_tl.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                                         hovermode="x unified", xaxis_title="")
                    fig_tl.update_xaxes(tickformat="%d.%m.%Y")
                    st.plotly_chart(fig_tl, use_container_width=True)

                    st.markdown("---")

                    # ── Detaljan log ──────────────────────────────────────────
                    st.markdown("#### 🗂️ Detaljan log alerta")
                    am_log_filter = st.selectbox("Filtriraj po AM-u:", ["Svi"] + sorted(filtered_log["am_name"].dropna().unique().tolist()), key="stats_am_filter")
                    log_view = filtered_log if am_log_filter == "Svi" else filtered_log[filtered_log["am_name"] == am_log_filter]
                    log_view = log_view.sort_values("timestamp", ascending=False).copy()
                    log_view["timestamp"] = log_view["timestamp"].dt.strftime("%d.%m.%Y %H:%M")
                    st.dataframe(log_view, use_container_width=True, hide_index=True, height=400)

                    # Eksport loga
                    csv_export = log_view.to_csv(index=False).encode("utf-8")
                    st.download_button("📥 Eksportuj log (CSV)", csv_export, "alert_log_export.csv", "text/csv")

        if st.session_state.get('pdf_files'):
            st.markdown("---"); st.subheader("📥 PDF Reports")
            pc = st.columns(4)
            for i, p in enumerate(st.session_state.pdf_files):
                with pc[i % 4]:
                    with open(p, "rb") as f: st.download_button(f"Download {os.path.basename(p)}", f.read(), os.path.basename(p), "application/pdf")
                    
        # VIDEO / HTML DEBUG DISPLAY (ONLY VISIBLE IF DEBUG CAUGHT SOMETHING)
        if st.session_state.get('error_screenshots'):
            st.markdown("---")
            st.error("⚠️ ATTENTION: The script logged potential issues or debug files. Check them below:")
            for media_path in st.session_state.error_screenshots:
                if media_path.endswith('.webm'):
                    st.video(media_path)
                elif media_path.endswith('.html'):
                    try:
                        with open(media_path, "rb") as f:
                            st.download_button(label=f"📥 Download HTML ({os.path.basename(media_path)})", data=f, file_name=os.path.basename(media_path), mime="text/html", key=media_path)
                    except: pass
                else:
                    st.image(media_path, caption=os.path.basename(media_path), use_container_width=True)

    if st.session_state.is_running:
        if auto_refresh:
            rem = int((sleep_interval * 60) - (time.time() - st.session_state.last_run))
            countdown_ph = st.sidebar.empty()
            while rem > 0:
                countdown_ph.info(f"⏳ Next auto-scan in: **{rem//60:02d}:{rem%60:02d}**")
                time.sleep(1); rem = int((sleep_interval * 60) - (time.time() - st.session_state.last_run))
            st.rerun()
        else: st.sidebar.success("✅ Scan completed. Click 'Start' for a new scan.")
        
else: 
    st.info("System is ready. Enter parameters in the left menu and click 'Start'.")
