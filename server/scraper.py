"""
Motores de scraping.
Tres tipos de extraccion soportados:
  - css:        selectores CSS sobre HTML renderizado (StealthyFetcher)
  - shopify:    JSON embebido tipo Shopify (handle + variants + sku)
  - json_embed: JSON embebido en el HTML ("products":[{...}])

No persiste nada en DB. Devuelve listas de dicts.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import os
import re
import json
import time
import random
import threading
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapling import Fetcher
from scrapling.fetchers import StealthyFetcher
from dotenv import load_dotenv

load_dotenv()


# --------------------------------------------------
# CONTADOR DE TRAFICO (para metricas del response)
# --------------------------------------------------
TOTAL_TRAFFIC_BYTES = 0
_traffic_lock = threading.Lock()
PROXY_URL = os.getenv("PROXY_URL")

BLOCKED_DOMAINS = {
    "google-analytics.com", "googletagmanager.com", "facebook.net",
    "hotjar.com", "doubleclick.net", "bing.com", "clarity.ms",
    "yandex.ru", "cdn.connectad.io", "criteo.com", "trustpilot.com",
    "px.ads.linkedin.com", "bat.bing.com", "static.ads-twitter.com",
}


def add_traffic(byte_count: int):
    global TOTAL_TRAFFIC_BYTES
    with _traffic_lock:
        TOTAL_TRAFFIC_BYTES += byte_count


def get_total_traffic_mb() -> float:
    return round(TOTAL_TRAFFIC_BYTES / (1024 * 1024), 2)


def reset_traffic():
    global TOTAL_TRAFFIC_BYTES
    with _traffic_lock:
        TOTAL_TRAFFIC_BYTES = 0


# --------------------------------------------------
# UTILIDADES DE PRECIO Y MONEDA
# --------------------------------------------------

def clean_price_number(price_str: str) -> float:
    """
    Parser robusto de precios con multiples formatos:
      "1,234.56"     -> 1234.56  (US)
      "1.234,56"     -> 1234.56  (EU)
      "1.919.050"    -> 1919050  (COP / miles con punto)
      "1,919,050"    -> 1919050  (miles con coma sin decimales)
      "159,99"       -> 159.99   (decimal europeo)
      "159.99"       -> 159.99   (decimal US)
    """
    if not price_str:
        return 0.0
    clean_str = re.sub(r"[^\d.,]", "", price_str)
    if not clean_str:
        return 0.0

    has_dot = "." in clean_str
    has_comma = "," in clean_str

    if has_dot and has_comma:
        # El separador decimal es el ultimo que aparece
        if clean_str.rfind(",") > clean_str.rfind("."):
            clean_str = clean_str.replace(".", "").replace(",", ".")
        else:
            clean_str = clean_str.replace(",", "")
    elif has_comma:
        parts = clean_str.split(",")
        # 2 partes y la segunda tiene 1-2 digitos -> decimal
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            clean_str = clean_str.replace(",", ".")
        else:
            clean_str = clean_str.replace(",", "")
    elif has_dot:
        parts = clean_str.split(".")
        # 2 partes y la segunda tiene 1-2 digitos -> decimal (159.99, 1234.5)
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            pass
        else:
            # Multiples puntos o segmentos de 3 digitos -> separador de miles (COP: 1.919.050)
            clean_str = clean_str.replace(".", "")

    try:
        return float(clean_str)
    except Exception:
        return 0.0


PRICE_TOKEN_REGEX = re.compile(
    r"(?:US\$|EUR|USD|GBP|MXN|COP|DKK|CHF|JPY|\$|€|£|¥)?\s?\d[\d.,]*\d(?:\s?(?:US\$|EUR|USD|GBP|MXN|COP|DKK|\$|€|£|¥))?"
)


def select_best_price_part(price_str: str) -> str:
    """
    Selecciona UN SOLO precio del string.
    Si el texto contiene varios precios (ej: precio actual + precio anterior +
    precio en cuotas + texto 'Hoy'), devuelve solo el primero.
    """
    if not price_str:
        return ""
    # 1. Split por separadores explicitos (/, |)
    parts = re.split(r"\s*[/|]\s*", price_str)
    target_part = parts[0]
    strong_currencies = ["USD", "EUR", "E", "US$", "GBP", "L"]
    for part in parts:
        u_part = part.upper()
        if any(c in u_part for c in strong_currencies):
            target_part = part
            break

    # 2. Extraer el PRIMER token con pinta de precio (numero con $, comas o puntos)
    match = PRICE_TOKEN_REGEX.search(target_part)
    if match:
        return match.group(0).strip()
    return target_part.strip()


def detect_currency(price_str: str) -> str:
    t = price_str.upper()
    if any(x in t for x in ["MXN", "MX$"]):
        return "MXN"
    if any(x in t for x in ["COP", "COL$"]):
        return "COP"
    if any(x in t for x in ["EUR", "E"]):
        return "EUR"
    if "GBP" in t or "L" in t:
        return "GBP"
    if "DKK" in t:
        return "DKK"
    if any(x in t for x in ["USD", "US$"]):
        return "USD"
    if "$" in t:
        return "USD"
    return "EUR"


CURRENCY_TLD_MAP = [
    # TLD / path generico  ->  ISO currency
    (".co.uk", "GBP"),
    (".uk/", "GBP"),
    (".com.co", "COP"),
    (".com.mx", "MXN"),
    (".com.br", "BRL"),
    (".com.ar", "ARS"),
    (".com.cl", "CLP"),
    (".com.pe", "PEN"),
    (".cl/", "CLP"),
    (".pe/", "PEN"),
    (".mx/", "MXN"),
    (".br/", "BRL"),
    (".ar/", "ARS"),
    (".dk/", "DKK"),
    (".se/", "SEK"),
    (".no/", "NOK"),
    (".ch/", "CHF"),
    (".jp/", "JPY"),
    (".au/", "AUD"),
    (".ca/", "CAD"),
    ("/en-us/", "USD"),
    ("/en-gb/", "GBP"),
    ("/en-eu/", "EUR"),
    ("/es-es/", "EUR"),
    ("/fr-fr/", "EUR"),
]


def detect_currency_from_config(config: Dict) -> str:
    """
    Moneda inferida desde el config.
    Prioridad: explicit currency -> mapping generico por TLD/path -> EUR default.
    Sin referencias a tiendas especificas.
    """
    explicit = config.get("currency")
    if explicit:
        return explicit.upper()
    url_low = config.get("url", "").lower()
    for marker, curr in CURRENCY_TLD_MAP:
        if marker in url_low:
            return curr
    return "EUR"


# --------------------------------------------------
# EXTRACCION DE SKU
# --------------------------------------------------

def extract_sku_from_url(product_url: str, config: Dict) -> str:
    """Extrae el SKU desde la URL del producto."""
    clean_url = product_url.split("?")[0].replace(".html", "")
    slug = clean_url.rstrip("/").split("/")[-1]
    sep = config.get("sku_url_separator", "-")
    parts = slug.split(sep)
    count = int(config.get("sku_url_count", 1))
    candidate_parts = parts[-count:] if len(parts) >= count else parts
    candidate_sku = sep.join(candidate_parts)
    if (candidate_sku.isdigit() and len(candidate_sku) < 6) or len(candidate_sku) < 3:
        if len(parts) > count:
            candidate_parts = parts[-(count + 1):]
            candidate_sku = sep.join(candidate_parts)
    return candidate_sku


def find_sku_in_json_ld(response) -> Optional[str]:
    """Busca el SKU en bloques de JSON-LD."""
    try:
        scripts = response.css('script[type="application/ld+json"]')
        for script in scripts:
            try:
                data = json.loads(script.text)
                if isinstance(data, list):
                    for item in data:
                        if item.get("@type") == "Product" and item.get("sku"):
                            return str(item["sku"]).strip().upper()
                elif data.get("@type") == "Product" and data.get("sku"):
                    return str(data["sku"]).strip().upper()
            except Exception:
                continue
    except Exception:
        pass
    return None


def find_sku_in_meta(response) -> Optional[str]:
    selectors = [
        'meta[property="og:sku"]',
        'meta[name="sku"]',
        'meta[property="product:sku"]',
        'meta[name="twitter:data2"]',
    ]
    for sel in selectors:
        try:
            elem = response.css(sel)
            if elem:
                val = elem[0].attrib.get("content")
                if val:
                    return val.strip().upper()
        except Exception:
            continue
    return None


def find_sku_by_text_labels(response) -> Optional[str]:
    labels = [
        "Supplier-sku", "Supplier SKU", "SKU:", "Ref:", "Referencia:",
        "Reference:", "Manufacturer code:", "Style:", "Article no.",
    ]
    try:
        text = response.html_content
        for label in labels:
            pattern = rf'{re.escape(label)}\s*[:#]?\s*([A-Z0-9][\w\-]{{3,20}})'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip().upper()
    except Exception:
        pass
    return None


# --------------------------------------------------
# PROXY HELPER
# --------------------------------------------------

def _get_proxy(config: Dict) -> Optional[str]:
    if config.get("use_proxy") and PROXY_URL:
        return PROXY_URL
    return None


# --------------------------------------------------
# CSS SCRAPING
# --------------------------------------------------

def scrape_page(url: str, config: Dict) -> List[Dict]:
    """Descarga una pagina y extrae productos con selectores CSS."""
    player = config.get("player", "Unknown")
    print(f"   FETCH: {player} - {url}")

    products_data = []
    try:
        proxy = _get_proxy(config)
        wait_sel = config.get("css_product_selector") or config.get("css_price_selector", ".price")

        response = StealthyFetcher.fetch(
            url,
            headless=True,
            disable_resources=True,
            blocked_domains=BLOCKED_DOMAINS,
            wait_selector=wait_sel,
            network_idle=True,
            proxy=proxy,
            timeout=60000,
            google_search=True,
            locale=config.get("locale", "en-US"),
            timezone_id=config.get("timezone_id", "UTC"),
        )

        status = getattr(response, "status", 0)
        if status not in (200, 0):
            print(f"   WARNING: Status {status} en {url}")
            return []

        html_text = getattr(response, "html_content", "") or ""
        add_traffic(len(html_text))

        product_selector = config.get("css_product_selector")
        if not product_selector:
            print("   ERROR: Falta css_product_selector en config")
            return []

        containers = response.css(product_selector)
        print(f"   CONTAINERS: {len(containers)} encontrados en {player}")

        for container in containers:
            try:
                product = _extract_single_product(container, config, url)
                if product:
                    products_data.append(product)
            except Exception:
                continue

    except Exception as e:
        print(f"   ERROR scrape_page ({player}): {e}")

    return products_data


def _find_in_context(container, css_selector: str, max_ancestors: int = 4):
    """Busca elementos en el container y luego en ancestros sucesivos."""
    try:
        results = container.css(css_selector)
        if results:
            return results
    except Exception:
        pass

    current = container
    for _ in range(max_ancestors):
        try:
            parent = current.parent
            if parent is None:
                break
            results = parent.css(css_selector)
            if results:
                return results
            current = parent
        except Exception:
            break

    return []


def _extract_single_product(container, config: Dict, base_url: str) -> Optional[Dict]:
    player = config.get("player", "Unknown")

    # --- URL ---
    url_sel = config.get("css_product_url_selector", "a")
    p_url = None
    if url_sel == "self":
        p_url = container.attrib.get("href")
        if not p_url:
            try:
                for ancestor in container.iterancestors():
                    if ancestor.attrib.get("href"):
                        p_url = ancestor.attrib.get("href")
                        break
            except Exception:
                pass
    else:
        link_elems = _find_in_context(container, url_sel)
        if link_elems:
            p_url = link_elems[0].attrib.get("href")
        else:
            link_elems = _find_in_context(container, "a[href]")
            if link_elems:
                p_url = link_elems[0].attrib.get("href")

    if not p_url:
        return None
    full_url = urljoin(base_url, p_url)

    # --- Nombre ---
    p_name = container.text.strip() if container.text else ""
    name_sel = config.get("css_name_selector")
    if name_sel:
        name_els = _find_in_context(container, name_sel, max_ancestors=2)
        if name_els:
            p_name = name_els[0].text.strip()

    if not p_name:
        p_name = full_url.split("/")[-1].replace("-", " ").title()

    # --- Precio ---
    price_sel = config.get("css_price_selector")
    raw_price = ""
    if price_sel:
        price_els = _find_in_context(container, price_sel)
        if price_els:
            raw_price = price_els[0].text.strip()
            if not raw_price:
                raw_price = re.sub(r'<[^>]+>', '', price_els[0].html_content).strip()

    best_price_str = select_best_price_part(raw_price)
    price_final = clean_price_number(best_price_str)
    if raw_price:
        currency = detect_currency(best_price_str)
        # Si la moneda solo se detecto por '$' (default USD) pero el contexto
        # de la URL sugiere otra cosa (COP, MXN, etc.), preferir el contexto.
        if currency == "USD" and "$" in best_price_str and not any(
            x in best_price_str.upper() for x in ("USD", "US$")
        ):
            ctx_currency = detect_currency_from_config(config)
            if ctx_currency != "EUR":  # EUR es el default fallback, no es senal real
                currency = ctx_currency
    else:
        currency = detect_currency_from_config(config)

    # --- Descuento / precio original ---
    price_original = None
    discount_pct = 0.0
    discount_sel = config.get("css_discount_selector")
    if discount_sel:
        disc_els = _find_in_context(container, discount_sel)
        if disc_els:
            disc_text = disc_els[0].text.strip()
            if not disc_text:
                disc_text = re.sub(r'<[^>]+>', '', disc_els[0].html_content).strip()
            disc_val = abs(clean_price_number(disc_text))
            if config.get("discount_is_original_price"):
                price_original = disc_val
                if price_original > price_final and price_original > 0:
                    discount_pct = round(((price_original - price_final) / price_original) * 100, 2)
            elif "%" in disc_text:
                discount_pct = disc_val
            elif disc_val > price_final:
                price_original = disc_val
                if price_original > 0:
                    discount_pct = round(((price_original - price_final) / price_original) * 100, 2)

    if price_original is None and 0 < discount_pct < 100:
        price_original = round(price_final / (1 - (discount_pct / 100)), 2)
    elif price_original is None:
        price_original = price_final

    # --- SKU ---
    sku = ""
    if config.get("sku_mode") == "url":
        sku = extract_sku_from_url(p_url, config)
    elif config.get("sku_mode") == "css" and not config.get("crawl_product_page"):
        sku_sel = config.get("sku_css_selector")
        if sku_sel:
            sku_els = _find_in_context(container, sku_sel)
            if sku_els:
                attr = config.get("sku_css_attribute")
                sku = sku_els[0].attrib.get(attr) if attr else sku_els[0].text.strip()

    # --- Filtro de basura ---
    if price_final == 0:
        return None
    if len(sku) > 100 or "%22" in sku or "{" in sku:
        return None
    if len(p_name) > 300:
        return None

    return {
        "player": player,
        "product_name": p_name[:200],
        "codigo_referencia": sku.upper() if sku else "",
        "price_final": price_final,
        "price_original": price_original,
        "currency": currency,
        "discount_pct": discount_pct,
        "url": full_url,
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------
# SHOPIFY EXTRACT (JSON embebido)
# --------------------------------------------------

def _shopify_price(raw_price) -> float:
    if raw_price is None:
        return 0.0
    if isinstance(raw_price, str):
        raw_str = raw_price.strip().replace(",", "")
        try:
            val = float(raw_str)
        except ValueError:
            return 0.0
        if "." in raw_str:
            return round(val, 2)
        if val > 0:
            return round(val / 100.0, 2)
        return 0.0
    else:
        val = float(raw_price)
        if isinstance(raw_price, int) and val > 0:
            return round(val / 100.0, 2)
        return round(val, 2)


def _collect_shopify_products(data, collected: list):
    if isinstance(data, dict):
        if "handle" in data and "variants" in data and isinstance(data["variants"], list):
            collected.append(data)
        else:
            for v in data.values():
                _collect_shopify_products(v, collected)
    elif isinstance(data, list):
        for item in data:
            _collect_shopify_products(item, collected)


def _clean_shopify_sku(raw_sku: str) -> str:
    """
    Devuelve el SKU tal cual viene del JSON de Shopify, sin asunciones
    de industria (no quitar sufijos de talla porque eso es shoe-specific).
    Si el usuario necesita partir el SKU por variante, puede hacerlo cliente-side.
    """
    return (raw_sku or "").strip()


def _extract_shopify_regex(html: str) -> list:
    products_map = {}
    pattern = r'"handle"\s*:\s*"([^"]+)"[^}]*?"sku"\s*:\s*"([^"]*)"[^}]*?"price"\s*:\s*(\d+(?:\.\d+)?)'
    for match in re.finditer(pattern, html):
        handle, sku, price = match.groups()
        if handle not in products_map:
            products_map[handle] = {"handle": handle, "variants": [{"sku": sku, "price": int(float(price))}]}
    pattern2 = r'"sku"\s*:\s*"([^"]*)"[^}]*?"price"\s*:\s*(\d+(?:\.\d+)?)[^}]*?"handle"\s*:\s*"([^"]+)"'
    for match in re.finditer(pattern2, html):
        sku, price, handle = match.groups()
        if handle not in products_map:
            products_map[handle] = {"handle": handle, "variants": [{"sku": sku, "price": int(float(price))}]}
    return list(products_map.values())


def extract_shopify_products(url: str, config: Dict) -> List[Dict]:
    """Extrae productos del JSON embebido de Shopify."""
    player = config.get("player", "Unknown")
    print(f"   SHOPIFY-EXTRACT: {player} - {url}")

    try:
        proxy = _get_proxy(config)
        response = StealthyFetcher.fetch(
            url,
            headless=True,
            disable_resources=True,
            blocked_domains=BLOCKED_DOMAINS,
            network_idle=True,
            proxy=proxy,
            timeout=60000,
            google_search=True,
            locale=config.get("locale", "en-US"),
            timezone_id=config.get("timezone_id", "UTC"),
        )

        status = getattr(response, "status", 0)
        if status not in (200, 0):
            print(f"   WARNING: Status {status} en {url}")
            return []

        html = getattr(response, "html_content", "") or ""
        add_traffic(len(html))

        if '"sku"' not in html:
            print(f"   WARNING: No se encontro datos Shopify en {url}")
            return []

        raw_products = []
        scripts = response.css("script")
        decoder = json.JSONDecoder()

        for script in scripts:
            text = script.text or ""
            if not text or '"sku"' not in text or '"handle"' not in text:
                continue

            text_stripped = text.strip()
            if text_stripped.startswith(("{", "[")):
                try:
                    data = json.loads(text_stripped)
                    _collect_shopify_products(data, raw_products)
                    continue
                except json.JSONDecodeError:
                    pass

            for match in re.finditer(r'(?:var\s+\w+|[\w.]+)\s*=\s*', text):
                start = match.end()
                if start < len(text) and text[start] in ('{', '['):
                    try:
                        data, _ = decoder.raw_decode(text, start)
                        _collect_shopify_products(data, raw_products)
                    except (json.JSONDecodeError, ValueError):
                        continue

        if not raw_products:
            raw_products = _extract_shopify_regex(html)

        parsed = urlparse(url)
        site_base = f"{parsed.scheme}://{parsed.netloc}"

        products = []
        seen_handles = set()

        for rp in raw_products:
            handle = rp.get("handle", "")
            if not handle or handle in seen_handles:
                continue
            seen_handles.add(handle)

            variants = rp.get("variants", [])
            sku = ""
            price_final = 0.0
            price_original = 0.0

            for v in variants:
                v_sku = str(v.get("sku", "")).strip()
                if v_sku:
                    sku = _clean_shopify_sku(v_sku)
                    price_final = _shopify_price(v.get("price"))
                    compare = v.get("compare_at_price")
                    price_original = _shopify_price(compare) if compare else price_final
                    break

            if not sku and variants:
                v = variants[0]
                price_final = _shopify_price(v.get("price"))
                compare = v.get("compare_at_price")
                price_original = _shopify_price(compare) if compare else price_final

            if price_final == 0:
                continue

            discount_pct = 0.0
            if price_original > price_final and price_original > 0:
                discount_pct = round(((price_original - price_final) / price_original) * 100, 2)
            elif price_original == 0:
                price_original = price_final

            product_url = rp.get("url") or f"/products/{handle}"
            full_url = urljoin(site_base, product_url)

            title = rp.get("title", "") or handle.replace("-", " ").title()

            products.append({
                "player": player,
                "product_name": title[:200],
                "codigo_referencia": sku.upper() if sku else "",
                "price_final": price_final,
                "price_original": price_original,
                "currency": detect_currency_from_config(config),
                "discount_pct": discount_pct,
                "url": full_url,
                "extraction_method": "shopify_json",
                "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

        print(f"   SHOPIFY-EXTRACT: {len(products)} productos extraidos")
        return products

    except Exception as e:
        print(f"   ERROR extract_shopify ({player}): {e}")
        return []


# --------------------------------------------------
# JSON EMBEDDED EXTRACT (patrones genericos)
# --------------------------------------------------

def extract_json_embed_products(url: str, config: Dict) -> List[Dict]:
    """
    Extrae productos de un JSON embebido en el HTML.
    Soporta marcadores configurables (config['json_embed_marker'])
    o auto-detecta patrones comunes en el HTML.
    """
    player = config.get("player", "Unknown")
    print(f"   JSON-EMBED: {player} - {url}")

    try:
        proxy = _get_proxy(config)
        response = StealthyFetcher.fetch(
            url,
            headless=True,
            disable_resources=True,
            blocked_domains=BLOCKED_DOMAINS,
            network_idle=True,
            proxy=proxy,
            timeout=60000,
            google_search=True,
            locale=config.get("locale", "en-US"),
            timezone_id=config.get("timezone_id", "UTC"),
        )

        status = getattr(response, "status", 0)
        if status not in (200, 0):
            print(f"   WARNING: Status {status} en {url}")
            return []

        html = getattr(response, "html_content", "") or ""
        add_traffic(len(html))

        marker = config.get("json_embed_marker")
        possible_markers = [marker] if marker else [
            '"products":[{"badges"', '"products":[{"base', '"products":[{"sku"',
            '"productList":[{', '"items":[{"sku"',
        ]

        raw_products = None
        for m in possible_markers:
            if not m:
                continue
            idx = html.find(m)
            if idx < 0:
                continue
            arr_start = html.find("[", idx)
            try:
                decoder = json.JSONDecoder()
                raw_products, _ = decoder.raw_decode(html, arr_start)
                break
            except (json.JSONDecodeError, ValueError):
                continue

        if not raw_products:
            print(f"   WARNING: No se encontro JSON de productos en {url}")
            return []

        print(f"   JSON-EMBED: {len(raw_products)} productos encontrados")

        parsed = urlparse(url)
        site_base = f"{parsed.scheme}://{parsed.netloc}"

        products = []
        seen = set()

        for rp in raw_products:
            # Normalizar campos comunes entre formatos
            sku = str(rp.get("sku") or rp.get("productId") or rp.get("id") or "").strip()
            if not sku or sku in seen:
                continue
            seen.add(sku)

            name = rp.get("name") or rp.get("productName") or rp.get("title") or ""

            # Precio: variar segun estructura
            price_obj = rp.get("price")
            orig_obj = rp.get("originalPrice")

            if isinstance(price_obj, dict):
                price_final = float(price_obj.get("value") or price_obj.get("amount") or 0)
                formatted = price_obj.get("formattedValue", "")
            else:
                price_final = float(price_obj or 0)
                formatted = ""

            if isinstance(orig_obj, dict):
                price_original = float(orig_obj.get("value") or orig_obj.get("amount") or 0)
            else:
                price_original = float(orig_obj or 0)

            if price_final == 0:
                continue
            if price_original == 0:
                price_original = price_final

            discount_pct = 0.0
            if price_original > price_final and price_original > 0:
                discount_pct = round(((price_original - price_final) / price_original) * 100, 2)

            # URL: si el JSON la trae, usarla; sino construir
            product_url = rp.get("url") or rp.get("link") or rp.get("href")
            if product_url:
                full_url = urljoin(site_base, product_url)
            else:
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                full_url = f"{site_base}/product/{slug}/{sku}.html" if slug else f"{site_base}/p/{sku}"

            # Moneda
            if "$" in formatted and "COP" not in formatted.upper():
                currency = "USD"
            elif "EUR" in formatted or "E" in formatted:
                currency = "EUR"
            elif "GBP" in formatted or "£" in formatted:
                currency = "GBP"
            elif formatted:
                currency = detect_currency(formatted)
            else:
                currency = detect_currency_from_config(config)

            products.append({
                "player": player,
                "product_name": str(name)[:200],
                "codigo_referencia": sku.upper(),
                "price_final": price_final,
                "price_original": price_original,
                "currency": currency,
                "discount_pct": discount_pct,
                "url": full_url,
                "extraction_method": "json_embed",
                "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

        return products

    except Exception as e:
        print(f"   ERROR extract_json_embed ({player}): {e}")
        return []


# --------------------------------------------------
# NEXT.JS EXTRACT (END., Asos, Asphaltgold, Zara, etc.)
# --------------------------------------------------

NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


PRODUCT_SIGNAL_KEYS = {
    "brand", "brandname", "image", "imageurl", "imageurls", "images",
    "thumbnail", "thumbnailurl", "thumbnails",
    "slug", "urlkey", "urlname",
    "sizes", "variants", "skus", "color", "colors", "colour", "colours",
    "category", "categoryid", "categories", "categorypath", "primarycategoryid",
    "productcode", "ean", "upc", "mpn", "model", "style",
    "isavailable", "instock", "available", "inventory",
    "salepct", "sale", "onsale",
    "vendor", "supplier", "manufacturer",
    "tags", "labels", "badges",
    "description", "shortdescription", "summary",
}

NON_PRODUCT_HINTS = {
    "shippingmethod", "deliverymethod", "deliveryoption", "shippingoption",
    "carrier", "courier", "deliverypartner", "shipping",
    "footer", "menu", "nav", "category", "filter", "facet", "breadcrumb",
    "country", "currency", "language", "locale", "store",
    "voucher", "coupon", "promotion",
}


def _looks_like_product(d) -> bool:
    """
    Heuristica estricta: distingue productos reales de otras entidades
    que tambien tienen id+name+price (envios, vouchers, categorias).

    Requiere id+name+price + al menos UNA senal de producto fisico
    (brand, image, slug, sizes, category, ...).
    """
    if not isinstance(d, dict):
        return False
    keys_low = {k.lower() for k in d.keys() if isinstance(k, str)}

    has_id = bool(keys_low & {"sku", "productid", "id", "code", "styleid"})
    has_name = bool(keys_low & {"name", "productname", "title", "displayname"})
    has_price = any(
        any(token in k for k in keys_low)
        for token in ("price", "amount", "current", "sale")
    )
    if not (has_id and has_name and has_price):
        return False

    # Debe tener al menos una senal de producto fisico
    if not (keys_low & PRODUCT_SIGNAL_KEYS):
        return False

    # Descartar si el nombre o tipo del objeto parece NO producto
    name = str(d.get("name", "") or d.get("title", "") or "").lower()
    type_field = str(d.get("type", "") or d.get("__typename", "") or "").lower()
    combined = name + " " + type_field
    if any(h in combined for h in NON_PRODUCT_HINTS):
        return False

    return True


def _walk_json_for_products(data, out: list, depth: int = 0):
    """Recorre un JSON buscando arrays de productos."""
    if depth > 40:
        return
    if isinstance(data, list):
        # Lista que parece array de productos
        if len(data) >= 2 and all(_looks_like_product(x) for x in data[:3]):
            out.extend(data)
            return
        for item in data:
            _walk_json_for_products(item, out, depth + 1)
    elif isinstance(data, dict):
        if _looks_like_product(data):
            out.append(data)
        for v in data.values():
            _walk_json_for_products(v, out, depth + 1)


def _coerce_price(v) -> float:
    """Convierte un valor de precio (int/float/str/dict) a float."""
    if v is None:
        return 0.0
    if isinstance(v, dict):
        for k in ("value", "amount", "current", "centAmount", "unitPrice", "price"):
            if k in v:
                return _coerce_price(v[k])
        return 0.0
    if isinstance(v, (int, float)):
        val = float(v)
        # centAmount-style (centavos): suelen ser >= 1000 y enteros
        if isinstance(v, int) and val >= 1000 and val == int(val):
            # Heuristica: si parece centavos (multiplo de 10, 4+ digitos), dividir
            # Pero no siempre — dejamos sin tocar y que el caller decida
            return val
        return round(val, 2)
    if isinstance(v, str):
        return clean_price_number(v)
    return 0.0


PRICE_KEY_PRIORITY = [
    "currentPrice", "salePrice", "sale_price", "price", "displayPrice",
    "amount", "final_price", "final_price_1", "current_price",
]
ORIGINAL_PRICE_KEY_PRIORITY = [
    "originalPrice", "regularPrice", "regular_price", "wasPrice", "was_price",
    "compareAtPrice", "compare_at_price", "rrp", "listPrice", "list_price",
    "full_price", "full_price_1", "originalPrice_value",
]
URL_KEY_PRIORITY = [
    "url", "href", "link", "permalink", "slug", "urlSlug",
    "url_key", "urlKey", "seoUrl", "seo_url", "seo_url_path", "path", "route",
]


def _first_numeric_with_token(d: dict, token: str) -> float:
    """Devuelve el primer valor numerico en d cuya key contiene `token`."""
    for k, v in d.items():
        if not isinstance(k, str) or token not in k.lower():
            continue
        cand = _coerce_price(v)
        if 0 < cand < 1_000_000_000:
            return cand
    return 0.0


def _extract_product_fields(d: dict, base_url: str) -> Optional[Dict]:
    """Normaliza un dict de producto al schema estandar."""
    name = (
        d.get("name") or d.get("productName") or d.get("title")
        or d.get("displayName") or d.get("display_name") or ""
    )
    if not name:
        return None

    sku = str(
        d.get("sku") or d.get("productId") or d.get("product_id")
        or d.get("id") or d.get("code") or d.get("styleId")
        or d.get("style_id") or d.get("objectID") or ""
    ).strip()

    # Precio actual
    price_final = 0.0
    for k in PRICE_KEY_PRIORITY:
        if k in d:
            price_final = _coerce_price(d[k])
            if price_final > 0:
                break
    if price_final == 0:
        # Fallback: cualquier key con "price" que tenga valor numerico razonable
        price_final = _first_numeric_with_token(d, "price")
    if price_final == 0:
        return None

    # Precio original (tachado)
    price_original = 0.0
    for k in ORIGINAL_PRICE_KEY_PRIORITY:
        if k in d:
            price_original = _coerce_price(d[k])
            if price_original > 0:
                break
    if price_original == 0 or price_original < price_final:
        price_original = price_final

    discount_pct = 0.0
    if price_original > price_final > 0:
        discount_pct = round(((price_original - price_final) / price_original) * 100, 2)

    # URL
    raw_url = None
    for k in URL_KEY_PRIORITY:
        v = d.get(k)
        if isinstance(v, dict):
            v = v.get("url") or v.get("href") or v.get("path")
        if isinstance(v, str) and v.strip():
            raw_url = v.strip()
            break

    if raw_url:
        if raw_url.startswith("http"):
            full_url = raw_url
        elif raw_url.startswith("/"):
            full_url = urljoin(base_url, raw_url)
        else:
            # Slug suelto sin '/' inicial: anteponer base path tipo /products/
            full_url = urljoin(base_url + "/", raw_url)
    else:
        full_url = base_url

    # Moneda
    currency = ""
    for k in ("currency", "currencyCode", "currency_code", "currencyIsoCode"):
        if k in d and isinstance(d[k], str):
            currency = d[k].upper()
            break
    if not currency:
        for k in ("currentPrice", "price"):
            v = d.get(k)
            if isinstance(v, dict):
                for ck in ("currency", "currencyCode"):
                    if ck in v and isinstance(v[ck], str):
                        currency = v[ck].upper()
                        break
    if not currency:
        currency = detect_currency_from_config({"url": base_url})

    return {
        "product_name": str(name)[:250],
        "codigo_referencia": sku.upper(),
        "price_final": price_final,
        "price_original": price_original,
        "currency": currency,
        "discount_pct": discount_pct,
        "url": full_url,
        "extraction_method": "nextjs_data",
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def extract_nextjs_products(url: str, config: Dict) -> List[Dict]:
    """
    Extrae productos del bloque <script id="__NEXT_DATA__"> de apps Next.js.
    Funciona en END., Asphaltgold, Asos y similares.
    """
    player = config.get("player", "Unknown")
    print(f"   NEXTJS-EXTRACT: {player} - {url}")

    try:
        proxy = _get_proxy(config)
        response = StealthyFetcher.fetch(
            url,
            headless=True,
            disable_resources=True,
            blocked_domains=BLOCKED_DOMAINS,
            network_idle=True,
            proxy=proxy,
            timeout=60000,
            google_search=True,
            locale=config.get("locale", "en-US"),
            timezone_id=config.get("timezone_id", "UTC"),
        )

        status = getattr(response, "status", 0)
        if status not in (200, 0):
            print(f"   WARNING: Status {status} en {url}")
            return []

        html = getattr(response, "html_content", "") or ""
        add_traffic(len(html))

        match = NEXT_DATA_RE.search(html)
        if not match:
            print("   WARNING: No se encontro __NEXT_DATA__")
            return []

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            print(f"   ERROR: __NEXT_DATA__ JSON invalido: {e}")
            return []

        raw_products: List[Dict] = []
        _walk_json_for_products(data, raw_products)
        print(f"   NEXTJS-EXTRACT: {len(raw_products)} candidatos en __NEXT_DATA__")

        parsed = urlparse(url)
        site_base = f"{parsed.scheme}://{parsed.netloc}"

        products = []
        seen = set()
        for rp in raw_products:
            norm = _extract_product_fields(rp, site_base)
            if not norm:
                continue
            key = norm.get("sku") or norm.get("url")
            if key in seen:
                continue
            seen.add(key)
            norm["player"] = player
            products.append(norm)

        print(f"   NEXTJS-EXTRACT: {len(products)} productos normalizados")
        return products

    except Exception as e:
        print(f"   ERROR extract_nextjs ({player}): {e}")
        return []


# --------------------------------------------------
# DETAIL CRAWL (opcional, para SKUs en pagina de detalle)
# --------------------------------------------------

def scrape_details_batch(products: List[Dict], config: Dict, max_workers: int = 5) -> List[Dict]:
    """Para cada producto, intenta extraer el SKU de su pagina de detalle."""
    if not products:
        return products

    print(f"   DETAIL-BATCH: Procesando {len(products)} detalles")

    def fetch_detail(product: Dict) -> Dict:
        url = product["url"]
        try:
            resp = Fetcher().get(url, timeout=15, proxy=_get_proxy(config))
            if resp and resp.status == 200:
                add_traffic(len(resp.html_content or ""))
                sku = _extract_sku_from_detail(resp, config)
                if sku:
                    product["codigo_referencia"] = sku
                    return product

            resp = StealthyFetcher.fetch(
                url,
                headless=True,
                disable_resources=True,
                blocked_domains=BLOCKED_DOMAINS,
                proxy=_get_proxy(config),
                timeout=30000,
                google_search=True,
            )
            add_traffic(len(resp.html_content or ""))
            sku = _extract_sku_from_detail(resp, config)
            if sku:
                product["codigo_referencia"] = sku

        except Exception as e:
            print(f"      DETAIL-ERR: {url[:60]}... -> {str(e)[:50]}")

        return product

    updated = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for p in products:
            time.sleep(random.uniform(0.2, 0.8))
            futures.append(executor.submit(fetch_detail, p))

        for future in as_completed(futures):
            try:
                updated.append(future.result())
            except Exception:
                pass

    found = sum(1 for p in updated if p.get("codigo_referencia"))
    print(f"   DETAIL-BATCH: codigo_referencia encontrados: {found}/{len(updated)}")
    return updated


def _extract_sku_from_detail(response, config: Dict) -> Optional[str]:
    sku = find_sku_in_json_ld(response)
    if sku:
        return sku
    sku = find_sku_in_meta(response)
    if sku:
        return sku
    sku_sel = config.get("sku_css_selector")
    if sku_sel:
        try:
            sku_els = response.css(sku_sel)
            if sku_els:
                attr = config.get("sku_css_attribute")
                sku = sku_els[0].attrib.get(attr) if attr else sku_els[0].text.strip()
                if sku:
                    return sku.upper()
        except Exception:
            pass
    sku = find_sku_by_text_labels(response)
    if sku:
        return sku
    return None
