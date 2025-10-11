import re
import json
import time
from datetime import date
from dateutil.relativedelta import relativedelta
from tqdm import tqdm
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from constants import *

def get_maps(url: str):
    items = []
    seen_links = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

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

        ctx.close()
        browser.close()

    return items

def get_periode_quatre_mois():
    aujourd_hui = date.today()
    il_y_a_4_mois = aujourd_hui - relativedelta(months=4)
    return f"{il_y_a_4_mois:%Y-%m-%d}...{aujourd_hui:%Y-%m-%d}"


if __name__ == '__main__':
    start_time = time.time()
    date_interval = get_periode_quatre_mois()
    output_file = SEEKED_PLAYER + "_" + date_interval + ".txt"
    url = MAP_LIST_URL + date_interval

    print(f"Récupération des maps en cours...")
    all_maps = get_maps(url)

    print(f"\n{len(all_maps)} maps trouvées.")

    print(f"\nRecherche de {SEEKED_PLAYER} dans les maps récupérées en cours...")
    print(f"Rapport enregistré dans '{output_file}' en {time.time() - start_time:.2f} sec.\n")
