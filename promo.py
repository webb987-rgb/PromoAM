# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA A — Konstante i novi fajlovi (dodaj uz ostale Path konstante ~red 45)
# ═══════════════════════════════════════════════════════════════════════════════

LOCK_FILE          = Path("_scan_running.lock")
SENT_NEW_REST_FILE = Path("sent_new_restaurants.json")
ALERT_COOLDOWN_FILE= Path("alert_cooldown.json")
COOLDOWN_DAYS      = 7


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA B — Global scan lock pomoćne funkcije
#            Dodaj POSLE definicije LOCK_FILE, pre keep-alive sekcije
# ═══════════════════════════════════════════════════════════════════════════════

def acquire_scan_lock() -> bool:
    """Pokušaj da zauzmeš lock. Vraća True ako uspešno, False ako je već zauzet."""
    if LOCK_FILE.exists():
        # Proveri da li je lock "star" (crash bez cleanup-a) — stariji od 3h
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < 10800:  # 3 sata
                return False
        except Exception:
            pass
    try:
        LOCK_FILE.write_text(str(time.time()))
        return True
    except Exception:
        return False

def release_scan_lock():
    """Otpusti lock."""
    LOCK_FILE.unlink(missing_ok=True)

def is_scan_locked() -> bool:
    """Da li je scan trenutno u toku (od bilo kog korisnika)?"""
    if not LOCK_FILE.exists():
        return False
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
        return age < 10800
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA C — Deduplication novih restorana (dodaj posle lock funkcija)
# ═══════════════════════════════════════════════════════════════════════════════

def load_sent_new_restaurants() -> set:
    """Učitaj skup slug-ova koji su već poslati prodavcima."""
    if SENT_NEW_REST_FILE.exists():
        try:
            data = json.loads(SENT_NEW_REST_FILE.read_text(encoding="utf-8"))
            return set(data)
        except Exception:
            pass
    return set()

def save_sent_new_restaurants(slugs: set):
    """Sačuvaj skup slug-ova koji su poslati."""
    try:
        SENT_NEW_REST_FILE.write_text(
            json.dumps(list(slugs), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA D — AM alert cooldown (dodaj posle deduplication funkcija)
# ═══════════════════════════════════════════════════════════════════════════════

def load_alert_cooldown() -> dict:
    """
    Vraća dict: { "am_email|restaurant_norm": "2025-05-19" }
    Ključ je kombinacija AM email-a i normalizovanog naziva restorana.
    """
    if ALERT_COOLDOWN_FILE.exists():
        try:
            return json.loads(ALERT_COOLDOWN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_alert_cooldown(cooldown: dict):
    try:
        tmp = Path(str(ALERT_COOLDOWN_FILE) + ".tmp")
        tmp.write_text(json.dumps(cooldown, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ALERT_COOLDOWN_FILE)
    except Exception:
        pass

def is_in_cooldown(am_email: str, restaurant_norm: str, cooldown: dict) -> bool:
    """Da li je ovaj AM već dobio alert za ovaj restoran u poslednjih COOLDOWN_DAYS dana?"""
    key = f"{am_email}|{restaurant_norm}"
    last_sent_str = cooldown.get(key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.date.fromisoformat(last_sent_str)
        return (datetime.date.today() - last_sent).days < COOLDOWN_DAYS
    except Exception:
        return False

def update_cooldown(am_email: str, restaurant_norm: str, cooldown: dict):
    """Zabeleži danas kao dan slanja za ovaj AM+restoran par."""
    key = f"{am_email}|{restaurant_norm}"
    cooldown[key] = datetime.date.today().isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA E — Bulk mail prodavcima (zameni funkciju send_sales_notification)
# ═══════════════════════════════════════════════════════════════════════════════

# OBRIŠI staru send_sales_notification funkciju i zameni ovom:

def send_sales_bulk_notification(to_email: str, grad: str, novi_restorani: list[dict]) -> bool:
    """
    Šalje JEDAN mail prodavcu sa svim novim restoranima u gradu.
    novi_restorani = [{"naziv": str, "slug": str}, ...]
    """
    try:
        rows_html = ""
        for r in novi_restorani:
            naziv = r.get("naziv", "")
            slug  = r.get("slug", "")
            grad_slug = grad.lower().replace(" ", "-").replace("š", "s").replace("ć", "c").replace("č", "c")
            wolt_link = f"https://wolt.com/sr/srb/{grad_slug}/restaurant/{slug}"
            rows_html += f"""
            <tr>
              <td style='padding:10px 14px;border-bottom:1px solid #eee;font-weight:600'>{naziv}</td>
              <td style='padding:10px 14px;border-bottom:1px solid #eee'>
                <a href='{wolt_link}' style='color:#009de0'>{wolt_link}</a>
              </td>
            </tr>"""

        today_str = datetime.date.today().strftime("%d.%m.%Y")
        html = f"""
        <html><body style='font-family:Arial,sans-serif;background:#f5f5f5;padding:20px'>
          <div style='max-width:680px;margin:auto;background:#fff;border-radius:12px;
                      padding:28px;box-shadow:0 2px 8px rgba(0,0,0,0.08)'>
            <div style='background:#009de0;color:#fff;padding:16px 24px;border-radius:8px;
                        margin-bottom:20px;font-size:1.2rem;font-weight:700'>
              🆕 Novi restorani na Woltu — {grad} — {today_str}
            </div>
            <p>Detektovani su novi restorani u gradu <b>{grad}</b> koji nemaju dodeljenog Account Managera:</p>
            <table style='border-collapse:collapse;width:100%;font-size:14px'>
              <thead>
                <tr style='background:#f0f4ff'>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Restoran</th>
                  <th style='padding:10px 14px;text-align:left;color:#1a1a2e;border-bottom:2px solid #dde'>Wolt link</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <p style='margin-top:20px;color:#666;font-size:0.9rem'>
              Ukupno novih restorana: <b>{len(novi_restorani)}</b><br>
              Molimo dodelite odgovornog AM-a što pre.
            </p>
            <p style='font-size:11px;color:#999;margin-top:20px'>
              Automatski izveštaj &bull; Promo Monitor &bull; {local_now()}
            </p>
          </div>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = to_email
        msg["Subject"] = f"🆕 {len(novi_restorani)} novi restoran(a) — {grad} — {today_str}"
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, to_email, msg.as_string())
        return True
    except Exception as e:
        _log_fetch(f"SALES BULK MAIL greška → {to_email}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA F — load_scheduler_config: default enabled = False
#            Zameni samo tu jednu liniju u funkciji
# ═══════════════════════════════════════════════════════════════════════════════

def load_scheduler_config() -> dict:
    if SCHEDULER_FILE.exists():
        try:
            return json.loads(SCHEDULER_FILE.read_text())
        except Exception:
            pass
    # ← IZMENA: enabled: False umesto True
    return {"enabled": False, "hour": 8, "minute": 0, "cities": CITIES}


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA G — run_scheduled_scan_and_send: lock + cooldown + bulk mail
#            Zameni celu funkciju
# ═══════════════════════════════════════════════════════════════════════════════

def run_scheduled_scan_and_send():
    import logging
    log = logging.getLogger("scheduler")
    cfg = load_scheduler_config()
    if not cfg.get("enabled"):
        return

    # ── LOCK: ne pokreći ako je već aktivan scan ─────────────────────────────
    if not acquire_scan_lock():
        log.warning("[Scheduler] Scan već u toku, preskačem zakazani sken.")
        return

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
        release_scan_lock()
        return
    finally:
        pass  # lock se pušta na kraju, posle slanja mailova

    if df.empty:
        release_scan_lock()
        return

    save_scan(df)

    # ── SALES: bulk mail o novim restoranima ──────────────────────────────────
    sent_slugs  = load_sent_new_restaurants()
    amm_df_curr = load_amm()
    sales_cfg   = load_sales()
    novi_df     = df[df["novo"] == "Da"].copy() if "novo" in df.columns else pd.DataFrame()

    if not novi_df.empty:
        # Grupiši po gradu, filtriraj već poslate i one sa AM-om
        novi_po_gradu = {}  # grad → [{"naziv": ..., "slug": ...}]
        new_sent_slugs = set(sent_slugs)

        for _, row in novi_df.iterrows():
            naziv = row.get("naziv", "")
            grad  = row.get("grad", "")
            slug  = row.get("slug", "")
            norm  = normalize(naziv)

            # Preskoči ako je već poslat
            if slug in sent_slugs:
                continue

            # Preskoči ako već ima AM-a
            has_am = False
            if not amm_df_curr.empty:
                has_am = not amm_df_curr[
                    (amm_df_curr["restaurant_norm"] == norm) &
                    (amm_df_curr["city"] == grad)
                ].empty

            if not has_am:
                if grad not in novi_po_gradu:
                    novi_po_gradu[grad] = []
                novi_po_gradu[grad].append({"naziv": naziv, "slug": slug})
                new_sent_slugs.add(slug)

        # Pošalji jedan mail po gradu
        for grad, restorani in novi_po_gradu.items():
            for email in sales_cfg.get(grad, []):
                ok = send_sales_bulk_notification(email, grad, restorani)
                if ok:
                    log.info(f"[Scheduler] Bulk sales mail → {email} ({grad}): {len(restorani)} restorana")

        # Sačuvaj ažurirani set poslatih slug-ova
        save_sent_new_restaurants(new_sent_slugs)

    # ── AM ALERTOVI sa cooldown-om ─────────────────────────────────────────────
    if amm_df_curr.empty:
        release_scan_lock()
        return

    df["naziv_norm"] = df["naziv"].apply(normalize)
    merged = df.merge(
        amm_df_curr[["restaurant_norm", "restaurant_display", "city", "am_name", "am_email"]],
        left_on="naziv_norm", right_on="restaurant_norm", how="inner"
    )

    cooldown = load_alert_cooldown()
    sent_log = []

    for (am_name, am_email_addr), grp in merged.groupby(["am_name", "am_email"]):
        alerts = []
        for _, row in grp.iterrows():
            akcije_filtered = filter_akcije_for_email(row["akcije"])
            if akcije_filtered == "-":
                continue
            rest_norm = normalize(row["naziv"])
            # Preskoči ako je u cooldown periodu
            if is_in_cooldown(am_email_addr, rest_norm, cooldown):
                continue
            alerts.append({
                "naziv": row["naziv"],
                "grad":  row["grad"],
                "akcije": row["akcije"],
                "link":  row.get("link", ""),
                "norm":  rest_norm,
            })

        if not alerts:
            continue

        ok = send_alert_email(am_email_addr, am_name, alerts)
        if ok:
            for a in alerts:
                # Zabeleži cooldown
                update_cooldown(am_email_addr, a["norm"], cooldown)
                sent_log.append({
                    "timestamp":          local_now(),
                    "city":               a["grad"],
                    "restaurant_display": a["naziv"],
                    "am_name":            am_name,
                    "am_email":           am_email_addr,
                    "akcije":             a["akcije"],
                })

    save_alert_cooldown(cooldown)
    if sent_log:
        append_alert_log(sent_log)

    release_scan_lock()


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA H — UI: lock provera pri manuelnom pokretanju scana
#
# U tab_scan bloku, zameni oba `if run_scan ...` i `if run_nopromo ...` bloka.
# Dodaj lock check NA SAMOM POČETKU svakog bloka, pre svega ostalog:
# ═══════════════════════════════════════════════════════════════════════════════

# Za `if run_scan and selected_cities and not st.session_state.scan_running:` blok,
# dodaj na početku:

    if is_scan_locked() and not st.session_state.scan_running:
        st.error("⛔ Scan je već aktivan (drugi korisnik ili zakazani sken). Pokušaj malo kasnije.")
    elif run_scan and selected_cities and not st.session_state.scan_running:
        if not acquire_scan_lock():
            st.error("⛔ Nije moguće pokrenuti scan — već je aktivan. Pokušaj malo kasnije.")
        else:
            st.session_state.scan_stop_event = threading.Event()
            st.session_state.scan_running = True
            # ... ostatak koda ostaje isti ...

            def _run_scan_bg():
                try:
                    # ... isti kod kao pre ...
                    pass
                finally:
                    release_scan_lock()  # ← DODAJ OVO na kraj background funkcije

# Ista logika za `if run_nopromo ...` blok — acquire na početku, release u finally


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA I — Ručno slanje alerta iz tab_alert: dodaj cooldown proveru
#
# U tab_alert, zameni blok koji šalje mailove (`if st.button("🚀 Pošalji alertove")`):
# ═══════════════════════════════════════════════════════════════════════════════

            if st.button("🚀 Pošalji alertove", type="primary"):
                cooldown = load_alert_cooldown()
                am_groups = preview.groupby(["am_name", "am_email"])
                sent_log = []
                success_count = 0
                skipped_count = 0

                for (am_name, am_email_addr), grp in am_groups:
                    alerts = []
                    for _, row in grp.iterrows():
                        rest_norm = normalize(row["naziv"])
                        if is_in_cooldown(am_email_addr, rest_norm, cooldown):
                            skipped_count += 1
                            continue
                        alerts.append({
                            "naziv": row["naziv"], "grad": row["grad"],
                            "akcije": row["akcije"], "link": row.get("link", ""),
                            "norm": rest_norm,
                        })

                    if not alerts:
                        continue

                    ok = send_alert_email(am_email_addr, am_name, alerts)
                    if ok:
                        success_count += 1
                        st.success(f"✅ Mail poslat: **{am_name}** – {len(alerts)} partnera")
                        for a in alerts:
                            update_cooldown(am_email_addr, a["norm"], cooldown)
                            sent_log.append({
                                "timestamp": local_now(), "city": a["grad"],
                                "restaurant_display": a["naziv"], "am_name": am_name,
                                "am_email": am_email_addr, "akcije": a["akcije"],
                            })
                    else:
                        st.error(f"❌ Greška: {am_name}")

                save_alert_cooldown(cooldown)
                if sent_log:
                    append_alert_log(sent_log)
                if skipped_count:
                    st.info(f"ℹ️ {skipped_count} partnera preskočeno (cooldown {COOLDOWN_DAYS} dana).")
                st.markdown(f"**Završeno:** {success_count}/{am_groups.ngroups} AM-ova kontaktirano.")


# ═══════════════════════════════════════════════════════════════════════════════
# IZMENA J — scan rezultati: bulk sales mail (zameni blok posle save_scan u UI)
#
# U tab_scan, posle `save_scan(df_result)`, zameni blok za novi restorani:
# ═══════════════════════════════════════════════════════════════════════════════

            # STARI KOD (obriši sve od "# Obavesti sales o novim restoranima" do kraja if bloka):
            # novi_df = df_result[df_result["novo"] == "Da"] if "novo" in df_result.columns else pd.DataFrame()
            # if not novi_df.empty:
            #     ... (stari individualni mail kod) ...

            # NOVI KOD:
            novi_df = df_result[df_result["novo"] == "Da"].copy() if "novo" in df_result.columns else pd.DataFrame()
            if not novi_df.empty:
                sent_slugs  = load_sent_new_restaurants()
                amm_check   = load_amm()
                sales_cfg   = load_sales()
                novi_po_gradu = {}
                new_sent_slugs = set(sent_slugs)

                for _, row in novi_df.iterrows():
                    naziv = row.get("naziv", "")
                    grad  = row.get("grad", "")
                    slug  = row.get("slug", "")
                    norm  = normalize(naziv)
                    if slug in sent_slugs:
                        continue
                    has_am = False
                    if not amm_check.empty:
                        has_am = not amm_check[
                            (amm_check["restaurant_norm"] == norm) & (amm_check["city"] == grad)
                        ].empty
                    if not has_am:
                        if grad not in novi_po_gradu:
                            novi_po_gradu[grad] = []
                        novi_po_gradu[grad].append({"naziv": naziv, "slug": slug})
                        new_sent_slugs.add(slug)

                notified = 0
                for grad, restorani in novi_po_gradu.items():
                    for email in sales_cfg.get(grad, []):
                        if send_sales_bulk_notification(email, grad, restorani):
                            notified += 1

                save_sent_new_restaurants(new_sent_slugs)
                if notified:
                    st.info(f"📬 Poslato **{notified}** bulk obaveštenja sales agentima ({sum(len(v) for v in novi_po_gradu.values())} novih restorana).")
