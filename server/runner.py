"""
Orquestacion ligera de una sola URL.
Pagina segun el config, llama al motor correspondiente (CSS / Shopify /
JSON embed / Next.js) y devuelve todos los productos.

IMPORTANTE: este modulo NO llama a la IA. La deteccion (incluyendo Gemini)
sucede UNA SOLA VEZ en detector.detect_config() antes de entrar a este loop.
Las paginas 2, 3, ... reutilizan exactamente la misma config inferida en la
pagina 1, por lo que la IA solo "ve" la primera pagina.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import random
import time
from typing import Dict, List
from urllib.parse import parse_qs, urlencode, urlparse

import scraper


# --------------------------------------------------
# PAGINACION
# --------------------------------------------------

def build_next_page_url(current_url: str, config: Dict, current_page: int, raw_count: int) -> str:
    """Construye la URL de la siguiente pagina segun la config."""
    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query)
    param_name = config.get("pagination_param", "page")
    mode = config.get("pagination_mode", "page")

    if mode == "offset_items":
        param_value = raw_count + 1
    else:
        step = config.get("pagination_step", 1)
        param_value = (current_page + 1) * step

    qs[param_name] = [str(param_value)]
    new_query = urlencode(qs, doseq=True)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"


# --------------------------------------------------
# RESOLUCION DE TIPO DE SITIO
# --------------------------------------------------

def resolve_site_type(config: Dict) -> str:
    """Devuelve 'shopify', 'json_embed' o 'css'. Default: css."""
    return config.get("site_type", "css")


# --------------------------------------------------
# SCRAPE DE UN SITIO (CON PAGINACION)
# --------------------------------------------------

def scrape_site(config: Dict) -> List[Dict]:
    """
    Procesa una URL completa con paginacion.
    Retorna la lista de productos extraidos (sin filtros).
    """
    player = config.get("player", "Unknown")
    base_url = config["url"]
    max_pages = config.get("max_pages", 3)
    site_type = resolve_site_type(config)

    print(f"\n>> {player} (max {max_pages} paginas, modo {site_type})")

    all_products = []
    current_url = base_url
    raw_count = 0

    for page_num in range(1, max_pages + 1):
        if page_num > 1:
            current_url = build_next_page_url(base_url, config, page_num - 1, raw_count)

        print(f"   PAGE {page_num}/{max_pages}: {current_url[:80]}...")

        if site_type == "shopify":
            page_results = scraper.extract_shopify_products(current_url, config)
        elif site_type == "json_embed":
            page_results = scraper.extract_json_embed_products(current_url, config)
        elif site_type == "nextjs":
            page_results = scraper.extract_nextjs_products(current_url, config)
        else:
            page_results = scraper.scrape_page(current_url, config)

        if not page_results:
            print(f"   STOP: No se encontraron productos en pagina {page_num}.")
            break

        raw_count += len(page_results)
        print(f"   FOUND: {len(page_results)} productos en pagina {page_num}")

        if site_type != "shopify" and config.get("crawl_product_page"):
            page_results = scraper.scrape_details_batch(page_results, config, max_workers=5)

        all_products.extend(page_results)

        if page_num < max_pages:
            delay = random.uniform(2, 5)
            print(f"   WAIT: {delay:.1f}s antes de siguiente pagina...")
            time.sleep(delay)

    print(f"<< {player}: {len(all_products)} productos total")
    return all_products
