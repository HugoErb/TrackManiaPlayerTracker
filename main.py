import re
import json
import time
from datetime import date
from pathlib import Path

from dateutil.relativedelta import relativedelta
from tqdm import tqdm
from constants import *

import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

def _extract_int(text: str, default=None):
    if not text:
        return default
    m = re.search(r"\d+", text.replace("\u202f","").replace("\xa0",""))
    return int(m.group(0)) if m else default

def fetch_online_records(maps):
    """
    maps: liste de tuples (name, href)
    Règles:
      - records >  MAX_RECORDS -> append nom dans forbidden_file
      - records <  MIN_RECORDS -> ignore
      - MIN_RECORDS <= records <= MAX_RECORDS -> print("OK") + retourne en éligibles
    Retourne: [(name, href, records), ...] pour les maps éligibles
    """
    eligible = []

    # 1) Charger les interdits existants pour ne pas dupliquer
    path = Path(FORBIDDEN_MAP_FILE_NAME)
    existing_forbidden = set()
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            existing_forbidden = {
                line.strip().casefold()
                for line in f
                if line.strip()
            }

    # 2) Ouvrir en append (ne réinitialise pas le fichier)
    with path.open("a", encoding="utf-8") as forb:
        for idx, (name, href) in enumerate(
                tqdm(maps, total=len(maps), desc="Lecture des Online records", unit="map"),
                start=1
        ):
            try:
                page.goto(href, wait_until="load")
                page.wait_for_load_state("networkidle")
            except PlaywrightTimeoutError:
                continue

            # Lire le nombre d'Online records
            records = None
            try:
                btn = page.locator("button[role='button'][data-bs-toggle='tab'][data-bs-target='#onlinerecs']").first
                if btn.count() > 0:
                    sp = btn.locator("span[template='FormatCountShort']").first
                    if sp.count() > 0:
                        records = _extract_int(sp.inner_text())
                    if records is None:
                        records = _extract_int(btn.inner_text())
                if records is None:
                    btn2 = page.get_by_role("button", name=re.compile(r"Online records", re.I)).first
                    if btn2.count() > 0:
                        records = _extract_int(btn2.inner_text())
            except Exception:
                pass

            if records is None:
                continue

                # > MAX_RECORDS -> ajouter SANS doublon
            if records > MAX_RECORDS:
                key = name.strip().casefold()
                if key not in existing_forbidden:
                    forb.write(f"{name}\n")  # append seulement si absent
                    existing_forbidden.add(key)
                continue

                # < MIN_RECORDS -> ignorer
            if records < MIN_RECORDS:
                continue

                # Entre les bornes -> OK
            eligible.append((name, href, records))

    return eligible

def get_maps(url: str):
    items = []
    seen_links = set()

    page.goto(url, wait_until="load")
    page.wait_for_load_state("networkidle")

    # Cookies
    try:
        for sel in [
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('I Accept')",
            "button:has-text('Agree')",
        ]:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                page.wait_for_load_state("networkidle")
                break
    except Exception:
        pass

    page_index = 1
    while True:
        # --- attendre la liste
        try:
            page.wait_for_selector("a[title][href^='/mapshow/']", state="visible", timeout=30000)
        except PlaywrightTimeoutError:
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("a[title][href^='/mapshow/']", state="visible", timeout=15000)

        # --- collecter cette page (UNE seule fois)
        links = page.locator("a[title][href^='/mapshow/']")
        count = links.count()

        for i in range(count):
            a = links.nth(i)
            name = (a.get_attribute("title") or a.inner_text()).strip()
            href = a.get_attribute("href") or ""
            if not name or not href:
                continue
            if href.startswith("/"):
                href = BASE_MAP_URL + href
            if href not in seen_links:
                items.append((name, href))
                seen_links.add(href)

        # --- si moins de 40 maps, on considère que c'est la dernière page
        if count < 40:
            break

        # --- pagination: "Next page" (URL change avec &from=ID)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(150)

        next_link = page.locator("a:has-text('Next page')").first
        if next_link.count() == 0 or not next_link.is_visible():
            break

        # si désactivé
        parent_li = next_link.locator("xpath=ancestor::li[contains(@class,'disabled')]")
        aria_dis = (next_link.get_attribute("aria-disabled") or "false").lower()
        if parent_li.count() > 0 or aria_dis == "true":
            break

        # mémoriser URL + premier lien pour détecter le changement
        prev_url = page.url
        prev_first = links.first.get_attribute("href") if count > 0 else None

        # clic (UNE seule fois) puis attendre vrai changement
        next_link.click()
        page.wait_for_load_state("networkidle")
        try:
            page.wait_for_function(
                """
                (prevUrl, prevFirst) => {
                    const changedUrl = window.location.href !== prevUrl;
                    const a = document.querySelector('a[title][href^="/mapshow/"]');
                    const changedFirst = a && a.getAttribute('href') !== prevFirst;
                    return changedUrl || changedFirst;
                }
                """,
                arg=[prev_url, prev_first],
                timeout=15000
            )
        except PlaywrightTimeoutError:
            break

        page_index += 1

    return items

def filter_maps_with_forbidden(maps):
    from pathlib import Path
    path = Path(FORBIDDEN_MAP_FILE_NAME)
    if not path.exists():
        return maps
    with path.open("r", encoding="utf-8") as f:
        forbidden = {line.strip().casefold() for line in f if line.strip()}
    return [(n, h) for (n, h) in maps if n.strip().casefold() not in forbidden]

def get_periode_quatre_mois():
    aujourd_hui = date.today()
    il_y_a_4_mois = aujourd_hui - relativedelta(months=INTERVAL_MONTH_NUMBER)
    return f"{il_y_a_4_mois:%Y-%m-%d}...{aujourd_hui:%Y-%m-%d}"


if __name__ == '__main__':

    # Init
    start_time = time.time()
    date_interval = get_periode_quatre_mois()
    output_file = SEEKED_PLAYER + "_" + date_interval + ".txt"
    url = MAP_LIST_URL + date_interval

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        print(f"Récupération des maps en cours...")
        all_maps = get_maps(url)
        print(f"\n{len(all_maps)} maps trouvées en {time.time() - start_time:.2f} sec.")
        all_maps = filter_maps_with_forbidden(all_maps)
        print(f"Filtrage des maps effectuées, {len(all_maps)} restante(s).\n")

        eligible_maps = fetch_online_records(all_maps)
        for name, href, records in eligible_maps:
            print(f"- {records} — {name} -> {href}")
        print(f"\n{len(eligible_maps)} maps éligibles trouvées en {time.time() - start_time:.2f} sec.")

        print(f"\nRecherche de {SEEKED_PLAYER} dans les maps récupérées en cours...")
        print(f"Rapport enregistré dans '{output_file}' en {time.time() - start_time:.2f} sec.\n")

        ctx.close()
        browser.close()
