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

def find_player_on_tmio(page, tmio_url: str, player_name: str, max_load_more: int = 50) -> bool:
    """
    Ouvre le leaderboard trackmania.io, clique 'Load more...' (avec tqdm),
    puis scanne la liste des joueurs (avec tqdm) pour trouver player_name.
    Retourne True si trouvé.
    """
    rows_sel = "table.table.is-fullwidth.is-striped tbody tr"

    # Aller sur l'URL (SPA avec #)
    page.goto(tmio_url, wait_until="load")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector(rows_sel, timeout=30000)

    # --- tqdm: chargement du leaderboard (Load more...)
    prev = page.locator(rows_sel).count()
    t_load = tqdm(total=0, desc="Load leaderboard", unit="rows", leave=False)
    for _ in range(max_load_more):
        btn = page.locator("button:has-text('Load more')").first
        if btn.count() == 0 or not btn.is_enabled():
            break
        rows_before = prev
        btn.click()
        page.wait_for_load_state("networkidle")
        try:
            page.wait_for_function(
                """
                (prev) => document.querySelectorAll('table.table.is-fullwidth.is-striped tbody tr').length > prev
                """,
                arg=rows_before,
                timeout=15000
            )
        except Exception:
            break
        prev = page.locator(rows_sel).count()
        t_load.update(max(0, prev - rows_before))
    t_load.close()

    # --- tqdm: scan des joueurs
    cells = page.locator(f"{rows_sel} td:nth-child(2)")
    n = cells.count()
    target = player_name.strip().casefold()
    t_scan = tqdm(total=n, desc="Scan joueurs", unit="joueur", leave=False)
    for i in range(n):
        try:
            txt = cells.nth(i).inner_text().strip()
            norm = " ".join(txt.split()).casefold()
            if target in norm:
                t_scan.update(n - t_scan.n)
                t_scan.close()
                return True
        except Exception:
            pass
        t_scan.update(1)
    t_scan.close()
    return False

def fetch_online_records(maps):
    """
    Pour chaque map:
      - lit Online records sur TMX
      - si > MAX_RECORDS -> ajoute le nom dans forbidden (sans doublon)
      - si < MIN_RECORDS -> ignore
      - sinon -> récupère le lien 'View more on trackmania.io', ouvre, charge tout, cherche TRACKED_PLAYER
    Retourne: [(name, href_tmx, records, href_tmio, found_bool), ...]
    """
    eligible = []

    # Interdits existants (pour ne pas dupliquer)
    path = Path(FORBIDDEN_MAP_FILE_NAME)
    existing_forbidden = set()
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            existing_forbidden = {line.strip().casefold() for line in f if line.strip()}

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

            # Online records
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

            # > MAX_RECORDS -> mettre dans forbidden (sans doublon)
            if records > MAX_RECORDS:
                key = name.strip().casefold()
                if key not in existing_forbidden:
                    forb.write(f"{name}\n")
                    existing_forbidden.add(key)
                continue

            # < MIN_RECORDS -> ignorer
            if records < MIN_RECORDS:
                continue

            # Éligible : récupérer le lien trackmania.io
            tmio_url = None
            try:
                link = page.locator("a.btn.btn-link[href*='trackmania.io/#/leaderboard/']").first
                if link.count() == 0:
                    link = page.locator("a.btn.btn-link:has-text('View more on trackmania.io')").first
                if link.count() > 0:
                    tmio_url = link.get_attribute("href")
            except Exception:
                tmio_url = None

            found = False
            if tmio_url:
                try:
                    found = find_player_on_tmio(page, tmio_url, TRACKED_PLAYER, max_load_more=50)
                except Exception:
                    found = False

            eligible.append((name, href, records, tmio_url or "", found))

    return eligible



if __name__ == '__main__':

    # Init
    start_time = time.time()
    date_interval = get_periode_quatre_mois()
    output_file = "reports/" + TRACKED_PLAYER + "_" + date_interval + ".txt"
    url = MAP_LIST_URL + date_interval

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        print(f"Récupération des maps en cours...")
        all_maps = get_maps(url)
        print(f"\n{len(all_maps)} maps trouvées en {time.time() - start_time:.2f} sec.")
        all_maps = filter_maps_with_forbidden(all_maps)
        # all_maps = all_maps[:10]
        print(f"Filtrage des maps effectué, {len(all_maps)} restante(s).\n")

        eligible_maps = fetch_online_records(all_maps)
        print(f"\n{len(eligible_maps)} maps éligibles trouvées en {time.time() - start_time:.2f} sec.")

        print(f"\nRecherche de {TRACKED_PLAYER} dans les maps récupérées en cours...")
        hits = [e for e in eligible_maps if e[-1] is True]
        with open(output_file, "a", encoding="utf-8") as rep:
            for name, href_tmx, records, href_tmio, found in hits:
                rep.write(f"{records:>4} — {name}\n")
                rep.write(f"TMX : {href_tmx}\n")
                rep.write(f"TMIO: {href_tmio}\n\n")

        print(f"\n{len(hits)} map(s) où '{TRACKED_PLAYER}' apparaît.")
        print(f"Rapport enregistré dans '{output_file}' en {time.time() - start_time:.2f} sec.\n")

        ctx.close()
        browser.close()
