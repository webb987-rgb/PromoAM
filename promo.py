def fetch_city(city: str, status_placeholder) -> list[dict]:
    """Skenira grad i odmah izvlači bedževe (akcije) iz Feed API-ja."""
    city_slug = CITY_SLUG_MAP.get(city, normalize(city).replace(" ", "-"))
    lat, lon  = CITY_COORDS.get(city, (44.8178, 20.4569))

    restaurants = {}
    skip = 0

    status_placeholder.info(f"🔍 Učitavam listu restorana i akcije za **{city}**...")

    for page_num in range(30):
        items_found = 0
        for endpoint in [
            f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}&skip={skip}",
            f"https://restaurant-api.wolt.com/v1/pages/delivery?lat={lat}&lon={lon}&skip={skip}",
        ]:
            data = wolt_get(endpoint)
            if not data:
                continue

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
                    rating = venue.get("rating") or {}
                    rating_score = rating.get("score", "-") if isinstance(rating, dict) else "-"
                    est = venue.get("estimate_range") or venue.get("estimate")
                    delivery_time = f"{est} min" if est else "-"

                    # ─────────────────────────────────────────────────────────
                    # NOVO: Čitamo SVE bedževe direktno iz feed-a
                    # ─────────────────────────────────────────────────────────
                    akcije_lista = []
                    
                    # 1. Provera 'badges' niza (npr. [{'text': '-20%'}, {'text': 'Wolt+'}])
                    badges = venue.get("badges", [])
                    for badge in badges:
                        txt = badge.get("text", "")
                        if txt:
                            akcije_lista.append(f"• {txt}")

                    # 2. Ponekad stave popust u 'label' ili 'short_description'
                    label = venue.get("label", "")
                    if label:
                        akcije_lista.append(f"• {label}")
                        
                    short_desc = venue.get("short_description", "")
                    if short_desc and any(k in short_desc.lower() for k in ["%", "popust", "rsd", "gratis", "akcija"]):
                        akcije_lista.append(f"• Info: {short_desc}")

                    # Uklanjamo duplikate i spajamo sve bedževe u jedan string
                    # Ako lista nije prazna spoji ih, u suprotnom stavi "-"
                    akcije_str = "\n".join(sorted(set(akcije_lista))) if akcije_lista else "-"

                    restaurants[slug] = {
                        "grad":       city,
                        "naziv":      name,
                        "slug":       slug,
                        "status":     status,
                        "ocena":      str(rating_score),
                        "dostava":    delivery_time,
                        "akcije":     akcije_str,
                        "link":       f"https://wolt.com/en/srb/{city_slug}/restaurant/{slug}",
                        "naziv_norm": normalize(name),
                    }
                    items_found += 1

            if items_found > 0:
                break  # Uspešno smo učitali podatke sa ovog endpointa, ne idi na sledeći

        status_placeholder.info(
            f"🚴 **{city}**: {len(restaurants)} restorana skenirano (stranica {page_num + 1})"
        )
        
        # Ako je našao manje od 10 novih restorana na stranici, to znači da smo stigli do kraja liste
        if items_found < 10:
            break
            
        skip += 40
        time.sleep(0.5)  # Mala pauza da nas ne blokiraju

    if not restaurants:
        status_placeholder.warning(f"⚠️ **{city}**: nije pronađen nijedan restoran.")
        return []

    # Pošto smo akcije već izvukli u letu, nema više potrebe za onim ThreadPoolExecutor
    # i višestrukim gađanjem API-ja koje puca! Odmah vraćamo listu.
    return list(restaurants.values())
