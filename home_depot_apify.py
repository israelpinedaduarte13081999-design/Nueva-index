"""Cliente Apify para precios y fotos locales de Home Depot."""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ACTOR_DEFAULT = "1mp/home-depot-prices"
DIAS_FRESCURA = 15
LIMITE_SKUS = 4
TIMEOUT_APIFY = 90


def token_apify():
    return (os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN") or "").strip()


def actor_home_depot():
    return (os.getenv("APIFY_HOME_DEPOT_ACTOR") or ACTOR_DEFAULT).strip() or ACTOR_DEFAULT


def _ahora_utc():
    return datetime.now(timezone.utc)


def parsear_fecha(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    texto = str(valor).strip()
    if not texto:
        return None
    texto = texto.replace("Z", "+00:00")
    try:
        fecha = datetime.fromisoformat(texto)
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def precio_stale(row, dias=DIAS_FRESCURA):
    if not isinstance(row, dict):
        return True
    precio = 0.0
    for key in ("precio_base", "precio_material", "precio_unitario", "price"):
        try:
            precio = float(row.get(key) or 0)
        except (TypeError, ValueError):
            precio = 0.0
        if precio > 0:
            break
    if precio <= 0:
        return True
    fecha = parsear_fecha(row.get("last_updated"))
    if fecha is None:
        return True
    return (_ahora_utc() - fecha) > timedelta(days=int(dias or DIAS_FRESCURA))


def consulta_home_depot(row):
    if not isinstance(row, dict):
        return ""
    textos = row.get("descripciones") if isinstance(row.get("descripciones"), dict) else {}
    desc = str(
        textos.get("en")
        or row.get("descripcion_en")
        or row.get("descripcion")
        or row.get("desc")
        or row.get("nombre")
        or ""
    ).strip()
    desc = re.sub(r"\s*[—\-]\s*(per square|por escuadra).*$", "", desc, flags=re.I)
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:90]


def _num(valor):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return numero if numero > 0 else 0.0


def precio_desde_home_depot(item, unidad=""):
    if not isinstance(item, dict):
        return None
    unitario = _num(item.get("unitPrice") or item.get("unit_price"))
    medida = str(item.get("unitMeasure") or item.get("unit_measure") or "").lower()
    precio = _num(item.get("price") or item.get("clearancePrice") or item.get("regularPrice"))
    unidad = str(unidad or "").upper()
    if unitario > 0 and re.search(r"sq\.?\s*ft|square\s*foot|pie cuadrado", medida):
        if unidad == "SQ":
            return round(unitario * 100, 2)
        if unidad in ("SF", ""):
            return round(unitario, 2)
    if precio > 0:
        return round(precio, 2)
    return None


def url_imagen_home_depot(item):
    if not isinstance(item, dict):
        return ""
    raw = str(
        item.get("imageUrl")
        or item.get("image_url")
        or item.get("image")
        or ""
    ).strip()
    if raw.lower().startswith(("http://", "https://")):
        return raw
    return ""


def _tokens(texto):
    return set(re.findall(r"[a-z0-9]{3,}", str(texto or "").lower()))


def parecido(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta))


def emparejar_producto(row, productos):
    consulta = consulta_home_depot(row)
    mejor, mejor_score = None, 0.0
    for item in productos or []:
        if not isinstance(item, dict):
            continue
        nombre = str(item.get("name") or item.get("title") or "")
        score = parecido(consulta, nombre)
        if score > mejor_score:
            mejor, mejor_score = item, score
    if mejor is None or mejor_score < 0.22:
        return None
    return mejor


def _actor_path(actor):
    return str(actor or ACTOR_DEFAULT).strip().replace("/", "~")


def scrapear_home_depot(terminos, zip_code):
    token = token_apify()
    if not token:
        raise RuntimeError("Falta APIFY_API_TOKEN en .env")
    busquedas = []
    vistos = set()
    for termino in terminos or []:
        texto = str(termino or "").strip()
        clave = texto.lower()
        if not texto or clave in vistos:
            continue
        vistos.add(clave)
        busquedas.append(texto)
    if not busquedas:
        return []
    zip5 = re.sub(r"\D", "", str(zip_code or ""))[:5]
    cuerpo = {
        "searchTerms": busquedas,
        "zipCode": zip5 if len(zip5) == 5 else "30327",
        "maxResults": 1,
        "inStockOnly": False,
    }
    url = (
        "https://api.apify.com/v2/acts/"
        + _actor_path(actor_home_depot())
        + "/run-sync-get-dataset-items?timeout="
        + str(TIMEOUT_APIFY)
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(cuerpo).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_APIFY + 15, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
    except urllib.error.HTTPError as err:
        detalle = err.read().decode("utf-8", errors="ignore")[:400]
        raise RuntimeError(f"Apify Home Depot {err.code}: {detalle}") from err
    return data if isinstance(data, list) else []
