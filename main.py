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

def dump_maps_list(maps, filepath, header=""):
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        if header:
            f.write(header.strip() + "\n\n")
        for name, href in maps:
            f.write(f"{name}\n{href}\n\n")


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
    Retourne:
      eligible: [(name, href_tmx, records, href_tmio, found_bool), ...]
      excluded_records: [(name, href_tmx, records, reason), ...]  # seulement MIN/MAX
      excluded_other: [(name, href_tmx, reason, details), ...]    # no_records/no_tmio/timeouts...
    """
    eligible = []
    excluded_records = []
    excluded_other = []

    # Interdits existants (pour ne pas dupliquer)
    path = Path(FORBIDDEN_MAP_FILE_NAME)
    existing_forbidden = set()
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            existing_forbidden = {line.strip().casefold() for line in f if line.strip()}

    with path.open("a", encoding="utf-8") as forb:
        for (name, href) in tqdm(
            maps,
            total=len(maps),
            desc=f"2eme filtre des maps + recherche de {TRACKED_PLAYER} ",
            unit="map"
        ):
            try:
                page.goto(href, wait_until="load")
                page.wait_for_load_state("networkidle")
            except PlaywrightTimeoutError:
                excluded_other.append((name, href, "NAV_TIMEOUT_TMX", "Navigation timeout sur TMX"))
                continue
            except Exception as e:
                excluded_other.append((name, href, "NAV_ERROR_TMX", repr(e)))
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
            except Exception as e:
                excluded_other.append((name, href, "RECORDS_PARSE_ERROR", repr(e)))
                continue

            if records is None:
                excluded_other.append((name, href, "NO_RECORDS_FOUND", "Impossible de lire 'Online records'"))
                continue

            # > MAX_RECORDS -> forbidden + exclusion records
            if records > MAX_RECORDS:
                key = name.strip().casefold()
                if key not in existing_forbidden:
                    forb.write(f"{name}\n")
                    existing_forbidden.add(key)
                excluded_records.append((name, href, records, f">{MAX_RECORDS}"))
                continue

            # < MIN_RECORDS -> exclusion records
            if records < MIN_RECORDS:
                excluded_records.append((name, href, records, f"<{MIN_RECORDS}"))
                continue

            # Éligible : récupérer le lien trackmania.io
            tmio_url = ""
            try:
                link = page.locator("a.btn.btn-link[href*='trackmania.io/#/leaderboard/']").first
                if link.count() == 0:
                    link = page.locator("a.btn.btn-link:has-text('View more on trackmania.io')").first
                if link.count() > 0:
                    tmio_url = link.get_attribute("href") or ""
            except Exception as e:
                excluded_other.append((name, href, "TMIO_LINK_ERROR", repr(e)))
                continue

            if not tmio_url:
                excluded_other.append((name, href, "NO_TMIO_LINK", f"records={records}"))
                continue

            found = False
            try:
                found = find_player_on_tmio(page, tmio_url, TRACKED_PLAYER, max_load_more=50)
            except PlaywrightTimeoutError:
                excluded_other.append((name, href, "TMIO_TIMEOUT", f"records={records} | tmio={tmio_url}"))
                continue
            except Exception as e:
                excluded_other.append((name, href, "TMIO_ERROR", f"{repr(e)} | tmio={tmio_url}"))
                continue

            eligible.append((name, href, records, tmio_url, found))

    return eligible, excluded_records, excluded_other

def should_block_request(req):
    rt = req.resource_type
    if rt in BLOCKED_RESOURCE_TYPES:
        return True
    url = req.url
    return any(d in url for d in BLOCKED_DOMAINS)

if __name__ == '__main__':

    start_time = time.time()
    date_interval = get_periode()
    url = MAP_LIST_URL + date_interval

    # Un seul report
    final_report = f"reports/report_{date_interval}.txt"
    Path("reports").mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
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
        )

        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)

        ctx.route("**/*", lambda route: route.abort() if should_block_request(route.request)
                  else route.continue_())

        page = ctx.new_page()
        page.set_default_timeout(10_000)
        page.set_default_navigation_timeout(15_000)

        print(f"Récupération des maps sur la période {date_interval.replace('...',' à ')}...")
        all_maps = get_maps(url)
        print(f"\n{len(all_maps)} maps trouvées en {time.time() - start_time:.2f} sec.")

        # -------- Filtre 1 : forbidden (blacklist)
        all_maps_before = all_maps
        maps_after_forbidden = filter_maps_with_forbidden(all_maps)

        before_dict = {n.strip().casefold(): (n, h) for n, h in all_maps_before}
        after_keys = {n.strip().casefold() for n, _ in maps_after_forbidden}
        excluded_forbidden = [before_dict[k] for k in (before_dict.keys() - after_keys)]

        print(f"Filtrage blacklist effectué, {len(maps_after_forbidden)} restante(s).\n")

        # -------- Filtre 2 : records + recherche TMIO
        eligible_maps, excluded_records, excluded_other = fetch_online_records(maps_after_forbidden)

        print(f"\n{len(eligible_maps)} maps éligibles trouvées en {time.time() - start_time:.2f} sec.")
        print(f"Report unique écrit dans: {final_report}")

        # -------- Hits (joueur trouvé)
        hits = [e for e in eligible_maps if e[-1] is True]

        # -------- Ecriture du report unique
        with open(final_report, "w", encoding="utf-8") as f:
            f.write(f"Période: {date_interval}\n")
            f.write(f"TRACKED_PLAYER: {TRACKED_PLAYER}\n")
            f.write(f"MIN_RECORDS={MIN_RECORDS} | MAX_RECORDS={MAX_RECORDS}\n")
            f.write(f"Temps total: {time.time() - start_time:.2f} sec.\n")
            f.write("\n" + "="*80 + "\n")
            f.write("1) MAPS TROUVÉES SUR LA PÉRIODE (avant filtres)\n")
            f.write("="*80 + "\n\n")
            for name, href in all_maps:
                f.write(f"{name}\n{href}\n\n")

            f.write("\n" + "="*80 + "\n")
            f.write("2) MAPS EXCLUES (blacklist / forbidden)\n")
            f.write("="*80 + "\n\n")
            if excluded_forbidden:
                for name, href in excluded_forbidden:
                    f.write(f"{name}\n{href}\nREASON: FORBIDDEN_LIST\n\n")
            else:
                f.write("Aucune\n\n")

            f.write("\n" + "="*80 + "\n")
            f.write("3) MAPS EXCLUES (online records hors plage)\n")
            f.write("="*80 + "\n\n")
            if excluded_records:
                # tri pour lecture
                for name, href, records, why in sorted(excluded_records, key=lambda x: (x[3], x[2])):
                    f.write(f"{name}\n")
                    f.write(f"{records} records\n")
                    f.write(f"{href}\n")
                    f.write(f"REASON: RECORDS_{why} (MIN={MIN_RECORDS}, MAX={MAX_RECORDS})\n\n")
            else:
                f.write("Aucune\n\n")

            f.write("\n" + "="*80 + "\n")
            f.write("4) MAPS ÉLIGIBLES À LA RECHERCHE FINALE (records OK + lien TMIO)\n")
            f.write("="*80 + "\n\n")
            if eligible_maps:
                for name, href_tmx, records, href_tmio, found in eligible_maps:
                    f.write(f"{name}\n")
                    f.write(f"{records} records\n")
                    f.write(f"TMX : {href_tmx}\n")
                    f.write(f"TMIO: {href_tmio}\n")
                    f.write(f"FOUND({TRACKED_PLAYER}): {found}\n\n")
            else:
                f.write("Aucune\n\n")

            # Optionnel mais très utile : les exclusions techniques (no_records/no_tmio/timeouts)
            f.write("\n" + "="*80 + "\n")
            f.write("5) EXCLUSIONS TECHNIQUES\n")
            f.write("="*80 + "\n\n")
            if excluded_other:
                for name, href, reason, details in excluded_other:
                    f.write(f"{name}\n{href}\nREASON: {reason}\nDETAILS: {details}\n\n")
            else:
                f.write("Aucune\n\n")

            f.write("\n" + "="*80 + "\n")
            f.write("6) RÉSULTATS (maps où le joueur est trouvé)\n")
            f.write("="*80 + "\n\n")
            if hits:
                for name, href_tmx, records, href_tmio, found in hits:
                    f.write(f"{name}\n")
                    f.write(f"{records} records\n")
                    f.write(f"TMX : {href_tmx}\n")
                    f.write(f"TMIO: {href_tmio}\n\n")
            else:
                f.write("Aucune\n\n")

        ctx.close()
        browser.close()


