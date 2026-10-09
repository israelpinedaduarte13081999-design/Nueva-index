"""Análisis de PDF con Claude (Anthropic)."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl

import httpx

# IDs actuales de la API (los de Claude 3.5 ya no existen en esta cuenta).
MODELO_CLAUDE = "claude-sonnet-4-6"
MODELOS_CLAUDE = (
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-20250514",
)
API_URL = "https://api.anthropic.com/v1/messages"
MODELS_URL = "https://api.anthropic.com/v1/models"


def _api_key():
    return (os.getenv("ANTHROPIC_API_KEY") or "").strip()


def _extraer_json(texto):
    if texto is None:
        return None
    if isinstance(texto, (dict, list)):
        return texto
    raw = str(texto).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None


def _texto_respuesta(body):
    partes = []
    for block in (body or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            partes.append(block.get("text") or "")
        elif isinstance(block, str):
            partes.append(block)
    return "\n".join(partes).strip()


def _headers():
    return {
        "x-api-key": _api_key(),
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "pdfs-2024-09-25",
        "content-type": "application/json",
    }


def _es_modelo_ausente(err):
    texto = str(err).lower()
    return "404" in texto or "not_found" in texto or "model:" in texto


def _modelos_desde_api():
    """Si los alias fijos fallan, usa un Sonnet que la cuenta sí tenga."""
    try:
        with httpx.Client(verify=False, timeout=20.0) as http:
            resp = http.get(MODELS_URL, headers=_headers())
            if resp.status_code >= 400:
                return []
            data = resp.json().get("data") or []
        ids = [str(row.get("id") or "") for row in data if isinstance(row, dict)]
        sonnet = [i for i in ids if "sonnet" in i.lower()]
        return sonnet or [i for i in ids if i.startswith("claude-")]
    except Exception as err:
        print(f"--> Claude /v1/models: {err}")
        return []


def _post_claude(payload, timeout=120.0):
    ssl._create_default_https_context = ssl._create_unverified_context
    with httpx.Client(verify=False, timeout=float(timeout or 120.0)) as http:
        resp = http.post(API_URL, json=payload, headers=_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"Claude HTTP {resp.status_code}: {resp.text[:800]}")
        return resp.json()


def _pixmap_plano(page, zoom):
    import fitz

    matriz = fitz.Matrix(zoom, zoom)
    try:
        return page.get_pixmap(matrix=matriz, alpha=False, colorspace=fitz.csRGB)
    except Exception:
        return page.get_pixmap(matrix=matriz, alpha=False)


def _jpeg_plano(pix, calidad=72, max_bytes=1_200_000):
    jpeg = b""
    usada = int(calidad or 72)
    for q in (usada, 62, 50, 40, 32):
        try:
            jpeg = pix.tobytes("jpeg", jpg_quality=q)
        except TypeError:
            jpeg = pix.tobytes("jpeg")
        usada = q
        if jpeg and len(jpeg) <= int(max_bytes):
            break
    return jpeg, usada


def pdf_paginas_a_imagenes(pdf_bytes, dpi=140, max_paginas=8, max_lado=1600, calidad=72, max_bytes=1_200_000):
    """Renderiza cada página ya al tamaño final. Un plano tabloide a 180 dpi llenaba la RAM y devolvía 500."""
    if not pdf_bytes:
        raise RuntimeError("PDF vacío")
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    imagenes = []
    try:
        total = doc.page_count
        limite = min(int(max_paginas or 8), total)
        for i in range(limite):
            page = doc.load_page(i)
            rect = page.rect
            lado_pts = max(float(rect.width or 1), float(rect.height or 1))
            zoom = max(0.5, float(dpi or 140) / 72.0)
            if lado_pts * zoom > float(max_lado):
                zoom = float(max_lado) / lado_pts
            pix = _pixmap_plano(page, zoom)
            jpeg, usada = _jpeg_plano(pix, calidad=calidad, max_bytes=max_bytes)
            if jpeg and len(jpeg) > int(max_bytes):
                zoom *= 0.72
                pix = _pixmap_plano(page, zoom)
                jpeg, usada = _jpeg_plano(pix, calidad=50, max_bytes=max_bytes)
            if not jpeg:
                raise RuntimeError(f"No se pudo comprimir la página {i + 1}")
            imagenes.append({
                "page": i + 1,
                "pages_total": total,
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(jpeg).decode("ascii"),
                "width": pix.width,
                "height": pix.height,
                "bytes": len(jpeg),
                "quality": usada,
            })
            print(f"--> plano pág {i + 1}/{total}: {pix.width}x{pix.height} image/jpeg {len(jpeg)} bytes")
            pix = None
    finally:
        doc.close()
    if not imagenes:
        raise RuntimeError("No se pudo renderizar ninguna página del PDF")
    return imagenes


def _contenido_vision(imagenes, prompt):
    bloques = []
    for img in imagenes or []:
        data = img.get("data") if isinstance(img, dict) else None
        if not data:
            continue
        pagina = img.get("page") or len(bloques) + 1
        bloques.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("media_type") or "image/jpeg",
                "data": data,
            },
        })
        bloques.append({
            "type": "text",
            "text": f"[Página {pagina} del plano]",
        })
    bloques.append({"type": "text", "text": prompt})
    return bloques


def analizar_imagenes_json(imagenes, prompt, system=None, max_tokens=8192, modelo=None, modelos=None, timeout=180.0):
    """Envía imágenes renderizadas al endpoint de visión de Claude y devuelve JSON."""
    key = _api_key()
    if not key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")
    if not imagenes:
        raise RuntimeError("No hay imágenes de plano para analizar")
    nombres = list(modelos or MODELOS_CLAUDE)
    if modelo:
        nombres = [modelo] + [m for m in nombres if m != modelo]
    contenido = _contenido_vision(imagenes, prompt)
    ultimo = None
    intentados = set()

    def _probar(nombre):
        nonlocal ultimo
        if not nombre or nombre in intentados:
            return None
        intentados.add(nombre)
        payload = {
            "model": nombre,
            "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": contenido}],
        }
        if system:
            payload["system"] = system
        try:
            body = _post_claude(payload, timeout=timeout)
            texto = _texto_respuesta(body)
            print("========== CLAUDE VISIÓN RAW ==========")
            print((texto or "")[:8000])
            print("========== FIN CLAUDE VISIÓN RAW ==========")
            parsed = _extraer_json(texto)
            if parsed is None:
                raise RuntimeError("Claude Vision no devolvió JSON válido")
            print(f"--> Plano analizado con visión ({nombre}, {len(imagenes)} img)")
            return parsed
        except Exception as err:
            ultimo = err
            print(f"--> Claude visión {nombre}: {err}")
            texto = str(err).lower()
            if any(marca in texto for marca in ("401", "403", "413", "invalid x-api-key", "credit", "too large", "prompt is too long")):
                raise
            return None

    for nombre in nombres:
        parsed = _probar(nombre)
        if parsed is not None:
            return parsed
    if _es_modelo_ausente(ultimo):
        for extra in _modelos_desde_api():
            parsed = _probar(extra)
            if parsed is not None:
                return parsed
    raise ultimo or RuntimeError("No se pudo analizar el plano con Claude Vision")


def analizar_pdf_json(pdf_bytes, prompt, max_tokens=4096, modelo=None, modelos=None):
    """Envía el PDF a Claude y devuelve un dict/list JSON."""
    key = _api_key()
    if not key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")
    if not pdf_bytes:
        raise RuntimeError("PDF vacío")
    nombres = list(modelos or MODELOS_CLAUDE)
    if modelo:
        nombres = [modelo] + [m for m in nombres if m != modelo]
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    contenido_pdf = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_b64,
            },
        },
        {"type": "text", "text": prompt},
    ]
    ultimo = None
    intentados = set()
    cola = list(nombres)

    def _probar(nombre):
        nonlocal ultimo
        if not nombre or nombre in intentados:
            return None
        intentados.add(nombre)
        payload = {
            "model": nombre,
            "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": contenido_pdf}],
        }
        try:
            body = _post_claude(payload)
            texto = _texto_respuesta(body)
            print("========== CLAUDE RAW (texto) ==========")
            print((texto or "")[:8000])
            print("========== FIN CLAUDE RAW ==========")
            parsed = _extraer_json(texto)
            if parsed is None:
                raise RuntimeError("Claude no devolvió JSON válido")
            print(f"--> PDF analizado con {nombre}")
            try:
                print("========== CLAUDE JSON PARSEADO ==========")
                print(json.dumps(parsed, ensure_ascii=False, indent=2)[:8000])
                print("========== FIN CLAUDE JSON PARSEADO ==========")
            except Exception as dump_err:
                print(f"--> Claude JSON dump: {dump_err} tipo={type(parsed)}")
            return parsed
        except Exception as err:
            ultimo = err
            print(f"--> Claude {nombre}: {err}")
            return None

    for nombre in cola:
        parsed = _probar(nombre)
        if parsed is not None:
            return parsed
    if _es_modelo_ausente(ultimo):
        for extra in _modelos_desde_api():
            parsed = _probar(extra)
            if parsed is not None:
                return parsed
    raise ultimo or RuntimeError("No se pudo analizar el PDF con Claude")
