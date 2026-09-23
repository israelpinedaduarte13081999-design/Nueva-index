"""Detección de oficio / tipo de proyecto y filtro de catálogo."""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.parse
import urllib.request

OFICIOS = {
    "roofing": {
        "label": "Roofing / Techos",
        "codigo_ui": "RFG",
        "prefijos": ("RFG", "EXT-ROF", "EXTROF"),
        "include": r"roof|techo(?!\s*raso)|shingle|teja asfalt|teja arquitect|sbs|epdm|tpo|eagleview|roofr|"
                   r"starter strip|valley metal|limahoya|ridge cap|cumbrera|\bhips?\b|\brakes?\b|"
                   r"tear.?off|ice\s*&\s*water|drip\s*edge",
        "exclude": r"siding|hardie|vinilo exterior|ventana|window|cerca|fence|alfombra|lvt|lvp|"
                   r"drywall|tablaroca|baño|bathroom|gabinete|cabinet",
        "busquedas": ("RFG", "ROF", "techo", "shingle", "teja", "starter", "sbs"),
    },
    "siding": {
        "label": "Siding / Revestimiento",
        "codigo_ui": "SDG",
        "prefijos": ("SDG", "EXT-SDG", "EXTSDG"),
        "include": r"siding|hardie|revestimiento exterior|soffit|fascia|vinyl siding|fiber.?cement",
        "exclude": r"teja asfalt|architectural shingle|roof report|sbs|epdm",
        "busquedas": ("siding", "SDG", "hardie", "soffit", "fascia"),
    },
    "flooring": {
        "label": "Pisos",
        "codigo_ui": "FCT",
        "prefijos": ("FCT", "WDH", "CPR", "FCC", "FLR", "EXT-FLR"),
        "include": r"piso|floor(?:ing)?|lvt|lvp|hardwood|alfombra|carpet|laminat|vinilo(?!\s*siding)|spc",
        "exclude": r"techo|roofing|siding|drywall|ducha|shower pan",
        "busquedas": ("piso", "lvt", "alfombra", "FCC", "FCT", "hardwood"),
    },
    "bathroom": {
        "label": "Baños",
        "codigo_ui": "BTR",
        "prefijos": ("BTR", "STP", "BTH"),
        "include": r"baño|bano|bath(?:room)?|ducha|shower|vanity|inodoro|toilet|shower pan",
        "exclude": r"techo|roofing|siding|alfombra residencial",
        "busquedas": ("baño", "bath", "ducha", "vanity", "BTR"),
    },
    "kitchen": {
        "label": "Cocina",
        "codigo_ui": "KTR",
        "prefijos": ("KTR", "CAB", "CTR", "KIT"),
        "include": r"cocina|kitchen|gabinete|cabinet|encimera|countertop|backsplash",
        "exclude": r"techo|roofing|siding",
        "busquedas": ("cocina", "gabinete", "CAB", "KTR"),
    },
    "drywall": {
        "label": "Drywall",
        "codigo_ui": "DRY",
        "prefijos": ("DRY", "DEM-DRY"),
        "include": r"drywall|tablaroca|sheetrock|yeso|tape\s*(and|&)\s*float|chirock",
        "exclude": r"teja|roofing|siding|lvt",
        "busquedas": ("drywall", "DRY", "tablaroca"),
    },
    "painting": {
        "label": "Pintura",
        "codigo_ui": "PNT",
        "prefijos": ("PNT", "PXT", "EXT-PNT"),
        "include": r"pintura|paint(?:ing)?|esmalte|primer",
        "exclude": r"teja asfalt|siding hardie",
        "busquedas": ("pintura", "PNT", "paint"),
    },
    "tile": {
        "label": "Azulejo / Tile",
        "codigo_ui": "TIL",
        "prefijos": ("TIL",),
        "include": r"azulejo|tile|porcelanato|ceramica|cerámica",
        "exclude": r"roofing|siding|carpet",
        "busquedas": ("azulejo", "tile", "TIL", "porcelanato"),
    },
    "framing": {
        "label": "Framing / Estructura",
        "codigo_ui": "FRM",
        "prefijos": ("FRM", "TRU", "SUB"),
        "include": r"framing|enmarcado|armadura|cercha|vigueta|subfloor|muros de carga",
        "exclude": r"siding|roofing shingle",
        "busquedas": ("framing", "FRM", "cercha"),
    },
    "windows": {
        "label": "Ventanas",
        "codigo_ui": "WND",
        "prefijos": ("WND", "EXT-WND"),
        "include": r"ventana|window",
        "exclude": r"roofing|siding hardie",
        "busquedas": ("ventana", "WND", "window"),
    },
    "gutters": {
        "label": "Canaletas",
        "codigo_ui": "GUT",
        "prefijos": ("GUT", "EXT-GUT"),
        "include": r"canaleta|gutter|downspout|bajante",
        "exclude": r"siding|drywall",
        "busquedas": ("canaleta", "GUT", "gutter"),
    },
    "plumbing": {
        "label": "Plomería",
        "codigo_ui": "PLM",
        "prefijos": ("PLM",),
        "include": r"plomer|plumb|pex|drenaje",
        "exclude": r"roofing|siding",
        "busquedas": ("plomer", "PLM", "pex"),
    },
    "electrical": {
        "label": "Electricidad",
        "codigo_ui": "ELE",
        "prefijos": ("ELE", "INT-ELE"),
        "include": r"electric|cableado|tomacorriente|panel el",
        "exclude": r"roofing|siding",
        "busquedas": ("electric", "ELE"),
    },
    "hvac": {
        "label": "HVAC",
        "codigo_ui": "HVC",
        "prefijos": ("HVC",),
        "include": r"hvac|climatiz|ducto|condensador",
        "exclude": r"roofing|siding",
        "busquedas": ("hvac", "HVC", "ducto"),
    },
    "demolition": {
        "label": "Demolición",
        "codigo_ui": "DEM",
        "prefijos": ("DEM",),
        "include": r"demolici[oó]n|demolition|haul off|retiro de escombros",
        "exclude": r"",
        "busquedas": ("demolici", "DEM"),
    },
    "deck": {
        "label": "Deck / Terraza",
        "codigo_ui": "DCK",
        "prefijos": ("DCK", "TRZ", "PRC"),
        "include": r"\bdeck\b|terraza|porche|barandal",
        "exclude": r"roofing|siding",
        "busquedas": ("deck", "terraza", "TRZ"),
    },
}

_CODIGO_UI = {meta["codigo_ui"]: key for key, meta in OFICIOS.items()}
_CODIGO_UI.update({
    "WDH": "flooring", "CPR": "flooring", "FCC": "flooring",
    "STP": "bathroom", "CAB": "kitchen", "CTR": "kitchen", "KIT": "kitchen",
    "PXT": "painting", "TRU": "framing", "SUB": "framing",
})

_CACHE_CATALOGO = None


def _texto(*partes):
    return " ".join(str(p or "") for p in partes)


def _codigo_clave(texto):
    return re.sub(r"[^A-Z0-9]", "", str(texto or "").upper())


def normalizar_oficio(valor):
    raw = str(valor or "").strip()
    if not raw:
        return ""
    bajo = raw.lower()
    if bajo in OFICIOS:
        return bajo
    match = re.search(r"\[([A-Z]{2,4})\]", raw.upper())
    if match and match.group(1) in _CODIGO_UI:
        return _CODIGO_UI[match.group(1)]
    compact = _codigo_clave(raw)
    if compact in _CODIGO_UI:
        return _CODIGO_UI[compact]
    alias = {
        "techo": "roofing", "roof": "roofing", "roofs": "roofing", "roof_report": "roofing",
        "pisos": "flooring", "floor": "flooring", "floors": "flooring",
        "bano": "bathroom", "baño": "bathroom", "bath": "bathroom",
        "cocina": "kitchen", "revestimiento": "siding",
        "pintura": "painting", "tablaroca": "drywall",
        "azulejo": "tile", "estructura": "framing",
        "canaletas": "gutters", "plomeria": "plumbing", "plomería": "plumbing",
        "electricidad": "electrical",
    }
    for clave, oficio in alias.items():
        if clave in bajo:
            return oficio
    return detectar_oficio(raw)


def detectar_oficio(*partes, preferido=""):
    hint = normalizar_oficio(preferido) if preferido else ""
    blob = _texto(*partes)
    if not blob.strip() and hint in OFICIOS:
        return hint
    mejor = (hint if hint in OFICIOS else "", 3 if hint in OFICIOS else 0)
    for key, meta in OFICIOS.items():
        score = 3 if key == hint else 0
        if re.search(meta["include"], blob, re.I):
            score += 8
        excl = meta.get("exclude") or ""
        if excl and re.search(excl, blob, re.I):
            score -= 6
        ui = meta.get("codigo_ui") or ""
        if ui and re.search(rf"\[{ui}\]|\b{ui}\b", blob, re.I):
            score += 10
        for pref in meta.get("prefijos") or ():
            if pref.lower() in blob.lower():
                score += 4
        if score > mejor[1]:
            mejor = (key, score)
    return mejor[0] if mejor[1] >= 6 else (preferido or "")


def blob_catalogo(row):
    if not isinstance(row, dict):
        return ""
    return _texto(
        row.get("codigo"),
        row.get("code"),
        row.get("descripcion"),
        row.get("description"),
        row.get("categoria"),
        row.get("category"),
    )


def fila_es_oficio(row, oficio):
    oficio = normalizar_oficio(oficio)
    if not oficio or oficio not in OFICIOS:
        return True
    meta = OFICIOS[oficio]
    blob = blob_catalogo(row)
    codigo = str(row.get("codigo") or row.get("code") or "").upper().strip()
    clave = _codigo_clave(codigo)
    prefijo_ok = False
    for pref in meta.get("prefijos") or ():
        p = pref.upper()
        if codigo.startswith(p) or clave.startswith(re.sub(r"[^A-Z0-9]", "", p)):
            prefijo_ok = True
            break
    excl = meta.get("exclude") or ""
    excluye = bool(excl and re.search(excl, blob, re.I))
    incluye = bool(re.search(meta["include"], blob, re.I))
    if excluye and not prefijo_ok:
        return False
    if prefijo_ok:
        return True
    return incluye and not excluye


def _creds():
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or ""
    )
    return url, key


def todas_filas_catalogo():
    global _CACHE_CATALOGO
    if _CACHE_CATALOGO is not None:
        return _CACHE_CATALOGO
    url, key = _creds()
    filas = []
    if not url or not key:
        _CACHE_CATALOGO = filas
        return filas
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    ctx = ssl._create_unverified_context()
    offset = 0
    while True:
        params = {"select": "*", "limit": "1000", "offset": str(offset)}
        query = urllib.parse.urlencode(params, safe="(),.*")
        req = urllib.request.Request(f"{url}/rest/v1/catalogo_items?{query}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                lote = json.loads(resp.read().decode("utf-8") or "[]")
        except Exception as err:
            print(f"oficios catalogo: {err}")
            break
        if not isinstance(lote, list) or not lote:
            break
        filas.extend(lote)
        if len(lote) < 1000:
            break
        offset += 1000
    _CACHE_CATALOGO = filas
    return filas


def filas_catalogo_oficio(oficio):
    oficio = normalizar_oficio(oficio)
    todas = todas_filas_catalogo()
    if not oficio:
        return list(todas)
    return [row for row in todas if fila_es_oficio(row, oficio)]


def _num_precio(row):
    for key in ("precio_base", "precio_unitario", "precio", "price", "rate"):
        raw = row.get(key) if isinstance(row, dict) else None
        if raw in (None, ""):
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if float(raw) > 0:
                return float(raw)
            continue
        texto = str(raw).replace("$", "").replace(",", "")
        match = re.search(r"-?\d+(?:\.\d+)?", texto)
        if match:
            valor = float(match.group(0))
            if valor > 0:
                return valor
    return 0.0


def _tokens(texto):
    return [t for t in re.split(r"[^a-záéíóúüñ0-9]+", str(texto or "").lower()) if len(t) >= 3]


def puntuar_fila_partida(row, descripcion, unidad=""):
    desc = str(descripcion or "").lower()
    blob = blob_catalogo(row).lower()
    if not desc or not blob:
        return 0
    score = 0
    for tok in _tokens(desc):
        if tok in blob:
            score += 3
    unidad_row = str(row.get("unidad") or row.get("unit") or "").upper().replace(" ", "")
    unidad = str(unidad or "").upper().replace(" ", "")
    if unidad and unidad_row and unidad == unidad_row:
        score += 6
    if _num_precio(row) > 0:
        score += 2
    return score


def mejores_precios_oficio(descripcion, unidad="", oficio="", limite=5):
    filas = filas_catalogo_oficio(oficio)
    ran = []
    for row in filas:
        precio = _num_precio(row)
        if precio <= 0:
            continue
        score = puntuar_fila_partida(row, descripcion, unidad)
        if score < 6:
            continue
        ran.append((score, row, precio))
    ran.sort(key=lambda x: x[0], reverse=True)
    vistos = set()
    out = []
    for score, row, precio in ran:
        codigo = str(row.get("codigo") or "").strip()
        desc = str(row.get("descripcion") or row.get("description") or "").strip()
        marca = (codigo, round(precio, 2))
        if marca in vistos:
            continue
        vistos.add(marca)
        out.append({
            "codigo": codigo,
            "descripcion": desc,
            "unidad": str(row.get("unidad") or row.get("unit") or unidad or "SF").upper(),
            "precio_unitario": round(precio, 2),
            "score": score,
            "oficio": normalizar_oficio(oficio),
        })
        if len(out) >= limite:
            break
    return out


def aplicar_precios_items(items, oficio, factor=1.0):
    if not isinstance(items, list):
        return items
    factor = float(factor or 1.0) or 1.0
    oficio = normalizar_oficio(oficio)
    label = (OFICIOS.get(oficio) or {}).get("label") or oficio
    for item in items:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description") or item.get("descripcion") or "")
        unidad = str(item.get("unit") or item.get("unidad") or "")
        matches = mejores_precios_oficio(desc, unidad, oficio, limite=1)
        if not matches:
            if oficio and label and not item.get("trade"):
                item["trade"] = label
            continue
        mejor = matches[0]
        precio = round(mejor["precio_unitario"] * factor, 2)
        actual = 0.0
        for key in ("unit_price", "precio_unitario", "price"):
            try:
                actual = float(item.get(key) or 0)
            except (TypeError, ValueError):
                actual = 0.0
            if actual > 0:
                break
        if actual <= 0 or mejor.get("score", 0) >= 12:
            item["unit_price"] = precio
            item["precio_unitario"] = precio
            item["precio_base"] = mejor["precio_unitario"]
            qty = 0.0
            try:
                qty = float(item.get("quantity") or item.get("cantidad") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty:
                item["total"] = round(qty * precio, 2)
        if mejor.get("codigo") and not str(item.get("codigo") or item.get("code") or "").strip():
            item["codigo"] = mejor["codigo"]
            item["code"] = mejor["codigo"]
        if label:
            item["trade"] = item.get("trade") or label
            item["oficio"] = oficio
    return items


def etiqueta_ui(oficio):
    meta = OFICIOS.get(normalizar_oficio(oficio)) or {}
    codigo = meta.get("codigo_ui") or ""
    label = meta.get("label") or oficio
    return f"[{codigo}] {label}" if codigo else label
