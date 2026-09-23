"""Parser independiente de reportes Roofr / Roof Report.

No forma parte del flujo de estimados generales. Extrae medidas por texto/regex,
consulta precios en catalogo_items (Supabase) según ZIP y arma partidas
con subtotal = cantidad × precio unitario (sin cargos mínimos si el total > $500).
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import ssl
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

UMBRAL_SIN_MINIMOS = 500.0
WASTE_TEJA = 1.15
TABLA_CATALOGO = "catalogo_items"

PRECIOS_RESPALDO = {
    "tear_off": 55.00,
    "arch_shingle": 85.00,
    "flat_sbs": 130.00,
    "starter": 1.25,
    "valley": 8.00,
    "ridges": 13.50,
    "hips": 8.50,
    "rakes": 1.25,
    "flashing": 4.50,
    "transitions": 6.50,
    "drip_edge": 2.25,
    "ventilation": 45.00,
}

# (clave, descripción, campo de cantidad, unidad, patrones de catálogo)
PLANTILLA_PARTIDAS = (
    ("tear_off", "Remoción de teja asfáltica (Tear-off)", "total_sq", "SQ", r"tear.?off|remoci[oó]n.{0,40}teja|retiro.{0,20}teja"),
    ("arch_shingle", "Instalación de teja arquitectónica", "pitched_sq_waste", "SQ", r"teja arquitect|architectural\s*shingle|(?<!total\s)shingle|instalaci[oó]n.{0,20}teja"),
    ("flat_sbs", "Instalación de techo plano (SBS Flat Roof)", "flat_sq", "SQ", r"flat\s*roof|sbs|techo plano"),
    ("starter", "Eaves / Starter Strip", "eaves_lf", "LF", r"starter|tira de inicio|\beaves?\b"),
    ("valley", "Valleys / Limahoya", "valleys_lf", "LF", r"valley|limahoya"),
    ("ridges", "Ridges / Ridge Cap", "ridges_lf", "LF", r"ridge\s*cap|shingle\s*ridge|hip\s*(and|&|/)\s*ridge|cumbrera(\s+asf|\s+de\s+teja)?|continuous\s*ridge\s*vent"),
    ("hips", "Hips / Cumbreras inclinadas", "hips_lf", "LF", r"\bhips?\b|lima[\s\-]?tesa"),
    ("rakes", "Rakes / Bordes inclinados", "rakes_lf", "LF", r"\brakes?\b|fald[oó]n"),
    ("drip_edge", "Drip edge / Gutter apron", "drip_edge_lf", "LF", r"drip.?edge|gutter.?apron|delantal|fald[oó]n de canal"),
    ("ventilation", "Ventilación de techo", "ventilation_ea", "EA", r"ventila|ridge.?vent|box.?vent|soffit.?vent"),
    ("flashing", "Wall / Step flashing", "flashing_lf", "LF", r"flashing|counter.?flash|tapajuntas"),
    ("transitions", "Transitions", "transitions_lf", "LF", r"transition"),
)

# Orden cronológico del alcance: R&R de escuadras, luego perímetros, luego accesorios.
FILAS_COTIZADOR = (
    ("rr_asphalt", "total_roof_area_sq", "SQ", "Remoción y reemplazo de techo asfáltico"),
    ("starter", "eaves_lf", "LF", "Eaves / Starter Strip"),
    ("valley", "valleys_lf", "LF", "Valleys / Limahoya"),
    ("ridges", "ridges_lf", "LF", "Ridges / Ridge Cap"),
    ("hips", "hips_lf", "LF", "Hips / Cumbreras inclinadas"),
    ("rakes", "rakes_lf", "LF", "Rakes / Bordes inclinados"),
    ("drip_edge", "drip_edge_lf", "LF", "Drip edge / Gutter apron"),
    ("ventilation", "ventilation_ea", "EA", "Ventilación de techo"),
    ("flat_sbs", "total_flat_area_sq", "SQ", "Instalación de techo plano (SBS Flat Roof)"),
)

# Códigos compactos (sin espacios) que aparecen en catalogo_items
CODIGOS_PLANTILLA = {
    "tear_off": {"1", "RFGTO", "RFGTEAR", "RFGDEM"},
    "arch_shingle": {"2", "RFGSHN", "RFGSHINGLE"},
    "flat_sbs": {"3", "RFGSBS", "RFGFLAT"},
    "starter": {"4", "RFGSTR", "RFGSTARTER"},
    "valley": {"5", "RFGVAL", "RFGVALLEY"},
    "hips": {"6", "RFGHIP", "RFGHIPS"},
    "ridges": {"7", "8", "RFGRDG", "RFGRIDGE", "RFGRIDGECAP", "RFGCAP"},
    "rakes": {"11", "9", "RFGRAK", "RFGRAKE"},
    "flashing": {"10", "RFGFLS", "RFGFLASH"},
    "transitions": {"12", "RFGTRN"},
    "drip_edge": {"RFGDRIP", "EXTROFDRIP", "RFGAPRON"},
    "ventilation": {"RFGVENT", "EXTROFVENT"},
}

PROMPT_REPORT_SUMMARY = """
Este PDF es un Roof Report (Roofr, EagleView, Hover, GAF QuickMeasure u otro informe de mediciones de techo).

Busca de forma FLEXIBLE. El título de la sección puede variar; usa la primera que encuentres:
- "Report Summary", "Report summary"
- "Measurements", "Measurement Summary"
- "Roof Summary", "Roof Measurements", "Area Summary"
- cualquier tabla o lista de áreas y perímetros (sqft / squares / LF)

Encabezado: extrae dirección y código postal ZIP (5 dígitos, p. ej. 30228), aunque el rótulo sea Zip, ZIP Code, Postal Code o vaya pegado a la ciudad/estado.

Métricas: identifica por significado, no por texto exacto. Acepta mayúsculas, plurales y sinónimos:
- Total Roof Area / Total area / Roof area / Measurements total
- Pitched Area / Pitched roof / Steep slope / Shingle area
- Flat Area / Flat roof / Low slope
- Eaves / Eave
- Valleys / Valley
- Ridges / Ridge
- Hips / Hip
- Rakes / Rake
- Flashings / Wall flashing / Step flashing / Counter flashing
- Transitions (si existe)
- Drip edge / Gutter apron
- Ventilación (ridge vents, box vents, soffit vents) si aparece

NO extraigas precios, mínimos, diagramas, disclaimers ni notas legales.
Áreas: si el número aparece en sqft (ej. 4705), déjalo TAL CUAL; Python lo convertirá a squares (÷100).
Si la tabla de Waste/Squares ya trae escuadras (ej. 56.46), ponlas en *_sq_waste.
Lineales: pies lineales en decimal. Si ves "166 ft 3 in", convierte a 166.25. Si no hay dato, usa 0.

WASTE / SQUARES: busca Suggested waste / Squares (prioridad 20% o 22%, o el % marcado).

Responde SOLO JSON con estas claves (números sin comas). Incluye SIEMPRE las llaves cortas y las *_lf:
{
  "zip_code": "30228",
  "address": "571 Togwatee Pass",
  "waste_percent": 20,
  "total_roof_area_sqft": 4705,
  "pitched_area_sqft": 4548,
  "flat_area_sqft": 158,
  "squares": 56.46,
  "eaves": 166.25,
  "valleys": 213.08,
  "hips": 93.33,
  "ridges": 148.25,
  "rakes": 199.75,
  "total_roof_area_sq": 56.46,
  "total_pitched_area_sq": 54.6,
  "total_flat_area_sq": 1.9,
  "total_roof_area_sq_waste": 56.46,
  "total_pitched_area_sq_waste": 54.6,
  "total_flat_area_sq_waste": 1.9,
  "eaves_lf": 166.25,
  "valleys_lf": 213.08,
  "ridges_lf": 148.25,
  "hips_lf": 93.33,
  "rakes_lf": 199.75,
  "drip_edge_lf": 0,
  "gutter_apron_lf": 0,
  "ventilation_ea": 0,
  "wall_flashing_lf": 0,
  "step_flashing_lf": 0,
  "flashing_lf": 0,
  "transitions_lf": 0
}
"""


def _num(texto, default=0.0):
    if texto is None or texto == "":
        return default
    if isinstance(texto, (int, float)):
        return float(texto)
    limpio = str(texto).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", limpio)
    return float(match.group(0)) if match else default


def _clave_norm(texto):
    t = str(texto or "").strip().replace("'", "")
    t = t.replace("sq. ft", "sqft").replace("sq ft", "sqft").replace("sqft.", "sqft")
    t = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", t)
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "_", t)
    return t.strip("_")


def _lf_desde_cualquier(valor):
    """Acepta 166.25, '166 ft 3 in', {length: 166, inches: 3}, etc."""
    if valor is None or valor == "":
        return 0.0
    if isinstance(valor, bool):
        return 0.0
    if isinstance(valor, (int, float)):
        return round(float(valor), 2)
    if isinstance(valor, (list, tuple)):
        if not valor:
            return 0.0
        if len(valor) >= 2 and all(isinstance(x, (int, float, str)) for x in valor[:2]):
            return round(_num(valor[0]) + _num(valor[1]) / 12.0, 2)
        return _lf_desde_cualquier(valor[0])
    if isinstance(valor, dict):
        for clave in ("lf", "length_ft", "length", "value", "qty", "quantity", "amount", "ft", "feet", "total"):
            if valor.get(clave) not in (None, ""):
                pulg = valor.get("in") or valor.get("inch") or valor.get("inches") or 0
                base = _lf_desde_cualquier(valor.get(clave))
                if pulg and clave in ("ft", "feet", "length", "length_ft"):
                    return round(base + _num(pulg) / 12.0, 2)
                if base:
                    return base
        ft = valor.get("ft") or valor.get("feet")
        inch = valor.get("in") or valor.get("inch") or valor.get("inches")
        if ft not in (None, "") or inch not in (None, ""):
            return round(_num(ft) + _num(inch) / 12.0, 2)
        return 0.0
    texto = str(valor).replace(",", "")
    match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*(?:ft|feet|')\s*[-+]?\s*(\d+(?:\.\d+)?)\s*(?:in|inch|inches|\")?",
        texto,
        re.I,
    )
    if match:
        return round(float(match.group(1)) + float(match.group(2)) / 12.0, 2)
    return round(_num(texto), 2)


def _pares_json_claude(obj, prefix=""):
    """Aplana dicts/listas de Claude a pares (clave_normalizada, valor)."""
    pares = []
    if isinstance(obj, dict):
        nombre = obj.get("name") or obj.get("label") or obj.get("type") or obj.get("metric") or obj.get("key") or obj.get("campo")
        valor = None
        for campo in ("value", "qty", "quantity", "length", "amount", "sqft", "squares", "lf", "ft", "total"):
            if obj.get(campo) not in (None, ""):
                valor = obj.get(campo)
                break
        if nombre and valor is not None and not isinstance(valor, (dict, list)):
            clave = _clave_norm(f"{prefix}_{nombre}" if prefix else nombre)
            pares.append((clave, valor))
        for k, v in obj.items():
            nxt = _clave_norm(f"{prefix}_{k}" if prefix else k)
            pares.extend(_pares_json_claude(v, nxt))
    elif isinstance(obj, list):
        for item in obj:
            pares.extend(_pares_json_claude(item, prefix))
    elif prefix:
        pares.append((_clave_norm(prefix), obj))
    return pares


_ALIAS_MEDIDAS = {
    "zip_code": ("zip_code", "zip", "postal_code", "zipcode"),
    "address": ("address", "property_address", "site_address", "street_address", "location"),
    "waste_percent": ("waste_percent", "waste", "suggested_waste", "waste_factor", "recommended_waste"),
    "total_roof_area_sqft": (
        "total_roof_area_sqft", "total_sqft", "total_roof_area", "roof_area_sqft", "total_area",
        "measurements_total_roof_area", "total_roof_area_sf", "roof_area",
    ),
    "pitched_area_sqft": (
        "pitched_area_sqft", "pitched_sqft", "pitched_roof_area_sqft", "pitched_area",
        "steep_area_sqft", "pitched_roof_area_sf", "shingle_area_sqft", "total_pitched_area",
    ),
    "flat_area_sqft": (
        "flat_area_sqft", "flat_sqft", "flat_roof_area_sqft", "flat_area",
        "low_slope_sqft", "flat_roof_area_sf", "total_flat_area",
    ),
    "total_roof_area_sq": ("total_roof_area_sq", "total_squares", "total_sq", "squares"),
    "total_pitched_area_sq": ("total_pitched_area_sq", "pitched_squares", "pitched_sq"),
    "total_flat_area_sq": ("total_flat_area_sq", "flat_squares", "flat_sq"),
    "total_roof_area_sq_waste": (
        "total_roof_area_sq_waste", "total_squares_waste", "squares_with_waste", "total_sq_waste",
    ),
    "total_pitched_area_sq_waste": (
        "total_pitched_area_sq_waste", "pitched_squares_waste", "pitched_sq_waste",
    ),
    "total_flat_area_sq_waste": ("total_flat_area_sq_waste", "flat_squares_waste", "flat_sq_waste"),
    "total_roof_facets": ("total_roof_facets", "facets", "facet_count", "number_of_facets"),
    "eaves_lf": ("eaves_lf", "eave_lf", "eaves", "eave", "length_of_eaves", "eaves_length", "eave_length"),
    "valleys_lf": ("valleys_lf", "valley_lf", "valleys", "valley", "length_of_valleys", "valleys_length"),
    "hips_lf": ("hips_lf", "hip_lf", "hips", "hip", "length_of_hips", "hips_length"),
    "ridges_lf": ("ridges_lf", "ridge_lf", "ridges", "ridge", "length_of_ridges", "ridges_length", "ridge_cap"),
    "rakes_lf": ("rakes_lf", "rake_lf", "rakes", "rake", "length_of_rakes", "rakes_length"),
    "wall_flashing_lf": ("wall_flashing_lf", "wall_flashing", "headwall_flashing", "wall_flash"),
    "step_flashing_lf": ("step_flashing_lf", "step_flashing", "step_flash"),
    "flashing_lf": ("flashing_lf", "flashings_lf", "flashing", "flashings", "length_of_flashings"),
    "transitions_lf": ("transitions_lf", "transition_lf", "transitions", "transition"),
    "drip_edge_lf": ("drip_edge_lf", "drip_edge", "dripedge", "gutter_apron_lf", "gutter_apron", "drip_eave"),
    "ventilation_ea": ("ventilation_ea", "vents_ea", "ridge_vents", "box_vents", "ventilation", "vents"),
}


def _valor_por_alias(plano, destino):
    aliases = _ALIAS_MEDIDAS.get(destino) or (destino,)
    for alias in aliases:
        if alias in plano and plano[alias] not in (None, ""):
            return plano[alias]
    for alias in aliases:
        for clave, valor in plano.items():
            if valor in (None, ""):
                continue
            if clave == alias:
                return valor
            if len(alias) >= 8 and (clave.endswith("_" + alias) or clave.startswith(alias + "_")):
                return valor
    # coincidencia por token (eaves dentro de length_of_eaves_lf)
    token = destino.replace("_lf", "").replace("_sqft", "").replace("_sq", "").replace("_percent", "")
    if token in ("eaves", "valleys", "hips", "ridges", "rakes", "transitions"):
        singular = token[:-1] if token.endswith("s") else token
        buscados = {token, singular}
        for clave, valor in plano.items():
            if valor in (None, ""):
                continue
            partes = set(clave.split("_"))
            if partes & buscados:
                if "area" in partes or "sqft" in partes or "square" in partes or "squares" in partes:
                    continue
                return valor
    return None


def medidas_desde_json_claude(parsed):
    """Traduce el JSON de Claude (plano, anidado o lista) a las claves de partidas."""
    if not isinstance(parsed, dict):
        return {}
    plano = {}
    for clave, valor in _pares_json_claude(parsed):
        if clave and valor not in (None, ""):
            plano[clave] = valor

    def _tomar(*destinos):
        for destino in destinos:
            hallado = _valor_por_alias(plano, destino)
            if hallado not in (None, ""):
                return hallado
        return None

    def _a_sq(valor):
        n = _lf_desde_cualquier(valor)
        if n >= 80:
            return round(n / 100.0, 2)
        return round(n, 2)

    zip_code = re.sub(r"\D", "", str(_tomar("zip_code") or ""))[:5] or None
    waste_percent = _lf_desde_cualquier(_tomar("waste_percent"))
    if waste_percent <= 0:
        waste_percent = 20.0
    if 0 < waste_percent <= 1.5:
        waste_percent = round(waste_percent * 100.0, 2)
    waste_factor = 1.0 + (waste_percent / 100.0)

    total_sf = _lf_desde_cualquier(_tomar("total_roof_area_sqft"))
    pitched_sf = _lf_desde_cualquier(_tomar("pitched_area_sqft"))
    flat_sf = _lf_desde_cualquier(_tomar("flat_area_sqft"))
    total_base = _a_sq(_tomar("total_roof_area_sq")) or _sqft_a_sq(total_sf)
    pitched_base = _a_sq(_tomar("total_pitched_area_sq")) or _sqft_a_sq(pitched_sf)
    flat_base = _a_sq(_tomar("total_flat_area_sq")) or _sqft_a_sq(flat_sf)
    if total_base <= 0 and total_sf > 0:
        total_base = _sqft_a_sq(total_sf)
    if pitched_base <= 0 and pitched_sf > 0:
        pitched_base = _sqft_a_sq(pitched_sf)
    if flat_base <= 0 and flat_sf > 0:
        flat_base = _sqft_a_sq(flat_sf)

    total_waste = _a_sq(_tomar("total_roof_area_sq_waste"))
    pitched_waste = _a_sq(_tomar("total_pitched_area_sq_waste"))
    flat_waste = _a_sq(_tomar("total_flat_area_sq_waste"))
    if total_waste <= 0 and total_base > 0:
        total_waste = round(total_base * waste_factor, 2)
    if pitched_waste <= 0 and pitched_base > 0:
        pitched_waste = round(pitched_base * waste_factor, 2)
    if flat_waste <= 0 and flat_base > 0:
        flat_waste = round(flat_base * waste_factor, 2)

    wall = _lf_desde_cualquier(_tomar("wall_flashing_lf"))
    step = _lf_desde_cualquier(_tomar("step_flashing_lf"))
    flashing = wall + step if (wall or step) else _lf_desde_cualquier(_tomar("flashing_lf"))
    eaves = _lf_desde_cualquier(_tomar("eaves_lf"))
    valleys = _lf_desde_cualquier(_tomar("valleys_lf"))
    hips = _lf_desde_cualquier(_tomar("hips_lf"))
    ridges = _lf_desde_cualquier(_tomar("ridges_lf"))
    rakes = _lf_desde_cualquier(_tomar("rakes_lf"))
    transitions = _lf_desde_cualquier(_tomar("transitions_lf"))

    return {
        "zip_code": zip_code,
        "address": str(_tomar("address") or "").strip() or None,
        "waste_percent": round(waste_percent, 2),
        "total_roof_area_sf": round(total_sf or (total_base * 100), 2),
        "pitched_roof_area_sf": round(pitched_sf or (pitched_base * 100), 2),
        "flat_roof_area_sf": round(flat_sf or (flat_base * 100), 2),
        "total_sq": total_waste or total_base,
        "pitched_sq": pitched_base,
        "flat_sq": flat_base,
        "pitched_sq_waste": pitched_waste or round(pitched_base * WASTE_TEJA, 2) if pitched_base else 0.0,
        "total_roof_area_sq": total_waste or total_base,
        "total_pitched_area_sq": pitched_waste or pitched_base,
        "total_flat_area_sq": flat_waste or flat_base,
        "total_roof_area_sq_base": total_base,
        "total_pitched_area_sq_base": pitched_base,
        "total_flat_area_sq_base": flat_base,
        "total_roof_facets": _lf_desde_cualquier(_tomar("total_roof_facets")),
        "eaves_lf": eaves,
        "valleys_lf": valleys,
        "hips_lf": hips,
        "ridges_lf": ridges,
        "rakes_lf": rakes,
        "wall_flashing_lf": wall,
        "step_flashing_lf": step,
        "flashing_lf": flashing,
        "transitions_lf": transitions,
        "drip_edge_lf": _lf_desde_cualquier(_tomar("drip_edge_lf")),
        "ventilation_ea": _lf_desde_cualquier(_tomar("ventilation_ea")),
        "source": "roofr-claude",
    }


def _pies_a_lf(entero, pulgadas=0):
    return round(_num(entero) + _num(pulgadas) / 12.0, 2)


def _sqft_a_sq(sqft):
    """Paso 2: el número original del reporte (sqft) se divide SIEMPRE entre 100 → SQ."""
    valor = max(_num(sqft), 0.0)
    if valor <= 0:
        return 0.0
    return round(valor / 100.0, 2)


def _aplicar_conversion_areas_sq(medidas):
    """Total / pitched / flat: original÷100 y unidad SQ. Perímetros no se tocan (LF)."""
    if not isinstance(medidas, dict):
        return medidas

    def _como_sq(sf_key, sq_key):
        actual = _num(medidas.get(sq_key))
        if 0 < actual < 80:
            return round(actual, 2)
        original = _num(medidas.get(sf_key))
        if original <= 0:
            original = actual
        if original >= 80:
            return _sqft_a_sq(original)
        return round(original, 2) if original else 0.0

    medidas["total_sq"] = _como_sq("total_roof_area_sf", "total_sq")
    medidas["pitched_sq"] = _como_sq("pitched_roof_area_sf", "pitched_sq")
    medidas["flat_sq"] = _como_sq("flat_roof_area_sf", "flat_sq")
    if _num(medidas.get("total_roof_area_sq")) <= 0:
        medidas["total_roof_area_sq"] = medidas["total_sq"]
    if _num(medidas.get("total_pitched_area_sq")) <= 0:
        medidas["total_pitched_area_sq"] = _num(medidas.get("pitched_sq_waste")) or medidas["pitched_sq"]
    if _num(medidas.get("total_flat_area_sq")) <= 0:
        medidas["total_flat_area_sq"] = medidas["flat_sq"]
    if _num(medidas.get("pitched_sq_waste")) <= 0:
        pitched = _num(medidas.get("pitched_sq"))
        medidas["pitched_sq_waste"] = round(pitched * WASTE_TEJA, 2) if pitched else 0.0
    medidas["unidad_area"] = "SQ"
    return medidas


def _normalizar_codigo(texto):
    """Quita espacios laterales y colapsa espacios internos del código de catálogo."""
    return re.sub(r"\s+", " ", str(texto or "").strip().upper())


def _codigo_clave(texto):
    """Código comparable: sin espacios, guiones ni puntuación (RFG SHN → RFGSHN)."""
    return re.sub(r"[^A-Z0-9]", "", _normalizar_codigo(texto))


def _codigos_coinciden(a, b):
    if not a or not b:
        return False
    na, nb = _normalizar_codigo(a), _normalizar_codigo(b)
    if na == nb:
        return True
    ca, cb = _codigo_clave(a), _codigo_clave(b)
    return bool(ca and cb and ca == cb)


def _fila_codigo(row):
    if not isinstance(row, dict):
        return ""
    return _normalizar_codigo(row.get("codigo") or row.get("code") or row.get("sku") or "")


_RE_METAL_ESPECIALIZADO = re.compile(
    r"standing\s*seam|metal\s*roof|cubierta de metal|hojalat|sheet\s*metal|"
    r"calibre\s*2[0-6]|copper\s*roof|techo de (cobre|acero|zinc)|"
    r"EXT-?ROF-?MET|\bMET-?(STN|SS|COP|ZNC|PNL)\b",
    re.I,
)
_RE_RIDGE_ASFALTICO = re.compile(
    r"ridge\s*cap|shingle\s*ridge|hip\s*(and|&|/)\s*ridge|cumbrera|"
    r"continuous\s*ridge|ridge\s*vent|teja.{0,24}cumbrera|cumbrera.{0,24}(teja|asf)",
    re.I,
)
_PRECIO_MAX_LF = {
    "starter": 8.0,
    "valley": 28.0,
    "ridges": 22.0,
    "hips": 22.0,
    "rakes": 8.0,
    "drip_edge": 12.0,
}


def _categoria_techo(row):
    return str((row or {}).get("categoria") or (row or {}).get("familia") or (row or {}).get("category") or "").upper()


def _blob_catalogo_techo(row):
    codigo = _fila_codigo(row)
    desc = str((row or {}).get("descripcion") or (row or {}).get("description") or "")
    return f"{codigo} {desc} {_categoria_techo(row)}"


def _es_categoria_techo(row):
    cat = _categoria_techo(row)
    if not cat:
        return True
    return bool(re.search(r"TECH|ROOF|RFG|TECHO", cat))


def _es_metal_especializado(row):
    blob = _blob_catalogo_techo(row)
    if not _RE_METAL_ESPECIALIZADO.search(blob):
        return False
    # Cumbrera asfáltica nunca viene como standing seam / MET-STN.
    if _RE_RIDGE_ASFALTICO.search(blob) and re.search(r"teja|shingle|asfalt|asfált", blob, re.I):
        return False
    return True


def _precio_lf_fuera_de_rango(clave, price):
    tope = _PRECIO_MAX_LF.get(clave)
    if tope is None:
        return False
    return price > tope


def _coincide_plantilla(row, clave, patron, codigo_pdf=""):
    if not _es_categoria_techo(row) or _es_metal_especializado(row):
        return False, 0
    codigo = _fila_codigo(row)
    desc = str(row.get("descripcion") or row.get("description") or "")
    categoria = str(row.get("categoria") or "")
    blob = f"{codigo} {desc} {categoria}".lower()
    clave_cod = _codigo_clave(codigo)
    if clave == "ridges" and clave_cod not in (CODIGOS_PLANTILLA.get("ridges") or set()) and not _RE_RIDGE_ASFALTICO.search(blob):
        return False, 0
    if codigo_pdf and _codigos_coinciden(codigo, codigo_pdf):
        return True, 40
    aliases = CODIGOS_PLANTILLA.get(clave) or set()
    if clave_cod and clave_cod in aliases:
        return True, 35
    extra = {
        "tear_off": r"remover|remove|tear|demo|escuadra|squares|shingle",
        "arch_shingle": r"instalar|install|teja|shingle|arquitect",
        "flat_sbs": r"plano|flat|sbs|modif",
        "starter": r"eaves|eave|starter|alero|inicio",
        "valley": r"valley|limahoya",
        "ridges": r"ridge\s*cap|shingle\s*ridge|continuous\s*ridge|cumbrera",
        "hips": r"\bhips?\b|lima[\s\-]?tesa",
        "rakes": r"\brakes?\b|faldon",
        "drip_edge": r"drip|apron|delantal",
        "ventilation": r"ventila|vent",
        "flashing": r"flashing|tapajuntas",
        "transitions": r"transition",
    }.get(clave, "")
    match_codigo = bool(codigo and re.search(patron, codigo, re.I))
    match_texto = bool(re.search(patron, blob, re.I))
    match_extra = bool(extra and re.search(extra, blob, re.I))
    if match_codigo:
        return True, 20
    if match_texto:
        return True, 12
    if match_extra:
        return True, 7
    return False, 0


def _unidad_catalogo(row):
    texto = str(row.get("unidad") or row.get("unit") or row.get("unidad_medida") or "").upper()
    texto = texto.replace(".", "").replace(" ", "")
    if texto in ("SQ", "SQUARES", "SQUARE", "ESCUADRA", "ESCUADRAS"):
        return "SQ"
    if texto in ("SF", "SQFT", "PIESCUADRADOS"):
        return "SF"
    if texto in ("LF", "LINFT", "LINEAR", "LIN"):
        return "LF"
    if texto in ("EA", "EACH", "UNIDAD"):
        return "EA"
    if texto in ("PC", "PCS", "PIEZA", "PIEZAS"):
        return "PC"
    return texto


def _precio_escuadra(row):
    """Unitario de catálogo expresado por SQ. Si el ítem está en SF, se escala ×100."""
    price = _precio_fila(row)
    if price <= 0:
        return 0.0
    unidad = _unidad_catalogo(row)
    if unidad in ("LF", "PC"):
        return 0.0
    if unidad == "SF" and price < 40:
        return round(price * 100.0, 2)
    return round(price, 2)


def extraer_texto_pdf(pdf_bytes):
    if not pdf_bytes:
        return ""
    lectores = (
        _texto_pypdf,
        _texto_pypdf2,
        _texto_pymupdf,
        _texto_pdfminer,
        _texto_binario,
    )
    for lector in lectores:
        try:
            texto = lector(pdf_bytes)
        except Exception:
            texto = ""
        if texto and len(texto.strip()) > 40:
            return texto
    return _texto_binario(pdf_bytes)


def _texto_pypdf(pdf_bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _texto_pypdf2(pdf_bytes):
    from PyPDF2 import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _texto_pymupdf(pdf_bytes):
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(page.get_text() or "" for page in doc)
    finally:
        doc.close()


def _texto_pdfminer(pdf_bytes):
    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(pdf_bytes)) or ""


def _texto_binario(pdf_bytes):
    bruto = pdf_bytes.decode("latin-1", errors="ignore")
    partes = re.findall(r"\((?:\\.|[^\\)]){3,}\)", bruto)
    if partes:
        texto = " ".join(p[1:-1] for p in partes)
        texto = texto.replace("\\n", "\n").replace("\\t", " ").replace("\\(", "(").replace("\\)", ")")
        return re.sub(r"\s+", " ", texto)
    return re.sub(r"[^\x20-\x7e\n]+", " ", bruto)


def es_reporte_roofr(pdf_bytes):
    muestra = (pdf_bytes or b"")[:900000].decode("latin-1", errors="ignore")
    if re.search(r"roofr|roof\s*report", muestra, re.I):
        return True
    texto = extraer_texto_pdf(pdf_bytes)
    return bool(re.search(r"\broofr\b|roof\s*report", texto, re.I))


def _buscar(patron, texto, flags=re.I):
    return re.search(patron, texto, flags)


def _capturar_sqft(texto, etiquetas):
    union = "|".join(etiquetas)
    patron = rf"(?:{union})\s*[:\-]?\s*(\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sf)\b"
    match = _buscar(patron, texto)
    if match:
        return _num(match.group(1))
    return 0.0


def _capturar_lf(texto, etiquetas):
    union = "|".join(etiquetas)
    patron = (
        rf"(?:{union})\s*[:\-]?\s*(\d{{1,5}}(?:\.\d+)?)\s*(?:ft|feet|')\s*"
        rf"(?:(\d{{1,2}}(?:\.\d+)?)\s*(?:in|\"))?"
    )
    match = _buscar(patron, texto)
    if match:
        return _pies_a_lf(match.group(1), match.group(2) or 0)
    patron_simple = rf"(?:{union})\s*[:\-]?\s*(\d{{1,5}}(?:\.\d+)?)\s*(?:lf|lin(?:ear)?\s*ft)\b"
    match = _buscar(patron_simple, texto)
    if match:
        return round(_num(match.group(1)), 2)
    return 0.0


def _extraer_json_local(texto):
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


MODELO_CLAUDE = "claude-sonnet-4-6"
MODELOS_CLAUDE = (
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-5",
)


def extraer_medidas_claude(pdf_bytes, parsed=None):
    """Claude lee encabezado + Report summary. Solo se usa en este módulo de Roof Report."""
    if parsed is None and not pdf_bytes:
        return None
    try:
        if not isinstance(parsed, dict):
            from claude_pdf import analizar_pdf_json

            parsed = analizar_pdf_json(
                pdf_bytes,
                PROMPT_REPORT_SUMMARY,
                modelo=MODELO_CLAUDE,
                modelos=MODELOS_CLAUDE,
            )
        if not isinstance(parsed, dict):
            return None
        medidas = medidas_desde_json_claude(parsed)
        print(
            "--> roof_parser Claude ZIP="
            f"{medidas.get('zip_code')} total_sq={medidas.get('total_sq')} "
            f"eaves={medidas.get('eaves_lf')} valleys={medidas.get('valleys_lf')} "
            f"hips={medidas.get('hips_lf')} ridges={medidas.get('ridges_lf')} rakes={medidas.get('rakes_lf')}"
        )
        return medidas
    except Exception as err:
        print(f"roof_parser Claude: {err}")
        return None


extraer_medidas_gemini = extraer_medidas_claude


def extraer_medidas_roofr(pdf_bytes, unidad_area="SQ", json_claude=None):
    """Report summary vía Claude (preferente) con respaldo regex. Áreas siempre SQ = sqft/100."""
    texto = extraer_texto_pdf(pdf_bytes)
    plano = re.sub(r"[ \t]+", " ", texto or "")
    unidad_area = "SQ"

    total_sf = _capturar_sqft(plano, (r"total\s+roof\s+area", r"measurements?\s+total\s+roof\s+area"))
    pitched_sf = _capturar_sqft(plano, (r"pitched\s+roof\s+area", r"total\s+pitched\s+area", r"pitched\s+area"))
    flat_sf = _capturar_sqft(plano, (r"flat\s+roof\s+area", r"total\s+flat\s+area", r"flat\s+area"))
    eaves_lf = _capturar_lf(plano, (r"\beaves\b",))
    valleys_lf = _capturar_lf(plano, (r"\bvalleys?\b",))
    ridges_lf = _capturar_lf(plano, (r"\bridges?\b",))
    hips_lf = _capturar_lf(plano, (r"\bhips?\b",))
    rakes_lf = _capturar_lf(plano, (r"\brakes?\b",))
    flashing_lf = _capturar_lf(plano, (r"wall\s*/?\s*step\s*flashing", r"step\s*flashing", r"wall\s*flashing"))
    transitions_lf = _capturar_lf(plano, (r"\btransitions?\b",))

    zip_match = _buscar(r"\b(\d{5})(?:-\d{4})?\b", plano)
    zip_code = zip_match.group(1) if zip_match else None
    addr_match = _buscar(
        r"(\d{1,6}\s+[A-Za-z0-9 .'-]+(?:pass|dr|drive|st|street|rd|road|ln|lane|ct|court|ave|way|blvd)[^\n,]{0,40})",
        plano,
    )
    address = addr_match.group(1).strip() if addr_match else None

    total_sq = _sqft_a_sq(total_sf)
    pitched_sq = _sqft_a_sq(pitched_sf)
    flat_sq = _sqft_a_sq(flat_sf)
    medidas = {
        "document_type": "roof_report",
        "source": "roofr",
        "zip_code": zip_code,
        "address": address,
        "unidad_area": unidad_area,
        "total_roof_area_sf": round(total_sf, 2),
        "pitched_roof_area_sf": round(pitched_sf, 2),
        "flat_roof_area_sf": round(flat_sf, 2),
        "total_sq": total_sq,
        "pitched_sq": pitched_sq,
        "flat_sq": flat_sq,
        "pitched_sq_waste": round(pitched_sq * WASTE_TEJA, 2) if pitched_sq else 0.0,
        "eaves_lf": eaves_lf,
        "valleys_lf": valleys_lf,
        "ridges_lf": ridges_lf,
        "hips_lf": hips_lf,
        "rakes_lf": rakes_lf,
        "flashing_lf": flashing_lf,
        "transitions_lf": transitions_lf,
        "units": {
            "total_roof_area": "SQ",
            "pitched_roof_area": "SQ",
            "flat_roof_area": "SQ",
            "eaves": "LF",
            "valleys": "LF",
            "ridges": "LF",
            "hips": "LF",
            "rakes": "LF",
            "flashing": "LF",
            "transitions": "LF",
        },
        "texto": plano,
    }
    claude = extraer_medidas_claude(pdf_bytes, parsed=json_claude)
    if isinstance(claude, dict):
        for clave, valor in claude.items():
            if valor in (None, "", 0, "0", 0.0):
                continue
            medidas[clave] = valor
        medidas["source"] = "roofr-claude"
        if _num(medidas.get("total_roof_area_sq")) > 0:
            medidas["total_sq"] = _num(medidas.get("total_roof_area_sq"))
        elif _num(medidas.get("total_roof_area_sf")) > 0:
            medidas["total_sq"] = _sqft_a_sq(medidas.get("total_roof_area_sf"))
        if _num(medidas.get("total_pitched_area_sq")) > 0:
            medidas["pitched_sq_waste"] = _num(medidas.get("total_pitched_area_sq"))
            if _num(medidas.get("pitched_sq")) <= 0:
                medidas["pitched_sq"] = _sqft_a_sq(medidas.get("pitched_roof_area_sf"))
        else:
            medidas["pitched_sq"] = _sqft_a_sq(medidas.get("pitched_roof_area_sf"))
            medidas["pitched_sq_waste"] = round(medidas["pitched_sq"] * WASTE_TEJA, 2) if medidas["pitched_sq"] else 0.0
        if _num(medidas.get("total_flat_area_sq")) > 0:
            medidas["flat_sq"] = _num(medidas.get("total_flat_area_sq"))
        elif _num(medidas.get("flat_roof_area_sf")) > 0:
            medidas["flat_sq"] = _sqft_a_sq(medidas.get("flat_roof_area_sf"))
        print(f"--> roof_parser Claude ZIP={medidas.get('zip_code')} total_sq={medidas.get('total_sq')} pitched_sq={medidas.get('pitched_sq')}")
    return _aplicar_conversion_areas_sq(medidas)


def _creds_supabase():
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or ""
    )
    return url, key


def _consultar_catalogo_supabase(termino="", limite=1000, offset=0):
    url, key = _creds_supabase()
    if not url or not key:
        return []
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    params = {"select": "*", "limit": str(int(limite) or 1000), "offset": str(int(offset) or 0)}
    q = _normalizar_codigo(termino)
    if q:
        like = f"*{q[:60]}*"
        compact = _codigo_clave(q)
        partes = [
            f"descripcion.ilike.{like}",
            f"codigo.ilike.{like}",
            f"categoria.ilike.{like}",
        ]
        if compact and compact not in (q, like):
            partes.append(f"codigo.ilike.*{compact[:60]}*")
        params["or"] = "(" + ",".join(partes) + ")"
    query = urllib.parse.urlencode(params, safe="(),.*")
    req = urllib.request.Request(f"{url}/rest/v1/{TABLA_CATALOGO}?{query}", headers=headers)
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
            return data if isinstance(data, list) else []
    except Exception as err:
        print(f"roof_parser catalogo: {err}")
        return []


def _filas_catalogo_techo():
    filas = []
    vistos = set()

    def _agregar(lote):
        for row in lote or []:
            if not isinstance(row, dict):
                continue
            codigo = _fila_codigo(row)
            marca = (row.get("id"), codigo, row.get("descripcion"))
            if marca in vistos:
                continue
            vistos.add(marca)
            row = dict(row)
            if codigo:
                row["codigo"] = codigo
            filas.append(row)

    offset = 0
    while True:
        lote = _consultar_catalogo_supabase("", limite=1000, offset=offset)
        if not lote:
            break
        _agregar(lote)
        if len(lote) < 1000:
            break
        offset += 1000
    for termino in (
        "RFG", "ROF", "techo", "shingle", "tear", "teja", "starter", "valley",
        "ridge", "rake", "eave", "flash", "transition", "sbs", "limahoya", "cumbrera",
    ):
        _agregar(_consultar_catalogo_supabase(termino, limite=200))
    return filas


def _compatible_unidad(unidad_item, unidad_base):
    if unidad_base == "LF":
        return unidad_item == "LF"
    if not unidad_item:
        return True
    if unidad_base == "SQ":
        return unidad_item == "SQ"
    if unidad_base == "EA":
        return unidad_item in ("EA", "PC", "")
    return unidad_item == unidad_base


def precios_unitarios_por_zip(zip_code):
    """Cruza por código/descripción de catalogo_items. Precio exacto; 0 si no hay match de unidad."""
    filas = _filas_catalogo_techo()
    rates = {clave: 0.0 for clave, _n, _c, _u, _p in PLANTILLA_PARTIDAS}
    mejores = {}
    for row in filas:
        try:
            from oficios import fila_es_oficio
            if not fila_es_oficio(row, "roofing"):
                continue
        except Exception:
            pass
        codigo = _fila_codigo(row)
        desc = str(row.get("descripcion") or row.get("description") or "")
        categoria = str(row.get("categoria") or "")
        blob = f"{codigo} {desc} {categoria}"
        unidad_item = _unidad_catalogo(row)
        for clave, _nombre, _campo, unidad_base, patron in PLANTILLA_PARTIDAS:
            ok, bonus = _coincide_plantilla(row, clave, patron)
            if not ok:
                continue
            if not _compatible_unidad(unidad_item, unidad_base):
                continue
            price = _precio_fila(row)
            if price <= 0:
                continue
            if _precio_lf_fuera_de_rango(clave, price):
                continue
            score = _score_region(row, zip_code) + bonus
            if unidad_item == unidad_base:
                score += 10
            if re.search(r"\btecho\b|\broof\b|rfg", blob, re.I):
                score += 6
            if re.search(r"siding|ventana|window|cerca|fence|drywall|pintura ciel", blob, re.I):
                score -= 12
            if zip_code and zip_code in blob:
                score += 4
            previo = mejores.get(clave)
            if previo is None or score >= previo[1]:
                mejores[clave] = (round(price, 2), score, codigo)
            break
    for clave, (price, _score, _codigo) in mejores.items():
        rates[clave] = price
    print(f"--> roof_parser ZIP {zip_code}: {len(filas)} ítems catálogo, tarifas={rates}")
    return rates


def _precio_fila(row):
    for key in ("precio_base", "precio_unitario", "precio", "price", "rate"):
        if row.get(key) not in (None, ""):
            valor = _num(row.get(key))
            if valor > 0:
                return valor
    return 0.0


def _score_region(row, zip_code):
    zip_digits = re.sub(r"\D", "", str(zip_code or ""))[:5]
    zona = " ".join(str(row.get(k) or "") for k in ("zip", "zip_code", "codigo_postal", "region", "zona", "estado", "state"))
    if zip_digits and zip_digits in re.sub(r"\D", "", zona):
        return 3
    if zip_digits and zip_digits[:3] in zona:
        return 2
    return 0


def _partida_catalogo(clave, unidad_objetivo, filas_cat, zip_code=""):
    plantilla = next((p for p in PLANTILLA_PARTIDAS if p[0] == clave), None)
    desc_fallback = plantilla[1] if plantilla else clave
    patron = plantilla[4] if plantilla else r"."
    action = "demo" if clave == "tear_off" else "install"
    mejor = None
    for row in filas_cat or []:
        try:
            from oficios import fila_es_oficio
            if not fila_es_oficio(row, "roofing"):
                continue
        except Exception:
            pass
        ok, bonus = _coincide_plantilla(row, clave, patron)
        if not ok:
            continue
        unidad_row = _unidad_catalogo(row)
        if unidad_objetivo == "LF":
            if unidad_row != "LF":
                continue
        elif unidad_row and unidad_row != unidad_objetivo:
            continue
        price = _precio_fila(row)
        if price <= 0:
            continue
        if _precio_lf_fuera_de_rango(clave, price):
            continue
        codigo = _fila_codigo(row)
        desc = str(row.get("descripcion") or row.get("description") or "").strip()
        blob = f"{codigo} {desc}"
        score = bonus + _score_region(row, zip_code)
        if unidad_row == unidad_objetivo:
            score += 10
        if clave == "tear_off" and re.search(r"tear.?off|remoci|demo|remove", blob, re.I):
            score += 10
        if clave != "tear_off" and re.search(r"instal|install|coloca", blob, re.I):
            score += 6
        if re.search(r"\btecho\b|\broof\b|rfg", blob, re.I):
            score += 6
        if re.search(r"siding|ventana|window|cerca|fence", blob, re.I):
            score -= 12
        if zip_code and zip_code in blob:
            score += 5
        if mejor is None or score > mejor[0]:
            mejor = (score, round(price, 2), codigo, desc or desc_fallback)
    if not mejor:
        return {
            "codigo": "",
            "descripcion": desc_fallback,
            "precio": round(_num(PRECIOS_RESPALDO.get(clave)), 2),
            "action": action,
        }
    return {
        "codigo": mejor[2] or "",
        "descripcion": mejor[3] or desc_fallback,
        "precio": mejor[1],
        "action": action,
        "categoria": "TECHO",
    }


def _raiz_codigo_techo(code):
    c = _codigo_clave(code)
    return re.sub(
        r"(DEMOLITION|DEMOLICION|TEAROFF|REMOVE|REMOVAL|DEMO|INSTALLATION|INSTALACION|INSTALL|INST|INS)$",
        "",
        c,
    )


def _precio_rr_asfalto(filas_cat, zip_code=""):
    """Suma fila de retiro + fila de instalación de teja asfáltica (mismo material, SQ)."""
    demo = _partida_catalogo("tear_off", "SQ", filas_cat, zip_code)
    inst = _partida_catalogo("arch_shingle", "SQ", filas_cat, zip_code)
    precio_demo = round(_num(demo.get("precio")), 2)
    precio_install = round(_num(inst.get("precio")), 2)
    codigo = inst.get("codigo") or demo.get("codigo") or ""
    codigo_base = _raiz_codigo_techo(codigo) or codigo
    return {
        "codigo": codigo,
        "codigo_base": codigo_base,
        "codigo_demo": demo.get("codigo") or "",
        "codigo_install": inst.get("codigo") or "",
        "descripcion": "Remoción y reemplazo de techo asfáltico",
        "precio": round(precio_demo + precio_install, 2),
        "precio_demo": precio_demo,
        "precio_install": precio_install,
        "action": "replace_material",
        "categoria": "TECHO",
        "nucleo": "techo asfáltico",
    }


def _qty_partida(medidas, campo, unidad):
    alias = {
        "total_sq": ("total_roof_area_sq", "total_sq"),
        "total_roof_area_sq": ("total_roof_area_sq", "total_sq"),
        "pitched_sq_waste": ("total_pitched_area_sq", "pitched_sq_waste", "pitched_sq"),
        "total_pitched_area_sq": ("total_pitched_area_sq", "pitched_sq_waste", "pitched_sq"),
        "flat_sq": ("total_flat_area_sq", "flat_sq"),
        "total_flat_area_sq": ("total_flat_area_sq", "flat_sq"),
        "flashing_lf": ("flashing_lf", "wall_flashing_lf"),
        "wall_flashing_lf": ("wall_flashing_lf", "flashing_lf"),
        "drip_edge_lf": ("drip_edge_lf", "gutter_apron_lf", "drip_edge"),
        "ventilation_ea": ("ventilation_ea", "vents_ea", "ventilation", "vents"),
    }
    for clave in alias.get(campo, (campo,)):
        qty = _num(medidas.get(clave))
        if qty > 0:
            return qty
    return max(_num(medidas.get(campo)), 0.0)


def json_medidas_canonico(medidas, zip_code=None, address=None):
    """JSON estricto de medidas que consume el frontend (Number(measures[clave]))."""
    zip_ok = re.sub(r"\D", "", str(zip_code or (medidas or {}).get("zip_code") or ""))[:5]
    addr = str(address or (medidas or {}).get("address") or "").strip()
    m = medidas if isinstance(medidas, dict) else {}

    def n(*claves):
        for clave in claves:
            valor = _num(m.get(clave))
            if valor:
                return round(valor, 2)
        return 0.0

    total_sq = n("total_roof_area_sq", "total_sq")
    pitched_sq = n("total_pitched_area_sq", "pitched_sq_waste", "pitched_sq")
    flat_sq = n("total_flat_area_sq", "flat_sq")
    return {
        "zip_code": zip_ok or None,
        "address": addr or None,
        "waste_percent": n("waste_percent") or 20.0,
        "total_roof_area_sf": n("total_roof_area_sf"),
        "pitched_roof_area_sf": n("pitched_roof_area_sf"),
        "flat_roof_area_sf": n("flat_roof_area_sf"),
        "total_roof_area_sq": total_sq,
        "total_pitched_area_sq": pitched_sq,
        "total_flat_area_sq": flat_sq,
        "total_sq": total_sq,
        "pitched_sq": n("pitched_sq") or pitched_sq,
        "pitched_sq_waste": n("pitched_sq_waste") or pitched_sq,
        "flat_sq": flat_sq,
        "eaves_lf": n("eaves_lf"),
        "valleys_lf": n("valleys_lf"),
        "hips_lf": n("hips_lf"),
        "ridges_lf": n("ridges_lf"),
        "rakes_lf": n("rakes_lf"),
        "drip_edge_lf": n("drip_edge_lf", "gutter_apron_lf"),
        "ventilation_ea": n("ventilation_ea", "vents_ea"),
        "wall_flashing_lf": n("wall_flashing_lf", "flashing_lf"),
        "step_flashing_lf": n("step_flashing_lf"),
        "flashing_lf": n("flashing_lf", "wall_flashing_lf"),
        "transitions_lf": n("transitions_lf"),
        "total_roof_facets": n("total_roof_facets"),
        "eaves": n("eaves_lf"),
        "valleys": n("valleys_lf"),
        "hips": n("hips_lf"),
        "ridges": n("ridges_lf"),
        "rakes": n("rakes_lf"),
        "squares": total_sq,
    }


def item_para_frontend(row, index=1):
    """Claves que parsearRespuestaPdfApi / insertarRoofReportEnTabla leen en index.html."""
    desc = str(row.get("description") or row.get("descripcion") or row.get("nombre") or "").strip()
    qty = round(_num(row.get("quantity") or row.get("cantidad") or row.get("qty")), 2)
    unit = str(row.get("unit") or row.get("unidad") or "SQ").upper()
    price = round(_num(row.get("unit_price") or row.get("precio_unitario") or row.get("precio") or row.get("price")), 2)
    total = round(_num(row.get("total") or row.get("subtotal")) or (qty * price), 2)
    codigo = str(row.get("codigo") or row.get("code") or "").strip()
    action = str(row.get("action") or ("demo" if re.search(r"tear.?off|remoci", desc, re.I) else "install"))
    return {
        "item": int(row.get("item") or index),
        "measure_key": row.get("measure_key") or "",
        "codigo": codigo,
        "code": codigo,
        "sku": codigo,
        "trade": row.get("trade") or "Roofing",
        "description": desc,
        "descripcion": desc,
        "desc": desc,
        "nombre": desc,
        "quantity": qty,
        "cantidad": qty,
        "qty": qty,
        "unit": unit,
        "unidad": unit,
        "unit_price": price,
        "precio_unitario": price,
        "precio": price,
        "price": price,
        "precio_base": round(_num(row.get("precio_base") or price), 2),
        "subtotal": total,
        "total": total,
        "importe": total,
        "action": action,
        "skip_minimums": bool(row.get("skip_minimums")),
        "aplicar_minimos": bool(row.get("aplicar_minimos")),
        "minimum_applied": False,
        "cargo_minimo": 0 if row.get("skip_minimums") else row.get("cargo_minimo") or 0,
        "categoria": str(row.get("categoria") or "TECHO"),
        "codigo_base": str(row.get("codigo_base") or codigo),
        "nucleo": str(row.get("nucleo") or desc),
        "precio_demo": round(_num(row.get("precio_demo")), 2),
        "precio_install": round(_num(row.get("precio_install")), 2),
    }


def extraer_json_claude_techo(pdf_bytes):
    """Paso 1–2: Claude lee el PDF del roof report y devuelve JSON estructurado."""
    from claude_pdf import analizar_pdf_json

    parsed = analizar_pdf_json(
        pdf_bytes,
        PROMPT_REPORT_SUMMARY,
        modelo=MODELO_CLAUDE,
        modelos=MODELOS_CLAUDE,
    )
    if not isinstance(parsed, dict):
        print("--> Claude no devolvió un objeto JSON; se usa {}")
        return {}
    return parsed


def armar_partidas(medidas, zip_code, unidad_area="SQ", factor_zona=1.0):
    """Paso 3–4: cruza JSON con catalogo_items (Supabase) y factor ZIP; arma renglones."""
    medidas = _aplicar_conversion_areas_sq(medidas)
    filas_cat = _filas_catalogo_techo()
    rates = precios_unitarios_por_zip(zip_code)
    factor = _num(factor_zona) or 1.0
    if factor <= 0:
        factor = 1.0
    waste_percent = _num(medidas.get("waste_percent")) or 20.0
    waste_lbl = f"{int(waste_percent) if waste_percent == int(waste_percent) else waste_percent}% waste"
    items = []
    n = 0
    for tarifa, campo, unidad, desc_fallback in FILAS_COTIZADOR:
        qty = round(_qty_partida(medidas, campo, unidad), 2)
        if unidad == "SQ" and qty >= 80:
            qty = round(qty / 100.0, 2)
        if qty <= 0:
            continue
        if tarifa == "rr_asphalt":
            cat = _precio_rr_asfalto(filas_cat, zip_code)
            base = _num(cat.get("precio")) or (_num(rates.get("tear_off")) + _num(rates.get("arch_shingle")))
        else:
            cat = _partida_catalogo(tarifa, unidad, filas_cat, zip_code)
            base = _num(cat.get("precio")) or _num(rates.get(tarifa)) or _num(PRECIOS_RESPALDO.get(tarifa))
        unit_price = round(base * factor, 2) if base > 0 else 0.0
        subtotal = round(qty * unit_price, 2)
        n += 1
        desc = desc_fallback if tarifa == "rr_asphalt" else (cat.get("descripcion") or desc_fallback)
        if tarifa != "rr_asphalt" and unidad == "SQ" and "waste" not in desc.lower() and "desperdicio" not in desc.lower():
            desc = f"{desc} ({waste_lbl})"
        demo_p = round(_num(cat.get("precio_demo")) * factor, 2) if tarifa == "rr_asphalt" else 0.0
        inst_p = round(_num(cat.get("precio_install")) * factor, 2) if tarifa == "rr_asphalt" else unit_price
        items.append(item_para_frontend({
            "item": n,
            "measure_key": campo,
            "codigo": cat.get("codigo") or "",
            "codigo_base": cat.get("codigo_base") or cat.get("codigo") or "",
            "code": cat.get("codigo") or "",
            "trade": "Roofing",
            "categoria": "TECHO",
            "description": desc,
            "descripcion": desc,
            "nucleo": cat.get("nucleo") or desc,
            "quantity": qty,
            "cantidad": qty,
            "unit": unidad,
            "unidad": unidad,
            "unit_price": unit_price,
            "precio_unitario": unit_price,
            "precio_base": round(base, 2),
            "precio_demo": demo_p,
            "precio_install": inst_p,
            "subtotal": subtotal,
            "total": subtotal,
            "action": cat.get("action") or ("replace_material" if tarifa == "rr_asphalt" else "install"),
        }))
        if tarifa == "rr_asphalt":
            rates["rr_asphalt"] = round(base, 2)
        elif _num(rates.get(tarifa)) <= 0 and unit_price > 0:
            rates[tarifa] = round(base, 2)
    gran_total = round(sum(_num(row.get("total")) for row in items), 2)
    skip_minimos = gran_total > UMBRAL_SIN_MINIMOS
    for row in items:
        row["aplicar_minimos"] = not skip_minimos
        row["skip_minimums"] = skip_minimos
        row["minimum_applied"] = False
        if skip_minimos:
            row["cargo_minimo"] = 0
    return items, gran_total, not skip_minimos, rates


def procesar_reporte_roofr(pdf_bytes, zip_activo=None, unidad_area="SQ", factor_zona=None, zona_fn=None):
    """Flujo de importación: Claude → JSON → tarifas Supabase/ZIP → partidas para la UI."""
    # 1) Claude lee el PDF  2) JSON estructurado
    json_claude = extraer_json_claude_techo(pdf_bytes) if pdf_bytes else {}
    print("========== JSON CLAUDE (antes de tarifas / navegador) ==========")
    try:
        print(json.dumps(json_claude, ensure_ascii=False, indent=2)[:8000])
    except TypeError:
        print(repr(json_claude)[:8000])
    print("========== FIN JSON CLAUDE ==========")

    # Normaliza mediciones; el texto del PDF solo rellena ceros
    medidas = extraer_medidas_roofr(pdf_bytes, unidad_area=unidad_area, json_claude=json_claude)
    medidas = _aplicar_conversion_areas_sq(medidas)
    zip_pdf = re.sub(r"\D", "", str(medidas.get("zip_code") or json_claude.get("zip_code") or ""))[:5]
    zip_form = re.sub(r"\D", "", str(zip_activo or ""))[:5]
    zip_code = zip_pdf or zip_form
    address = str(medidas.get("address") or json_claude.get("address") or "").strip() or None
    if zip_code:
        medidas["zip_code"] = zip_code
    if address:
        medidas["address"] = address

    zona = {}
    factor = _num(factor_zona) or 1.0
    if callable(zona_fn) and zip_code:
        try:
            zona = zona_fn(zip_code) or {}
            factor = _num(zona.get("factor")) or factor or 1.0
        except Exception as err:
            print(f"--> factor ZIP: {err}")

    # 3) Supabase catalogo_items + factor de zona  4) partidas listas para la tabla
    items, gran_total, aplicar_minimos, rates = armar_partidas(
        medidas, zip_code, unidad_area=unidad_area, factor_zona=factor
    )
    measures = json_medidas_canonico(medidas, zip_code=zip_code, address=address)
    skip_minimos = gran_total > UMBRAL_SIN_MINIMOS
    print("========== MEDIDAS ESTRUCTURADAS ==========")
    print(json.dumps(measures, ensure_ascii=False, indent=2))
    print("========== PARTIDAS PARA EL FRONTEND ==========")
    print(json.dumps(
        [{"item": r["item"], "description": r["description"], "quantity": r["quantity"], "unit": r["unit"], "unit_price": r["unit_price"], "total": r["total"]} for r in items],
        ensure_ascii=False,
        indent=2,
    )[:8000])
    print(
        f"--> cotización techo ZIP={zip_code} factor={factor} "
        f"partidas={len(items)} total={gran_total}"
    )
    payload = {
        "success": True,
        "document_type": "roof_report",
        "project_type": "roofing",
        "oficio": "roofing",
        "oficio_label": "[RFG] Roofing / Techos",
        "zip_code": zip_code,
        "address": address,
        "waste_percent": measures.get("waste_percent"),
        "factor_zona": factor,
        "ciudad": zona.get("ciudad"),
        "estado": zona.get("estado"),
        "items": items,
        "data": items,
        "partidas": items,
        "measures": measures,
        "area_unit": "SQ",
        "unidad_area": "SQ",
        "aplicar_minimos": aplicar_minimos,
        "skip_minimums": skip_minimos,
        "umbral_minimos": UMBRAL_SIN_MINIMOS,
        "total": gran_total,
        "grand_total": gran_total,
        "roof": {
            "zip_code": zip_code,
            "address": address,
            "total_sqft": measures.get("total_roof_area_sf"),
            "pitched_sqft": measures.get("pitched_roof_area_sf"),
            "flat_sqft": measures.get("flat_roof_area_sf"),
            "total_squares": measures.get("total_roof_area_sq"),
            "pitched_squares": measures.get("total_pitched_area_sq"),
            "flat_squares": measures.get("total_flat_area_sq"),
            "eaves_lf": measures.get("eaves_lf"),
            "valleys_lf": measures.get("valleys_lf"),
            "ridges_lf": measures.get("ridges_lf"),
            "hips_lf": measures.get("hips_lf"),
            "rakes_lf": measures.get("rakes_lf"),
            "flashing_lf": measures.get("flashing_lf"),
            "transitions_lf": measures.get("transitions_lf"),
            "eaves": measures.get("eaves_lf"),
            "valleys": measures.get("valleys_lf"),
            "hips": measures.get("hips_lf"),
            "ridges": measures.get("ridges_lf"),
            "rakes": measures.get("rakes_lf"),
            "squares": measures.get("total_roof_area_sq"),
            "rates": {str(k): float(v) for k, v in (rates or {}).items()},
            "factor": factor,
            "medidas": measures,
        },
    }
    return payload_http_cotizador(payload)


def _json_num(valor):
    n = _num(valor)
    if not math.isfinite(n):
        return 0.0
    return round(n, 2)


def fila_http_tabla(row, index=1):
    """Un renglón listo para las columnas del cotizador (código, desc, qty, unidad, P.U.)."""
    limpio = item_para_frontend(row or {}, index)
    return {
        "item": limpio["item"],
        "codigo": limpio["codigo"],
        "code": limpio["codigo"],
        "sku": limpio["codigo"],
        "description": limpio["description"],
        "descripcion": limpio["description"],
        "desc": limpio["description"],
        "nombre": limpio["description"],
        "quantity": limpio["quantity"],
        "cantidad": limpio["quantity"],
        "qty": limpio["quantity"],
        "unit": limpio["unit"],
        "unidad": limpio["unit"],
        "unit_price": limpio["unit_price"],
        "precio_unitario": limpio["unit_price"],
        "precio": limpio["unit_price"],
        "price": limpio["unit_price"],
        "total": limpio["total"],
        "subtotal": limpio["total"],
        "importe": limpio["total"],
        "measure_key": limpio.get("measure_key") or "",
        "action": limpio["action"],
        "trade": "Roofing",
        "categoria": limpio.get("categoria") or "TECHO",
        "codigo_base": limpio.get("codigo_base") or limpio["codigo"],
        "nucleo": limpio.get("nucleo") or limpio["description"],
        "precio_demo": limpio.get("precio_demo") or 0,
        "precio_install": limpio.get("precio_install") or 0,
    }


def payload_http_cotizador(payload):
    """Inyecta métricas de Claude y las partidas de Supabase en el JSON que pinta index.html."""
    bruto = payload if isinstance(payload, dict) else {}
    measures = json_medidas_canonico(bruto.get("measures") or bruto.get("roof") or bruto)
    filas = []
    for i, row in enumerate(bruto.get("items") or bruto.get("partidas") or [], 1):
        if not isinstance(row, dict):
            continue
        fila = fila_http_tabla(row, i)
        if fila["quantity"] <= 0 or not fila["description"]:
            continue
        filas.append(fila)
    eaves = _json_num(measures.get("eaves_lf"))
    valleys = _json_num(measures.get("valleys_lf"))
    hips = _json_num(measures.get("hips_lf"))
    ridges = _json_num(measures.get("ridges_lf"))
    rakes = _json_num(measures.get("rakes_lf"))
    squares = _json_num(measures.get("total_roof_area_sq") or measures.get("total_sq"))
    gran_total = _json_num(bruto.get("grand_total") or bruto.get("total") or sum(f["total"] for f in filas))
    return {
        "success": True,
        "document_type": "roof_report",
        "project_type": "roofing",
        "oficio": "roofing",
        "oficio_label": bruto.get("oficio_label") or "[RFG] Roofing / Techos",
        "zip_code": measures.get("zip_code") or bruto.get("zip_code"),
        "address": measures.get("address") or bruto.get("address"),
        "waste_percent": _json_num(measures.get("waste_percent")) or 20.0,
        "factor_zona": _json_num(bruto.get("factor_zona") or 1),
        "ciudad": bruto.get("ciudad"),
        "estado": bruto.get("estado"),
        "eaves": eaves,
        "valleys": valleys,
        "hips": hips,
        "ridges": ridges,
        "rakes": rakes,
        "eaves_lf": eaves,
        "valleys_lf": valleys,
        "hips_lf": hips,
        "ridges_lf": ridges,
        "rakes_lf": rakes,
        "drip_edge_lf": _json_num(measures.get("drip_edge_lf")),
        "ventilation_ea": _json_num(measures.get("ventilation_ea")),
        "squares": squares,
        "total_sq": squares,
        "total_roof_area_sq": squares,
        "total_pitched_area_sq": _json_num(measures.get("total_pitched_area_sq")),
        "total_flat_area_sq": _json_num(measures.get("total_flat_area_sq")),
        "items": filas,
        "data": filas,
        "partidas": filas,
        "line_items": filas,
        "rows": filas,
        "measures": measures,
        "area_unit": "SQ",
        "unidad_area": "SQ",
        "aplicar_minimos": bool(bruto.get("aplicar_minimos")),
        "skip_minimums": bool(bruto.get("skip_minimums")),
        "umbral_minimos": UMBRAL_SIN_MINIMOS,
        "total": gran_total,
        "grand_total": gran_total,
        "roof": bruto.get("roof") or {
            "zip_code": measures.get("zip_code"),
            "address": measures.get("address"),
            "eaves": eaves,
            "valleys": valleys,
            "hips": hips,
            "ridges": ridges,
            "rakes": rakes,
            "eaves_lf": eaves,
            "valleys_lf": valleys,
            "hips_lf": hips,
            "ridges_lf": ridges,
            "rakes_lf": rakes,
            "squares": squares,
            "medidas": measures,
            "rates": ((bruto.get("roof") or {}).get("rates") if isinstance(bruto.get("roof"), dict) else {}),
        },
    }


def _es_concepto_lineal(desc, unit=""):
    texto = str(desc or "").lower()
    u = str(unit or "").upper()
    if u in ("LF", "LIN", "LINFT"):
        return True
    return bool(re.search(r"eaves|valleys?|rakes?|\bhips?\b|ridges?|flashing|transition|starter|ice\s*&\s*water", texto))


def _es_concepto_area(desc, unit=""):
    if _es_concepto_lineal(desc, unit):
        return False
    texto = str(desc or "").lower()
    u = str(unit or "").upper()
    return u in ("SF", "SQFT", "SQ", "SQUARES", "") or bool(
        re.search(r"roof area|pitched|flat area|tear.?off|teja|shingle|escuadra|sbs|steep", texto)
    )


def _sanitizar_partida_roof(row):
    """Último filtro antes del frontend: áreas >100 → SQ/100; perímetros → LF; subtotal = cant×precio."""
    if not isinstance(row, dict):
        return None
    row = dict(row)
    desc = str(row.get("description") or row.get("descripcion") or "")
    unit = str(row.get("unit") or row.get("unidad") or "").upper().replace(" ", "")
    qty = _num(row.get("quantity") or row.get("cantidad") or row.get("qty"))
    price = _num(row.get("unit_price") or row.get("precio_unitario") or row.get("price"))
    if _es_concepto_lineal(desc, unit):
        row["unit"] = "LF"
        row["unidad"] = "LF"
        row["quantity"] = round(qty, 2)
    else:
        if qty > 100:
            qty = round(qty / 100.0, 2)
        row["quantity"] = round(qty, 2)
        row["unit"] = "SQ"
        row["unidad"] = "SQ"
    row["unit_price"] = round(price, 2)
    row["precio_unitario"] = round(price, 2)
    row["total"] = round(_num(row["quantity"]) * price, 2)
    return row
