"""
Fallback dinamico con Gemini para detectar selectores de scraping.

Se activa cuando la heuristica CSS no puede inferir el config o cuando
los productos extraidos son inconsistentes (precio 0, nombre vacio, etc.).

Recibe un fragmento de HTML del listado y devuelve un dict con los
selectores CSS necesarios para extraer producto / nombre / precio / URL.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import json
import os
import re
import time
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Lista de modelos en orden de preferencia. Se prueba el siguiente si el actual da 429.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
GEMINI_FALLBACK_MODELS: List[str] = [
    m.strip() for m in (
        GEMINI_MODEL, "gemini-2.5-flash-lite", "gemini-1.5-flash-8b", "gemini-1.5-flash"
    ) if m.strip()
]
# Deduplicar preservando orden
seen = set()
GEMINI_FALLBACK_MODELS = [m for m in GEMINI_FALLBACK_MODELS if not (m in seen or seen.add(m))]


def _api_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


# --------------------------------------------------
# UTIL: reducir HTML para que quepa en el contexto
# --------------------------------------------------

def _trim_html(html: str, max_chars: int = 60000) -> str:
    """
    Quita head, scripts, styles, SVGs y noscript para reducir el tamaño.
    Recorta al maximo permitido.
    """
    if not html:
        return ""
    # Quitar bloques pesados
    html = re.sub(r"<head\b[^>]*>.*?</head>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<svg\b[^>]*>.*?</svg>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<noscript\b[^>]*>.*?</noscript>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    # Comprimir whitespace
    html = re.sub(r"\s+", " ", html)
    if len(html) > max_chars:
        # Intentar centrar en el cuerpo del listado: buscar primera ocurrencia
        # de patrones tipicos de producto
        hot_anchors = ["product", "price", "card", "item", "tile", "precio"]
        center = -1
        for anchor in hot_anchors:
            idx = html.lower().find(anchor)
            if idx > 0:
                center = idx
                break
        if center < 0:
            return html[:max_chars]
        start = max(0, center - max_chars // 4)
        return html[start : start + max_chars]
    return html


# --------------------------------------------------
# PROMPT
# --------------------------------------------------

PROMPT_TEMPLATE = """Eres un experto en web scraping. Te paso un fragmento de HTML de una página de listado de productos de e-commerce. Tu trabajo es identificar los selectores CSS que un scraper debería usar para extraer información de cada producto, y detectar la moneda real de la tienda.

URL de la página: {url}

HTML (recortado):
```html
{html}
```

Devuelve EXCLUSIVAMENTE un objeto JSON válido con esta forma (sin texto extra, sin markdown, sin code fences):

{{
  "css_product_selector": "selector CSS del CONTENEDOR de cada tarjeta de producto (debe matchear varios elementos repetidos en la página)",
  "css_name_selector": "selector CSS RELATIVO al contenedor que apunta al nombre del producto",
  "css_price_selector": "selector CSS RELATIVO al contenedor que apunta al precio actual del producto",
  "css_product_url_selector": "selector CSS RELATIVO que apunta al enlace del producto (normalmente 'a' o 'a.algo')",
  "css_discount_selector": "selector CSS RELATIVO al precio original/tachado o al porcentaje de descuento, o null si no hay",
  "discount_is_original_price": true_si_el_selector_de_descuento_apunta_a_un_precio_tachado_false_si_apunta_a_un_porcentaje,
  "sku_mode": "url o css",
  "sku_url_separator": "separador del codigo en la URL (normalmente '-' o '_')",
  "sku_url_count": numero_de_segmentos_finales_que_componen_el_codigo_de_referencia,
  "sku_css_selector": "selector CSS RELATIVO al contenedor con el codigo de referencia (sólo si sku_mode='css'), o null",
  "sku_css_attribute": "atributo HTML que contiene el codigo (ej: 'data-sku', 'content') o null si esta en el text",
  "currency": "Codigo ISO de la moneda REAL usada en los precios de esta tienda. NO te bases solo en el simbolo '$' porque puede ser USD, COP, MXN, ARS, CLP, etc. Mira el TLD del dominio, el path (/co/, /mx/, /es/), el formato de los precios (1.234.567 sugiere COP/CLP) y cualquier pista en el HTML (texto 'COP', 'MXN', selector de moneda).",
  "pagination_param": "el query param para paginar (page, p, start, offset)",
  "pagination_mode": "page o offset_items",
  "notes": "una linea muy corta describiendo en español qué identificaste"
}}

Reglas:
- El "css_product_selector" debe ser válido y devolver MÚLTIPLES elementos. NO uses :nth-child ni atributos id únicos.
- Si una clase tiene varios tokens (ej: "card product on-sale"), usa el tag.clase-mas-distintiva.
- Los selectores RELATIVOS no llevan el selector del contenedor delante (el scraper hace `container.css(price_selector)` por su cuenta).
- Si no hay descuento visible, "css_discount_selector": null y "discount_is_original_price": false.
- Si el código de referencia no se puede deducir de la URL, usa "sku_mode": "css".
- Sé conciso con "notes" (máximo 100 caracteres).
- "currency" es OBLIGATORIO: debe ser un código ISO de 3 letras (EUR, USD, COP, MXN, ARS, CLP, GBP, BRL, PEN, etc.).
"""


CURRENCY_PROMPT = """Identifica la moneda en la que esta tienda online vende sus productos.

URL: {url}

Pistas del HTML (precio de ejemplo + metadatos):
```
{snippet}
```

Considera:
- El TLD del dominio (.com.co → COP, .com.mx → MXN, .co.uk → GBP, .com.ar → ARS, .com.br → BRL).
- El path del URL (/co/, /mx/, /es/, /us/, etc.).
- El formato numérico (1.234.567 sin decimales sugiere COP/CLP; 1,234.56 sugiere USD).
- Cualquier mención explícita de "COP", "MXN", "$ USD", "EUR" en el HTML.
- El símbolo "$" SOLO no es suficiente: puede ser USD, COP, MXN, ARS, CLP, etc.

Devuelve EXCLUSIVAMENTE este JSON (sin texto extra, sin markdown):
{{
  "currency": "código ISO 4217 de 3 letras",
  "confidence": número entre 0 y 1,
  "reason": "una línea muy corta en español"
}}
"""


def _build_prompt(url: str, html: str) -> str:
    return PROMPT_TEMPLATE.format(url=url, html=_trim_html(html))


def _build_currency_prompt(url: str, snippet: str) -> str:
    if len(snippet) > 8000:
        snippet = snippet[:8000]
    return CURRENCY_PROMPT.format(url=url, snippet=snippet)


# --------------------------------------------------
# LLAMADA A GEMINI
# --------------------------------------------------

def _call_gemini_once(model: str, prompt: str, api_key: str, timeout: int = 60) -> tuple:
    """
    Una llamada a un modelo concreto con una API key especifica.
    Retorna (status_code, text_o_error).
    """
    headers = {"Content-Type": "application/json"}
    params = {"key": api_key}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    try:
        r = requests.post(_api_url(model), params=params, json=payload, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return r.status_code, r.text[:300]
        data = r.json()
        try:
            return 200, data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return 500, f"Estructura inesperada: {str(data)[:200]}"
    except Exception as e:
        return -1, str(e)


def _call_gemini(prompt: str, api_key: str, timeout: int = 60) -> Optional[str]:
    """
    Intenta varios modelos en cascada con retry si hay 429 (rate-limit).
    """
    if not api_key:
        return None

    for model in GEMINI_FALLBACK_MODELS:
        for attempt in range(2):
            status, body = _call_gemini_once(model, prompt, api_key, timeout)
            if status == 200:
                if attempt > 0 or model != GEMINI_FALLBACK_MODELS[0]:
                    print(f"AI: OK con modelo={model} (intento {attempt + 1})")
                return body
            if status == 429:
                wait = 2 ** attempt
                print(f"AI: 429 rate-limit con {model}, esperando {wait}s antes de reintentar...")
                time.sleep(wait)
                continue
            print(f"AI: {model} respondio {status}: {body[:150]}")
            break
        else:
            print(f"AI: {model} agoto reintentos por 429, pasando al siguiente modelo")
            continue
    print("AI: Todos los modelos Gemini fallaron")
    return None


# --------------------------------------------------
# PARSEO Y SANEAMIENTO DE LA RESPUESTA
# --------------------------------------------------

def _parse_gemini_json(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    raw = raw.strip()
    # Si vino con code fences a pesar de la instruccion, quitarlos
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Intentar extraer el primer objeto {...}
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return None


def _sanitize_selector(sel) -> Optional[str]:
    """Acepta selectores 'razonables', rechaza None / vacios / muy largos / con :nth."""
    if not sel or not isinstance(sel, str):
        return None
    sel = sel.strip()
    if not sel or sel.lower() in ("null", "none", "n/a"):
        return None
    if len(sel) > 200:
        return None
    return sel


# --------------------------------------------------
# API PUBLICA
# --------------------------------------------------

def _extract_price_context(html: str, max_chars: int = 6000) -> str:
    """Extrae un snippet del HTML alrededor de la primera mencion de precio."""
    if not html:
        return ""
    # Limpiar
    h = re.sub(r"<head\b[^>]*>.*?</head>", "", html, flags=re.IGNORECASE | re.DOTALL)
    h = re.sub(r"<script\b[^>]*>.*?</script>", "", h, flags=re.IGNORECASE | re.DOTALL)
    h = re.sub(r"<style\b[^>]*>.*?</style>", "", h, flags=re.IGNORECASE | re.DOTALL)
    h = re.sub(r"\s+", " ", h)
    # Buscar ancla "price"
    idx = h.lower().find("price")
    if idx < 0:
        idx = h.lower().find("precio")
    if idx < 0:
        return h[:max_chars]
    start = max(0, idx - 500)
    return h[start : start + max_chars]


def detect_currency_with_ai(
    url: str, html: str, api_key: Optional[str] = None
) -> Optional[str]:
    """
    Pregunta a Gemini que moneda usa esta tienda.
    Util cuando la heuristica TLD no es concluyente y el simbolo '$' es ambiguo.
    Retorna el codigo ISO de 3 letras o None si falla.
    """
    key = (api_key or GEMINI_API_KEY or "").strip()
    if not key or not html:
        return None

    snippet = _extract_price_context(html)
    if not snippet:
        return None

    print(f"AI: Consultando Gemini para detectar moneda de {url[:80]}...")
    raw = _call_gemini(_build_currency_prompt(url, snippet), api_key=key)
    if not raw:
        return None

    parsed = _parse_gemini_json(raw)
    if not parsed:
        return None

    currency = parsed.get("currency")
    if not isinstance(currency, str):
        return None
    currency = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        return None
    confidence = parsed.get("confidence", 1.0)
    reason = parsed.get("reason", "")
    print(f"AI: Moneda detectada = {currency} (confidence={confidence}) — {reason[:100]}")
    return currency


def detect_with_ai(url: str, html: str, api_key: Optional[str] = None) -> Optional[Dict]:
    """
    Llama a Gemini con el HTML y devuelve un dict listo para merge con la config.
    Prioridad de la API key: parametro de la llamada (BYOK) > GEMINI_API_KEY de .env.
    None si la llamada falla, no hay key, o el HTML es insuficiente.
    """
    key = (api_key or GEMINI_API_KEY or "").strip()
    if not key:
        print("AI: No hay API key (ni en request ni en .env), fallback IA deshabilitado")
        return None
    if not html or len(html) < 500:
        return None

    print(f"AI: Consultando Gemini ({GEMINI_MODEL}) para {url[:80]}...")
    raw = _call_gemini(_build_prompt(url, html), api_key=key)
    if not raw:
        return None

    parsed = _parse_gemini_json(raw)
    if not parsed:
        print(f"AI: No se pudo parsear JSON de Gemini. Crudo: {raw[:200]}")
        return None

    # Saneamiento
    result = {}
    for key in (
        "css_product_selector", "css_name_selector", "css_price_selector",
        "css_product_url_selector", "css_discount_selector",
        "sku_css_selector", "sku_css_attribute",
    ):
        clean = _sanitize_selector(parsed.get(key))
        if clean:
            result[key] = clean

    for key in ("sku_mode", "currency", "pagination_param", "pagination_mode", "notes"):
        v = parsed.get(key)
        if isinstance(v, str) and v.strip():
            result[key] = v.strip()

    if isinstance(parsed.get("sku_url_separator"), str):
        result["sku_url_separator"] = parsed["sku_url_separator"]
    if isinstance(parsed.get("sku_url_count"), (int, float)):
        result["sku_url_count"] = int(parsed["sku_url_count"])
    if isinstance(parsed.get("discount_is_original_price"), bool):
        result["discount_is_original_price"] = parsed["discount_is_original_price"]

    result["detection_method"] = "ai_gemini"
    result["site_type"] = "css"

    print(f"AI: Selectores detectados -> product={result.get('css_product_selector')}, "
          f"name={result.get('css_name_selector')}, price={result.get('css_price_selector')}, "
          f"notes={result.get('notes', '')[:80]}")
    return result
