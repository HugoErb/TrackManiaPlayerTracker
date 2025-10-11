import re
import json
import time
from datetime import date
from dateutil.relativedelta import relativedelta
from tqdm import tqdm
from playwright.sync_api import sync_playwright
from constants import *

def track_player(url, output_file):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url)

    # with open(output_file, "w", encoding="utf-8") as f:




def get_periode_quatre_mois():
    """Retourne une chaîne de la forme 'YYYY-MM-DD...YYYY-MM-DD'
    représentant la date d'il y a 4 mois et aujourd'hui."""

    aujourd_hui = date.today()
    il_y_a_4_mois = aujourd_hui - relativedelta(months=4)
    return f"{il_y_a_4_mois:%Y-%m-%d}...{aujourd_hui:%Y-%m-%d}"


if __name__ == '__main__':
    start_time = time.time()
    date_interval = get_periode_quatre_mois()
    output_file = SEEKED_PLAYER + "_" + date_interval + ".txt"
    url = MAP_LIST_URL + date_interval

    print(f"\nRecherche de {SEEKED_PLAYER} en cours...")
    track_player(url, output_file)
    print(f"Rapport enregistré dans '{output_file}' en {time.time() - start_time:.2f} sec.\n")
