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


def _post_claude(payload):
    ssl._create_default_https_context = ssl._create_unverified_context
    with httpx.Client(verify=False, timeout=120.0) as http:
        resp = http.post(API_URL, json=payload, headers=_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"Claude HTTP {resp.status_code}: {resp.text[:800]}")
        return resp.json()


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
