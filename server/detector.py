"""
Auto-detector de configuracion de scraping.
Dada una URL de listado de productos, infiere el tipo de pagina (Shopify, JSON
embebido, CSS clasico) y devuelve un config compatible con scraper.py.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import re
from collections import Counter
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from scrapling import Fetcher
from scrapling.fetchers import StealthyFetcher

import scraper as scr

try:
    import ai_detector
except Exception:
    ai_detector = None


# --------------------------------------------------
# UTILIDADES
# --------------------------------------------------

CURRENCY_REGEX = re.compile(
    r"(?:US\$|EUR|USD|GBP|MXN|COP|DKK|CHF|JPY|\$|€|£|¥)\s?\d|\d[\d.,]*\s?(?:US\$|EUR|USD|GBP|MXN|COP|DKK|\$|€|£)"
)


def _fetch(url: str, use_browser: bool = True, timeout: int = 30000):
    """Descarga la URL. Por defecto usa StealthyFetcher (browser)."""
    if use_browser:
        return StealthyFetcher.fetch(
            url,
            headless=True,
            disable_resources=True,
            blocked_domains=scr.BLOCKED_DOMAINS,
            network_idle=True,
            timeout=timeout,
            google_search=True,
            locale="en-US",
            timezone_id="UTC",
        )
    return Fetcher().get(url, timeout=15)


def _site_base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# --------------------------------------------------
# DETECCION DE TIPO
# --------------------------------------------------

def detect_shopify(html: str) -> bool:
    """
    True si la pagina contiene marcadores fuertes de Shopify.
    Estricto: requiere referencia al CDN de Shopify o asignacion JS de Shopify,
    O el trio handle/variants/sku junto con compare_at_price (campo Shopify
    especifico). Esto evita falsos positivos en sites Magento o custom.
    """
    strong_markers = (
        "cdn.shopify.com",
        "Shopify.shop",
        "window.Shopify =",
        "shopify.com/s/files",
    )
    if any(m in html for m in strong_markers):
        return True
    # Patron de variante Shopify: tiene que tener todo el grupo
    if (
        '"handle"' in html
        and '"variants"' in html
        and '"sku"' in html
        and '"compare_at_price"' in html
    ):
        return True
    return False


def detect_nextjs(html: str) -> bool:
    """True si la pagina es una app Next.js con __NEXT_DATA__ embebido."""
    return ('id="__NEXT_DATA__"' in html or "id='__NEXT_DATA__'" in html) and "</script>" in html


def detect_json_embed(html: str) -> Optional[str]:
    """
    Detecta un array de productos embebido en el HTML (patron generico).
    Devuelve el patron encontrado o None.
    """
    patterns = [
        '"products":[{"badges"',
        '"products":[{"base',
        '"products":[{"sku"',
        '"productList":[{',
        '"items":[{"sku"',
    ]
    for p in patterns:
        if p in html:
            return p
    return None


# --------------------------------------------------
# HEURISTICA CSS (para sitios sin JSON)
# --------------------------------------------------

PRICE_CLASS_HINTS = ("price", "precio", "amount", "cost")
NAME_CLASS_HINTS = ("title", "name", "product-name", "card__title", "product-title")
CARD_CLASS_HINTS = ("product", "card", "item", "tile", "grid__item")

# Tokens que indican que una clase pertenece a un *sub-elemento* de la tarjeta,
# no a la tarjeta misma. Evita bubbling up que se quede en un wrapper de precio.
BAD_CARD_TOKENS = (
    "__price", "__img", "__image", "__rating", "__discount", "__badges",
    "__variants", "__brand", "__sticker", "__media",
    "price-box", "price__", "rating", "badge", "discount__", "image-",
    "product-image", "swatch", "review", "filter", "facet",
)


def _has_class_hint(attribs: dict, hints: tuple) -> bool:
    cls = str(attribs.get("class", "")).lower()
    return any(h in cls for h in hints)


def _shortest_class_selector(tag: str, class_str: str) -> Optional[str]:
    """Convierte 'card product on-sale' -> 'tag.card.product.on-sale'."""
    if not class_str:
        return None
    classes = [c for c in class_str.strip().split() if c]
    if not classes:
        return None
    # Filtrar clases que parecen estado/utility
    filtered = [c for c in classes if not c.startswith(("is-", "has-", "js-"))]
    if not filtered:
        filtered = classes
    return tag + "".join(f".{c}" for c in filtered)


def _is_product_card(el) -> bool:
    """
    Heuristica: ¿este elemento parece la tarjeta de un producto?
    - Tag estructural (article, li, div, section)
    - Clase con keyword tipo 'product/card/item/tile'
    - Sin tokens de sub-componente (__price, __img, __rating...)
    - Contiene al menos un <a href>
    """
    if el is None:
        return False
    tag = getattr(el, "tag", None)
    if tag not in ("article", "li", "div", "section"):
        return False
    cls = (el.attrib.get("class", "") or "").lower()
    if not cls:
        return False
    if not any(k in cls for k in CARD_CLASS_HINTS):
        return False
    if any(b in cls for b in BAD_CARD_TOKENS):
        return False
    try:
        if not el.css("a[href]"):
            return False
    except Exception:
        return False
    return True


def _has_name_descendant(el) -> bool:
    """True si el elemento contiene un descendiente que parece nombre de producto."""
    try:
        for child in el.css("*"):
            attribs = dict(child.attrib) if child.attrib else {}
            cls = (attribs.get("class", "") or "").lower()
            if not cls:
                continue
            if any(h in cls for h in NAME_CLASS_HINTS):
                return True
        for tag in ("h1", "h2", "h3", "h4"):
            try:
                for el2 in el.css(tag):
                    t = (el2.text or "").strip()
                    if 5 < len(t) < 200:
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


_TAG_PRIORITY = {"li": 0, "article": 1, "section": 2, "div": 3}


def _walk_up_to_product_card(el, max_levels: int = 8):
    """
    Colecciona TODOS los ancestros que parecen tarjeta de producto y elige
    el mejor por jerarquia:
      1. Prefiere tags estructurales (li > article > section > div) — la
         tarjeta real suele ser un <li> en grids modernos.
      2. Entre tags equivalentes, prefiere el que tiene un descendiente con
         nombre claro (heading o link con texto).
    Esto evita devolver sub-bloques como 'product__item__information' que
    contienen al precio pero NO la tarjeta completa.
    """
    candidates = []
    cur = el
    for _ in range(max_levels):
        try:
            cur = cur.parent
            if cur is None:
                break
            if _is_product_card(cur):
                candidates.append(cur)
        except Exception:
            break

    if not candidates:
        return None

    def _score(c):
        tag = c.tag
        tag_rank = _TAG_PRIORITY.get(tag, 9)
        has_name = _has_name_descendant(c)
        # Menor es mejor: tag estructural y con nombre
        return (tag_rank, 0 if has_name else 1)

    candidates.sort(key=_score)
    return candidates[0]


def _pick_key_class(class_str: str) -> Optional[str]:
    """Elige la clase mas representativa: prioriza las que contienen un CARD hint."""
    classes = [c for c in class_str.split() if c and not c.startswith(("is-", "has-", "js-"))]
    if not classes:
        return None
    for cl in classes:
        if any(k in cl.lower() for k in CARD_CLASS_HINTS):
            return cl
    return classes[0]


def _pick_significant_class(class_str: str, must_contain: str) -> Optional[str]:
    """De un string de clases, elige la primera que contiene `must_contain`."""
    classes = [c for c in class_str.split() if c]
    for cl in classes:
        cl_low = cl.lower()
        if must_contain in cl_low and not any(b in cl_low for b in ("box", "wrapper", "container")):
            return cl
    return None


def detect_css_by_prices(response) -> Optional[Tuple[str, List, Optional[str]]]:
    """
    Estrategia primaria CSS: encuentra elementos con clase `*price*` (case-insensitive),
    sube al ancestro que parece tarjeta de producto y agrupa.
    Retorna (product_selector, container_list, price_selector) o None.

    Resiste:
    - Precios anidados en spans (no depende de `.text` aggregation)
    - Sites con BEM o clases custom (independiente del proveedor)
    """
    try:
        price_els = response.css('[class*="price"]') or response.css('[class*="Price"]')
    except Exception:
        return None

    if not price_els or len(price_els) < 2:
        return None

    # Agregar clases de precio mas frecuentes para luego derivar el price_selector
    price_class_counter: Counter = Counter()
    containers = []

    for price in price_els[:150]:
        # Registrar la clase mas representativa del precio
        pcls = price.attrib.get("class", "") if price.attrib else ""
        sig = _pick_significant_class(pcls, "price")
        if sig:
            price_class_counter[sig] += 1

        # Subir hasta encontrar la tarjeta
        card = _walk_up_to_product_card(price, max_levels=8)
        if card is not None:
            containers.append(card)

    if len(containers) < 2:
        return None

    # Agrupar tarjetas por (tag, clase-clave)
    groups: Counter = Counter()
    repr_map: Dict[Tuple[str, str], str] = {}
    for c in containers:
        tag = c.tag
        cls = c.attrib.get("class", "") or ""
        key_class = _pick_key_class(cls)
        if not key_class:
            continue
        key = (tag, key_class)
        groups[key] += 1
        repr_map.setdefault(key, cls)

    if not groups:
        return None

    (best_tag, best_class), count = groups.most_common(1)[0]
    if count < 2:
        return None

    # Construir el selector: tag.clase-clave (formato simple, mas robusto que multi-clase)
    selector = f"{best_tag}.{best_class}"
    try:
        matches = response.css(selector)
        if len(matches) < 2:
            return None
    except Exception:
        return None

    # Derivar price_selector con la clase de precio mas comun
    price_selector = None
    if price_class_counter:
        top_price_class, _ = price_class_counter.most_common(1)[0]
        price_selector = f".{top_price_class}"

    print(f"   price-first: {len(matches)} tarjetas via {selector} (price={price_selector})")
    return selector, matches, price_selector


def detect_css_product_selector(response) -> Optional[Tuple[str, List]]:
    """
    Busca el selector mas probable del contenedor de producto.
    Estrategia: encuentra todos los elementos con precio (texto que parece moneda),
    sube al ancestro comun mas pequeno que se repite y devuelve el selector.
    """
    try:
        # Buscar elementos con texto tipo precio
        candidates = []
        # Posibles "tarjetas": elementos que contienen un link <a> y un texto con moneda
        for sel in ("article", "li", "div"):
            try:
                items = response.css(sel)
            except Exception:
                continue
            for item in items:
                try:
                    text = (item.text or "")[:500]
                    if not CURRENCY_REGEX.search(text):
                        continue
                    # Debe tener al menos un link
                    if not item.css("a[href]"):
                        continue
                    attribs = dict(item.attrib) if item.attrib else {}
                    cls = attribs.get("class", "")
                    if not cls:
                        continue
                    candidates.append((item.tag, cls, attribs))
                except Exception:
                    continue

        if not candidates:
            return None

        # Quedarnos con los que tienen clases tipo "product/card/item"
        scored = []
        for tag, cls, attribs in candidates:
            score = 0
            cls_lower = cls.lower()
            for hint in CARD_CLASS_HINTS:
                if hint in cls_lower:
                    score += 2
            scored.append((score, tag, cls, attribs))

        if not scored:
            return None

        # Agrupar por (tag, primer-class-significativa) y elegir el mas frecuente con score>0
        groups = Counter()
        repr_map = {}
        for score, tag, cls, attribs in scored:
            classes = [c for c in cls.split() if c and not c.startswith(("is-", "has-", "js-"))]
            if not classes:
                continue
            key = (tag, classes[0])
            groups[key] += 1 + score
            repr_map.setdefault(key, (tag, cls, attribs))

        if not groups:
            return None

        (best_tag, best_class), _ = groups.most_common(1)[0]
        tag, cls, attribs = repr_map[(best_tag, best_class)]

        selector = _shortest_class_selector(tag, cls)
        if not selector:
            selector = f"{tag}.{best_class}"

        # Verificar que el selector matchea varios elementos
        try:
            matches = response.css(selector)
            if len(matches) >= 2:
                return selector, matches
        except Exception:
            pass

        # Fallback: solo la primera clase
        selector = f"{best_tag}.{best_class}"
        try:
            matches = response.css(selector)
            if len(matches) >= 2:
                return selector, matches
        except Exception:
            pass

        return None
    except Exception:
        return None


def detect_inner_selector(container, hints: tuple, require_text: bool = False) -> Optional[str]:
    """
    Busca dentro de un contenedor un elemento cuyo className contenga uno de los hints.
    Si require_text=True, prefiere un elemento cuyo texto NO este vacio.
    """
    best_with_text = None
    best_any = None
    try:
        for el in container.css("*"):
            try:
                attribs = dict(el.attrib) if el.attrib else {}
                if not _has_class_hint(attribs, hints):
                    continue
                cls = attribs.get("class", "")
                classes = [c for c in cls.split() if c]
                chosen = None
                for c in classes:
                    if any(h in c.lower() for h in hints):
                        chosen = c
                        break
                if chosen is None and classes:
                    chosen = classes[0]
                if not chosen:
                    continue
                sel = f"{el.tag}.{chosen}"
                if best_any is None:
                    best_any = sel
                text = (el.text or "").strip() if hasattr(el, "text") else ""
                if text:
                    if best_with_text is None:
                        best_with_text = sel
                    if not require_text:
                        return sel
            except Exception:
                continue
    except Exception:
        pass
    return best_with_text or best_any


def _score_name_candidate(el, cls_chosen: str) -> int:
    """
    Puntua un candidato a 'nombre del producto'.
    Mas alto = mejor.
    """
    score = 0
    cls_low = cls_chosen.lower()
    # Clase que combina 'product' o 'item' con 'name' o 'title' es muy especifica
    if ("product" in cls_low or "item" in cls_low) and any(h in cls_low for h in ("name", "title")):
        score += 5
    elif any(h in cls_low for h in ("name", "title")):
        score += 2
    # Penalizar clases que parecen ser promociones / banners
    for bad in ("kasado", "promo", "banner", "ad-", "ads", "marketing", "sticker", "tag-"):
        if bad in cls_low:
            score -= 4
    # Texto largo es mejor (es un nombre real, no una etiqueta)
    try:
        text = (el.text or "").strip()
        if 10 <= len(text) <= 200:
            score += 2
        elif len(text) < 5:
            score -= 2
    except Exception:
        pass
    # Anchor es mejor (los nombres de producto son links a la pagina de detalle)
    if getattr(el, "tag", "") == "a":
        score += 1
    return score


def detect_name_selector(container) -> Optional[str]:
    """
    Detecta el selector del nombre del producto dentro del contenedor.
    Puntua candidatos con clase 'name/title' por especificidad (producto/item +
    name/title) y descarta promos (kasado, banner, promo).
    """
    candidates = []  # list of (score, selector)
    try:
        for el in container.css("*"):
            try:
                attribs = dict(el.attrib) if el.attrib else {}
                cls = attribs.get("class", "") or ""
                if not cls:
                    continue
                if not _has_class_hint(attribs, NAME_CLASS_HINTS):
                    continue
                classes = [c for c in cls.split() if c]
                chosen = None
                for c in classes:
                    if any(h in c.lower() for h in NAME_CLASS_HINTS):
                        chosen = c
                        break
                if chosen is None and classes:
                    chosen = classes[0]
                if not chosen:
                    continue
                sel = f"{el.tag}.{chosen}"
                score = _score_name_candidate(el, chosen)
                candidates.append((score, sel))
            except Exception:
                continue
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        if candidates[0][0] > 0:
            return candidates[0][1]

    # Fallback: headings con texto razonable
    for tag in ("h1", "h2", "h3", "h4"):
        try:
            els = container.css(tag)
        except Exception:
            continue
        for el in els:
            text = (el.text or "").strip()
            if 3 < len(text) < 250:
                cls = el.attrib.get("class", "") or ""
                first_class = next((c for c in cls.split() if c and not c.startswith(("is-", "has-", "js-"))), None)
                return f"{tag}.{first_class}" if first_class else tag

    # Fallback: anchor con texto significativo
    try:
        anchors = container.css("a")
    except Exception:
        anchors = []
    for a in anchors:
        text = (a.text or "").strip()
        if 10 < len(text) < 250:
            cls = a.attrib.get("class", "") or ""
            first_class = next((c for c in cls.split() if c and not c.startswith(("is-", "has-", "js-"))), None)
            return f"a.{first_class}" if first_class else "a"

    return None


# --------------------------------------------------
# DETECCION DE PAGINACION
# --------------------------------------------------

def detect_pagination(response, base_url: str) -> Tuple[str, str, int]:
    """
    Intenta detectar el parametro de paginacion.
    Retorna (pagination_param, pagination_mode, pagination_step).
    """
    try:
        links = response.css("a[href]")
        page_params = Counter()
        for link in links:
            href = link.attrib.get("href", "")
            if not href:
                continue
            try:
                parsed = urlparse(href if href.startswith("http") else urljoin(base_url, href))
                qs = parsed.query
            except Exception:
                continue
            for key in ("page", "p", "start", "offset", "from"):
                if f"{key}=" in qs:
                    page_params[key] += 1
                    break

        if page_params:
            top_param = page_params.most_common(1)[0][0]
            mode = "offset_items" if top_param in ("start", "offset", "from") else "page"
            return top_param, mode, 1
    except Exception:
        pass
    return "page", "page", 1


# --------------------------------------------------
# DETECTOR PRINCIPAL
# --------------------------------------------------

def _validate_selectors(response, cfg: Dict) -> bool:
    """
    Verifica que el config produce >=2 contenedores en la pagina actual
    y que dentro hay nombre + precio extraibles.
    """
    sel = cfg.get("css_product_selector")
    if not sel:
        return False
    try:
        containers = response.css(sel)
    except Exception:
        return False
    if not containers or len(containers) < 2:
        return False

    # Verificar que en el primer contenedor podamos sacar nombre y precio
    first = containers[0]
    price_sel = cfg.get("css_price_selector")
    if price_sel:
        try:
            price_els = first.css(price_sel)
            if not price_els:
                return False
        except Exception:
            return False
    return True


def _maybe_set_currency_with_ai(
    cfg: Dict, url: str, html: str, ai_api_key: Optional[str]
) -> None:
    """
    Si hay una API key disponible, pide a Gemini que detecte la moneda.
    Sobrescribe cfg['currency'] con la respuesta de la IA si llega.
    NOTA: se invoca solo durante la deteccion (pagina 1). Las paginas
    siguientes reutilizan el currency ya inferido.
    """
    if ai_detector is None or not ai_api_key:
        return
    try:
        currency = ai_detector.detect_currency_with_ai(url, html, api_key=ai_api_key)
        if currency:
            cfg["currency"] = currency
            cfg["currency_detected_by"] = "ai"
    except Exception as e:
        print(f"DETECT: detect_currency_with_ai fallo: {e}")


def detect_config(
    url: str,
    player: Optional[str] = None,
    ai_api_key: Optional[str] = None,
) -> Dict:
    """
    Inspecciona la URL y devuelve un dict de configuracion listo para usar
    con scraper.py / runner.scrape_site.

    Cascada (totalmente agnostica al sitio):
      1. Shopify (marcadores estrictos: cdn.shopify.com, etc.)
      2. JSON embebido generico ("products":[{...}], "items":[...])
      3. Next.js (__NEXT_DATA__)
      4. CSS price-first (heuristica primaria)
      5. CSS text-based (heuristica fallback)
      6. Gemini AI para selectores (si se provee ai_api_key)
      + Gemini AI para detectar la moneda (si se provee ai_api_key,
        independiente del resultado de las heuristicas)

    IMPORTANTE: detect_config se invoca UNA SOLA VEZ por URL. Las paginas
    siguientes (page=2, page=3, ...) reutilizan el config inferido aqui,
    asi que Gemini solo "ve" la pagina 1.

    ai_api_key: API key de Gemini a usar (BYOK). Si no se provee, se
                intenta usar GEMINI_API_KEY de .env. Sin key, la IA queda
                deshabilitada y solo se usan heuristicas.
    """
    if not player:
        player = urlparse(url).netloc.replace("www.", "").split(".")[0].title()

    print(f"DETECT: Analizando {url} (player tentativo: {player})")

    response = _fetch(url, use_browser=True)
    html = getattr(response, "html_content", "") or ""

    base_config = {
        "player": player,
        "url": url,
        "max_pages": 3,
        "pagination_param": "page",
        "pagination_mode": "page",
        "pagination_step": 1,
        "sku_mode": "url",
        "sku_url_separator": "-",
        "sku_url_count": 1,
    }

    # 1. Detectar paginacion (vale para los tres modos)
    pag_param, pag_mode, pag_step = detect_pagination(response, url)
    base_config["pagination_param"] = pag_param
    base_config["pagination_mode"] = pag_mode
    base_config["pagination_step"] = pag_step

    # 2. Shopify?
    if detect_shopify(html):
        print("DETECT: Shopify detectado")
        base_config["site_type"] = "shopify"
        base_config["detection_method"] = "shopify_markers"
        _maybe_set_currency_with_ai(base_config, url, html, ai_api_key)
        return base_config

    # 3. JSON embebido en el HTML?
    embed_marker = detect_json_embed(html)
    if embed_marker:
        print(f"DETECT: JSON embebido detectado (marcador: {embed_marker[:30]}...)")
        base_config["site_type"] = "json_embed"
        base_config["json_embed_marker"] = embed_marker
        base_config["detection_method"] = "json_embed"
        _maybe_set_currency_with_ai(base_config, url, html, ai_api_key)
        return base_config

    # 4. Next.js __NEXT_DATA__
    if detect_nextjs(html):
        print("DETECT: Next.js detectado (__NEXT_DATA__)")
        base_config["site_type"] = "nextjs"
        base_config["detection_method"] = "nextjs_data"
        _maybe_set_currency_with_ai(base_config, url, html, ai_api_key)
        return base_config

    # 5. CSS - estrategia primaria price-first (retailers genericos)
    print("DETECT: Aplicando heuristica CSS price-first...")
    pf_result = detect_css_by_prices(response)
    if pf_result:
        product_selector, containers, price_selector = pf_result
        candidate = dict(base_config)
        candidate["css_product_selector"] = product_selector
        if price_selector:
            candidate["css_price_selector"] = price_selector
        first = containers[0]
        name_sel = detect_name_selector(first)
        if not price_selector:
            price_sel_inner = detect_inner_selector(first, PRICE_CLASS_HINTS)
            if price_sel_inner:
                candidate["css_price_selector"] = price_sel_inner
        if name_sel:
            candidate["css_name_selector"] = name_sel
        candidate["css_product_url_selector"] = "a"
        candidate["site_type"] = "css"
        candidate["detection_method"] = "css_price_first"
        candidate["products_found_in_sample"] = len(containers)
        if _validate_selectors(response, candidate):
            print(
                f"DETECT: CSS price-first OK -> product={product_selector}, "
                f"price={candidate.get('css_price_selector')}, name={name_sel}, "
                f"items={len(containers)}"
            )
            _maybe_set_currency_with_ai(candidate, url, html, ai_api_key)
            return candidate
        print("DETECT: price-first no valido los selectores, probando text-based...")

    # 5. CSS - fallback heuristica basada en texto
    print("DETECT: Aplicando heuristica CSS basada en texto...")
    product_result = detect_css_product_selector(response)
    if product_result:
        product_selector, containers = product_result
        candidate = dict(base_config)
        candidate["css_product_selector"] = product_selector
        first = containers[0]
        name_sel = detect_name_selector(first)
        price_sel = detect_inner_selector(first, PRICE_CLASS_HINTS)
        if name_sel:
            candidate["css_name_selector"] = name_sel
        if price_sel:
            candidate["css_price_selector"] = price_sel
        candidate["css_product_url_selector"] = "a"
        candidate["site_type"] = "css"
        candidate["detection_method"] = "css_heuristic_text"
        candidate["products_found_in_sample"] = len(containers)
        if _validate_selectors(response, candidate):
            print(
                f"DETECT: CSS text-based OK -> product={product_selector}, "
                f"name={name_sel}, price={price_sel}, items={len(containers)}"
            )
            _maybe_set_currency_with_ai(candidate, url, html, ai_api_key)
            return candidate
        print("DETECT: text-based no valido los selectores")

    # 7. Fallback final: pedir ayuda a Gemini (BYOK o env)
    if ai_detector is not None:
        print("DETECT: Heuristicas agotadas, consultando IA (Gemini)...")
        ai_cfg = ai_detector.detect_with_ai(url, html, api_key=ai_api_key)
        if ai_cfg:
            merged = dict(base_config)
            merged.update(ai_cfg)
            merged["url"] = url
            merged["player"] = player
            if _validate_selectors(response, merged):
                print("DETECT: IA produjo selectores validos")
                # La IA ya devolvio currency en ai_cfg, asi que no llamamos
                # detect_currency_with_ai (seria un call duplicado a Gemini).
                return merged
            print("DETECT: IA produjo selectores pero NO validaron; no se usaran (evita 60s de wait_selector fantasma)")
            # Devolver fallback minimal con notas de la IA (informativo, sin usar selectores)
            base_config["site_type"] = "css"
            base_config["detection_method"] = "fallback"
            base_config["warning"] = "Selectores no validados; usa config manual o reintenta"
            base_config["ai_suggested_but_invalid"] = {
                k: v for k, v in ai_cfg.items() if k.startswith("css_") or k == "notes"
            }
            return base_config

    print("DETECT: Sin selectores. Devuelvo fallback minimal.")
    base_config["site_type"] = "css"
    base_config["detection_method"] = "fallback"
    base_config["warning"] = "No se detecto selector de producto automaticamente"
    return base_config
