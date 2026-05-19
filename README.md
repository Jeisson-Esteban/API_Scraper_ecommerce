# API_Scraper_ecommerce

> API REST que detecta y scrapea catálogos de e-commerce con cascada heurística + fallback Gemini AI (BYOK).

## Qué es esto

Un microservicio HTTP que recibe la URL de un listado de productos de cualquier tienda online y te devuelve los productos en JSON, sin que tengas que configurarle nada por tienda.

- **Qué es**: una API stateless. Le mandas `POST /scrape` con una URL y te devuelve los productos.
- **Por qué existe**: la mayoría de scrapers están atados a una tienda específica — cuando aparece otra, toca reescribir todo. Acá la lógica de "qué selectores usar" la decide el servicio en runtime mirando el HTML, no un humano por adelantado.
- **Para qué sirve / a quién le sirve**: equipos que necesitan extraer catálogos de varias tiendas distintas (comparadores de precios, agregadores, dashboards internos) sin escribir un scraper a mano por cada dominio.

Por dentro corre una cascada: primero detecta si la página es Shopify, después si tiene un JSON embebido tipo `"products":[...]`, después Next.js (`__NEXT_DATA__`), después heurísticas CSS. Si nada cuadra y el cliente mandó su propia API key de Gemini, la IA infiere los selectores en caliente.

## Stack

- **Lenguaje**: Python 3.11+
- **Frameworks / libs clave**: FastAPI, Uvicorn, Pydantic, [Scrapling](https://github.com/D4Vinci/Scrapling) (StealthyFetcher + Patchright/Chromium para anti-detección)
- **Servicios externos**: Gemini API (opcional, BYOK — el cliente trae su propia key)

## Casos de uso

- **Caso 1 — Agregador de precios multi-tienda**: tu sistema necesita pollear cada noche 15 catálogos distintos y guardar precios. Mandas 15 requests a `/scrape`, te devuelve JSON listo para meter a tu DB.
- **Caso 2 — Catálogo nuevo sin escribir código**: el equipo de producto quiere monitorear una tienda nueva. En vez de programar un scraper, le pasas la URL al endpoint y ya corre. Si la heurística falla, mandas la key de Gemini y la IA arma el config.
- **Caso 3 — Detector de estructura**: solo quieres entender cómo está armada una tienda (qué selectores CSS usa, qué tipo de plataforma corre). `POST /detect` te devuelve el config sin descargar los productos.
- **Caso 4 — Integración rápida con n8n / Make / Zapier**: cualquier herramienta no-code que pueda hacer un POST con JSON puede usar esta API. Cero acoplamiento.
- **Caso 5 — Comparadores ligeros para clientes**: vas a montar un MVP de comparador. No quieres invertir 2 semanas en N scrapers. Levantas este servicio y empiezas a iterar.

## Requisitos previos

- Python 3.11 o superior
- Docker (opcional, recomendado para correr en cualquier server)
- Una API key de Gemini si quieres el fallback IA — [se saca gratis acá](https://aistudio.google.com/apikey)

## Cómo usarla

### Instalación

```bash
git clone https://github.com/ejeisson81/API_Scraper_ecommerce.git
cd API_Scraper_ecommerce

python -m venv venv
# Windows
.\venv\Scripts\Activate.ps1
# Linux/macOS
source venv/bin/activate

pip install -r requirements.txt
python -m patchright install chromium

cp .env.example .env
# editar .env si quieres key fija de Gemini, si no la mandas por request
```

### Correrla

```bash
cd server
uvicorn api:app --host 0.0.0.0 --port 5000
```

Swagger interactivo: `http://localhost:5000/docs`

Con Docker:

```bash
docker compose up --build -d
```

### Ejemplo rápido

```bash
curl -X POST http://localhost:5000/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://alguna-tienda.com/categoria/zapatillas",
    "max_pages": 2,
    "gemini_api_key": "AIza..."
  }'
```

Respuesta (recortada):

```json
{
  "success": true,
  "player": "alguna-tienda.com",
  "site_type": "nextjs",
  "products": [
    {
      "product_name": "Tenis Modelo X",
      "codigo_referencia": "ABC-123",
      "price_final": 159990,
      "currency": "COP",
      "url": "https://alguna-tienda.com/p/abc-123",
      "scraped_at": "2026-05-19 12:34:56"
    }
  ],
  "metrics": {"count": 47, "duration_seconds": 18.4}
}
```

### Recomendaciones y tips

- **Tip 1**: si la heurística no detecta nada, mira el campo `detection_method` en la respuesta. Si dice `fallback` y no mandaste `gemini_api_key`, mándala y reintenta.
- **Tip 2**: Gemini se llama una sola vez por URL (en la página 1). Si paginas 10 páginas, la IA no se invoca 10 veces — la config se reusa. Eso te ahorra cuota.
- **Tip 3**: para tiendas latinas el símbolo `$` puede ser COP, MXN, ARS o CLP. Si tienes key de Gemini, la moneda se resuelve dinámicamente. Sin key, el servicio mira el TLD (`.com.co` → COP, etc.).
- **Ojo con**: `StealthyFetcher` evade detección de bots, pero **no evade baneos por IP**. Si lo corres desde una VM cloud y un dominio te bloquea, configura `PROXY_URL` con un proxy residencial.

## Estructura del proyecto

```
API_Scraper_ecommerce/
├── server/
│   ├── api.py            # FastAPI - endpoints HTTP, único entry point
│   ├── detector.py       # Cascada de detección agnóstica
│   ├── ai_detector.py    # Cliente Gemini (selectores + moneda)
│   ├── runner.py         # Paginación y dispatch por site_type
│   └── scraper.py        # Motores: CSS, Shopify, JSON embed, Next.js
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

Cada archivo tiene una responsabilidad clara — si quieres añadir un nuevo tipo de sitio (ej. una plataforma nueva), agregas una función `extract_xxx_products` en `scraper.py`, un detector en `detector.py`, y un dispatch en `runner.py`. Tres archivos tocados, sin tocar el resto.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET`  | `/health` | Liveness probe |
| `POST` | `/detect` | Devuelve el config JSON inferido para una URL |
| `POST` | `/scrape` | Scrapea una URL (autodetect si no le pasas `config`) |
| `POST` | `/scrape/bulk` | Hasta 20 URLs en un solo request |

Schema del producto devuelto:

```json
{
  "player": "string",
  "product_name": "string",
  "codigo_referencia": "string",
  "price_final": 0.0,
  "price_original": 0.0,
  "currency": "EUR | USD | GBP | COP | MXN | BRL | ARS | CLP | ...",
  "discount_pct": 0.0,
  "url": "https://...",
  "extraction_method": "shopify_json | json_embed | nextjs_data | (ausente para CSS)",
  "scraped_at": "YYYY-MM-DD HH:MM:SS"
}
```

## Variables de entorno

Ver [.env.example](.env.example). Las que aplican:

- `GEMINI_API_KEY` — opcional, key compartida si no quieres que cada cliente mande la suya en el request.
- `GEMINI_MODEL` — modelo por defecto. Hay cascada interna si el primero da 429.
- `PROXY_URL` — proxy residencial opcional (solo si tu IP está baneada en algún dominio).

## Contribuir / ideas para mejorarlo

Issues y PRs bienvenidos. Si lo rompiste, lo usaste o se te ocurrió algo, abre un issue.

Ideas que tengo en la cabeza y no he metido todavía:
- Soporte para sitios que cargan productos vía scroll infinito (algunos Shopify y Next.js modernos).
- Endpoint `/verify` que tome un config existente y revise si los selectores siguen funcionando (útil cuando la tienda cambia el HTML).
- Cache opcional del config detectado por dominio para no re-correr la cascada cada vez.

## Agradecimientos

Gracias por pasarte. Si te sirvió aunque sea para sacar una idea, ya valió la pena publicarlo.

Construido sobre los hombros de [Scrapling](https://github.com/D4Vinci/Scrapling), que se come Cloudflare y los fingerprint checks sin chistar.

## Licencia

MIT — ver [LICENSE](LICENSE).

---

Hecho con café en Bogotá por [Jeisson](https://github.com/ejeisson81) — dev backend / automatización.
