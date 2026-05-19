"""
API REST stateless del scraper.
Recibe una solicitud, scrapea, devuelve JSON. Sin DB, sin estado, sin batch.

Endpoints:
    GET    /health           -> Liveness probe
    POST   /detect           -> Inferir config JSON a partir de una URL
    POST   /scrape           -> Scrapear una URL (config opcional, autodetect si falta)
    POST   /scrape/bulk      -> Scrapear varias URLs en una sola peticion

Lanzar:
    uvicorn api:app --host 0.0.0.0 --port 5000
    python api.py --port 5000
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detector
import runner
import scraper


# --------------------------------------------------
# APP
# --------------------------------------------------

app = FastAPI(
    title="Scrapling Scraper API",
    description="Servicio HTTP stateless para detectar y scrapear catalogos de e-commerce. Devuelve JSON.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# MODELOS Pydantic
# --------------------------------------------------

class DetectRequest(BaseModel):
    url: HttpUrl
    player: Optional[str] = Field(None, description="Nombre opcional para identificar la fuente")
    gemini_api_key: Optional[str] = Field(
        None,
        description="API key de Gemini (BYOK). Si se provee, habilita el fallback IA cuando las heuristicas no detectan productos.",
    )


class ScrapeRequest(BaseModel):
    url: HttpUrl
    config: Optional[Dict[str, Any]] = Field(
        None,
        description="Config completa. Si se omite, se autodetecta desde la URL.",
    )
    max_pages: Optional[int] = Field(None, ge=1, le=50)
    player: Optional[str] = None
    gemini_api_key: Optional[str] = Field(
        None,
        description="API key de Gemini (BYOK). Solo se usa para fallback de deteccion si las heuristicas fallan.",
    )


class BulkScrapeRequest(BaseModel):
    urls: List[HttpUrl] = Field(..., min_items=1, max_items=20)
    max_pages: Optional[int] = Field(None, ge=1, le=50)
    gemini_api_key: Optional[str] = Field(
        None,
        description="API key de Gemini (BYOK) compartida para todas las URLs.",
    )


# --------------------------------------------------
# HEALTH
# --------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "service": "scrapling-api", "version": app.version}


# --------------------------------------------------
# DETECT
# --------------------------------------------------

@app.post("/detect")
def detect_endpoint(req: DetectRequest):
    """
    Inspecciona una URL de listado y devuelve la config JSON inferida.
    Stateless: no persiste nada.
    Si se provee `gemini_api_key`, se usa como fallback IA cuando las
    heuristicas no logren identificar selectores.
    """
    try:
        cfg = detector.detect_config(
            str(req.url),
            player=req.player,
            ai_api_key=req.gemini_api_key,
        )
        return {"success": True, "config": cfg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Detect error: {e}")


# --------------------------------------------------
# SCRAPE (UNA URL)
# --------------------------------------------------

def _build_config(req: ScrapeRequest) -> Dict[str, Any]:
    """Resuelve el config: usa el del request o autodetecta."""
    cfg = req.config or detector.detect_config(
        str(req.url),
        player=req.player,
        ai_api_key=req.gemini_api_key,
    )

    if "url" not in cfg or not cfg["url"]:
        cfg["url"] = str(req.url)
    if "player" not in cfg or not cfg["player"]:
        cfg["player"] = req.player or urlparse(str(req.url)).netloc.replace("www.", "")
    if req.max_pages:
        cfg["max_pages"] = req.max_pages
    cfg.setdefault("max_pages", 2)
    return cfg


@app.post("/scrape")
def scrape_endpoint(req: ScrapeRequest):
    """
    Scrapea una URL.
    - Si no se manda `config`, se autodetecta.
    - Siempre devuelve JSON con los productos extraidos.
    """
    cfg = _build_config(req)

    scraper.reset_traffic()
    start = time.time()
    products = runner.scrape_site(cfg)
    duration = round(time.time() - start, 2)

    return {
        "success": True,
        "player": cfg["player"],
        "url": cfg["url"],
        "site_type": cfg.get("site_type"),
        "config_used": cfg,
        "products": products,
        "metrics": {
            "count": len(products),
            "duration_seconds": duration,
            "traffic_mb": scraper.get_total_traffic_mb(),
        },
    }


# --------------------------------------------------
# BULK SCRAPE (VARIAS URLS)
# --------------------------------------------------

@app.post("/scrape/bulk")
def scrape_bulk(req: BulkScrapeRequest):
    """
    Scrapea varias URLs en secuencia (auto-detect por cada una).
    Devuelve un JSON con todos los productos agrupados por URL.
    """
    results = []
    total = 0
    scraper.reset_traffic()
    start = time.time()

    for u in req.urls:
        url_str = str(u)
        try:
            cfg = detector.detect_config(url_str, ai_api_key=req.gemini_api_key)
            if req.max_pages:
                cfg["max_pages"] = req.max_pages
            cfg.setdefault("max_pages", 2)

            products = runner.scrape_site(cfg)
            total += len(products)

            results.append({
                "url": url_str,
                "player": cfg.get("player"),
                "site_type": cfg.get("site_type"),
                "count": len(products),
                "config_used": cfg,
                "products": products,
            })
        except Exception as e:
            results.append({"url": url_str, "error": str(e)[:300]})

    return {
        "success": True,
        "results": results,
        "metrics": {
            "urls": len(req.urls),
            "total_products": total,
            "duration_seconds": round(time.time() - start, 2),
            "traffic_mb": scraper.get_total_traffic_mb(),
        },
    }


# El entry point es el ASGI app `app` arriba.
# Lanzar con uvicorn:
#   uvicorn api:app --host 0.0.0.0 --port 5000
