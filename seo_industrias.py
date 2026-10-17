"""Programmatic SEO industries for QuantixBuilds landing pages."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

ARCHIVO_INDUSTRIAS = Path(__file__).resolve().parent / "seo_industrias.json"
_SLUG_OK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _fila_limpia(raw):
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("slug") or "").strip().lower()
    name = str(raw.get("name") or "").strip()
    if not slug or not name or not _SLUG_OK.match(slug):
        return None
    keywords = raw.get("keywords") or []
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except Exception:
            keywords = [part.strip() for part in keywords.split(",") if part.strip()]
    if not isinstance(keywords, list):
        keywords = []
    keywords = [str(k).strip() for k in keywords if str(k).strip()]
    return {
        "slug": slug,
        "name": name,
        "contractor": str(raw.get("contractor") or name.lower() + " contractors").strip(),
        "job": str(raw.get("job") or "construction work").strip(),
        "title": str(raw.get("title") or f"{name} Estimating Software | QuantixBuilds").strip(),
        "h1": str(raw.get("h1") or f"{name} estimating software for U.S. contractors").strip(),
        "meta": str(raw.get("meta") or "").strip(),
        "lead": str(raw.get("lead") or "").strip(),
        "keywords": keywords,
        "path": f"/{slug}-software",
    }


def _desde_json():
    try:
        data = json.loads(ARCHIVO_INDUSTRIAS.read_text(encoding="utf-8"))
    except Exception:
        return []
    filas = data.get("industrias") if isinstance(data, dict) else data
    out = []
    for raw in filas or []:
        fila = _fila_limpia(raw)
        if fila:
            out.append(fila)
    return out


def _desde_supabase(sb):
    if sb is None:
        return []
    try:
        res = sb.table("seo_industrias").select("*").execute()
    except Exception:
        return []
    filas = []
    for raw in list(res.data or []):
        fila = _fila_limpia(raw)
        if fila:
            filas.append(fila)
    return filas


@lru_cache(maxsize=1)
def _industrias_json():
    return tuple(_desde_json())


_COMBINADO = None


def listar_industrias(sb=None):
    global _COMBINADO
    if _COMBINADO is not None:
        return _COMBINADO
    base = {fila["slug"]: dict(fila) for fila in _industrias_json()}
    for fila in _desde_supabase(sb) if sb is not None else []:
        base[fila["slug"]] = fila
    _COMBINADO = tuple(base.values())
    return _COMBINADO


def industria_por_slug(slug, sb=None):
    clave = str(slug or "").strip().lower()
    if not _SLUG_OK.match(clave or ""):
        return None
    for fila in listar_industrias(sb):
        if fila["slug"] == clave:
            return fila
    return None


def relacionadas(slug, limite=10, sb=None):
    clave = str(slug or "").strip().lower()
    otras = [fila for fila in listar_industrias(sb) if fila["slug"] != clave]
    return otras[: max(0, int(limite or 0))]


def url_industria(slug, origin="https://quantixbuilds.com", sb=None):
    fila = industria_por_slug(slug, sb)
    if not fila:
        return ""
    return str(origin).rstrip("/") + fila["path"]


def fecha_lastmod():
    try:
        ts = ARCHIVO_INDUSTRIAS.stat().st_mtime
        return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()
