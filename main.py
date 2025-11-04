import re
import json
import time
from datetime import date, datetime
from pathlib import Path

from dateutil.relativedelta import relativedelta
from tqdm import tqdm
from constants import *

import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BLOCKED_RESOURCE_TYPES = {
    "image", "font", "media", "websocket", "eventsource", "other"
}

BLOCKED_DOMAINS = [
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "facebook.net",
    "cdn.onesignal.com",
    "hotjar.com",
    "segment.com",
    "mixpanel.com",
    "clarity.ms",
]

def _extract_int(text: str, default=None):
    """
    Extrait un entier uniquement si le texte ne contient que des chiffres
    (espaces normaux, insécables et fines insécables supprimés).
    Sinon, retourne `default` (None par défaut).
    """
    if not text:
        return default
    # Supprime les espaces normaux, insécables (\xa0) et fines insécables (\u202f)
    s = re.sub(r"[\s\u00a0\u202f]", "", text)
    # Refuse tout ce qui n'est pas strictement numérique (ex: '52k' -> None)
    if not s.isdigit():
        return default
    return int(s)


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

def get_periode() -> str:
    """
    Calcule une période 'YYYY-MM-DD...YYYY-MM-DD' selon les règles décrites ci-dessus.
    """
    today = date.today()
    d_start = _parse_date_or_none(START_DATE)
    d_end = _parse_date_or_none(END_DATE)

    if d_start is None:
        # START_DATE manquante -> on s'appuie sur END_DATE (ou today) et l'intervalle
        if d_end is None:
            d_end = today
        d_start = d_end - relativedelta(months=INTERVAL_MONTHS, days=INTERVAL_DAYS)
    else:
        # START_DATE fournie -> END_DATE = fournie ou today par défaut
        if d_end is None:
            d_end = today

    # Sécurité : s'assurer que début ≤ fin
    if d_start > d_end:
        d_start, d_end = d_end, d_start

    return f"{d_start:%Y-%m-%d}...{d_end:%Y-%m-%d}"

def _parse_date_or_none(s: str):
    """Retourne une date si s est non vide et bien formatée (YYYY-MM-DD), sinon None."""
    s = (s or "").strip()
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()

def find_player_on_tmio(page, tmio_url: str, player_name: str, max_load_more: int = 50) -> bool:
    """
    Ouvre le leaderboard trackmania.io, clique 'Load more...' jusqu'à ce qu'il n'y en ait plus
    (ou jusqu'à max_load_more), puis scanne la liste des joueurs pour trouver player_name.
    Retourne True si trouvé.
    """
    rows_sel = "table.table.is-fullwidth.is-striped tbody tr"

    # Aller sur l'URL (SPA avec #)
    page.goto(tmio_url, wait_until="load")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector(rows_sel, timeout=30000)

    # Charger le leaderboard (sans tqdm)
    prev = page.locator(rows_sel).count()
    for _ in range(max_load_more):
        btn = page.locator("button:has-text('Load more')").first
        if btn.count() == 0 or not btn.is_enabled():
            break
        rows_before = prev
        btn.click()
        page.wait_for_load_state("networkidle")
        try:
            page.wait_for_function(
                "(prev) => document.querySelectorAll('table.table.is-fullwidth.is-striped tbody tr').length > prev",
                arg=rows_before,
                timeout=15000
            )
        except Exception:
            break
        prev = page.locator(rows_sel).count()

    # Scan des joueurs (sans tqdm)
    target = player_name.strip().casefold()
    cells = page.locator(f"{rows_sel} td:nth-child(2)")
    n = cells.count()
    for i in range(n):
        try:
            txt = cells.nth(i).inner_text().strip()
            norm = " ".join(txt.split()).casefold()
            if target in norm:
                return True
        except Exception:
            pass

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
            tqdm(maps, total=len(maps), desc=f"2eme filtre des maps + recherche de {TRACKED_PLAYER} ", unit="map"),
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

def should_block_request(req):
    rt = req.resource_type
    if rt in BLOCKED_RESOURCE_TYPES:
        return True
    url = req.url
    return any(d in url for d in BLOCKED_DOMAINS)

if __name__ == '__main__':

    # Init
    start_time = time.time()
    date_interval = get_periode()
    output_file = "reports/" + TRACKED_PLAYER + "_" + date_interval + ".txt"
    url = MAP_LIST_URL + date_interval

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,  # souvent nécessaire pour contourner Cloudflare / anti-bot
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            # service_workers="block",  # active si ta version Playwright le supporte
        )

        # Petites astuces "stealth"
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)

        # Bloquer images/polices/media/ws + domaines tracking
        ctx.route("**/*", lambda route: route.abort() if should_block_request(route.request)
                  else route.continue_())

        page = ctx.new_page()
        page.set_default_timeout(10_000)             # 10 s pour les attentes DOM
        page.set_default_navigation_timeout(15_000)  # 15 s pour les navigations

        print(f"Récupération des maps sur la période {date_interval.replace('...',' à ')}...")
        all_maps = get_maps(url)
        print(f"\n{len(all_maps)} maps trouvées en {time.time() - start_time:.2f} sec.")
        all_maps = filter_maps_with_forbidden(all_maps)
        # all_maps = all_maps[:10]
        print(f"Filtrage des maps effectué, {len(all_maps)} restante(s).\n")

        eligible_maps = fetch_online_records(all_maps)
        print(f"\n{len(eligible_maps)} maps éligibles trouvées en {time.time() - start_time:.2f} sec.")

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

