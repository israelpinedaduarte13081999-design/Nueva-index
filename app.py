import json
import math
import os
import re
import secrets
import ssl
import uuid
from datetime import datetime, timezone
from pathlib import Path
ssl._create_default_https_context = ssl._create_unverified_context
from flask import Flask, Response, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
client = genai.Client()
TABLA_CATALOGO = "catalogo_items"
TABLA_FACTORES = "factores_regionales_usa"
TABLA_FALTANTES = "items_faltantes"
TABLA_HISTORIAL = "historial_estimados"
TABLA_CONTRATISTAS = "contratistas"
_SQL_MULTITENANT = "Ejecuta multitenant.sql en el editor SQL de Supabase."

def _supabase_creds():
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or ""
    )
    return url, key


def _crear_cliente_supabase(url, key):
    if not url or not key:
        return None
    import httpx
    from supabase import create_client
    try:
        from supabase import ClientOptions
    except ImportError:
        from supabase.lib.client_options import ClientOptions
    http = httpx.Client(verify=False, timeout=30.0)
    try:
        options = ClientOptions(httpx_client=http)
        return create_client(url, key, options=options)
    except TypeError:
        return create_client(url, key)


try:
    _sb_url, _sb_key = _supabase_creds()
    supabase = _crear_cliente_supabase(_sb_url, _sb_key)
    if supabase is None:
        print("--> Cliente supabase es None (faltan SUPABASE_URL / SUPABASE_KEY)")
    else:
        print("--> Cliente supabase creado (SSL verify desactivado)")
except Exception as e:
    print(f"ERROR SUPABASE DETALLADO al crear cliente: {e}")
    supabase = None

import requests as _requests_uszip

FACTORES_POR_ESTADO = {
    "AL": 1.00, "AK": 1.22, "AZ": 1.08, "AR": 0.98, "CA": 1.35,
    "CO": 1.18, "CT": 1.22, "DE": 1.08, "DC": 1.28, "FL": 1.10,
    "GA": 1.05, "HI": 1.42, "ID": 1.04, "IL": 1.18, "IN": 1.02,
    "IA": 1.00, "KS": 0.99, "KY": 1.00, "LA": 1.04, "ME": 1.08,
    "MD": 1.18, "MA": 1.28, "MI": 1.06, "MN": 1.12, "MS": 0.96,
    "MO": 1.02, "MT": 1.04, "NE": 1.00, "NV": 1.12, "NH": 1.12,
    "NJ": 1.28, "NM": 1.02, "NY": 1.30, "NC": 1.04, "ND": 1.00,
    "OH": 1.04, "OK": 0.98, "OR": 1.16, "PA": 1.12, "RI": 1.18,
    "SC": 1.03, "SD": 0.99, "TN": 1.04, "TX": 1.08, "UT": 1.08,
    "VT": 1.10, "VA": 1.10, "WA": 1.22, "WV": 0.98, "WI": 1.06,
    "WY": 1.02,
}
FACTOR_ESTADO_DEFAULT = 1.05
FACTORES_ZIP_ESPECIALES = {
    "30327": 1.18,  # Buckhead, Atlanta
}

def _iniciar_uszipcode():
    orig_get = _requests_uszip.get

    def _get_sin_verify(*args, **kwargs):
        kwargs.setdefault("verify", False)
        return orig_get(*args, **kwargs)

    _requests_uszip.get = _get_sin_verify
    try:
        from uszipcode import SearchEngine
        return SearchEngine()
    finally:
        _requests_uszip.get = orig_get


import threading

_search_lock = threading.Lock()
_search_local = threading.local()

def _motor_uszipcode():
    motor = getattr(_search_local, "engine", None)
    if motor is not None:
        return motor
    with _search_lock:
        motor = getattr(_search_local, "engine", None)
        if motor is None:
            try:
                motor = _iniciar_uszipcode()
                print("--> uszipcode SearchEngine listo")
            except Exception as e:
                print(f"ERROR uszipcode al iniciar SearchEngine: {e}")
                motor = None
            _search_local.engine = motor
        return motor

@app.route("/")
@app.route("/estimado")
@app.route("/estimate")
@app.route("/contrato")
@app.route("/contract")
@app.route("/factura")
@app.route("/invoice")
@app.route("/cotizador")
@app.route("/historial")
def home():
    return send_file("index.html")


def _texto_zipcode(result, *attrs):
    if result is None:
        return None
    for attr in attrs:
        value = getattr(result, attr, None)
        if value is None:
            continue
        texto = str(value).strip()
        if texto:
            return texto
    return None


def _parse_factor_num(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numero = float(value)
        return numero if numero > 0 else None
    texto = str(value).strip().replace("$", "").replace(",", ".")
    try:
        numero = float(texto)
        return numero if numero > 0 else None
    except (TypeError, ValueError):
        return None


def _campo_factor(row, *keys):
    if not isinstance(row, dict):
        return None
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def _factor_de_fila_regional(row):
    return _parse_factor_num(
        _campo_factor(row, "factor", "factor_zip", "factor_regional", "multiplicador", "rate")
    )


def _zip_de_fila_regional(row):
    crudo = str(_campo_factor(row, "zip", "zip_code", "codigo_postal", "postal_code") or "")
    return "".join(ch for ch in crudo if ch.isdigit())[:5]


def _estado_de_fila_regional(row):
    estado = str(_campo_factor(row, "estado", "state", "state_code", "st") or "").strip().upper()
    if len(estado) > 2:
        estado = estado[:2]
    return estado


def _es_default_estado(row):
    if not _zip_de_fila_regional(row):
        return True
    marca = _campo_factor(row, "es_default", "default", "is_default", "base_estado")
    if isinstance(marca, str):
        marca = marca.strip().lower()
    return marca in (True, 1, "1", "t", "true", "si", "sí", "yes")


def _filas_factores_regionales():
    if supabase is not None:
        try:
            res = supabase.table(TABLA_FACTORES).select("*").limit(2000).execute()
            filas = list(res.data or [])
            print(f"--> {TABLA_FACTORES}: {len(filas)} registros")
            if filas:
                print(f"--> Columnas {TABLA_FACTORES}: {list(filas[0].keys())}")
            return filas
        except Exception as e:
            print(f"ERROR {TABLA_FACTORES} supabase: {e}")
    url, key = _supabase_creds()
    if not url or not key:
        return []
    try:
        import urllib.request

        req = urllib.request.Request(
            f"{url}/rest/v1/{TABLA_FACTORES}?select=*&limit=2000",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        filas = _urlopen_json(req)
        return filas if isinstance(filas, list) else []
    except Exception as e:
        print(f"ERROR {TABLA_FACTORES} REST: {e}")
        return []


def _factor_desde_supabase(zip_code, estado):
    filas = _filas_factores_regionales()
    if not filas:
        return None
    for row in filas:
        factor = _factor_de_fila_regional(row)
        if factor and _zip_de_fila_regional(row) == zip_code:
            print(f"--> Factor ZIP {zip_code} en Supabase: {factor}")
            return factor
    if estado:
        for row in filas:
            factor = _factor_de_fila_regional(row)
            if factor and _estado_de_fila_regional(row) == estado and _es_default_estado(row):
                print(f"--> Factor estado {estado} (default) en Supabase: {factor}")
                return factor
        for row in filas:
            factor = _factor_de_fila_regional(row)
            if factor and _estado_de_fila_regional(row) == estado and not _zip_de_fila_regional(row):
                print(f"--> Factor estado {estado} en Supabase: {factor}")
                return factor
    return None


def _zona_mercado_por_zip(zip_code):
    zip_limpio = "".join(ch for ch in str(zip_code or "") if ch.isdigit())[:5]
    zona = {
        "zip": zip_limpio,
        "ciudad": None,
        "estado": None,
        "zona": None,
        "factor": 1.0,
        "exito": False,
    }
    if len(zip_limpio) != 5:
        return zona
    try:
        motor = _motor_uszipcode()
        result = motor.by_zipcode(zip_limpio) if motor is not None else None
        ciudad = _texto_zipcode(result, "major_city", "city", "post_office_city")
        if ciudad and "," in ciudad:
            ciudad = ciudad.split(",")[0].strip()
        estado = _texto_zipcode(result, "state")
        if estado:
            estado = estado.upper()
            if len(estado) > 2:
                estado = estado[:2]
        zona["ciudad"] = ciudad
        zona["estado"] = estado
    except Exception as e:
        print(f"ERROR uszipcode lookup {zip_limpio}: {e}")

    factor = _factor_desde_supabase(zip_limpio, zona.get("estado"))
    if factor is None and zip_limpio in FACTORES_ZIP_ESPECIALES:
        factor = float(FACTORES_ZIP_ESPECIALES[zip_limpio])
        if zip_limpio == "30327":
            zona["ciudad"] = zona["ciudad"] or "Atlanta"
            zona["estado"] = zona["estado"] or "GA"
            zona["zona"] = "Buckhead"
    if factor is None:
        factor = float(FACTORES_POR_ESTADO.get(zona.get("estado") or "", FACTOR_ESTADO_DEFAULT))
    zona["factor"] = round(float(factor), 4)
    zona["exito"] = True
    return zona


@app.route("/api/get-zip-factor/<zip_code>")
def get_zip_factor(zip_code):
    zona = _zona_mercado_por_zip(zip_code)
    if not zona.get("exito"):
        return jsonify({"ciudad": None, "estado": None, "factor": FACTOR_ESTADO_DEFAULT, "exito": False}), 400
    payload = {
        "ciudad": zona.get("ciudad"),
        "estado": zona.get("estado"),
        "factor": round(float(zona.get("factor") or FACTOR_ESTADO_DEFAULT), 2),
        "exito": True,
    }
    if zona.get("zona"):
        payload["zona"] = zona["zona"]
    print(f"--> ZIP {zona.get('zip')}: ciudad={payload['ciudad']} estado={payload['estado']} factor={payload['factor']}")
    return jsonify(payload)

client = genai.Client(api_key="AQ.Ab8RN6LkOhQ2FcF4mtAC2F9HMXz5pZeJRM6wAOaM3CYHL2yU_w")

# Tarifas de mano de obra de techo por ZIP (USD). Claves alineadas con las partidas generadas.
PRECIOS_TECHO_POR_ZIP = {
    "30228": {  # Hampton / Lovejoy, GA
        "tear_off": 50.00,
        "arch_shingle": 78.00,
        "steep": 22.00,
        "flat_sbs": 130.00,
        "drip_edge": 11.00,
        "starter": 3.25,
        "ice_water": 3.40,
        "valley": 11.00,
        "hips_ridges": 13.50,
    },
    "30060": {  # Marietta, GA
        "tear_off": 58.00,
        "arch_shingle": 90.00,
        "steep": 28.00,
        "flat_sbs": 145.00,
        "drip_edge": 12.50,
        "starter": 3.75,
        "ice_water": 3.85,
        "valley": 13.00,
        "hips_ridges": 9.50,
    },
    "30080": {  # Smyrna, GA
        "tear_off": 60.00,
        "arch_shingle": 92.00,
        "steep": 30.00,
        "flat_sbs": 148.00,
        "drip_edge": 13.00,
        "starter": 3.85,
        "ice_water": 4.00,
        "valley": 13.50,
        "hips_ridges": 10.00,
    },
    "default": {
        "tear_off": 55.00,
        "arch_shingle": 85.00,
        "steep": 25.00,
        "flat_sbs": 130.00,
        "drip_edge": 11.00,
        "starter": 1.25,
        "ice_water": 1.40,
        "valley": 8.00,
        "hips_ridges": 13.50,
    },
}

# Tarifas fijas de mano de obra usadas al armar el cotizador desde un Roofr.
PRECIOS_ROOFR = {
    "tear_off": 55.00,
    "arch_shingle": 85.00,
    "steep": 25.00,
    "flat_sbs": 130.00,
    "starter": 1.25,
    "drip_edge": 11.00,
    "ice_water": 1.40,
    "hips_ridges": 13.50,
    "valley": 8.00,
    "rakes": 1.25,
}

WASTE_FACTOR = 1.15  # 15% waste solo en instalación de teja
DRIP_EDGE_LENGTH_FT = 10.0
MAX_LABOR_SQ = 200.0  # nunca usar tarifas tipo material/GC (~$375/SQ)
BASURA_REPORTE = re.compile(
    r"roof report|measurements?(?:\s+total)?|total roof area|no laps|\bsqft\b|"
    r"total pitched area|total flat area|shingle\s*\(total sqft\)|total squares|"
    r"predominant pitch|facets?|waste factor|length of|roof area(?!\s*sbs)|"
    r"\bsubtotal\b|grand total|total general|diagram|legend|disclaimer|"
    r"number of facets|page\s*\d+|sq\s*ft\s+total|squares?\s+total",
    re.I,
)

PROMPT_PDF = """
Analiza este PDF de construcción.

Clasifica el documento:
- "roof_report" si es Roofr, EagleView, Hover, GAF QuickMeasure o cualquier reporte de MEDICIONES de techo (áreas, pitch, eaves, rakes, valleys, ridges). NO es un estimado de precios.
- "estimate" solo si hay partidas de trabajo cobrables (estimado, factura o cotización con precios de oficio).

Si es reporte de techo, extrae MEDICIONES CRUDAS. Las áreas van en pies cuadrados (sqft), NUNCA en squares.
NO conviertas a SQ. NO inventes partidas. items DEBE ser [].
NO copies textos del reporte como "measurements", "total roof area", "no laps", "eaves", "rakes", "subtotal".
NO dupliques la misma área en Sq Ft y en Squares.

{
  "document_type": "roof_report",
  "zip_code": "30228",
  "address": "dirección completa",
  "pitched_sqft": 4548,
  "flat_sqft": 158,
  "total_sqft": 4705,
  "steep_sqft": 3308,
  "pitch": "8/12",
  "eaves_lf": 366,
  "rakes_lf": 0,
  "eaves_rakes_lf": 366,
  "valleys_lf": 48,
  "ridges_lf": 242,
  "hips_lf": 0,
  "hips_ridges_lf": 242,
  "ice_water_lf": 414,
  "drip_edge_pcs": 38,
  "items": []
}

steep_sqft = suma de facetas con pitch >= 8/12, en sqft.
eaves_lf, rakes_lf, ridges_lf, valleys_lf en pies lineales (LF).

Si es estimado tradicional:
Detecta el oficio PRINCIPAL del documento (uno solo): roofing, siding, flooring, bathroom, kitchen, drywall, painting, tile, framing, windows, gutters, plumbing, electrical, hvac, demolition, deck.
No mezcles partidas de otro oficio (p. ej. no pongas siding HardieShingle en un techo).
{
  "document_type": "estimate",
  "project_type": "flooring",
  "zip_code": null,
  "address": null,
  "items": [{"item":1,"trade":"Pisos","description":"trabajo real","quantity":10,"unit":"SF","unit_price":null,"total":null}]
}
"""

PROMPT_ROOF_REPORT = """
Este PDF es un ROOF REPORT de MEDICIONES (Roofr, EagleView, Hover, GAF QuickMeasure u otro).
NO es un estimado: no trae precios. Tú lees el TEXTO línea por línea.

TAREA:
1) Lee cada línea del reporte. Ignora encabezados, diagramas, leyendas, disclaimers, números de página,
   "Measurements", "Total roof area" duplicado, "Shingle (total sqft)" si ya tienes el área principal,
   "no laps", subtotales y totales generales.
2) Identifica la naturaleza de cada medida útil:
   - Área de techo / shingle / pitched / steep / flat / squares / escuadras → unidad de ÁREA.
   - Pies cuadrados de superficie de techo (sq ft / sqft) → convertir o dejar según la unidad pedida.
   - Pies lineales (eaves, rakes, valleys, hips, ridges, starter, ice & water, flashing lineal) → LF.
   - Drip edge en piezas de 10 ft → PC.
3) Mapea EXACTAMENTE a unidades de estimado:
   - SQ = squares / escuadras. 1 SQ = 100 sq ft. Si el reporte da 4548 sqft, quantity=45.48 y unit=SQ.
   - SF = pies cuadrados. Úsalo SOLO si AREA_UNIT=SF.
   - LF = pies lineales. NUNCA pongas un área de techo en LF ni un lineal en SQ.
   AREA_UNIT pedida por el estimador: {UNIDAD_AREA}
   Si AREA_UNIT=SQ, todas las ÁREAS de techo van en SQ (nunca dejes 4548 como SQ).
   Si AREA_UNIT=SF, todas las ÁREAS de techo van en SF (4548 SF).
4) Devuelve partidas LIMPIAS de estimado (descripciones de trabajo, no etiquetas del reporte).
   Incluye, según medidas halladas: tear-off, teja arquitectónica (waste 15% solo en teja),
   recargo steep si pitch >= 8/12, techo plano SBS, starter, drip edge, ice & water, ridge cap, valley, rakes.
5) unit_price y total DEBEN ser null. No inventes precios.

JSON estricto:
{
  "document_type": "roof_report",
  "zip_code": "30228 o null",
  "address": "dirección o null",
  "pitch": "8/12",
  "pitched_sqft": 4548,
  "flat_sqft": 158,
  "total_sqft": 4705,
  "steep_sqft": 3308,
  "eaves_lf": 366,
  "rakes_lf": 0,
  "valleys_lf": 48,
  "ridges_lf": 242,
  "ice_water_lf": 414,
  "drip_edge_pcs": 38,
  "items": [
    {"item":1,"trade":"Roofing","description":"Remoción de teja asfáltica (Tear-off)","quantity":47.05,"unit":"SQ","unit_price":null,"total":null,"action":"demo"},
    {"item":2,"trade":"Roofing","description":"Instalación de teja arquitectónica (con waste 15%)","quantity":52.3,"unit":"SQ","unit_price":null,"total":null,"action":"install"},
    {"item":3,"trade":"Roofing","description":"Starter Strip","quantity":366,"unit":"LF","unit_price":null,"total":null,"action":"install"}
  ]
}
"""


def _num(value, default=0.0):
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else default


def _zip_key(zip_code):
    digits = re.sub(r"\D", "", str(zip_code or ""))[:5]
    if digits in PRECIOS_TECHO_POR_ZIP:
        return digits
    return "default"


def tarifas_techo(zip_code):
    return PRECIOS_TECHO_POR_ZIP[_zip_key(zip_code)]


def parse_pitch_rise(pitch):
    text = str(pitch or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)\s*[/:(]\s*12", text)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:in\s*12|inch(?:es)?)", text, re.I)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else 0.0


def sqft_a_squares(value):
    """Convierte área de Roofr (sqft) a SQ. 4548 sqft -> 45.48 SQ. 47.05 ya es SQ."""
    qty = max(_num(value), 0.0)
    if qty <= 0:
        return 0.0
    if qty >= 100:
        qty = qty / 100.0
    return round(qty, 2)


def area_a_squares(medidas, sqft_keys, square_keys=()):
    for key in sqft_keys:
        if medidas.get(key) not in (None, "", 0, "0"):
            return round(_num(medidas.get(key)) / 100.0, 2)
    for key in square_keys:
        if medidas.get(key) not in (None, "", 0, "0"):
            return sqft_a_squares(medidas.get(key))
    return 0.0


def texto_fila(row):
    if not isinstance(row, dict):
        return str(row or "")
    parts = []
    for key in ("description", "descripcion", "desc", "nombre", "name", "title", "item", "trade", "oficio", "unit", "unidad", "label"):
        value = row.get(key)
        if value is not None and not isinstance(value, (int, float)):
            parts.append(str(value))
    return " ".join(parts)


def es_medida_area_redundante(desc):
    texto = str(desc or "").strip().lower()
    if not texto:
        return True
    return bool(
        re.search(
            r"measurements?\s*(?:total\s*)?roof\s*area|"
            r"total\s+pitched\s+area|"
            r"total\s+flat\s+area|"
            r"shingle\s*\(\s*total\s*sqft\s*\)|"
            r"shingle\s*\(\s*total\s*\)|"
            r"total\s+roof\s+area|"
            r"total\s+squares?|"
            r"^roof\s+area$|"
            r"^pitched\s+area$|"
            r"^flat\s+area$",
            texto,
        )
    )


def es_etiqueta_reporte(desc):
    text = str(desc or "").strip().lower()
    if not text:
        return True
    if es_medida_area_redundante(text):
        return True
    if re.search(
        r"tear-off|teja arquitect|starter strip|drip edge|ice\s*&\s*water|ridge cap|sbs|remoci[oó]n de teja|steep charge|pendiente pronunciada|valley metal|limahoya|\brakes\b",
        text,
    ):
        return False
    if BASURA_REPORTE.search(text):
        return True
    return bool(
        re.search(
            r"measurement|total roof area|no laps|eaves|rakes|valleys?|hips?\b|ridges?\b|pitch\b|squares?\b|sqft|sq ft|facet|waste factor|flashing|length of|roof area|predominant|roof report|subtotal|grand total|diagram|legend",
            text,
        )
    )


def unidad_area_techo(valor):
    texto = str(valor or "").strip().upper().replace(".", "")
    if texto in ("SF", "SQFT", "SQ FT", "SQFT", "SQUARE FEET", "PIES", "PIES CUADRADOS"):
        return "SF"
    return "SQ"


def nucleo_desc_techo(desc):
    texto = re.sub(r"\s+", " ", str(desc or "").lower()).strip()
    texto = re.sub(r"\b(sq\.?\s*ft|sqft|squares?|escuadras?|\bsq\b|\bsf\b|pies cuadrados)\b", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def es_fila_vacia_o_info(row):
    if not isinstance(row, dict):
        return True
    desc = str(row.get("description") or row.get("descripcion") or "").strip()
    qty = _num(row.get("quantity") or row.get("cantidad") or row.get("qty"))
    if not desc or qty <= 0:
        return True
    if es_etiqueta_reporte(desc):
        return True
    if re.search(r"\bsubtotal\b|grand total|total general|^total\s*$", desc, re.I):
        return True
    return False


def qty_a_sqft(qty, unit):
    u = str(unit or "").upper().replace(" ", "")
    q = max(_num(qty), 0.0)
    if u in ("SQ", "SQUARES", "SQUARE"):
        return q * 100.0 if q < 80 else q
    if u in ("SF", "SQFT"):
        return q if q >= 80 else q * 100.0
    return None


def unificar_y_filtrar_partidas(items, unidad_area="SQ"):
    preferida = unidad_area_techo(unidad_area)
    limpios = []
    vistos_desc = set()
    areas_vistas = []
    for row in items or []:
        if es_fila_vacia_o_info(row):
            continue
        row = dict(row)
        desc = str(row.get("description") or row.get("descripcion") or "").strip()
        if es_medida_area_redundante(desc):
            continue
        nucleo = nucleo_desc_techo(desc)
        if nucleo in vistos_desc:
            continue
        unit = str(row.get("unit") or row.get("unidad") or "SQ").upper().replace(" ", "")
        qty = _num(row.get("quantity") or row.get("cantidad") or row.get("qty"))
        price = _num(row.get("unit_price") or row.get("precio_unitario") or row.get("price"))
        if unit in ("SQ", "SQUARES", "SQUARE", "SF", "SQFT"):
            sqft = qty_a_sqft(qty, unit)
            if sqft:
                duplicada = False
                for previa, nuc_prev in areas_vistas:
                    misma_area = abs(previa - sqft) <= max(2.0, previa * 0.02)
                    generica = bool(re.search(r"\b(total|area|pitched|flat|measurement|square)\b", nucleo + " " + nuc_prev))
                    if misma_area and (nucleo == nuc_prev or generica):
                        duplicada = True
                        break
                if duplicada:
                    continue
                areas_vistas.append((sqft, nucleo))
                qty_sq = round(sqft / 100.0, 2)
                era_sq = unit in ("SQ", "SQUARES", "SQUARE")
                if preferida == "SF":
                    if era_sq and price:
                        price = round(price / 100.0, 4)
                    qty = round(sqft, 2)
                    unit = "SF"
                else:
                    if not era_sq and price:
                        price = round(price * 100.0, 2)
                    qty = qty_sq
                    unit = "SQ"
        vistos_desc.add(nucleo)
        row["description"] = desc
        row["quantity"] = round(qty, 2)
        row["unit"] = unit
        row["unidad"] = unit
        row["unit_price"] = round(price, 4 if unit == "SF" else 2)
        row["total"] = round(row["quantity"] * row["unit_price"], 2)
        limpios.append(row)
    for i, row in enumerate(limpios, 1):
        row["item"] = i
    return limpios


def iterar_filas(parsed):
    filas = []
    if isinstance(parsed, list):
        filas.extend(row for row in parsed if isinstance(row, dict))
        return filas
    if not isinstance(parsed, dict):
        return filas
    for key in ("items", "partidas", "data", "lines", "line_items"):
        value = parsed.get(key)
        if isinstance(value, str):
            extra = extraer_json(value)
            if isinstance(extra, list):
                filas.extend(row for row in extra if isinstance(row, dict))
            elif isinstance(extra, dict):
                filas.extend(iterar_filas(extra))
        elif isinstance(value, list):
            filas.extend(row for row in value if isinstance(row, dict))
    return filas


def parece_reporte_techo(parsed):
    if isinstance(parsed, list):
        items = parsed
        blob = " ".join(str((row or {}).get("description") or "") for row in items if isinstance(row, dict)).lower()
        return bool(re.search(r"roof area|roofr|eagleview|no laps|pitched roof|total roof|measurements", blob))
    if not isinstance(parsed, dict):
        return False
    if "roof" in str(parsed.get("document_type") or "").lower():
        return True
    roof_keys = (
        "pitched_sqft",
        "flat_sqft",
        "total_sqft",
        "pitched_squares",
        "flat_squares",
        "eaves_rakes_lf",
        "eaves_lf",
        "steep_sqft",
    )
    if any(parsed.get(key) not in (None, "", 0, "0") for key in roof_keys):
        return True
    items = parsed.get("items") or parsed.get("partidas") or []
    return parece_reporte_techo(items) if items else False


def cosechar_medidas_de_items(items):
    medidas = {}
    for row in items or []:
        if not isinstance(row, dict):
            continue
        desc = texto_fila(row).lower()
        qty = _num(row.get("quantity") or row.get("cantidad") or row.get("qty") or row.get("value") or row.get("area"))
        unit = str(row.get("unit") or row.get("unidad") or "").lower()
        if qty <= 0:
            continue
        es_area = bool(re.search(r"sqft|sq ft|\bsf\b|\bsq\b|area", desc + " " + unit)) or qty >= 400
        if re.search(r"total pitched|pitched roof|pitched area", desc):
            medidas["pitched_sqft"] = qty
        elif re.search(r"total flat|flat roof|flat area|low.?slope", desc):
            medidas["flat_sqft"] = qty
        elif re.search(r"shingle\s*\(total|total roof|measurements total|roof area", desc) and es_area:
            medidas["total_sqft"] = qty
        elif re.search(r"steep|8/12|9/12|10/12|12/12", desc) and es_area:
            medidas["steep_sqft"] = medidas.get("steep_sqft", 0) + qty
        elif "pitched" in desc and es_area:
            medidas["pitched_sqft"] = qty
        elif re.search(r"ice\s*&\s*water|ice and water", desc):
            medidas["ice_water_lf"] = qty
        elif re.search(r"drip", desc):
            medidas["drip_edge_pcs"] = qty if qty < 500 else math.ceil(qty / DRIP_EDGE_LENGTH_FT)
        elif "eaves" in desc and "rake" in desc:
            medidas["eaves_rakes_lf"] = qty
        elif re.search(r"\beaves\b", desc):
            medidas["eaves_lf"] = qty
        elif re.search(r"\brakes\b", desc):
            medidas["rakes_lf"] = qty
        elif re.search(r"valleys?", desc):
            medidas["valleys_lf"] = qty
        elif re.search(r"ridges?", desc) and "hip" not in desc:
            medidas["ridges_lf"] = qty
        elif re.search(r"\bhips?\b", desc) and "ridge" in desc:
            medidas["hips_ridges_lf"] = qty
        elif re.search(r"\bhips?\b", desc):
            medidas["hips_lf"] = qty
        elif re.search(r"\bpitch\b", desc) and not medidas.get("pitch"):
            medidas["pitch"] = texto_fila(row)
        elif es_area and qty >= 1000 and not medidas.get("total_sqft"):
            medidas["total_sqft"] = qty
    return medidas


def fusionar_medidas(parsed):
    medidas = dict(parsed) if isinstance(parsed, dict) else {}
    items = []
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        items = parsed.get("items") or parsed.get("partidas") or []
        roof = parsed.get("roof") if isinstance(parsed.get("roof"), dict) else {}
        medidas.update(roof)
    cosecha = cosechar_medidas_de_items(items)
    for key, value in cosecha.items():
        if medidas.get(key) in (None, "", 0, "0"):
            medidas[key] = value
    return medidas


def linea(item, trade, description, quantity, unit, unit_price, unidad_area="SQ"):
    qty = round(max(_num(quantity), 0.0), 2)
    price = round(max(_num(unit_price), 0.0), 2)
    if unit == "SQ":
        qty_sq = sqft_a_squares(qty)
        if unidad_area_techo(unidad_area) == "SF":
            qty = round(qty_sq * 100.0, 2)
            price = round(price / 100.0, 4)
            unit = "SF"
        else:
            qty = qty_sq
            unit = "SQ"
    if qty <= 0:
        return None
    price = max(price, 0.0)
    if unit == "PC":
        qty = int(round(qty))
    total = round(qty * price, 2)
    return {
        "item": item,
        "trade": trade,
        "description": description,
        "quantity": qty,
        "unit": unit,
        "unidad": unit,
        "unit_price": round(price, 4 if unit == "SF" else 2),
        "precio_unitario": round(price, 4 if unit == "SF" else 2),
        "total": total,
        "action": "demo" if "tear-off" in description.lower() or "remoción" in description.lower() else "install",
    }


def _precios_partidas_techo(zip_code):
    """Precios de techo: catálogo Supabase (región/ZIP) con respaldo regional local. Sin precios del PDF."""
    rates = dict(PRECIOS_ROOFR)
    try:
        rates.update({k: v for k, v in (tarifas_techo(zip_code) or {}).items() if _num(v) > 0})
    except Exception:
        pass
    try:
        rates.update({k: v for k, v in (_tarifas_techo_desde_catalogo(zip_code) or {}).items() if _num(v) > 0})
    except Exception as err:
        print(f"precios catalogo techo: {err}")
    return rates


def generar_partidas_techo(medidas, unidad_area="SQ"):
    unidad_area = unidad_area_techo(unidad_area)
    rates = _precios_partidas_techo(medidas.get("zip_code"))
    pitched = area_a_squares(medidas, ("pitched_sqft", "pitched_sf"), ("pitched_squares", "pitched"))
    flat = area_a_squares(medidas, ("flat_sqft", "flat_sf"), ("flat_squares", "flat"))
    total = area_a_squares(medidas, ("total_sqft", "total_roof_sqft", "roof_sqft"), ("total_squares",))
    if pitched <= 0 and total > 0:
        pitched = round(max(total - flat, 0.0), 2)
    if total <= 0:
        total = round(pitched + flat, 2)
    steep = area_a_squares(medidas, ("steep_sqft",), ("steep_squares",))
    eaves = round(max(_num(medidas.get("eaves_lf")), 0.0), 2)
    rakes = round(max(_num(medidas.get("rakes_lf")), 0.0), 2)
    eaves_rakes = round(max(_num(medidas.get("eaves_rakes_lf")), eaves + rakes), 2)
    valleys = round(max(_num(medidas.get("valleys_lf")), 0.0), 2)
    ridges = round(max(_num(medidas.get("ridges_lf")), 0.0), 2)
    hips_ridges = round(max(_num(medidas.get("hips_ridges_lf")), ridges + _num(medidas.get("hips_lf"))), 2)
    ice_water = round(max(_num(medidas.get("ice_water_lf")), 0.0), 2)
    if ice_water <= 0:
        ice_water = round((eaves or eaves_rakes) + valleys, 2)
    starter = eaves if eaves > 0 else eaves_rakes
    drip_pcs = _num(medidas.get("drip_edge_pcs"))
    if drip_pcs <= 0 and eaves_rakes > 0:
        drip_pcs = math.ceil(eaves_rakes / DRIP_EDGE_LENGTH_FT) + 1
    drip_pcs = int(math.ceil(drip_pcs)) if drip_pcs > 0 else 0
    pitch_raw = medidas.get("pitch") or ""
    rise = parse_pitch_rise(pitch_raw)
    pitch_label = f"{int(rise)}/12" if rise else (str(pitch_raw).strip() or "8/12")
    if steep <= 0 and rise >= 8:
        steep = pitched
    shingle_qty = round(pitched * WASTE_FACTOR, 2)

    items = []
    n = 1
    tear_off_qty = total if total > 0 else pitched
    if tear_off_qty > 0:
        items.append(linea(n, "Roofing", "Remoción de teja asfáltica (Tear-off)", tear_off_qty, "SQ", rates.get("tear_off", PRECIOS_ROOFR["tear_off"]), unidad_area))
        n += 1
    if shingle_qty > 0:
        items.append(linea(n, "Roofing", "Instalación de teja arquitectónica (con waste 15%)", shingle_qty, "SQ", rates.get("arch_shingle", PRECIOS_ROOFR["arch_shingle"]), unidad_area))
        n += 1
    if steep > 0:
        items.append(linea(n, "Roofing", "Recargo por pendiente pronunciada (Pitch >= 8/12)", steep, "SQ", rates.get("steep", PRECIOS_ROOFR["steep"]), unidad_area))
        n += 1
    if flat > 0:
        items.append(linea(n, "Roofing", "Instalación de techo plano (SBS Flat Roof)", flat, "SQ", rates.get("flat_sbs", PRECIOS_ROOFR["flat_sbs"]), unidad_area))
        n += 1
    if starter > 0:
        items.append(linea(n, "Roofing", "Starter Strip", starter, "LF", rates.get("starter", PRECIOS_ROOFR["starter"]), unidad_area))
        n += 1
    if drip_pcs > 0:
        items.append(linea(n, "Roofing", "Drip Edge (10 ft)", drip_pcs, "PC", rates.get("drip_edge", PRECIOS_ROOFR["drip_edge"]), unidad_area))
        n += 1
    if ice_water > 0:
        items.append(linea(n, "Roofing", "Ice & Water Shield", ice_water, "LF", rates.get("ice_water", PRECIOS_ROOFR["ice_water"]), unidad_area))
        n += 1
    ridge_qty = ridges if ridges > 0 else hips_ridges
    if ridge_qty > 0:
        items.append(linea(n, "Roofing", "Ridge Cap / Cumbrera", ridge_qty, "LF", rates.get("hips_ridges", PRECIOS_ROOFR["hips_ridges"]), unidad_area))
        n += 1
    if valleys > 0:
        items.append(linea(n, "Roofing", "Valley metal / Limahoya", valleys, "LF", rates.get("valley", PRECIOS_ROOFR["valley"]), unidad_area))
        n += 1
    if rakes > 0 and eaves > 0:
        items.append(linea(n, "Roofing", "Rakes", rakes, "LF", rates.get("rakes", PRECIOS_ROOFR["rakes"]), unidad_area))
        n += 1
    return [row for row in items if row]


def parece_medidas_techo_en_filas(filas, parsed=None):
    blob = " ".join(texto_fila(row) for row in filas).lower()
    if BASURA_REPORTE.search(blob) or re.search(r"no laps|roofr|eagleview|pitched roof|total pitched", blob):
        return True
    if parsed is not None and parece_reporte_techo(parsed):
        return True
    for row in filas:
        qty = _num(row.get("quantity") or row.get("cantidad") or row.get("qty"))
        unit = str(row.get("unit") or row.get("unidad") or "").lower()
        if qty >= 400 and re.search(r"sqft|sq ft|\bsf\b|\bsq\b", texto_fila(row).lower() + " " + unit):
            return True
        price = _num(row.get("unit_price") or row.get("precio") or row.get("price"))
        if qty >= 400 and price >= 200:
            return True
    return False


def limitar_precio_labor(row):
    if not isinstance(row, dict):
        return None
    unit = str(row.get("unit") or row.get("unidad") or "").upper()
    qty = _num(row.get("quantity") or row.get("cantidad") or row.get("qty"))
    price = _num(row.get("unit_price") or row.get("precio_unitario") or row.get("price"))
    if unit in ("SQ", "SF", "SQFT") and qty >= 100:
        qty = round(qty / 100.0, 2)
        unit = "SQ"
    if unit in ("LF", "PC", "EA") and price >= 50:
        price = PRECIOS_ROOFR.get("starter", 1.25) if unit == "LF" else PRECIOS_ROOFR["drip_edge"]
    if unit == "SQ" and price >= MAX_LABOR_SQ:
        price = PRECIOS_ROOFR["arch_shingle"]
    row = dict(row)
    row["quantity"] = qty
    row["unit"] = unit or row.get("unit")
    row["unit_price"] = round(price, 2)
    row["total"] = round(qty * price, 2)
    return row


def sanitizar_respuesta_pdf(parsed, unidad_area="SQ", zip_activo=None, forzar_estimado=False):
    unidad_area = unidad_area_techo(unidad_area)
    filas = iterar_filas(parsed)
    medidas = fusionar_medidas(parsed)
    cosecha = cosechar_medidas_de_items(filas)
    for key, value in cosecha.items():
        if medidas.get(key) in (None, "", 0, "0"):
            medidas[key] = value
    if not forzar_estimado and (
        parece_medidas_techo_en_filas(filas, parsed)
        or any(_num(medidas.get(k)) > 0 for k in ("pitched_sqft", "flat_sqft", "total_sqft", "pitched_squares", "total_squares"))
    ):
        zip_precio = re.sub(r"\D", "", str(zip_activo or medidas.get("zip_code") or ""))[:5]
        if zip_precio:
            medidas["zip_code"] = zip_precio
        items = unificar_y_filtrar_partidas(generar_partidas_techo(medidas, unidad_area), unidad_area)
        return items, medidas, True
    limpios = []
    for row in filas:
        if not isinstance(row, dict):
            continue
        desc = str(row.get("description") or row.get("descripcion") or "").strip()
        qty = _num(row.get("quantity") or row.get("cantidad") or row.get("qty"))
        if not desc or qty <= 0:
            continue
        limpios.append(dict(row))
    return limpios, medidas, False


_PALABRAS_EN_DOC = re.compile(
    r"\b(the|and|of|for|with|install|installation|remove|removal|replace|replacement|"
    r"carpet|padding|stair|door|paint|wall|ceiling|floor|drywall|tile|cabinet|"
    r"kitchen|bathroom|shower|roof|siding)\b",
    re.I,
)
_PALABRAS_ES_DOC = re.compile(
    r"\b(instalaci[oó]n|demolici[oó]n|gabinete|azulejo|techo|piso|pintura|suministro|"
    r"retiro|pared|cielo|ducha|ba[nñ]o|cocina|moldura|alfombra|reemplazo)\b",
    re.I,
)
IDIOMAS_CATALOGO = {
    "es": ("descripcion_es", "descripcion", "desc"),
    "en": (
        "descripcion_en",
        "description_en",
        "descripcion_ingles",
        "desc_en",
        "description",
        "ingles",
        "english",
        "english_description",
    ),
}


def _claves_idioma_en_fila(row, idioma="es"):
    claves = list(IDIOMAS_CATALOGO.get(str(idioma or "es").lower()) or IDIOMAS_CATALOGO["es"])
    if not isinstance(row, dict):
        return claves
    extra = []
    for key in row.keys():
        kl = str(key).lower()
        if idioma == "en":
            if kl.endswith("_en") or "ingles" in kl or kl in ("description", "english"):
                extra.append(key)
        elif idioma == "es":
            if kl.endswith("_es") or kl in ("descripcion", "desc"):
                extra.append(key)
    vistos = {str(c).lower() for c in claves}
    for key in extra:
        if str(key).lower() not in vistos:
            claves.append(key)
            vistos.add(str(key).lower())
    return claves


def _descripcion_idioma_catalogo(row, idioma="es"):
    if not isinstance(row, dict):
        return ""
    lang = str(idioma or "es").strip().lower()
    for key in _claves_idioma_en_fila(row, lang):
        val = str(row.get(key) or "").strip()
        if not val:
            continue
        if lang == "en" and str(key).lower() in ("descripcion", "desc", "descripcion_es"):
            continue
        return val
    if lang == "en":
        desc = str(row.get("descripcion") or row.get("desc") or "").strip()
        if desc and _parece_ingles_doc(desc):
            return desc
    return ""


def _textos_idioma_catalogo(row):
    return {code: _descripcion_idioma_catalogo(row, code) for code in IDIOMAS_CATALOGO}


def _parece_ingles_doc(texto):
    t = str(texto or "").strip()
    if not t:
        return False
    if re.search(r"[áéíóúñüÁÉÍÓÚÑÜ]", t):
        return False
    en = len(_PALABRAS_EN_DOC.findall(t))
    es = len(_PALABRAS_ES_DOC.findall(t))
    return en >= 2 and en > es


def _texto_ingles_catalogo(row):
    return _descripcion_idioma_catalogo(row, "en")


def extraer_json(texto):
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
        return json.loads(match.group(1))


def _supabase_client():
    if supabase is not None:
        return supabase
    url, key = _supabase_creds()
    if not url or not key:
        return None
    try:
        return _crear_cliente_supabase(url, key)
    except Exception as e:
        print(f"ERROR SUPABASE DETALLADO al crear cliente: {e}")
        return None


def _limpiar_termino_catalogo(q):
    texto = re.sub(r"[^\wáéíóúüñÁÉÍÓÚÜÑ\s\-./]", " ", str(q or ""), flags=re.UNICODE)
    return re.sub(r"\s+", " ", texto).strip()[:80]


_PREFIJO_ACCION_CATALOGO = re.compile(
    r"^(remover,\s*instalar m[aá]s materiales:?|instalar m[aá]s materiales:?|remover e instalar|"
    r"retiro\s*/\s*demolici[oó]n(?:\s*de)?|solo demolici[oó]n(?:\s*/\s*remover)?|"
    r"intalaci[oó]n de|instalaci[oó]n de|instalacion de|solo instalar|retiro de|tear-?off(?:\s+de)?|"
    r"suministro de material|material only|labor only|solo mano de obra|suministro de)\s+",
    re.I,
)


def _corregir_typos_catalogo(q):
    texto = str(q or "")
    reemplazos = (
        (r"\bintalaci[oó]n(es)?\b", "instalacion"),
        (r"\bintalar\b", "instalar"),
        (r"\binstalcion(es)?\b", "instalacion"),
        (r"\bceramicas?\b", "ceramica"),
        (r"\bazulejos?\b", "azulejo"),
    )
    for patron, dest in reemplazos:
        texto = re.sub(patron, dest, texto, flags=re.I)
    return texto


def _nucleo_busqueda_catalogo(q):
    texto = _limpiar_termino_catalogo(q)
    for _ in range(4):
        nuevo = _PREFIJO_ACCION_CATALOGO.sub("", texto).strip()
        if nuevo == texto:
            break
        texto = nuevo
    return texto or _limpiar_termino_catalogo(q)


def _codigo_catalogo_compacto(q):
    return re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ]", "", _limpiar_termino_catalogo(q))


def _valor_fila(row, *keys):
    if not isinstance(row, dict):
        return None
    lower = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        if key.lower() in lower:
            return lower[key.lower()]
    return None


def _num_catalogo(row, *keys):
    for key in keys:
        raw = _valor_fila(row, key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        texto = str(raw).strip().replace("$", "").replace(",", "")
        try:
            return float(texto)
        except (TypeError, ValueError):
            match = re.search(r"-?\d+(?:\.\d+)?", texto)
            if match:
                return float(match.group(0))
    return 0.0


def _texto_catalogo(row, *keys, default=""):
    for key in keys:
        value = _valor_fila(row, key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def normalizar_item_catalogo(row, idioma="es"):
    if not isinstance(row, dict):
        return None
    textos = _textos_idioma_catalogo(row)
    desc_es = textos.get("es") or _texto_catalogo(row, "descripcion", "desc", "nombre", "name", "trabajo")
    if not desc_es and not any(textos.values()):
        return None
    idioma = str(idioma or "es").strip().lower()
    desc_en = textos.get("en") or ""
    desc_ui = (textos.get(idioma) or "").strip() or desc_es or desc_en or ""
    precio = round(_num_catalogo(
        row,
        "precio_base",
        "precio_unitario",
        "unit_price",
        "precio",
        "price",
        "rate",
        "costo",
        "precio_install",
        "precio_completo",
        "tarifa",
    ), 2)
    return {
        "codigo": _texto_catalogo(row, "codigo", "code", "sku", "id"),
        "categoria": _texto_catalogo(row, "categoria", "category", "rama", "familia", default="GENERAL").upper(),
        "descripcion": desc_ui,
        "descripcion_es": desc_es or "",
        "descripcion_en": desc_en,
        "descripciones": textos,
        "desc": desc_ui,
        "unidad": _texto_catalogo(row, "unidad", "unit", "unidad_medida", default="SF").upper(),
        "unit": _texto_catalogo(row, "unidad", "unit", "unidad_medida", default="SF").upper(),
        "precio_unitario": precio,
        "precio": precio,
        "price": precio,
        "precio_labor": round(_num_catalogo(row, "precio_labor", "labor", "precio_mano_obra", "labor_price"), 2),
        "precio_material": round(_num_catalogo(row, "precio_material", "material", "material_price", "precio_mat"), 2),
        "precio_demo": round(_num_catalogo(row, "precio_demo", "demo", "precio_demolicion", "tear_off"), 2),
        "priceLabor": round(_num_catalogo(row, "precio_labor", "labor", "precio_mano_obra", "labor_price"), 2),
        "priceMaterial": round(_num_catalogo(row, "precio_material", "material", "material_price", "precio_mat"), 2),
        "priceDemo": round(_num_catalogo(row, "precio_demo", "demo", "precio_demolicion", "tear_off"), 2),
        "fuente": "supabase",
    }


def _urlopen_json(req):
    import ssl
    import urllib.error
    import urllib.request

    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8") or "[]")
    except urllib.error.HTTPError as err:
        detalle = err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"{err.code} {detalle}") from err


def _buscar_catalogo_rest(q, limite):
    import urllib.parse
    import urllib.request

    url, key = _supabase_creds()
    if not url or not key:
        raise RuntimeError("Faltan SUPABASE_URL o SUPABASE_KEY en .env")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    tablas = ["catalogo_items"]
    last_error = None
    for tabla in tablas:
        params = {"select": "*", "limit": str(limite)}
        if q:
            like = f"*{q}*"
            compacto = _codigo_catalogo_compacto(q)
            filtro = f"(descripcion.ilike.{like},codigo.ilike.{like}"
            if compacto and compacto.lower() != str(q).lower():
                filtro += f",codigo.ilike.*{compacto}*"
            params["or"] = filtro + ")"
        query = urllib.parse.urlencode(params, safe="(),.*")
        req = urllib.request.Request(f"{url}/rest/v1/{tabla}?{query}", headers=headers)
        try:
            return _urlopen_json(req)
        except RuntimeError as err:
            last_error = err
            if q:
                params = {"select": "*", "limit": str(limite), "descripcion": f"ilike.{like}"}
                query = urllib.parse.urlencode(params, safe="(),.*")
                req = urllib.request.Request(f"{url}/rest/v1/{tabla}?{query}", headers=headers)
                try:
                    return _urlopen_json(req)
                except RuntimeError as err2:
                    last_error = err2
                    continue
            continue
    raise RuntimeError(str(last_error) if last_error else "No se encontró tabla de catálogo en Supabase")


def buscar_filas_catalogo(q):
    termino = _limpiar_termino_catalogo(q)
    limite = 50 if termino else 200
    client = _supabase_client()
    if client is not None:
        try:
            query = client.table(TABLA_CATALOGO).select("*")
            if termino:
                compacto = _codigo_catalogo_compacto(termino)
                filtro = f"descripcion.ilike.%{termino}%,codigo.ilike.%{termino}%"
                if compacto and compacto.lower() != termino.lower():
                    filtro += f",codigo.ilike.%{compacto}%"
                query = query.or_(filtro)
            data = query.limit(limite).execute()
            return list(data.data or [])
        except Exception:
            try:
                query = client.table(TABLA_CATALOGO).select("*").ilike("descripcion", f"%{termino}%") if termino else client.table(TABLA_CATALOGO).select("*")
                data = query.limit(limite).execute()
                return list(data.data or [])
            except Exception:
                pass
    return _buscar_catalogo_rest(termino, limite)


_CATALOGO_TECHO_CACHE = {}


def _tarifas_techo_desde_catalogo(zip_code=None):
    zip_digits = re.sub(r"\D", "", str(zip_code or ""))[:5]
    cache_key = zip_digits or "default"
    if cache_key in _CATALOGO_TECHO_CACHE:
        return dict(_CATALOGO_TECHO_CACHE[cache_key])
    reglas = (
        ("tear_off", r"tear.?off|remoci[oó]n de teja|tear out"),
        ("arch_shingle", r"teja arquitect|architectural\s*shingle|(?<!total\s)shingle"),
        ("steep", r"steep|pendiente pronunciada|pitch\s*>=?\s*8"),
        ("flat_sbs", r"flat\s*roof|sbs|techo plano|modificado"),
        ("starter", r"starter"),
        ("drip_edge", r"drip\s*edge"),
        ("ice_water", r"ice\s*(&|and)\s*water"),
        ("hips_ridges", r"ridge\s*cap|shingle\s*ridge|continuous\s*ridge\s*vent|cumbrera(\s+asf|\s+de\s+teja)?"),
        ("valley", r"valley|limahoya"),
        ("rakes", r"\brakes?\b"),
    )
    filas = []
    try:
        vistos = set()
        terminos = ("roof", "techo", "shingle", "tear-off", "teja", "rfg", "drip", "starter", "ridge")
        for termino in terminos:
            for row in buscar_filas_catalogo(termino) or []:
                if not isinstance(row, dict):
                    continue
                marca = tuple(sorted((str(k), str(v)) for k, v in list(row.items())[:8]))
                if marca in vistos:
                    continue
                vistos.add(marca)
                filas.append(row)
        for row in buscar_filas_catalogo("") or []:
            if not isinstance(row, dict):
                continue
            blob = " ".join(str(v) for v in row.values() if v is not None and not isinstance(v, (int, float, bool)))
            if not re.search(r"roof|techo|rfg|shingle|teja", blob, re.I):
                continue
            marca = tuple(sorted((str(k), str(v)) for k, v in list(row.items())[:8]))
            if marca in vistos:
                continue
            vistos.add(marca)
            filas.append(row)
    except Exception as err:
        print(f"catalogo techo: {err}")
        return {}

    def _score_region(row):
        zona = " ".join(
            str(_valor_fila(row, key) or "")
            for key in ("zip", "zip_code", "codigo_postal", "region", "zona", "estado", "state", "ciudad", "city")
        )
        zip_fila = re.sub(r"\D", "", zona)
        if zip_digits and zip_digits and zip_digits in zip_fila:
            return 3
        if zip_digits and zip_digits[:3] and zip_digits[:3] in zip_fila:
            return 2
        if zip_digits and zip_digits[:2] and zip_digits[:2] in zona:
            return 1
        return 0

    filas.sort(key=_score_region)
    mapa = {}
    for row in filas:
        desc = str(_valor_fila(row, "descripcion", "desc", "description", "nombre") or "")
        categoria = str(_valor_fila(row, "categoria", "category", "rama", "familia") or "")
        codigo = str(_valor_fila(row, "codigo", "code", "sku") or "")
        blob = f"{codigo} {desc} {categoria}"
        if es_medida_area_redundante(desc):
            continue
        if categoria and not re.search(r"TECH|ROOF|RFG|TECHO", categoria, re.I):
            continue
        if re.search(
            r"standing\s*seam|metal\s*roof|cubierta de metal|hojalat|sheet\s*metal|"
            r"calibre\s*2[0-6]|EXT-?ROF-?MET|\bMET-?(STN|SS|COP|ZNC|PNL|RDG)\b",
            blob,
            re.I,
        ):
            continue
        if not re.search(r"roof|techo|rfg|shingle|teja|tear|drip|starter|ridge|valley|ice|sbs|flat", blob, re.I):
            continue
        price = _num_catalogo(row, "precio_base", "precio_unitario", "precio", "price", "rate", "costo")
        if price <= 0:
            continue
        unit = str(_valor_fila(row, "unidad", "unit", "unidad_medida") or "SQ").upper().replace(" ", "")
        if unit in ("SF", "SQFT"):
            continue
        prioridad = _score_region(row)
        for key, patron in reglas:
            if not re.search(patron, blob, re.I):
                continue
            if key == "hips_ridges":
                if unit and unit != "LF":
                    continue
                if price > 22:
                    continue
                if not re.search(r"ridge\s*cap|shingle\s*ridge|continuous\s*ridge|cumbrera", blob, re.I):
                    continue
                prioridad += 8
            previo = mapa.get(key)
            if previo is None or prioridad >= previo[1]:
                mapa[key] = (round(float(price), 2), prioridad)
            break
    limpio = {key: valor[0] for key, valor in mapa.items()}
    if limpio:
        _CATALOGO_TECHO_CACHE[cache_key] = limpio
    return dict(limpio)


def _precios_ia_mercado_zip(termino, zona):
    if not termino:
        return []
    zip_txt = zona.get("zip") or ""
    ciudad = zona.get("ciudad") or ""
    estado = zona.get("estado") or ""
    prompt = f"""Eres un estimador de construcción residencial y comercial en Estados Unidos.
Usa SOLO descripciones de partidas estándar de la industria (oficio, alcance y unidad: SF, LF, SQ, EA, CY).
Ejemplos de redacción: "Instalación de teja asfáltica arquitectónica", "Remoción de drywall", "Pintura interior de muros".
NO uses nombres de software, plataformas, bases de datos ni marcas comerciales de estimación de terceros.

Para el código postal {zip_txt} ({ciudad}, {estado}), sugiere 1 a 5 partidas que coincidan con: "{termino}".
Los precios unitarios deben reflejar PROMEDIOS PÚBLICOS de mercado para esa región postal: costos típicos de materiales más mano de obra local (índices salariales y costo de vida de la zona), en USD.
No copies tarifas de un único proveedor ni de un programa propietario.

JSON array únicamente:
[{{"codigo":"","descripcion":"partida estándar de industria","unidad":"SF","precio_unitario":0.00}}]
"""
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = extraer_json(getattr(response, "text", None) or "")
    except Exception as err:
        print(f"--> precios IA ZIP {zip_txt}: {err}")
        return []
    filas = parsed if isinstance(parsed, list) else ((parsed or {}).get("items") if isinstance(parsed, dict) else [])
    items = []
    for row in filas or []:
        if not isinstance(row, dict):
            continue
        desc = str(row.get("descripcion") or row.get("description") or "").strip()
        if not desc:
            continue
        precio = _num_catalogo(row, "precio_unitario", "precio_base", "unit_price", "precio", "price")
        unidad = str(row.get("unidad") or row.get("unit") or "SF").upper()
        items.append({
            "codigo": str(row.get("codigo") or row.get("code") or "").strip(),
            "categoria": "MERCADO",
            "descripcion": desc,
            "desc": desc,
            "unidad": unidad,
            "unit": unidad,
            "precio_base": round(precio, 2),
            "precio_unitario": round(precio, 2),
            "precio": round(precio, 2),
            "price": round(precio, 2),
            "precio_labor": 0,
            "precio_material": 0,
            "precio_demo": 0,
            "priceLabor": 0,
            "priceMaterial": 0,
            "priceDemo": 0,
            "fuente": "promedio_regional",
            "ajuste_zip": False,
        })
    return items


_STOP_BUSQUEDA_CATALOGO = {
    "de", "la", "el", "the", "of", "and", "y", "a", "en", "con", "para", "un", "una",
    "del", "los", "las", "to", "for", "or", "con", "por", "que", "mas", "más",
}
_SINONIMOS_BUSQUEDA = {
    "remover": ("remover", "remove", "tear", "demo", "demolicion", "demolition", "arrancar", "tearoff"),
    "instalar": ("instalar", "install", "instalacion", "installation", "replace", "reemplazo", "poner"),
    "material": ("material", "materials", "suministro", "mat", "supply"),
    "eaves": ("eaves", "eave", "starter", "alero"),
    "valleys": ("valleys", "valley", "limahoya"),
    "hips": ("hips", "hip"),
    "ridges": ("ridges", "ridge cap", "shingle ridge", "cumbrera", "continuous ridge"),
    "rakes": ("rakes", "rake"),
    "squares": ("squares", "square", "escuadra", "teja", "shingle"),
    "flashing": ("flashing", "tapajuntas"),
    "ceramica": ("ceramica", "cerámica", "azulejo", "tile", "porcelanato"),
    "azulejo": ("azulejo", "ceramica", "cerámica", "tile", "porcelanato"),
}


def _tokens_busqueda_catalogo(q):
    crudos = re.findall(r"[a-zA-Z0-9áéíóúñÁÉÍÓÚÑ]{3,}", str(q or "").lower())
    tokens = []
    for t in crudos:
        if t in _STOP_BUSQUEDA_CATALOGO:
            continue
        tokens.append(t)
        for grupo in _SINONIMOS_BUSQUEDA.values():
            if t in grupo:
                tokens.extend(grupo)
    vistos = set()
    out = []
    for t in tokens:
        if t in vistos:
            continue
        vistos.add(t)
        out.append(t)
        if len(out) >= 12:
            break
    return out


def _sin_acentos(texto):
    tabla = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return str(texto or "").translate(tabla).lower()


def _texto_item_catalogo(row):
    if not isinstance(row, dict):
        return ""
    partes = [
        row.get("codigo"),
        row.get("code"),
        row.get("sku"),
        row.get("descripcion"),
        row.get("descripcion_es"),
        row.get("descripcion_en"),
        row.get("description_en"),
        row.get("descripcion_ingles"),
        row.get("desc"),
        row.get("description"),
        row.get("nombre"),
        row.get("categoria"),
        row.get("unidad"),
        row.get("unit"),
    ]
    return " ".join(str(p) for p in partes if p).lower()


def _puntaje_coincidencia_catalogo(row, q, tokens=None, unidad=None):
    blob = _texto_item_catalogo(row)
    if not blob:
        return 0
    qn = str(q or "").strip().lower()
    score = 0
    codigo = str(row.get("codigo") or row.get("code") or "").lower().replace(" ", "")
    desc = " ".join(
        str(row.get(k) or "")
        for k in (
            "descripcion",
            "descripcion_es",
            "descripcion_en",
            "description_en",
            "descripcion_ingles",
            "desc",
            "description",
        )
    ).lower()
    q_compact = re.sub(r"[^a-z0-9]", "", qn)
    if q_compact and codigo and q_compact == codigo.replace(" ", ""):
        score += 100
    elif q_compact and codigo and (q_compact in codigo.replace(" ", "") or codigo.replace(" ", "") in q_compact) and min(len(q_compact), len(codigo)) >= 3:
        score += 70
    if qn and desc and qn == desc:
        score += 95
    elif qn and desc and (qn in desc or desc in qn) and min(len(qn), len(desc)) >= 6:
        score += 45
    blob_n = _sin_acentos(blob)
    hits = 0
    for t in tokens or _tokens_busqueda_catalogo(q):
        if not t:
            continue
        if t in blob or _sin_acentos(t) in blob_n:
            score += 8
            hits += 1
    if hits >= 2:
        score += 16
    if unidad:
        u = str(row.get("unidad") or row.get("unit") or "").upper()
        if u and str(unidad).upper() in u:
            score += 12
    score += _puntaje_catalogo_zip(row, {"zip": "", "ciudad": "", "estado": ""}) if False else 0
    cat = str(row.get("categoria") or row.get("category") or row.get("rama") or row.get("familia") or "").upper()
    consulta_ridge = bool(re.search(r"ridge|cumbrera", qn))
    if consulta_ridge:
        if re.search(r"TECH|ROOF|RFG|TECHO", cat):
            score += 28
        elif cat:
            score -= 45
        if re.search(
            r"standing\s*seam|metal\s*roof|cubierta de metal|hojalat|sheet\s*metal|"
            r"calibre\s*2[0-6]|EXT-?ROF-?MET|\bMET-?(STN|SS|COP|ZNC|PNL|RDG)\b",
            blob,
            re.I,
        ):
            score -= 90
        if re.search(r"ridge\s*cap|shingle\s*ridge|continuous\s*ridge|cumbrera", blob, re.I):
            score += 32
        precio = _num_catalogo(row, "precio_base", "precio_unitario", "precio", "price", "rate")
        u = str(row.get("unidad") or row.get("unit") or "").upper()
        if "LF" in u and 8 <= precio <= 22:
            score += 24
        elif precio > 80:
            score -= 55
    return score


def _filtro_or_flexible(q):
    termino = _limpiar_termino_catalogo(q)
    if not termino:
        return ""
    partes = []
    vistos = set()

    def _add(campo, valor):
        limpio = re.sub(r"[,*%()]", "", str(valor or "")).strip()[:80]
        if not limpio or len(limpio) < 3:
            return
        clave = f"{campo}:{limpio.lower()}"
        if clave in vistos:
            return
        vistos.add(clave)
        partes.append(f"{campo}.ilike.%{limpio}%")

    _add("descripcion", termino)
    _add("descripcion_es", termino)
    _add("descripcion_en", termino)
    _add("description", termino)
    _add("description_en", termino)
    _add("codigo", termino)
    compacto = _codigo_catalogo_compacto(termino)
    if compacto and compacto.lower() != termino.lower():
        _add("codigo", compacto)
    for token in _tokens_busqueda_catalogo(termino):
        if token in _STOP_BUSQUEDA_CATALOGO:
            continue
        if len(token) < 4 and token not in ("tile", "sbs", "rfg", "lvt", "lvp"):
            continue
        _add("descripcion", token)
        _add("descripcion_es", token)
        _add("descripcion_en", token)
        _add("codigo", token)
    return ",".join(partes[:18])


def _puntaje_catalogo_zip(row, zona):
    zip_txt = zona.get("zip") or ""
    ciudad = str(zona.get("ciudad") or "").lower()
    estado = str(zona.get("estado") or "").upper()
    blob = " ".join(str(v) for v in row.values() if v is not None).lower()
    score = 0
    if zip_txt and zip_txt in blob:
        score += 12
    elif zip_txt and zip_txt[:3] in blob:
        score += 5
    if estado and re.search(rf"\b{re.escape(estado.lower())}\b", blob):
        score += 4
    if ciudad and ciudad in blob:
        score += 6
    return score


_SQL_ITEMS_FALTANTES = """
create table if not exists public.items_faltantes (
  id uuid primary key default gen_random_uuid(),
  termino text not null,
  termino_norm text not null unique,
  oficio text,
  zip_code text,
  veces integer not null default 1,
  fecha date not null default (now() at time zone 'utc')::date,
  primera_busqueda timestamptz not null default now(),
  ultima_busqueda timestamptz not null default now()
);
"""
_aviso_tabla_faltantes = False


def _termino_faltante_norm(q):
    crudo = _corregir_typos_catalogo(q)
    nucleo = _nucleo_busqueda_catalogo(crudo) or crudo
    return re.sub(r"\s+", " ", str(nucleo or "").strip().lower())[:160]


def _es_busqueda_faltante_valida(q):
    texto = str(q or "").strip()
    if len(texto) < 5:
        return False
    letras = re.sub(r"[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]", "", texto)
    if len(letras) < 4:
        return False
    if re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{1,3}", texto):
        return False
    return True


def registrar_item_faltante(termino, oficio="", zip_code=""):
    """Guarda o actualiza un término sin coincidencias en items_faltantes."""
    global _aviso_tabla_faltantes
    texto = str(termino or "").strip()
    if not _es_busqueda_faltante_valida(texto):
        return False
    if supabase is None:
        return False
    norm = _termino_faltante_norm(texto)
    if len(norm) < 3:
        return False
    ahora = datetime.now(timezone.utc)
    payload = {
        "termino": texto[:180],
        "termino_norm": norm,
        "oficio": (oficio or None),
        "zip_code": (str(zip_code or "").strip()[:10] or None),
        "fecha": ahora.date().isoformat(),
        "ultima_busqueda": ahora.isoformat(),
    }
    try:
        existente = (
            supabase.table(TABLA_FALTANTES)
            .select("id,veces")
            .eq("termino_norm", norm)
            .limit(1)
            .execute()
        )
        filas = list(existente.data or [])
        if filas:
            row = filas[0]
            supabase.table(TABLA_FALTANTES).update({
                "veces": int(row.get("veces") or 1) + 1,
                "termino": payload["termino"],
                "oficio": payload["oficio"],
                "zip_code": payload["zip_code"],
                "fecha": payload["fecha"],
                "ultima_busqueda": payload["ultima_busqueda"],
            }).eq("id", row["id"]).execute()
        else:
            payload["veces"] = 1
            payload["primera_busqueda"] = ahora.isoformat()
            supabase.table(TABLA_FALTANTES).insert(payload).execute()
        print(f"--> item faltante registrado: '{norm}'")
        return True
    except Exception as err:
        detalle = str(err)
        if (not _aviso_tabla_faltantes) and re.search(r"items_faltantes|PGRST205|does not exist|schema cache", detalle, re.I):
            _aviso_tabla_faltantes = True
            print("--> Falta crear la tabla items_faltantes en Supabase. SQL:")
            print(_SQL_ITEMS_FALTANTES)
        else:
            print(f"ERROR items_faltantes: {err}")
        return False


@app.route("/api/items-faltantes", methods=["GET", "POST"])
def api_items_faltantes():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        termino = str(payload.get("termino") or payload.get("q") or request.args.get("q") or "").strip()
        oficio = str(payload.get("oficio") or "").strip()
        zip_code = str(payload.get("zip") or payload.get("zip_code") or "").strip()
        ok = registrar_item_faltante(termino, oficio, zip_code)
        return jsonify({"ok": ok, "termino": termino})
    if supabase is None:
        return jsonify({"error": "Supabase no configurado"}), 500
    try:
        res = (
            supabase.table(TABLA_FALTANTES)
            .select("*")
            .order("ultima_busqueda", desc=True)
            .limit(200)
            .execute()
        )
        return jsonify(list(res.data or []))
    except Exception as err:
        return jsonify({"error": str(err)}), 500


_SQL_HISTORIAL_ESTIMADOS = """
create table if not exists public.historial_estimados (
  id uuid primary key default gen_random_uuid(),
  folio text,
  tipo text not null default 'estimate',
  cliente_nombre text,
  fecha date,
  total numeric,
  estado text default 'pending',
  cliente jsonb,
  items jsonb,
  areas jsonb,
  snapshot jsonb,
  notas text,
  terminos text,
  firmas jsonb,
  payload jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
"""
_aviso_tabla_historial = False


def _es_uuid(valor):
    return bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        str(valor or "").strip(),
    ))


def _json_campo(valor, fallback=None):
    if valor is None or valor == "":
        return fallback
    if isinstance(valor, (dict, list)):
        return valor
    if isinstance(valor, str):
        try:
            parsed = json.loads(valor)
            return parsed
        except Exception:
            return fallback if fallback is not None else valor
    return valor


def _aviso_historial_faltante(err):
    global _aviso_tabla_historial
    detalle = str(err or "")
    if (not _aviso_tabla_historial) and re.search(
        r"historial_estimados|PGRST205|does not exist|schema cache", detalle, re.I
    ):
        _aviso_tabla_historial = True
        print("--> Falta crear la tabla historial_estimados en Supabase. SQL:")
        print(_SQL_HISTORIAL_ESTIMADOS)
    else:
        print(f"ERROR historial_estimados: {err}")


def _formatear_direccion_cliente(cliente, payload=None, snapshot=None):
    payload = payload if isinstance(payload, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snap_client = snapshot.get("client") if isinstance(snapshot.get("client"), dict) else {}
    cliente = cliente if isinstance(cliente, dict) else {}
    street = str(
        cliente.get("street")
        or cliente.get("direccion")
        or snap_client.get("street")
        or payload.get("address")
        or payload.get("direccion")
        or ""
    ).strip()
    city = str(cliente.get("city") or snap_client.get("city") or "").strip()
    state = str(cliente.get("state") or snap_client.get("state") or "").strip()
    zipc = str(cliente.get("zip") or cliente.get("zip_code") or snap_client.get("zip") or "").strip()
    localidad = " ".join(p for p in (city, state, zipc) if p)
    return ", ".join(p for p in (street, localidad) if p)


def documento_desde_fila_historial(row):
    if not isinstance(row, dict):
        return {}
    payload = _json_campo(row.get("payload"), {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    cliente = _json_campo(row.get("cliente")) or payload.get("cliente") or payload.get("client")
    items = _json_campo(row.get("items")) or payload.get("items") or []
    areas = _json_campo(row.get("areas")) or payload.get("areas") or []
    snapshot = _json_campo(row.get("snapshot")) or payload.get("snapshot") or {}
    firmas = _json_campo(row.get("firmas")) or payload.get("firmas") or {}
    tipo = str(row.get("tipo") or payload.get("tipo") or payload.get("type") or payload.get("docMode") or "estimate")
    folio = row.get("folio") or payload.get("folio") or payload.get("number") or payload.get("quote") or payload.get("invoice")
    notas = row.get("notas") or payload.get("notas") or payload.get("projectNotes") or (snapshot.get("projectNotes") if isinstance(snapshot, dict) else "")
    terminos = row.get("terminos") or payload.get("terminos") or payload.get("terms") or (snapshot.get("terms") if isinstance(snapshot, dict) else "")
    cliente_nombre = row.get("cliente_nombre") or payload.get("clienteNombre") or payload.get("client")
    if isinstance(cliente, dict):
        cliente_nombre = cliente_nombre or cliente.get("name")
    elif isinstance(cliente, str) and not cliente_nombre:
        cliente_nombre = cliente
    direccion = _formatear_direccion_cliente(cliente if isinstance(cliente, dict) else {}, payload, snapshot if isinstance(snapshot, dict) else {})
    return {
        "id": row.get("id"),
        "folio": folio,
        "number": folio,
        "invoice": folio,
        "quote": folio,
        "documentNo": folio,
        "address": direccion,
        "direccion": direccion,
        "type": tipo,
        "tipo": tipo,
        "mode": tipo,
        "modo": tipo,
        "docMode": tipo,
        "client": cliente_nombre,
        "clienteNombre": cliente_nombre,
        "cliente": cliente if isinstance(cliente, dict) else {"name": cliente_nombre or ""},
        "date": row.get("fecha") or payload.get("date") or payload.get("fecha"),
        "fecha": row.get("fecha") or payload.get("fecha"),
        "total": row.get("total") if row.get("total") is not None else payload.get("total"),
        "status": row.get("estado") or payload.get("status") or "pending",
        "items": items if isinstance(items, list) else [],
        "areas": areas if isinstance(areas, list) else [],
        "snapshot": snapshot if isinstance(snapshot, dict) else {},
        "firmas": firmas if isinstance(firmas, dict) else {},
        "projectNotes": notas or "",
        "notas": notas or "",
        "terms": terminos or "",
        "terminos": terminos or "",
        "payload": payload,
        "contratista_id": row.get("contratista_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _fila_historial_desde_body(body):
    data = body if isinstance(body, dict) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
    snap_client = snapshot.get("client") if isinstance(snapshot.get("client"), dict) else {}
    cliente = data.get("cliente") if isinstance(data.get("cliente"), dict) else snap_client
    if not isinstance(cliente, dict):
        cliente = {"name": str(data.get("client") or data.get("clienteNombre") or "")}
    for clave in ("name", "email", "phone", "street", "city", "state", "zip"):
        if snap_client.get(clave) and not cliente.get(clave):
            cliente[clave] = snap_client.get(clave)
    for clave, origen in (
        ("street", data.get("street") or data.get("direccion") or data.get("address")),
        ("city", data.get("city")),
        ("state", data.get("state")),
        ("zip", data.get("zip") or data.get("zip_code")),
    ):
        if origen and not cliente.get(clave):
            cliente[clave] = str(origen).strip()
    folio = str(
        data.get("folio")
        or data.get("documentNo")
        or data.get("number")
        or data.get("quote")
        or data.get("invoice")
        or snapshot.get("quote")
        or ""
    ).strip()
    tipo = str(data.get("tipo") or data.get("type") or data.get("docMode") or data.get("mode") or snapshot.get("docMode") or "estimate").strip().lower()
    fecha = data.get("date") or data.get("fecha") or snapshot.get("date") or ""
    if fecha:
        fecha = str(fecha)[:10]
    else:
        fecha = None
    notas = data.get("projectNotes") or data.get("notas") or snapshot.get("projectNotes") or ""
    terminos = data.get("terms") or data.get("terminos") or snapshot.get("terms") or ""
    total = data.get("total")
    try:
        total = float(total) if total is not None and total != "" else None
    except (TypeError, ValueError):
        total = None
    payload = dict(data)
    ident = data.get("id")
    fila = {
        "folio": folio or None,
        "tipo": tipo or "estimate",
        "cliente_nombre": (cliente.get("name") if isinstance(cliente, dict) else None) or data.get("clienteNombre") or data.get("client") or None,
        "fecha": fecha,
        "total": total,
        "estado": data.get("status") or data.get("estado") or "pending",
        "cliente": cliente,
        "items": data.get("items") or [],
        "areas": data.get("areas") or snapshot.get("areas") or [],
        "snapshot": snapshot or None,
        "notas": notas or None,
        "terminos": terminos or None,
        "firmas": data.get("firmas") or snapshot.get("firmas") or {},
        "payload": payload,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if _es_uuid(ident):
        fila["id"] = str(ident).strip()
    return fila


def _token_contratista_solicitud():
    auth = str(request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.headers.get("X-Contratista-Token") or "").strip()


def _error_sin_funcion_tenant(err):
    return bool(re.search(r"PGRST202|Could not find the function", str(err or ""), re.I))


def _desenvolver_rpc(data):
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], str):
        try:
            return json.loads(data[0])
        except Exception:
            return data
    return data


def _dict_rpc(data):
    data = _desenvolver_rpc(data)
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict):
            return data[0]
        return None
    return data if isinstance(data, dict) else None


def _lista_rpc(data):
    data = _desenvolver_rpc(data)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], list):
        data = data[0]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _rpc_tenant(sb, nombre, params):
    res = sb.rpc(nombre, params).execute()
    return _desenvolver_rpc(res.data)


def _aviso_multitenant(err=None):
    global _aviso_tenant_hecho
    if not _aviso_tenant_hecho:
        _aviso_tenant_hecho = True
        print("-->", _SQL_MULTITENANT)
    if err:
        print("ERROR tenant:", str(err).encode("ascii", "replace").decode())


_aviso_tenant_hecho = False


def _contratista_publico(row):
    if not isinstance(row, dict) or not row.get("id"):
        return None
    return {
        "id": row.get("id"),
        "email": row.get("email"),
        "nombre_empresa": row.get("nombre_empresa") or "",
    }


def _registrar_contratista_tabla(sb, email, nombre):
    email = str(email or "").strip().lower()
    nombre = str(nombre or "").strip()
    previos = sb.table(TABLA_CONTRATISTAS).select("id").limit(1).execute()
    es_primero = not list(previos.data or [])
    existente = (
        sb.table(TABLA_CONTRATISTAS)
        .select("id")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    if list(existente.data or []):
        raise RuntimeError("already_registered")
    token = secrets.token_hex(32)
    res = (
        sb.table(TABLA_CONTRATISTAS)
        .insert({"nombre_empresa": nombre, "email": email, "token": token})
        .execute()
    )
    rows = list(res.data or [])
    if not rows:
        raise RuntimeError("No se pudo registrar el contratista")
    row = rows[0]
    if es_primero and row.get("id"):
        try:
            sb.table(TABLA_HISTORIAL).update({"contratista_id": row["id"]}).is_("contratista_id", "null").execute()
        except Exception as err:
            print(f"--> no se pudieron asignar documentos previos: {err}")
    row["token"] = token
    return row


def _contratista_por_token_tabla(sb, token):
    res = (
        sb.table(TABLA_CONTRATISTAS)
        .select("id,email,nombre_empresa")
        .eq("token", token)
        .limit(1)
        .execute()
    )
    rows = list(res.data or [])
    return rows[0] if rows else None


def _actualizar_contratista_tabla(sb, token, email, nombre):
    cambios = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if email:
        cambios["email"] = str(email).strip().lower()
    if nombre:
        cambios["nombre_empresa"] = str(nombre).strip()
    res = (
        sb.table(TABLA_CONTRATISTAS)
        .update(cambios)
        .eq("token", token)
        .execute()
    )
    rows = list(res.data or [])
    if rows:
        return _contratista_publico(rows[0])
    return _contratista_por_token_tabla(sb, token)


def registrar_contratista(email, nombre):
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    try:
        data = _rpc_tenant(sb, "registrar_contratista", {"p_email": email, "p_nombre": nombre})
        row = _dict_rpc(data)
        if row:
            return row
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
        _aviso_multitenant(err)
    return _registrar_contratista_tabla(sb, email, nombre)


def contratista_por_token(token):
    token = str(token or "").strip()
    if not token:
        return None
    sb = _supabase_client()
    if sb is None:
        return None
    try:
        data = _rpc_tenant(sb, "contratista_por_token", {"p_token": token})
        return _contratista_publico(_dict_rpc(data))
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
    return _contratista_publico(_contratista_por_token_tabla(sb, token))


def actualizar_contratista(token, email=None, nombre=None):
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    try:
        data = _rpc_tenant(sb, "actualizar_contratista", {
            "p_token": token,
            "p_email": email or "",
            "p_nombre": nombre or "",
        })
        row = _contratista_publico(_dict_rpc(data))
        if row:
            return row
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
    return _actualizar_contratista_tabla(sb, token, email, nombre)


def _exigir_contratista():
    token = _token_contratista_solicitud()
    if not token:
        return None, (jsonify({"error": "Contractor session required"}), 401)
    try:
        contratista = contratista_por_token(token)
    except Exception as err:
        _aviso_multitenant(err)
        return None, (jsonify({"error": _SQL_MULTITENANT}), 503)
    if not contratista or not _correo_valido(contratista.get("email")):
        return None, (jsonify({"error": "Contractor not authorized"}), 403)
    contratista["token"] = token
    return contratista, None


def _buscar_historial_por_id(sb, ident, contratista_id=None):
    ident = str(ident or "").strip()
    if not ident:
        return None
    if _es_uuid(ident):
        try:
            query = sb.table(TABLA_HISTORIAL).select("*").eq("id", ident)
            if contratista_id:
                query = query.eq("contratista_id", contratista_id)
            res = query.limit(1).execute()
            rows = list(res.data or [])
            if rows:
                return rows[0]
        except Exception as err:
            _aviso_historial_faltante(err)
            return None
    for campo in ("folio", "id"):
        try:
            query = sb.table(TABLA_HISTORIAL).select("*").eq(campo, ident)
            if contratista_id:
                query = query.eq("contratista_id", contratista_id)
            res = query.limit(1).execute()
            rows = list(res.data or [])
            if rows:
                return rows[0]
        except Exception as err:
            _aviso_historial_faltante(err)
            break
    return None


def _fila_tenant_json(fila):
    limpia = {}
    for clave, valor in (fila or {}).items():
        if clave == "contratista_id":
            continue
        if valor is None or isinstance(valor, (str, int, float, bool, dict, list)):
            limpia[clave] = valor
        else:
            limpia[clave] = str(valor)
    return limpia


def _guardar_historial_tabla(sb, contratista, fila):
    cid = contratista["id"]
    fila = dict(fila)
    fila["contratista_id"] = cid
    fila.pop("id", None) if not _es_uuid(fila.get("id")) else None
    tipo = str(fila.get("tipo") or "").lower()
    existente = None
    if _es_uuid(fila.get("id")):
        existente = _buscar_historial_por_id(sb, fila["id"], cid)
    if not existente and fila.get("folio"):
        existente = _buscar_historial_por_id(sb, fila["folio"], cid)
    if tipo in ("contract", "contrato"):
        token_actual = (existente or {}).get("token_aceptacion")
        fila["token_aceptacion"] = token_actual or str(uuid.uuid4())
    elif existente and existente.get("token_aceptacion"):
        fila["token_aceptacion"] = existente.get("token_aceptacion")
    if existente and str(existente.get("estado") or "").lower() == "accepted":
        fila["estado"] = "accepted"
    if existente:
        ident = existente.get("id")
        update = {k: v for k, v in fila.items() if k != "id"}
        sb.table(TABLA_HISTORIAL).update(update).eq("id", ident).eq("contratista_id", cid).execute()
        return _buscar_historial_por_id(sb, ident, cid) or {**existente, **update, "id": ident}
    insert = {k: v for k, v in fila.items() if k != "id"}
    res = sb.table(TABLA_HISTORIAL).insert(insert).execute()
    rows = list(res.data or [])
    return rows[0] if rows else insert


def guardar_historial_tenant(contratista, fila):
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    payload = _fila_tenant_json(fila)
    try:
        data = _rpc_tenant(sb, "guardar_historial_tenant", {
            "p_token": contratista["token"],
            "p_fila": payload,
        })
        row = _dict_rpc(data)
        if row:
            return row
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
        _aviso_multitenant(err)
    return _guardar_historial_tabla(sb, contratista, payload)


def listar_historial_tenant(contratista):
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    try:
        data = _rpc_tenant(sb, "listar_historial_tenant", {"p_token": contratista["token"]})
        return _lista_rpc(data)
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
    res = (
        sb.table(TABLA_HISTORIAL)
        .select("id,folio,tipo,cliente_nombre,fecha,total,estado,updated_at,cliente,snapshot,contratista_id")
        .eq("contratista_id", contratista["id"])
        .order("updated_at", desc=True)
        .limit(100)
        .execute()
    )
    return list(res.data or [])


def obtener_historial_tenant(contratista, ident):
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    try:
        data = _rpc_tenant(sb, "obtener_historial_tenant", {
            "p_token": contratista["token"],
            "p_ident": str(ident or ""),
        })
        return _dict_rpc(data)
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
    return _buscar_historial_por_id(sb, ident, contratista["id"])


def leer_contrato_publico(token_aceptacion):
    if not _es_uuid(token_aceptacion):
        return None
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    try:
        data = _rpc_tenant(sb, "leer_contrato_publico", {"p_token": str(token_aceptacion)})
        return _dict_rpc(data)
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
    res = (
        sb.table(TABLA_HISTORIAL)
        .select("*")
        .eq("token_aceptacion", str(token_aceptacion))
        .limit(1)
        .execute()
    )
    rows = list(res.data or [])
    if not rows:
        return None
    doc = rows[0]
    if str(doc.get("tipo") or "").lower() not in ("contract", "contrato"):
        return None
    contratista = None
    if doc.get("contratista_id"):
        cres = (
            sb.table(TABLA_CONTRATISTAS)
            .select("id,email,nombre_empresa")
            .eq("id", doc["contratista_id"])
            .limit(1)
            .execute()
        )
        crows = list(cres.data or [])
        contratista = crows[0] if crows else None
    cliente = doc.get("cliente") if isinstance(doc.get("cliente"), dict) else {}
    return {
        "id": doc.get("id"),
        "folio": doc.get("folio"),
        "tipo": doc.get("tipo"),
        "cliente_nombre": doc.get("cliente_nombre"),
        "total": doc.get("total"),
        "estado": doc.get("estado"),
        "fecha": doc.get("fecha"),
        "cliente_email": cliente.get("email") or "",
        "contratista_id": doc.get("contratista_id"),
        "contratista_email": (contratista or {}).get("email") or "",
        "nombre_empresa": (contratista or {}).get("nombre_empresa") or "",
    }


def marcar_contrato_aceptado(token_aceptacion):
    if not _es_uuid(token_aceptacion):
        return None
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    try:
        data = _rpc_tenant(sb, "marcar_contrato_aceptado", {"p_token": str(token_aceptacion)})
        return _dict_rpc(data)
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            raise
    actual = leer_contrato_publico(token_aceptacion)
    if not actual:
        return None
    already = str(actual.get("estado") or "").lower() == "accepted"
    if not already:
        sb.table(TABLA_HISTORIAL).update({
            "estado": "accepted",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", actual["id"]).eq("token_aceptacion", str(token_aceptacion)).execute()
    actual["already"] = already
    actual["estado"] = "accepted"
    return actual


def revertir_aceptacion_contrato(token_aceptacion, estado_anterior):
    if not _es_uuid(token_aceptacion):
        return False
    estado = str(estado_anterior or "pending").strip().lower()
    if estado not in ("pending", "open", "sent"):
        estado = "pending"
    sb = _supabase_client()
    if sb is None:
        return False
    try:
        _rpc_tenant(sb, "revertir_aceptacion_contrato", {
            "p_token": str(token_aceptacion),
            "p_estado": estado,
        })
        return True
    except Exception as err:
        if not _error_sin_funcion_tenant(err):
            print(f"--> revertir aceptacion: {err}")
            return False
    try:
        sb.table(TABLA_HISTORIAL).update({
            "estado": estado,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("token_aceptacion", str(token_aceptacion)).eq("estado", "accepted").execute()
        return True
    except Exception as err:
        print(f"--> revertir aceptacion: {err}")
        return False


def _respuesta_tenant(err):
    texto = str(err or "")
    if "already_registered" in texto:
        return jsonify({"error": "This contractor email is already registered in another session."}), 409
    if "unauthorized" in texto:
        return jsonify({"error": "Contractor not authorized"}), 403
    if "invalid_contractor" in texto:
        return jsonify({"error": "Company name and a valid contractor email are required."}), 400
    if re.search(r"duplicate key|contratistas_email", texto, re.I):
        return jsonify({"error": "That contractor email is already in use."}), 409
    if re.search(r"contratistas|contratista_id|token_aceptacion|PGRST204|PGRST205|does not exist", texto, re.I):
        _aviso_multitenant(err)
        return jsonify({"error": _SQL_MULTITENANT}), 503
    _aviso_historial_faltante(err)
    return jsonify({"error": texto}), 500


@app.route("/api/contratistas", methods=["POST", "PATCH"])
def api_contratistas():
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        email = str(body.get("email") or "").strip().lower()
        nombre = str(body.get("nombre_empresa") or body.get("company") or "").strip()
        if not _correo_valido(email) or not nombre:
            return jsonify({"error": "Company name and a valid contractor email are required."}), 400
        try:
            row = registrar_contratista(email, nombre)
        except Exception as err:
            return _respuesta_tenant(err)
        if not row or not row.get("token"):
            return jsonify({"error": _SQL_MULTITENANT}), 503
        print(f"--> contratista registrado id={row.get('id')}")
        return jsonify({
            "id": row.get("id"),
            "email": row.get("email"),
            "nombre_empresa": row.get("nombre_empresa"),
            "token": row.get("token"),
        })
    contratista, error = _exigir_contratista()
    if error:
        return error
    email = str(body.get("email") or body.get("contact") or "").strip().lower()
    nombre = str(body.get("nombre_empresa") or body.get("company") or "").strip()
    if email and not _correo_valido(email):
        return jsonify({"error": "Contractor email is not valid."}), 400
    try:
        row = actualizar_contratista(contratista["token"], email or None, nombre or None)
    except Exception as err:
        return _respuesta_tenant(err)
    if not row:
        return jsonify({"error": "Contractor not authorized"}), 403
    return jsonify(row)


@app.route("/api/contratistas/yo", methods=["GET"])
def api_contratista_actual():
    contratista, error = _exigir_contratista()
    if error:
        return error
    return jsonify({
        "id": contratista.get("id"),
        "email": contratista.get("email"),
        "nombre_empresa": contratista.get("nombre_empresa"),
    })


@app.route("/api/historial", methods=["GET", "POST"])
def api_historial():
    sb = _supabase_client()
    if sb is None:
        return jsonify({"error": "Supabase no configurado", "items": []}), 503
    contratista, error = _exigir_contratista()
    if error:
        return error
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        fila = _fila_historial_desde_body(body)
        try:
            guardado = guardar_historial_tenant(contratista, fila)
            if not isinstance(guardado, dict) or not guardado.get("id"):
                return jsonify({"error": "Could not save document"}), 500
            print(f"--> historial guardado id={guardado.get('id')} folio={guardado.get('folio')} contratista={contratista.get('id')}")
            return jsonify(documento_desde_fila_historial(guardado))
        except Exception as err:
            return _respuesta_tenant(err)
    try:
        docs = [documento_desde_fila_historial(row) for row in listar_historial_tenant(contratista)]
        return jsonify({"items": docs})
    except Exception as err:
        respuesta = _respuesta_tenant(err)
        if isinstance(respuesta, tuple):
            cuerpo, codigo = respuesta
            data = cuerpo.get_json(silent=True) or {"error": "error"}
            data["items"] = []
            return jsonify(data), codigo
        return respuesta


@app.route("/api/historial/<path:doc_id>", methods=["GET"])
def api_historial_documento(doc_id):
    ident = str(doc_id or "").strip()
    if not ident:
        return jsonify({"error": "not found"}), 404
    if _supabase_client() is None:
        return jsonify({"error": "Supabase no configurado"}), 503
    contratista, error = _exigir_contratista()
    if error:
        return error
    try:
        row = obtener_historial_tenant(contratista, ident)
    except Exception as err:
        return _respuesta_tenant(err)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(documento_desde_fila_historial(row))


PROMPT_SISTEMA_TRADUCCION = (
    "Eres un experto contratista bilingüe operando en Georgia. "
    "Traduce esta línea de presupuesto de construcción al inglés técnico profesional. "
    "Traduce absolutamente todos los términos y materiales "
    "(por ejemplo, Fascia Madera debe ser Wood Fascia), "
    "no dejes palabras en español asumiendo que son nombres propios."
)


def _traducir_lote_claude(pendientes):
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY")
    import httpx
    from claude_pdf import MODELOS_CLAUDE, _texto_respuesta

    user = (
        "Traduce cada línea al inglés técnico de construcción en Georgia. "
        "Devuelve JSON únicamente con la misma cantidad y orden: "
        '{"translations":["..."]}. '
        "No dejes ningún término en español.\n\n"
        + json.dumps(pendientes, ensure_ascii=False)
    )
    ultimo = None
    with httpx.Client(verify=False, timeout=90.0) as http:
        for modelo in MODELOS_CLAUDE:
            try:
                resp = http.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": modelo,
                        "max_tokens": 4096,
                        "system": PROMPT_SISTEMA_TRADUCCION,
                        "messages": [{"role": "user", "content": user}],
                    },
                )
                if resp.status_code >= 400:
                    ultimo = RuntimeError(f"Claude HTTP {resp.status_code}: {resp.text[:400]}")
                    print(f"--> traducir Claude {modelo}: {ultimo}")
                    continue
                parsed = extraer_json(_texto_respuesta(resp.json()) or "") or {}
                trads = parsed.get("translations") or parsed.get("traducciones") or []
                if isinstance(trads, list) and trads:
                    print(f"--> traducción IA con Claude {modelo}")
                    return trads, "claude"
            except Exception as err:
                ultimo = err
                print(f"--> traducir Claude {modelo}: {err}")
    raise ultimo or RuntimeError("Claude no tradujo")


def _traducir_lote_gemini(pendientes):
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("Falta GEMINI_API_KEY")
    ia = genai.Client(api_key=key)
    user = (
        "Traduce cada línea al inglés técnico de construcción en Georgia. "
        "Devuelve JSON únicamente con la misma cantidad y orden: "
        '{"translations":["..."]}. '
        "No dejes ningún término en español.\n\n"
        + json.dumps(pendientes, ensure_ascii=False)
    )
    ultimo = None
    for modelo in ("gemini-2.5-flash", "gemini-flash-latest", "gemini-3.6-flash"):
        try:
            response = ia.models.generate_content(
                model=modelo,
                contents=user,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    system_instruction=PROMPT_SISTEMA_TRADUCCION,
                ),
            )
            parsed = extraer_json(getattr(response, "text", None) or "") or {}
            trads = parsed.get("translations") or parsed.get("traducciones") or []
            if isinstance(trads, list) and trads:
                print(f"--> traducción IA con Gemini {modelo}")
                return trads, "gemini"
        except Exception as err:
            ultimo = err
            texto_err = str(err).lower()
            print(f"--> traducir Gemini {modelo}: {err}")
            if any(token in texto_err for token in ("503", "unavailable", "high demand", "overloaded", "429", "404", "not_found", "not found")):
                continue
            raise
    raise ultimo or RuntimeError("Gemini no tradujo")


def traducir_textos_a_ingles(textos, forzar=False):
    limpios = [str(t or "").strip() for t in (textos or [])]
    if not any(limpios):
        return limpios, "vacio"
    pendientes = []
    indices = []
    resultado = list(limpios)
    for i, texto in enumerate(limpios):
        if texto and (forzar or not _parece_ingles_doc(texto)):
            pendientes.append(texto)
            indices.append(i)
    if not pendientes:
        return resultado, "ya_ingles"
    parsed_trads = []
    fuente = "ia"
    ultimo = None
    if (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        try:
            parsed_trads, fuente = _traducir_lote_claude(pendientes)
        except Exception as err:
            ultimo = err
            print(f"--> traducir_textos_a_ingles Claude: {err}")
    if not parsed_trads:
        try:
            parsed_trads, fuente = _traducir_lote_gemini(pendientes)
        except Exception as err:
            ultimo = err
            print(f"--> traducir_textos_a_ingles Gemini: {err}")
    if not parsed_trads:
        raise RuntimeError(ultimo or "No hay API de IA disponible para traducir")
    trads = list(parsed_trads) if isinstance(parsed_trads, list) else []
    while len(trads) < len(pendientes):
        trads.append(pendientes[len(trads)])
    for pos, idx in enumerate(indices):
        traducido = trads[pos] if pos < len(trads) else resultado[idx]
        resultado[idx] = str(traducido or resultado[idx]).strip() or resultado[idx]
    return resultado, fuente


def _filas_catalogo_por_codigos(codigos):
    sb = _supabase_client()
    if sb is None or not codigos:
        return {}
    limpios = []
    vistos = set()
    for codigo in codigos:
        c = str(codigo or "").strip()
        if not c or c in vistos:
            continue
        vistos.add(c)
        limpios.append(c)
    mapa = {}
    for i in range(0, len(limpios), 80):
        lote = limpios[i:i + 80]
        try:
            res = sb.table(TABLA_CATALOGO).select("*").in_("codigo", lote).execute()
            for row in res.data or []:
                clave = str(row.get("codigo") or "").strip()
                if clave:
                    mapa[clave] = row
                    mapa[clave.upper()] = row
        except Exception as err:
            print(f"--> catalogo por codigo: {err}")
    return mapa


@app.route("/api/traducir-descripciones", methods=["POST"])
def traducir_descripciones():
    payload = request.get_json(silent=True) or {}
    textos = payload.get("texts") or payload.get("textos") or []
    if not isinstance(textos, list):
        textos = [textos]
    trads, fuente = traducir_textos_a_ingles(textos)
    return jsonify({"translations": trads, "fuente": fuente})


@app.route("/api/descripciones-idioma", methods=["POST"])
def descripciones_idioma():
    payload = request.get_json(silent=True) or {}
    idioma = str(payload.get("idioma") or payload.get("lang") or "es").strip().lower()
    if idioma not in IDIOMAS_CATALOGO:
        idioma = "es"
    items_in = payload.get("items") or payload.get("codigos") or []
    if not isinstance(items_in, list):
        items_in = []
    codigos = []
    for it in items_in:
        if isinstance(it, dict):
            codigos.append(it.get("codigo") or it.get("code") or "")
        else:
            codigos.append(it)
    mapa = _filas_catalogo_por_codigos(codigos)
    salida = []
    pendientes = []
    idx_pendientes = []
    for codigo in codigos:
        c = str(codigo or "").strip()
        row = mapa.get(c) or mapa.get(c.upper())
        textos = _textos_idioma_catalogo(row) if row else {"es": "", "en": ""}
        if not textos.get("es") and row:
            textos["es"] = str(row.get("descripcion") or row.get("desc") or "").strip()
        desc = textos.get(idioma) or ""
        if idioma == "en" and not desc and textos.get("es"):
            pendientes.append(textos["es"])
            idx_pendientes.append(len(salida))
        salida.append({
            "codigo": c,
            "descripcion": desc,
            "descripcion_es": textos.get("es") or "",
            "descripcion_en": textos.get("en") or "",
            "desc": desc,
            "descripciones": textos,
            "encontrado": bool(row),
            "idioma": idioma,
        })
    if pendientes:
        trads, _fuente = traducir_textos_a_ingles(pendientes, forzar=True)
        for pos, idx in enumerate(idx_pendientes):
            en = str(trads[pos] if pos < len(trads) else "").strip()
            if not en:
                continue
            salida[idx]["descripcion"] = en
            salida[idx]["descripcion_en"] = en
            salida[idx]["desc"] = en
            textos = dict(salida[idx].get("descripciones") or {})
            textos["en"] = en
            salida[idx]["descripciones"] = textos
    return jsonify({"idioma": idioma, "items": salida})


_ESTADOS_USA = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def _codigo_estado_usa(valor):
    texto = str(valor or "").strip()
    if not texto:
        return ""
    if "-" in texto:
        texto = texto.split("-")[-1].strip()
    if len(texto) == 2 and texto.isalpha():
        return texto.upper()
    return _ESTADOS_USA.get(texto.lower(), "")


def _http_json_get(url, headers=None, timeout=8):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _calle_desde_partes(partes):
    partes = partes if isinstance(partes, dict) else {}
    numero = str(partes.get("house_number") or partes.get("housenumber") or "").strip()
    via = str(
        partes.get("road")
        or partes.get("street")
        or partes.get("pedestrian")
        or partes.get("residential")
        or partes.get("name")
        or ""
    ).strip()
    if numero and via and not via.lower().startswith(numero.lower()):
        return f"{numero} {via}"
    return via or numero


def _ciudad_desde_partes(partes):
    partes = partes if isinstance(partes, dict) else {}
    for clave in ("city", "town", "village", "hamlet", "municipality", "city_district", "locality", "county"):
        valor = str(partes.get(clave) or "").strip()
        if valor:
            return valor
    return ""


def _sugerencia_direccion(calle, ciudad, estado, zipc, etiqueta=None):
    calle = str(calle or "").strip()
    ciudad = str(ciudad or "").strip()
    estado = _codigo_estado_usa(estado)
    zipc = re.sub(r"\D", "", str(zipc or ""))[:5]
    localidad = " ".join(p for p in (ciudad, estado, zipc) if p)
    etiqueta = str(etiqueta or "").strip() or ", ".join(p for p in (calle, localidad) if p)
    if not calle and not localidad:
        return None
    return {
        "label": etiqueta,
        "street": calle,
        "city": ciudad,
        "state": estado,
        "zip": zipc,
    }


def _sugerencias_photon(q):
    from urllib.parse import urlencode
    params = urlencode({
        "q": q,
        "limit": 8,
        "lang": "en",
        "lat": 33.9526,
        "lon": -84.5499,
    })
    data = _http_json_get(
        f"https://photon.komoot.io/api/?{params}",
        headers={"User-Agent": "SolidRemodelingEstimates/1.0 (address-autocomplete)"},
    )
    items = []
    for feat in (data or {}).get("features") or []:
        props = feat.get("properties") if isinstance(feat, dict) else {}
        if not isinstance(props, dict):
            continue
        pais = str(props.get("countrycode") or props.get("country") or "").upper()
        if pais and pais not in ("US", "USA", "UNITED STATES"):
            continue
        item = _sugerencia_direccion(
            _calle_desde_partes(props),
            _ciudad_desde_partes(props),
            props.get("state"),
            props.get("postcode"),
            props.get("name") and ", ".join(
                p for p in (
                    _calle_desde_partes(props) or props.get("name"),
                    _ciudad_desde_partes(props),
                    _codigo_estado_usa(props.get("state")),
                    re.sub(r"\D", "", str(props.get("postcode") or ""))[:5],
                ) if p
            ),
        )
        if item:
            items.append(item)
    return items


def _sugerencias_nominatim(q):
    from urllib.parse import urlencode
    params = urlencode({
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 8,
        "countrycodes": "us",
        "q": q,
    })
    data = _http_json_get(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={
            "User-Agent": "SolidRemodelingEstimates/1.0 (address-autocomplete)",
            "Accept-Language": "en",
        },
    )
    items = []
    for row in data if isinstance(data, list) else []:
        addr = row.get("address") if isinstance(row, dict) else {}
        if not isinstance(addr, dict):
            addr = {}
        item = _sugerencia_direccion(
            _calle_desde_partes(addr),
            _ciudad_desde_partes(addr),
            addr.get("state") or addr.get("ISO3166-2-lvl4"),
            addr.get("postcode"),
            row.get("display_name"),
        )
        if item:
            items.append(item)
    return items


@app.route("/api/direcciones", methods=["GET"])
def api_direcciones():
    q = str(request.args.get("q") or request.args.get("query") or "").strip()
    if len(q) < 3:
        return jsonify({"items": []})
    items = []
    errores = []
    for fuente in (_sugerencias_photon, _sugerencias_nominatim):
        try:
            items = fuente(q)
            if items:
                break
        except Exception as err:
            errores.append(str(err))
            print(f"--> direcciones {fuente.__name__}: {err}")
    vistos = set()
    unicos = []
    for item in items:
        clave = (item.get("street"), item.get("city"), item.get("state"), item.get("zip"), item.get("label"))
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(item)
    return jsonify({"items": unicos[:8], "errors": errores[:2]})


@app.route("/api/buscar-catalogo", methods=["GET"])
def buscar_catalogo():
    q = request.args.get("q", "").strip()
    zip_q = request.args.get("zip") or request.args.get("zip_code") or ""
    from oficios import detectar_oficio, fila_es_oficio, normalizar_oficio
    q_norm = _corregir_typos_catalogo(q)
    oficio_ui = normalizar_oficio(request.args.get("oficio") or request.args.get("project_type") or "")
    oficio = detectar_oficio(q_norm) or oficio_ui
    idioma = str(request.args.get("idioma") or request.args.get("lang") or "es").strip().lower()
    if idioma not in IDIOMAS_CATALOGO:
        idioma = "es"
    zona = _zona_mercado_por_zip(zip_q)
    print(f"--> Término buscado: '{q}' nucleo='{_nucleo_busqueda_catalogo(q_norm)}' oficio={oficio} ui={oficio_ui} ZIP={zona.get('zip')} {zona.get('ciudad')} {zona.get('estado')} factor={zona.get('factor')}")
    try:
        if supabase is None:
            raise RuntimeError("Cliente supabase es None (revisa SUPABASE_URL y SUPABASE_KEY en .env)")
        q_match = _nucleo_busqueda_catalogo(q_norm) or q_norm
        filas = []
        if q_match:
            filtros = [
                _filtro_or_flexible(q_match),
                f"descripcion.ilike.%{_limpiar_termino_catalogo(q_match)}%,codigo.ilike.%{_limpiar_termino_catalogo(q_match)}%",
            ]
            ultimo_err = None
            for filtro in filtros:
                if not filtro:
                    continue
                try:
                    res = supabase.table(TABLA_CATALOGO).select("*").or_(filtro).limit(200).execute()
                    filas = list(res.data or [])
                    ultimo_err = None
                    break
                except Exception as err:
                    ultimo_err = err
                    print(f"--> buscar-catalogo filtro: {err}")
            if ultimo_err and not filas:
                raise ultimo_err
        else:
            res = supabase.table(TABLA_CATALOGO).select("*").limit(200).execute()
            filas = list(res.data or [])
        tokens = _tokens_busqueda_catalogo(q_match)
        unidad_q = (request.args.get("unidad") or request.args.get("unit") or "").upper()
        if oficio:
            filtradas = [row for row in filas if fila_es_oficio(row, oficio)]
            filas = filtradas if filtradas else filas
        consulta_ridge = bool(re.search(r"\bridges?\b|ridge\s*cap|cumbrera", q_match or q_norm, re.I))
        if consulta_ridge:
            def _cat_row(row):
                return str(row.get("categoria") or row.get("category") or row.get("rama") or row.get("familia") or "").upper()
            def _blob_row(row):
                return f"{row.get('codigo') or ''} {row.get('descripcion') or row.get('description') or ''} {_cat_row(row)}"
            def _es_metal_row(row):
                return bool(re.search(
                    r"standing\s*seam|metal\s*roof|cubierta de metal|hojalat|sheet\s*metal|"
                    r"calibre\s*2[0-6]|EXT-?ROF-?MET|\bMET-?(STN|SS|COP|ZNC|PNL|RDG)\b",
                    _blob_row(row),
                    re.I,
                ))
            residenciales = [row for row in filas if not _es_metal_row(row)]
            techo = [row for row in residenciales if re.search(r"TECH|ROOF|RFG|TECHO", _cat_row(row))]
            filas = techo or residenciales
            if not unidad_q:
                unidad_q = "LF"
        umbral = 6
        def _orden_catalogo(row):
            score = _puntaje_coincidencia_catalogo(row, q_match, tokens, unidad_q) + _puntaje_catalogo_zip(row, zona)
            precio = _num_catalogo(row, "precio_base", "precio_unitario", "precio", "price")
            if consulta_ridge:
                return (score, -abs((precio or 0) - 13.5))
            return (score, precio)
        filas.sort(key=_orden_catalogo, reverse=True)
        razonables = [
            row for row in filas
            if _puntaje_coincidencia_catalogo(row, q_match, tokens, unidad_q) >= umbral
        ] or filas
        limite = 40
        items = [item for item in (normalizar_item_catalogo(row, idioma) for row in razonables[:limite]) if item]
        factor = float(zona.get("factor") or 1.0)
        for item in items:
            base = _num(item.get("precio_unitario"))
            item["precio_base"] = round(base, 2)
            item["factor_zip"] = round(factor, 4)
            item["zip_code"] = zona.get("zip")
            item["ciudad"] = zona.get("ciudad")
            item["estado"] = zona.get("estado")
            item["precio_zona"] = round(base * factor, 2)
            item["ajuste_zip"] = True
            item["fuente"] = item.get("fuente") or "supabase"
        print(f"--> catálogo supabase q='{q_match}' filas={len(filas)} items={len(items)}")
        if not items and str(request.args.get("registrar_faltante") or "") in ("1", "true", "si", "yes"):
            registrar_item_faltante(q, oficio, zona.get("zip") or zip_q)
        return jsonify(items)
    except Exception as e:
        print(f"ERROR SUPABASE DETALLADO: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sugerir-precios-ia", methods=["POST"])
def sugerir_precios_ia():
    payload = request.get_json(silent=True) or {}
    descripcion = str(payload.get("descripcion") or payload.get("description") or "").strip()
    unidad = str(payload.get("unidad") or payload.get("unit") or "SF").upper()
    zip_q = payload.get("zip_code") or payload.get("zip") or ""
    if not descripcion:
        return jsonify({"error": "Falta la descripción del trabajo"}), 400
    from oficios import OFICIOS, detectar_oficio, mejores_precios_oficio, normalizar_oficio
    oficio = detectar_oficio(
        descripcion,
        payload.get("oficio"),
        payload.get("project_type"),
        payload.get("categoria"),
        preferido=normalizar_oficio(payload.get("oficio") or payload.get("project_type") or ""),
    )
    meta_oficio = OFICIOS.get(oficio) or {}
    label_oficio = meta_oficio.get("label") or oficio or "construcción general"
    zona = _zona_mercado_por_zip(zip_q)
    zip_txt = zona.get("zip") or ""
    ciudad = zona.get("ciudad") or ""
    estado = zona.get("estado") or ""
    factor = float(zona.get("factor") or 1.0) or 1.0
    refs = mejores_precios_oficio(descripcion, unidad, oficio, limite=5)
    refs_txt = "\n".join(
        f"- {r.get('codigo')}: {r.get('descripcion')} → ${r['precio_unitario']:.2f}/{r.get('unidad')}"
        for r in refs
    ) or "(sin coincidencias de catálogo en este oficio)"
    prompt = f"""Eres un estimador de construcción en Estados Unidos.
Oficio del proyecto (OBLIGATORIO, no mezcles otros): {label_oficio} ({oficio or 'general'}).
Partida: "{descripcion}"
Unidad: {unidad}
Código postal: {zip_txt} ({ciudad}, {estado})

Usa SOLO precios de este oficio. Prohibido sugerir partidas de otro gremio
(p. ej. siding vs techo, LVT vs teja, drywall vs baño).
Referencias filtradas del catálogo interno de ESTE oficio:
{refs_txt}

Devuelve 3 a 5 precios unitarios de MERCADO en USD para ESA región y ESE oficio, basados en promedios públicos de materiales y mano de obra.
No uses software, marcas ni bases propietarias de terceros.
JSON únicamente:
{{"oficio":"{oficio or ''}","sugerencias":[{{"etiqueta":"Catálogo / Económico","precio_unitario":0,"unidad":"{unidad}","nota":"solo {label_oficio}"}}]}}
"""
    try:
        response = None
        ultimo_error = None
        for modelo in ("gemini-2.5-flash", "gemini-3.6-flash", "gemini-flash-latest"):
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                break
            except Exception as err:
                ultimo_error = err
                texto = str(err).lower()
                if any(token in texto for token in ("503", "unavailable", "high demand", "overloaded", "429", "404", "not_found", "not found")):
                    print(f"--> sugerir-precios-ia: {modelo} no disponible ({err}); reintento")
                    continue
                raise
        if response is None:
            raise ultimo_error or RuntimeError("No hay un modelo Gemini disponible")
        parsed = extraer_json(getattr(response, "text", None) or "")
    except Exception as err:
        print(f"--> sugerir-precios-ia Gemini: {err}")
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"sugerencias": parsed if isinstance(parsed, list) else []}
    crudas = parsed.get("sugerencias") or parsed.get("items") or parsed.get("precios") or []
    sugerencias = []
    vistos = set()
    for ref in refs:
        precio = round(float(ref["precio_unitario"]) * factor, 2)
        if precio <= 0 or precio in vistos:
            continue
        vistos.add(precio)
        codigo = ref.get("codigo") or ""
        sugerencias.append({
            "etiqueta": (codigo + " · catálogo").strip(" ·") if codigo else "Catálogo del oficio",
            "precio_unitario": precio,
            "unidad": str(ref.get("unidad") or unidad).upper(),
            "nota": (ref.get("descripcion") or "")[:80],
            "fuente": "catalogo",
            "oficio": oficio,
        })
    for row in crudas:
        if not isinstance(row, dict):
            continue
        precio = _num_catalogo(row, "precio_unitario", "precio", "price", "unit_price")
        if precio <= 0:
            continue
        precio = round(precio, 2)
        if precio in vistos:
            continue
        vistos.add(precio)
        sugerencias.append({
            "etiqueta": str(row.get("etiqueta") or row.get("label") or row.get("nivel") or "Mercado").strip() or "Mercado",
            "precio_unitario": precio,
            "unidad": str(row.get("unidad") or row.get("unit") or unidad).upper(),
            "nota": str(row.get("nota") or row.get("note") or label_oficio).strip(),
            "fuente": "ia",
            "oficio": oficio,
        })
    sugerencias.sort(key=lambda x: x["precio_unitario"])
    return jsonify({
        "zip_code": zip_txt,
        "ciudad": ciudad,
        "estado": estado,
        "factor": zona.get("factor"),
        "oficio": oficio,
        "project_type": oficio,
        "oficio_label": label_oficio,
        "sugerencias": sugerencias[:6],
    })


@app.route("/api/test-db")
def test_db():
    try:
        if supabase is None:
            raise RuntimeError("Cliente supabase es None (revisa SUPABASE_URL y SUPABASE_KEY en .env)")
        res = supabase.table(TABLA_CATALOGO).select("*").limit(5).execute()
        print(f"--> /api/test-db filas: {len(res.data) if res.data else 0}")
        return jsonify(res.data)
    except Exception as e:
        print(f"ERROR SUPABASE DETALLADO: {e}")
        return jsonify({"error": str(e)}), 500


def _es_documento_roof_report(parsed):
    if not isinstance(parsed, dict):
        return False
    tipo = str(parsed.get("document_type") or "").strip().lower()
    if tipo in ("estimate", "invoice", "quote", "xactimate"):
        return False
    if "roof" in tipo:
        return True
    return parece_reporte_techo(parsed)


def _normalizar_unidad_medida_techo(unit, qty, desc, unidad_area="SQ"):
    u = str(unit or "").upper().replace(" ", "").replace(".", "")
    q = max(_num(qty), 0.0)
    texto = str(desc or "").lower()
    preferida = unidad_area_techo(unidad_area)
    if u in ("LF", "LINFT", "LINEAR", "LIN", "LFOT"):
        return round(q, 2), "LF"
    if u in ("PC", "PCS", "EA", "PIEZA", "PIEZAS"):
        return int(round(q)) if q else 0, "PC"
    lineal = bool(re.search(r"eaves|rakes|valley|limahoya|ridge|cumbrera|\bhips?\b|starter|ice\s*&\s*water|ice and water|flashing", texto))
    if lineal and u not in ("SQ", "SF", "SQFT", "SQUARES"):
        if "drip" in texto and q < 800:
            return int(math.ceil(q)) if q else 0, "PC"
        return round(q, 2), "LF"
    if "drip" in texto and u not in ("SQ", "SF", "SQFT"):
        return (int(math.ceil(q)) if q else 0), "PC"
    es_sq = u in ("SQ", "SQUARES", "SQUARE", "ESCUADRA", "ESCUADRAS")
    es_sf = u in ("SF", "SQFT", "SQFT")
    if preferida == "SF":
        if es_sq or (q > 0 and q < 80 and not es_sf):
            q = round(q * 100.0, 2)
        return round(q, 2), "SF"
    if es_sf or (not es_sq and q >= 80):
        q = round(q / 100.0, 2)
    elif es_sq and q >= 80:
        q = round(q / 100.0, 2)
    return round(q, 2), "SQ"


def _clave_precio_roof(desc, unit):
    texto = str(desc or "").lower()
    u = str(unit or "").upper()
    if re.search(r"tear.?off|remoci[oó]n de teja|tear out", texto):
        return "tear_off"
    if re.search(r"pendiente pronunciada|steep charge|\bsteep\b", texto):
        return "steep"
    if re.search(r"techo plano|sbs|flat roof", texto):
        return "flat_sbs"
    if re.search(r"starter", texto):
        return "starter"
    if re.search(r"drip\s*edge", texto):
        return "drip_edge"
    if re.search(r"ice\s*(&|and)\s*water", texto):
        return "ice_water"
    if re.search(r"ridge\s*cap|shingle\s*ridge|continuous\s*ridge|cumbrera", texto):
        return "hips_ridges"
    if re.search(r"valley|limahoya", texto):
        return "valley"
    if re.search(r"\brakes?\b", texto):
        return "rakes"
    if re.search(r"teja arquitect|architectural|shingle", texto) and u in ("SQ", "SF", "SQFT"):
        return "arch_shingle"
    if u in ("SQ", "SF", "SQFT"):
        return "arch_shingle"
    if u == "LF":
        return "starter"
    if u in ("PC", "EA"):
        return "drip_edge"
    return "arch_shingle"


def _preciar_partidas_roof_ia(filas, zip_code, unidad_area="SQ"):
    rates = _precios_partidas_techo(zip_code)
    unidad_area = unidad_area_techo(unidad_area)
    items = []
    n = 1
    for row in filas or []:
        if not isinstance(row, dict):
            continue
        desc = str(row.get("description") or row.get("descripcion") or "").strip()
        if not desc or es_medida_area_redundante(desc) or es_etiqueta_reporte(desc):
            continue
        qty, unit = _normalizar_unidad_medida_techo(
            row.get("unit") or row.get("unidad"),
            row.get("quantity") or row.get("cantidad") or row.get("qty"),
            desc,
            unidad_area,
        )
        clave = _clave_precio_roof(desc, unit)
        price = _num(rates.get(clave) or PRECIOS_ROOFR.get(clave) or 0)
        if unit == "SF" and clave in ("tear_off", "arch_shingle", "steep", "flat_sbs") and price > 5:
            price = round(price / 100.0, 4)
        partida = linea(n, "Roofing", desc, qty, unit, price, unidad_area if unit in ("SQ", "SF") else unit)
        if not partida:
            continue
        if unit in ("LF", "PC", "EA"):
            partida["unit"] = unit
            partida["unidad"] = unit
            partida["quantity"] = qty if unit != "PC" else int(qty)
            partida["unit_price"] = round(price, 2)
            partida["precio_unitario"] = round(price, 2)
            partida["total"] = round(partida["quantity"] * price, 2)
        action = str(row.get("action") or "").strip().lower()
        if action in ("demo", "install", "replace", "labor", "material"):
            partida["action"] = action
        items.append(partida)
        n += 1
    return items


def interpretar_roof_report_con_ia(pdf_bytes, unidad_area="SQ"):
    unidad_area = unidad_area_techo(unidad_area)
    prompt = PROMPT_ROOF_REPORT.replace("{UNIDAD_AREA}", unidad_area)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            prompt,
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return extraer_json(response.text)


def procesar_roof_report_ia(pdf_bytes, parsed_inicial, zip_activo, unidad_area="SQ"):
    unidad_area = unidad_area_techo(unidad_area)
    ia = interpretar_roof_report_con_ia(pdf_bytes, unidad_area)
    if not isinstance(ia, dict):
        ia = {}
    medidas = fusionar_medidas(parsed_inicial if isinstance(parsed_inicial, dict) else {})
    medidas.update(fusionar_medidas(ia))
    cosecha = cosechar_medidas_de_items(iterar_filas(ia) + iterar_filas(parsed_inicial))
    for key, value in cosecha.items():
        if medidas.get(key) in (None, "", 0, "0"):
            medidas[key] = value
    zip_code = None
    address = None
    for fuente in (ia, parsed_inicial if isinstance(parsed_inicial, dict) else {}, medidas):
        zip_code = zip_code or fuente.get("zip_code")
        address = address or fuente.get("address")
    zip_precio = re.sub(r"\D", "", str(zip_activo or zip_code or ""))[:5]
    if zip_precio:
        medidas["zip_code"] = zip_precio
        zip_code = zip_precio
    filas = [
        row for row in iterar_filas(ia)
        if isinstance(row, dict)
        and str(row.get("description") or row.get("descripcion") or "").strip()
        and not es_medida_area_redundante(row.get("description") or row.get("descripcion"))
    ]
    items = _preciar_partidas_roof_ia(filas, zip_precio or zip_code, unidad_area)
    items = unificar_y_filtrar_partidas(items, unidad_area) if items else []
    if len(items) < 2:
        items = unificar_y_filtrar_partidas(generar_partidas_techo(medidas, unidad_area), unidad_area)
    return {
        "document_type": "roof_report",
        "zip_code": zip_code,
        "address": address,
        "items": items,
        "data": items,
        "area_unit": unidad_area,
        "roof": {
            "zip_code": zip_precio or zip_code,
            "address": address,
            "pitched_sqft": _num(medidas.get("pitched_sqft")),
            "flat_sqft": _num(medidas.get("flat_sqft")),
            "total_sqft": _num(medidas.get("total_sqft")),
            "pitched_squares": area_a_squares(medidas, ("pitched_sqft",), ("pitched_squares",)),
            "flat_squares": area_a_squares(medidas, ("flat_sqft",), ("flat_squares",)),
            "rates": _precios_partidas_techo(zip_precio or zip_code),
        },
    }


@app.route("/api/import-roof-report", methods=["POST"])
@app.route("/api/process-roof-report", methods=["POST"])
def import_roof_report():
    try:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        ssl._create_default_https_context = ssl._create_unverified_context
        try:
            import certifi
            _ca = certifi.where()
            os.environ["SSL_CERT_FILE"] = _ca
            os.environ["REQUESTS_CA_BUNDLE"] = _ca
            os.environ["CURL_CA_BUNDLE"] = _ca
            os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = _ca
        except Exception:
            os.environ["CURL_CA_BUNDLE"] = ""
            os.environ["REQUESTS_CA_BUNDLE"] = ""
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass
        try:
            import httplib2
            _Http = httplib2.Http

            def _HttpSinVerify(*args, **kwargs):
                kwargs["disable_ssl_certificate_validation"] = True
                return _Http(*args, **kwargs)

            httplib2.Http = _HttpSinVerify
        except Exception:
            pass
        try:
            import requests as _req_ssl

            _orig_req = _req_ssl.Session.request

            def _request_sin_verify(self, method, url, **kwargs):
                kwargs["verify"] = False
                return _orig_req(self, method, url, **kwargs)

            _req_ssl.Session.request = _request_sin_verify
        except Exception:
            pass

        from roof_parser import procesar_reporte_roofr, payload_http_cotizador

        file = request.files.get("file") or request.files.get("pdf_file")
        if file is None:
            return jsonify({"success": False, "error": "No se encontró el archivo PDF"}), 400
        if file.filename == "":
            return jsonify({"success": False, "error": "Archivo no seleccionado"}), 400

        pdf_bytes = file.read()
        if not pdf_bytes:
            return jsonify({"success": False, "error": "Archivo no seleccionado"}), 400

        zip_form = re.sub(r"\D", "", str(request.form.get("zip_code") or request.args.get("zip_code") or ""))[:5]
        payload = payload_http_cotizador(procesar_reporte_roofr(
            pdf_bytes,
            zip_activo=zip_form,
            unidad_area="SQ",
            zona_fn=_zona_mercado_por_zip,
        ))
        items = payload.get("items") or []
        if not items:
            return jsonify({
                "success": False,
                "error": "Claude no devolvió mediciones suficientes para armar las partidas.",
                "measures": payload.get("measures"),
                "eaves": payload.get("eaves"),
                "valleys": payload.get("valleys"),
                "hips": payload.get("hips"),
                "ridges": payload.get("ridges"),
                "rakes": payload.get("rakes"),
            }), 422

        def _fila_http(row):
            return {
                "item": row.get("item"),
                "codigo": row.get("codigo") or row.get("code") or "",
                "code": row.get("codigo") or row.get("code") or "",
                "description": row.get("description") or row.get("descripcion") or "",
                "descripcion": row.get("description") or row.get("descripcion") or "",
                "quantity": float(row.get("quantity") or row.get("qty") or 0),
                "cantidad": float(row.get("quantity") or row.get("qty") or 0),
                "qty": float(row.get("quantity") or row.get("qty") or 0),
                "unit": row.get("unit") or row.get("unidad") or "SQ",
                "unidad": row.get("unit") or row.get("unidad") or "SQ",
                "unit_price": float(row.get("unit_price") or row.get("precio") or 0),
                "precio_unitario": float(row.get("unit_price") or row.get("precio") or 0),
                "precio": float(row.get("unit_price") or row.get("precio") or 0),
                "price": float(row.get("unit_price") or row.get("precio") or 0),
                "total": float(row.get("total") or 0),
                "subtotal": float(row.get("total") or 0),
                "measure_key": row.get("measure_key") or "",
                "action": row.get("action") or "install",
                "trade": "Roofing",
                "categoria": row.get("categoria") or "TECHO",
                "codigo_base": row.get("codigo_base") or row.get("codigo") or row.get("code") or "",
                "nucleo": row.get("nucleo") or "",
                "precio_demo": float(row.get("precio_demo") or 0),
                "precio_install": float(row.get("precio_install") or 0),
            }

        filas = []
        for r in items:
            if not isinstance(r, dict):
                continue
            fila = _fila_http(r)
            if fila["quantity"] <= 0 or not str(fila["description"]).strip():
                continue
            filas.append(fila)
        medidas = payload.get("measures") or {}
        eaves = float(medidas.get("eaves") or medidas.get("eaves_lf") or payload.get("eaves") or 0)
        valleys = float(medidas.get("valleys") or medidas.get("valleys_lf") or payload.get("valleys") or 0)
        hips = float(medidas.get("hips") or medidas.get("hips_lf") or payload.get("hips") or 0)
        ridges = float(medidas.get("ridges") or medidas.get("ridges_lf") or payload.get("ridges") or 0)
        rakes = float(medidas.get("rakes") or medidas.get("rakes_lf") or payload.get("rakes") or 0)
        squares = float(medidas.get("squares") or medidas.get("total_roof_area_sq") or payload.get("squares") or 0)
        http_json = {
            "success": True,
            "ok": True,
            "document_type": "roof_report",
            "project_type": "roofing",
            "oficio": "roofing",
            "zip_code": payload.get("zip_code"),
            "address": payload.get("address"),
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
            "items": filas,
            "partidas": filas,
            "data": filas,
            "line_items": filas,
            "rows": filas,
            "measures": medidas,
            "grand_total": payload.get("grand_total"),
            "skip_minimums": payload.get("skip_minimums"),
            "roof": payload.get("roof") or {},
        }
        print("========== HTTP /api/import-roof-report (JSON al navegador) ==========")
        print(json.dumps({
            "zip_code": http_json.get("zip_code"),
            "eaves": http_json.get("eaves"),
            "valleys": http_json.get("valleys"),
            "hips": http_json.get("hips"),
            "ridges": http_json.get("ridges"),
            "rakes": http_json.get("rakes"),
            "squares": http_json.get("squares"),
            "n_items": len(filas),
            "items": filas,
            "grand_total": http_json.get("grand_total"),
        }, ensure_ascii=False, indent=2)[:8000])
        print("========== FIN HTTP PAYLOAD ==========")
        resp = jsonify(http_json)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    except Exception as e:
        return jsonify({"success": False, "error": f"No se pudieron extraer mediciones del roof report: {str(e)}"}), 500


@app.route("/api/process-pdf", methods=["POST"])
def process_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No se envió ningún archivo"}), 400

    file = request.files["file"]
    pdf_bytes = file.read()

    try:
        from roof_parser import es_reporte_roofr, payload_http_cotizador, procesar_reporte_roofr

        oficio_form = str(request.form.get("oficio") or request.form.get("project_type") or "")
        if es_reporte_roofr(pdf_bytes) or "roof" in oficio_form.lower():
            zip_form = re.sub(r"\D", "", str(request.form.get("zip_code") or request.args.get("zip_code") or ""))[:5]
            payload = payload_http_cotizador(procesar_reporte_roofr(
                pdf_bytes,
                zip_activo=zip_form,
                unidad_area="SQ",
                zona_fn=_zona_mercado_por_zip,
            ))
            resp = jsonify(payload)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(
                    data=pdf_bytes,
                    mime_type="application/pdf",
                ),
                PROMPT_PDF,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        parsed = extraer_json(response.text)
        if parsed is None:
            return jsonify({"error": "No se pudo interpretar la respuesta del modelo"}), 500

        zip_activo = re.sub(r"\D", "", str(request.form.get("zip_code") or request.args.get("zip_code") or ""))[:5]
        unidad_area = unidad_area_techo(request.form.get("area_unit") or request.args.get("area_unit") or "SQ")
        oficio_form = request.form.get("oficio") or request.form.get("project_type") or request.form.get("categoria") or ""
        nombre_pdf = str(getattr(file, "filename", "") or "")

        zip_code = None
        address = None
        if isinstance(parsed, dict):
            zip_code = parsed.get("zip_code") or (parsed.get("roof") or {}).get("zip_code")
            address = parsed.get("address") or (parsed.get("roof") or {}).get("address")

        items, medidas, es_techo = sanitizar_respuesta_pdf(parsed, unidad_area, zip_activo, forzar_estimado=True)
        zip_code = zip_code or medidas.get("zip_code")
        address = address or medidas.get("address")
        blob_items = " ".join(
            str((row or {}).get("description") or (row or {}).get("descripcion") or (row or {}).get("trade") or "")
            for row in (items or []) if isinstance(row, dict)
        )
        from oficios import aplicar_precios_items, detectar_oficio, etiqueta_ui
        oficio = detectar_oficio(
            oficio_form,
            parsed.get("project_type") if isinstance(parsed, dict) else "",
            parsed.get("document_type") if isinstance(parsed, dict) else "",
            nombre_pdf,
            blob_items,
            "roofing" if es_techo else "",
            preferido=oficio_form,
        )
        if es_techo and not oficio:
            oficio = "roofing"
        zona = _zona_mercado_por_zip(zip_code or zip_activo)
        items = aplicar_precios_items(items or [], oficio, zona.get("factor") or 1.0)
        payload = {
            "document_type": "estimate" if not es_techo else "roof_report",
            "project_type": oficio,
            "oficio": oficio,
            "oficio_label": etiqueta_ui(oficio) if oficio else "",
            "zip_code": zip_code,
            "address": address,
            "items": items,
            "data": items,
            "area_unit": unidad_area,
        }
        return jsonify(payload), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _buscar_navegador_pdf():
    import shutil
    candidatos = []
    for clave in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        raiz = os.environ.get(clave) or ""
        candidatos.extend([
            os.path.join(raiz, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(raiz, "Google", "Chrome", "Application", "chrome.exe"),
        ])
    for nombre in ("msedge", "chrome", "google-chrome", "chromium"):
        encontrado = shutil.which(nombre)
        if encontrado:
            candidatos.append(encontrado)
    for ruta in candidatos:
        if ruta and os.path.isfile(ruta):
            return ruta
    return None


SERVICIOS_COMERCIALES_INGLES = {
    "DEM": "Demolition and Haul-Off",
    "WTR": "Water Mitigation and Structural Drying",
    "HAZ": "Hazardous Material Removal",
    "MAS": "Concrete and Masonry",
    "STN": "Retaining Walls and Hardscaping",
    "FRM": "Rough Framing",
    "SUB": "Subfloor, Joist, and Blocking Repair",
    "TRU": "Roof Trusses and Rafters",
    "DCK": "Deck and Porch Construction",
    "FNC": "Fence Installation",
    "RFG": "Roofing, Shingles, and Membranes",
    "SDG": "Siding, Fascia, and Soffit",
    "GUT": "Gutters and Downspouts",
    "WND": "Window Replacement and Exterior Sealing",
    "PLM": "Residential Plumbing and Rough-In",
    "ELE": "Electrical and Lighting",
    "HVC": "HVAC and Ductwork",
    "INS": "Thermal and Acoustic Insulation",
    "DRY": "Drywall Installation and Finishing",
    "PNT": "Interior Painting and Trim",
    "PXT": "Exterior Painting and Sealing",
    "TIL": "Tile and Porcelain Installation",
    "STP": "Shower Waterproofing",
    "FCT": "Vinyl, LVT, and Laminate Flooring Installation",
    "WDH": "Hardwood Sanding and Staining",
    "CPR": "Carpet and Pad Installation",
    "CAB": "Cabinet Installation",
    "CTR": "Granite and Quartz Countertops",
    "DOR": "Interior Doors, Trim, and Baseboards",
    "STR": "Stairs, Treads, and Handrails",
    "KTR": "Turnkey Kitchen Remodeling",
    "BTR": "Turnkey Bathroom Remodeling",
    "BMT": "Basement Finishing",
    "ADD": "Residential Addition",
    "GEN": "General Residential Remodeling",
}

_SERVICIOS_ES_A_CODIGO = {
    "demolicion general y retiro de escombros": "DEM",
    "mitigacion y secado de agua": "WTR",
    "retiro de materiales contaminados": "HAZ",
    "trabajos de concreto y mamposteria": "MAS",
    "muros de retencion y piedra": "STN",
    "estructura de madera y muros": "FRM",
    "reparacion de subfloor y viguetas": "SUB",
    "armaduras de techo y cerchas": "TRU",
    "construccion de deck y porche": "DCK",
    "instalacion de cercas": "FNC",
    "techado de shingle y membrana": "RFG",
    "revestimiento exterior siding": "SDG",
    "canales y bajantes": "GUT",
    "ventanas y sellos exteriores": "WND",
    "plomeria residencial y rough in": "PLM",
    "electricidad e iluminacion": "ELE",
    "climatizacion y ductos": "HVC",
    "aislamiento termico y acustico": "INS",
    "instalacion y acabado de drywall": "DRY",
    "pintura interior y molduras": "PNT",
    "pintura exterior y sellador": "PXT",
    "instalacion de azulejo y porcelanato": "TIL",
    "impermeabilizacion de duchas": "STP",
    "instalacion de pisos lvt spc": "FCT",
    "lijado y tenido de hardwood": "WDH",
    "alfombra y bajo alfombra": "CPR",
    "instalacion de gabinetes": "CAB",
    "instalacion de gabinetes de cocina": "CAB",
    "montaje de gabinetes y vanity": "CAB",
    "encimeras de granito y cuarzo": "CTR",
    "puertas interiores y molduras": "DOR",
    "escaleras huellas y barandales": "STR",
    "remodelacion completa de cocina": "KTR",
    "remodelacion completa de bano": "BTR",
    "acabado de sotano basement": "BMT",
    "ampliacion residencial addition": "ADD",
    "remodelacion general residencial": "GEN",
}


def _clave_servicio(valor):
    import unicodedata
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def _servicio_comercial_ingles(valor):
    texto = str(valor or "").strip()
    if not texto:
        return texto
    codigo = re.search(r"\[([A-Z]{2,4})\]", texto, re.I)
    if codigo:
        clave = codigo.group(1).upper()
        if clave in SERVICIOS_COMERCIALES_INGLES:
            return f"[{clave}] {SERVICIOS_COMERCIALES_INGLES[clave]}"
    clave = _clave_servicio(texto)
    codigo = _SERVICIOS_ES_A_CODIGO.get(clave)
    if codigo:
        return SERVICIOS_COMERCIALES_INGLES[codigo]
    return texto


def _traducir_servicio_en_html_pdf(html):
    import html as html_lib
    patron = re.compile(
        r'(<label\b(?=[^>]*\bfor\s*=\s*["\'](?:service|campo-servicio)["\'])'
        r'[^>]*>.*?</label>\s*<span\b[^>]*\bclass\s*=\s*["\']'
        r'[^"\']*\bprint-value\b[^"\']*["\'][^>]*>)(.*?)(</span>)',
        re.I | re.S,
    )

    def reemplazar(match):
        valor = html_lib.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        traducido = _servicio_comercial_ingles(valor)
        return match.group(1) + html_lib.escape(traducido) + match.group(3)

    return patron.sub(reemplazar, str(html or ""))


def _html_a_pdf_navegador(html):
    import subprocess
    import tempfile
    from pathlib import Path
    exe = _buscar_navegador_pdf()
    if not exe:
        raise RuntimeError("No se encontró Chrome o Edge para generar el PDF")
    with tempfile.TemporaryDirectory(prefix="estimado-pdf-") as tmp:
        html_path = os.path.join(tmp, "document.html")
        pdf_path = os.path.join(tmp, "document.pdf")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        uri = Path(html_path).resolve().as_uri()
        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--allow-file-access-from-files",
            "--no-pdf-header-footer",
            "--virtual-time-budget=8000",
            f"--print-to-pdf={pdf_path}",
            uri,
        ]
        creacion = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(cmd, capture_output=True, timeout=90, creationflags=creacion)
        if proc.returncode != 0 or not os.path.isfile(pdf_path):
            cmd[1] = "--headless"
            proc = subprocess.run(cmd, capture_output=True, timeout=90, creationflags=creacion)
        if not os.path.isfile(pdf_path):
            detalle = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="ignore")[:400]
            raise RuntimeError(detalle or "El navegador no generó el PDF")
        with open(pdf_path, "rb") as fh:
            return fh.read()


def _bytes_imagen_pdf(raw):
    import base64
    texto = raw if isinstance(raw, str) else ""
    if not texto and isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if "," in texto and texto.strip().lower().startswith("data:"):
        texto = texto.split(",", 1)[1]
    texto = re.sub(r"\s+", "", texto)
    if not texto:
        return b""
    return base64.b64decode(texto)


def _imagen_a_pdf(png_bytes):
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    page_w, page_h = 612, 792
    margin = 28
    usable_w = page_w - (margin * 2)
    usable_h = page_h - (margin * 2)
    scale = usable_w / max(img.width, 1)
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    resized = img.resize(new_size, Image.Resampling.LANCZOS)
    pages = []
    y = 0
    while y < resized.height:
        corte = resized.crop((0, y, resized.width, min(y + usable_h, resized.height)))
        pagina = Image.new("RGB", (page_w, page_h), "#ffffff")
        pagina.paste(corte, (margin, margin))
        pages.append(pagina)
        y += usable_h
    if not pages:
        pages = [Image.new("RGB", (page_w, page_h), "#ffffff")]
    salida = io.BytesIO()
    pages[0].save(salida, format="PDF", save_all=True, append_images=pages[1:], resolution=72.0)
    return salida.getvalue()


def _pdf_desde_solicitud(data):
    html = str((data or {}).get("html") or "").strip()
    if html:
        html = _traducir_servicio_en_html_pdf(html)
        return _html_a_pdf_navegador(html)
    imagen = _bytes_imagen_pdf((data or {}).get("image") or (data or {}).get("png") or (data or {}).get("dataUrl") or "")
    if imagen:
        return _imagen_a_pdf(imagen)
    raise ValueError("No se recibió el HTML del documento")


def _nombre_pdf_adjunto(tipo, folio):
    etiquetas = {"estimate": "Estimate", "contract": "Contract", "invoice": "Invoice"}
    etiqueta = etiquetas.get(str(tipo or "estimate").strip().lower(), "Estimate")
    limpio = re.sub(r"[^A-Za-z0-9-]+", "", str(folio or "").replace("#", ""))
    return f"{etiqueta}_{limpio or 'document'}.pdf"


def _correo_valido(valor):
    texto = str(valor or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", texto))


def _config_smtp():
    host = (os.getenv("SMTP_HOST") or "").strip().strip('"').strip("'")
    usuario = (os.getenv("SMTP_USER") or "").strip().strip('"').strip("'")
    clave = (os.getenv("SMTP_PASSWORD") or "").strip().strip('"').strip("'")
    remitente = (os.getenv("SMTP_FROM") or "").strip().strip('"').strip("'")
    try:
        puerto = int(os.getenv("SMTP_PORT", 587))
    except (TypeError, ValueError):
        puerto = 587
    return {
        "host": host,
        "port": puerto,
        "user": usuario,
        "password": clave,
        "from": remitente or usuario,
    }


def _sanear_nombre_remitente(nombre):
    texto = re.sub(r"[\r\n]+", " ", str(nombre or "")).strip().replace('"', "'")
    return texto[:120]


def _sanear_asunto(asunto):
    return re.sub(r"[\r\n]+", " ", str(asunto or "")).strip()[:180]


def _partes_documento(tipo):
    tipo = str(tipo or "estimate").strip().lower()
    if tipo in ("contract", "contrato"):
        return "contract", "Nuevo Contrato", "contrato"
    if tipo in ("invoice", "factura"):
        return "invoice", "Nueva Factura", "factura"
    return "estimate", "Nuevo Estimado", "estimado"


def _asunto_documento(tipo, empresa, folio=""):
    _, titulo, _ = _partes_documento(tipo)
    empresa = _sanear_nombre_remitente(empresa) or "Contratista"
    asunto = f"{titulo} de {empresa}"
    folio_limpio = re.sub(r"[\r\n#]+", " ", str(folio or "")).strip()
    if folio_limpio:
        asunto = f"{asunto} — {folio_limpio}"
    return _sanear_asunto(asunto)


def _cuerpo_html_estimado(tipo, empresa, enlace_aceptacion=None):
    import html as html_lib
    _, _, nombre_doc = _partes_documento(tipo)
    empresa = _sanear_nombre_remitente(empresa) or "Contratista"
    empresa_html = html_lib.escape(empresa)
    boton = ""
    if enlace_aceptacion and nombre_doc == "contrato":
        url = html_lib.escape(str(enlace_aceptacion), quote=True)
        boton = (
            "<p style=\"margin:22px 0;\">"
            f"<a href=\"{url}\" style=\"display:inline-block;background:#0d47a1;color:#ffffff;"
            "text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:700;\">"
            "Aceptar contrato</a></p>"
            "<p style=\"margin:0 0 14px;color:#475569;font-size:13px;\">"
            "Si el botón no abre, copia este enlace en el navegador:<br>"
            f"<span style=\"word-break:break-all;\">{url}</span></p>"
        )
    return (
        "<html><body style=\"margin:0;padding:24px;background:#ffffff;font-family:Arial,Helvetica,sans-serif;"
        "color:#1e293b;font-size:15px;line-height:1.6;\">"
        "<p style=\"margin:0 0 14px;\">Hola,</p>"
        f"<p style=\"margin:0 0 14px;\"><strong>{empresa_html}</strong> te envió un nuevo {nombre_doc}.</p>"
        "<p style=\"margin:0 0 14px;\">El documento va adjunto en este correo.</p>"
        f"{boton}"
        f"<p style=\"margin:0;\">Gracias,<br><strong>{empresa_html}</strong></p>"
        "</body></html>"
    )


def _texto_documento_cliente(tipo, empresa, enlace_aceptacion=None):
    _, _, nombre_doc = _partes_documento(tipo)
    empresa = _sanear_nombre_remitente(empresa) or "Contratista"
    lineas = [
        "Hola,",
        "",
        f"{empresa} te envió un nuevo {nombre_doc}.",
        "El documento va adjunto en este correo.",
    ]
    if enlace_aceptacion and nombre_doc == "contrato":
        lineas.extend(["", f"Aceptar contrato: {enlace_aceptacion}"])
    lineas.extend(["", "Gracias,", empresa, ""])
    return "\n".join(lineas)


def _credenciales_smtp():
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    smtp_host = (os.getenv("SMTP_HOST") or "").strip().strip('"').strip("'")
    try:
        smtp_port = int(os.getenv("SMTP_PORT", 587))
    except (TypeError, ValueError):
        smtp_port = 587
    smtp_user = (os.getenv("SMTP_USER") or "").strip().strip('"').strip("'")
    smtp_password = (os.getenv("SMTP_PASSWORD") or "").strip().strip('"').strip("'")
    smtp_from = (os.getenv("SMTP_FROM") or smtp_user).strip().strip('"').strip("'")
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is missing from the .env file.")
    if not smtp_user or not smtp_password:
        raise RuntimeError("SMTP_USER and SMTP_PASSWORD are missing from the .env file.")
    if not smtp_from:
        raise RuntimeError("SMTP_FROM is missing from the .env file.")
    return smtp_host, smtp_port, smtp_user, smtp_password, smtp_from


def _despachar_smtp(msg, destino):
    import smtplib
    smtp_host, smtp_port, smtp_user, smtp_password, smtp_from = _credenciales_smtp()
    server = smtplib.SMTP(smtp_host, smtp_port, timeout=40)
    try:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_password)
        server.send_message(msg, from_addr=smtp_from, to_addrs=[destino])
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _enviar_correo_pdf(destino, asunto, html_body, pdf_bytes, filename, sender_name=None, reply_to=None, texto=None, **kwargs):
    from email.message import EmailMessage
    sender_name = sender_name if sender_name is not None else kwargs.get("company") or kwargs.get("empresa")
    if reply_to is None:
        reply_to = kwargs.get("reply_to")
    company_name = _sanear_nombre_remitente(sender_name) or "Contratista"
    _, _, _, _, smtp_from = _credenciales_smtp()
    msg = EmailMessage()
    msg["Subject"] = _sanear_asunto(asunto)
    msg["From"] = f"{company_name} <{smtp_from}>"
    msg["To"] = destino
    if _correo_valido(reply_to):
        msg["Reply-To"] = str(reply_to).strip()
    msg.set_content(texto or _texto_documento_cliente("estimate", company_name))
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)
    _despachar_smtp(msg, destino)


def _enviar_correo_html(destino, asunto, html_body, texto, sender_name=None, reply_to=None):
    from email.message import EmailMessage
    company_name = _sanear_nombre_remitente(sender_name) or "Contratista"
    _, _, _, _, smtp_from = _credenciales_smtp()
    msg = EmailMessage()
    msg["Subject"] = _sanear_asunto(asunto)
    msg["From"] = f"{company_name} <{smtp_from}>"
    msg["To"] = destino
    if _correo_valido(reply_to):
        msg["Reply-To"] = str(reply_to).strip()
    msg.set_content(texto)
    msg.add_alternative(html_body, subtype="html")
    _despachar_smtp(msg, destino)


def enviar_correo_pdf(*args, **kwargs):
    return _enviar_correo_pdf(*args, **kwargs)


@app.route("/api/generar-pdf", methods=["POST"])
def api_generar_pdf():
    data = request.get_json(silent=True) or {}
    filename = re.sub(r"[^A-Za-z0-9._#-]+", "_", str(data.get("filename") or "document.pdf"))
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    preview = bool(data.get("preview", True))
    try:
        pdf_bytes = _pdf_desde_solicitud(data)
    except Exception as err:
        print(f"--> generar-pdf: {err}")
        return jsonify({"error": str(err)}), 500
    disposicion = "inline" if preview else "attachment"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'{disposicion}; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _url_aceptacion(token):
    base = (os.getenv("APP_BASE_URL") or request.host_url or "").strip().rstrip("/")
    return f"{base}/aceptar/{token}"


def _contratista_para_envio(data):
    contratista, error = _exigir_contratista()
    if error:
        return None, error
    email = str(data.get("contact") or data.get("contactEmail") or "").strip().lower()
    nombre = str(data.get("company") or data.get("nombre_empresa") or "").strip()
    if email and not _correo_valido(email):
        return None, (jsonify({"error": "Write a valid email in the Contact field."}), 400)
    if email or nombre:
        try:
            fresco = actualizar_contratista(contratista["token"], email or None, nombre or None)
        except Exception as err:
            return None, _respuesta_tenant(err)
        if not fresco:
            return None, (jsonify({"error": "Contractor not authorized"}), 403)
        fresco["token"] = contratista["token"]
        contratista = fresco
    if not _correo_valido(contratista.get("email")):
        return None, (jsonify({"error": "The contractor does not have a valid email in the database."}), 400)
    return contratista, None


def _enlace_aceptacion_documento(contratista, doc_id, tipo):
    if _partes_documento(tipo)[0] != "contract":
        return None
    ident = str(doc_id or "").strip()
    if not ident:
        raise RuntimeError("Save the contract before sending it.")
    row = obtener_historial_tenant(contratista, ident)
    if not row:
        raise RuntimeError("Contract not found for this contractor.")
    if str(row.get("tipo") or "").lower() not in ("contract", "contrato"):
        row = dict(row)
        row["tipo"] = "contract"
    if not row.get("token_aceptacion"):
        row = dict(row)
        row["token_aceptacion"] = str(uuid.uuid4())
        row["tipo"] = "contract"
        guardado = guardar_historial_tenant(contratista, row)
        if isinstance(guardado, dict):
            row = guardado
    token = row.get("token_aceptacion")
    if not token:
        raise RuntimeError("Could not create the contract acceptance link.")
    return _url_aceptacion(token)


def _notificar_aceptacion_contratista(datos):
    destino = str((datos or {}).get("contratista_email") or "").strip()
    if not _correo_valido(destino):
        raise RuntimeError("The contractor does not have a valid email in the database.")
    import html as html_lib
    empresa = _sanear_nombre_remitente((datos or {}).get("nombre_empresa")) or "Contratista"
    cliente = str((datos or {}).get("cliente_nombre") or "tu cliente").strip() or "tu cliente"
    folio = str((datos or {}).get("folio") or "").strip() or "sin folio"
    empresa_html = html_lib.escape(empresa)
    cliente_html = html_lib.escape(cliente)
    folio_html = html_lib.escape(folio)
    asunto = _sanear_asunto(f"Contrato aceptado — {empresa}")
    texto = (
        f"Hola,\n\n{cliente} aceptó el contrato {folio} de {empresa}.\n\n"
        "Puedes ver el estado actualizado en tu historial.\n"
    )
    html_body = (
        "<html><body style=\"margin:0;padding:24px;background:#ffffff;font-family:Arial,Helvetica,sans-serif;"
        "color:#1e293b;font-size:15px;line-height:1.6;\">"
        "<p style=\"margin:0 0 14px;\">Hola,</p>"
        f"<p style=\"margin:0 0 14px;\"><strong>{cliente_html}</strong> aceptó el contrato "
        f"<strong>{folio_html}</strong> de <strong>{empresa_html}</strong>.</p>"
        "<p style=\"margin:0;\">Puedes ver el estado actualizado en tu historial.</p>"
        "</body></html>"
    )
    reply_cliente = (datos or {}).get("cliente_email")
    _enviar_correo_html(
        destino,
        asunto,
        html_body,
        texto,
        sender_name=empresa,
        reply_to=reply_cliente,
    )
    return destino


def _html_aceptacion(titulo, mensaje, empresa="", folio="", cliente="", total=None, boton=False, token="", aviso=""):
    import html as html_lib
    def esc(valor):
        return html_lib.escape(str(valor or ""))
    detalle = ""
    if empresa or folio or cliente or total is not None:
        monto = ""
        if total is not None and total != "":
            try:
                monto = f"${float(total):,.2f}"
            except (TypeError, ValueError):
                monto = str(total)
        filas = "".join(
            f"<p style=\"margin:0 0 8px;\"><strong>{esc(etiq)}</strong> {esc(val)}</p>"
            for etiq, val in (
                ("Empresa", empresa),
                ("Contrato", folio),
                ("Cliente", cliente),
                ("Total", monto),
            ) if val
        )
        detalle = f"<div style=\"margin:18px 0;padding:14px 16px;background:#f8fafc;border-radius:8px;\">{filas}</div>"
    accion = ""
    if boton and token:
        accion = (
            f"<form method=\"post\" action=\"/aceptar/{esc(token)}\">"
            "<button type=\"submit\" style=\"background:#0d47a1;color:#fff;border:0;border-radius:6px;"
            "padding:12px 22px;font-size:16px;font-weight:700;cursor:pointer;\">Aceptar contrato</button>"
            "</form>"
        )
    nota = f"<p style=\"margin:16px 0 0;color:#b45309;\">{esc(aviso)}</p>" if aviso else ""
    cuerpo = (
        "<!DOCTYPE html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        f"<title>{esc(titulo)}</title>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "</head><body style=\"margin:0;background:#eef2f7;font-family:Arial,Helvetica,sans-serif;color:#1e293b;\">"
        "<main style=\"max-width:560px;margin:40px auto;background:#fff;padding:28px 24px;border-radius:12px;\">"
        f"<h1 style=\"margin:0 0 12px;font-size:24px;\">{esc(titulo)}</h1>"
        f"<p style=\"margin:0 0 8px;line-height:1.5;\">{esc(mensaje)}</p>"
        f"{detalle}{accion}{nota}</main></body></html>"
    )
    return cuerpo


@app.route("/api/enviar-estimado", methods=["POST"])
def api_enviar_estimado():
    data = request.get_json(silent=True) or {}
    destino = str(data.get("to") or data.get("email") or data.get("clientEmail") or "").strip()
    if not _correo_valido(destino):
        return jsonify({"error": "Add a valid client email before sending."}), 400
    tipo, _, _ = _partes_documento(data.get("type") or data.get("docMode") or "estimate")
    folio = str(data.get("folio") or data.get("quote") or data.get("number") or "").strip()
    contratista, error = _contratista_para_envio(data)
    if error:
        return error
    empresa = _sanear_nombre_remitente(contratista.get("nombre_empresa")) or "Contratista"
    reply_to = str(contratista.get("email") or "").strip()
    filename = _nombre_pdf_adjunto(tipo, folio)
    asunto = _asunto_documento(tipo, empresa, folio)
    try:
        enlace = _enlace_aceptacion_documento(contratista, data.get("id") or data.get("documentId"), tipo)
        pdf_bytes = _pdf_desde_solicitud(data)
        _enviar_correo_pdf(
            destino,
            asunto,
            _cuerpo_html_estimado(tipo, empresa, enlace),
            pdf_bytes,
            filename,
            sender_name=empresa,
            reply_to=reply_to,
            texto=_texto_documento_cliente(tipo, empresa, enlace),
        )
    except Exception as err:
        print(f"--> enviar-estimado: {err}")
        texto = str(err or "")
        if re.search(r"contratistas|contratista_id|token_aceptacion|PGRST202|PGRST204|PGRST205|does not exist", texto, re.I):
            _aviso_multitenant(err)
            return jsonify({"error": _SQL_MULTITENANT}), 503
        return jsonify({"error": texto}), 500
    print(f"--> correo enviado a={destino} reply_to={reply_to} adjunto={filename}")
    return jsonify({
        "ok": True,
        "to": destino,
        "reply_to": reply_to,
        "filename": filename,
        "subject": asunto,
        "enlace_aceptacion": enlace,
    })


@app.route("/aceptar/<token_aceptacion>", methods=["GET", "POST"])
def aceptar_contrato_publico(token_aceptacion):
    token = str(token_aceptacion or "").strip()
    if not _es_uuid(token):
        pagina = _html_aceptacion("Contrato no encontrado", "Este enlace de aceptación no es válido.")
        return Response(pagina, status=404, mimetype="text/html")
    try:
        if request.method == "GET":
            datos = leer_contrato_publico(token)
        else:
            previo = leer_contrato_publico(token)
            datos = marcar_contrato_aceptado(token)
    except Exception as err:
        print(f"--> aceptar contrato: {err}")
        _aviso_multitenant(err)
        pagina = _html_aceptacion("No se pudo abrir el contrato", _SQL_MULTITENANT)
        return Response(pagina, status=503, mimetype="text/html")
    if not datos:
        pagina = _html_aceptacion("Contrato no encontrado", "Este enlace no corresponde a un contrato pendiente.")
        return Response(pagina, status=404, mimetype="text/html")
    empresa = datos.get("nombre_empresa") or ""
    folio = datos.get("folio") or ""
    cliente = datos.get("cliente_nombre") or ""
    total = datos.get("total")
    aviso = ""
    if request.method == "POST" and not datos.get("already"):
        try:
            _notificar_aceptacion_contratista(datos)
        except Exception as err:
            print(f"--> notificar aceptacion: {err}")
            estado_previo = str((previo or {}).get("estado") or "pending")
            revertir_aceptacion_contrato(token, estado_previo)
            pagina = _html_aceptacion(
                "No se pudo completar la aceptación",
                "El contrato no se marcó como aceptado porque falló el aviso al contratista. Inténtalo de nuevo.",
                empresa=empresa,
                folio=folio,
                cliente=cliente,
                total=total,
                boton=True,
                token=token,
            )
            return Response(pagina, status=502, mimetype="text/html")
        titulo = "Contrato aceptado"
        mensaje = "Gracias. El contratista ya recibió la confirmación."
    elif str(datos.get("estado") or "").lower() == "accepted":
        titulo = "Contrato ya aceptado"
        mensaje = "Este contrato ya había sido aceptado. No hace falta volver a confirmarlo."
    else:
        titulo = "Aceptar contrato"
        mensaje = "Revisa los datos y confirma con el botón si estás de acuerdo."
    pagina = _html_aceptacion(
        titulo,
        mensaje,
        empresa=empresa,
        folio=folio,
        cliente=cliente,
        total=total,
        boton=request.method == "GET" and str(datos.get("estado") or "").lower() != "accepted",
        token=token,
        aviso=aviso,
    )
    return Response(pagina, mimetype="text/html", headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    app.run(port=5000, debug=True)
