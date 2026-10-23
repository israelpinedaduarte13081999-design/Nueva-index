import atexit
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
ssl._create_default_https_context = ssl._create_unverified_context
from flask import Flask, Response, redirect, request, jsonify, send_file, send_from_directory, render_template
from xml.sax.saxutils import escape as xml_escape
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

app = Flask(__name__, template_folder=".")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
DOMINIO_APP = "quantixbuilds.com"


@app.errorhandler(413)
def _archivo_demasiado_grande(_err):
    if request.path.startswith("/api/"):
        resp = jsonify({
            "success": False,
            "error": "El PDF supera 32 MB. Exporta el plano más liviano y vuelve a subirlo.",
        })
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 413
    return ("Archivo demasiado grande", 413)
COOKIE_CONTRATISTA = "contratista_token"
COOKIE_PERSISTIR = "contratista_persistir"
COOKIE_OAUTH = "oauth_verifier"
_MAX_EDAD_RECORDADA = 60 * 60 * 24 * 400
_ALERTA_ADMIN_EMAIL = "israelpinedaduarte13081999@gmail.com"
_ALERTA_MINIMA_SEG = 15 * 60
_ALERTAS_CRITICAS_PENDIENTES = []
_ALERTAS_CRITICAS_TS = {}
_MSG_LOGIN_AMABLE = "We could not finish signing you in. Please try again in a moment."
_FALTA_SERVICE_ROLE = False
_ERROR_ARRANQUE_SUPABASE = ""


def _gemini_ssl_verificado():
    return (os.getenv("GEMINI_SSL_VERIFY") or "1").strip().lower() not in ("0", "false", "no", "off")


def _crear_cliente_gemini(api_key=None):
    kwargs = {"api_key": api_key} if api_key else {}
    if not _gemini_ssl_verificado():
        # google-genai sustituye verify=False por su propio contexto; hay que pasar un SSLContext.
        ctx = ssl._create_unverified_context()
        kwargs["http_options"] = types.HttpOptions(client_args={"verify": ctx})
    return genai.Client(**kwargs)


if not _gemini_ssl_verificado():
    print("--> AVISO: Gemini sin verificación SSL (GEMINI_SSL_VERIFY=0). Solo para desarrollo local.")
print("--> Cache de precios IA vacia al arrancar (llave: descripcion + accion + ZIP). Search Grounding activo.")
client = _crear_cliente_gemini()
TABLA_CATALOGO = "catalogo_items"
EMBEDDING_MODELO = "text-embedding-3-small"
EMBEDDING_DIM = 1536
UMBRAL_SIMILITUD_CATALOGO = 0.70
TABLA_FACTORES = "factores_regionales_usa"
TABLA_FALTANTES = "items_faltantes"
TABLA_HISTORIAL = "historial_estimados"
TABLA_CONTRATISTAS = "contratistas"
COLUMNAS_CATALOGO_BASE = "id,codigo,descripcion,unidad,categoria,precio_base,precio_material,precio_mano_obra,precio_remocion"
COLUMNAS_CATALOGO = COLUMNAS_CATALOGO_BASE + ",image_url,last_updated,proveedor,product_url"
_SQL_MULTITENANT = "Ejecuta multitenant.sql en el editor SQL de Supabase."
_SQL_HOME_DEPOT = "Ejecuta catalogo_home_depot.sql en el editor SQL de Supabase."


def _ajustar_columnas_catalogo(err):
    global COLUMNAS_CATALOGO
    texto = str(err or "")
    if not ("42703" in texto or "does not exist" in texto):
        return False
    if "product_url" in COLUMNAS_CATALOGO and "product_url" in texto:
        COLUMNAS_CATALOGO = COLUMNAS_CATALOGO_BASE + ",image_url,last_updated,proveedor"
        print("-->", _SQL_HOME_DEPOT)
        return True
    if "proveedor" in COLUMNAS_CATALOGO and "proveedor" in texto:
        COLUMNAS_CATALOGO = COLUMNAS_CATALOGO_BASE + ",image_url,last_updated"
        print("-->", _SQL_HOME_DEPOT)
        return True
    if "last_updated" in COLUMNAS_CATALOGO and "last_updated" in texto:
        COLUMNAS_CATALOGO = COLUMNAS_CATALOGO_BASE + ",image_url"
        print("-->", _SQL_HOME_DEPOT)
        return True
    if "image_url" in COLUMNAS_CATALOGO and "image_url" in texto:
        COLUMNAS_CATALOGO = COLUMNAS_CATALOGO_BASE
        print("-->", _SQL_HOME_DEPOT)
        return True
    return False

def _tipo_clave_supabase(key):
    texto = str(key or "")
    if texto.startswith("sb_secret_"):
        return "service"
    if texto.startswith("sb_publishable_"):
        return "publishable"
    if texto.startswith("eyJ"):
        return "jwt"
    return "otra" if texto else "vacia"


def _supabase_creds():
    """El backend usa la clave de servicio para saltar RLS en INSERT/UPDATE."""
    global _SB_ROL
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    service = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SECRET_KEY")
        or ""
    ).strip()
    publica = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or ""
    ).strip()
    if service:
        _SB_ROL = "service"
        return url, service
    _SB_ROL = _tipo_clave_supabase(publica)
    return url, publica


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


_SB_ROL = "vacia"
_FALTA_SERVICE_ROLE = not (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
if _FALTA_SERVICE_ROLE:
    print("ALERTA: Falta la variable SUPABASE_SERVICE_ROLE_KEY en el entorno")
try:
    _sb_url, _sb_key = _supabase_creds()
    _sb_host = _sb_url.split("//", 1)[-1].split("/", 1)[0] if _sb_url else "(sin url)"
    print(f"--> Supabase host={_sb_host} clave={_SB_ROL} len={len(_sb_key)}")
    if _SB_ROL != "service":
        print("--> AVISO: falta SUPABASE_SERVICE_ROLE_KEY. El INSERT en catalogo_items lo bloquea RLS.")
    supabase = _crear_cliente_supabase(_sb_url, _sb_key)
    if supabase is None:
        print("--> Cliente supabase es None (faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")
        _ERROR_ARRANQUE_SUPABASE = "Cliente supabase es None (faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)"
    else:
        print("--> Cliente supabase creado (SSL verify desactivado)")
except Exception as e:
    print(f"ERROR SUPABASE DETALLADO al crear cliente: {e}")
    _ERROR_ARRANQUE_SUPABASE = str(e or "ERROR SUPABASE al crear cliente")
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
    "30327": 1.18,  # Buckhead, Atlanta — no se aplica; 30327 es mercado base 1.00
}
FACTOR_MERCADO_BASE = 1.00
FACTOR_RESPALDO = 1.00
PREFIXES_MERCADO_BASE_ATLANTA = frozenset({"303"})
ZIPS_MERCADO_BASE_ATLANTA = frozenset({"30303", "30327"})

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


import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import time

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

_RUTAS_LOGIN = {"/", "/login", "/iniciar-sesion", "/restablecer"}
_RUTAS_PLANES = {"/planes"}
_RUTAS_PAGO = {"/pago", "/pago/exito"}
_RUTAS_PAYWALL = {"/suscripcion", "/activar"}
_RUTAS_PANEL = {
    "/estimado", "/estimate", "/contrato", "/contract",
    "/factura", "/invoice", "/cotizador", "/quote",
    "/historial", "/history",
}


@app.before_request
def _redirigir_login_si_hay_sesion():
    if request.method not in ("GET", "HEAD"):
        return None
    path = request.path.rstrip("/") or "/"
    if path not in {"/", "/login", "/iniciar-sesion"}:
        return None
    if not _token_contratista_solicitud():
        return None
    try:
        contratista, error = _exigir_contratista()
        if not contratista or error:
            return None
        if _prueba_expirada(contratista):
            return redirect("/suscripcion")
        if not _plan_valido(contratista.get("plan_seleccionado")):
            return redirect("/planes")
        return redirect("/estimado")
    except Exception:
        return None


@app.before_request
def _exigir_login_panel():
    if request.method not in ("GET", "HEAD"):
        return None
    path = request.path.rstrip("/") or "/"
    protegidas = _RUTAS_PLANES | _RUTAS_PAGO | _RUTAS_PAYWALL | _RUTAS_PANEL
    if path not in protegidas:
        return None
    if not _token_contratista_solicitud():
        return redirect("/login")
    try:
        contratista, error = _exigir_contratista()
        if contratista and not error:
            if _prueba_expirada(contratista):
                if path not in _RUTAS_PAYWALL:
                    return redirect("/suscripcion")
                return None
            if path in _RUTAS_PAYWALL or path in _RUTAS_PAGO:
                return redirect("/estimado")
    except Exception:
        pass
    return None


@app.before_request
def _bloquear_api_si_expirada():
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    if path.startswith("/api/contratistas") or path.startswith("/api/pagos"):
        return None
    if not _token_contratista_solicitud():
        return None
    try:
        contratista, error = _exigir_contratista()
        if contratista and not error and _prueba_expirada(contratista):
            return _respuesta_prueba_expirada()
    except Exception:
        pass
    return None


def _vista_inicial():
    path = request.path.rstrip("/") or "/"
    token = _token_contratista_solicitud()
    if path == "/restablecer" or not token:
        if path in _RUTAS_LOGIN or not token:
            return "login"
    if path in _RUTAS_LOGIN:
        return "home"
    if path in _RUTAS_PAYWALL:
        return "paywall"
    if path in _RUTAS_PLANES:
        return "planes"
    if path in _RUTAS_PAGO:
        return "pago"
    if path in ("/historial", "/history"):
        return "history"
    nuevo = str(request.args.get("nuevo") or "") == "1"
    if path in ("/estimado", "/estimate") and not request.args.get("id") and not nuevo:
        return "home"
    if path in _RUTAS_PANEL:
        return "quote"
    return "login"


@app.route("/privacidad")
def privacidad():
    return render_template("privacidad.html")


def _industrias_seo():
    from seo_industrias import listar_industrias
    return listar_industrias(_supabase_client())


def _origin_seo():
    return "https://" + DOMINIO_APP


_FAVICON_HEAD = (
    '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">'
    '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">'
)


@app.route("/favicon.ico")
def favicon_ico():
    return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")


@app.route("/robots.txt")
def robots_txt():
    origin = _origin_seo()
    cuerpo = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /*-software\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        f"Sitemap: {origin}/sitemap.xml\n"
    )
    return Response(cuerpo, mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml")
def sitemap_xml():
    from seo_industrias import fecha_lastmod
    origin = _origin_seo()
    lastmod = fecha_lastmod()
    urls = [
        ("/", "weekly", "1.0"),
        ("/privacidad", "yearly", "0.3"),
        ("/login", "weekly", "0.6"),
    ]
    for fila in _industrias_seo():
        urls.append((fila["path"], "weekly", "0.8"))
    partes = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, changefreq, priority in urls:
        loc = xml_escape(origin + path)
        partes.extend([
            "  <url>",
            f"    <loc>{loc}</loc>",
            f"    <lastmod>{lastmod}</lastmod>",
            f"    <changefreq>{changefreq}</changefreq>",
            f"    <priority>{priority}</priority>",
            "  </url>",
        ])
    partes.append("</urlset>")
    xml = "\n".join(partes) + "\n"
    return Response(xml, mimetype="application/xml; charset=utf-8")


_ETIQUETA_ACCION_DEMO = {
    "replace": "Remove and install",
    "replace_material": "Remove, install plus materials",
    "install_material": "Install plus materials",
    "install": "Install only",
    "demo": "Demolition only",
    "material": "Material only",
    "labor": "Install only",
}
_DEMO_ZIP_OBRA = "30327"
_DEMO_DESC_EN = {
    "ZN-COM-TEJ-ROO-FEL": "3-tab, 25-year composition shingle roofing, without felt",
    "ZN-DEM-TEJ-ASF-WAS": "Asphalt shingle tear-off and haul-off (20% waste)",
    "ZN-LIM-MET": "Valley metal",
    "ZN-STA-STR": "Starter strip",
    "ZN-DRY-12-HUN": '1/2" drywall — hung only (no tape or finish)',
    "ZN-DRY-58-HUN": '5/8" drywall — hung only (no tape or finish)',
    "ZN-SND-DRY-ENC-MAS": "Tape, float, and Level 4 finish, ready for paint",
    "ZN-INS-PLO-VAL-ESF": "Ball valve, main water shutoff — supply and install",
    "ZN-INS-PLO-CAL-AGU-5": "Tankless water heater — supply and install",
    "ZN-PLO-DRE-TEN-TUB": "PVC drain and vent piping, Schedule 40",
    "ZN-INS-ELE-TOM-GFC": "20A GFCI receptacle — supply and install",
    "ZN-RPR-ELE-SUS-CAJ": "Replace plastic/metal junction box",
    "ZN-VOL-WIR-COP-GRO-14": "110V 12/2 copper with ground, metal-clad / armored",
    "ZN-INS-HVA-MIN-SPL": "12k Mini-split inverter — complete install",
    "ZN-HVA-LIN-COB-TEN": "Copper line set for mini-split (gas and liquid)",
    "ZN-INS-HVA-TER-MON": "Wi-Fi thermostat — supply, mount, and configure",
    "ZN-PNT-PRE-LAV-HID": "Pressure-wash prep and surface treatment",
    "ZN-PNT-IMP-SEL-FIJ": "One coat acrylic primer / sealer",
    "ZN-PNT-MUR-MAN-LAT-2": "Two coats premium latex on walls",
    "ZN-LOS-VIN-LUJ-LVT": "Luxury vinyl plank, click-lock, residential grade",
    "ZN-DEM-LOS-VIN-LUJ": "Remove existing LVP / LVT click-lock flooring",
    "ZN-DEM-PIS-CER-BAL": "Remove existing ceramic / tile floor",
    "ZN-SID-VIN-REV-042IN": "Vinyl siding, residential 0.040–0.042 in",
    "ZN-SID-FIB-DUE-HOR": 'James HardiePlank 8-1/4" horizontal lap siding',
    "ZN-DEM-SID-VIE-DIS": "Remove existing siding and haul-off debris",
    "ZN-INS-SOF-FIB-LAM": "Fiber-cement soffit panels — supply and install",
    "ZN-SID-TRI-BOA-5": '1" x 8" PVC trim board',
    "ZN-INS-GAB-ANC-STO": "Stock cabinets — install and anchor",
    "ZN-INS-BAC-POR-PIE": "Large-format porcelain backsplash",
    "ZN-DEM-ENC-DES-CUI": "Careful countertop removal for reuse or replacement",
    "ZN-BAN-AZU-PAR-DUC": "Shower or tub surround wall tile",
    "ZN-BAN-SAL-BAC-AZU": "Vanity tile backsplash",
    "ZN-INS-VEN-EXT-BAN": "Standard bath exhaust fan — supply and install",
    "ZN-TIL-BAS": "Tile base / sanitary cove",
    "ZN-INS-PTE-ENT-FIB": "Fiberglass entry door — supply and install",
    "ZN-MOL-ZAP-CLA-NEU": "Base shoe — pneumatic nail, labor only",
    "ZN-INS-MOL-ZOC-REM": "Base cap / decorative baseboard cap",
    "ZN-FRA-MET-RIE-CAN": 'Metal stud track 3-5/8", 25 ga',
    "ZN-FRA-MET-STU-MUR-3": '6" metal stud, 20 ga, wet-wall / facade',
    "ZN-BET-LAR-MAT-ONL-6": '2x6x16 #2 & better fir/larch — material only',
    "ZN-VIN-WIN-AWN": "Vinyl awning window, 7–12 SF",
    "ZN-BAR-FLA-MEM-AUT": "4–6 in self-adhered butyl flashing at openings",
    "ZN-INS-CAN-BAJ-PLU": "Corrugated downspout — supply and install",
    "ZN-DEM-GUT-SEA-ALU": 'Remove 5" seamless aluminum gutters',
    "ZN-INS-GUT-SEA-ALU": 'Install 5" seamless aluminum gutters',
    "ZN-INS-CES-ROL-NAT": "Natural sod rolls (Bermuda or fescue) — supply and install",
    "ZN-ELE-303": "200A breaker panel with arc-fault breakers",
    "ZN-VAC-ACA-CNC-DRI": "Place and finish driveway concrete",
    "ZN-DEM-LOS-CNC-4IN": "Remove existing concrete slab up to 4 in",
    "ZN-INS-CNC-REF-EMP": "3/8 in rebar supply and tying",
    "ZN-INS-JAR-SUE-ACA": "Screened topsoil — haul and spread",
    "ZN-ARB-MUN-DES-MAQ": "Stump grinding with a stump grinder",
    "ZN-DEM-DRY-ACA-LAM": "Drywall tear-out and haul-off",
    "ZN-CON-GEN-GES-COO": "Project management / subcontractor coordination",
    "ZN-CON-GEN-REN-ACA": "Roll-off dumpster rental, haul, and dump fees",
    "ZN-CLN-CON-GEN-FIN": "Final clean and disinfect for owner turnover",
    "ZN-INS-CON-GEN-MUR": "Poly dust-barrier wall",
}


def _descripcion_demo_en(codigo, fallback=""):
    return _DEMO_DESC_EN.get(str(codigo or "").strip()) or str(fallback or "").strip() or str(codigo or "").strip()


def _money_demo(valor):
    return f"${float(valor):,.2f}"


def _qty_demo(valor):
    numero = float(valor)
    if abs(numero - round(numero)) < 1e-9:
        return f"{int(round(numero)):,}"
    return f"{numero:,.2f}"


def _unitario_con_factor_zip(unitario_base, paquete, factor):
    """Misma secuencia que aplicarPrecioAccionEnFila + topePaquete."""
    factor = float(factor)
    unitario = round(float(unitario_base) * factor, 2)
    tope = round(float(paquete) * factor, 2) if float(paquete or 0) > 0 else 0.0
    if tope > 0 and unitario > tope + 0.009:
        return tope
    return unitario


def _fila_demo_catalogo(spec, fila_cat, factor):
    codigo = str((spec or {}).get("codigo") or "").strip()
    accion = str((spec or {}).get("accion") or "install_material").strip()
    qty = float((spec or {}).get("qty") or 0)
    etiqueta = _ETIQUETA_ACCION_DEMO.get(accion, accion)
    base = {
        "codigo": codigo,
        "accion": accion,
        "accion_label": etiqueta,
        "qty": qty,
        "qty_txt": _qty_demo(qty),
        "tax": 0,
        "tax_txt": "0",
        "disponible": False,
        "unit_price": None,
        "unit_price_txt": "Price unavailable",
        "subtotal": 0.0,
        "subtotal_txt": "—",
        "tax_amount": 0.0,
    }
    if not fila_cat or factor is None:
        base["descripcion"] = _descripcion_demo_en(codigo, (fila_cat or {}).get("descripcion"))
        base["unidad"] = str((fila_cat or {}).get("unidad") or (spec or {}).get("unidad") or "")
        return base
    labor, material, remocion = _componentes_desde_columnas(
        fila_cat.get("precio_base"),
        fila_cat.get("precio_material"),
        fila_cat.get("precio_mano_obra"),
        fila_cat.get("precio_remocion"),
        fila_cat.get("categoria") or "",
        fila_cat.get("descripcion") or "",
    )
    paquete = _num(fila_cat.get("precio_base"))
    unitario_base = _precio_roles_por_accion(labor, remocion, material, labor, accion, paquete)
    descripcion = _descripcion_demo_en(codigo, fila_cat.get("descripcion"))
    unidad_cat = str(fila_cat.get("unidad") or "").strip()
    unidad_spec = str((spec or {}).get("unidad") or "").strip()
    unidad = unidad_cat or unidad_spec
    if unidad_spec.upper() == "SQ" and unidad_cat.upper() == "SF":
        unitario_base = round(float(unitario_base) * 100, 2)
        paquete = round(float(paquete) * 100, 2) if paquete else 0.0
        unidad = "SQ"
        if "per square" not in descripcion.lower():
            descripcion = f"{descripcion} — per square (1 SQ = 100 SF)"
    unitario = _unitario_con_factor_zip(unitario_base, paquete, factor)
    base["descripcion"] = descripcion
    base["unidad"] = unidad
    if unitario <= 0:
        return base
    subtotal = round(qty * unitario, 2)
    base.update({
        "disponible": True,
        "unit_price": unitario,
        "unit_price_txt": _money_demo(unitario),
        "subtotal": subtotal,
        "subtotal_txt": _money_demo(subtotal),
        "tax_amount": 0.0,
    })
    return base


def _armar_demo_publica(slug):
    slug = str(slug or "").strip().lower()
    squares = 28.4
    shingle_qty = round(squares * float(WASTE_FACTOR), 2)
    plantillas = {
        "roofing": {
            "trade": "[RFG] Roofing, shingles, and membranes",
            "title": "Roof report reading and priced takeoff",
            "lead": "Sample QuantixBuilds estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Roof / exterior measurement",
            "area_ref": "Sample roof report",
            "metrics": [
                {"value": "28.4", "label": "Squares"},
                {"value": "6/12", "label": "Pitch"},
                {"value": "86 LF", "label": "Ridges"},
                {"value": "42 LF", "label": "Valleys"},
                {"value": "118 LF", "label": "Eaves"},
                {"value": "64 LF", "label": "Rakes"},
                {"value": "15%", "label": "Waste"},
                {"value": "2,840 SF", "label": "Roof area"},
            ],
            "lineas": [
                {"codigo": "ZN-COM-TEJ-ROO-FEL", "accion": "replace_material", "qty": shingle_qty},
                {"codigo": "ZN-DEM-TEJ-ASF-WAS", "accion": "demo", "qty": squares},
                {"codigo": "ZN-LIM-MET", "accion": "install_material", "qty": 42},
                {"codigo": "ZN-STA-STR", "accion": "install_material", "qty": 118},
            ],
        },
        "drywall": {
            "trade": "[DRY] Drywall, tape, and texture",
            "title": "Drywall estimate",
            "lead": "Sample QuantixBuilds estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Living areas — hang and finish",
            "area_ref": "Sample project",
            "metrics": [
                {"value": "1,280 SF", "label": "Walls 1/2\""},
                {"value": "640 SF", "label": "Ceilings 5/8\""},
                {"value": "1,920 SF", "label": "Finish area"},
                {"value": "Level 4", "label": "Finish"},
            ],
            "lineas": [
                {"codigo": "ZN-DRY-12-HUN", "accion": "install_material", "qty": 1280},
                {"codigo": "ZN-DRY-58-HUN", "accion": "install_material", "qty": 640},
                {"codigo": "ZN-SND-DRY-ENC-MAS", "accion": "install", "qty": 1920},
            ],
        },
        "plumbing": {
            "trade": "[PLM] Plumbing, fixtures, and water heaters",
            "title": "Plumbing rough-in and fixture takeoff",
            "lead": "Sample QuantixBuilds plumbing estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Kitchen / water heater — rough-in",
            "area_ref": "Sample plumbing walkthrough",
            "metrics": [
                {"value": "1", "label": "Water heater"},
                {"value": "1", "label": "Shutoff valve"},
                {"value": "48 LF", "label": "Drain / vent"},
            ],
            "lineas": [
                {"codigo": "ZN-INS-PLO-CAL-AGU-5", "accion": "install_material", "qty": 1},
                {"codigo": "ZN-INS-PLO-VAL-ESF", "accion": "install_material", "qty": 1},
                {"codigo": "ZN-PLO-DRE-TEN-TUB", "accion": "install_material", "qty": 48},
            ],
        },
        "electrical": {
            "trade": "[ELE] Electrical, devices, and wiring",
            "title": "200A panel, devices, and circuit takeoff",
            "lead": "Sample QuantixBuilds electrical estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Service panel and kitchen / bath circuits",
            "area_ref": "Sample electrical walkthrough",
            "metrics": [
                {"value": "1", "label": "200A panel"},
                {"value": "4", "label": "GFCI devices"},
                {"value": "2", "label": "Junction boxes"},
                {"value": "86 LF", "label": "12/2 MC"},
            ],
            "lineas": [
                {"codigo": "ZN-ELE-303", "accion": "replace_material", "qty": 1},
                {"codigo": "ZN-INS-ELE-TOM-GFC", "accion": "install_material", "qty": 4},
                {"codigo": "ZN-RPR-ELE-SUS-CAJ", "accion": "install_material", "qty": 2},
                {"codigo": "ZN-VOL-WIR-COP-GRO-14", "accion": "install_material", "qty": 86},
            ],
        },
        "hvac": {
            "trade": "[HVC] HVAC, equipment, and line sets",
            "title": "Mini-split change-out takeoff",
            "lead": "Sample QuantixBuilds HVAC estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Single-zone living area",
            "area_ref": "Sample HVAC walkthrough",
            "metrics": [
                {"value": "1", "label": "12k head"},
                {"value": "25 LF", "label": "Line set"},
                {"value": "1", "label": "Thermostat"},
            ],
            "lineas": [
                {"codigo": "ZN-INS-HVA-MIN-SPL", "accion": "install_material", "qty": 1},
                {"codigo": "ZN-HVA-LIN-COB-TEN", "accion": "install_material", "qty": 25},
                {"codigo": "ZN-INS-HVA-TER-MON", "accion": "install_material", "qty": 1},
            ],
        },
        "painting": {
            "trade": "[PNT] Painting, prep, and finish coats",
            "title": "Interior wall painting takeoff",
            "lead": "Sample QuantixBuilds painting estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Living room and hall walls",
            "area_ref": "Sample painting takeoff",
            "metrics": [
                {"value": "1,680 SF", "label": "Wall area"},
                {"value": "2 coats", "label": "Finish"},
                {"value": "1", "label": "Primer coat"},
            ],
            "lineas": [
                {"codigo": "ZN-PNT-PRE-LAV-HID", "accion": "install_material", "qty": 1680},
                {"codigo": "ZN-PNT-IMP-SEL-FIJ", "accion": "install_material", "qty": 1680},
                {"codigo": "ZN-PNT-MUR-MAN-LAT-2", "accion": "install_material", "qty": 1680},
            ],
        },
        "flooring": {
            "trade": "[FCT] Flooring, LVP, and tile removal",
            "title": "LVP replacement takeoff",
            "lead": "Sample QuantixBuilds flooring estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Main-floor living areas",
            "area_ref": "Sample flooring takeoff",
            "metrics": [
                {"value": "640 SF", "label": "New LVP"},
                {"value": "640 SF", "label": "Tear-out"},
                {"value": "Click-lock", "label": "Product"},
            ],
            "lineas": [
                {"codigo": "ZN-DEM-LOS-VIN-LUJ", "accion": "demo", "qty": 640},
                {"codigo": "ZN-LOS-VIN-LUJ-LVT", "accion": "install_material", "qty": 640},
                {"codigo": "ZN-DEM-PIS-CER-BAL", "accion": "demo", "qty": 80},
            ],
        },
        "siding": {
            "trade": "[SDG] Siding, soffit, and trim",
            "title": "Siding takeoff — SF, squares, soffit, install, and tear-off",
            "lead": "Sample QuantixBuilds siding estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Rear elevation",
            "area_ref": "Sample exterior takeoff",
            "metrics": [
                {"value": "1,120 SF", "label": "Vinyl siding"},
                {"value": "12 SQ", "label": "HardiePlank"},
                {"value": "180 SF", "label": "Soffit"},
                {"value": "64 LF", "label": "Trim labor"},
            ],
            "lineas": [
                {"codigo": "ZN-SID-VIN-REV-042IN", "accion": "install_material", "qty": 1120},
                {"codigo": "ZN-SID-FIB-DUE-HOR", "accion": "install_material", "qty": 12, "unidad": "SQ"},
                {"codigo": "ZN-INS-SOF-FIB-LAM", "accion": "install_material", "qty": 180},
                {"codigo": "ZN-SID-TRI-BOA-5", "accion": "install", "qty": 64},
                {"codigo": "ZN-DEM-SID-VIE-DIS", "accion": "demo", "qty": 1120},
            ],
        },
        "kitchen-remodel": {
            "trade": "[KTR] Kitchen remodel — cabinets and backsplash",
            "title": "Kitchen cabinet and backsplash takeoff",
            "lead": "Sample QuantixBuilds kitchen estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Galley kitchen",
            "area_ref": "Sample kitchen walkthrough",
            "metrics": [
                {"value": "22 LF", "label": "Cabinets"},
                {"value": "28 SF", "label": "Backsplash"},
                {"value": "18 LF", "label": "Countertop removal"},
            ],
            "lineas": [
                {"codigo": "ZN-DEM-ENC-DES-CUI", "accion": "demo", "qty": 18},
                {"codigo": "ZN-INS-GAB-ANC-STO", "accion": "install_material", "qty": 22},
                {"codigo": "ZN-INS-BAC-POR-PIE", "accion": "install_material", "qty": 28},
            ],
        },
        "bathroom-remodel": {
            "trade": "[BTR] Bathroom remodel — tile and ventilation",
            "title": "Bathroom tile and exhaust takeoff",
            "lead": "Sample QuantixBuilds bathroom estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Hall bath",
            "area_ref": "Sample bathroom walkthrough",
            "metrics": [
                {"value": "86 SF", "label": "Shower walls"},
                {"value": "12 SF", "label": "Vanity backsplash"},
                {"value": "1", "label": "Exhaust fan"},
            ],
            "lineas": [
                {"codigo": "ZN-BAN-AZU-PAR-DUC", "accion": "install_material", "qty": 86},
                {"codigo": "ZN-BAN-SAL-BAC-AZU", "accion": "install_material", "qty": 12},
                {"codigo": "ZN-INS-VEN-EXT-BAN", "accion": "install_material", "qty": 1},
            ],
        },
        "tile": {
            "trade": "[TIL] Tile, showers, and base",
            "title": "Shower tile and base takeoff",
            "lead": "Sample QuantixBuilds tile estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Owner bath shower",
            "area_ref": "Sample tile takeoff",
            "metrics": [
                {"value": "72 SF", "label": "Shower walls"},
                {"value": "16 LF", "label": "Tile base"},
                {"value": "8 SF", "label": "Backsplash"},
            ],
            "lineas": [
                {"codigo": "ZN-BAN-AZU-PAR-DUC", "accion": "install_material", "qty": 72},
                {"codigo": "ZN-TIL-BAS", "accion": "install_material", "qty": 16},
                {"codigo": "ZN-BAN-SAL-BAC-AZU", "accion": "install_material", "qty": 8},
            ],
        },
        "carpentry": {
            "trade": "[CRP] Carpentry, doors, and trim",
            "title": "Entry door and trim takeoff",
            "lead": "Sample QuantixBuilds carpentry estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Front entry and hall",
            "area_ref": "Sample carpentry walkthrough",
            "metrics": [
                {"value": "1", "label": "Entry door"},
                {"value": "42 LF", "label": "Base cap"},
                {"value": "42 LF", "label": "Shoe mold"},
            ],
            "lineas": [
                {"codigo": "ZN-INS-PTE-ENT-FIB", "accion": "install_material", "qty": 1},
                {"codigo": "ZN-INS-MOL-ZOC-REM", "accion": "install_material", "qty": 42},
                {"codigo": "ZN-MOL-ZAP-CLA-NEU", "accion": "install", "qty": 42},
            ],
        },
        "framing": {
            "trade": "[FRM] Framing, metal studs, and lumber",
            "title": "Interior wall framing takeoff",
            "lead": "Sample QuantixBuilds framing estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "New partition — wet wall",
            "area_ref": "Sample framing takeoff",
            "metrics": [
                {"value": "48 LF", "label": "Track"},
                {"value": "96 LF", "label": "Studs"},
                {"value": "8", "label": "2x6 material"},
            ],
            "lineas": [
                {"codigo": "ZN-FRA-MET-RIE-CAN", "accion": "install_material", "qty": 48},
                {"codigo": "ZN-FRA-MET-STU-MUR-3", "accion": "install_material", "qty": 96},
                {"codigo": "ZN-BET-LAR-MAT-ONL-6", "accion": "material", "qty": 8},
            ],
        },
        "windows": {
            "trade": "[WND] Windows and opening flashing",
            "title": "Window replacement takeoff",
            "lead": "Sample QuantixBuilds window estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Rear elevation openings",
            "area_ref": "Sample window count",
            "metrics": [
                {"value": "6", "label": "Vinyl awning"},
                {"value": "48 LF", "label": "Opening flashing"},
            ],
            "lineas": [
                {"codigo": "ZN-VIN-WIN-AWN", "accion": "install_material", "qty": 6},
                {"codigo": "ZN-BAR-FLA-MEM-AUT", "accion": "install_material", "qty": 48},
            ],
        },
        "gutters": {
            "trade": "[GUT] Gutters and downspouts",
            "title": "Seamless gutter and downspout takeoff",
            "lead": "Sample QuantixBuilds gutter estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Eaves and downspouts",
            "area_ref": "Sample exterior takeoff",
            "metrics": [
                {"value": "118 LF", "label": "Seamless gutter"},
                {"value": "36 LF", "label": "Downspout"},
            ],
            "lineas": [
                {"codigo": "ZN-DEM-GUT-SEA-ALU", "accion": "demo", "qty": 118},
                {"codigo": "ZN-INS-GUT-SEA-ALU", "accion": "install_material", "qty": 118},
                {"codigo": "ZN-INS-CAN-BAJ-PLU", "accion": "install_material", "qty": 36},
            ],
        },
        "concrete": {
            "trade": "[CNC] Concrete, slabs, and driveways",
            "title": "Driveway replacement takeoff",
            "lead": "Sample QuantixBuilds concrete estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Front driveway",
            "area_ref": "Sample concrete takeoff",
            "metrics": [
                {"value": "480 SF", "label": "New pour"},
                {"value": "480 SF", "label": "Tear-out"},
                {"value": "96 LF", "label": "Rebar"},
            ],
            "lineas": [
                {"codigo": "ZN-DEM-LOS-CNC-4IN", "accion": "demo", "qty": 480},
                {"codigo": "ZN-INS-CNC-REF-EMP", "accion": "install_material", "qty": 96},
                {"codigo": "ZN-VAC-ACA-CNC-DRI", "accion": "install_material", "qty": 480},
            ],
        },
        "landscaping": {
            "trade": "[LND] Landscaping — soil, sod, and stump work",
            "title": "Yard soil, sod, and stump takeoff",
            "lead": "Sample QuantixBuilds landscaping estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Front yard",
            "area_ref": "Sample landscape walkthrough",
            "metrics": [
                {"value": "6 CY", "label": "Topsoil"},
                {"value": "800 SF", "label": "Sod"},
                {"value": "2", "label": "Stumps"},
            ],
            "lineas": [
                {"codigo": "ZN-INS-JAR-SUE-ACA", "accion": "install_material", "qty": 6},
                {"codigo": "ZN-INS-CES-ROL-NAT", "accion": "install_material", "qty": 800},
                {"codigo": "ZN-ARB-MUN-DES-MAQ", "accion": "install_material", "qty": 2},
            ],
        },
        "demolition": {
            "trade": "[DEM] Demolition, tear-out, and haul-off",
            "title": "Interior selective demo takeoff",
            "lead": "Sample QuantixBuilds demolition estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Kitchen and hall",
            "area_ref": "Sample demo walkthrough",
            "metrics": [
                {"value": "320 SF", "label": "Drywall tear-out"},
                {"value": "180 SF", "label": "Tile floor tear-out"},
                {"value": "1", "label": "Dumpster"},
            ],
            "lineas": [
                {"codigo": "ZN-DEM-DRY-ACA-LAM", "accion": "demo", "qty": 320},
                {"codigo": "ZN-DEM-PIS-CER-BAL", "accion": "demo", "qty": 180},
                {"codigo": "ZN-CON-GEN-REN-ACA", "accion": "install_material", "qty": 1},
            ],
        },
        "remodeling": {
            "trade": "[RMD] Multi-trade residential remodel",
            "title": "Small remodel — cabinets, drywall, paint",
            "lead": "Sample QuantixBuilds remodeling estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Kitchen wall and cabinet package",
            "area_ref": "Sample remodel walkthrough",
            "metrics": [
                {"value": "14 LF", "label": "Cabinets"},
                {"value": "240 SF", "label": "Hang drywall"},
                {"value": "240 SF", "label": "Paint"},
            ],
            "lineas": [
                {"codigo": "ZN-INS-GAB-ANC-STO", "accion": "install_material", "qty": 14},
                {"codigo": "ZN-DRY-12-HUN", "accion": "install_material", "qty": 240},
                {"codigo": "ZN-PNT-MUR-MAN-LAT-2", "accion": "install_material", "qty": 240},
            ],
        },
        "general-contractor": {
            "trade": "[GC] General conditions and job coordination",
            "title": "General conditions takeoff",
            "lead": "Sample QuantixBuilds GC estimate. Quantities are examples. Unit prices, actions, and totals use the same catalog engine as the live estimator.",
            "area_title": "Two-week residential job",
            "area_ref": "Sample GC conditions",
            "metrics": [
                {"value": "2 WK", "label": "PM / coordination"},
                {"value": "1", "label": "Dumpster"},
                {"value": "1,200 SF", "label": "Final clean"},
            ],
            "lineas": [
                {"codigo": "ZN-CON-GEN-GES-COO", "accion": "install_material", "qty": 2},
                {"codigo": "ZN-CON-GEN-REN-ACA", "accion": "install_material", "qty": 1},
                {"codigo": "ZN-INS-CON-GEN-MUR", "accion": "install_material", "qty": 40},
                {"codigo": "ZN-CLN-CON-GEN-FIN", "accion": "install_material", "qty": 1200},
            ],
        },
    }
    plantilla = plantillas.get(slug)
    if not plantilla:
        return None
    zona = _zona_mercado_por_zip(_DEMO_ZIP_OBRA)
    factor = round(float(zona.get("factor")), 2) if zona.get("exito") else None
    ciudad = str(zona.get("ciudad") or "").strip()
    estado = str(zona.get("estado") or "").strip()
    zip_code = str(zona.get("zip") or _DEMO_ZIP_OBRA).strip()
    zona_nombre = str(zona.get("zona") or "").strip()
    mercado = str(zona.get("mercado") or "").strip()
    factor_validado = bool(zona.get("validado"))
    if factor_validado:
        factor_fuente_txt = "Confirmed Atlanta base market"
    elif zona.get("clase") == "indice_interno_supabase":
        factor_fuente_txt = "Internal index — not commercially validated"
    else:
        factor_fuente_txt = "Documented fallback 1.00 — no validated regional index"
    partes_ubi = [parte for parte in (ciudad, estado) if parte]
    ubicacion = ", ".join(partes_ubi)
    if zip_code:
        ubicacion = f"{ubicacion} {zip_code}".strip() if ubicacion else zip_code
    codigos = [linea["codigo"] for linea in plantilla["lineas"]]
    catalogo = _filas_catalogo_por_codigos(codigos) if factor is not None else {}
    filas = []
    for linea in plantilla["lineas"]:
        fila_cat = catalogo.get(linea["codigo"]) or catalogo.get(str(linea["codigo"]).upper())
        filas.append(_fila_demo_catalogo(linea, fila_cat, factor))
    subtotal = round(sum(float(fila["subtotal"]) for fila in filas), 2)
    tax_total = round(sum(float(fila["tax_amount"]) for fila in filas), 2)
    overhead = round(subtotal * 0.10, 2)
    profit = round(subtotal * 0.10, 2)
    grand = round(subtotal + tax_total + overhead + profit, 2)
    return {
        "slug": slug,
        "trade": plantilla["trade"],
        "title": plantilla["title"],
        "lead": plantilla["lead"],
        "area_title": plantilla["area_title"],
        "area_ref": plantilla["area_ref"],
        "metrics": plantilla.get("metrics") or [],
        "filas": filas,
        "ciudad": ciudad,
        "estado": estado,
        "zip": zip_code,
        "zona": zona_nombre,
        "mercado": mercado,
        "factor": factor,
        "factor_validado": factor_validado,
        "factor_fuente": zona.get("fuente"),
        "factor_clase": zona.get("clase"),
        "ubicacion": ubicacion,
        "factor_txt": f"×{factor:.2f}" if factor is not None else "unavailable",
        "factor_fuente_txt": factor_fuente_txt,
        "subtotal_txt": _money_demo(subtotal),
        "tax_total_txt": _money_demo(tax_total),
        "overhead_txt": _money_demo(overhead),
        "profit_txt": _money_demo(profit),
        "grand_txt": _money_demo(grand),
        "catalogo_notas": plantilla.get("catalogo_notas") or "",
    }


def _snapshot_demo_estimado(slug):
    try:
        return _armar_demo_publica(slug)
    except Exception as err:
        print(f"--> demo publica {slug}: {err}")
        return None


@app.route("/<industry>-software")
def landing_industria(industry):
    from seo_industrias import industria_por_slug, relacionadas
    sb = _supabase_client()
    fila = industria_por_slug(industry, sb)
    if not fila:
        return Response("Page not found", status=404, mimetype="text/plain; charset=utf-8")
    canonical = _origin_seo() + fila["path"]
    json_ld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "SoftwareApplication",
                "name": f"QuantixBuilds {fila['name']} Software",
                "applicationCategory": "BusinessApplication",
                "operatingSystem": "Web",
                "url": canonical,
                "description": fila["meta"],
                "featureList": fila["keywords"],
                "offers": {
                    "@type": "Offer",
                    "price": "0",
                    "priceCurrency": "USD",
                    "description": "14-day free trial, no card required",
                },
                "provider": {
                    "@type": "Organization",
                    "name": "QuantixBuilds",
                    "url": _origin_seo(),
                },
            },
            {
                "@type": "WebPage",
                "url": canonical,
                "name": fila["title"],
                "description": fila["meta"],
                "inLanguage": "en-US",
            },
        ],
    }, ensure_ascii=False)
    return render_template(
        "industria.html",
        industria=fila,
        canonical=canonical,
        relacionadas=relacionadas(fila["slug"], 19, sb),
        json_ld=json_ld,
        demo=_snapshot_demo_estimado(fila.get("slug")),
    )


@app.route("/")
@app.route("/login")
@app.route("/iniciar-sesion")
@app.route("/restablecer")
@app.route("/planes")
@app.route("/pago")
@app.route("/pago/exito")
@app.route("/suscripcion")
@app.route("/activar")
@app.route("/estimado")
@app.route("/estimate")
@app.route("/contrato")
@app.route("/contract")
@app.route("/factura")
@app.route("/invoice")
@app.route("/cotizador")
@app.route("/quote")
@app.route("/historial")
@app.route("/history")
def home():
    nombre = _nombre_empresa_sesion()
    vista = _vista_inicial()
    industrias_seo = []
    if vista == "login":
        from seo_industrias import listar_industrias
        try:
            industrias_seo = list(listar_industrias(_supabase_client()) or ())
        except Exception:
            industrias_seo = []
        if not industrias_seo:
            from seo_industrias import listar_industrias as _listar_json
            industrias_seo = list(_listar_json(None) or ())
    return render_template(
        "index.html",
        nombre_empresa=nombre,
        nombre_empresa_visible=nombre or "Your company",
        vista=vista,
        industrias_seo=industrias_seo,
    )


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


def _codigo_zona_fila(row):
    return str(_campo_factor(row, "codigo_zona", "zona_codigo") or "").strip().upper()


def _zip_de_fila_regional(row):
    crudo = str(_campo_factor(row, "zip", "zip_code", "codigo_postal", "postal_code") or "")
    zip5 = "".join(ch for ch in crudo if ch.isdigit())[:5]
    if len(zip5) == 5:
        return zip5
    zona = _codigo_zona_fila(row)
    if len(zona) == 5 and zona.isdigit():
        return zona
    return ""


def _prefijo_de_fila_regional(row):
    zona = _codigo_zona_fila(row)
    if len(zona) == 3 and zona.isdigit():
        return zona
    return ""


def _estado_de_fila_regional(row):
    estado = str(_campo_factor(row, "estado", "state", "state_code", "st") or "").strip().upper()
    if len(estado) > 2:
        estado = estado[:2]
    if len(estado) == 2 and estado.isalpha():
        return estado
    zona = _codigo_zona_fila(row)
    if len(zona) == 2 and zona.isalpha():
        return zona
    return ""


def _es_default_estado(row):
    if not _zip_de_fila_regional(row):
        return True
    marca = _campo_factor(row, "es_default", "default", "is_default", "base_estado")
    if isinstance(marca, str):
        marca = marca.strip().lower()
    return marca in (True, 1, "1", "t", "true", "si", "sí", "yes")


_FACTORES_CACHE = None


def _filas_factores_regionales():
    global _FACTORES_CACHE
    if _FACTORES_CACHE is not None:
        return _FACTORES_CACHE
    if supabase is not None:
        try:
            res = supabase.table(TABLA_FACTORES).select("*").limit(2000).execute()
            filas = list(res.data or [])
            print(f"--> {TABLA_FACTORES}: {len(filas)} registros")
            if filas:
                print(f"--> Columnas {TABLA_FACTORES}: {list(filas[0].keys())}")
                _FACTORES_CACHE = filas
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
        if isinstance(filas, list) and filas:
            _FACTORES_CACHE = filas
            return filas
        return []
    except Exception as e:
        print(f"ERROR {TABLA_FACTORES} REST: {e}")
        return []


def _es_mercado_base_atlanta(zip_code):
    zip5 = "".join(ch for ch in str(zip_code or "") if ch.isdigit())[:5]
    if zip5 in ZIPS_MERCADO_BASE_ATLANTA:
        return True
    return len(zip5) == 5 and zip5[:3] in PREFIXES_MERCADO_BASE_ATLANTA


def _meta_factor_interno(row, nivel):
    factor = _factor_de_fila_regional(row)
    if not factor:
        return None
    nombre = str(row.get("nombre_zona") or "").strip() or None
    codigo = _codigo_zona_fila(row)
    return {
        "factor": factor,
        "codigo_zona": codigo,
        "nombre_zona": nombre,
        "nivel": nivel,
        "fuente": "factores_regionales_usa",
        "validado": False,
        "clase": "indice_interno_supabase",
    }


def _factor_desde_supabase(zip_code, estado):
    """Devuelve metadatos del factor interno. No implica validación comercial."""
    filas = _filas_factores_regionales()
    if not filas:
        return None
    zip5 = "".join(ch for ch in str(zip_code or "") if ch.isdigit())[:5]
    prefijo = zip5[:3] if len(zip5) == 5 else ""
    estado = str(estado or "").strip().upper()[:2]
    for row in filas:
        if len(zip5) == 5 and _zip_de_fila_regional(row) == zip5:
            meta = _meta_factor_interno(row, "zip")
            if meta:
                print(f"--> Factor ZIP {zip5} en Supabase: {meta['factor']} ({meta.get('nombre_zona') or zip5}, interno)")
                return meta
    if prefijo:
        for row in filas:
            if _prefijo_de_fila_regional(row) == prefijo:
                meta = _meta_factor_interno(row, "prefijo")
                if meta:
                    print(f"--> Factor prefijo {prefijo} en Supabase: {meta['factor']} ({meta.get('nombre_zona') or prefijo}, interno)")
                    return meta
    if estado:
        for row in filas:
            if _estado_de_fila_regional(row) == estado:
                meta = _meta_factor_interno(row, "estado")
                if meta:
                    print(f"--> Factor estado {estado} en Supabase: {meta['factor']} ({meta.get('nombre_zona') or estado}, interno)")
                    return meta
    return None


def _zona_mercado_por_zip(zip_code):
    zip_limpio = "".join(ch for ch in str(zip_code or "") if ch.isdigit())[:5]
    zona = {
        "zip": zip_limpio,
        "ciudad": None,
        "estado": None,
        "zona": None,
        "factor": FACTOR_RESPALDO,
        "fuente": "fallback",
        "mercado": None,
        "codigo_zona": None,
        "nivel": None,
        "validado": False,
        "clase": "respaldo_sin_dato",
        "exito": False,
    }
    if len(zip_limpio) != 5:
        return zona
    motor = _motor_uszipcode()
    try:
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

    if _es_mercado_base_atlanta(zip_limpio):
        zona["factor"] = FACTOR_MERCADO_BASE
        zona["fuente"] = "atlanta_base"
        zona["mercado"] = "Atlanta"
        zona["codigo_zona"] = zip_limpio[:3]
        zona["nivel"] = "mercado_base"
        zona["validado"] = True
        zona["clase"] = "mercado_base_confirmado"
        zona["ciudad"] = zona["ciudad"] or "Atlanta"
        zona["estado"] = zona["estado"] or "GA"
        if zip_limpio == "30327":
            zona["zona"] = zona["zona"] or "Buckhead"
        zona["exito"] = True
        return zona

    meta = _factor_desde_supabase(zip_limpio, zona.get("estado"))
    if meta is not None:
        zona["factor"] = round(float(meta["factor"]), 4)
        zona["fuente"] = "factores_regionales_usa"
        zona["mercado"] = meta.get("nombre_zona")
        zona["codigo_zona"] = meta.get("codigo_zona")
        zona["nivel"] = meta.get("nivel")
        zona["validado"] = False
        zona["clase"] = "indice_interno_supabase"
        zona["zona"] = zona.get("zona") or meta.get("nombre_zona")
        zona["exito"] = True
        return zona

    zona["factor"] = FACTOR_RESPALDO
    zona["fuente"] = "fallback"
    zona["validado"] = False
    zona["clase"] = "respaldo_sin_dato"
    zona["exito"] = True
    return zona


def _referencia_factor(zona):
    """Texto honesto de procedencia. No presenta índices internos como mercado validado."""
    clase = str((zona or {}).get("clase") or "")
    mercado = str((zona or {}).get("mercado") or "").strip()
    codigo = str((zona or {}).get("codigo_zona") or "").strip()
    nivel = str((zona or {}).get("nivel") or "").strip()
    if clase == "mercado_base_confirmado":
        return "Atlanta catalog base (confirmed). Factor 1.00. No extra surcharge."
    if clase == "indice_interno_supabase":
        detalle = " ".join(parte for parte in (nivel, codigo) if parte).strip()
        nombre = mercado or detalle or "regional row"
        return (
            f"Internal Supabase index ({nombre}). "
            "Not a commercially validated market index. Table has no source or date."
        )
    return "Atlanta catalog base 1.00 — no verified local or regional index for this ZIP."


@app.route("/api/get-zip-factor/<zip_code>")
def get_zip_factor(zip_code):
    zona = _zona_mercado_por_zip(zip_code)
    if not zona.get("exito"):
        return jsonify({
            "ciudad": None,
            "estado": None,
            "zip": zona.get("zip") or "",
            "factor": FACTOR_RESPALDO,
            "fuente": "fallback",
            "validado": False,
            "clase": "respaldo_sin_dato",
            "referencia": "Invalid ZIP. Enter exactly five digits. Using Atlanta catalog base 1.00.",
            "exito": False,
        }), 400
    payload = {
        "ciudad": zona.get("ciudad"),
        "estado": zona.get("estado"),
        "zip": zona.get("zip"),
        "factor": round(float(zona.get("factor") if zona.get("factor") is not None else FACTOR_RESPALDO), 2),
        "fuente": zona.get("fuente") or "fallback",
        "mercado": zona.get("mercado"),
        "codigo_zona": zona.get("codigo_zona"),
        "nivel": zona.get("nivel"),
        "validado": bool(zona.get("validado")),
        "clase": zona.get("clase") or "respaldo_sin_dato",
        "referencia": _referencia_factor(zona),
        "exito": True,
    }
    if zona.get("zona"):
        payload["zona"] = zona["zona"]
    print(
        f"--> ZIP {zona.get('zip')}: ciudad={payload['ciudad']} estado={payload['estado']} "
        f"factor={payload['factor']} fuente={payload['fuente']} mercado={payload.get('mercado')} "
        f"validado={payload['validado']}"
    )
    return jsonify(payload)

client = _crear_cliente_gemini(os.environ.get("GCP_API_KEY") or os.environ.get("GEMINI_API_KEY"))

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

PROMPT_IMPORT_LINE_ITEMS = (
    "Eres un estimador experto de Solid Remodeling & Reconstruction LLC. "
    "Analiza este texto de un presupuesto en PDF. Extrae todas las partidas "
    "(ignorando totales generales) y adáptalas a nuestro formato de SCOPE OF WORK. "
    "You MUST extract all line item descriptions EXACTLY in the original language of the PDF document. Do NOT translate English terms to Spanish under any circumstances, even if requested by other language variables. Maintain the original terminology intact. "
    "Devuelve ÚNICAMENTE un JSON válido que sea un arreglo de objetos. "
    "Cada objeto debe tener exactamente estas claves: CODE, ACTION, WORK DESCRIPTION, "
    "QTY, UNIT, UNIT PRICE, y SUBTOTAL."
)

PROMPT_PDF = """
Analiza este PDF de construcción.
You MUST extract all line item descriptions EXACTLY in the original language of the PDF document. Do NOT translate English terms to Spanish under any circumstances, even if requested by other language variables. Maintain the original terminology intact.

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

SYSTEM_PROMPT_PLANOS = (
    "Eres un medidor de planos (takeoff). Solo extraes medidas por cuarto. "
    "Lees cotas y la escala dibujada. No inventes cuartos. "
    "Altura de pared = 8 ft si el plano no dice otra. "
    "You MUST extract all line item descriptions EXACTLY in the original language of the PDF document. Do NOT translate English terms to Spanish under any circumstances, even if requested by other language variables. Maintain the original terminology intact. "
    "Responde solo JSON válido, sin markdown."
)

PROMPT_PLANOS_VISION = """De estas láminas saca SOLO medidas, cuarto por cuarto.

Por cada cuarto visible:
- room_name
- floor_sf: área de piso
- ceiling_sf: área de cielo (igual al piso si no hay doble altura)
- wall_sf: perímetro x altura (8 ft si no hay otra cota)
- wall_and_ceiling_sf: wall_sf + ceiling_sf
- trim_lf: perímetro (zócalo)
- cabinets_lf: pies lineales de gabinetes solo en cocina, baño o laundry; si no hay, 0

JSON obligatorio, números en pies, 2 decimales:
{"rooms":[{"room_name":"Kitchen","floor_sf":168,"ceiling_sf":168,"wall_sf":416,"wall_and_ceiling_sf":584,"trim_lf":52,"cabinets_lf":0,"source_page":1}],"grand_totals":{"floor_sf":168,"ceiling_sf":168,"wall_sf":416,"wall_and_ceiling_sf":584,"trim_lf":52,"cabinets_lf":0}}
grand_totals es la suma de todos los cuartos. Si no hay planta, rooms=[].
"""

PLANOS_MAX_PAGINAS = 4
PLANOS_LOTE_PAGINAS = 4
PLANOS_DPI = 120

PROMPT_ROOF_REPORT = """
Este PDF es un ROOF REPORT de MEDICIONES (Roofr, EagleView, Hover, GAF QuickMeasure u otro).
NO es un estimado: no trae precios. Tú lees el TEXTO línea por línea.
You MUST extract all line item descriptions EXACTLY in the original language of the PDF document. Do NOT translate English terms to Spanish under any circumstances, even if requested by other language variables. Maintain the original terminology intact.

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
    {"item":2,"trade":"Roofing","description":"Instalación de teja arquitectónica (con waste 15%)","quantity":52.3,"unit":"SQ","unit_price":null,"total":null,"action":"install_material"},
    {"item":3,"trade":"Roofing","description":"Starter Strip","quantity":366,"unit":"LF","unit_price":null,"total":null,"action":"install_material"}
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
    from roof_parser import accion_exacta_item
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
        "action": accion_exacta_item(description),
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
    return _idioma_de_texto(texto) == "en"


_LEMAS_BUSQUEDA_ES = {
    "piso", "pisos", "pizos", "pared", "paredes", "cielo", "pintura", "pintur",
    "gabinete", "gabinetes", "gabinet", "teja", "techo", "puerta", "puertas",
    "ventana", "ventanas", "alfombra", "tablaroca", "yeso", "moldura", "zocalo",
    "azulejo", "ceramica", "baldosa", "baldosas", "porcelanato", "ducha", "bano",
    "cocina", "revestimiento",
}
_LEMAS_BUSQUEDA_EN = {
    "floor", "floors", "flooring", "flor", "wall", "walls", "wal", "ceiling",
    "paint", "painting", "cabinet", "cabinets", "roof", "roofing", "door", "doors",
    "window", "windows", "carpet", "drywall", "drywal", "trim", "baseboard",
    "siding", "tile", "tiles", "shingle", "hardwood", "vinyl", "laminate", "plank",
    "backsplash", "travertine", "bathroom", "shower", "kitchen",
}


def _idioma_de_texto(texto):
    t = str(texto or "").strip()
    if not t:
        return ""
    if re.search(r"[áéíóúñüÁÉÍÓÚÑÜ¿¡]", t):
        return "es"
    bajo = _sin_acentos(t)
    es = len(_PALABRAS_ES_DOC.findall(t))
    en = len(_PALABRAS_EN_DOC.findall(t))
    if re.search(r"\b(de|del|para|con|sin|sobre|mano|aplicacion|reparacion|reemplazo|instalacion|paredes|cielos|pisos)\b", bajo):
        es += 1
    if es > en:
        return "es"
    if en > es and not es:
        return "en"
    if es:
        return "es"
    if re.search(r"\b(the|and|with|for|grade|finish|install|removal|panel|molding)\b", bajo):
        return "en"
    return "en"


def _idioma_de_token_busqueda(plano):
    """El idioma lo marca la palabra que escribió, no toda la familia mezclada."""
    if plano in _LEMAS_BUSQUEDA_ES:
        return "es"
    if plano in _LEMAS_BUSQUEDA_EN:
        return "en"
    grupos = _familias_de_token(plano)
    if not grupos:
        return ""
    palabras = [_sin_acentos(p) for p in grupos[0] if p]
    if not palabras:
        return ""
    if plano in palabras:
        elegido = plano
    else:
        elegido = min(palabras, key=lambda p: _distancia_token(plano, p))
    if elegido in _LEMAS_BUSQUEDA_ES:
        return "es"
    if elegido in _LEMAS_BUSQUEDA_EN:
        return "en"
    return ""


def _idioma_de_busqueda(q):
    texto = _corregir_typos_catalogo(q)
    if re.search(r"[áéíóúñüÁÉÍÓÚÑÜ¿¡]", texto):
        return "es"
    es = en = 0
    for token in re.findall(r"[a-zA-ZáéíóúñÁÉÍÓÚÑ]{3,}", str(texto or "").lower()):
        lang = _idioma_de_token_busqueda(_sin_acentos(token))
        if lang == "es":
            es += 2
        elif lang == "en":
            en += 2
    if es > en:
        return "es"
    if en > es:
        return "en"
    return ""


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
        try:
            _alerta_critica("conexion_supabase", detalle=e, conexion="cliente_runtime")
        except Exception:
            pass
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
        (r"\b(?:tiel|tyle|tille|tils|teail|taile)\b", "tile"),
        (r"\bbaldosas?\b", "baldosa"),
        (r"\bpizos\b", "pisos"),
        (r"\bpiso[sz]\b", "pisos"),
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


def _url_imagen_catalogo(row):
    from home_depot_apify import sanitizar_url, url_imagen_home_depot

    if not isinstance(row, dict):
        return ""
    for key in ("image_url", "imageUrl", "imagen_url", "url_imagen", "foto_url", "thumbnail", "thumbnailUrl"):
        url = sanitizar_url(row.get(key), imagen=True)
        if url:
            return url
    return url_imagen_home_depot(row) or ""


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
        "precio_demo": round(_num_catalogo(row, "precio_remocion", "precio_demo", "demo", "precio_demolicion", "tear_off"), 2),
        "precio_remocion": round(_num_catalogo(row, "precio_remocion"), 2),
        "priceLabor": round(_num_catalogo(row, "precio_labor", "labor", "precio_mano_obra", "labor_price"), 2),
        "priceMaterial": round(_num_catalogo(row, "precio_material", "material", "material_price", "precio_mat"), 2),
        "priceDemo": round(_num_catalogo(row, "precio_remocion", "precio_demo", "demo", "precio_demolicion", "tear_off"), 2),
        "precio_mano_obra": round(_num_catalogo(row, "precio_mano_obra", "precio_labor", "labor"), 2),
        "alcance": _alcance_exclusivo_descripcion(desc_ui) or "completo",
        "fuente": "supabase",
        "image_url": _url_imagen_catalogo(row),
        "imageUrl": _url_imagen_catalogo(row),
        "last_updated": row.get("last_updated") or "",
        "proveedor": _texto_catalogo(row, "proveedor", "supplier", "vendor", "retailer"),
        "product_url": _texto_catalogo(row, "product_url", "productUrl", "url_producto"),
        "productUrl": _texto_catalogo(row, "product_url", "productUrl", "url_producto"),
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
        params = {"select": COLUMNAS_CATALOGO, "activo": "eq.true", "limit": str(limite)}
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
                params = {"select": COLUMNAS_CATALOGO, "activo": "eq.true", "limit": str(limite), "descripcion": f"ilike.{like}"}
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
            query = client.table(TABLA_CATALOGO).select(COLUMNAS_CATALOGO).eq("activo", True)
            if termino:
                compacto = _codigo_catalogo_compacto(termino)
                filtro = f"descripcion.ilike.%{termino}%,codigo.ilike.%{termino}%"
                if compacto and compacto.lower() != termino.lower():
                    filtro += f",codigo.ilike.%{compacto}%"
                query = query.or_(filtro)
            data = query.limit(limite).execute()
            return list(data.data or [])
        except Exception as err:
            if _ajustar_columnas_catalogo(err):
                return buscar_filas_catalogo(q)
            try:
                query = client.table(TABLA_CATALOGO).select(COLUMNAS_CATALOGO).eq("activo", True)
                if termino:
                    query = query.ilike("descripcion", f"%{termino}%")
                data = query.limit(limite).execute()
                return list(data.data or [])
            except Exception as err2:
                if _ajustar_columnas_catalogo(err2):
                    return buscar_filas_catalogo(q)
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
    "out", "off",
}
_SINONIMOS_DEMOLICION = (
    "remover", "remove", "removal", "remocion", "quitar", "tear", "tearoff", "demo",
    "demoler", "demolicion", "demolition", "arrancar", "retiro", "retirar", "desmontar",
    "desinstalar", "pull", "rip", "haul", "raspar", "raspado", "decapar", "decapado", "stripping",
)
_SINONIMOS_BUSQUEDA = {
    "remover": _SINONIMOS_DEMOLICION,
    "quitar": _SINONIMOS_DEMOLICION,
    "arrancar": _SINONIMOS_DEMOLICION,
    "retiro": _SINONIMOS_DEMOLICION,
    "retirar": _SINONIMOS_DEMOLICION,
    "remove": _SINONIMOS_DEMOLICION,
    "tear": _SINONIMOS_DEMOLICION,
    "demoler": _SINONIMOS_DEMOLICION,
    "demo": _SINONIMOS_DEMOLICION,
    "instalar": ("instalar", "install", "instalacion", "installation", "replace", "reemplazo", "poner", "colocar"),
    "pintar": ("pintar", "pintura", "paint", "painting", "primer", "esmalte"),
    "material": ("material", "materials", "suministro", "mat", "supply"),
    "eaves": ("eaves", "eave", "starter", "alero"),
    "valleys": ("valleys", "valley", "limahoya"),
    "hips": ("hips", "hip"),
    "ridges": ("ridges", "ridge cap", "shingle ridge", "cumbrera", "continuous ridge"),
    "rakes": ("rakes", "rake"),
    "squares": ("squares", "square", "escuadra", "teja", "shingle"),
    "flashing": ("flashing", "tapajuntas"),
    "ceramica": (
        "ceramica", "azulejo", "porcelanato", "baldosa", "baldosas", "tile", "tiles",
        "backsplash", "travertine", "shower", "bathroom", "bano", "cocina", "kitchen",
    ),
    "azulejo": (
        "azulejo", "ceramica", "porcelanato", "baldosa", "baldosas", "tile", "tiles",
        "backsplash", "travertine", "shower", "bathroom", "bano", "cocina", "kitchen",
    ),
    "baldosa": (
        "baldosa", "baldosas", "ceramica", "azulejo", "porcelanato", "tile", "tiles",
        "backsplash", "travertine", "shower", "bathroom", "bano", "cocina", "kitchen",
    ),
    "tile": (
        "tile", "tiles", "baldosa", "baldosas", "ceramica", "azulejo", "porcelanato",
        "backsplash", "travertine", "shower", "bathroom", "bano", "cocina", "kitchen",
    ),
    "bathroom": (
        "bathroom", "bano", "shower", "tile", "baldosa", "ceramica", "backsplash", "travertine",
    ),
    "bano": (
        "bano", "bathroom", "ducha", "shower", "tile", "baldosa", "ceramica", "backsplash",
    ),
    "cocina": (
        "cocina", "kitchen", "backsplash", "baldosa", "tile", "ceramica", "porcelanato",
    ),
    "backsplash": (
        "backsplash", "baldosa", "tile", "ceramica", "porcelanato", "cocina", "kitchen",
    ),
    "floor": ("floor", "flooring", "piso", "pisos", "lvt", "lvp", "vinyl", "hardwood", "laminate", "carpet", "alfombra", "spc"),
    "piso": ("piso", "pisos", "floor", "flooring", "lvt", "lvp", "vinyl", "hardwood", "laminate", "carpet", "alfombra", "spc"),
    "pisos": ("pisos", "piso", "floor", "flooring", "lvt", "lvp", "vinyl", "hardwood", "laminate", "carpet", "alfombra"),
    "ceiling": ("ceiling", "cielo", "drywall", "tablaroca", "pintura", "paint"),
    "cielo": ("cielo", "ceiling", "drywall", "tablaroca", "pintura"),
    "wall": ("wall", "walls", "pared", "paredes", "drywall", "tablaroca", "pintura", "paint"),
    "walls": ("walls", "wall", "pared", "drywall", "pintura", "paint"),
    "pared": ("pared", "paredes", "wall", "drywall", "tablaroca", "pintura"),
    "trim": ("trim", "baseboard", "zocalo", "moldura", "molding", "casings"),
    "baseboard": ("baseboard", "trim", "zocalo", "moldura", "molding"),
    "cabinet": ("cabinet", "cabinets", "gabinete", "gabinetes"),
    "cabinets": ("cabinets", "cabinet", "gabinete", "gabinetes"),
    "gabinete": ("gabinete", "gabinetes", "cabinet", "cabinets"),
    "pintura": ("pintura", "paint", "painting", "primer", "esmalte"),
    "paint": ("paint", "painting", "pintura", "primer"),
    "drywall": ("drywall", "tablaroca", "sheetrock", "yeso"),
    "tablaroca": ("tablaroca", "drywall", "sheetrock", "yeso"),
    "techo": ("techo", "roof", "roofing", "shingle", "teja"),
    "roof": ("roof", "roofing", "techo", "shingle", "teja"),
    "puerta": ("puerta", "puertas", "door", "doors"),
    "door": ("door", "doors", "puerta", "puertas"),
    "ventana": ("ventana", "ventanas", "window", "windows"),
    "window": ("window", "windows", "ventana"),
    "siding": ("siding", "revestimiento", "hardie", "soffit"),
    "alfombra": ("alfombra", "carpet", "floor", "flooring"),
    "carpet": ("carpet", "alfombra", "floor"),
}
_TOKENS_MEDIDA = {
    "floor", "flooring", "piso", "pisos", "ceiling", "cielo", "wall", "walls",
    "pared", "paredes", "trim", "baseboard", "zocalo", "cabinet", "cabinets", "gabinete",
    "tile", "tiles", "baldosa", "baldosas", "ceramica", "azulejo", "porcelanato", "backsplash",
}


def _distancia_token(a, b):
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _familias_de_token(token):
    """'pizos' y 'flor' abren la familia (pisos, floor, vinyl, hardwood) para poder elegir."""
    plano = _sin_acentos(token)
    if len(plano) < 3 or plano in _STOP_BUSQUEDA_CATALOGO:
        return []
    exactas = []
    prefijos = []
    typos = []
    mejor = 9
    for grupo in _SINONIMOS_BUSQUEDA.values():
        palabras = [_sin_acentos(p) for p in grupo if p]
        if plano in palabras:
            exactas.append(grupo)
            continue
        if any(palabra.startswith(plano) for palabra in palabras):
            prefijos.append(grupo)
            continue
        if len(plano) < 4:
            continue
        tope = 2
        dist = min((_distancia_token(plano, palabra) for palabra in palabras), default=9)
        if dist <= tope and dist < mejor:
            mejor = dist
            typos = [grupo]
        elif dist <= tope and dist == mejor and grupo not in typos:
            typos.append(grupo)
    vistos = []
    for grupo in exactas or prefijos or typos:
        if grupo not in vistos:
            vistos.append(grupo)
    return vistos


def _tokens_busqueda_catalogo(q):
    crudos = re.findall(r"[a-zA-Z0-9áéíóúñÁÉÍÓÚÑ]{3,}", str(q or "").lower())
    tokens = []
    for t in crudos:
        if t in _STOP_BUSQUEDA_CATALOGO:
            continue
        tokens.append(_sin_acentos(t))
        for grupo in _familias_de_token(t):
            tokens.extend(grupo)
    vistos = set()
    out = []
    for t in tokens:
        plano = _sin_acentos(t)
        if not plano or plano in vistos:
            continue
        vistos.add(plano)
        out.append(plano)
        if len(out) >= 24:
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
    blob_n = row.get("_busca")
    if not isinstance(blob_n, str) or not blob_n:
        blob_n = _sin_acentos(blob)
        if isinstance(row, dict):
            row["_busca"] = blob_n
    hits = 0
    for t in tokens or _tokens_busqueda_catalogo(q):
        if not t:
            continue
        tn = _sin_acentos(t)
        if tn in blob_n:
            score += 18 if tn in _TOKENS_MEDIDA else 8
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

    def _add(campo, valor, minimo=2):
        limpio = re.sub(r"[,*%()]", "", str(valor or "")).strip()[:80]
        if not limpio or len(limpio) < minimo:
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
    _add("work_description", termino)
    _add("codigo", termino, 1)
    _add("code", termino, 1)
    _add("accion", termino)
    _add("action", termino)
    compacto = _codigo_catalogo_compacto(termino)
    if compacto and compacto.lower() != termino.lower():
        _add("codigo", compacto, 1)
        _add("code", compacto, 1)
    for token in _tokens_busqueda_catalogo(termino):
        if token in _STOP_BUSQUEDA_CATALOGO:
            continue
        if len(token) < 4 and token not in ("tile", "sbs", "rfg", "lvt", "lvp"):
            continue
        _add("descripcion", token)
        _add("descripcion_es", token)
        _add("descripcion_en", token)
        _add("codigo", token, 1)
    return ",".join(partes[:24])


_RE_MEDIDA_PLANO = re.compile(
    r"(?:—|–|-)\s*(floor|ceiling|walls?|trim|baseboard(?:\s*/\s*trim)?|cabinets?|"
    r"piso|cielo|pared(?:es)?|z[oó]calo|gabinetes?)\b",
    re.I,
)


def _consulta_medida_plano(q):
    """Solo 'Kitchen — Floor' (importar medidas). No recorta 'pintar paredes' ni 'remover pisos'."""
    texto = str(q or "")
    match = _RE_MEDIDA_PLANO.search(texto)
    if match:
        return match.group(1).split("/")[0].strip()
    return ""


_CATALOGO_INDICE = {"id": None, "tokens": {}}
_TOKEN_REMOCION = "__remocion__"
_INDICE_CATALOGO_LOCK = threading.Lock()


def _indice_busqueda_catalogo(filas):
    marca = id(filas)
    if _CATALOGO_INDICE.get("id") == marca:
        return _CATALOGO_INDICE["tokens"]
    with _INDICE_CATALOGO_LOCK:
        if _CATALOGO_INDICE.get("id") == marca:
            return _CATALOGO_INDICE["tokens"]
        tokens = {}
        for i, row in enumerate(filas):
            if not isinstance(row, dict):
                continue
            blob = row.get("_busca")
            if not isinstance(blob, str) or not blob:
                blob = _sin_acentos(_texto_item_catalogo(row))
                row["_busca"] = blob
            for tok in set(re.findall(r"[a-z0-9]{3,}", blob)):
                bucket = tokens.get(tok)
                if bucket is None:
                    tokens[tok] = [i]
                else:
                    bucket.append(i)
            codigo = re.sub(r"[^a-z0-9]", "", _sin_acentos(row.get("codigo") or row.get("code") or ""))
            if len(codigo) >= 3:
                bucket = tokens.get(codigo)
                if bucket is None:
                    tokens[codigo] = [i]
                elif i not in bucket:
                    bucket.append(i)
            if _num_catalogo(row, "precio_remocion") > 0:
                tokens.setdefault(_TOKEN_REMOCION, []).append(i)
        _CATALOGO_INDICE["id"] = marca
        _CATALOGO_INDICE["tokens"] = tokens
        return tokens


def _postings_catalogo(indice, plano):
    exacto = indice.get(plano)
    if exacto:
        return exacto
    if len(plano) < 4:
        return []
    unidos = []
    for clave, hits in indice.items():
        if clave.startswith(plano):
            unidos.extend(hits)
            if len(unidos) >= 400:
                break
    return unidos


def _parece_codigo_catalogo(q):
    texto = str(q or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_.]*", texto) and re.search(r"[-_.]|\d", texto))


def _es_consulta_demolicion(q):
    palabras = set(re.findall(r"[a-z]{3,}", _sin_acentos(q)))
    return bool(palabras & set(_SINONIMOS_DEMOLICION)) or bool(re.search(r"tear[\s-]*(out|off)", str(q or ""), re.I))


def _puntaje_demolicion(row):
    """Para 'remover/quitar/tear out': primero los ítems de demolición o con precio_remocion."""
    score = 0
    if _num_catalogo(row, "precio_remocion") > 0:
        score += 20
    codigo = str(row.get("codigo") or row.get("code") or "").upper()
    blob = row.get("_busca") or _sin_acentos(_texto_item_catalogo(row))
    if "-DEM-" in codigo or re.search(r"remoc|retiro|retirar|demolic|tear|remov|desmont|arranc", blob):
        score += 25
    return score


def _clave_sinonimo_catalogo(palabra):
    if palabra in _SINONIMOS_BUSQUEDA:
        return palabra
    if len(palabra) < 4:
        return None
    tope = 2 if len(palabra) >= 6 else 1
    mejor, distancia = None, tope + 1
    for clave in _SINONIMOS_BUSQUEDA:
        if " " in clave:
            continue
        d = 0 if clave.startswith(palabra) else _distancia_token(palabra, clave)
        if d < distancia:
            mejor, distancia = clave, d
    return mejor


def _grupos_busqueda_catalogo(consulta, indice):
    """Un grupo de sinónimos por palabra escrita: una fila debe cubrir cada grupo, no solo el sinónimo más raro."""
    grupos = []
    for palabra in re.findall(r"[a-z0-9]{3,}", _sin_acentos(consulta)):
        if palabra in _STOP_BUSQUEDA_CATALOGO:
            continue
        grupo = [palabra]
        clave = _clave_sinonimo_catalogo(palabra)
        if clave:
            grupo += [_sin_acentos(p) for p in _SINONIMOS_BUSQUEDA[clave] if " " not in p]
            if _SINONIMOS_BUSQUEDA[clave] is _SINONIMOS_DEMOLICION:
                grupo.append(_TOKEN_REMOCION)
        grupo = list(dict.fromkeys(p for p in grupo if len(p) >= 3))
        if grupo not in grupos:
            grupos.append(grupo)
    return grupos[:6]


def _candidatos_indice_catalogo(filas, grupos):
    indice = _indice_busqueda_catalogo(filas)
    cobertura = {}
    for grupo in grupos or []:
        hits = set()
        for token in grupo:
            hits.update(_postings_catalogo(indice, token))
        for i in hits:
            cobertura[i] = cobertura.get(i, 0) + 1
    if not cobertura:
        return [], {}
    orden = sorted(cobertura, key=lambda i: -cobertura[i])
    maximo = cobertura[orden[0]]
    elegidos = [i for i in orden if cobertura[i] >= maximo]
    if len(elegidos) < 30:
        elegidos = [i for i in orden if cobertura[i] >= max(1, maximo - 1)]
    elegidos = [i for i in elegidos[:600] if 0 <= i < len(filas)]
    return [filas[i] for i in elegidos], {id(filas[i]): cobertura[i] for i in elegidos}


def _filas_catalogo_locales(q, unidad="", limite=80, con_puntaje=False):
    """El catálogo ya cabe en memoria. Buscar ahí evita varias consultas lentas a Supabase."""
    from oficios import todas_filas_catalogo

    filas = todas_filas_catalogo() or []
    consulta = _consulta_medida_plano(q) or _nucleo_busqueda_catalogo(_corregir_typos_catalogo(q)) or str(q or "").strip()
    if not filas or not consulta:
        return []
    tokens = _tokens_busqueda_catalogo(consulta)
    compacto = re.sub(r"[^a-z0-9]", "", _sin_acentos(consulta))
    if len(compacto) >= 3 and compacto not in tokens:
        tokens = list(tokens) + [compacto]
    unidad_q = str(unidad or "").upper()
    indice = _indice_busqueda_catalogo(filas)
    if len(compacto) >= 3 and _parece_codigo_catalogo(consulta):
        grupos = [[compacto]]
    else:
        grupos = _grupos_busqueda_catalogo(consulta, indice)
    candidatos, cobertura = _candidatos_indice_catalogo(filas, grupos)
    demolicion = _es_consulta_demolicion(consulta)
    puntuadas = []
    for row in candidatos:
        cubiertos = cobertura.get(id(row), 0)
        score = _puntaje_coincidencia_catalogo(row, consulta, tokens, unidad_q)
        score += 30 * cubiertos - 30 * (len(grupos) - cubiertos)
        if demolicion:
            score += _puntaje_demolicion(row)
        if score < 8:
            continue
        precio = _num_catalogo(row, "precio_base", "precio_unitario", "precio", "price")
        puntuadas.append((score, precio, row))
    puntuadas.sort(key=lambda par: (par[0], par[1]), reverse=True)
    salida = []
    vistos = set()
    for score, _precio, row in puntuadas:
        clave = (
            str(row.get("codigo") or row.get("code") or "").strip().lower(),
            _sin_acentos(row.get("descripcion") or row.get("description") or ""),
        )
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append((score, row) if con_puntaje else row)
        if len(salida) >= int(limite or 60):
            break
    return salida


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
  items jsonb, -- partidas: descripcion TEXT ilimitado dentro del JSON
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
    if isinstance(snapshot, dict) and snapshot.get("areas"):
        snapshot = dict(snapshot)
        snapshot["areas"] = _preservar_descripciones_areas(snapshot.get("areas") or [])
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
        "items": _preservar_descripciones_items(items if isinstance(items, list) else []),
        "areas": _preservar_descripciones_areas(areas if isinstance(areas, list) else []),
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


def _texto_descripcion_partida(item):
    if not isinstance(item, dict):
        return ""
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    cands = [
        item.get("descCustom"),
        meta.get("descCustom"),
        item.get("descripcion"),
        item.get("description"),
        item.get("desc"),
        item.get("WORK DESCRIPTION"),
        item.get("work description"),
    ]
    return max((str(x if x is not None else "") for x in cands), key=len)


def _preservar_descripcion_partida(item):
    if not isinstance(item, dict):
        return item
    desc = _texto_descripcion_partida(item)
    row = dict(item)
    row["descripcion"] = desc
    row["description"] = desc
    row["desc"] = desc
    row["descCustom"] = desc
    meta = dict(row.get("meta") or {}) if isinstance(row.get("meta"), dict) else {}
    meta["descManual"] = "1"
    meta["descCustom"] = desc
    row["meta"] = meta
    return row


def _preservar_descripciones_items(items):
    if not isinstance(items, list):
        return items
    return [_preservar_descripcion_partida(it) if isinstance(it, dict) else it for it in items]


def _preservar_descripciones_areas(areas):
    if not isinstance(areas, list):
        return areas
    out = []
    for area in areas:
        if not isinstance(area, dict):
            out.append(area)
            continue
        copia = dict(area)
        rows = copia.get("rows")
        if isinstance(rows, list):
            copia["rows"] = [
                _preservar_descripcion_partida(r) if isinstance(r, dict) else r for r in rows
            ]
        out.append(copia)
    return out


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
    items = _preservar_descripciones_items(data.get("items") or [])
    areas = _preservar_descripciones_areas(data.get("areas") or snapshot.get("areas") or [])
    if isinstance(snapshot, dict) and snapshot.get("areas"):
        snapshot = dict(snapshot)
        snapshot["areas"] = _preservar_descripciones_areas(snapshot.get("areas") or [])
    fila = {
        "folio": folio or None,
        "tipo": tipo or "estimate",
        "cliente_nombre": (cliente.get("name") if isinstance(cliente, dict) else None) or data.get("clienteNombre") or data.get("client") or None,
        "fecha": fecha,
        "total": total,
        "estado": data.get("status") or data.get("estado") or "pending",
        "cliente": cliente,
        "items": items,
        "areas": areas,
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


def _partida_estimado_ejemplo(desc, qty, price, material, labor, remocion=0.0, unit="SF", action="install_material"):
    desc = str(desc or "").strip()
    qty = float(qty)
    price = round(float(price), 2)
    material = round(float(material), 2)
    labor = round(float(labor), 2)
    remocion = round(float(remocion), 2)
    paquete = round(material + labor + remocion, 2)
    columnas = json.dumps({
        "precio_base": paquete,
        "precio_material": material,
        "precio_mano_obra": labor,
        "precio_remocion": remocion,
    })
    meta = {
        "descManual": "1",
        "descCustom": desc,
        "skipZipFactor": "1",
        "skipMinimo": "1",
        "accion": action,
        "columnasCatalogo": columnas,
        "precioBase": str(paquete),
        "precioPaquete": str(paquete),
        "precioMaterial": str(material),
        "precioLabor": str(labor),
        "precioDemo": str(remocion),
        "precioImportado": "1",
        "unidadManual": "1",
    }
    return {
        "descripcion": desc,
        "description": desc,
        "desc": desc,
        "descCustom": desc,
        "cantidad": qty,
        "qty": qty,
        "precio_unitario": price,
        "price": price,
        "unidad": unit,
        "unit": unit,
        "accion": action,
        "action": action,
        "tax": 0,
        "codigo": "",
        "code": "",
        "meta": meta,
        "subtotal": round(qty * price, 2),
    }


def _fila_estimado_ejemplo():
    proyecto = "Sample: Kitchen & living refresh"
    drywall = _partida_estimado_ejemplo(
        'Hang, tape, and finish 1/2" drywall', 420, 4.75, material=1.60, labor=3.15
    )
    pintura = _partida_estimado_ejemplo(
        "Interior paint, two coats (walls)", 680, 2.85, material=0.90, labor=1.95
    )
    drywall["area"] = proyecto
    pintura["area"] = proyecto
    items = [drywall, pintura]
    total = round(sum(float(it["subtotal"]) for it in items), 2)
    hoy = datetime.now(timezone.utc).date().isoformat()
    cliente = {
        "name": "Jordan Hale",
        "email": "jordan.hale@example.com",
        "phone": "(404) 555-0198",
        "street": "1842 Oakridge Lane",
        "city": "Atlanta",
        "state": "GA",
        "zip": "30318",
    }
    areas = [{
        "title": proyecto,
        "titleEn": proyecto,
        "titleEs": "Ejemplo: cocina y sala",
        "reference": "DEMO",
        "rows": [
            {k: it[k] for k in ("code", "desc", "description", "descripcion", "descCustom", "qty", "price", "unit", "tax", "action", "meta")}
            for it in items
        ],
    }]
    snapshot = {
        "docMode": "estimate",
        "language": "en",
        "quote": "EST-EXAMPLE",
        "date": hoy,
        "status": "open",
        "client": cliente,
        "areas": areas,
        "projectNotes": "This is a sample QuantixBuilds estimate. Review quantities × unit price, then change Action to see material, labor, and demo.",
        "terms": "Sample document for training only. Replace these items with your own scope when you start a real estimate.",
        "zip": "30318",
        "tipoProyecto": "residencial",
        "contratista": "general",
        "autoPrecios": False,
        "minimos": False,
    }
    return {
        "folio": "EST-EXAMPLE",
        "tipo": "estimate",
        "cliente_nombre": cliente["name"],
        "fecha": hoy,
        "total": total,
        "estado": "pending",
        "cliente": cliente,
        "items": items,
        "areas": areas,
        "snapshot": snapshot,
        "notas": snapshot["projectNotes"],
        "terminos": snapshot["terms"],
        "firmas": {},
        "payload": {
            "type": "estimate",
            "docMode": "estimate",
            "folio": "EST-EXAMPLE",
            "client": cliente["name"],
            "clienteNombre": cliente["name"],
            "total": total,
        },
    }


def _token_contratista_solicitud():
    auth = str(request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = str(request.headers.get("X-Contratista-Token") or "").strip()
    if header:
        return header
    cookie = str(request.cookies.get(COOKIE_CONTRATISTA) or "").strip()
    if cookie:
        return cookie
    return str(request.args.get("token") or "").strip()


def _nombre_empresa_sesion():
    token = _token_contratista_solicitud()
    if not token:
        return ""
    try:
        row = contratista_por_token(token)
    except Exception:
        return ""
    return str((row or {}).get("nombre_empresa") or "").strip()


def _host_publico():
    bruto = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    return bruto.split(":")[0].lower()


def _peticion_https():
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "").split(",")[0].strip().lower()
    return proto == "https" or request.is_secure


def _es_dominio_app(host=None):
    host = (host or _host_publico()).lower()
    return host == DOMINIO_APP or host.endswith("." + DOMINIO_APP)


def _limpiar_cookies_contratista(respuesta):
    seguro = _peticion_https()
    variantes = [{"path": "/", "samesite": "Lax", "secure": seguro}]
    if _es_dominio_app():
        for dominio in (DOMINIO_APP, "." + DOMINIO_APP, "www." + DOMINIO_APP):
            variantes.append({"path": "/", "domain": dominio, "samesite": "Lax", "secure": seguro})
    for nombre in (COOKIE_CONTRATISTA, COOKIE_PERSISTIR, COOKIE_OAUTH):
        for kwargs in variantes:
            respuesta.delete_cookie(nombre, **kwargs)
    return respuesta


def _recordarme_activo(valor, predeterminado=True):
    if isinstance(valor, bool):
        return valor
    if valor is None:
        return bool(predeterminado)
    return str(valor).strip().lower() not in ("0", "false", "no", "off")


def _adjuntar_sesion_contratista(respuesta, token, persistir=None):
    seguro = _peticion_https()
    respuesta.delete_cookie(COOKIE_CONTRATISTA, path="/", samesite="Lax", secure=seguro)
    respuesta.delete_cookie(COOKIE_PERSISTIR, path="/", samesite="Lax", secure=seguro)
    if not token:
        return _limpiar_cookies_contratista(respuesta)
    if persistir is None:
        persistir = request.cookies.get(COOKIE_PERSISTIR) != "0"
    kwargs = {
        "httponly": True,
        "samesite": "Lax",
        "secure": seguro,
        "path": "/",
    }
    if persistir:
        kwargs["max_age"] = _MAX_EDAD_RECORDADA
    if _es_dominio_app():
        kwargs["domain"] = "." + DOMINIO_APP
    respuesta.set_cookie(COOKIE_CONTRATISTA, token, **kwargs)
    respuesta.set_cookie(COOKIE_PERSISTIR, "1" if persistir else "0", **kwargs)
    return respuesta


@app.before_request
def _canonico_quantixbuilds():
    if request.method not in ("GET", "HEAD"):
        return None
    host = _host_publico()
    if host != "www." + DOMINIO_APP:
        return None
    destino = "https://" + DOMINIO_APP + request.full_path
    if destino.endswith("?") and not request.query_string:
        destino = destino[:-1]
    return redirect(destino, code=301)


def _error_sin_funcion_tenant(err):
    return bool(re.search(
        r"PGRST202|PGRST205|Could not find the function|Could not find the table|schema cache",
        str(err or ""),
        re.I,
    ))


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
        "user_id": row.get("user_id") or "",
    }


_sin_columna_user_id = False


def _es_error_columna_user_id(err):
    return bool(re.search(r"\buser_id\b", str(err or ""), re.I))


def _marcar_sin_user_id(err):
    global _sin_columna_user_id
    if _es_error_columna_user_id(err):
        _sin_columna_user_id = True
        return True
    return False


def _cols_contratista():
    if _sin_columna_user_id:
        return "id,email,nombre_empresa,token"
    return "id,email,nombre_empresa,token,user_id"


def _select_contratista(sb, **filtros):
    query = sb.table(TABLA_CONTRATISTAS).select(_cols_contratista())
    for clave, valor in filtros.items():
        query = query.eq(clave, valor)
    try:
        return list(query.limit(1).execute().data or [])
    except Exception as err:
        if _marcar_sin_user_id(err):
            try:
                query = sb.table(TABLA_CONTRATISTAS).select("id,email,nombre_empresa,token")
                for clave, valor in filtros.items():
                    query = query.eq(clave, valor)
                return list(query.limit(1).execute().data or [])
            except Exception as err2:
                print(f"--> select contratista sin user_id: {err2}")
                return []
        print(f"--> select contratista: {err}")
        return []


def _asegurar_user_id_contratista(sb, row, user_id):
    uid = str(user_id or "").strip()
    if not sb or not row or not row.get("id") or not _es_uuid(uid) or _sin_columna_user_id:
        return row
    if str(row.get("user_id") or "") == uid:
        return row
    try:
        sb.table(TABLA_CONTRATISTAS).update({"user_id": uid}).eq("id", row["id"]).execute()
        row["user_id"] = uid
    except Exception as err:
        if not _marcar_sin_user_id(err):
            print(f"--> user_id contratista: {err}")
    return row


def _user_id_de_contratista(sb, contratista):
    uid = str((contratista or {}).get("user_id") or "").strip()
    if _es_uuid(uid):
        return uid
    if not sb or _sin_columna_user_id:
        return ""
    cid = str((contratista or {}).get("id") or "").strip()
    email = str((contratista or {}).get("email") or "").strip().lower()
    if cid:
        try:
            rows = _select_contratista(sb, id=cid)
            if rows and _es_uuid(rows[0].get("user_id")):
                return str(rows[0]["user_id"])
            if rows:
                email = email or str(rows[0].get("email") or "").strip().lower()
        except Exception:
            pass
    if email:
        user = _usuario_auth_por_email(email)
        uid = str((user or {}).get("id") or "").strip()
        if _es_uuid(uid):
            if cid:
                _asegurar_user_id_contratista(sb, {"id": cid}, uid)
            return uid
    return ""


def _contratista_por_email_tabla(sb, email):
    email = str(email or "").strip().lower()
    if not email:
        return None
    rows = _select_contratista(sb, email=email)
    return rows[0] if rows else None


def _registrar_contratista_tabla(sb, email, nombre, user_id=""):
    email = str(email or "").strip().lower()
    nombre = str(nombre or "").strip() or None
    if _contratista_por_email_tabla(sb, email):
        raise ErrorAuth("already_registered", "This email already has an account. Use Log in.", 409)
    previos = sb.table(TABLA_CONTRATISTAS).select("id").limit(1).execute()
    es_primero = not list(previos.data or [])
    token = secrets.token_hex(32)
    payload = {"nombre_empresa": nombre, "email": email, "token": token}
    uid = str(user_id or "").strip()
    if _es_uuid(uid) and not _sin_columna_user_id:
        payload["user_id"] = uid

    def _insertar(fila):
        return sb.table(TABLA_CONTRATISTAS).insert(fila).execute()

    try:
        res = _insertar(payload)
    except Exception as err:
        if "user_id" in payload and _marcar_sin_user_id(err):
            payload.pop("user_id", None)
            try:
                res = _insertar(payload)
                err = None
            except Exception as err2:
                err = err2
        if err is not None:
            if nombre is not None or not re.search(r"not-null|null value", str(err or ""), re.I):
                raise err
            payload["nombre_empresa"] = ""
            res = _insertar(payload)
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
    if uid:
        row["user_id"] = uid
    return row


def _contratista_por_token_tabla(sb, token):
    rows = _select_contratista(sb, token=token)
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


class ErrorAuth(Exception):
    def __init__(self, code, message, status=400):
        self.code = code
        self.status = int(status)
        super().__init__(message)


def _validar_password(password):
    texto = str(password or "")
    if len(texto) < 8 or len(texto) > 72:
        raise ErrorAuth("password", "Password must be at least 8 characters.", 400)
    return texto


def _auth_http(method, path, body=None, clave=None):
    url, key = _supabase_creds()
    key = str(clave or key or "").strip()
    if not url or not key:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    import httpx
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    with httpx.Client(verify=False, timeout=30.0) as http:
        kwargs = {"headers": headers}
        if body is not None and str(method or "").upper() not in ("GET", "HEAD"):
            kwargs["json"] = body
        res = http.request(method, url + path, **kwargs)
    try:
        data = res.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return res.status_code, data


def _mensaje_auth(data):
    return str(
        (data or {}).get("msg")
        or (data or {}).get("message")
        or (data or {}).get("error_description")
        or (data or {}).get("error")
        or ""
    )


_PLANES_VALIDOS = {"basico", "profesional", "premium"}
_PLANES_PRECIOS = {
    "basico": {"nombre": "Basic Plan", "centavos": 2900},
    "profesional": {"nombre": "Professional Plan", "centavos": 4900},
    "premium": {"nombre": "Premium Plan", "centavos": 9900},
}


def _plan_valido(plan):
    clave = str(plan or "").strip().lower()
    return clave if clave in _PLANES_VALIDOS else ""


_CORREOS_ADMIN = {
    "israelpinedaurte13081999@gmail.com",
    "israelpinedaduarte13081999@gmail.com",
}


def _es_admin(email):
    correo = str(email or "").strip().lower()
    extra = str(os.getenv("ADMIN_EMAIL") or "").strip().lower()
    if extra:
        _CORREOS_ADMIN.add(extra)
    return bool(correo) and correo in _CORREOS_ADMIN


def _es_bypass_facturacion(email):
    return _es_admin(email)


def _activar_plan_bypass_admin(contratista, plan=""):
    if not _es_bypass_facturacion((contratista or {}).get("email")):
        return False
    plan = _plan_valido(plan or (contratista or {}).get("plan_seleccionado")) or "premium"
    email = str((contratista or {}).get("email") or "").strip().lower()
    uid = str((contratista or {}).get("user_id") or "").strip()
    ok = _activar_premium_por_square(
        email,
        customer_id="admin_bypass",
        plan=plan,
        user_id=uid,
    )
    if uid:
        try:
            _guardar_pago_cliente(_supabase_client(), uid, email, "admin_bypass", plan, activar=True)
        except Exception as err:
            print(f"--> admin billing bypass perfil: {err}")
    if contratista is not None:
        contratista["plan_seleccionado"] = plan
        contratista["pago_cliente_id"] = "admin_bypass"
        contratista["requiere_pago"] = False
        contratista["trial_expirada"] = False
        contratista["ai_pdf_extractions_count"] = 0
    print("--> admin billing bypass: Square skipped, plan active")
    return ok


def _necesita_paso_pago(_contratista):
    return False


def _prueba_expirada(contratista):
    if not contratista or _es_admin(contratista.get("email")):
        return False
    if contratista.get("trial_expirada") is True:
        return True
    fin = _parse_ts(contratista.get("fecha_fin_prueba"))
    return bool(fin and datetime.now(timezone.utc) > fin)


def _respuesta_prueba_expirada():
    return jsonify({
        "error": "Your 14-day trial has ended. Activate your account to keep using QuantixBuilds.",
        "code": "trial_expired",
        "siguiente": "suscripcion",
    }), 402


def _nombre_plan(plan):
    info = _PLANES_PRECIOS.get(_plan_valido(plan)) or {}
    return str(info.get("nombre") or "plan")


_NIVEL_PLAN = {"basico": 1, "profesional": 2, "premium": 3}
_API_CATALOGO = {
    "/api/search-catalog",
    "/api/buscar-catalogo",
    "/api/precio-accion",
    "/api/catalogo/guardar-ia",
    "/api/catalogo/home-depot",
}
_API_IA_PDF = (
    "/api/sugerir-precios-ia",
    "/api/sugerir_descripcion",
)
_API_IA_PDF_CUOTA = (
    "/api/import-roof-report",
    "/api/process-roof-report",
    "/api/process-pdf",
    "/api/import-line-items",
    "/api/import-past-estimate",
    "/api/import-planos",
    "/api/process-architectural-plans",
)
_LIMITE_IA_PDF_GRATIS = 5
_MSG_CUOTA_IA_PDF = (
    "Has utilizado tus 5 extracciones gratuitas con IA. "
    "Por favor, actualiza tu plan a Premium para uso ilimitado"
)
_SQL_IA_PDF = "Ejecuta ai_pdf_extractions.sql en el editor SQL de Supabase."
_sin_columna_ai_pdf = False


def _nivel_plan(contratista):
    if not contratista:
        return 0
    if _es_admin(contratista.get("email")):
        return 99
    return _NIVEL_PLAN.get(_plan_valido(contratista.get("plan_seleccionado")), 0)


def _puede_feature(contratista, feature):
    nivel = _nivel_plan(contratista)
    if feature == "catalog":
        return nivel >= 2
    if feature == "ai_pdf":
        return nivel >= 3
    if feature == "ai_pdf_quota":
        return True
    return nivel >= 1


def _es_premium_ia(contratista):
    if not contratista:
        return False
    if _es_admin(contratista.get("email")):
        return True
    return _plan_valido(contratista.get("plan_seleccionado")) == "premium"


def _es_error_columna_ai_pdf(err):
    return bool(re.search(r"ai_pdf_extractions_count", str(err or ""), re.I))


def _marcar_sin_columna_ai_pdf(err):
    global _sin_columna_ai_pdf
    if _es_error_columna_ai_pdf(err):
        _sin_columna_ai_pdf = True
        print("-->", _SQL_IA_PDF)
        return True
    return False


def _respuesta_upgrade():
    return jsonify({
        "error": "This feature requires an upgrade",
        "code": "upgrade_required",
        "siguiente": "planes",
    }), 403


def _respuesta_cuota_ia_pdf(count=None):
    usado = _LIMITE_IA_PDF_GRATIS if count is None else max(int(count or 0), _LIMITE_IA_PDF_GRATIS)
    return jsonify({
        "success": False,
        "error": _MSG_CUOTA_IA_PDF,
        "code": "ai_quota",
        "siguiente": "planes",
        "ai_pdf_extractions_count": usado,
        "limite": _LIMITE_IA_PDF_GRATIS,
    }), 403


def _parse_rpc_cuota_ia(data):
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = None
    if not isinstance(data, dict):
        return None
    if "ok" not in data:
        return None
    try:
        count = int(data.get("count") or 0)
    except Exception:
        count = 0
    return bool(data.get("ok")), count


def _leer_contador_ia_pdf(sb, contratista):
    if not sb or _sin_columna_ai_pdf or not contratista:
        return 0
    uid = str(contratista.get("user_id") or "").strip()
    email = str(contratista.get("email") or "").strip().lower()
    cid = str(contratista.get("id") or "").strip()
    try:
        fila = None
        if uid:
            res = sb.table("perfiles").select("ai_pdf_extractions_count").eq("id", uid).limit(1).execute()
            rows = list(res.data or [])
            fila = rows[0] if rows else None
        if not fila and email:
            res = sb.table("perfiles").select("ai_pdf_extractions_count").eq("email", email).limit(1).execute()
            rows = list(res.data or [])
            fila = rows[0] if rows else None
        if not fila and cid:
            res = sb.table(TABLA_CONTRATISTAS).select("ai_pdf_extractions_count").eq("id", cid).limit(1).execute()
            rows = list(res.data or [])
            fila = rows[0] if rows else None
        if not fila and email:
            res = sb.table(TABLA_CONTRATISTAS).select("ai_pdf_extractions_count").eq("email", email).limit(1).execute()
            rows = list(res.data or [])
            fila = rows[0] if rows else None
        return max(0, int((fila or {}).get("ai_pdf_extractions_count") or 0))
    except Exception as err:
        if _marcar_sin_columna_ai_pdf(err):
            return 0
        print(f"--> leer cuota IA PDF: {err}")
        return 0


def _incrementar_contador_tabla(sb, tabla, filtros, actual):
    query = sb.table(tabla).update({
        "ai_pdf_extractions_count": int(actual) + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    for clave, valor in filtros.items():
        query = query.eq(clave, valor)
    query = query.eq("ai_pdf_extractions_count", int(actual))
    res = query.execute()
    return list(res.data or [])


def _intentar_consumir_ia_pdf(contratista):
    sb = _supabase_client()
    if sb is None:
        return True, 0
    if _sin_columna_ai_pdf:
        return True, _leer_contador_ia_pdf(sb, contratista)
    uid = str((contratista or {}).get("user_id") or "").strip()
    email = str((contratista or {}).get("email") or "").strip().lower()
    cid = str((contratista or {}).get("id") or "").strip()
    try:
        data = _rpc_tenant(sb, "consumir_ai_pdf_extraction", {
            "p_user_id": uid if _es_uuid(uid) else None,
            "p_email": email,
        })
        parsed = _parse_rpc_cuota_ia(data)
        if parsed is not None:
            return parsed
    except Exception as err:
        if _marcar_sin_columna_ai_pdf(err):
            return True, 0
        if not _error_sin_funcion_tenant(err):
            print(f"--> RPC cuota IA PDF: {err}")
    actual = _leer_contador_ia_pdf(sb, contratista)
    if actual >= _LIMITE_IA_PDF_GRATIS:
        return False, actual
    try:
        filas = []
        if uid:
            filas = _incrementar_contador_tabla(sb, "perfiles", {"id": uid}, actual)
        if not filas and email:
            filas = _incrementar_contador_tabla(sb, "perfiles", {"email": email}, actual)
        if not filas and cid:
            filas = _incrementar_contador_tabla(sb, TABLA_CONTRATISTAS, {"id": cid}, actual)
        if not filas and email:
            filas = _incrementar_contador_tabla(sb, TABLA_CONTRATISTAS, {"email": email}, actual)
        if filas:
            try:
                return True, int((filas[0] or {}).get("ai_pdf_extractions_count") or actual + 1)
            except Exception:
                return True, actual + 1
        nuevo = _leer_contador_ia_pdf(sb, contratista)
        if nuevo > actual:
            return True, nuevo
        if nuevo >= _LIMITE_IA_PDF_GRATIS:
            return False, nuevo
        return True, actual
    except Exception as err:
        if _marcar_sin_columna_ai_pdf(err):
            return True, actual
        print(f"--> incrementar cuota IA PDF: {err}")
        return True, actual


def _consumir_extraccion_ia_pdf():
    contratista, error = _exigir_contratista()
    if error:
        return error
    if _prueba_expirada(contratista):
        return _respuesta_prueba_expirada()
    if _es_premium_ia(contratista):
        return None
    ok, count = _intentar_consumir_ia_pdf(contratista)
    if not ok:
        return _respuesta_cuota_ia_pdf(count)
    return None


def _feature_api(path):
    ruta = str(path or "").split("?")[0]
    if ruta.startswith("/api/get-zip-factor"):
        return "catalog"
    if ruta.startswith("/api/import-planos/status") or ruta.startswith("/api/process-architectural-plans/status"):
        return "ai_pdf_quota"
    base = ruta.rstrip("/") or ruta
    if (
        base in _API_CATALOGO
        or ruta.startswith("/api/search-catalog")
        or ruta.startswith("/api/buscar-catalogo")
        or ruta.startswith("/api/catalogo/home-depot")
    ):
        return "catalog"
    for pref in _API_IA_PDF_CUOTA:
        if ruta == pref or ruta.startswith(pref + "/"):
            return "ai_pdf_quota"
    for pref in _API_IA_PDF:
        if ruta == pref or ruta.startswith(pref + "/"):
            return "ai_pdf"
    return None


@app.before_request
def _bloquear_api_por_plan():
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    feature = _feature_api(path)
    if not feature:
        return None
    contratista, error = _exigir_contratista()
    if error:
        return error
    if _prueba_expirada(contratista):
        return _respuesta_prueba_expirada()
    if feature == "ai_pdf_quota":
        return None
    if not _puede_feature(contratista, feature):
        return _respuesta_upgrade()
    return None


def _crear_usuario_auth(email, password, nombre, plan=""):
    status, data = _auth_http("POST", "/auth/v1/admin/users", {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {
            "nombre": nombre or "",
            "plan_seleccionado": _plan_valido(plan),
            "nombre_empresa": "",
        },
    })
    if status in (200, 201) and data.get("id"):
        return data
    msg = _mensaje_auth(data).lower()
    codigo = str(data.get("error_code") or "").lower()
    if status in (409, 422) or "already" in msg or "exists" in msg or codigo == "email_exists":
        raise ErrorAuth("auth_existe", "This email already has a password. Use Log in.", 409)
    print(f"--> auth crear usuario status={status} code={codigo} detalle={_mensaje_auth(data)[:400]}")
    raise ErrorAuth("auth_error", "Could not create the account.", 502)


def _verificar_contrasena(email, password):
    publica = _clave_publica_supabase()
    clave = publica if publica and _tipo_clave_supabase(publica) != "service" else None
    status, data = _auth_http("POST", "/auth/v1/token?grant_type=password", {
        "email": email,
        "password": password,
    }, clave=clave)
    if status == 200 and (data.get("access_token") or data.get("user")):
        return data
    msg = _mensaje_auth(data).lower()
    if "not confirmed" in msg or "email_not_confirmed" in msg:
        raise ErrorAuth("sin_confirmar", "Confirm the email before signing in.", 403)
    raise ErrorAuth("credenciales", "Email or password is incorrect.", 401)


_CODIGOS_AUTH_USUARIO = {
    "credenciales", "email", "password", "sin_cuenta", "already_registered",
    "auth_existe", "enlace", "sin_confirmar", "plan", "trial_expired",
}


def _respuesta_auth(err):
    if isinstance(err, ErrorAuth):
        if err.code not in _CODIGOS_AUTH_USUARIO or _es_fallo_infra(err):
            return _respuesta_login_silenciosa(err, err.code or "auth")
        return jsonify({"error": str(err), "code": err.code}), err.status
    return _respuesta_login_silenciosa(err, "auth")


def _sesion_desde_fila(row):
    row = row or {}
    prueba = {
        "fecha_fin_prueba": "",
        "trial_expirada": False,
        "plan_seleccionado": "",
        "pago_cliente_id": "",
        "requiere_pago": False,
        "ai_pdf_extractions_count": 0,
    }
    try:
        prueba.update(_datos_prueba(_supabase_client(), row) or {})
    except Exception as err:
        print(f"--> sesion perfil/contadores: {err}")
    try:
        count = int(prueba.get("ai_pdf_extractions_count") or 0)
    except Exception:
        count = 0
    return {
        "id": row.get("id"),
        "email": row.get("email"),
        "nombre_empresa": row.get("nombre_empresa") or "",
        "token": row.get("token"),
        "user_id": row.get("user_id") or "",
        "fecha_fin_prueba": "" if _es_admin(row.get("email")) else (prueba.get("fecha_fin_prueba") or ""),
        "trial_expirada": False if _es_admin(row.get("email")) else bool(prueba.get("trial_expirada")),
        "plan_seleccionado": prueba.get("plan_seleccionado") or "",
        "pago_cliente_id": prueba.get("pago_cliente_id") or "",
        "requiere_pago": False if _es_admin(row.get("email")) else bool(prueba.get("requiere_pago")),
        "nombre_plan": _nombre_plan(prueba.get("plan_seleccionado")),
        "ai_pdf_extractions_count": 0 if _es_admin(row.get("email")) else count,
    }


def _usuario_auth_por_email(email):
    from urllib.parse import quote
    status, data = _auth_http("GET", "/auth/v1/admin/users?email=" + quote(email))
    if status != 200:
        return None
    if isinstance(data, dict) and data.get("id"):
        return data
    usuarios = (data or {}).get("users") if isinstance(data, dict) else data
    if not isinstance(usuarios, list):
        return None
    email_n = str(email or "").strip().lower()
    for user in usuarios:
        if str((user or {}).get("email") or "").strip().lower() == email_n:
            return user
    return usuarios[0] if usuarios else None


def _confirmar_email_auth(email):
    user = _usuario_auth_por_email(email)
    uid = str((user or {}).get("id") or "").strip()
    if not uid:
        return user
    _auth_http("PUT", "/auth/v1/admin/users/" + uid, {"email_confirm": True})
    return user


def _parse_ts(valor):
    if isinstance(valor, datetime):
        dt = valor
    else:
        texto = str(valor or "").strip()
        if not texto:
            return None
        if texto.endswith("Z"):
            texto = texto[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(texto)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fecha_fin_prueba_desde(inicio=None):
    base = _parse_ts(inicio) or datetime.now(timezone.utc)
    return base + timedelta(days=14)


def _datos_prueba(sb, contratista):
    datos = {
        "fecha_fin_prueba": "",
        "trial_expirada": False,
        "plan_seleccionado": "",
        "pago_cliente_id": "",
        "requiere_pago": False,
        "ai_pdf_extractions_count": 0,
    }
    if not sb or not contratista:
        return datos
    try:
        return _datos_prueba_cargar(sb, contratista, datos)
    except Exception as err:
        print(f"--> perfil prueba: {err}")
        return datos


def _datos_prueba_cargar(sb, contratista, datos):
    uid = str(contratista.get("user_id") or "").strip()
    email = str(contratista.get("email") or "").strip().lower()
    fila = None
    columnas = "fecha_fin_prueba,trial_inicio,plan_seleccionado,pago_cliente_id,requiere_pago"
    try:
        if uid:
            res = sb.table("perfiles").select(columnas).eq("id", uid).limit(1).execute()
            rows = list(res.data or [])
            fila = rows[0] if rows else None
        if not fila and email:
            res = sb.table("perfiles").select(columnas).eq("email", email).limit(1).execute()
            rows = list(res.data or [])
            fila = rows[0] if rows else None
    except Exception:
        try:
            if uid:
                res = sb.table("perfiles").select("fecha_fin_prueba,trial_inicio,plan_seleccionado").eq("id", uid).limit(1).execute()
                rows = list(res.data or [])
                fila = rows[0] if rows else None
            if not fila and email:
                res = sb.table("perfiles").select("fecha_fin_prueba,trial_inicio,plan_seleccionado").eq("email", email).limit(1).execute()
                rows = list(res.data or [])
                fila = rows[0] if rows else None
        except Exception as err:
            print(f"--> perfil prueba: {err}")
            return datos
    datos["plan_seleccionado"] = _plan_valido((fila or {}).get("plan_seleccionado"))
    datos["pago_cliente_id"] = str((fila or {}).get("pago_cliente_id") or "").strip()
    datos["requiere_pago"] = bool((fila or {}).get("requiere_pago"))
    if _es_admin(email):
        datos["ai_pdf_extractions_count"] = 0
    else:
        try:
            datos["ai_pdf_extractions_count"] = _leer_contador_ia_pdf(sb, contratista)
        except Exception as err:
            print(f"--> contador IA PDF al entrar: {err}")
            datos["ai_pdf_extractions_count"] = 0
    fin = _parse_ts((fila or {}).get("fecha_fin_prueba"))
    if not fin:
        inicio = _parse_ts((fila or {}).get("trial_inicio"))
        if inicio:
            fin = _fecha_fin_prueba_desde(inicio)
    if not fin:
        return datos
    datos["fecha_fin_prueba"] = fin.isoformat()
    datos["trial_expirada"] = datetime.now(timezone.utc) > fin
    if _es_admin(email):
        datos["fecha_fin_prueba"] = ""
        datos["trial_expirada"] = False
        datos["requiere_pago"] = False
        datos["ai_pdf_extractions_count"] = 0
    return datos


def _guardar_perfil(sb, user_id, email, nombre, plan, exigir_pago=False):
    uid = str(user_id or "").strip()
    if not uid or sb is None:
        return
    ahora_dt = datetime.now(timezone.utc)
    ahora = ahora_dt.isoformat()
    trial_inicio = ahora
    fecha_fin = _fecha_fin_prueba_desde(ahora_dt).isoformat()
    nombre_guardar = str(nombre or "").strip()
    plan_guardar = _plan_valido(plan)
    pago_cliente_id = ""
    requiere_pago = bool(exigir_pago)
    try:
        existente = sb.table("perfiles").select(
            "trial_inicio,fecha_fin_prueba,nombre,plan_seleccionado,pago_cliente_id,requiere_pago"
        ).eq("id", uid).limit(1).execute()
        filas = existente.data if existente else None
        if isinstance(filas, list) and filas:
            if filas[0].get("trial_inicio"):
                trial_inicio = filas[0]["trial_inicio"]
            if filas[0].get("fecha_fin_prueba"):
                fecha_fin = filas[0]["fecha_fin_prueba"]
            else:
                fecha_fin = _fecha_fin_prueba_desde(trial_inicio).isoformat()
            if not nombre_guardar:
                nombre_guardar = str(filas[0].get("nombre") or "").strip()
            if not plan_guardar:
                plan_guardar = _plan_valido(filas[0].get("plan_seleccionado"))
            pago_cliente_id = str(filas[0].get("pago_cliente_id") or "").strip()
            if pago_cliente_id:
                requiere_pago = False
            elif exigir_pago:
                requiere_pago = True
            else:
                requiere_pago = bool(filas[0].get("requiere_pago"))
    except Exception:
        pass
    fila = {
        "id": uid,
        "email": email,
        "nombre": nombre_guardar,
        "plan_seleccionado": plan_guardar,
        "trial_inicio": trial_inicio,
        "fecha_fin_prueba": fecha_fin,
        "pago_cliente_id": pago_cliente_id,
        "requiere_pago": requiere_pago,
        "updated_at": ahora,
    }
    try:
        sb.table("perfiles").upsert(fila, on_conflict="id").execute()
        return
    except Exception as err:
        print(f"--> perfil columnas extra: {err}")
    fila.pop("pago_cliente_id", None)
    fila.pop("requiere_pago", None)
    try:
        sb.table("perfiles").upsert(fila, on_conflict="id").execute()
        return
    except Exception:
        pass
    fila.pop("fecha_fin_prueba", None)
    try:
        sb.table("perfiles").upsert(fila, on_conflict="id").execute()
        return
    except Exception:
        pass
    minimo = {"id": uid, "email": email, "nombre_empresa": str(nombre or "").strip(), "updated_at": ahora}
    try:
        sb.table("perfiles").upsert(minimo, on_conflict="id").execute()
    except Exception as err:
        print(f"--> perfil no guardado: {err}")


def guardar_plan_contratista(contratista, plan):
    sb = _supabase_client()
    if sb is None:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    plan = _plan_valido(plan)
    if not plan:
        raise ErrorAuth("plan", "Choose a plan to start your trial.", 400)
    uid = str(contratista.get("user_id") or _user_id_de_contratista(sb, contratista) or "").strip()
    email = str(contratista.get("email") or "").strip().lower()
    ahora = datetime.now(timezone.utc).isoformat()
    cambios = {"plan_seleccionado": plan, "updated_at": ahora}
    cambios["requiere_pago"] = False
    try:
        if uid:
            res = sb.table("perfiles").update(cambios).eq("id", uid).execute()
            if list(res.data or []):
                return plan
        if email:
            res = sb.table("perfiles").update(cambios).eq("email", email).execute()
            if list(res.data or []):
                return plan
    except Exception as err:
        print(f"--> plan perfil: {err}")
    if uid:
        _guardar_perfil(sb, uid, email, "", plan, exigir_pago=True)
    else:
        raise ErrorAuth("sin_cuenta", "Could not save the selected plan.", 404)
    return plan


def _stripe_secreta():
    return (os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_API_KEY") or "").strip()


def _stripe_publica():
    return (os.getenv("STRIPE_PUBLISHABLE_KEY") or os.getenv("STRIPE_PUBLIC_KEY") or "").strip()


def _stripe_webhook_secreta():
    return (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()


def _stripe_mod():
    try:
        import stripe
    except ImportError:
        return None
    clave = _stripe_secreta()
    if not clave:
        return None
    stripe.api_key = clave
    return stripe


def _url_publica_app():
    env = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    try:
        if _es_dominio_app():
            return "https://" + DOMINIO_APP
        return (request.host_url or "").strip().rstrip("/")
    except Exception:
        return "https://" + DOMINIO_APP


def _guardar_pago_cliente(sb, uid, email, customer_id, plan="", activar=False):
    uid = str(uid or "").strip()
    customer_id = str(customer_id or "").strip()
    if not sb or not uid or not customer_id:
        return False
    ahora_dt = datetime.now(timezone.utc)
    ahora = ahora_dt.isoformat()
    fecha_fin = (ahora_dt + timedelta(days=3650)).isoformat() if activar else _fecha_fin_prueba_desde(ahora_dt).isoformat()
    plan_guardar = _plan_valido(plan)
    try:
        existente = sb.table("perfiles").select(
            "fecha_fin_prueba,trial_inicio,plan_seleccionado"
        ).eq("id", uid).limit(1).execute()
        filas = list(existente.data or [])
        if filas:
            if not activar:
                if filas[0].get("fecha_fin_prueba"):
                    fecha_fin = filas[0]["fecha_fin_prueba"]
                elif filas[0].get("trial_inicio"):
                    fecha_fin = _fecha_fin_prueba_desde(filas[0]["trial_inicio"]).isoformat()
            if not plan_guardar:
                plan_guardar = _plan_valido(filas[0].get("plan_seleccionado"))
    except Exception:
        pass
    cambios = {
        "pago_cliente_id": customer_id,
        "requiere_pago": False,
        "fecha_fin_prueba": fecha_fin,
        "updated_at": ahora,
    }
    if plan_guardar:
        cambios["plan_seleccionado"] = plan_guardar
    try:
        res = sb.table("perfiles").update(cambios).eq("id", uid).execute()
        if list(res.data or []):
            print(f"--> pago cliente guardado uid={uid}")
            return True
    except Exception as err:
        print(f"--> pago cliente columnas: {err}")
    if email:
        try:
            res = sb.table("perfiles").update(cambios).eq("email", str(email).strip().lower()).execute()
            if list(res.data or []):
                return True
        except Exception as err:
            print(f"--> pago cliente email: {err}")
    _guardar_perfil(sb, uid, email, "", plan_guardar, exigir_pago=False)
    try:
        sb.table("perfiles").update({
            "pago_cliente_id": customer_id,
            "requiere_pago": False,
            "fecha_fin_prueba": fecha_fin,
            "updated_at": ahora,
        }).eq("id", uid).execute()
        return True
    except Exception as err:
        print(f"--> pago cliente no guardado: {err}")
        return False


def _aplicar_sesion_checkout(session_obj, contratista=None):
    if not session_obj:
        return False
    meta = session_obj.get("metadata") if isinstance(session_obj, dict) else (getattr(session_obj, "metadata", None) or {})
    meta = meta if isinstance(meta, dict) else {}
    uid = str(
        meta.get("user_id")
        or (session_obj.get("client_reference_id") if isinstance(session_obj, dict) else getattr(session_obj, "client_reference_id", ""))
        or ((contratista or {}).get("user_id") or "")
    ).strip()
    email = str(
        (contratista or {}).get("email")
        or meta.get("email")
        or ""
    ).strip().lower()
    customer = session_obj.get("customer") if isinstance(session_obj, dict) else getattr(session_obj, "customer", None)
    if isinstance(customer, dict):
        customer = customer.get("id")
    customer_id = str(customer or "").strip()
    plan = _plan_valido(meta.get("plan") or ((contratista or {}).get("plan_seleccionado") or ""))
    status = str(session_obj.get("status") if isinstance(session_obj, dict) else getattr(session_obj, "status", "") or "").lower()
    pago = str(session_obj.get("payment_status") if isinstance(session_obj, dict) else getattr(session_obj, "payment_status", "") or "").lower()
    if status and status not in ("complete",):
        if pago not in ("paid", "no_payment_required"):
            return False
    if not uid or not customer_id:
        return False
    return _guardar_pago_cliente(_supabase_client(), uid, email, customer_id, plan)


def registrar_contratista(email, nombre, password, plan=""):
    sb = _supabase_client()
    if sb is None:
        raise RuntimeError("Supabase no configurado")
    email = str(email or "").strip().lower()
    nombre = str(nombre or "").strip() or None
    password = _validar_password(password)
    plan = _plan_valido(plan)
    if _contratista_por_email_tabla(sb, email):
        raise ErrorAuth("already_registered", "This email already has an account. Use Log in.", 409)
    auth_user = None
    try:
        auth_user = _crear_usuario_auth(email, password, nombre or "", plan)
    except ErrorAuth as err:
        if err.code != "auth_existe":
            raise
        try:
            sesion = _verificar_contrasena(email, password)
        except ErrorAuth as err2:
            if err2.code != "sin_confirmar":
                raise
            auth_user = _confirmar_email_auth(email)
            sesion = _verificar_contrasena(email, password)
        auth_user = auth_user or (sesion or {}).get("user") or _usuario_auth_por_email(email)
    uid = str((auth_user or {}).get("id") or "").strip()
    _guardar_perfil(sb, uid, email, nombre or "", plan, exigir_pago=True)
    params = {"p_email": email, "p_nombre": nombre}
    if _es_uuid(uid):
        params["p_user_id"] = uid
    try:
        data = _rpc_tenant(sb, "registrar_contratista", params)
        row = _dict_rpc(data)
        if row and row.get("token"):
            return _completar_alta_contratista(sb, row, uid)
    except Exception as err:
        texto = str(err or "")
        if "already_registered" in texto or re.search(r"duplicate key|contratistas_email", texto, re.I):
            raise ErrorAuth("already_registered", "This email already has an account. Use Log in.", 409)
        if params.get("p_user_id") and _error_sin_funcion_tenant(err):
            try:
                data = _rpc_tenant(sb, "registrar_contratista", {"p_email": email, "p_nombre": nombre})
                row = _dict_rpc(data)
                if row and row.get("token"):
                    return _completar_alta_contratista(sb, row, uid)
            except Exception as err2:
                err = err2
                texto = str(err2 or "")
        if "already_registered" in texto or re.search(r"duplicate key|contratistas_email", texto, re.I):
            raise ErrorAuth("already_registered", "This email already has an account. Use Log in.", 409)
        if "invalid_contractor" in texto and not nombre:
            return _completar_alta_contratista(sb, _registrar_contratista_tabla(sb, email, None, uid), uid)
        if "invalid_contractor" in texto:
            raise ErrorAuth("email", "Enter a valid email.", 400)
        print(f"--> registrar contratista {type(err).__name__}: {err}")
        if not _error_sin_funcion_tenant(err):
            raise
        _aviso_multitenant(err)
    return _completar_alta_contratista(sb, _registrar_contratista_tabla(sb, email, nombre, uid), uid)


def iniciar_sesion_contratista(email, password):
    sb = _supabase_client()
    if sb is None:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    email = str(email or "").strip().lower()
    password = _validar_password(password)
    _verificar_contrasena(email, password)
    row = _contratista_por_email_tabla(sb, email)
    if not row or not row.get("token"):
        raise ErrorAuth("sin_cuenta", "No contractor account uses this email. Create one first.", 404)
    try:
        uid = _user_id_de_contratista(sb, row)
        if uid:
            row["user_id"] = uid
    except Exception as err:
        print(f"--> login user_id/perfil: {err}")
    print(f"--> sesion iniciada email={email}")
    return row


def crear_contrasena_contratista(email, password):
    sb = _supabase_client()
    if sb is None:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    email = str(email or "").strip().lower()
    password = _validar_password(password)
    row = _contratista_por_email_tabla(sb, email)
    if not row or not row.get("token"):
        raise ErrorAuth("sin_cuenta", "No contractor account uses this email. Create one first.", 404)
    try:
        _crear_usuario_auth(email, password, row.get("nombre_empresa") or "")
    except ErrorAuth as err:
        if err.code == "auth_existe":
            raise ErrorAuth("auth_existe", "This account already has a password. Use Log in.", 409)
        raise
    _verificar_contrasena(email, password)
    print(f"--> contrasena creada email={email}")
    return row


def _clave_publica_supabase():
    return (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or ""
    ).strip()


def _origen_publico():
    bruto = (request.headers.get("X-Forwarded-Host") or request.host or "localhost").split(",")[0].strip()
    proto = "https" if _peticion_https() else (request.scheme or "http")
    if bruto.count(":") == 1:
        host, puerto = bruto.rsplit(":", 1)
        if (proto == "https" and puerto == "443") or (proto == "http" and puerto == "80"):
            bruto = host
    return proto + "://" + bruto


def _par_pkce():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _auth_como_usuario(method, path, access_token, body=None):
    token = str(access_token or "").strip()
    url, key = _supabase_creds()
    if not url or not key:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    if not token or token == key:
        raise ErrorAuth("enlace", "The reset link expired. Request a new one.", 400)
    import httpx
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    with httpx.Client(verify=False, timeout=30.0) as http:
        res = http.request(method, url + path, headers=headers, json=body)
    try:
        data = res.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return res.status_code, data


def _nombre_oauth(user):
    meta = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
    for clave in ("full_name", "name", "nombre_empresa"):
        texto = str(meta.get(clave) or "").strip()
        if texto:
            return texto[:120]
    identidades = user.get("identities") or []
    if identidades and isinstance(identidades[0], dict):
        datos = identidades[0].get("identity_data") or {}
        if isinstance(datos, dict):
            for clave in ("full_name", "name"):
                texto = str(datos.get(clave) or "").strip()
                if texto:
                    return texto[:120]
    email = str(user.get("email") or "")
    base = email.split("@")[0].replace(".", " ").replace("_", " ").strip()
    return (base.title() or "My company")[:120]


def asegurar_contratista_oauth(email, nombre, user_id=""):
    sb = _supabase_client()
    if sb is None:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    uid = str(user_id or "").strip()
    row = None
    try:
        row = _contratista_por_email_tabla(sb, email)
    except Exception as err:
        print(f"--> oauth buscar contratista: {err}")
    if _es_uuid(uid):
        try:
            _guardar_perfil(sb, uid, email, nombre or "", "", exigir_pago=not row)
        except Exception as err:
            print(f"--> oauth perfil: {err}")
    if row and row.get("token"):
        try:
            return _asegurar_user_id_contratista(sb, row, uid)
        except Exception as err:
            print(f"--> oauth user_id: {err}")
            return row
    params = {"p_email": email, "p_nombre": nombre}
    if _es_uuid(uid):
        params["p_user_id"] = uid
    try:
        data = _rpc_tenant(sb, "registrar_contratista", params)
        row = _dict_rpc(data)
        if row and row.get("token"):
            try:
                return _completar_alta_contratista(sb, row, uid)
            except Exception as err:
                print(f"--> oauth alta post-rpc: {err}")
                return row
    except Exception as err:
        texto = str(err or "")
        print(f"--> oauth registrar contratista {type(err).__name__}: {err}")
        if "already_registered" in texto or re.search(r"duplicate key|contratistas_email", texto, re.I):
            try:
                row = _contratista_por_email_tabla(sb, email)
            except Exception as err2:
                print(f"--> oauth buscar duplicado: {err2}")
                row = None
            if row and row.get("token"):
                try:
                    return _asegurar_user_id_contratista(sb, row, uid)
                except Exception as err2:
                    print(f"--> oauth user_id duplicado: {err2}")
                    return row
        try:
            return _completar_alta_contratista(
                sb, _registrar_contratista_tabla(sb, email, nombre, uid), uid
            )
        except Exception as fallback:
            print(f"--> oauth registrar tabla {type(fallback).__name__}: {fallback}")
            try:
                row = _contratista_por_email_tabla(sb, email)
            except Exception:
                row = None
            if row and row.get("token"):
                return row
            raise
    try:
        return _completar_alta_contratista(sb, _registrar_contratista_tabla(sb, email, nombre, uid), uid)
    except Exception as err:
        print(f"--> oauth alta final: {err}")
        try:
            row = _contratista_por_email_tabla(sb, email)
        except Exception:
            row = None
        if row and row.get("token"):
            return row
        raise


def _email_usuario_auth(user):
    if not isinstance(user, dict):
        return ""
    candidatos = [user.get("email")]
    anidado = user.get("user")
    if isinstance(anidado, dict):
        candidatos.append(anidado.get("email"))
    for identidad in user.get("identities") or []:
        if not isinstance(identidad, dict):
            continue
        datos = identidad.get("identity_data") or {}
        if isinstance(datos, dict):
            candidatos.append(datos.get("email"))
    for valor in candidatos:
        email = str(valor or "").strip().lower()
        if _correo_valido(email):
            return email
    return ""


def _usuario_oauth(access_token, user=None):
    if _email_usuario_auth(user):
        return user
    status, data = _auth_como_usuario("GET", "/auth/v1/user", access_token)
    if status == 200 and _email_usuario_auth(data):
        return data
    raise ErrorAuth("oauth_email", "Google did not share an email for this account.", 400)


def _claves_intercambio_oauth():
    claves = []
    publica = _clave_publica_supabase()
    if publica and _tipo_clave_supabase(publica) != "service":
        claves.append(publica)
    _url, servicio = _supabase_creds()
    if servicio and servicio not in claves:
        claves.append(servicio)
    return claves


def _modelo_auth_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        data = obj.model_dump()
        return data if isinstance(data, dict) else {}
    if hasattr(obj, "dict"):
        data = obj.dict()
        return data if isinstance(data, dict) else {}
    return {}


def _redirect_oauth():
    if _es_dominio_app():
        return "https://" + DOMINIO_APP + "/auth/callback"
    return _origen_publico() + "/auth/callback"


def _cliente_auth_oauth():
    url, servicio = _supabase_creds()
    publica = _clave_publica_supabase()
    clave = publica if publica and _tipo_clave_supabase(publica) != "service" else servicio
    return _crear_cliente_supabase(url, clave)


def _intercambiar_codigo_cliente(code, verifier):
    cliente = _cliente_auth_oauth()
    if cliente is None:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    try:
        respuesta = cliente.auth.exchange_code_for_session({
            "auth_code": code,
            "code_verifier": verifier,
        })
    except Exception as e:
        print(f"--> oauth cliente intercambio {_detalle_error(e)}")
        raise
    sesion = getattr(respuesta, "session", None)
    access = getattr(sesion, "access_token", None) if sesion is not None else None
    if isinstance(sesion, dict):
        access = sesion.get("access_token")
    usuario = getattr(respuesta, "user", None)
    if usuario is None and sesion is not None:
        usuario = sesion.get("user") if isinstance(sesion, dict) else getattr(sesion, "user", None)
    if not access:
        print("--> oauth cliente intercambio sin access_token")
        return None
    return {"access_token": access, "user": _modelo_auth_dict(usuario)}


def _intercambiar_codigo_pkce(code, verifier):
    error_cliente = None
    try:
        data = _intercambiar_codigo_cliente(code, verifier)
        if data and data.get("access_token"):
            return data
    except Exception as e:
        error_cliente = e
    cuerpo = {"auth_code": code, "code_verifier": verifier}
    ultimo_status = 0
    ultimo_msg = ""
    for clave in _claves_intercambio_oauth():
        status, data = _auth_http(
            "POST",
            "/auth/v1/token?grant_type=pkce",
            cuerpo,
            clave=clave,
        )
        if status == 200 and data.get("access_token"):
            return data
        ultimo_status = status
        ultimo_msg = _mensaje_auth(data)
        print(f"--> oauth google intercambio status={status} msg={ultimo_msg[:180]}")
    if error_cliente:
        raise error_cliente
    if not ultimo_status:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    raise ErrorAuth("oauth", f"Could not finish Google sign-in. status={ultimo_status} {ultimo_msg[:160]}", 401)


def _contratista_por_email_con_jwt(access_token, email):
    email = str(email or "").strip().lower()
    token = str(access_token or "").strip()
    if not email or not token:
        return None
    url, key = _supabase_creds()
    apikey = _clave_publica_supabase() or key
    if not url or not apikey:
        return None
    try:
        import httpx
        from urllib.parse import quote
        headers = {
            "apikey": apikey,
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }
        dest = (
            f"{url}/rest/v1/{TABLA_CONTRATISTAS}"
            f"?email=eq.{quote(email)}"
            "&select=id,email,nombre_empresa,token,user_id&limit=1"
        )
        with httpx.Client(verify=False, timeout=20.0) as http:
            res = http.get(dest, headers=headers)
        if res.status_code != 200:
            print(f"--> oauth rest jwt status={res.status_code}")
            return None
        data = res.json()
        rows = data if isinstance(data, list) else []
        return rows[0] if rows else None
    except Exception as err:
        print(f"--> oauth rest jwt: {err}")
        return None


def _sesion_contratista_oauth(access_token, user=None):
    user = _usuario_oauth(access_token, user)
    email = _email_usuario_auth(user)
    uid = str((user or {}).get("id") or "").strip()
    row = None
    try:
        row = asegurar_contratista_oauth(email, _nombre_oauth(user), uid)
    except Exception as err:
        print(f"--> oauth alta/perfil: {err}")
        try:
            row = _contratista_por_email_tabla(_supabase_client(), email)
        except Exception as err2:
            print(f"--> oauth fallback email: {err2}")
            row = None
    if not row or not row.get("token"):
        try:
            row = _contratista_por_email_con_jwt(access_token, email)
            if row and row.get("token"):
                print("--> oauth entra con JWT de Google tras fallo de llaves/perfil")
        except Exception as err:
            print(f"--> oauth jwt fallback: {err}")
            row = None
    if not row or not row.get("token"):
        raise ErrorAuth("sin_cuenta", "Could not open the contractor account.", 404)
    print(f"--> sesion google email={email}")
    return row


def sesion_oauth_google(code, verifier):
    code = str(code or "").strip()
    verifier = str(verifier or "").strip()
    print(f"--> oauth sesion entra code={bool(code)} verifier={bool(verifier)} verifier_len={len(verifier)}")
    if not code or not verifier:
        print(f"--> oauth google incompleto code={bool(code)} verifier={bool(verifier)}")
        raise ErrorAuth("enlace", "The Google sign-in expired. Try again.", 400)
    try:
        data = _intercambiar_codigo_pkce(code, verifier)
    except Exception as err:
        _log_oauth_fallo("intercambio", err)
        raise
    user = data.get("user") if isinstance(data.get("user"), dict) else None
    try:
        return _sesion_contratista_oauth(data.get("access_token"), user)
    except Exception as err:
        _log_oauth_fallo(_etapa_oauth(err), err, extra="despues_intercambio")
        try:
            email = _email_usuario_auth(user)
            if email:
                row = _contratista_por_email_tabla(_supabase_client(), email)
                if row and row.get("token"):
                    print("--> oauth entra con cuenta existente tras fallo de perfil/contadores")
                    return row
        except Exception as err2:
            print(f"--> oauth rescate sesion: {err2}")
        raise


def sesion_desde_token_google(access_token):
    token = str(access_token or "").strip()
    if not token:
        raise ErrorAuth("oauth", "Could not finish Google sign-in.", 401)
    return _sesion_contratista_oauth(token, None)


def restablecer_contrasena(password, access_token="", code="", code_verifier=""):
    password = _validar_password(password)
    token = str(access_token or "").strip()
    user = None
    if not token:
        codigo = str(code or "").strip()
        verifier = str(code_verifier or "").strip()
        if not codigo or not verifier:
            raise ErrorAuth("enlace", "The reset link expired. Request a new one.", 400)
        status, data = _auth_http("POST", "/auth/v1/token?grant_type=pkce", {
            "auth_code": codigo,
            "code_verifier": verifier,
        })
        if status != 200 or not data.get("access_token"):
            print(f"--> restablecer intercambio status={status}")
            raise ErrorAuth("enlace", "The reset link expired. Request a new one.", 400)
        token = data.get("access_token")
        user = data.get("user") if isinstance(data.get("user"), dict) else None
    status, data = _auth_como_usuario("PUT", "/auth/v1/user", token, {"password": password})
    if status not in (200, 201):
        print(f"--> restablecer password status={status}")
        raise ErrorAuth("enlace", "The reset link expired. Request a new one.", 400)
    if not isinstance(user, dict) or not _correo_valido(user.get("email")):
        user = data if isinstance(data, dict) else {}
    email = str(user.get("email") or "").strip().lower()
    if not _correo_valido(email) and isinstance(user.get("user"), dict):
        email = str(user["user"].get("email") or "").strip().lower()
    if not _correo_valido(email):
        raise ErrorAuth("enlace", "The reset link expired. Request a new one.", 400)
    sb = _supabase_client()
    if sb is None:
        raise ErrorAuth("config", "Supabase no configurado", 503)
    row = _contratista_por_email_tabla(sb, email)
    print(f"--> contrasena restablecida email={email}")
    return row if row and row.get("token") else None


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
        if _error_sin_funcion_tenant(err):
            _aviso_multitenant(err)
            _alerta_critica("sesion", err, conexion="supabase")
            return None, (jsonify({"error": _MSG_LOGIN_AMABLE, "code": "reintento"}), 503)
        print(f"--> sesion contratista: {type(err).__name__}")
        _alerta_desde_error(err, "sesion")
        return None, (jsonify({"error": _MSG_LOGIN_AMABLE, "code": "reintento"}), 503)
    if not contratista or not _correo_valido(contratista.get("email")):
        return None, (jsonify({"error": "Contractor not authorized"}), 403)
    contratista["token"] = token
    sb = _supabase_client()
    if sb is not None:
        try:
            uid = _user_id_de_contratista(sb, contratista)
            if uid:
                contratista["user_id"] = uid
            contratista.update(_datos_prueba(sb, contratista))
        except Exception as err:
            print(f"--> sesion perfil/contadores: {err}")
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
        if clave in ("contratista_id", "user_id"):
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
    uid = _user_id_de_contratista(sb, contratista)
    if uid:
        fila["user_id"] = uid
    else:
        fila.pop("user_id", None)
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

    def _escribir(metodo, payload, ident=None):
        query = getattr(sb.table(TABLA_HISTORIAL), metodo)(payload)
        if ident:
            query = query.eq("id", ident).eq("contratista_id", cid)
        try:
            return query.execute()
        except Exception as err:
            if "user_id" in payload and _marcar_sin_user_id(err):
                limpio = dict(payload)
                limpio.pop("user_id", None)
                query = getattr(sb.table(TABLA_HISTORIAL), metodo)(limpio)
                if ident:
                    query = query.eq("id", ident).eq("contratista_id", cid)
                return query.execute()
            raise

    if existente:
        ident = existente.get("id")
        update = {k: v for k, v in fila.items() if k != "id"}
        _escribir("update", update, ident)
        return _buscar_historial_por_id(sb, ident, cid) or {**existente, **update, "id": ident}
    insert = {k: v for k, v in fila.items() if k != "id"}
    res = _escribir("insert", insert)
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


def _sembrar_estimado_ejemplo(contratista):
    if not contratista or not contratista.get("id") or not contratista.get("token"):
        return
    try:
        existentes = listar_historial_tenant(contratista)
        if existentes:
            return
        guardado = guardar_historial_tenant(contratista, _fila_estimado_ejemplo())
        if guardado and guardado.get("id"):
            print(f"--> estimado ejemplo id={guardado.get('id')} contratista={contratista.get('id')}")
    except Exception as err:
        print(f"--> estimado ejemplo: {err}")


def _completar_alta_contratista(sb, row, uid):
    try:
        row = _asegurar_user_id_contratista(sb, row, uid)
    except Exception as err:
        print(f"--> alta user_id: {err}")
    try:
        _sembrar_estimado_ejemplo(row)
    except Exception as err:
        print(f"--> alta estimado ejemplo: {err}")
    return row


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
        return jsonify({
            "error": "This email already has an account. Use Log in.",
            "code": "already_registered",
        }), 409
    if "unauthorized" in texto:
        return jsonify({"error": "Contractor not authorized"}), 403
    if "invalid_contractor" in texto:
        return jsonify({"error": "Enter a valid email.", "code": "email"}), 400
    if re.search(r"duplicate key|contratistas_email", texto, re.I):
        return jsonify({"error": "That contractor email is already in use."}), 409
    if _error_sin_funcion_tenant(err):
        _aviso_multitenant(err)
        return jsonify({"error": _SQL_MULTITENANT}), 503
    _aviso_historial_faltante(err)
    return jsonify({"error": texto}), 500


@app.route("/api/contratistas", methods=["POST", "PATCH"])
def api_contratistas():
    body = request.get_json(silent=True) or {}
    if request.method == "POST":
        email = str(body.get("email") or "").strip().lower()
        nombre = str(body.get("nombre") or body.get("nombre_empresa") or body.get("company") or "").strip()
        password = body.get("password") or body.get("contrasena") or ""
        plan = str(body.get("plan_seleccionado") or body.get("plan") or "").strip()
        if not _correo_valido(email):
            return jsonify({"error": "Enter a valid email.", "code": "email"}), 400
        try:
            row = registrar_contratista(email, nombre, password, plan)
        except ErrorAuth as err:
            print(f"--> registro contratista {err.code} status={err.status}: {err}")
            return _respuesta_auth(err)
        except Exception as err:
            print(f"--> registro contratista {type(err).__name__}: {err}")
            return _respuesta_login_silenciosa(err, "registro")
        if not row or not row.get("token"):
            _alerta_critica("registro", "alta sin token", conexion="supabase")
            return jsonify({"error": _MSG_LOGIN_AMABLE, "code": "reintento"}), 503
        print(f"--> contratista registrado id={row.get('id')}")
        resp = jsonify(_sesion_desde_fila(row))
        return _adjuntar_sesion_contratista(resp, row.get("token"), persistir=True)
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
    resp = jsonify(row)
    return _adjuntar_sesion_contratista(resp, contratista.get("token"))


@app.route("/api/contratistas/plan", methods=["POST"])
def api_guardar_plan_contratista():
    body = request.get_json(silent=True) or {}
    contratista, error = _exigir_contratista()
    if error:
        return error
    try:
        plan = guardar_plan_contratista(
            contratista,
            body.get("plan_seleccionado") or body.get("plan") or "",
        )
    except ErrorAuth as err:
        return _respuesta_auth(err)
    except Exception as err:
        print(f"--> guardar plan {type(err).__name__}: {err}")
        return _respuesta_tenant(err)
    contratista["plan_seleccionado"] = plan
    sb = _supabase_client()
    if sb is not None:
        contratista.update(_datos_prueba(sb, contratista))
    payload = _sesion_desde_fila(contratista)
    payload["requiere_pago"] = False
    payload["siguiente"] = "estimado"
    resp = jsonify(payload)
    return _adjuntar_sesion_contratista(resp, contratista.get("token"))


@app.route("/api/pagos/simular", methods=["POST"])
def api_pagos_simular():
    contratista, error = _exigir_contratista()
    if error:
        return error
    if _es_bypass_facturacion(contratista.get("email")):
        body = request.get_json(silent=True) or {}
        plan = _plan_valido(
            body.get("plan_seleccionado") or body.get("plan") or contratista.get("plan_seleccionado")
        ) or "premium"
        _activar_plan_bypass_admin(contratista, plan)
        sb = _supabase_client()
        if sb is not None:
            contratista.update(_datos_prueba(sb, contratista))
        payload = _sesion_desde_fila(contratista)
        payload["ok"] = True
        payload["bypass"] = True
        payload["requiere_pago"] = False
        payload["trial_expirada"] = False
        payload["plan_seleccionado"] = plan
        payload["nombre_plan"] = _nombre_plan(plan)
        payload["siguiente"] = "estimado"
        resp = jsonify(payload)
        return _adjuntar_sesion_contratista(resp, contratista.get("token"))
    body = request.get_json(silent=True) or {}
    numero = re.sub(r"\D", "", str(body.get("numero") or body.get("card") or ""))
    vencimiento = re.sub(r"\D", "", str(body.get("vencimiento") or body.get("fecha") or ""))
    cvc = re.sub(r"\D", "", str(body.get("cvc") or ""))
    if not numero or not vencimiento or not cvc:
        return jsonify({"error": "Enter the card number, expiration date, and CVC.", "code": "tarjeta"}), 400
    activar = bool(body.get("activar") or body.get("paywall") or _prueba_expirada(contratista))
    uid = str(contratista.get("user_id") or "").strip()
    email = str(contratista.get("email") or "").strip().lower()
    plan = _plan_valido(contratista.get("plan_seleccionado"))
    if not plan:
        return jsonify({"error": "Choose a plan before adding your card.", "code": "plan"}), 400
    if not uid:
        return jsonify({"error": "No se pudo identificar tu cuenta.", "code": "sin_cuenta"}), 404
    customer_id = "sim_" + uid.replace("-", "")[:12] + "_" + (numero[-4:] if len(numero) >= 4 else numero)
    if not _guardar_pago_cliente(_supabase_client(), uid, email, customer_id, plan, activar=activar):
        print("--> pago simulado: no se pudo persistir en perfiles")
    sb = _supabase_client()
    if sb is not None:
        contratista.update(_datos_prueba(sb, contratista))
    contratista["pago_cliente_id"] = customer_id
    contratista["requiere_pago"] = False
    if activar:
        contratista["trial_expirada"] = False
        if not contratista.get("fecha_fin_prueba"):
            contratista["fecha_fin_prueba"] = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()
    payload = _sesion_desde_fila(contratista)
    payload["pago_cliente_id"] = customer_id
    payload["requiere_pago"] = False
    payload["trial_expirada"] = False if activar else bool(payload.get("trial_expirada"))
    payload["nombre_plan"] = _nombre_plan(plan)
    payload["siguiente"] = "estimado"
    resp = jsonify(payload)
    return _adjuntar_sesion_contratista(resp, contratista.get("token"))


@app.route("/api/pagos/checkout", methods=["POST"])
def api_pagos_checkout():
    contratista, error = _exigir_contratista()
    if error:
        return error
    if _es_bypass_facturacion(contratista.get("email")):
        plan = _plan_valido(contratista.get("plan_seleccionado")) or "premium"
        _activar_plan_bypass_admin(contratista, plan)
        return jsonify({
            "ok": True,
            "bypass": True,
            "url": "/estimado",
            "siguiente": "estimado",
            "plan": plan,
            "nombre_plan": _nombre_plan(plan),
            "requiere_pago": False,
            "trial_expirada": False,
        })
    stripe = _stripe_mod()
    if stripe is None:
        return jsonify({
            "error": "Pagos no configurados. Agrega STRIPE_SECRET_KEY en Render.",
            "code": "stripe_config",
        }), 503
    plan = _plan_valido(contratista.get("plan_seleccionado"))
    if not plan:
        return jsonify({"error": "Choose a plan before adding your card.", "code": "plan"}), 400
    info = _PLANES_PRECIOS[plan]
    uid = str(contratista.get("user_id") or "").strip()
    email = str(contratista.get("email") or "").strip().lower()
    customer_id = str(contratista.get("pago_cliente_id") or "").strip()
    base = _url_publica_app()
    params = {
        "mode": "subscription",
        "line_items": [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "QuantixBuilds · " + info["nombre"]},
                "unit_amount": info["centavos"],
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        }],
        "subscription_data": {
            "trial_period_days": 14,
            "metadata": {"user_id": uid, "plan": plan, "email": email},
        },
        "metadata": {"user_id": uid, "plan": plan, "email": email},
        "payment_method_collection": "always",
        "locale": "es",
    }
    if uid:
        params["client_reference_id"] = uid
    if customer_id:
        params["customer"] = customer_id
    elif email:
        params["customer_email"] = email
    publica = _stripe_publica()
    extra = {
        "plan": plan,
        "nombre_plan": info["nombre"],
        "precio": info["centavos"] // 100,
    }
    if publica:
        try:
            session = stripe.checkout.Session.create(
                ui_mode="embedded",
                return_url=base + "/pago/exito?session_id={CHECKOUT_SESSION_ID}",
                **params,
            )
            extra["clientSecret"] = session.client_secret
            extra["publishableKey"] = publica
            return jsonify(extra)
        except Exception as err:
            print(f"--> stripe embedded: {type(err).__name__}: {err}")
    try:
        session = stripe.checkout.Session.create(
            success_url=base + "/pago/exito?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base + "/pago",
            **params,
        )
    except Exception as err:
        print(f"--> stripe checkout: {type(err).__name__}: {err}")
        return jsonify({"error": "No se pudo abrir el formulario de pago.", "code": "stripe"}), 502
    extra["url"] = session.url
    return jsonify(extra)


@app.route("/api/pagos/confirmar", methods=["POST"])
def api_pagos_confirmar():
    contratista, error = _exigir_contratista()
    if error:
        return error
    if _es_bypass_facturacion(contratista.get("email")):
        plan = _plan_valido(contratista.get("plan_seleccionado")) or "premium"
        _activar_plan_bypass_admin(contratista, plan)
        sb = _supabase_client()
        if sb is not None:
            contratista.update(_datos_prueba(sb, contratista))
        payload = _sesion_desde_fila(contratista)
        payload["ok"] = True
        payload["bypass"] = True
        payload["siguiente"] = "estimado"
        payload["requiere_pago"] = False
        payload["trial_expirada"] = False
        resp = jsonify(payload)
        return _adjuntar_sesion_contratista(resp, contratista.get("token"))
    stripe = _stripe_mod()
    if stripe is None:
        return jsonify({"error": "Pagos no configurados.", "code": "stripe_config"}), 503
    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "Falta la sesión de pago.", "code": "sesion"}), 400
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as err:
        print(f"--> stripe confirmar: {type(err).__name__}: {err}")
        return jsonify({"error": "No se pudo confirmar el pago.", "code": "stripe"}), 502
    meta = session.get("metadata") or {}
    uid = str(contratista.get("user_id") or "").strip()
    email = str(contratista.get("email") or "").strip().lower()
    dueño = str(meta.get("user_id") or session.get("client_reference_id") or "").strip()
    if dueño and uid and dueño != uid:
        return jsonify({"error": "Esta sesión de pago no coincide con tu cuenta.", "code": "dueño"}), 403
    if not _aplicar_sesion_checkout(session, contratista):
        return jsonify({"error": "Stripe aún no confirmó la tarjeta. Intenta de nuevo.", "code": "incompleto"}), 409
    sb = _supabase_client()
    if sb is not None:
        contratista.update(_datos_prueba(sb, contratista))
    resp = jsonify(_sesion_desde_fila(contratista))
    return _adjuntar_sesion_contratista(resp, contratista.get("token"))


_SQUARE_VERSION = "2025-01-23"
_SQUARE_PLAN_VARIATION_ID = ""
_SQUARE_LOCATION_ID = ""


def _square_token():
    return (os.getenv("SQUARE_ACCESS_TOKEN") or os.getenv("SQUARE_TOKEN") or "").strip()


def _square_sandbox():
    env = (os.getenv("SQUARE_ENVIRONMENT") or os.getenv("SQUARE_ENV") or "").strip().lower()
    if env in ("sandbox", "test"):
        return True
    if env in ("production", "prod", "live"):
        return False
    return False


def _square_host():
    return "https://connect.squareupsandbox.com" if _square_sandbox() else "https://connect.squareup.com"


def _square_headers():
    token = _square_token()
    if not token:
        return None
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Square-Version": _SQUARE_VERSION,
        "Accept": "application/json",
    }


def _square_http(method, path, body=None):
    headers = _square_headers()
    if not headers:
        raise RuntimeError("Falta SQUARE_ACCESS_TOKEN en .env")
    url = _square_host() + path
    resp = None
    try:
        import requests as req
        resp = req.request(method, url, headers=headers, json=body, timeout=30)
        data = resp.json() if resp.content else {}
    except Exception as err:
        raise RuntimeError("Square no respondió") from err
    if resp.status_code >= 400:
        errores = data.get("errors") if isinstance(data, dict) else None
        detalle = ""
        if isinstance(errores, list) and errores:
            detalle = str((errores[0] or {}).get("detail") or (errores[0] or {}).get("code") or "")
        raise RuntimeError(detalle or ("Square HTTP " + str(resp.status_code)))
    return data if isinstance(data, dict) else {}


def _square_location_id():
    global _SQUARE_LOCATION_ID
    env = (os.getenv("SQUARE_LOCATION_ID") or "").strip()
    if env:
        return env
    if _SQUARE_LOCATION_ID:
        return _SQUARE_LOCATION_ID
    data = _square_http("GET", "/v2/locations")
    for loc in data.get("locations") or []:
        if str((loc or {}).get("status") or "").upper() == "ACTIVE":
            _SQUARE_LOCATION_ID = str(loc.get("id") or "")
            if _SQUARE_LOCATION_ID:
                return _SQUARE_LOCATION_ID
    locs = data.get("locations") or []
    if locs:
        _SQUARE_LOCATION_ID = str((locs[0] or {}).get("id") or "")
    return _SQUARE_LOCATION_ID


def _square_nota_pago(plan, email, user_id):
    return "qb|" + str(plan or "premium") + "|" + str(email or "") + "|" + str(user_id or "")


def _square_parse_nota(nota):
    texto = str(nota or "")
    partes = texto.split("|")
    if len(partes) >= 3 and partes[0] == "qb":
        plan = _plan_valido(partes[1]) or "premium"
        email = str(partes[2] or "").strip().lower()
        uid = str(partes[3] if len(partes) > 3 else "").strip()
        return plan, email, uid
    return "premium", "", ""


def _square_buscar_email(obj):
    if not isinstance(obj, dict):
        return ""
    for clave in ("buyer_email_address", "buyer_email", "email_address", "email"):
        valor = str(obj.get(clave) or "").strip().lower()
        if valor and _correo_valido(valor):
            return valor
    rec = obj.get("primary_recipient") if isinstance(obj.get("primary_recipient"), dict) else {}
    valor = str(rec.get("email_address") or rec.get("email") or "").strip().lower()
    if valor and _correo_valido(valor):
        return valor
    _, email, _ = _square_parse_nota(obj.get("note") or obj.get("payment_note") or "")
    return email


def _square_plan_variation_id(plan, centavos):
    global _SQUARE_PLAN_VARIATION_ID
    env = (os.getenv("SQUARE_SUBSCRIPTION_PLAN_ID") or os.getenv("SQUARE_PLAN_VARIATION_ID") or "").strip()
    if env:
        return env
    if _SQUARE_PLAN_VARIATION_ID:
        return _SQUARE_PLAN_VARIATION_ID
    nombre = "QuantixBuilds Premium Monthly"
    try:
        data = _square_http("POST", "/v2/catalog/search", {
            "object_types": ["SUBSCRIPTION_PLAN_VARIATION"],
            "query": {"exact_query": {"attribute_name": "name", "attribute_value": nombre}},
            "limit": 5,
        })
        for obj in data.get("objects") or []:
            ident = str((obj or {}).get("id") or "")
            if ident and not ident.startswith("#"):
                _SQUARE_PLAN_VARIATION_ID = ident
                return ident
    except Exception as err:
        print(f"--> Square catalog search: {err}")
    try:
        data = _square_http("POST", "/v2/catalog/object", {
            "idempotency_key": uuid.uuid4().hex,
            "object": {
                "type": "SUBSCRIPTION_PLAN",
                "id": "#qb-premium-plan",
                "subscription_plan_data": {
                    "name": "QuantixBuilds Premium",
                    "subscription_plan_variations": [{
                        "type": "SUBSCRIPTION_PLAN_VARIATION",
                        "id": "#qb-premium-monthly",
                        "subscription_plan_variation_data": {
                            "name": nombre,
                            "phases": [{
                                "cadence": "MONTHLY",
                                "ordinal": 0,
                                "pricing": {
                                    "type": "STATIC",
                                    "price_money": {"amount": int(centavos), "currency": "USD"},
                                },
                            }],
                        },
                    }],
                },
            },
        })
        for obj in data.get("id_mappings") or []:
            if str((obj or {}).get("client_object_id") or "") == "#qb-premium-monthly":
                ident = str(obj.get("object_id") or "")
                if ident:
                    _SQUARE_PLAN_VARIATION_ID = ident
                    return ident
        creado = data.get("catalog_object") or {}
        variaciones = ((creado.get("subscription_plan_data") or {}).get("subscription_plan_variations")) or []
        if variaciones:
            ident = str((variaciones[0] or {}).get("id") or "")
            if ident:
                _SQUARE_PLAN_VARIATION_ID = ident
                return ident
    except Exception as err:
        print(f"--> Square subscription plan: {err}")
    return ""


def _crear_enlace_pago_square(contratista, plan="premium"):
    plan = _plan_valido(plan) or "premium"
    if _es_bypass_facturacion((contratista or {}).get("email")):
        _activar_plan_bypass_admin(contratista, plan)
        return "/estimado"
    info = _PLANES_PRECIOS.get(plan) or _PLANES_PRECIOS["premium"]
    email = str((contratista or {}).get("email") or "").strip().lower()
    uid = str((contratista or {}).get("user_id") or "").strip()
    if not email or not _correo_valido(email):
        raise RuntimeError("Your account has no valid email for Square checkout.")
    location_id = _square_location_id()
    if not location_id:
        raise RuntimeError("Falta SQUARE_LOCATION_ID. Agrégalo al .env o activa una Location en Square.")
    body = {
        "idempotency_key": uuid.uuid4().hex,
        "description": "QuantixBuilds " + info["nombre"] + " for " + email,
        "quick_pay": {
            "name": "QuantixBuilds · " + info["nombre"],
            "price_money": {"amount": int(info["centavos"]), "currency": "USD"},
            "location_id": location_id,
        },
        "checkout_options": {
            "redirect_url": _url_publica_app() + "/estimado",
            "ask_for_shipping_address": False,
        },
        "pre_populated_data": {"buyer_email": email},
        "payment_note": _square_nota_pago(plan, email, uid),
    }
    variation = _square_plan_variation_id(plan, info["centavos"])
    if variation:
        body["checkout_options"]["subscription_plan_id"] = variation
    data = _square_http("POST", "/v2/online-checkout/payment-links", body)
    enlace = (data.get("payment_link") or {}).get("url") or data.get("url") or ""
    if not enlace:
        raise RuntimeError("Square no devolvió un enlace de pago.")
    return enlace


def _square_firma_valida(cuerpo):
    clave = (os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY") or os.getenv("SQUARE_WEBHOOK_SIGNATURE") or "").strip()
    firma = (
        request.headers.get("x-square-hmacsha256-signature")
        or request.headers.get("X-Square-HmacSha256-Signature")
        or request.headers.get("Square-Signature")
        or ""
    ).strip()
    if not clave:
        print("--> Square webhook: falta SQUARE_WEBHOOK_SIGNATURE_KEY")
        return True
    if not firma:
        return False
    url = (os.getenv("SQUARE_WEBHOOK_URL") or "").strip() or (_url_publica_app() + "/square-webhook")
    if isinstance(cuerpo, bytes):
        texto = cuerpo.decode("utf-8")
    else:
        texto = str(cuerpo or "")
    digest = hmac.new(clave.encode("utf-8"), (url + texto).encode("utf-8"), hashlib.sha256).digest()
    esperada = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(esperada, firma)


def _activar_premium_por_square(email, customer_id="", plan="premium", user_id=""):
    email = str(email or "").strip().lower()
    plan = _plan_valido(plan) or "premium"
    if not email or not _correo_valido(email):
        print("--> Square webhook: pago sin email de cliente")
        return False
    sb = _supabase_client()
    if sb is None:
        return False
    ahora = datetime.now(timezone.utc)
    cambios = {
        "plan_seleccionado": plan,
        "requiere_pago": False,
        "ai_pdf_extractions_count": 0,
        "fecha_fin_prueba": (ahora + timedelta(days=3650)).isoformat(),
        "updated_at": ahora.isoformat(),
    }
    if customer_id:
        cambios["pago_cliente_id"] = str(customer_id)
    uid = str(user_id or "").strip()
    if not uid:
        try:
            res = sb.table("perfiles").select("id").eq("email", email).limit(1).execute()
            filas = list(res.data or [])
            if filas:
                uid = str(filas[0].get("id") or "")
        except Exception as err:
            print(f"--> Square perfil email: {err}")
    ok = False
    try:
        query = sb.table("perfiles").update(cambios)
        if uid:
            query = query.eq("id", uid)
        else:
            query = query.eq("email", email)
        res = query.execute()
        ok = bool(list(res.data or []))
    except Exception as err:
        if "ai_pdf_extractions_count" in str(err or ""):
            cambios.pop("ai_pdf_extractions_count", None)
            try:
                query = sb.table("perfiles").update(cambios)
                query = query.eq("id", uid) if uid else query.eq("email", email)
                res = query.execute()
                ok = bool(list(res.data or []))
            except Exception as err2:
                print(f"--> Square activar perfil: {err2}")
        else:
            print(f"--> Square activar perfil: {err}")
    try:
        sb.table(TABLA_CONTRATISTAS).update({
            "ai_pdf_extractions_count": 0,
            "updated_at": ahora.isoformat(),
        }).eq("email", email).execute()
    except Exception:
        pass
    if ok:
        print(f"--> Square pago OK plan={plan} email={email}")
    else:
        print(f"--> Square pago sin fila de perfil email={email}")
    return ok


def _procesar_evento_square(event):
    if not isinstance(event, dict):
        return
    tipo = str(event.get("type") or event.get("event_type") or "").lower()
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else data
    if "payment" in obj and isinstance(obj.get("payment"), dict):
        obj = obj.get("payment")
    if "invoice" in obj and isinstance(obj.get("invoice"), dict) and "payment" not in tipo:
        obj = obj.get("invoice")
    if "subscription" in obj and isinstance(obj.get("subscription"), dict) and "subscription" in tipo:
        obj = obj.get("subscription")
    if tipo.startswith("payment."):
        estado = str(obj.get("status") or "").upper()
        if tipo == "payment.created" and estado not in ("COMPLETED", "APPROVED"):
            return
        if tipo == "payment.updated" and estado not in ("COMPLETED", "APPROVED"):
            return
    elif tipo not in (
        "invoice.payment_made",
        "invoice.scheduled_charge_collected",
        "subscription.created",
        "subscription.updated",
    ):
        return
    email = _square_buscar_email(obj)
    plan, email_nota, uid = _square_parse_nota(obj.get("note") or obj.get("payment_note") or "")
    email = email or email_nota
    customer_id = str(obj.get("customer_id") or obj.get("customer_id") or "").strip()
    if not email:
        cid = customer_id
        if cid:
            try:
                cust = _square_http("GET", "/v2/customers/" + cid)
                persona = cust.get("customer") or {}
                email = str(persona.get("email_address") or "").strip().lower()
            except Exception as err:
                print(f"--> Square customer: {err}")
        order_id = str(obj.get("order_id") or "").strip()
        if not email and order_id:
            try:
                pedido = _square_http("GET", "/v2/orders/" + order_id)
                orden = (pedido.get("order") or {})
                _, email, uid = _square_parse_nota(orden.get("reference_id") or orden.get("ticket_name") or "")
                if not email:
                    email = _square_buscar_email(orden)
                plan = plan or "premium"
            except Exception as err:
                print(f"--> Square order: {err}")
    if not email:
        print("--> Square webhook: no se pudo resolver el email del pagador")
        return
    _activar_premium_por_square(email, customer_id=customer_id, plan=plan or "premium", user_id=uid)


@app.route("/checkout")
def checkout_square():
    if not _token_contratista_solicitud():
        return redirect("/login")
    contratista, error = _exigir_contratista()
    if error or not contratista:
        return redirect("/login")
    plan = _plan_valido(request.args.get("plan") or contratista.get("plan_seleccionado")) or "premium"
    try:
        url = _crear_enlace_pago_square(contratista, plan)
    except Exception as err:
        print(f"--> Square checkout: {err}")
        return Response(
            "Could not open Square checkout. Check SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID.",
            status=502,
            mimetype="text/plain; charset=utf-8",
        )
    return redirect(url, code=303)


@app.route("/square-webhook", methods=["GET", "POST"])
def square_webhook():
    if request.method == "GET":
        return jsonify({"ok": True, "service": "square-webhook"}), 200
    raw = request.get_data() or b""
    if not _square_firma_valida(raw):
        return jsonify({"error": "invalid signature"}), 403
    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return jsonify({"error": "json"}), 400
    threading.Thread(target=_procesar_evento_square, args=(event,), daemon=True, name="square-webhook").start()
    return jsonify({"ok": True}), 200


@app.route("/api/pagos/webhook", methods=["POST"])
def api_pagos_webhook():
    stripe = _stripe_mod()
    if stripe is None:
        return jsonify({"error": "stripe"}), 503
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature")
    secret = _stripe_webhook_secreta()
    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        else:
            event = json.loads(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload)
    except Exception as err:
        print(f"--> stripe webhook: {type(err).__name__}: {err}")
        return jsonify({"error": "firma"}), 400
    if not isinstance(event, dict):
        event = dict(event)
    tipo = str(event.get("type") or "")
    data = ((event.get("data") or {}).get("object")) or {}
    if tipo in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        _aplicar_sesion_checkout(data)
    return jsonify({"ok": True})


@app.route("/api/contratistas/login", methods=["POST"])
def api_login_contratista():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = body.get("password") or body.get("contrasena") or ""
    if not _correo_valido(email):
        return jsonify({"error": "Enter a valid email.", "code": "email"}), 400
    try:
        row = iniciar_sesion_contratista(email, password)
    except ErrorAuth as err:
        return _respuesta_auth(err)
    except Exception as err:
        return _respuesta_login_silenciosa(err, "login")
    if not row or not row.get("token"):
        return jsonify({"error": "No contractor account uses this email. Create one first.", "code": "sin_cuenta"}), 404
    try:
        payload = _sesion_desde_fila(row)
    except Exception as err:
        print(f"--> login sesion perfil/contadores: {err}")
        payload = {
            "id": row.get("id"),
            "email": row.get("email"),
            "nombre_empresa": row.get("nombre_empresa") or "",
            "token": row.get("token"),
            "user_id": row.get("user_id") or "",
        }
    resp = jsonify(payload)
    return _adjuntar_sesion_contratista(
        resp,
        row.get("token"),
        persistir=_recordarme_activo(body.get("recordarme"), True),
    )


@app.route("/api/contratistas/recuperar", methods=["POST"])
def api_recuperar_contrasena():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    if not _correo_valido(email):
        return jsonify({"error": "Enter a valid email.", "code": "email"}), 400
    payload = {"email": email, "redirect_to": _origen_publico() + "/restablecer"}
    challenge = str(body.get("code_challenge") or "").strip()
    if challenge:
        payload["code_challenge"] = challenge
        payload["code_challenge_method"] = "s256"
    try:
        status, _data = _auth_http("POST", "/auth/v1/recover", payload)
    except ErrorAuth as err:
        return _respuesta_auth(err)
    except Exception as err:
        return _respuesta_login_silenciosa(err, "recuperar")
    if status == 429:
        return jsonify({"error": "Too many attempts. Wait a moment and try again.", "code": "limite"}), 429
    if status >= 500:
        print(f"--> recuperar status={status}")
        _alerta_critica("recuperar", f"status={status}", conexion="supabase_auth")
        return jsonify({"error": _MSG_LOGIN_AMABLE, "code": "reintento"}), 503
    return jsonify({"ok": True})


def _detalle_error(err):
    texto = re.sub(r"[\r\n]+", " ", f"{type(err).__name__}: {err}").strip()
    texto = re.sub(r"(sb_secret_|sb_publishable_|eyJ)[A-Za-z0-9_\-\.]+", "[redacted]", texto)

    def ocultar_secreto(coincidencia):
        valor = coincidencia.group(2)
        if len(valor) < 24:
            return coincidencia.group(0)
        return coincidencia.group(1) + "[redacted]"

    texto = re.sub(
        r"((?:access_token|refresh_token|token|code)=)([^&\s]+)",
        ocultar_secreto,
        texto,
        flags=re.I,
    )
    return texto[:500]


def _variables_entorno_auth():
    nombres = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_KEY",
        "SUPABASE_ANON_KEY",
        "SUPABASE_PUBLISHABLE_KEY",
    )
    presentes = []
    faltantes = []
    for nombre in nombres:
        if (os.getenv(nombre) or "").strip():
            presentes.append(nombre)
        else:
            faltantes.append(nombre)
    return presentes, faltantes


def _es_fallo_infra(err):
    codigo = str(getattr(err, "code", "") or "").strip().lower()
    if codigo in ("config", "servicio", "reintento"):
        return True
    texto = str(err or "").lower()
    if not texto:
        return False
    return bool(re.search(
        r"supabase|service_role|service role|jwt|api key|apikey|"
        r"connection|connect|timeout|timed out|ssl|dns|"
        r"multitenant|postgrest|row-level|rls|"
        r"no configurado|not configured|faltan|missing",
        texto,
        re.I,
    ))


def _payload_alerta_critica(motivo, detalle="", variable="", conexion="", etapa=""):
    presentes, faltantes = _variables_entorno_auth()
    host = ""
    try:
        url, _key = _supabase_creds()
        host = url.split("//", 1)[-1].split("/", 1)[0] if url else "(sin url)"
    except Exception:
        host = "(sin url)"
    return {
        "motivo": str(motivo or "desconocido"),
        "etapa": str(etapa or motivo or ""),
        "variable": str(variable or "").strip(),
        "conexion": str(conexion or host or "").strip(),
        "detalle": _detalle_error(detalle) if detalle is not None and not isinstance(detalle, str) else re.sub(
            r"(sb_secret_|sb_publishable_|eyJ)[A-Za-z0-9_\-\.]+",
            "[redacted]",
            str(detalle or ""),
        )[:500],
        "faltantes": faltantes,
        "presentes": presentes,
        "ruta": "",
        "cuando": datetime.now(timezone.utc).isoformat(),
    }


def _imprimir_alerta_critica(payload):
    variable = payload.get("variable") or ""
    faltantes = payload.get("faltantes") or []
    conexion = payload.get("conexion") or ""
    motivo = payload.get("motivo") or ""
    detalle = payload.get("detalle") or ""
    partes = [f"ALERTA CRITICA: {motivo}"]
    if variable:
        partes.append(f"variable={variable}")
    if faltantes:
        partes.append("faltan=" + ",".join(faltantes))
    if conexion:
        partes.append(f"conexion={conexion}")
    if payload.get("ruta"):
        partes.append(f"ruta={payload.get('ruta')}")
    if detalle:
        partes.append(detalle[:300])
    print(" | ".join(partes), flush=True)


def _enviar_alerta_en_silencio(payload):
    try:
        enviador = globals().get("_enviar_correo_alerta_admin")
        if callable(enviador):
            enviador(payload)
            print(f"ALERTA CRITICA: correo interno enviado a {_ALERTA_ADMIN_EMAIL}", flush=True)
            return
        _ALERTAS_CRITICAS_PENDIENTES.append(payload)
        print("ALERTA CRITICA: correo pendiente (SMTP aun no listo)", flush=True)
    except Exception as err:
        print(f"ALERTA CRITICA: no se pudo enviar el correo interno: {err}", flush=True)


def _alerta_critica(motivo, detalle="", variable="", conexion="", etapa=""):
    payload = _payload_alerta_critica(motivo, detalle, variable, conexion, etapa)
    try:
        payload["ruta"] = str(getattr(request, "path", "") or "")
    except Exception:
        payload["ruta"] = ""
    _imprimir_alerta_critica(payload)
    clave = "|".join([payload.get("motivo") or "", payload.get("variable") or "", payload.get("conexion") or ""])
    ahora = time.time()
    previa = float(_ALERTAS_CRITICAS_TS.get(clave) or 0)
    if ahora - previa < _ALERTA_MINIMA_SEG:
        print("ALERTA CRITICA: correo omitido (rate limit)", flush=True)
        return payload
    _ALERTAS_CRITICAS_TS[clave] = ahora
    try:
        threading.Thread(
            target=_enviar_alerta_en_silencio,
            args=(payload,),
            daemon=True,
            name="alerta-admin",
        ).start()
    except Exception as err:
        print(f"ALERTA CRITICA: no se pudo programar el correo interno: {err}", flush=True)
        _ALERTAS_CRITICAS_PENDIENTES.append(payload)
    return payload


def _alerta_desde_error(err, etapa="login"):
    texto = _detalle_error(err) if err is not None else ""
    variable = ""
    bajo = texto.lower()
    if _FALTA_SERVICE_ROLE or "service_role" in bajo or "service role" in bajo:
        variable = "SUPABASE_SERVICE_ROLE_KEY"
    elif "supabase_url" in bajo or "sin url" in bajo:
        variable = "SUPABASE_URL"
    conexion = "supabase"
    if "smtp" in bajo:
        conexion = "smtp"
    etapa_n = str(etapa or "").lower()
    es_infra = (
        _FALTA_SERVICE_ROLE
        or _es_fallo_infra(err)
        or any(x in etapa_n for x in ("config", "servicio", "cookie", "supabase_redirect", "conexion", "oauth_inicio"))
    )
    if es_infra:
        _alerta_critica(etapa or "login", detalle=texto, variable=variable, conexion=conexion, etapa=etapa)
        return True
    print(f"--> auth fallo etapa={etapa} {texto}", flush=True)
    return False


def _respuesta_login_silenciosa(err, etapa="login"):
    _alerta_desde_error(err, etapa)
    return jsonify({"error": _MSG_LOGIN_AMABLE, "code": "reintento"}), 503


def _oauth_destino_silencioso():
    token = ""
    try:
        token = str(request.cookies.get(COOKIE_CONTRATISTA) or "").strip()
    except Exception:
        token = ""
    if token:
        try:
            contratista, error = _exigir_contratista()
            if contratista and not error:
                return "/estimado"
        except Exception as err:
            print(f"--> oauth dashboard silencioso: {err}")
    return "/login?aviso=reintento"


def _vaciar_alertas_pendientes():
    pendientes = list(_ALERTAS_CRITICAS_PENDIENTES)
    _ALERTAS_CRITICAS_PENDIENTES.clear()
    for payload in pendientes:
        threading.Thread(
            target=_enviar_alerta_en_silencio,
            args=(payload,),
            daemon=True,
            name="alerta-admin",
        ).start()


def _fallo_recuperacion(err, code, status=502):
    import traceback
    print(f"--> recuperar-password {code} {_detalle_error(err)}")
    traceback.print_exc()
    if code not in ("email",):
        _alerta_desde_error(err, "recuperar")
    return jsonify({
        "error": "We could not send the email. Please try again in a moment.",
        "code": "reintento",
    }), status


def _enlace_absoluto_auth(enlace):
    enlace = str(enlace or "").strip()
    if enlace.startswith("https://") or enlace.startswith("http://"):
        return enlace
    base = (_supabase_creds()[0] or "").rstrip("/")
    if enlace.startswith("/"):
        return base + enlace
    if enlace.startswith("auth/"):
        return base + "/" + enlace
    return enlace


def _enlace_recuperacion_admin(email):
    cliente = _supabase_client()
    if cliente is None:
        raise ErrorAuth("config", "Supabase no configurado. Revisa SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY.", 503)
    try:
        respuesta = cliente.auth.admin.generate_link({
            "type": "recovery",
            "email": email,
            "options": {"redirect_to": _origen_publico() + "/restablecer"},
        })
    except Exception as err:
        codigo = str(getattr(err, "code", "") or "").lower()
        status = getattr(err, "status", None)
        if status == 404 or codigo == "user_not_found":
            print(f"--> recuperar-password sin usuario auth status={status} code={codigo}")
            return ""
        if status == 429:
            raise ErrorAuth("limite", f"{type(err).__name__}: {err}", 429)
        raise ErrorAuth("auth_error", f"{type(err).__name__} status={status} code={codigo}: {err}", 502)
    propiedades = getattr(respuesta, "properties", None)
    enlace = _enlace_absoluto_auth(getattr(propiedades, "action_link", "") or "")
    if not (enlace.startswith("https://") or enlace.startswith("http://")):
        raise ErrorAuth("auth_error", "generate_link no devolvió action_link", 502)
    return enlace


def _html_correo_recuperacion(enlace):
    import html as html_lib
    url = html_lib.escape(enlace, quote=True)
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Restablece tu contraseña - QuantixBuilds</title>
</head>
<body style="margin:0;padding:0;background:#eef2f6;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f6;margin:0;padding:0;">
    <tr>
      <td align="center" style="padding:28px 16px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;background:#ffffff;border-radius:18px;border:1px solid #e2e8f0;">
          <tr>
            <td style="padding:32px 24px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
              <p style="margin:0 0 18px;font-size:12px;font-weight:800;letter-spacing:.16em;color:#1d4ed8;">QUANTIXBUILDS</p>
              <h1 style="margin:0 0 14px;font-size:24px;line-height:1.25;letter-spacing:-.03em;">Restablece tu contraseña</h1>
              <p style="margin:0 0 22px;font-size:16px;line-height:1.55;color:#334155;">Hola, recibimos una solicitud para cambiar tu contraseña en QuantixBuilds. Haz clic en el botón de abajo para crear una nueva.</p>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" bgcolor="#0f172a" style="border-radius:12px;">
                    <a href="{url}" style="display:block;padding:16px 18px;color:#ffffff;font-size:16px;font-weight:700;text-decoration:none;text-align:center;">Crear nueva contraseña</a>
                  </td>
                </tr>
              </table>
              <p style="margin:22px 0 0;font-size:13px;line-height:1.5;color:#64748b;">Si no pediste este cambio, puedes ignorar este correo. Tu contraseña actual sigue igual.</p>
              <p style="margin:14px 0 0;font-size:12px;line-height:1.5;color:#94a3b8;">Si el botón no abre, copia este enlace en el navegador:<br><span style="word-break:break-all;">{url}</span></p>
            </td>
          </tr>
        </table>
        <p style="margin:16px 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:12px;color:#94a3b8;">Equipo QuantixBuilds</p>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _enviar_correo_recuperacion(destino, enlace):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr
    _host, _puerto, _usuario, _clave, smtp_from = _credenciales_smtp()
    texto = (
        "Hola, recibimos una solicitud para cambiar tu contraseña en QuantixBuilds. "
        "Haz clic en el botón de abajo para crear una nueva.\n\n"
        f"{enlace}\n\n"
        "Si no pediste este cambio, puedes ignorar este correo.\n\n"
        "Equipo QuantixBuilds\n"
    )
    mensaje = MIMEMultipart("alternative")
    mensaje["Subject"] = "Restablece tu contraseña - QuantixBuilds"
    mensaje["From"] = formataddr(("Equipo QuantixBuilds", smtp_from))
    mensaje["To"] = destino
    mensaje.attach(MIMEText(texto, "plain", "utf-8"))
    mensaje.attach(MIMEText(_html_correo_recuperacion(enlace), "html", "utf-8"))
    _despachar_smtp(mensaje, destino)


@app.route("/api/contratistas/recuperar-password", methods=["POST"])
def api_recuperar_password():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    if not _correo_valido(email):
        return jsonify({"error": "Enter a valid email.", "code": "email"}), 400
    try:
        enlace = _enlace_recuperacion_admin(email)
    except ErrorAuth as err:
        return _fallo_recuperacion(err, err.code, err.status)
    except Exception as err:
        return _fallo_recuperacion(err, "auth_error", 502)
    if not enlace:
        return jsonify({"ok": True})
    try:
        _enviar_correo_recuperacion(email, enlace)
    except Exception as err:
        return _fallo_recuperacion(err, "smtp", 502)
    print("--> correo recuperacion enviado")
    return jsonify({"ok": True})


@app.route("/api/contratistas/restablecer", methods=["POST"])
def api_restablecer_contrasena():
    body = request.get_json(silent=True) or {}
    try:
        row = restablecer_contrasena(
            body.get("password") or body.get("contrasena") or "",
            access_token=body.get("access_token") or "",
            code=body.get("code") or "",
            code_verifier=body.get("code_verifier") or "",
        )
    except ErrorAuth as err:
        return _respuesta_auth(err)
    except Exception as err:
        return _respuesta_login_silenciosa(err, "restablecer")
    if not row or not row.get("token"):
        return jsonify({"ok": True, "sesion": False})
    resp = jsonify(_sesion_desde_fila(row))
    return _adjuntar_sesion_contratista(resp, row.get("token"), persistir=True)


def _cookie_oauth(respuesta, verifier):
    seguro = _peticion_https()
    kwargs = {
        "max_age": 600,
        "httponly": True,
        "samesite": "Lax",
        "secure": seguro,
        "path": "/",
    }
    respuesta.set_cookie(COOKIE_OAUTH, verifier, **kwargs)
    if _es_dominio_app():
        respuesta.set_cookie(COOKIE_OAUTH, verifier, domain="." + DOMINIO_APP, **kwargs)
    return respuesta


def _olvidar_cookie_oauth(respuesta):
    seguro = _peticion_https()
    respuesta.delete_cookie(COOKIE_OAUTH, path="/", samesite="Lax", secure=seguro)
    if _es_dominio_app():
        respuesta.delete_cookie(
            COOKIE_OAUTH,
            domain="." + DOMINIO_APP,
            path="/",
            samesite="Lax",
            secure=seguro,
        )
    return respuesta


def _oauth_entrar(row):
    resp = redirect("/estimado")
    resp.headers["Cache-Control"] = "no-store"
    _olvidar_cookie_oauth(resp)
    return _adjuntar_sesion_contratista(resp, row.get("token"), persistir=True)


def _log_oauth_fallo(etapa, err=None, extra=""):
    tipo = type(err).__name__ if err is not None else ""
    codigo = str(getattr(err, "code", "") or "")
    status = getattr(err, "status", "")
    detalle = _detalle_error(err) if err is not None else ""
    print(
        f"--> oauth fallo etapa={etapa} tipo={tipo} status={status} "
        f"code={codigo} {extra} {detalle}".strip()
    )


def _etapa_oauth(err):
    codigo = str(getattr(err, "code", "") or "")
    texto = str(err or "").lower()
    if codigo in ("oauth_email",):
        return "email"
    if codigo in ("sin_cuenta", "already_registered") or "contratista" in texto:
        return "contratista"
    if codigo in ("enlace", "oauth") or "flow" in texto or "verifier" in texto or "authapierror" in type(err).__name__.lower():
        return "intercambio"
    if codigo == "config":
        return "config"
    return codigo or "desconocido"


def _oauth_login_fallo(etapa="desconocido", err=None):
    _log_oauth_fallo(etapa, err)
    _alerta_desde_error(err, "oauth:" + str(etapa or "desconocido"))
    destino = _oauth_destino_silencioso()
    print(f"--> oauth redirige {destino} desde etapa={etapa}")
    resp = redirect(destino)
    resp.headers["Cache-Control"] = "no-store"
    return _olvidar_cookie_oauth(resp)


def _pagina_puente(destino, verifier=""):
    guardar = ""
    if verifier:
        guardar = f"try{{sessionStorage.setItem('oauth_pkce_verifier',{json.dumps(verifier)});}}catch(e){{}}"
    pagina = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{_FAVICON_HEAD}"
        f"<script>{guardar}location.replace({json.dumps(destino)});</script>"
        "</head><body></body></html>"
    )
    resp = Response(pagina, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _pagina_callback_hash():
    pagina = """<!doctype html><html><head><meta charset="utf-8">""" + _FAVICON_HEAD + """<title>Signing in</title></head><body>
<form id="completar" method="post" action="/auth/callback">
<input type="hidden" name="code" id="code">
<input type="hidden" name="access_token" id="access_token">
</form>
<script>
var q=new URLSearchParams(location.search||"");
var h=new URLSearchParams(String(location.hash||"").replace(/^#/,""));
var code=q.get("code")||h.get("code")||"";
var token=h.get("access_token")||"";
var err=q.get("error_description")||h.get("error_description")||q.get("error")||h.get("error")||"";
var verifier="";
try{verifier=sessionStorage.getItem("oauth_pkce_verifier")||"";}catch(e){}
if((err&&!code&&!token)||(!code&&!token)){location.replace("/login?aviso=reintento");}
else{
  var extra=document.createElement("input");
  extra.type="hidden";
  extra.name="code_verifier";
  extra.value=verifier;
  var form=document.getElementById("completar");
  form.appendChild(extra);
  document.getElementById("code").value=code;
  document.getElementById("access_token").value=token;
  form.submit();
}
</script></body></html>"""
    resp = Response(pagina, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _url_oauth_google_cliente(redirect_to):
    cliente = _cliente_auth_oauth()
    if cliente is None:
        return "", ""
    respuesta = cliente.auth.sign_in_with_oauth({
        "provider": "google",
        "options": {
            "redirect_to": redirect_to,
            "scopes": "openid email profile",
        },
    })
    url = str(getattr(respuesta, "url", "") or "")
    auth = getattr(cliente, "auth", None)
    storage = getattr(auth, "_storage", None)
    clave = str(getattr(auth, "_storage_key", "") or "")
    verifier = ""
    if storage is not None and clave:
        verifier = str(storage.get_item(f"{clave}-code-verifier") or "")
    return url, verifier


@app.route("/api/contratistas/oauth/google")
def api_oauth_google():
    try:
        url, _key = _supabase_creds()
        if not url:
            _alerta_critica("oauth_inicio", "Falta SUPABASE_URL", variable="SUPABASE_URL", conexion="supabase")
            return redirect(_oauth_destino_silencioso())
        authorize, verifier = _url_oauth_google_cliente(_redirect_oauth())
        if not authorize or not verifier or "code_challenge=" not in authorize:
            print(f"--> oauth google inicio incompleto destino={bool(authorize)} verifier={bool(verifier)}")
            _alerta_critica(
                "oauth_inicio",
                "Google OAuth no devolvio URL o verifier",
                variable="SUPABASE_URL" if not url else "",
                conexion="supabase_auth",
            )
            return redirect(_oauth_destino_silencioso())
        resp = _pagina_puente(authorize, verifier)
        return _cookie_oauth(resp, verifier)
    except Exception as err:
        print(f"--> oauth google inicio: {err}")
        _alerta_desde_error(err, "oauth_inicio")
        return redirect(_oauth_destino_silencioso())


def _pagina_completar_pkce(code):
    import html as html_lib
    pagina = f"""<!doctype html><html><head><meta charset="utf-8">{_FAVICON_HEAD}<title>Signing in</title></head><body>
<form id="completar" method="post" action="/auth/callback">
<input type="hidden" name="code" value="{html_lib.escape(code, quote=True)}">
<input type="hidden" name="code_verifier" id="code_verifier">
</form>
<script>
var verifier="";
try{{verifier=sessionStorage.getItem("oauth_pkce_verifier")||"";}}catch(e){{}}
document.getElementById("code_verifier").value=verifier;
document.getElementById("completar").submit();
</script></body></html>"""
    resp = Response(pagina, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _verifiers_oauth_solicitud():
    vistos = []
    for valor in (request.values.get("code_verifier"), request.cookies.get(COOKIE_OAUTH)):
        texto = str(valor or "").strip()
        if texto and texto not in vistos:
            vistos.append(texto)
    return vistos


def _error_intercambio_pkce(err):
    codigo = str(getattr(err, "code", "") or "")
    if codigo in ("oauth_email", "sin_cuenta", "already_registered", "config"):
        return False
    texto = str(err or "").lower()
    return (
        codigo in ("oauth", "enlace")
        or "invalid flow" in texto
        or "flow state" in texto
        or "code verifier" in texto
        or "authapierror" in texto
    )


def _sesion_oauth_con_verifiers(code, verifiers):
    ultimo = None
    for verifier in verifiers:
        try:
            return sesion_oauth_google(code, verifier)
        except Exception as err:
            ultimo = err
            print(f"--> oauth verifier intento {_detalle_error(err)}")
            if not _error_intercambio_pkce(err):
                raise
    if ultimo:
        raise ultimo
    raise ErrorAuth("enlace", "The Google sign-in expired. Try again.", 400)


@app.route("/auth/callback", methods=["GET", "POST"])
def auth_callback():
    import traceback
    code = str(request.values.get("code") or request.values.get("auth_code") or "").strip()
    access = str(request.values.get("access_token") or "").strip()
    cookie_verifier = bool(str(request.cookies.get(COOKIE_OAUTH) or "").strip())
    form_verifier = bool(str(request.values.get("code_verifier") or "").strip())
    print(
        f"--> oauth callback method={request.method} code={bool(code)} "
        f"access={bool(access)} cookie={cookie_verifier} form_verifier={form_verifier}"
    )
    if request.method == "GET" and (request.args.get("error") or request.args.get("error_description")) and not code and not access:
        print(request.args.get("error"))
        print(request.args.get("error_description"))
        return _oauth_login_fallo("supabase_redirect")
    if request.method == "GET" and not code and not access:
        return _pagina_callback_hash()
    if request.method == "GET" and code and not access:
        return _pagina_completar_pkce(code)
    try:
        if access:
            row = sesion_desde_token_google(access)
        else:
            verifiers = _verifiers_oauth_solicitud()
            row = _sesion_oauth_con_verifiers(code, verifiers)
    except Exception as e:
        print(f"--> oauth callback post {_detalle_error(e)}")
        traceback.print_exc()
        return _oauth_login_fallo(_etapa_oauth(e), e)
    try:
        return _oauth_entrar(row)
    except Exception as e:
        print(f"--> oauth cookie/sesion: {_detalle_error(e)}")
        traceback.print_exc()
        try:
            return _oauth_entrar(row)
        except Exception as err2:
            print(f"--> oauth cookie reintento: {err2}")
            return _oauth_login_fallo("cookie", err2)


@app.route("/api/contratistas/contrasena", methods=["POST"])
def api_crear_contrasena_contratista():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = body.get("password") or body.get("contrasena") or ""
    if not _correo_valido(email):
        return jsonify({"error": "Enter a valid email.", "code": "email"}), 400
    try:
        row = crear_contrasena_contratista(email, password)
    except ErrorAuth as err:
        return _respuesta_auth(err)
    except Exception as err:
        return _respuesta_login_silenciosa(err, "contrasena")
    if not row or not row.get("token"):
        return jsonify({"error": "No contractor account uses this email. Create one first.", "code": "sin_cuenta"}), 404
    try:
        payload = _sesion_desde_fila(row)
    except Exception as err:
        print(f"--> contrasena sesion perfil/contadores: {err}")
        payload = {
            "id": row.get("id"),
            "email": row.get("email"),
            "nombre_empresa": row.get("nombre_empresa") or "",
            "token": row.get("token"),
            "user_id": row.get("user_id") or "",
        }
    resp = jsonify(payload)
    return _adjuntar_sesion_contratista(
        resp,
        row.get("token"),
        persistir=_recordarme_activo(body.get("recordarme"), True),
    )


@app.route("/api/contratistas/sesion", methods=["DELETE"])
def api_cerrar_sesion_contratista():
    resp = jsonify({"ok": True})
    return _limpiar_cookies_contratista(resp)


@app.route("/api/contratistas/yo", methods=["GET"])
def api_contratista_actual():
    contratista, error = _exigir_contratista()
    if error:
        return error
    try:
        payload = _sesion_desde_fila(contratista)
    except Exception as err:
        print(f"--> /yo perfil/contadores: {err}")
        payload = {
            "id": contratista.get("id"),
            "email": contratista.get("email"),
            "nombre_empresa": contratista.get("nombre_empresa"),
            "user_id": contratista.get("user_id") or "",
            "token": contratista.get("token"),
        }
    resp = jsonify(payload)
    return _adjuntar_sesion_contratista(resp, contratista.get("token"))


@app.route("/api/historial", methods=["GET", "POST"])
def api_historial():
    sb = _supabase_client()
    if sb is None:
        return jsonify({"error": "Supabase no configurado", "items": []}), 503
    contratista, error = _exigir_contratista()
    if error:
        return error
    if request.method == "POST":
        if contratista.get("trial_expirada"):
            return jsonify({
                "error": "Your 14-day free trial has expired. Please add a payment method to keep using QuantixBuilds.",
                "code": "trial_expired",
            }), 402
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
    ia = _crear_cliente_gemini(key)
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


_CACHE_TRADUCCION_IDIOMA = {}
_CACHE_TRAD_LOCK = threading.Lock()
_CACHE_TRAD_RUTA = Path(tempfile.gettempdir()) / "zipnova-traducciones.json"


def _cargar_cache_traduccion():
    if _CACHE_TRADUCCION_IDIOMA:
        return
    try:
        if _CACHE_TRAD_RUTA.is_file() and _CACHE_TRAD_RUTA.stat().st_size <= 2_000_000:
            data = json.loads(_CACHE_TRAD_RUTA.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _CACHE_TRADUCCION_IDIOMA.update({k: v for k, v in data.items() if isinstance(v, str) and v})
    except Exception as err:
        print(f"--> cache traduccion: {err}")


def _guardar_cache_traduccion():
    try:
        _CACHE_TRAD_RUTA.write_text(json.dumps(_CACHE_TRADUCCION_IDIOMA, ensure_ascii=False), encoding="utf-8")
    except Exception as err:
        print(f"--> cache traduccion no se guardo: {err}")


def _traducir_lote_openai(textos, destino, timeout=25.0, modelos=None):
    key = _openai_api_key()
    if not key:
        raise RuntimeError("Falta OPENAI_API_KEY")
    nombre = "español" if destino == "es" else "inglés"
    origen = "inglés" if destino == "es" else "español"
    user = (
        f"Traduce cada partida de construcción del {origen} al {nombre}. "
        "Conserva números, medidas, unidades y códigos. No mezcles idiomas. "
        'Devuelve solo JSON {"translations":["..."]} con la misma cantidad y el mismo orden.\n\n'
        + json.dumps(list(textos), ensure_ascii=False)
    )
    verify = (os.getenv("GEMINI_SSL_VERIFY") or "1").strip().lower() not in ("0", "false", "no", "off")
    ultimo = None
    import httpx
    segundos = min(float(timeout or 8), 8.0)
    limite_http = httpx.Timeout(segundos, connect=3.0)

    with httpx.Client(verify=verify, timeout=limite_http) as http:
        for modelo in (modelos or ("gpt-4o-mini", "gpt-4.1-mini", "gpt-4o")):
            try:
                resp = http.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": modelo,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": "Eres traductor de partidas de construcción. Respondes solo JSON."},
                            {"role": "user", "content": user},
                        ],
                    },
                )
                if resp.status_code >= 400:
                    ultimo = RuntimeError(f"OpenAI {modelo} HTTP {resp.status_code}: {resp.text[:240]}")
                    print(f"--> {ultimo}")
                    continue
                content = (((resp.json() or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                parsed = extraer_json(content) or {}
                trads = parsed.get("translations") or parsed.get("traducciones") or []
                if isinstance(trads, list) and trads:
                    print(f"--> traduccion OpenAI {modelo} a {destino}: {len(trads)}")
                    return [str(t or "").strip() for t in trads]
            except Exception as err:
                ultimo = err
                print(f"--> traducir OpenAI {modelo}: {err}")
    raise ultimo or RuntimeError("OpenAI no tradujo")


def _traducciones_openai(textos, destino):
    _cargar_cache_traduccion()
    salida = [""] * len(textos)
    pendientes = []
    indices = []
    with _CACHE_TRAD_LOCK:
        for i, texto in enumerate(textos):
            guardado = _CACHE_TRADUCCION_IDIOMA.get(f"{destino}:{texto}")
            if guardado:
                salida[i] = guardado
            else:
                pendientes.append(texto)
                indices.append(i)
    if not pendientes:
        return salida
    try:
        trads = _traducir_lote_openai(pendientes, destino)
    except Exception as err:
        print(f"--> traducir catalogo: {err}")
        return salida
    cambio = False
    with _CACHE_TRAD_LOCK:
        for pos, idx in enumerate(indices):
            trad = trads[pos] if pos < len(trads) else ""
            if not trad:
                continue
            salida[idx] = trad
            _CACHE_TRADUCCION_IDIOMA[f"{destino}:{textos[idx]}"] = trad
            cambio = True
    if cambio:
        _guardar_cache_traduccion()
    return salida


_TRAD_EN_COLA = set()


def _aplicar_texto_idioma(item, original, texto, idioma):
    if idioma == "es":
        item["descripcion_es"] = texto
        item["descripcion_en"] = item.get("descripcion_en") or (original if original != texto else "")
    else:
        item["descripcion_en"] = texto
        item["descripcion_es"] = item.get("descripcion_es") or (original if original != texto else "")
    item["descripcion"] = texto
    item["desc"] = texto
    item["idioma"] = idioma
    item["descripciones"] = {"es": item.get("descripcion_es") or "", "en": item.get("descripcion_en") or ""}


def _encolar_traduccion(textos, destino):
    """OpenAI traduce después de responder. La búsqueda no se queda esperando."""
    nuevos = []
    with _CACHE_TRAD_LOCK:
        _cargar_cache_traduccion()
        for texto in textos:
            clave = f"{destino}:{texto}"
            if not texto or clave in _CACHE_TRADUCCION_IDIOMA or clave in _TRAD_EN_COLA:
                continue
            _TRAD_EN_COLA.add(clave)
            nuevos.append(texto)
    if not nuevos:
        return

    def _trabajo():
        try:
            _traducciones_openai(nuevos, destino)
        finally:
            with _CACHE_TRAD_LOCK:
                for texto in nuevos:
                    _TRAD_EN_COLA.discard(f"{destino}:{texto}")

    threading.Thread(target=_trabajo, daemon=True, name="traduccion-catalogo").start()


def _traduccion_guardada(texto, idioma):
    with _CACHE_TRAD_LOCK:
        return _CACHE_TRADUCCION_IDIOMA.get(f"{idioma}:{texto}") or ""


def _items_en_idioma_busqueda(items, idioma):
    """Inglés solo en inglés. Español solo en español. No espera a traducir si ya hay coincidencias."""
    if idioma not in ("es", "en"):
        return items, False
    _cargar_cache_traduccion()
    finales = []
    pendientes = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        original = str(item.get("descripcion") or item.get("desc") or "").strip()
        if not original:
            continue
        propio = _idioma_de_texto(original)
        if propio == "es":
            item["descripcion_es"] = original
        elif propio == "en":
            item["descripcion_en"] = original
        if propio == idioma:
            _aplicar_texto_idioma(item, original, original, idioma)
            finales.append(item)
            continue
        trad = _traduccion_guardada(original, idioma)
        if trad and _idioma_de_texto(trad) == idioma:
            _aplicar_texto_idioma(item, original, trad, idioma)
            finales.append(item)
        else:
            pendientes.append(original)
    if pendientes and not finales:
        _encolar_traduccion(pendientes[:24], idioma)
        return list(items or [])[:50], True
    if pendientes:
        _encolar_traduccion(pendientes, idioma)
    return finales[:50], False


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
            res = sb.table(TABLA_CATALOGO).select(COLUMNAS_CATALOGO).in_("codigo", lote).execute()
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


UMBRAL_VECTOR_BARRA = 0.40
PESO_VECTOR_BARRA = 400
ESPERA_VECTOR_BARRA = 0
_CACHE_VECTOR_BARRA = OrderedDict()
_VECTOR_BARRA_EN_CURSO = {}
_CACHE_VECTOR_BARRA_LOCK = threading.Lock()
_FILAS_POR_ID = {"id": None, "mapa": {}}


def _clave_vector_barra(q):
    clave = re.sub(r"\s+", " ", _sin_acentos(q)).strip()
    if len(clave) < 4 or not _openai_api_key():
        return ""
    return clave


def _iniciar_vector_barra(q):
    """Lanza la búsqueda semántica sin bloquear; si ya hay una igual en curso, la reutiliza."""
    clave = _clave_vector_barra(q)
    if not clave:
        return "", None
    with _CACHE_VECTOR_BARRA_LOCK:
        if clave in _CACHE_VECTOR_BARRA:
            _CACHE_VECTOR_BARRA.move_to_end(clave)
            return clave, None
        evento = _VECTOR_BARRA_EN_CURSO.get(clave)
        if evento is not None:
            return clave, evento
        evento = threading.Event()
        _VECTOR_BARRA_EN_CURSO[clave] = evento

    def correr():
        try:
            _vector_busqueda_barra(q, clave)
        except Exception as err:
            print(f"--> barra vector: {type(err).__name__}: {str(err)[:160]}")
        finally:
            with _CACHE_VECTOR_BARRA_LOCK:
                _VECTOR_BARRA_EN_CURSO.pop(clave, None)
            evento.set()

    threading.Thread(target=correr, daemon=True, name="barra-vector").start()
    return clave, evento


def _resultado_vector_barra(clave, evento, espera):
    """(similitudes, pendiente). Si la base tarda, se responde sin esperar y el frontend vuelve a pedir."""
    if not clave:
        return {}, False
    if evento is not None:
        evento.wait(espera)
    with _CACHE_VECTOR_BARRA_LOCK:
        if clave in _CACHE_VECTOR_BARRA:
            return dict(_CACHE_VECTOR_BARRA[clave]), False
        return {}, clave in _VECTOR_BARRA_EN_CURSO


def _vector_busqueda_barra(q, clave, limite=40):
    """{id: similitud} de match_catalog_items (o match_items) para la frase escrita en la barra."""
    client = _supabase_client()
    if client is None:
        return {}
    vector = _embedding_texto(q)
    if not vector:
        return {}
    payload = {"query_embedding": vector, "match_threshold": UMBRAL_VECTOR_BARRA, "match_count": limite}
    datos = None
    try:
        datos = client.rpc("match_catalog_items", payload).execute().data or []
    except Exception as err:
        print(f"--> barra vector match_catalog_items: {type(err).__name__}: {str(err)[:160]}")
        try:
            datos = client.rpc("match_items", payload).execute().data or []
        except Exception as err2:
            print(f"--> barra vector match_items: {type(err2).__name__}: {str(err2)[:160]}")
            return {}
    if not datos:
        return {}
    resultado = {
        row["id"]: float(row.get("similarity") or 0)
        for row in datos
        if isinstance(row, dict) and row.get("id") is not None
    }
    with _CACHE_VECTOR_BARRA_LOCK:
        _CACHE_VECTOR_BARRA[clave] = resultado
        while len(_CACHE_VECTOR_BARRA) > 300:
            _CACHE_VECTOR_BARRA.popitem(last=False)
    return dict(resultado)


def _filas_catalogo_por_id(filas):
    if _FILAS_POR_ID.get("id") != id(filas):
        _FILAS_POR_ID["mapa"] = {row.get("id"): row for row in filas if isinstance(row, dict)}
        _FILAS_POR_ID["id"] = id(filas)
    return _FILAS_POR_ID["mapa"]


@app.route("/api/search-catalog", methods=["GET"])
@app.route("/api/buscar-catalogo", methods=["GET"])
def buscar_catalogo():
    q = request.args.get("q", "").strip()
    zip_q = request.args.get("zip") or request.args.get("zip_code") or ""
    from oficios import detectar_oficio, fila_es_oficio, normalizar_oficio
    q_norm = _corregir_typos_catalogo(q)
    oficio_ui = normalizar_oficio(request.args.get("oficio") or request.args.get("project_type") or "")
    medida = _consulta_medida_plano(q) or _consulta_medida_plano(q_norm)
    oficio = detectar_oficio(medida) if medida else (detectar_oficio(q_norm) or oficio_ui)
    idioma = _idioma_de_busqueda(q) or _idioma_de_busqueda(q_norm) or str(request.args.get("idioma") or request.args.get("lang") or "es").strip().lower()
    if idioma not in IDIOMAS_CATALOGO:
        idioma = "es"
    zona = _zona_mercado_por_zip(zip_q)
    campo = str(request.args.get("campo") or request.args.get("field") or "").strip().lower()
    print(f"--> Término buscado: '{q}' nucleo='{_nucleo_busqueda_catalogo(q_norm)}' oficio={oficio} ui={oficio_ui} ZIP={zona.get('zip')} {zona.get('ciudad')} {zona.get('estado')} factor={zona.get('factor')}")
    try:
        client = _supabase_client()
        q_match = _nucleo_busqueda_catalogo(q_norm) or q_norm
        unidad_q = (request.args.get("unidad") or request.args.get("unit") or "").upper()
        filas = []
        ultimo_err = None
        from oficios import catalogo_en_carga, todas_filas_catalogo
        if not (todas_filas_catalogo() or []):
            cargando = bool(catalogo_en_carga())
            payload = {
                "success": True,
                "items": [],
                "count": 0,
                "idioma": idioma,
                "traduciendo": False,
                "cargando": cargando,
                "listo": not cargando,
            }
            resp = jsonify(payload)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
        clave_vector, evento_vector = "", None
        if q_match and not medida and campo not in ("code", "codigo") and not _parece_codigo_catalogo(q_match):
            clave_vector, evento_vector = _iniciar_vector_barra(q_norm)
        puntaje_lexico = {}
        if q_match:
            try:
                for score, row in _filas_catalogo_locales(q_match, unidad_q, con_puntaje=True):
                    puntaje_lexico[id(row)] = score
                    filas.append(row)
            except Exception as err:
                ultimo_err = err
                print(f"--> buscar-catalogo local: {err}")
        elif client is None:
            raise RuntimeError("Cliente supabase es None (revisa SUPABASE_URL y SUPABASE_KEY en .env)")
        else:
            filas = _filas_catalogo_locales(q or "", unidad_q)
        similitud, semantica_pendiente = _resultado_vector_barra(clave_vector, evento_vector, ESPERA_VECTOR_BARRA)
        if similitud:
            por_id = _filas_catalogo_por_id(todas_filas_catalogo() or [])
            vistos = {row.get("id") for row in filas}
            for item_id in sorted(similitud, key=similitud.get, reverse=True):
                row = por_id.get(item_id)
                if row is not None and item_id not in vistos:
                    filas.append(row)
                    vistos.add(item_id)
        if ultimo_err and not filas:
            raise ultimo_err
        demolicion = _es_consulta_demolicion(q_norm)
        tokens = _tokens_busqueda_catalogo(_consulta_medida_plano(q_match) or q_match)
        busca_baldosa = any(
            "baldosa" in grupo or "tile" in grupo
            for token in re.findall(r"[a-zA-ZáéíóúñÁÉÍÓÚÑ]{3,}", str(q_match or q).lower())
            for grupo in _familias_de_token(token)
        )
        if oficio and not busca_baldosa:
            filtradas = [
                row for row in filas
                if fila_es_oficio(row, oficio)
                or similitud.get(row.get("id"), 0) >= 0.55
                or (demolicion and _puntaje_demolicion(row) >= 25)
            ]
            if len(filtradas) >= 8:
                filas = filtradas
        if busca_baldosa:
            def _es_baldosa_obra(row):
                cat = str(row.get("categoria") or "").upper()
                codigo = str(row.get("codigo") or "").upper()
                blob = f"{codigo} {row.get('descripcion') or ''}".lower()
                if re.search(r"roof|teja|shingle|\brfg", blob) and cat != "TILE":
                    return False
                if re.search(r"acustic|acoustic|cielo", blob) and not re.search(r"acustic|acoustic|cielo", str(q_match or q), re.I):
                    return False
                if cat == "TILE" or codigo.startswith("TIL"):
                    return True
                return bool(re.search(r"baldosa|ceramic|ceramica|porcelanato|azulejo|backsplash|travertine|shower", blob))
            preferidas = [row for row in filas if _es_baldosa_obra(row)]
            if len(preferidas) >= 8:
                filas = preferidas
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
        umbral = 4 if campo in ("code", "codigo") else 6

        def _puntaje_hibrido(row):
            """Texto (sinónimos y cobertura de palabras) + semántica (embeddings)."""
            if id(row) in puntaje_lexico:
                score = puntaje_lexico[id(row)]
            else:
                score = _puntaje_coincidencia_catalogo(row, q_match, tokens, unidad_q)
                if demolicion:
                    score += _puntaje_demolicion(row)
            sim = similitud.get(row.get("id"), 0)
            if sim:
                score += PESO_VECTOR_BARRA * max(0.0, sim - UMBRAL_VECTOR_BARRA)
            return score

        def _orden_catalogo(row):
            score = _puntaje_hibrido(row) + _puntaje_catalogo_zip(row, zona)
            precio = _num_catalogo(row, "precio_base", "precio_unitario", "precio", "price")
            if consulta_ridge:
                return (score, -abs((precio or 0) - 13.5))
            return (score, precio)
        filas.sort(key=_orden_catalogo, reverse=True)
        razonables = [
            row for row in filas
            if _puntaje_hibrido(row) >= umbral or similitud.get(row.get("id"), 0) >= UMBRAL_VECTOR_BARRA
        ] or filas
        limite = 50
        candidatos = [item for item in (normalizar_item_catalogo(row, idioma) for row in razonables[:200]) if item]
        items = candidatos
        factor = float(zona.get("factor") or 1.0)
        for item in items:
            base = _num(item.get("precio_unitario"))
            labor = _num(item.get("precio_labor") or item.get("precio_mano_obra") or item.get("priceLabor"))
            material = _num(item.get("precio_material") or item.get("priceMaterial"))
            demo = _num(item.get("precio_remocion") or item.get("precio_demo") or item.get("priceDemo"))
            item["precio_base"] = round(base, 2)
            item["factor_zip"] = round(factor, 4)
            item["zip_code"] = zona.get("zip")
            item["ciudad"] = zona.get("ciudad")
            item["estado"] = zona.get("estado")
            item["precio_zona"] = round(base * factor, 2)
            item["precio_labor"] = round(labor, 2)
            item["precio_mano_obra"] = round(labor, 2)
            item["precio_material"] = round(material, 2)
            item["precio_demo"] = round(demo, 2)
            item["precio_remocion"] = round(demo, 2)
            item["priceLabor"] = round(labor, 2)
            item["priceMaterial"] = round(material, 2)
            item["priceDemo"] = round(demo, 2)
            item["ajuste_zip"] = True
            item["fuente"] = item.get("fuente") or "supabase"
        items = _overlay_precios_proveedores(items, zona.get("zip") or zip_q)
        items, traduciendo = _items_en_idioma_busqueda(items, idioma)
        print(
            f"--> catálogo supabase q='{q_match}' idioma={idioma} filas={len(filas)} items={len(items)} "
            f"vector={len(similitud)} pendiente={semantica_pendiente} demolicion={demolicion}"
        )
        if not items and str(request.args.get("registrar_faltante") or "") in ("1", "true", "si", "yes"):
            registrar_item_faltante(q, oficio, zona.get("zip") or zip_q)
        payload = {
            "success": True,
            "items": items,
            "count": len(items),
            "idioma": idioma,
            "traduciendo": bool(traduciendo),
            "cargando": False,
            "listo": True,
            "semantica_pendiente": bool(semantica_pendiente),
        }
        resp = jsonify(payload)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    except Exception as e:
        print(f"ERROR SUPABASE DETALLADO: {e}")
        resp = jsonify({"success": False, "error": str(e), "items": []})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 500


PROVEEDOR_HOME_DEPOT = "Home Depot"


def _tienda_precios(client=None):
    from proveedores_precios import TiendaPrecios
    return TiendaPrecios(client if client is not None else _supabase_client())


def _aplicar_cache_proveedor(row, cache, zip5, fresco=True):
    from proveedores_precios import fila_publica_cache, imagen_fresca, precio_fresco
    if not isinstance(row, dict) or not isinstance(cache, dict):
        return False
    publico = fila_publica_cache(cache) or {}
    try:
        material = float(publico.get("precio_material") or cache.get("precio_normalizado") or 0)
    except (TypeError, ValueError):
        material = 0.0
    if material <= 0:
        return False
    row["precio_material"] = round(material, 2)
    labor = row.get("_labor_protegida")
    if labor is None:
        labor = _num_catalogo(row, "precio_mano_obra", "precio_labor", "labor")
    demo = _num_catalogo(row, "precio_remocion", "precio_demo")
    if labor is not None:
        row["precio_mano_obra"] = labor
        row["_paquete_respuesta"] = round(float(labor or 0) + material + float(demo or 0), 2)
    if row.get("_base_protegida") is not None:
        row["precio_base"] = row["_base_protegida"]
    foto = publico.get("image_url") or cache.get("image_url") or ""
    if foto:
        row["image_url"] = foto
    enlace = publico.get("product_url") or cache.get("product_url") or ""
    if enlace:
        row["product_url"] = enlace
    row["proveedor"] = publico.get("proveedor") or cache.get("proveedor") or PROVEEDOR_HOME_DEPOT
    row["store_id"] = publico.get("store_id") or cache.get("tienda_id") or ""
    row["store_name"] = publico.get("store_name") or cache.get("tienda_nombre") or ""
    row["last_updated"] = cache.get("last_updated") or publico.get("last_updated") or ""
    row["image_updated"] = cache.get("image_updated") or ""
    row["precio_original"] = cache.get("precio_original")
    row["unidad_original"] = cache.get("unidad_original") or ""
    row["presentacion"] = cache.get("presentacion") or ""
    row["moneda"] = cache.get("moneda") or "USD"
    row["producto_id"] = cache.get("producto_id") or ""
    row["zip_consulta"] = zip5
    row["_precio_proveedor"] = True
    row["_precio_fresco"] = bool(fresco and precio_fresco(cache))
    row["_imagen_fresca"] = imagen_fresca(cache)
    row["match_status"] = "ok"
    row["candidatos"] = []
    return True


def _aplicar_publico_proveedor(row, publico, zip5):
    from proveedores_precios import ahora_utc
    if not isinstance(row, dict) or not isinstance(publico, dict):
        return False
    try:
        material = float(publico.get("precio_material") or publico.get("precio_normalizado") or 0)
    except (TypeError, ValueError):
        material = 0.0
    if material <= 0:
        return False
    row["precio_material"] = round(material, 2)
    labor = row.get("_labor_protegida")
    demo = _num_catalogo(row, "precio_remocion", "precio_demo")
    if labor is not None:
        row["precio_mano_obra"] = labor
        row["_paquete_respuesta"] = round(float(labor or 0) + material + float(demo or 0), 2)
    if row.get("_base_protegida") is not None:
        row["precio_base"] = row["_base_protegida"]
    if publico.get("image_url") or publico.get("imageUrl"):
        row["image_url"] = publico.get("image_url") or publico.get("imageUrl")
    if publico.get("product_url") or publico.get("productUrl"):
        row["product_url"] = publico.get("product_url") or publico.get("productUrl")
    row["proveedor"] = publico.get("proveedor") or PROVEEDOR_HOME_DEPOT
    row["store_id"] = publico.get("store_id") or publico.get("tienda_id") or ""
    row["store_name"] = publico.get("store_name") or publico.get("tienda_nombre") or ""
    row["last_updated"] = publico.get("last_updated") or ahora_utc().isoformat()
    row["precio_original"] = publico.get("precio_original")
    row["unidad_original"] = publico.get("unidad_original") or ""
    row["presentacion"] = publico.get("presentacion") or ""
    row["moneda"] = publico.get("moneda") or "USD"
    row["producto_id"] = publico.get("producto_id") or publico.get("sku") or ""
    row["zip_consulta"] = zip5
    row["_precio_proveedor"] = True
    row["_precio_fresco"] = True
    row["match_status"] = "ok"
    row["candidatos"] = []
    return True


def _guardar_precio_proveedor(tienda, row, publico, zip5, consulta=""):
    from proveedores_precios import fila_desde_publico
    if tienda is None:
        return False
    payload = fila_desde_publico(publico, zip5, consulta=consulta or (row or {}).get("consulta_material") or "")
    if not payload:
        return False
    return bool(tienda.guardar(payload))


def _reusar_imagen_proveedor(tienda, row, proveedor=PROVEEDOR_HOME_DEPOT):
    if tienda is None or not isinstance(row, dict):
        return
    if row.get("image_url") and row.get("_imagen_fresca"):
        return
    producto_id = str(row.get("producto_id") or "")
    if not producto_id:
        return
    foto = tienda.imagen_producto(proveedor, producto_id)
    if foto:
        row["image_url"] = foto
        row["_imagen_fresca"] = True


def _resolver_precios_proveedor(client, filas, zip5, proveedor=PROVEEDOR_HOME_DEPOT):
    """Revisa Supabase, unifica el scrape entre instancias y no escribe el precio en catalogo_items."""
    from home_depot_apify import (
        analizar_coincidencias,
        extraer_consulta_material,
        precio_desde_home_depot,
        producto_publico,
        scrapear_home_depot,
        token_apify,
        unidad_catalogo,
    )
    from proveedores_precios import clave_consulta, consultar_con_lock, es_fila_cache, scrape_de, zip_obra

    zip5 = zip_obra(zip5)
    tienda = _tienda_precios(client)
    info = {"store_id": "", "store_name": "", "zip": zip5}
    if not filas:
        return {"scraped": 0, "fuente": "", "error": "", "tienda": info, "stale": 0}
    for row in filas:
        consulta = extraer_consulta_material(row)
        row["consulta_material"] = consulta
        row["zip_consulta"] = zip5
        labor_guardada = _num_catalogo(row, "precio_mano_obra", "precio_labor", "labor")
        row["_labor_protegida"] = labor_guardada
        row["_base_protegida"] = _num_catalogo(row, "precio_base")
        if zip5:
            hits = tienda.fresco(proveedor, zip5, consulta=consulta)
            if hits:
                _aplicar_cache_proveedor(row, hits[0], zip5, fresco=True)
                _reusar_imagen_proveedor(tienda, row, proveedor)
                if hits[0].get("tienda_id"):
                    info["store_id"] = str(hits[0].get("tienda_id") or "")
                    info["store_name"] = str(hits[0].get("tienda_nombre") or "")
    if not zip5:
        return {
            "scraped": 0,
            "fuente": "",
            "error": "Enter the contractor job ZIP to price the nearest store.",
            "tienda": info,
            "stale": 0,
        }
    pendientes = [row for row in filas if not row.get("_precio_fresco")]
    if not pendientes:
        return {"scraped": 0, "fuente": "cache", "error": "", "tienda": info, "stale": 0}

    def _aplicar_ultimo(row):
        last = tienda.ultimo(proveedor, zip5, consulta=row.get("consulta_material") or "")
        if last:
            _aplicar_cache_proveedor(row, last, zip5, fresco=False)
            _reusar_imagen_proveedor(tienda, row, proveedor)
            return True
        return False

    scrape_fn = scrape_de(proveedor) or scrapear_home_depot
    if proveedor == PROVEEDOR_HOME_DEPOT and not token_apify():
        for row in pendientes:
            _aplicar_ultimo(row)
        return {
            "scraped": 0,
            "fuente": "stale",
            "error": "Missing APIFY_API_TOKEN",
            "tienda": info,
            "stale": len(pendientes),
        }
    scrapeados_holder = {"n": 0}

    def _persistir(productos, zip_ok, _terminos):
        usados = set()
        for row in pendientes:
            disponibles = [item for item in (productos or []) if id(item) not in usados and not es_fila_cache(item)]
            info_match = analizar_coincidencias(row, disponibles or [item for item in (productos or []) if not es_fila_cache(item)])
            row["match_status"] = info_match.get("status") or "none"
            row["candidatos"] = info_match.get("candidatos") or []
            match = info_match.get("match")
            if not match:
                continue
            usados.add(id(match))
            unidad = unidad_catalogo(row)
            precio_hd = precio_desde_home_depot(match, unidad)
            publico = producto_publico(match, unidad) or {}
            if not (precio_hd and precio_hd > 0):
                if publico.get("image_url"):
                    _actualizar_sku_home_depot(client, row, publico, None)
                continue
            publico["precio_material"] = round(float(precio_hd), 2)
            if _guardar_precio_proveedor(tienda, row, publico, zip_ok, row.get("consulta_material") or ""):
                scrapeados_holder["n"] += 1
            _aplicar_publico_proveedor(row, publico, zip_ok)
            _actualizar_sku_home_depot(client, row, publico, None)
            info["store_id"] = str(publico.get("store_id") or info.get("store_id") or "")
            info["store_name"] = str(publico.get("store_name") or info.get("store_name") or "")

    productos, fuente, hizo_scrape = consultar_con_lock(
        tienda,
        proveedor,
        zip5,
        [row.get("consulta_material") for row in pendientes],
        scrape_fn,
        persistir_fn=_persistir,
    )
    if str(fuente).startswith("error:"):
        for row in pendientes:
            _aplicar_ultimo(row)
        return {
            "scraped": 0,
            "fuente": "error",
            "error": str(fuente)[6:],
            "tienda": info,
            "stale": len(pendientes),
        }
    if fuente in ("cache", "shared") or (productos and es_fila_cache(productos[0])):
        por_consulta = {}
        for item in productos or []:
            if not es_fila_cache(item):
                continue
            por_consulta.setdefault(clave_consulta(item.get("consulta_clave")), []).append(item)
        for row in pendientes:
            if row.get("_precio_fresco"):
                continue
            hits = por_consulta.get(clave_consulta(row.get("consulta_material")))
            if hits:
                _aplicar_cache_proveedor(row, hits[0], zip5, fresco=True)
                _reusar_imagen_proveedor(tienda, row, proveedor)
                if hits[0].get("tienda_id"):
                    info["store_id"] = str(hits[0].get("tienda_id") or "")
                    info["store_name"] = str(hits[0].get("tienda_nombre") or "")
            else:
                _aplicar_ultimo(row)
        return {
            "scraped": 0,
            "fuente": fuente or "cache",
            "error": tienda.last_error or "",
            "tienda": info,
            "stale": 0,
        }
    for row in pendientes:
        if row.get("_precio_proveedor"):
            continue
        _aplicar_ultimo(row)
    return {
        "scraped": scrapeados_holder["n"],
        "fuente": "scraped" if hizo_scrape else fuente,
        "error": tienda.last_error or "",
        "tienda": info,
        "stale": len([row for row in filas if row.get("_precio_proveedor") and not row.get("_precio_fresco")]),
    }


def _overlay_precios_proveedores(items, zip5, proveedor=PROVEEDOR_HOME_DEPOT):
    from home_depot_apify import extraer_consulta_material
    from proveedores_precios import zip_obra
    zip5 = zip_obra(zip5)
    if not zip5 or not items:
        return items
    tienda = _tienda_precios()
    if not tienda.disponible:
        return items
    for item in items[:12]:
        if not isinstance(item, dict):
            continue
        consulta = extraer_consulta_material(item)
        if not consulta:
            continue
        hits = tienda.fresco(proveedor, zip5, consulta=consulta)
        if not hits:
            continue
        cache = hits[0]
        try:
            material = float(cache.get("precio_normalizado") or 0)
        except (TypeError, ValueError):
            material = 0.0
        if material <= 0:
            continue
        labor = _num(item.get("precio_labor") or item.get("precio_mano_obra"))
        demo = _num(item.get("precio_remocion") or item.get("precio_demo"))
        item["precio_material"] = round(material, 2)
        item["priceMaterial"] = round(material, 2)
        item["precio_unitario"] = round(labor + material + demo, 2)
        item["precio_base"] = item["precio_unitario"]
        item["precio_zona"] = item["precio_unitario"]
        item["ajuste_zip"] = False
        item["proveedor"] = cache.get("proveedor") or proveedor
        item["last_updated"] = cache.get("last_updated") or ""
        item["store_id"] = cache.get("tienda_id") or ""
        item["store_name"] = cache.get("tienda_nombre") or ""
        item["zip_code"] = zip5
        if cache.get("image_url"):
            item["image_url"] = cache.get("image_url")
            item["imageUrl"] = cache.get("image_url")
        if cache.get("product_url"):
            item["product_url"] = cache.get("product_url")
            item["productUrl"] = cache.get("product_url")
        item["fuente"] = "supabase_proveedor"
    return items


def _es_sku_material_principal(row):
    codigo = str((row or {}).get("codigo") or (row or {}).get("code") or "").upper()
    blob = f"{codigo} {(row or {}).get('descripcion') or (row or {}).get('description') or ''}".lower()
    if codigo.startswith("ZN-DEM") or re.search(r"\bdemo\b|tear.?off|demolici|retiro de", blob):
        return False
    return True


def _skus_principales_home_depot(termino, limite=4):
    from oficios import esperar_catalogo
    from home_depot_apify import LIMITE_SKUS, consulta_home_depot

    esperar_catalogo(12)
    tope = int(limite or LIMITE_SKUS)
    filas = _filas_catalogo_locales(termino, limite=40) or []
    principales = [row for row in filas if _es_sku_material_principal(row) and consulta_home_depot(row)]
    if len(principales) < tope:
        vistos = {id(row) for row in principales}
        for row in filas:
            if id(row) in vistos:
                continue
            if consulta_home_depot(row):
                principales.append(row)
            if len(principales) >= tope:
                break
    return principales[:tope]


def _asegurar_columnas_home_depot(client):
    if client is None:
        return False
    try:
        client.table(TABLA_CATALOGO).select("id,image_url,last_updated,proveedor,product_url").limit(1).execute()
        return True
    except Exception as err:
        _ajustar_columnas_catalogo(err)
        print("-->", _SQL_HOME_DEPOT)
        return False


def _actualizar_sku_home_depot(client, row, producto, precio_hd, persistir_consulta=True):
    from home_depot_apify import _ahora_utc, sanitizar_url, url_imagen_home_depot, url_producto_home_depot

    if client is None or not isinstance(row, dict):
        return False
    ident = row.get("id")
    if ident is None:
        return False
    foto = ""
    enlace = ""
    if isinstance(producto, dict):
        foto = sanitizar_url(producto.get("image_url") or producto.get("imageUrl"), imagen=True) or url_imagen_home_depot(producto)
        enlace = sanitizar_url(producto.get("product_url") or producto.get("productUrl") or producto.get("url"), imagen=False) or url_producto_home_depot(producto)
    payload = {}
    if persistir_consulta:
        payload["proveedor"] = "Home Depot" if (foto or enlace) else (row.get("proveedor") or "Catalog")
    if foto:
        payload["image_url"] = foto
    if enlace:
        payload["product_url"] = enlace
    try:
        client.table(TABLA_CATALOGO).update(payload).eq("id", ident).execute()
        return True
    except Exception as err:
        if _ajustar_columnas_catalogo(err) and "product_url" in payload:
            payload.pop("product_url", None)
            try:
                client.table(TABLA_CATALOGO).update(payload).eq("id", ident).execute()
                return True
            except Exception as err2:
                print(f"--> home-depot update sku: {err2}")
                return False
        print(f"--> home-depot update sku: {err}")
        return False


def _enriquecer_item_home_depot(item, row, zona, zip5, factor):
    if not item:
        return None
    base = _num(item.get("precio_unitario"))
    labor = _num(item.get("precio_labor") or item.get("precio_mano_obra"))
    material = _num(item.get("precio_material"))
    demo = _num(item.get("precio_remocion") or item.get("precio_demo"))
    paquete = _num(row.get("_paquete_respuesta")) if row.get("_precio_proveedor") else 0
    if paquete > 0:
        base = paquete
        factor = 1.0
    item["precio_base"] = round(base, 2)
    item["precio_base_catalogo"] = _num(row.get("_base_protegida") or item.get("precio_unitario"))
    item["factor_zip"] = round(factor, 4)
    item["zip_code"] = zip5 or (zona or {}).get("zip") or ""
    item["ciudad"] = (zona or {}).get("ciudad")
    item["estado"] = (zona or {}).get("estado")
    item["precio_zona"] = round(base * factor, 2)
    item["precio_labor"] = round(labor, 2)
    item["precio_mano_obra"] = round(labor, 2)
    item["precio_material"] = round(material, 2)
    item["precio_demo"] = round(demo, 2)
    item["precio_remocion"] = round(demo, 2)
    item["priceLabor"] = round(labor, 2)
    item["priceMaterial"] = round(material, 2)
    item["priceDemo"] = round(demo, 2)
    item["ajuste_zip"] = False if row.get("_precio_proveedor") else True
    item["image_url"] = _url_imagen_catalogo(row) or item.get("image_url") or ""
    item["imageUrl"] = item["image_url"]
    item["product_url"] = item.get("product_url") or row.get("product_url") or ""
    item["productUrl"] = item["product_url"]
    item["last_updated"] = row.get("last_updated") or item.get("last_updated") or ""
    item["proveedor"] = item.get("proveedor") or row.get("proveedor") or ""
    item["candidatos"] = row.get("candidatos") or []
    item["match_status"] = row.get("match_status") or ("ok" if item["image_url"] else "none")
    item["consulta_material"] = row.get("consulta_material") or ""
    if row.get("_labor_protegida") is not None:
        labor = round(float(row["_labor_protegida"] or 0), 2)
        item["precio_labor"] = labor
        item["precio_mano_obra"] = labor
        item["priceLabor"] = labor
    item["store_id"] = row.get("store_id") or ""
    item["store_name"] = row.get("store_name") or ""
    item["precio_original"] = row.get("precio_original")
    item["unidad_original"] = row.get("unidad_original") or ""
    item["presentacion"] = row.get("presentacion") or ""
    item["moneda"] = row.get("moneda") or "USD"
    item["producto_id"] = row.get("producto_id") or ""
    item["stale"] = bool(row.get("_precio_proveedor") and not row.get("_precio_fresco"))
    return item


@app.route("/api/catalogo/home-depot", methods=["GET", "POST"])
def catalogo_home_depot():
    from home_depot_apify import extraer_consulta_material, token_apify, zip_consulta as _zip_consulta_hd
    from proveedores_precios import ahora_utc

    payload = request.get_json(silent=True) or {}
    q = str(request.args.get("q") or payload.get("q") or payload.get("termino") or "").strip()
    zip_q = request.args.get("zip") or request.args.get("zip_code") or payload.get("zip") or payload.get("zip_code") or ""
    zip5 = _zip_consulta_hd(zip_q)
    zona = _zona_mercado_por_zip(zip5) if zip5 else {}
    idioma = _idioma_de_busqueda(q) or "en"
    if idioma not in IDIOMAS_CATALOGO:
        idioma = "en"
    factor = float(zona.get("factor") or 1.0)
    client = _supabase_client()
    _asegurar_columnas_home_depot(client)
    elegir = payload.get("elegir") if isinstance(payload.get("elegir"), dict) else None
    if elegir:
        codigo = str(elegir.get("codigo") or elegir.get("code") or "").strip()
        filas_elegir = _skus_principales_home_depot(codigo or q, 8) if (codigo or q) else []
        row = next((r for r in filas_elegir if str(r.get("codigo") or "").strip() == codigo), filas_elegir[0] if filas_elegir else None)
        if row is None:
            resp = jsonify({"success": False, "error": "Catalog item not found.", "items": []})
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp, 404
        row["consulta_material"] = extraer_consulta_material(row)
        row["_labor_protegida"] = _num_catalogo(row, "precio_mano_obra", "precio_labor", "labor")
        row["_base_protegida"] = _num_catalogo(row, "precio_base")
        precio_hd = _num(elegir.get("precio_material") or elegir.get("priceMaterial"))
        if precio_hd > 0 and zip5:
            publico = {
                "proveedor": elegir.get("proveedor") or PROVEEDOR_HOME_DEPOT,
                "producto_id": elegir.get("producto_id") or elegir.get("sku") or "",
                "nombre": elegir.get("nombre") or "",
                "image_url": elegir.get("image_url") or elegir.get("imageUrl") or "",
                "product_url": elegir.get("product_url") or elegir.get("productUrl") or "",
                "precio_material": precio_hd,
                "precio_original": elegir.get("precio_original"),
                "unidad_original": elegir.get("unidad_original") or "",
                "unidad": elegir.get("unidad") or row.get("unidad") or "",
                "presentacion": elegir.get("presentacion") or "",
                "moneda": elegir.get("moneda") or "USD",
                "store_id": elegir.get("store_id") or elegir.get("tienda_id") or "",
                "store_name": elegir.get("store_name") or elegir.get("tienda_nombre") or "",
                "cobertura_sf": elegir.get("cobertura_sf"),
                "last_updated": ahora_utc().isoformat(),
            }
            _guardar_precio_proveedor(_tienda_precios(client), row, publico, zip5, row.get("consulta_material") or "")
            _aplicar_publico_proveedor(row, publico, zip5)
        elif precio_hd > 0:
            row["precio_material"] = round(precio_hd, 2)
        _actualizar_sku_home_depot(client, row, elegir, None)
        item = _enriquecer_item_home_depot(normalizar_item_catalogo(row, idioma), row, zona, zip5, factor)
        resp = jsonify({"success": True, "items": [item] if item else [], "count": 1 if item else 0, "elegido": True, "zip": zip5})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    if len(q) < 3:
        resp = jsonify({"success": False, "error": "Enter at least 3 characters.", "items": []})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 400
    filas = _skus_principales_home_depot(q, 4)
    if not filas:
        resp = jsonify({"success": True, "items": [], "count": 0, "scraped": False, "zip": zip5})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    resolucion = _resolver_precios_proveedor(client, filas, zip5, PROVEEDOR_HOME_DEPOT)
    scrapeados = int(resolucion.get("scraped") or 0)
    error_apify = resolucion.get("error") or ""
    tienda = resolucion.get("tienda") or {"store_id": "", "store_name": "", "zip": zip5}
    items = []
    for row in filas:
        if row.get("_labor_protegida") is not None:
            row["precio_mano_obra"] = row["_labor_protegida"]
        if row.get("_base_protegida") is not None and not row.get("_paquete_respuesta"):
            row["precio_base"] = row["_base_protegida"]
        item = _enriquecer_item_home_depot(normalizar_item_catalogo(row, idioma), row, zona, zip5, factor)
        if not item:
            continue
        if scrapeados:
            item["fuente"] = "home_depot"
        elif row.get("_precio_proveedor"):
            item["fuente"] = "supabase_proveedor"
        else:
            item["fuente"] = item.get("fuente") or "supabase"
        items.append(item)
    print(
        f"--> home-depot q='{q}' zip={zip5} skus={len(filas)} fuente={resolucion.get('fuente')} "
        f"scraped={scrapeados} token={'si' if token_apify() else 'no'}"
    )
    payload_out = {
        "success": True,
        "items": items[:4],
        "count": len(items[:4]),
        "zip": zip5,
        "scraped": scrapeados > 0,
        "refrescados": scrapeados,
        "stale": int(resolucion.get("stale") or 0),
        "fuente": resolucion.get("fuente") or "",
        "store_id": tienda.get("store_id") or "",
        "store_name": tienda.get("store_name") or "",
        "last_updated": items[0].get("last_updated") if items else "",
    }
    if error_apify:
        payload_out["error"] = error_apify
    resp = jsonify(payload_out)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


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


ACCIONES_PRECIO = {
    "replace": "remover e instalar",
    "replace_material": "remover, instalar más materiales",
    "install_material": "instalar más materiales",
    "install": "solo instalar",
    "labor": "solo mano de obra",
    "demo": "solo demolición",
    "material": "solo material",
}


def _factor_ubicacion(ubicacion):
    texto = str(ubicacion or "").strip()
    digitos = re.sub(r"\D", "", texto)[:5]
    if len(digitos) == 5:
        zona = _zona_mercado_por_zip(digitos)
        return float(zona.get("factor") or 1.0), zona
    estado = texto.upper()[:2] if re.fullmatch(r"[A-Za-z]{2}", texto) else ""
    if estado:
        meta = _factor_desde_supabase("", estado)
        if meta is None:
            return FACTOR_RESPALDO, {"estado": estado, "fuente": "fallback", "validado": False, "clase": "respaldo_sin_dato"}
        return float(meta["factor"]), {
            "estado": estado,
            "fuente": "factores_regionales_usa",
            "mercado": meta.get("nombre_zona"),
            "codigo_zona": meta.get("codigo_zona"),
            "validado": False,
            "clase": "indice_interno_supabase",
        }
    return FACTOR_RESPALDO, {"fuente": "fallback", "validado": False, "clase": "respaldo_sin_dato"}


_RE_FILA_DEMOLICION = re.compile(r"demolici|retiro|remov|tear|arranc|desmont|raspado", re.I)


CONTEXTO_ESTIMADOR_PRECIOS = (
    "Lee la ACCIÓN y la DESCRIPCIÓN. Si necesitas el costo del material, DEBES usar la herramienta de búsqueda de Google "
    "para buscar el precio de venta actual de ese producto en tiendas de EE. UU. (ej. Home Depot, Lowe's). "
    "Devuelve tu cálculo basándote ESTRICTAMENTE en los resultados de búsqueda web en tiempo real. "
    "No recicles un precio de demolición, instalación u otra acción anterior: cada acción se cotiza por separado.\n"
    "Eres un estimador experto de construcción y ajustador de seguros en Estados Unidos. "
    "Tu motor de cálculo opera EXCLUSIVAMENTE utilizando la lógica de las Bases de Datos de Precios por Partida "
    "(Line-Item Pricing Databases), los Manuales de Costos de Construcción (Construction Cost Data / RSMeans) y "
    "los Estándares de Tabuladores de Reclamaciones de Seguros (Insurance Claim Pricing Standards) tipo Xactimate.\n"
    "METODOLOGÍA XACTIMATE — 3 REGLAS DE COTIZACIÓN (inviolables):\n"
    "REGLA A — Desglose de componentes base. Para CUALQUIER ítem, asigna internamente un valor realista "
    "a 3 variables independientes, con tarifas actuales del mercado de EE. UU. (ej. Georgia/Atlanta):\n"
    "  1) Costo de Material, CON factor de desperdicio del oficio.\n"
    "  2) Costo de Instalación (mano de obra).\n"
    "  3) Costo de Remoción (demolición y desecho).\n"
    "  El precio unitario final ES la suma matemática exacta de los componentes que exija la acción seleccionada. Nada más.\n"
    "REGLA B — Líneas suplementarias (maximizador de estimación). Actúa como contratista experto. "
    "Si la descripción principal requiere pasos previos obligatorios que el cliente suele olvidar, inclúyelos en el análisis. "
    "Ejemplos: piso LVT -> preparación/nivelación del subsuelo (FCW PREP) e instalación de molduras (FNC BSHOE); "
    "pintura interior -> enmascarillado y protección de pisos/muebles. "
    "Como cada fila solo admite un precio unitario, prorratea un porcentaje pequeño de esos costos DENTRO "
    "del precio unitario de instalación (no como líneas aparte) para que el contratista no pierda dinero. "
    "Si la acción no incluye instalación, no prorratees nada.\n"
    "REGLA C — Restricción de integridad. Un precio de 'Material + Instalación' JAMÁS puede ser igual o menor "
    "al costo del 'Material solo'. Valida tu propia suma ANTES de devolver el JSON; si falla, corrige y vuelve a sumar.\n"
    "Para cada solicitud de precio, aplica además estas reglas inviolables de mercado:\n"
    "1. Divide estrictamente el valor entre Labor y Material según los estándares de RSMeans.\n"
    "2. Si la acción es 'Material only', el precio debe reflejar solo el costo del insumo, sin mano de obra. NUNCA es 0.\n"
    "3. Si la acción es 'Demolition only' o 'Remove', el precio debe cubrir solo la labor de desmantelamiento y bote. NUNCA es 0.\n"
    "4. 'Install plus materials' SIEMPRE debe ser matemáticamente mayor que 'Install only'.\n"
    "5. 'Remove, install plus materials' debe ser la suma coherente de las partes individuales.\n"
    "6. Ajusta los promedios nacionales al mercado específico del código postal/estado proporcionado en la solicitud.\n"
    "REGLA CRÍTICA: Si la acción solicitada es 'Material only' o 'Demolition only', IGNORA palabras como "
    "'Instalación' o 'Colocación' en la descripción del ítem. Enfócate en el material base (ej. Pisos Vinilo, Loseta) "
    "y devuelve el costo de mercado exclusivo para esa acción. JAMÁS devuelvas 0. Si dudas, devuelve el promedio nacional del material crudo.\n\n"
    "FORMATO DE LA SOLICITUD. Cada solicitud trae dos campos que debes correlacionar:\n"
    "  - MATERIAL Y TRABAJO A EVALUAR: el material/partida (la descripción del trabajo).\n"
    "  - ALCANCE DE COBRO EXACTO: la acción que define QUÉ se cobra.\n"
    "RAZONAMIENTO CRUZADO (obligatorio y en este orden):\n"
    "  a) Lee PRIMERO el MATERIAL Y TRABAJO A EVALUAR para identificar el [MATERIAL] y sus costos de mercado "
    "(material, mano de obra de instalación y demolición). Las palabras de la descripción como 'Instalación', 'Colocación' "
    "o 'Remover' NO definen el cobro: solo sirven para identificar el material.\n"
    "  b) Lee DESPUÉS el ALCANCE DE COBRO EXACTO para decidir qué componentes sumar. El alcance SIEMPRE manda sobre la descripción.\n"
    "Si el ALCANCE DE COBRO dice 'Remove, install plus materials', estás OBLIGADO a sumar: "
    "(Costo de demolición de [MATERIAL]) + (Costo del [MATERIAL]) + (Costo de mano de obra para instalar [MATERIAL]). "
    "Nunca devuelvas un precio inferior a la suma de estos componentes.\n"
    "Ejemplo: MATERIAL Y TRABAJO A EVALUAR = 'Instalación de piso vinílico de lujo (LVT)' y ALCANCE DE COBRO EXACTO = "
    "'Remove, install plus materials' -> demolición de LVT + material LVT + instalación de LVT. "
    "Devolver solo la instalación (≈ $2.36/SF) es un ERROR GRAVE.\n\n"
    "PROCESO OBLIGATORIO (Chain of Thought). Razona paso a paso ANTES de dar cualquier precio:\n"
    "PASO 1 - Análisis estricto de la acción (ALCANCE DE COBRO EXACTO). Lee la acción palabra por palabra y decide qué componentes incluye:\n"
    "  - Si contiene 'Remove', 'Remover', 'Demolition' o 'Demolición': incluye el costo de REMOCIÓN "
    "(desmantelamiento, retiro, carga y desecho/bote del material existente).\n"
    "  - Si contiene 'materials', 'materiales' o 'Material': incluye el costo de MATERIAL "
    "(precio promedio en tienda del producto en EE. UU., tipo Home Depot / Lowe's / distribuidor, por unidad solicitada, "
    "incluyendo desperdicio normal y consumibles).\n"
    "  - Si contiene 'install', 'instalar' o 'Labor': incluye el costo de INSTALACIÓN (tarifa de mano de obra).\n"
    "  - Los componentes que la acción NO menciona quedan fuera de la suma, pero igual debes declarar su valor base (PASO 3).\n"
    "PASO 2 - Contexto geográfico. Usa exclusivamente tarifas actuales del mercado de construcción de Estados Unidos "
    "(ej. Georgia/Atlanta), basadas en estándares RSMeans y en la metodología de estructuración por partidas de Xactimate, "
    "y ajústalas al código postal/estado indicado.\n"
    "PASO 3 - Desglose de componentes base (anclaje de precios). ANTES de calcular cualquier total, asigna internamente "
    "un valor realista por unidad a 3 variables independientes, sin importar la acción solicitada:\n"
    "  - material: costo de material en tienda CON factor de desperdicio estándar del oficio "
    "(ej. pisos 10%, azulejo 10-15%, drywall 10-12%, teja 10-15%, pintura según rendimiento por galón).\n"
    "  - instalacion: costo de mano de obra de instalación (incluye el prorrateo de líneas suplementarias del PASO 4).\n"
    "  - remocion: costo de demolición, retiro y desecho/bote del material existente.\n"
    "  Los tres valores base son SIEMPRE mayores que 0 y se reportan SIEMPRE, sea cual sea la acción. "
    "Son los mismos números para cualquier acción sobre el mismo ítem, unidad y ubicación: la acción solo decide cuáles se suman.\n"
    "PASO 4 - Líneas suplementarias (maximizador de estimación). Actúa como un contratista experto: identifica los pasos "
    "previos o complementarios obligatorios que el cliente suele olvidar para este ítem. Ejemplos:\n"
    "  - Piso LVT/LVP/laminado: preparación y nivelación del subsuelo (FCW PREP) e instalación de molduras/zapatillas (FNC BSHOE).\n"
    "  - Pintura interior: enmascarillado y protección de pisos y muebles.\n"
    "  - Azulejo: membrana/impermeabilización y preparación de superficie. Drywall: cinta, compuesto y lijado.\n"
    "  Como cada fila solo admite un precio unitario, NO devuelvas líneas aparte: prorratea un porcentaje pequeño y realista "
    "del costo de esas líneas (normalmente 5-15% de la instalación) DENTRO del valor base de instalacion, para que el contratista "
    "no pierda dinero. Si la acción no incluye instalación, no prorrateas nada. "
    "Lista las líneas consideradas en lineas_suplementarias.\n"
    "PASO 5 - Lógica aditiva estricta. precio_unitario = suma exacta de los valores base de los componentes incluidos en el PASO 1:\n"
    "  - Material only = material. Install only = instalacion. Demolition only = remocion.\n"
    "  - Install plus materials = material + instalacion.\n"
    "  - Remove and install = remocion + instalacion.\n"
    "  - Remove, install plus materials = remocion + material + instalacion.\n"
    "  Ese total es el 'Unit Price'. No apliques descuentos, redondeos de paquete ni ajustes adicionales a la suma.\n"
    "PASO 6 - Restricción de integridad. Valida tu propia suma antes de devolver el JSON:\n"
    "  - 'Material + Instalación' (Install plus materials) JAMÁS puede ser igual o menor que 'Material solo'; "
    "debe ser exactamente material + instalacion, con instalacion > 0.\n"
    "  - Ninguna acción combinada puede costar igual o menos que cualquiera de sus componentes por separado.\n"
    "  - precio_unitario debe coincidir al centavo con la suma de los componentes incluidos.\n"
    "  - Si la acción combina varios componentes, precio_unitario NUNCA puede ser igual a uno solo de ellos. "
    "Ejemplo: 'Remove, install plus materials' de piso LVT = remoción + material LVT + instalación; "
    "devolver solo la mano de obra (≈ $2/SF) es un ERROR. Si 'Material only' de un LVT comercial es $2.40/SF, "
    "'Install plus materials' tiene que ser $2.40 + instalación, nunca $1.85.\n"
    "  - Si alguna verificación falla, corrige los valores base y vuelve a sumar.\n"
    "Responde SIEMPRE con un único JSON válido, sin texto fuera del JSON, con este formato exacto: "
    '{"analisis": "razonamiento breve de los pasos 1 a 6", "componentes_incluidos": ["remocion", "material", "instalacion"], '
    '"lineas_suplementarias": [{"codigo": "FCW PREP", "descripcion": "texto", "prorrateo_instalacion": numero_decimal}], '
    '"remocion": numero_decimal, "material": numero_decimal, "instalacion": numero_decimal, "precio_unitario": numero_decimal}. '
    "remocion, material e instalacion son los valores base del PASO 3 (los tres > 0); instalacion ya incluye el prorrateo."
)

COMPONENTES_POR_ACCION = {
    "replace": ("remocion", "instalacion"),
    "replace_material": ("remocion", "material", "instalacion"),
    "install_material": ("material", "instalacion"),
    "install": ("instalacion",),
    "labor": ("instalacion",),
    "demo": ("remocion",),
    "material": ("material",),
}

# Multiplicadores de precio_base (mano de obra de instalación) por especialidad.
# labor, material, remoción. RSMeans / Xactimate, mercado EE. UU.
DESGLOSE_POR_CATEGORIA = {
    "TECHO": (1.00, 1.45, 0.45),
    "ROOFING": (1.00, 1.45, 0.45),
    "TECHOS": (1.00, 1.45, 0.45),
    "PISO": (1.00, 1.20, 0.30),
    "PISOS": (1.00, 1.20, 0.30),
    "FLOORING": (1.00, 1.20, 0.30),
    "ALFOMBRA": (1.00, 1.10, 0.22),
    "FLR PREP": (1.00, 0.45, 0.20),
    "DRYWALL": (1.00, 0.55, 0.40),
    "PINTURA": (1.00, 0.35, 0.20),
    "PAINTING": (1.00, 0.35, 0.20),
    "AZULEJO": (1.00, 1.10, 0.35),
    "TILE": (1.00, 1.10, 0.35),
    "PLOMERIA": (1.00, 0.85, 0.30),
    "PLUMBING": (1.00, 0.85, 0.30),
    "ELECTRICIDAD": (1.00, 0.70, 0.25),
    "ELECTRICAL": (1.00, 0.70, 0.25),
    "HVAC": (1.00, 1.30, 0.35),
    "COCINA": (1.00, 1.40, 0.30),
    "KITCHEN": (1.00, 1.40, 0.30),
    "ENCIMERAS": (1.00, 1.60, 0.25),
    "BAÑO": (1.00, 1.25, 0.35),
    "BANO": (1.00, 1.25, 0.35),
    "BATHROOM": (1.00, 1.25, 0.35),
    "FRAMING": (1.00, 0.90, 0.40),
    "CARPINTERIA": (1.00, 0.80, 0.30),
    "CARPINTERÍA": (1.00, 0.80, 0.30),
    "MADERA": (1.00, 0.95, 0.30),
    "DECK": (1.00, 1.35, 0.40),
    "CANALES": (1.00, 0.95, 0.35),
    "GUTTERS": (1.00, 0.95, 0.35),
    "EXTERIOR": (1.00, 1.25, 0.40),
    "SIDING": (1.00, 1.25, 0.40),
    "VENTANAS": (1.00, 1.80, 0.25),
    "WINDOWS": (1.00, 1.80, 0.25),
    "CERCA": (1.00, 1.20, 0.35),
    "CERCAS": (1.00, 1.20, 0.35),
    "PAISAJISMO": (1.00, 1.10, 0.30),
    "GAS": (1.00, 0.75, 0.30),
    "CRAWL_SPACE": (1.00, 0.50, 0.45),
    "BODEGAS": (1.00, 0.70, 0.35),
    "DEMOLICION": (0.00, 0.00, 1.00),
    "DEMOLITION": (0.00, 0.00, 1.00),
    "COMERCIAL": (1.00, 0.95, 0.32),
    "RESIDENCIAL": (1.00, 0.90, 0.30),
    "GENERAL": (1.00, 0.90, 0.30),
}

_DESGLOSE_POR_TEXTO = (
    ("TECHO", r"techo|roof|shingle|teja|sbs|epdm|tpo"),
    ("SIDING", r"siding|hardie|revestimiento|soffit|fascia"),
    ("PISO", r"piso|floor|lvt|lvp|spc|laminat|hardwood|vinilo"),
    ("ALFOMBRA", r"alfombra|carpet"),
    ("DRYWALL", r"drywall|tablaroca|sheetrock|yeso"),
    ("PINTURA", r"pintura|paint|esmalte|primer"),
    ("AZULEJO", r"azulejo|tile|porcelanato|ceramica|cerámica"),
    ("PLOMERIA", r"plomer|plumb|pex|drenaje"),
    ("ELECTRICIDAD", r"electric|cableado|tomacorriente"),
    ("HVAC", r"hvac|climatiz|ducto"),
    ("COCINA", r"cocina|kitchen|gabinete|cabinet"),
    ("ENCIMERAS", r"encimera|countertop|granito|cuarzo"),
    ("BAÑO", r"baño|bano|bath|ducha|vanity|inodoro"),
    ("FRAMING", r"framing|enmarcado|cercha|vigueta"),
    ("CARPINTERIA", r"carpinter|moldura|puerta|trim"),
    ("DECK", r"deck|terraza|porche"),
    ("CANALES", r"canaleta|gutter|bajante"),
    ("VENTANAS", r"ventana|window"),
    ("CERCA", r"cerca|fence"),
    ("PAISAJISMO", r"paisaj|landscape"),
    ("GAS", r"\bgas\b|tuberia de gas"),
    ("DEMOLICION", r"demolici|demolition|escombro"),
)


def _clave_desglose_categoria(categoria, descripcion=""):
    cat = re.sub(r"[^A-ZÁÉÍÓÚÜÑ_ ]", "", str(categoria or "").upper()).strip()
    if cat in DESGLOSE_POR_CATEGORIA and cat not in ("GENERAL", "COMERCIAL", "RESIDENCIAL"):
        return cat
    blob = f"{cat} {descripcion or ''}"
    for clave, patron in _DESGLOSE_POR_TEXTO:
        if re.search(patron, blob, re.I):
            return clave
    return cat if cat in DESGLOSE_POR_CATEGORIA else "GENERAL"


def _desglose_desde_precio_base(precio_base, categoria="", descripcion=""):
    base = _num(precio_base)
    if base <= 0:
        return 0.0, 0.0, 0.0
    labor_f, material_f, demo_f = DESGLOSE_POR_CATEGORIA.get(
        _clave_desglose_categoria(categoria, descripcion),
        DESGLOSE_POR_CATEGORIA["GENERAL"],
    )
    return round(base * labor_f, 2), round(base * material_f, 2), round(base * demo_f, 2)


def _alcance_exclusivo_descripcion(descripcion):
    """Solo si el texto dice claramente 'only'. Si no, precio_base incluye las 3 columnas."""
    texto = _sin_acentos(descripcion)
    if re.search(r"material only|solo material|supply only|suministro de material", texto) and not re.search(r"instal", texto):
        return "material"
    if re.search(r"labor only|solo mano|solo instalacion|install(?:ation)? only|mano de obra solamente", texto):
        return "labor"
    if re.search(r"demolition only|solo demol|solo remoc|removal only|tear[\s-]*(out|off) only|demo only", texto):
        return "demo"
    return ""


def _alinear_componentes_a_paquete(labor, material, demo, paquete):
    suma = labor + material + demo
    if paquete <= 0 or suma <= 0:
        return round(labor, 2), round(material, 2), round(demo, 2)
    if abs(suma - paquete) <= 0.02:
        return round(labor, 2), round(material, 2), round(demo, 2)
    factor = paquete / suma
    labor = round(labor * factor, 2)
    material = round(material * factor, 2)
    demo = round(paquete - labor - material, 2)
    return labor, material, demo


def _componentes_desde_columnas(precio_base, precio_material, precio_mano_obra, precio_demo=0, categoria="", descripcion=""):
    """precio_base incluye material + mano de obra + remoción, salvo que la descripción diga 'only'."""
    base = _num(precio_base)
    material = _num(precio_material)
    labor = _num(precio_mano_obra)
    demo = _num(precio_demo)
    exclusivo = _alcance_exclusivo_descripcion(descripcion)
    if not exclusivo and base > 0:
        n_lab, n_mat, n_dem = int(labor > 0), int(material > 0), int(demo > 0)
        if n_lab + n_mat + n_dem == 1:
            if n_mat and abs(material - base) <= 0.05:
                exclusivo = "material"
            elif n_lab and abs(labor - base) <= 0.05:
                exclusivo = "labor"
            elif n_dem and abs(demo - base) <= 0.05:
                exclusivo = "demo"
    if exclusivo == "material":
        return 0.0, round(material or base, 2), 0.0
    if exclusivo == "labor":
        return round(labor or base, 2), 0.0, 0.0
    if exclusivo == "demo":
        return 0.0, 0.0, round(demo or base, 2)
    if base <= 0:
        return round(labor, 2), round(material, 2), round(demo, 2)
    conocidos = (labor if labor > 0 else 0.0) + (material if material > 0 else 0.0) + (demo if demo > 0 else 0.0)
    if labor > 0 and material > 0 and demo > 0:
        return _alinear_componentes_a_paquete(labor, material, demo, base)
    if conocidos > 0 and conocidos <= base + 0.05:
        falta = round(max(0.0, base - conocidos), 2)
        if labor <= 0:
            labor = falta
        elif material <= 0:
            material = falta
        elif demo <= 0:
            demo = falta
    if labor <= 0 or material <= 0 or demo <= 0:
        est_l, est_m, est_d = _desglose_desde_precio_base(base, categoria, descripcion)
        if labor <= 0:
            labor = est_l
        if material <= 0:
            material = est_m
        if demo <= 0:
            demo = round(max(0.01, base - labor - material), 2) if labor > 0 and material > 0 else est_d
    return _alinear_componentes_a_paquete(labor, material, demo, base)


def _precio_roles_por_accion(install, demo, material, labor, accion, paquete=0):
    def _pos(valor):
        numero = _num(valor)
        return numero if numero > 0 else 0.0

    def _tope(valor):
        numero = round(_pos(valor), 2)
        tope = _pos(paquete)
        if tope > 0 and numero > tope + 0.009:
            return round(tope, 2)
        return numero

    mano = _pos(labor) if _pos(labor) > 0 else 0.0
    paquete = _pos(paquete)
    if accion == "replace_material":
        return paquete or round(_pos(mano) + _pos(material) + _pos(demo), 2)
    if accion == "install_material":
        return _tope(_pos(material) + mano)
    if accion == "replace":
        return _tope(_pos(demo) + mano)
    if accion == "demo":
        return _tope(demo)
    if accion == "material":
        return _tope(material)
    if accion in ("install", "labor"):
        return _tope(mano)
    return _tope(mano)


def _openai_api_key():
    por_nombre = {}
    for clave, valor in os.environ.items():
        norm = clave.strip().lower().replace(" ", "")
        texto = str(valor or "").strip()
        if norm in {"openai_api_key", "openai_key", "opennai_api_key", "opennai_key"} and texto:
            por_nombre[norm] = texto
    return (
        por_nombre.get("openai_api_key")
        or por_nombre.get("openai_key")
        or por_nombre.get("opennai_api_key")
        or por_nombre.get("opennai_key")
        or ""
    )


def _openai_ssl_verify():
    return (os.getenv("GEMINI_SSL_VERIFY") or "1").strip().lower() not in ("0", "false", "no", "off")


_OPENAI_HTTP = None
_OPENAI_HTTP_CFG = None
_OPENAI_HTTP_LOCK = threading.Lock()
_OPENAI_HTTP_LISTO = False
_CACHE_EMBEDDING_TEXTO = OrderedDict()
_CACHE_EMBEDDING_LOCK = threading.Lock()
_CACHE_EMBEDDING_MAX = 400
_openai_async_local = threading.local()


def _cerrar_cliente_openai_locked():
    global _OPENAI_HTTP, _OPENAI_HTTP_CFG
    if _OPENAI_HTTP is not None:
        try:
            _OPENAI_HTTP.close()
        except Exception:
            pass
        _OPENAI_HTTP = None
        _OPENAI_HTTP_CFG = None


def _cerrar_cliente_openai():
    with _OPENAI_HTTP_LOCK:
        _cerrar_cliente_openai_locked()


atexit.register(_cerrar_cliente_openai)


def _cfg_cliente_openai():
    key = _openai_api_key()
    verify = _openai_ssl_verify()
    return (verify, len(key), key[:8] if key else ""), key, verify


def _limites_openai():
    import httpx

    return httpx.Limits(max_keepalive_connections=8, max_connections=16, keepalive_expiry=180.0)


def _timeout_openai():
    import httpx

    return httpx.Timeout(connect=4.0, read=20.0, write=10.0, pool=4.0)


def _cliente_openai_http():
    """Un httpx.Client por proceso: keep-alive HTTP/1.1, reutilizado entre hilos."""
    global _OPENAI_HTTP, _OPENAI_HTTP_CFG, _OPENAI_HTTP_LISTO
    import httpx

    cfg, key, verify = _cfg_cliente_openai()
    if not key:
        return None
    with _OPENAI_HTTP_LOCK:
        if _OPENAI_HTTP is not None and _OPENAI_HTTP_CFG == cfg:
            return _OPENAI_HTTP
        _cerrar_cliente_openai_locked()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
        _OPENAI_HTTP = httpx.Client(
            base_url="https://api.openai.com/v1",
            headers=headers,
            verify=verify,
            timeout=_timeout_openai(),
            limits=_limites_openai(),
            http2=False,
        )
        _OPENAI_HTTP_CFG = cfg
        _OPENAI_HTTP_LISTO = False
        return _OPENAI_HTTP


def _precalentar_openai():
    """Negocia TLS y deja el keep-alive listo antes de la primera búsqueda del contratista."""
    global _OPENAI_HTTP_LISTO
    http = _cliente_openai_http()
    if http is None:
        return
    try:
        resp = http.get("/models")
        _OPENAI_HTTP_LISTO = resp.status_code < 500
        print(f"--> OpenAI keep-alive listo HTTP {resp.status_code}")
    except Exception as err:
        print(f"--> OpenAI keep-alive: {type(err).__name__}: {str(err)[:160]}")


def _vectores_de_respuesta_openai(entradas, datos):
    por_indice = {int(item.get("index", i)): item.get("embedding") for i, item in enumerate(datos)}
    vectores = []
    for i, texto in enumerate(entradas):
        vector = por_indice.get(i)
        if not texto or not isinstance(vector, list) or len(vector) != EMBEDDING_DIM:
            print(
                f"--> OpenAI embeddings: vector invalido i={i} "
                f"tipo={type(vector).__name__} dim={len(vector) if isinstance(vector, list) else 0}"
            )
            vectores.append(None)
        else:
            vectores.append(vector)
    return vectores


def _texto_para_embedding(descripcion, unidad=""):
    texto = _descripcion_catalogo_normalizada(descripcion) or re.sub(r"\s+", " ", str(descripcion or "")).strip()
    unidad = str(unidad or "").strip().upper()
    return f"{texto} | {unidad}".strip(" |") if unidad else texto


def _embeddings_openai(textos):
    key = _openai_api_key()
    if not key:
        print("--> OpenAI embeddings: falta OPENAI_API_KEY en .env")
        raise RuntimeError("Falta OPENAI_API_KEY en .env")
    entradas = [re.sub(r"\s+", " ", str(t or "")).strip()[:4000] for t in textos]
    if not any(entradas):
        return [None] * len(textos)
    http = _cliente_openai_http()
    if http is None:
        raise RuntimeError("Falta OPENAI_API_KEY en .env")
    try:
        resp = http.post("/embeddings", json={"model": EMBEDDING_MODELO, "input": entradas})
    except Exception as err:
        print(f"--> OpenAI embeddings: fallo de conexion {type(err).__name__}: {err}")
        raise
    if resp.status_code >= 400:
        cuerpo = (resp.text or "")[:300]
        print(f"--> OpenAI embeddings: error {cuerpo}")
        raise RuntimeError(f"OpenAI embeddings HTTP {resp.status_code}: {cuerpo}")
    datos = (resp.json() or {}).get("data") or []
    vectores = _vectores_de_respuesta_openai(entradas, datos)
    if len(entradas) > 1:
        print(f"--> OpenAI embeddings: lote={len(entradas)} ok={sum(1 for v in vectores if v)}")
    return vectores


def _embedding_texto(descripcion, unidad=""):
    texto = _texto_para_embedding(descripcion, unidad)
    if len(texto) < 3:
        return None
    clave = texto.lower()
    with _CACHE_EMBEDDING_LOCK:
        if clave in _CACHE_EMBEDDING_TEXTO:
            _CACHE_EMBEDDING_TEXTO.move_to_end(clave)
            return _CACHE_EMBEDDING_TEXTO[clave]
    vectores = _embeddings_openai([texto])
    vector = vectores[0] if vectores else None
    if vector:
        with _CACHE_EMBEDDING_LOCK:
            _CACHE_EMBEDDING_TEXTO[clave] = vector
            while len(_CACHE_EMBEDDING_TEXTO) > _CACHE_EMBEDDING_MAX:
                _CACHE_EMBEDDING_TEXTO.popitem(last=False)
    return vector


EMBEDDINGS_LOTE = 80
EMBEDDINGS_INTERVALO = 600
EMBEDDINGS_HILOS = 4
_EMBEDDINGS_LOCK = threading.Lock()


def _filas_sin_embedding(sb, forzar=False, tamano=500):
    """Pagina por id con keyset: así no se salta filas mientras se van rellenando."""
    ultimo_id = 0
    while True:
        query = (
            sb.table(TABLA_CATALOGO)
            .select("id,descripcion,unidad")
            .gt("id", ultimo_id)
            .order("id")
            .limit(tamano)
        )
        if not forzar:
            query = query.is_("embedding", "null")
        lote = query.execute().data or []
        if not lote:
            return
        for row in lote:
            if str(row.get("descripcion") or "").strip():
                yield row
        ultimo_id = lote[-1]["id"]
        if len(lote) < tamano:
            return


def _guardar_embedding_catalogo(sb, item_id, vector):
    try:
        sb.rpc("set_item_embedding", {"item_id": int(item_id), "item_embedding": vector}).execute()
        return
    except Exception as err:
        print(f"--> set_item_embedding id={item_id}: {err}")
    time.sleep(2)
    sb.table(TABLA_CATALOGO).update({"embedding": vector}).eq("id", item_id).execute()


def _guardar_lote_embeddings(sb, pares):
    """Cada update tarda varios segundos en Supabase; con más de 4 hilos corta conexiones."""
    def guardar(par):
        row, vector = par
        if not vector:
            return False
        try:
            _guardar_embedding_catalogo(sb, row["id"], vector)
            return True
        except Exception as err:
            print(f"--> embeddings update id={row.get('id')}: {err}")
            return False

    with ThreadPoolExecutor(max_workers=EMBEDDINGS_HILOS) as ex:
        return sum(ex.map(guardar, pares))


def rellenar_embeddings_catalogo(forzar=False):
    """Genera el vector de cada catalogo_items sin embedding. Devuelve (guardados, pendientes)."""
    if not _EMBEDDINGS_LOCK.acquire(blocking=False):
        print("--> embeddings: ya hay un relleno en curso")
        return 0, 0
    try:
        if not _openai_api_key():
            print("--> embeddings: falta OPENAI_API_KEY")
            return 0, 0
        sb = _supabase_client()
        if sb is None:
            print("--> embeddings: sin cliente Supabase")
            return 0, 0
        pendientes = list(_filas_sin_embedding(sb, forzar))
        if not pendientes:
            return 0, 0
        print(f"--> embeddings: {len(pendientes)} ítems sin vector")
        ok = 0
        for i in range(0, len(pendientes), EMBEDDINGS_LOTE):
            lote = pendientes[i : i + EMBEDDINGS_LOTE]
            textos = [_texto_para_embedding(r.get("descripcion"), r.get("unidad")) for r in lote]
            try:
                vectores = _embeddings_openai(textos)
            except Exception as err:
                texto = str(err)
                print(f"--> embeddings lote {i}: {texto[:200]}")
                if any(s in texto for s in ("insufficient_quota", "credit_balance", "401", "Incorrect API key")):
                    break
                continue
            ok += _guardar_lote_embeddings(sb, list(zip(lote, vectores)))
            print(f"--> embeddings progreso {min(i + EMBEDDINGS_LOTE, len(pendientes))}/{len(pendientes)}")
        print(f"--> embeddings guardados: {ok}")
        return ok, len(pendientes)
    finally:
        _EMBEDDINGS_LOCK.release()


def _embeddings_automaticos():
    time.sleep(30)
    while True:
        try:
            rellenar_embeddings_catalogo()
        except Exception as err:
            print(f"--> embeddings automáticos: {err}")
        time.sleep(EMBEDDINGS_INTERVALO)


def _solo_activos_catalogo(client, filas):
    """La consulta vectorial solo conserva catalogo_items con activo = true."""
    ids = []
    for row in filas or []:
        if isinstance(row, dict) and row.get("id") is not None:
            ids.append(row["id"])
    if not ids:
        return []
    try:
        vivos = (
            client.table(TABLA_CATALOGO)
            .select("id")
            .in_("id", ids)
            .eq("activo", True)
            .execute()
            .data
            or []
        )
    except Exception as err:
        print(f"--> match_items filtro activo: {type(err).__name__}: {err}")
        return [row for row in filas if isinstance(row, dict) and row.get("activo") is not False]
    permitidos = {row.get("id") for row in vivos}
    return [row for row in filas if isinstance(row, dict) and row.get("id") in permitidos]


def _filas_catalogo_vector(descripcion, unidad=""):
    client = _supabase_client()
    if client is None:
        print("--> match_items: cliente supabase es None")
        return []
    try:
        vector = _embedding_texto(descripcion, unidad)
    except Exception as err:
        print(f"--> match_items: embedding fallo {type(err).__name__}: {err}")
        return []
    if not vector:
        print(f"--> match_items: sin vector para '{str(descripcion)[:60]}'")
        return []
    payload = {
        "query_embedding": vector,
        "match_threshold": UMBRAL_SIMILITUD_CATALOGO,
        "match_count": 40,
    }
    if unidad:
        payload["filter_unidad"] = str(unidad).upper()
    print(
        f"--> match_items: consulta '{str(descripcion)[:60]}' unidad={unidad or '-'} "
        f"dim={len(vector)} umbral={UMBRAL_SIMILITUD_CATALOGO} activo=true"
    )
    try:
        datos = client.rpc("match_items", payload).execute().data or []
    except Exception as err:
        print(f"--> match_items: error {type(err).__name__}: {err}")
        return []
    antes = len(datos) if isinstance(datos, list) else 0
    datos = _solo_activos_catalogo(client, datos)[:12]
    print(f"--> match_items activo=true {len(datos)}/{antes}")
    filas = []
    for row in datos:
        if not isinstance(row, dict):
            continue
        if row.get("activo") is False:
            continue
        fila = dict(row)
        fila["fuente"] = "vector"
        filas.append(fila)
    print(f"--> match_items '{descripcion[:60]}' {unidad}: {len(filas)} filas")
    return filas


def _precio_supabase_por_accion(descripcion, unidad, accion):
    client = _supabase_client()
    if client is None:
        raise RuntimeError("Cliente supabase es None (revisa SUPABASE_URL y SUPABASE_KEY en .env)")
    nucleo = _nucleo_busqueda_catalogo(_corregir_typos_catalogo(descripcion)) or descripcion
    consulta = _consulta_medida_plano(nucleo) or nucleo
    filas, vistos = [], set()
    try:
        for row in _filas_catalogo_locales(consulta, unidad, limite=40):
            clave = row.get("id") or row.get("codigo")
            if clave not in vistos:
                vistos.add(clave)
                filas.append(row)
    except Exception as err:
        print(f"--> precio-accion catalogo local: {err}")
    if filas:
        print(f"--> precio-accion catalogo local '{consulta[:60]}' {unidad}: {len(filas)} filas")
    else:
        try:
            for row in _filas_catalogo_vector(nucleo, unidad):
                clave = row.get("id") or row.get("codigo")
                if clave not in vistos:
                    vistos.add(clave)
                    filas.append(row)
            if accion in ("demo", "replace", "replace_material"):
                for row in _filas_catalogo_vector(f"{nucleo} retiro demolición", unidad):
                    clave = row.get("id") or row.get("codigo")
                    if clave not in vistos:
                        vistos.add(clave)
                        filas.append(row)
        except Exception as err:
            print(f"--> precio-accion vector: {err}")
    if not filas:
        termino = _limpiar_termino_catalogo(nucleo)
        categoria = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{4,}", nucleo.split(" - ", 1)[0])
        palabras = list(dict.fromkeys(categoria + sorted(
            re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{4,}", nucleo), key=lambda p: (-len(p), p.lower())
        )))
        filtros = [f"descripcion.ilike.%{termino}%,codigo.ilike.%{termino}%"]
        filtros += [f"descripcion.ilike.%{p}%" for p in palabras[:4]]
        for filtro in filtros:
            try:
                datos = client.table(TABLA_CATALOGO).select(COLUMNAS_CATALOGO).eq("activo", True).or_(filtro).limit(200).execute().data or []
            except Exception as err:
                print(f"--> precio-accion filtro: {err}")
                continue
            for row in datos:
                clave = row.get("id") or row.get("codigo")
                if clave not in vistos:
                    vistos.add(clave)
                    filas.append(row)
        if not filas:
            filas = list(buscar_filas_catalogo(nucleo) or [])
    tokens = _tokens_busqueda_catalogo(consulta)
    trabajo, demolicion = [], []
    for row in filas:
        item = normalizar_item_catalogo(row)
        if not item:
            continue
        if unidad and str(item.get("unidad") or "").upper() != unidad:
            continue
        es_vector = str(row.get("fuente") or "") == "vector"
        if es_vector:
            puntaje = 10 + _num(row.get("similarity")) * 10
        else:
            if _num(item.get("precio_unitario")) <= 0:
                continue
            puntaje = _puntaje_coincidencia_catalogo(row, nucleo, tokens, unidad)
            if puntaje < 6:
                continue
        destino = demolicion if _RE_FILA_DEMOLICION.search(item.get("descripcion") or "") else trabajo
        destino.append((puntaje, item))
    principal = max(trabajo, key=lambda c: c[0])[1] if trabajo else None
    fila_demo = max(demolicion, key=lambda c: c[0])[1] if demolicion else None
    item_ref = principal or fila_demo or {}
    demo_col = (
        _num((principal or {}).get("precio_remocion"))
        or _num((principal or {}).get("precio_demo"))
        or _num((fila_demo or {}).get("precio_remocion"))
        or _num((fila_demo or {}).get("precio_unitario"))
    )
    if principal:
        labor = _num(principal.get("precio_mano_obra") or principal.get("precio_labor"))
        material = _num(principal.get("precio_material"))
        demo = demo_col
    elif fila_demo:
        labor, material, demo = 0.0, 0.0, demo_col
    else:
        labor, material, demo = 0.0, 0.0, 0.0
    install = labor
    referencias = {"unidad": unidad, "desglose": _clave_desglose_categoria(item_ref.get("categoria") or "", item_ref.get("descripcion") or nucleo)}
    if install > 0:
        referencias["precio_unitario_catalogo"] = round(install, 2)
    if material > 0:
        referencias["precio_material"] = round(material, 2)
    if labor > 0:
        referencias["precio_mano_obra"] = round(labor, 2)
    if demo > 0:
        referencias["precio_demolicion"] = round(demo, 2)
    if principal:
        referencias["codigo"] = principal.get("codigo") or ""
        referencias["descripcion_catalogo"] = (principal.get("descripcion") or "")[:180]
        if principal.get("fuente") == "vector":
            referencias["metodo"] = "vector"
    if fila_demo:
        referencias["codigo_demolicion"] = fila_demo.get("codigo") or ""
        referencias["descripcion_demolicion"] = (fila_demo.get("descripcion") or "")[:180]
    paquete = _num((principal or {}).get("precio_unitario")) or _num(demo_col)
    precio = _precio_roles_por_accion(install, demo, material, labor, accion, paquete)
    return (precio if precio > 0 else None), (principal or fila_demo), referencias


def _precio_json_por_accion(accion, parsed):
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        return None, None
    componentes = {
        "remocion": _num_catalogo(parsed, "remocion", "demolicion", "demo", "demolition", "precio_demo"),
        "material": _num_catalogo(parsed, "material", "precio_material"),
        "instalacion": _num_catalogo(parsed, "instalacion", "labor", "mano_obra", "precio_labor"),
    }
    precio_modelo = _num_catalogo(parsed, "precio_unitario", "precio", "price", "unit_price")
    requeridos = COMPONENTES_POR_ACCION.get(accion, COMPONENTES_POR_ACCION["replace"])
    faltantes = [c for c in requeridos if componentes[c] <= 0]
    if faltantes:
        # Con un solo componente, el total del modelo es ese componente; en acciones combinadas no hay forma de saber qué falta.
        if len(requeridos) == 1 and precio_modelo > 0:
            otros = [componentes[c] for c in componentes if c != requeridos[0] and componentes[c] > 0]
            if any(abs(precio_modelo - otro) < 0.01 for otro in otros):
                print(f"--> Gemini reuso un componente ajeno ({precio_modelo}) como '{requeridos[0]}'; se descarta.")
                return None, None
            componentes[requeridos[0]] = precio_modelo
        else:
            print(f"--> Gemini omitio {faltantes} para '{accion}': {str(parsed)[:300]}".encode("ascii", "replace").decode("ascii"))
            return None, None
    precio = round(sum(componentes[c] for c in requeridos), 2)
    if accion == "material" and componentes["remocion"] > 0 and abs(precio - componentes["remocion"]) < 0.01:
        print("--> Gemini devolvio el precio de demolicion como Material only; se descarta.")
        return None, None
    if precio <= 0:
        return None, None
    if precio_modelo > 0 and abs(precio_modelo - precio) > 0.01:
        print(f"--> Gemini sumo {precio_modelo} pero los componentes de '{accion}' suman {precio}; se usa la suma.")
    return precio, {
        "labor": round(componentes["instalacion"], 2),
        "material": round(componentes["material"], 2),
        "demolicion": round(componentes["remocion"], 2),
        "analisis": str(parsed.get("analisis") or "")[:600],
        "suplementarias": _lineas_suplementarias_json(parsed.get("lineas_suplementarias")),
    }


def _lineas_suplementarias_json(crudas):
    if not isinstance(crudas, list):
        return []
    lineas = []
    for linea in crudas[:6]:
        if isinstance(linea, dict):
            texto = " ".join(str(linea.get(k) or "").strip() for k in ("codigo", "descripcion")).strip()
            prorrateo = _num(linea.get("prorrateo_instalacion"))
            if texto:
                lineas.append(f"{texto} (+${prorrateo:.2f})" if prorrateo > 0 else texto)
        elif str(linea or "").strip():
            lineas.append(str(linea).strip())
    return [l[:160] for l in lineas]


_ETIQUETAS_REFERENCIA_PRECIO = {
    "precio_unitario_catalogo": "instalacion_catalogo (solo instalación/mano de obra, sin material ni remoción)",
    "precio_mano_obra": "mano_obra_catalogo",
    "precio_material": "material_catalogo",
    "precio_demolicion": "remocion_catalogo",
}


def _mensaje_precio_gemini(ubicacion, accion, accion_texto, descripcion, unidad, referencias):
    refs = {
        _ETIQUETAS_REFERENCIA_PRECIO.get(clave, clave): valor
        for clave, valor in (referencias or {}).items()
        if valor not in (None, "", 0, 0.0)
    }
    requeridos = COMPONENTES_POR_ACCION.get(accion, COMPONENTES_POR_ACCION["replace"])
    excluidos = [c for c in ("remocion", "material", "instalacion") if c not in requeridos]
    material = _descripcion_catalogo_normalizada(descripcion) or descripcion
    terminos = {
        "remocion": f"(Costo de demolición de {material})",
        "material": f"(Costo del {material})",
        "instalacion": f"(Costo de mano de obra para instalar {material})",
    }
    formula = " + ".join(terminos[c] for c in ("remocion", "material", "instalacion") if c in requeridos)
    return (
        f"MATERIAL Y TRABAJO A EVALUAR: {descripcion}\n"
        f"ALCANCE DE COBRO EXACTO: {accion_texto}\n"
        f"UNIDAD: {unidad}\n"
        f"CÓDIGO POSTAL/ESTADO: {ubicacion}\n\n"
        f"Razonamiento cruzado: primero identifica el material ({material}) y sus costos de mercado; "
        f"después aplica el ALCANCE DE COBRO EXACTO '{accion_texto}'.\n"
        f"Estás OBLIGADO a devolver precio_unitario = {formula}. "
        "Nunca devuelvas un precio inferior a la suma de estos componentes.\n"
        "Lee la ACCIÓN y la DESCRIPCIÓN. Si necesitas el costo del material, DEBES usar la herramienta de búsqueda de Google "
        "para buscar el precio de venta actual de ese producto en tiendas de EE. UU. (ej. Home Depot, Lowe's). "
        "Devuelve tu cálculo basándote ESTRICTAMENTE en los resultados de búsqueda web en tiempo real.\n"
        f"Componentes que esta acción suma: {', '.join(requeridos)}.\n"
        f"Componentes que esta acción NO suma (reporta igual su valor base): {', '.join(excluidos) or 'ninguno'}.\n\n"
        "Precios de referencia que ya existen en Supabase. Son promedios nacionales, anteriores al ajuste de zona, "
        "y cada uno cubre SOLO el componente que indica su nombre. "
        "Si un componente no aparece aquí, no existe en el catálogo y debes estimarlo con tarifas de mercado de EE. UU.\n"
        + json.dumps(refs, ensure_ascii=False)
        + "\n\n"
        "Sigue los PASOS 1 a 6 del proceso obligatorio. remocion, material e instalacion son los valores base en USD por "
        f"{unidad}, ya ajustados al código postal/estado; precio_unitario es la suma de los componentes que la acción suma."
    )


_CACHE_PRECIO_IA = {}
_cache_precio_lock = threading.Lock()
HERRAMIENTA_BUSQUEDA_GOOGLE = types.Tool(google_search=types.GoogleSearch())


def _zip_cache_precio(ubicacion):
    digitos = re.sub(r"\D", "", str(ubicacion or ""))[:5]
    if len(digitos) == 5:
        return digitos
    return str(ubicacion or "").strip().upper() or "US"


def _clave_cache_precio(descripcion, accion, ubicacion):
    return (
        _limpiar_termino_catalogo(descripcion).lower(),
        str(accion or "").strip().lower(),
        _zip_cache_precio(ubicacion),
    )


def _precio_gemini_por_accion(ubicacion, accion, accion_texto, descripcion, unidad, referencias):
    clave = _clave_cache_precio(descripcion, accion, ubicacion)
    with _cache_precio_lock:
        guardado = _CACHE_PRECIO_IA.get(clave)
    if guardado:
        print(f"--> precio IA en cache desc+accion+zip {clave} = ${guardado[0]}".encode("ascii", "replace").decode("ascii"))
        return guardado[0], dict(guardado[1])
    precio, desglose = _precio_gemini_consulta(ubicacion, accion, accion_texto, descripcion, unidad, referencias)
    if precio:
        with _cache_precio_lock:
            _CACHE_PRECIO_IA[clave] = (precio, dict(desglose))
    return precio, desglose


def _config_gemini_precio(con_json=True):
    kwargs = {
        "system_instruction": CONTEXTO_ESTIMADOR_PRECIOS,
        "tools": [HERRAMIENTA_BUSQUEDA_GOOGLE],
    }
    if con_json:
        kwargs["response_mime_type"] = "application/json"
    return types.GenerateContentConfig(**kwargs)


def _precio_gemini_consulta(ubicacion, accion, accion_texto, descripcion, unidad, referencias):
    prompt = _mensaje_precio_gemini(ubicacion, accion, accion_texto, descripcion, unidad, referencias)
    ultimo_error = None
    for modelo in ("gemini-2.5-flash", "gemini-3.6-flash", "gemini-flash-latest"):
        try:
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=_config_gemini_precio(True),
                )
            except Exception as err_json:
                texto_json = str(err_json).lower()
                if any(t in texto_json for t in ("tool", "mime", "json", "grounding", "search", "invalid argument", "400")):
                    print(f"--> Gemini {modelo}: reintento con Search y sin JSON forzado ({err_json})")
                    response = client.models.generate_content(
                        model=modelo,
                        contents=prompt,
                        config=_config_gemini_precio(False),
                    )
                else:
                    raise
        except Exception as err:
            ultimo_error = err
            texto = str(err).lower()
            if any(t in texto for t in ("503", "unavailable", "high demand", "overloaded", "429", "404", "not_found", "not found")):
                continue
            raise
        precio, desglose = _precio_json_por_accion(accion, extraer_json(getattr(response, "text", None) or ""))
        if precio:
            linea = (
                f"--> Gemini {modelo} '{accion}' {descripcion} = ${precio} | {desglose.get('analisis')}"
                f" | suplementarias: {'; '.join(desglose.get('suplementarias') or []) or 'ninguna'}"
            )
            print(linea.encode("ascii", "replace").decode("ascii"))
            return precio, desglose
    if ultimo_error:
        raise ultimo_error
    return None, None


@app.route("/api/precio-accion", methods=["POST"])
def precio_accion():
    payload = request.get_json(silent=True) or {}
    accion = str(payload.get("accion") or "install_material").strip().lower()
    if accion not in ACCIONES_PRECIO:
        accion = "install_material"
    accion_texto = str(payload.get("accion_texto") or "").strip() or ACCIONES_PRECIO[accion]
    descripcion = re.sub(r"\s+", " ", str(payload.get("descripcion") or "")).strip()[:300]
    unidad = str(payload.get("unidad") or "SF").strip().upper()[:6]
    ubicacion = re.sub(r"[^\w\s-]", "", str(payload.get("ubicacion") or "")).strip()[:20]
    if not descripcion:
        return jsonify({"success": False, "error": "Falta la descripción del ítem"}), 400

    factor, zona = _factor_ubicacion(ubicacion)
    mercado = ubicacion or "Estados Unidos (promedio nacional)"
    try:
        precio_base, item, referencias = _precio_supabase_por_accion(descripcion, unidad, accion)
    except Exception as err:
        print(f"--> precio-accion Supabase: {err}")
        precio_base, item, referencias = None, None, {"unidad": unidad}
    precio_ia, desglose = None, None
    if not precio_base:
        try:
            precio_ia, desglose = _precio_gemini_por_accion(mercado, accion, accion_texto, descripcion, unidad, referencias)
        except Exception as err:
            print(f"--> precio-accion Gemini: {err}")
            precio_ia, desglose = None, None
    if precio_ia:
        return jsonify({
            "success": True,
            "fuente": "gemini",
            "precio": round(precio_ia, 2),
            "labor": (desglose or {}).get("labor"),
            "material": (desglose or {}).get("material"),
            "demolicion": (desglose or {}).get("demolicion"),
            "suplementarias": (desglose or {}).get("suplementarias") or [],
            "ubicacion": ubicacion,
            "estado": zona.get("estado"),
        })
    if precio_base:
        return jsonify({
            "success": True,
            "fuente": "supabase",
            "precio": round(precio_base * factor, 2),
            "precio_base": round(precio_base, 2),
            "factor": round(factor, 4),
            "labor": round(_num((referencias or {}).get("precio_mano_obra")) * factor, 2),
            "material": round(_num((referencias or {}).get("precio_material")) * factor, 2),
            "demolicion": round(_num((referencias or {}).get("precio_demolicion")) * factor, 2),
            "codigo": (item or {}).get("codigo") or "",
            "metodo": (referencias or {}).get("metodo") or "texto",
            "desglose": (referencias or {}).get("desglose") or "",
            "ubicacion": ubicacion,
            "estado": zona.get("estado"),
        })
    return jsonify({"success": False, "error": "No hay precio en Supabase y Gemini no respondió"}), 502


def _descripcion_catalogo_normalizada(descripcion):
    texto = re.sub(r"\s+", " ", _corregir_typos_catalogo(descripcion)).strip()
    for _ in range(4):
        nuevo = _PREFIJO_ACCION_CATALOGO.sub("", texto).strip()
        if nuevo == texto:
            break
        texto = nuevo
    texto = texto.strip(" \t-–:.")[:180]
    return texto[:1].upper() + texto[1:]


_aviso_rls_catalogo = False
_SQL_POLITICA_CATALOGO_IA = (
    "create policy \"catalogo_items insert IA\" on public.catalogo_items\n"
    "  for insert to anon, authenticated with check (codigo like 'IA %');"
)


def _codigo_catalogo_ia(descripcion, unidad, sufijo=""):
    import hashlib

    base = f"{_limpiar_termino_catalogo(descripcion).lower()}|{unidad}"
    return f"IA {hashlib.sha1(base.encode('utf-8')).hexdigest()[:6].upper()}{sufijo}"


def _fila_catalogo_existe(sb, codigo, descripcion, unidad):
    por_codigo = sb.table(TABLA_CATALOGO).select("id").eq("codigo", codigo).limit(1).execute().data
    if por_codigo:
        return True
    patron = re.sub(r"([%_\\])", r"\\\1", descripcion)
    por_desc = (
        sb.table(TABLA_CATALOGO).select("id")
        .ilike("descripcion", patron).eq("unidad", unidad).limit(1).execute().data
    )
    return bool(por_desc)


@app.route("/api/catalogo/guardar-ia", methods=["POST"])
def guardar_precio_ia_catalogo():
    payload = request.get_json(silent=True) or {}
    global _aviso_rls_catalogo
    items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
    sb = _supabase_client()
    if sb is None:
        return jsonify({"success": False, "error": "Cliente supabase es None"}), 503
    if _SB_ROL != "service":
        print("--> catalogo IA: no se inserta. Falta SUPABASE_SERVICE_ROLE_KEY y RLS exige authenticated.")
        return jsonify({
            "success": False,
            "error": "Falta SUPABASE_SERVICE_ROLE_KEY. El INSERT de catalogo_items exige la clave de servicio.",
        }), 503
    resultados = []
    for raw in items[:50]:
        if not isinstance(raw, dict):
            continue
        descripcion = _descripcion_catalogo_normalizada(raw.get("descripcion"))
        unidad = str(raw.get("unidad") or "SF").strip().upper()[:6]
        accion = str(raw.get("accion") or "").strip().lower()
        if accion not in ACCIONES_PRECIO:
            accion = "install_material"
        clave = raw.get("clave")
        if len(descripcion) < 3:
            resultados.append({"clave": clave, "estado": "omitido", "motivo": "descripción vacía"})
            continue
        factor, _zona = _factor_ubicacion(re.sub(r"[^\w\s-]", "", str(raw.get("ubicacion") or ""))[:20])
        factor = factor if factor > 0 else 1.0
        precio = _num(raw.get("precio")) / factor
        labor = _num(raw.get("labor")) / factor
        material = _num(raw.get("material")) / factor
        demolicion = _num(raw.get("demolicion")) / factor
        if accion == "material" and material <= 0:
            material = precio
        elif accion == "demo" and demolicion <= 0:
            demolicion = precio
        elif accion in ("install", "labor") and labor <= 0:
            labor = precio
        elif accion == "install_material" and labor <= 0 and material <= 0:
            labor = precio

        categoria = re.sub(r"[^A-ZÁÉÍÓÚÜÑ_ ]", "", descripcion.split(" - ", 1)[0].upper()).strip()
        categoria = categoria if 0 < len(categoria) <= 24 else "GENERAL"
        filas = []
        if labor > 0:
            filas.append({
                "categoria": categoria,
                "codigo": _codigo_catalogo_ia(descripcion, unidad),
                "descripcion": descripcion,
                "unidad": unidad,
                "precio_base": round(labor, 2),
                "precio_mano_obra": round(labor, 2),
                "precio_material": round(max(material, 0.0), 2),
            })
        if demolicion > 0:
            desc_demo = f"{descripcion} - Retiro / demolición"[:200]
            filas.append({
                "categoria": categoria,
                "codigo": _codigo_catalogo_ia(descripcion, unidad, "D"),
                "descripcion": desc_demo,
                "unidad": unidad,
                "precio_base": round(demolicion, 2),
                "precio_mano_obra": round(demolicion, 2),
                "precio_material": 0.0,
            })
        if not filas:
            resultados.append({"clave": clave, "estado": "omitido", "motivo": "sin componentes de precio guardables"})
            continue
        insertadas, existentes, error = 0, 0, None
        for fila in filas:
            try:
                if _fila_catalogo_existe(sb, fila["codigo"], fila["descripcion"], unidad):
                    existentes += 1
                    continue
                try:
                    vector = _embedding_texto(fila["descripcion"], unidad)
                    if vector:
                        fila["embedding"] = vector
                except Exception as err_emb:
                    print(f"--> embedding IA omitido: {err_emb}")
                print(f"--> catalogo IA insert rol={_SB_ROL} codigo={fila['codigo']} unidad={unidad}")
                sb.table(TABLA_CATALOGO).insert(fila).execute()
                insertadas += 1
                print(f"--> catálogo IA: insertado {fila['codigo']} '{fila['descripcion']}' {unidad} ${fila['precio_base']}")
            except Exception as err:
                error = str(err)[:300]
                print(f"--> catálogo IA: error insertando '{fila['descripcion']}': {err}")
                if not _aviso_rls_catalogo and re.search(r"row-level security|42501", error, re.I):
                    _aviso_rls_catalogo = True
                    print("--> catalogo_items bloquea inserts (RLS). Revisa que exista esta política:")
                    print(_SQL_POLITICA_CATALOGO_IA)
        if error and not insertadas:
            estado = "error"
        elif insertadas:
            estado = "guardado"
        else:
            estado = "existente"
        resultados.append({"clave": clave, "estado": estado, "insertadas": insertadas, "existentes": existentes, "error": error})
    ok = any(r.get("estado") in ("guardado", "existente") for r in resultados)
    return jsonify({"success": ok or not resultados, "resultados": resultados}), (200 if ok or not resultados else 502)


PROMPT_SUGERIR_DESCRIPCION = (
    "Eres un experto en estándares de construcción y ajustador de seguros (Xactimate). "
    "El usuario está tecleando la descripción de un material de construcción (puede tener faltas de ortografía, "
    "ej. 'LBT' en lugar de 'LVT', o descripciones muy básicas como 'techo asfáltico').\n"
    "Tu tarea es predecir y sugerir de 3 a 4 descripciones técnicas, limpias, altamente profesionales y "
    "estandarizadas para cotizaciones. Las sugerencias DEBEN ser solo sustantivos (el material y sus especificaciones). "
    "NUNCA incluyas verbos (como instalar, remover).\n"
    'Devuelve ESTRICTAMENTE un JSON válido con este formato: {"sugerencias": ["Opción 1", "Opción 2", "Opción 3"]}'
)
_RE_PREFIJO_ACCION_DESC = re.compile(
    r"^(?:instalaci[oó]n(?:\s+de)?|instalar|install(?:ation)?(?:\s+of)?|remover(?:\s+e\s+instalar)?|"
    r"remove(?:\s+and\s+install)?|demolici[oó]n(?:\s+de)?|demolition(?:\s+of)?|colocaci[oó]n(?:\s+de)?|"
    r"colocar|suministro(?:\s+e\s+instalaci[oó]n)?(?:\s+de)?)\s+",
    re.I,
)


def _limpiar_sugerencia_descripcion(texto):
    limpio = re.sub(r"\s+", " ", str(texto or "")).strip(" \t-–:.")
    for _ in range(3):
        siguiente = _RE_PREFIJO_ACCION_DESC.sub("", limpio).strip(" \t-–:.")
        if not siguiente or siguiente == limpio:
            break
        limpio = siguiente
    return limpio[:180]


def _sugerencias_descripcion_desde_json(parsed):
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        parsed = parsed[0]
    crudas = parsed.get("sugerencias") if isinstance(parsed, dict) else parsed
    if isinstance(crudas, str):
        crudas = [crudas]
    if not isinstance(crudas, list):
        return []
    sugerencias = []
    for item in crudas:
        if isinstance(item, dict):
            item = item.get("texto") or item.get("descripcion") or item.get("text") or ""
        texto = _limpiar_sugerencia_descripcion(item)
        if texto and texto not in sugerencias:
            sugerencias.append(texto)
        if len(sugerencias) == 4:
            break
    return sugerencias


@app.route("/api/sugerir_descripcion", methods=["POST"])
def sugerir_descripcion():
    payload = request.get_json(silent=True) or {}
    query = re.sub(r"\s+", " ", str(payload.get("query") or "")).strip()[:200]
    if len(query) < 3:
        return jsonify({"sugerencias": []})
    ultimo_error = None
    for modelo in ("gemini-2.5-flash", "gemini-3.6-flash", "gemini-flash-latest"):
        try:
            response = client.models.generate_content(
                model=modelo,
                contents=f"Texto parcial del usuario: {query}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    system_instruction=PROMPT_SUGERIR_DESCRIPCION,
                ),
            )
        except Exception as err:
            ultimo_error = err
            texto = str(err).lower()
            if any(t in texto for t in ("503", "unavailable", "high demand", "overloaded", "429", "404", "not_found", "not found")):
                continue
            print(f"--> sugerir-descripcion Gemini: {err}")
            return jsonify({"sugerencias": []}), 502
        sugerencias = _sugerencias_descripcion_desde_json(extraer_json(getattr(response, "text", None) or ""))
        if sugerencias:
            return jsonify({"sugerencias": sugerencias})
    if ultimo_error:
        print(f"--> sugerir-descripcion Gemini: {ultimo_error}")
        return jsonify({"sugerencias": []}), 502
    return jsonify({"sugerencias": []})


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
        from roof_parser import accion_exacta_item
        partida["action"] = accion_exacta_item(desc, row.get("action"))
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


def _aplicar_coincidencia_vector(row, match):
    precio = 0.0
    try:
        precio = float(match.get("precio_base") or 0)
    except (TypeError, ValueError):
        precio = 0.0
    codigo = str(match.get("codigo") or "").strip()
    if precio <= 0 or not codigo:
        return False
    try:
        qty = float(row.get("quantity") or row.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    total = round(qty * precio, 2) if qty else 0.0
    row["codigo"] = codigo
    row["code"] = codigo
    row["sku"] = codigo
    row["codigo_base"] = codigo
    row["unit_price"] = precio
    row["precio_unitario"] = precio
    row["precio"] = precio
    row["price"] = precio
    row["precio_base"] = precio
    row["precio_demo"] = 0.0
    row["precio_install"] = precio
    row["UNIT PRICE"] = precio
    if qty:
        row["total"] = total
        row["subtotal"] = total
        row["importe"] = total
    row["action"] = "install_material"
    row["ACTION"] = "Install plus materials"
    return True


def _match_catalogo_vector(client, vector, unidad=""):
    payload = {
        "query_embedding": vector,
        "match_threshold": UMBRAL_SIMILITUD_CATALOGO,
        "match_count": 5,
    }
    if unidad:
        payload["filter_unidad"] = str(unidad).upper()
    ultimo = None
    for nombre in ("match_catalog_items", "match_items"):
        try:
            datos = client.rpc(nombre, payload).execute().data or []
            if isinstance(datos, list) and datos:
                return datos[0]
            if isinstance(datos, list):
                return None
        except Exception as err:
            ultimo = err
            print(f"--> {nombre}: {type(err).__name__}: {err}")
    if ultimo and "filter_unidad" in payload:
        payload.pop("filter_unidad", None)
        try:
            datos = client.rpc("match_catalog_items", payload).execute().data or []
            if isinstance(datos, list) and datos:
                return datos[0]
        except Exception as err:
            print(f"--> match_catalog_items sin unidad: {err}")
    return None


def _preciar_medidas_por_vector(filas):
    """Cada descripción del roof report se embebe y toma el precio, código y acción del catálogo."""
    from roof_parser import aplicar_precios_reales_catalogo

    pendientes = [row for row in filas or [] if isinstance(row, dict)]
    if not pendientes:
        return filas
    textos = [
        _texto_para_embedding(
            row.get("description") or row.get("descripcion") or "",
            row.get("unit") or row.get("unidad") or "",
        )
        for row in pendientes
    ]
    try:
        vectores = _embeddings_openai(textos)
    except Exception as err:
        print(f"--> medidas vector: embedding fallo {type(err).__name__}: {err}")
        aplicar_precios_reales_catalogo(filas)
        return filas
    client = _supabase_client()
    if client is None:
        print("--> medidas vector: sin cliente Supabase")
        aplicar_precios_reales_catalogo(filas)
        return filas
    sin_match = []
    for row, vector in zip(pendientes, vectores):
        if not vector:
            sin_match.append(row)
            continue
        unidad = str(row.get("unit") or row.get("unidad") or "").upper()
        match = _match_catalogo_vector(client, vector, unidad)
        if not match or not _aplicar_coincidencia_vector(row, match):
            sin_match.append(row)
            continue
        print(
            f"--> vector '{str(row.get('description') or '')[:50]}' "
            f"-> {row.get('codigo')} ${row.get('unit_price')} sim={match.get('similarity')}"
        )
    if sin_match:
        aplicar_precios_reales_catalogo(sin_match)
    return filas


async def _cliente_openai_http_async():
    """Un AsyncClient por hilo y event loop, con keep-alive."""
    import asyncio
    import httpx

    cfg, key, verify = _cfg_cliente_openai()
    if not key:
        raise RuntimeError("Falta OPENAI_API_KEY en .env")
    loop = asyncio.get_running_loop()
    cliente = getattr(_openai_async_local, "client", None)
    if cliente is not None and getattr(_openai_async_local, "cfg", None) == cfg and getattr(_openai_async_local, "loop", None) is loop:
        return cliente
    if cliente is not None:
        try:
            await cliente.aclose()
        except Exception:
            pass
    cliente = httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
        },
        verify=verify,
        timeout=_timeout_openai(),
        limits=_limites_openai(),
        http2=False,
    )
    _openai_async_local.client = cliente
    _openai_async_local.cfg = cfg
    _openai_async_local.loop = loop
    return cliente


async def _embeddings_openai_async(textos):
    """OpenAI text-embedding-3-small. No lee el PDF."""
    entradas = [re.sub(r"\s+", " ", str(t or "")).strip()[:4000] for t in textos]
    if not any(entradas):
        return [None] * len(textos)
    http = await _cliente_openai_http_async()
    resp = await http.post("/embeddings", json={"model": EMBEDDING_MODELO, "input": entradas})
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI embeddings HTTP {resp.status_code}: {(resp.text or '')[:300]}")
    datos = (resp.json() or {}).get("data") or []
    return _vectores_de_respuesta_openai(entradas, datos)


async def _rpc_match_catalogo_async(http, url, key, vector, unidad=""):
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    cuerpo = {
        "query_embedding": vector,
        "match_threshold": UMBRAL_SIMILITUD_CATALOGO,
        "match_count": 5,
    }
    if unidad:
        cuerpo["filter_unidad"] = unidad
    for nombre in ("match_catalog_items", "match_items"):
        resp = await http.post(f"{url}/rest/v1/rpc/{nombre}", json=cuerpo, headers=headers)
        if resp.status_code == 404:
            continue
        if resp.status_code >= 400:
            print(f"--> {nombre}: HTTP {resp.status_code} {(resp.text or '')[:200]}")
            continue
        datos = resp.json() if resp.content else []
        if isinstance(datos, list) and datos:
            return datos[0]
        if isinstance(datos, list) and unidad:
            cuerpo.pop("filter_unidad", None)
            unidad = ""
            continue
        if isinstance(datos, list):
            return None
    return None


def _mezclar_items_anthropic(items, parsed):
    """Usa la descripción que devolvió Anthropic en cada partida, sin tocar la cantidad medida."""
    crudos = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(crudos, list):
        return
    usados = set()
    for row in items or []:
        if not isinstance(row, dict):
            continue
        desc_row = str(row.get("description") or row.get("descripcion") or "").lower()
        unit = str(row.get("unit") or row.get("unidad") or "").upper()
        elegido = None
        for indice, raw in enumerate(crudos):
            if indice in usados or not isinstance(raw, dict):
                continue
            desc = str(raw.get("description") or raw.get("descripcion") or "").strip()
            if not desc:
                continue
            unidad = str(raw.get("unit") or raw.get("unidad") or "").upper()
            if unit and unidad and unit != unidad:
                continue
            texto = desc.lower()
            tokens = [t for t in re.split(r"[^a-z0-9]+", desc_row) if len(t) > 3]
            if tokens and any(t in texto for t in tokens):
                elegido = (indice, desc)
                break
            if elegido is None:
                elegido = (indice, desc)
        if not elegido:
            continue
        usados.add(elegido[0])
        row["description"] = elegido[1]
        row["descripcion"] = elegido[1]
        row["desc"] = elegido[1]
        row["nucleo"] = elegido[1]


async def _preciar_medidas_vector_async(filas):
    """Después de Anthropic: embedding de OpenAI y coincidencia pgvector, en ese orden."""
    from roof_parser import aplicar_precios_reales_catalogo

    pendientes = [row for row in filas or [] if isinstance(row, dict)]
    if not pendientes:
        return filas
    textos = [
        _texto_para_embedding(
            row.get("description") or row.get("descripcion") or "",
            row.get("unit") or row.get("unidad") or "",
        )
        for row in pendientes
    ]
    print("--> 2/3 OpenAI: embeddings de las descripciones")
    try:
        vectores = await _embeddings_openai_async(textos)
    except Exception as err:
        print(f"--> medidas vector: embedding fallo {type(err).__name__}: {err}")
        aplicar_precios_reales_catalogo(filas)
        return filas
    url, key = _supabase_creds()
    if not url or not key:
        aplicar_precios_reales_catalogo(filas)
        return filas
    print("--> 3/3 Supabase: match_catalog_items")
    import httpx

    sin_match = []
    async with httpx.AsyncClient(verify=False, timeout=45.0) as http:
        for row, vector in zip(pendientes, vectores):
            if not vector:
                sin_match.append(row)
                continue
            unidad = str(row.get("unit") or row.get("unidad") or "").upper()
            match = await _rpc_match_catalogo_async(http, url, key, vector, unidad)
            if not match or not _aplicar_coincidencia_vector(row, match):
                sin_match.append(row)
                continue
            print(
                f"--> vector '{str(row.get('description') or '')[:50]}' "
                f"-> {row.get('codigo')} ${row.get('unit_price')} sim={match.get('similarity')}"
            )
    if sin_match:
        aplicar_precios_reales_catalogo(sin_match)
    return filas


async def _importar_medidas_async(pdf_bytes, zip_code):
    """1 Anthropic (ítems y cantidades) → 2 OpenAI (vectores) → 3 Supabase (precio, código, acción)."""
    import asyncio

    from claude_pdf import analizar_pdf_json_async
    from roof_parser import (
        MODELO_CLAUDE,
        MODELOS_CLAUDE,
        PROMPT_REPORT_SUMMARY,
        SYSTEM_PROMPT_MEDIDAS_TECHO,
        payload_http_cotizador,
        procesar_reporte_roofr,
    )

    print("--> 1/3 Anthropic: extrayendo ítems y cantidades del PDF")
    parsed = {}
    try:
        parsed = await analizar_pdf_json_async(
            pdf_bytes,
            PROMPT_REPORT_SUMMARY,
            modelo=MODELO_CLAUDE,
            modelos=MODELOS_CLAUDE,
            system=SYSTEM_PROMPT_MEDIDAS_TECHO,
        )
    except Exception as err:
        print(f"--> Anthropic no leyó el PDF ({err}). Se usan las medidas del texto.")
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    bruto = await asyncio.to_thread(
        procesar_reporte_roofr,
        pdf_bytes,
        zip_code,
        "SQ",
        None,
        _zona_mercado_por_zip,
        parsed,
    )
    _mezclar_items_anthropic(bruto.get("items") or [], parsed)
    envuelto = payload_http_cotizador(bruto)
    await _preciar_medidas_vector_async(envuelto.get("items") or [])
    total = round(sum(float(f.get("total") or 0) for f in (envuelto.get("items") or [])), 2)
    envuelto["total"] = total
    envuelto["grand_total"] = total
    envuelto["partidas"] = envuelto["data"] = envuelto["line_items"] = envuelto["rows"] = envuelto.get("items") or []
    return envuelto


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
        if not getattr(app, "_ssl_import_pdf_listo", False):
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
                if not getattr(_req_ssl.Session.request, "_zipnova_sin_verify", False):
                    _orig_req = _req_ssl.Session.request

                    def _request_sin_verify(self, method, url, **kwargs):
                        kwargs["verify"] = False
                        return _orig_req(self, method, url, **kwargs)

                    _request_sin_verify._zipnova_sin_verify = True
                    _req_ssl.Session.request = _request_sin_verify
            except Exception:
                pass
            app._ssl_import_pdf_listo = True

        from roof_parser import accion_exacta_item

        file = request.files.get("file") or request.files.get("pdf_file")
        if file is None:
            return jsonify({"success": False, "error": "No se encontró el archivo PDF"}), 400
        if file.filename == "":
            return jsonify({"success": False, "error": "Archivo no seleccionado"}), 400

        pdf_bytes = file.read()
        if not pdf_bytes:
            return jsonify({"success": False, "error": "Archivo no seleccionado"}), 400
        bloqueo = _consumir_extraccion_ia_pdf()
        if bloqueo:
            return bloqueo

        zip_form = re.sub(r"\D", "", str(request.form.get("zip_code") or request.args.get("zip_code") or ""))[:5]
        import asyncio
        payload = asyncio.run(_importar_medidas_async(pdf_bytes, zip_form))
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
                "action": accion_exacta_item(row.get("description") or row.get("descripcion") or "", row.get("action")),
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
            "grand_total": round(sum(float(f.get("total") or 0) for f in filas), 2),
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
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": f"No se pudieron extraer mediciones del roof report: {str(e)}"}), 500


@app.route("/api/process-pdf", methods=["POST"])
def process_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No se envió ningún archivo"}), 400

    file = request.files["file"]
    pdf_bytes = file.read()
    if not pdf_bytes:
        return jsonify({"error": "El PDF está vacío"}), 400
    bloqueo = _consumir_extraccion_ia_pdf()
    if bloqueo:
        return bloqueo

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
        from roof_parser import aplicar_precios_reales_catalogo
        aplicar_precios_reales_catalogo(items, zona.get("factor") or 1.0)
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


@app.route("/api/import-line-items", methods=["POST"])
@app.route("/api/import-past-estimate", methods=["POST"])
def import_line_items_ia():
    archivo = request.files.get("file") or request.files.get("pdf")
    if archivo is None:
        return jsonify({"success": False, "error": "No PDF was uploaded"}), 400
    nombre = str(getattr(archivo, "filename", "") or "")
    if nombre and not nombre.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Upload a PDF file"}), 400
    pdf_bytes = archivo.read()
    if not pdf_bytes:
        return jsonify({"success": False, "error": "The PDF is empty"}), 400
    bloqueo = _consumir_extraccion_ia_pdf()
    if bloqueo:
        return bloqueo
    try:
        from estimate_pdf_parser import extraer_texto_pdf

        texto = extraer_texto_pdf(pdf_bytes)
        if not str(texto or "").strip():
            resp = jsonify({"success": False, "error": "The PDF has no extractable text.", "items": []})
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp, 422
        parsed = None
        ultimo = None
        for modelo in ("gemini-2.5-flash", "gemini-flash-latest", "gemini-3.6-flash"):
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=f"{PROMPT_IMPORT_LINE_ITEMS}\n\n--- PDF TEXT ---\n{texto[:120000]}",
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                parsed = extraer_json(getattr(response, "text", None) or "")
                if parsed is not None:
                    break
            except Exception as err:
                ultimo = err
                print(f"--> import line items Gemini {modelo}: {err}")
                continue
        if parsed is None:
            raise ultimo or RuntimeError("The AI did not return JSON")
        items = _normalizar_partidas_ia(parsed)
        payload = {
            "success": True,
            "items": items,
            "count": len(items),
            "source": "gemini",
        }
        status = 200 if items else 422
        if not items:
            payload["success"] = False
            payload["error"] = "The AI did not find line items in this PDF."
        resp = jsonify(payload)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, status
    except Exception as e:
        resp = jsonify({"success": False, "error": f"Could not import line items: {e}", "items": []})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 500


def _dinero_partida_ia(valor):
    if valor is None or valor == "":
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    raw = re.sub(r"[^\d.,-]", "", str(valor)).replace(",", "")
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _accion_partida_ia(valor):
    blob = re.sub(r"\s+", " ", str(valor or "")).strip().lower()
    mapa = {
        "remove, install plus materials": "replace_material",
        "remove and install": "replace",
        "install plus materials": "install_material",
        "install only": "install",
        "demolition only": "demo",
        "material only": "material",
        "replace_material": "replace_material",
        "replace": "replace",
        "install_material": "install_material",
        "install": "install_material",
        "demo": "demo",
        "material": "material",
        "labor": "labor",
    }
    if blob in mapa:
        return mapa[blob]
    if "demolition" in blob or "demo" in blob:
        return "demo"
    if "material only" in blob:
        return "material"
    if "install plus" in blob:
        return "install_material"
    if "install only" in blob:
        return "install"
    if "remove" in blob and "install" in blob:
        return "replace_material" if "material" in blob else "replace"
    return "install_material"


def _normalizar_partidas_ia(parsed):
    filas = parsed
    if isinstance(parsed, dict):
        filas = parsed.get("items") or parsed.get("partidas") or parsed.get("data") or []
    if not isinstance(filas, list):
        return []
    items = []
    for row in filas:
        if not isinstance(row, dict):
            continue
        desc = str(
            row.get("WORK DESCRIPTION")
            or row.get("work description")
            or row.get("description")
            or row.get("descripcion")
            or ""
        ).strip()
        if not desc:
            continue
        qty = _dinero_partida_ia(row.get("QTY") if row.get("QTY") is not None else row.get("qty", row.get("quantity", 1)))
        unit = str(row.get("UNIT") or row.get("unit") or "SF").strip().upper() or "SF"
        unit_price = _dinero_partida_ia(
            row.get("UNIT PRICE") if row.get("UNIT PRICE") is not None else row.get("unit_price", row.get("UNIT_PRICE"))
        )
        subtotal = _dinero_partida_ia(
            row.get("SUBTOTAL") if row.get("SUBTOTAL") is not None else row.get("subtotal")
        )
        if unit_price <= 0 and subtotal and qty:
            unit_price = round(subtotal / qty, 2)
        if subtotal <= 0 and qty and unit_price:
            subtotal = round(qty * unit_price, 2)
        action_raw = row.get("ACTION") or row.get("action") or ""
        from roof_parser import accion_exacta_item
        items.append({
            "CODE": str(row.get("CODE") or row.get("code") or "").strip(),
            "ACTION": str(action_raw).strip() or "Remove and install",
            "WORK DESCRIPTION": desc,
            "QTY": qty if qty else 1.0,
            "UNIT": unit,
            "UNIT PRICE": unit_price,
            "SUBTOTAL": subtotal,
            "code": str(row.get("CODE") or row.get("code") or "").strip(),
            "action": accion_exacta_item(desc, action_raw),
            "description": desc,
            "qty": qty if qty else 1.0,
            "unit": unit,
            "unit_price": unit_price,
            "subtotal": subtotal,
        })
    from roof_parser import aplicar_precios_reales_catalogo
    return aplicar_precios_reales_catalogo(items)


def _titulo_tabla_plano(clave):
    texto = re.sub(r"[_\-]+", " ", str(clave or "")).strip()
    return texto.title() if texto else "Schedule"


def _es_lista_objetos(valor):
    return isinstance(valor, list) and bool(valor) and all(isinstance(x, dict) for x in valor)


def _es_lista_notas(valor):
    if not isinstance(valor, list) or not valor:
        return False
    return all(isinstance(x, str) or (isinstance(x, dict) and (x.get("text") or x.get("note") or x.get("nota"))) for x in valor)


def _fusionar_json_planos(destino, extra):
    if not isinstance(extra, dict):
        return destino if isinstance(destino, dict) else {}
    if not isinstance(destino, dict):
        return dict(extra)
    for clave, valor in extra.items():
        if clave not in destino or destino[clave] in (None, "", [], {}):
            destino[clave] = valor
            continue
        actual = destino[clave]
        if isinstance(actual, list) and isinstance(valor, list):
            destino[clave] = actual + valor
        elif isinstance(actual, dict) and isinstance(valor, dict):
            _fusionar_json_planos(actual, valor)
    return destino


def _notas_desde_valor(valor, kind, source_key):
    notas = []
    if isinstance(valor, str) and valor.strip():
        notas.append({"text": valor.strip(), "kind": kind, "source_key": source_key, "source_page": None})
        return notas
    if not isinstance(valor, list):
        return notas
    for item in valor:
        if isinstance(item, str) and item.strip():
            notas.append({"text": item.strip(), "kind": kind, "source_key": source_key, "source_page": None})
        elif isinstance(item, dict):
            texto = str(item.get("text") or item.get("note") or item.get("nota") or item.get("description") or "").strip()
            if not texto:
                continue
            notas.append({
                "text": texto,
                "kind": str(item.get("kind") or kind),
                "source_key": source_key,
                "source_page": item.get("source_page") or item.get("page"),
            })
    return notas


def _columnas_tabla_plano(filas):
    orden = []
    vistos = set()
    preferidas = (
        "mark", "code", "codigo", "type", "description", "descripcion", "size", "width",
        "height", "qty", "quantity", "cantidad", "unit", "unidad", "material", "finish",
        "location", "remarks", "notes", "source_page",
    )
    for fila in filas or []:
        if not isinstance(fila, dict):
            continue
        for clave in fila.keys():
            k = str(clave)
            if k.lower() in vistos:
                continue
            vistos.add(k.lower())
            orden.append(k)
    ranking = {k: i for i, k in enumerate(preferidas)}
    orden.sort(key=lambda c: (ranking.get(str(c).lower(), 80), str(c).lower()))
    return orden[:16]


def _celda_plano(fila, claves, default=""):
    if not isinstance(fila, dict):
        return default
    lower = {str(k).lower(): v for k, v in fila.items()}
    for clave in claves:
        if clave.lower() in lower and lower[clave.lower()] not in (None, ""):
            return lower[clave.lower()]
    return default


def _qty_plano(fila):
    raw = _celda_plano(fila, ("qty", "quantity", "cantidad", "count", "no", "number", "q"))
    n = _dinero_partida_ia(raw)
    return n if n > 0 else 1.0


def _unidad_plano(fila, clave_tabla=""):
    raw = str(_celda_plano(fila, ("unit", "unidad", "uom"), "")).upper()
    if raw in ("SQ", "SF", "LF", "EA", "PC", "HR", "CY", "BAG", "LS"):
        return raw
    blob = f"{clave_tabla} {_celda_plano(fila, ('type', 'description', 'descripcion', 'remarks'), '')}".lower()
    if re.search(r"door|window|fixture|luminar|light|toilet|sink|vanity|hwc|mark\b", blob):
        return "EA"
    if re.search(r"floor|finish|paint|drywall|tile|sf\b|sq\s*ft", blob):
        return "SF"
    if re.search(r"base|trim|gutter|lf\b|linear", blob):
        return "LF"
    return "EA"


def _accion_plano(clave_tabla, fila):
    blob = f"{clave_tabla} {_celda_plano(fila, ('remarks', 'notes', 'type', 'description'), '')}"
    from roof_parser import accion_exacta_item
    return accion_exacta_item(blob)


def _descripcion_item_plano(clave_tabla, fila):
    mark = str(_celda_plano(fila, ("mark", "code", "codigo", "tag", "symbol"), "")).strip()
    tipo = str(_celda_plano(fila, ("type", "description", "descripcion", "item", "name", "nombre"), "")).strip()
    size = str(_celda_plano(fila, ("size", "width", "height", "dim", "dimensions"), "")).strip()
    w = str(_celda_plano(fila, ("width", "w"), "")).strip()
    h = str(_celda_plano(fila, ("height", "h"), "")).strip()
    if w and h and not size:
        size = f"{w} x {h}"
    material = str(_celda_plano(fila, ("material", "finish", "hardware"), "")).strip()
    loc = str(_celda_plano(fila, ("location", "room", "area"), "")).strip()
    remarks = str(_celda_plano(fila, ("remarks", "notes", "note"), "")).strip()
    partes = []
    titulo = _titulo_tabla_plano(clave_tabla)
    if mark:
        partes.append(f"{titulo} {mark}".strip())
    elif tipo:
        partes.append(tipo)
    else:
        partes.append(titulo)
    extra = []
    if tipo and mark:
        extra.append(tipo)
    if size:
        extra.append(size)
    if material:
        extra.append(material)
    if loc:
        extra.append(loc)
    if remarks:
        extra.append(remarks)
    if extra:
        partes.append(" — ".join(extra))
    return re.sub(r"\s+", " ", " ".join(partes)).strip()[:240]


def _item_desde_fila_plano(clave_tabla, fila, index=1):
    if not isinstance(fila, dict):
        return None
    desc = _descripcion_item_plano(clave_tabla, fila)
    if not desc:
        return None
    qty = _qty_plano(fila)
    unit = _unidad_plano(fila, clave_tabla)
    action = _accion_plano(clave_tabla, fila)
    codigo = str(_celda_plano(fila, ("mark", "code", "codigo", "tag"), "")).strip()
    return {
        "item": index,
        "table_key": clave_tabla,
        "table_title": _titulo_tabla_plano(clave_tabla),
        "codigo": codigo,
        "code": codigo,
        "CODE": codigo,
        "description": desc,
        "descripcion": desc,
        "WORK DESCRIPTION": desc,
        "quantity": qty,
        "qty": qty,
        "QTY": qty,
        "unit": unit,
        "unidad": unit,
        "UNIT": unit,
        "action": action,
        "ACTION": action,
        "unit_price": 0.0,
        "UNIT PRICE": 0.0,
        "subtotal": 0.0,
        "SUBTOTAL": 0.0,
        "source_page": fila.get("source_page") or fila.get("page"),
        "raw": fila,
    }


SKETCH_CAMPOS = (
    "floor_sf",
    "ceiling_sf",
    "wall_sf",
    "wall_and_ceiling_sf",
    "trim_lf",
    "cabinets_lf",
)
SKETCH_ALTURA_PARED = 8.0
SKETCH_LINEAS = (
    ("floor_sf", "Floor", "SF", "install_material"),
    ("ceiling_sf", "Ceiling", "SF", "install_material"),
    ("wall_sf", "Walls", "SF", "install_material"),
    ("trim_lf", "Baseboard / trim", "LF", "install_material"),
    ("cabinets_lf", "Cabinets", "LF", "install_material"),
)


def _num_sketch(valor):
    n = _dinero_partida_ia(valor)
    if n < 0 or not math.isfinite(n):
        return 0.0
    return round(n, 2)


def _alias_cuarto(fila, *claves):
    if not isinstance(fila, dict):
        return None
    lower = {str(k).lower(): v for k, v in fila.items()}
    for clave in claves:
        if clave.lower() in lower and lower[clave.lower()] not in (None, ""):
            return lower[clave.lower()]
    return None


def _normalizar_cuarto(fila):
    if not isinstance(fila, dict):
        return None
    nombre = str(
        _alias_cuarto(fila, "room_name", "name", "room", "area", "area_name", "title") or ""
    ).strip()
    if not nombre:
        return None
    floor = _num_sketch(_alias_cuarto(fila, "floor_sf", "floor", "sf", "area_sf", "sqft"))
    ceiling = _num_sketch(_alias_cuarto(fila, "ceiling_sf", "ceiling"))
    wall = _num_sketch(_alias_cuarto(fila, "wall_sf", "walls_sf", "walls"))
    trim = _num_sketch(_alias_cuarto(fila, "trim_lf", "perimeter_lf", "baseboard_lf", "perimeter"))
    cabinets = _num_sketch(_alias_cuarto(fila, "cabinets_lf", "cabinet_lf", "millwork_lf"))
    wac = _num_sketch(_alias_cuarto(fila, "wall_and_ceiling_sf", "walls_and_ceiling_sf"))
    if ceiling <= 0:
        ceiling = floor
    if wall <= 0 and trim > 0:
        wall = round(trim * SKETCH_ALTURA_PARED, 2)
    if trim <= 0 and wall > 0:
        trim = round(wall / SKETCH_ALTURA_PARED, 2)
    if wac <= 0:
        wac = round(wall + ceiling, 2)
    cocina_bano = bool(re.search(r"kitchen|cocina|bath|baño|bano|laundry|lavadero|bar|island", nombre, re.I))
    if not cocina_bano:
        cabinets = 0.0
    if floor <= 0 and wall <= 0 and trim <= 0 and cabinets <= 0:
        return None
    return {
        "room_name": nombre,
        "floor_sf": floor,
        "ceiling_sf": ceiling,
        "wall_sf": wall,
        "wall_and_ceiling_sf": wac,
        "trim_lf": trim,
        "cabinets_lf": cabinets,
        "source_page": fila.get("source_page") or fila.get("page"),
    }


def _fusionar_cuarto(a, b):
    out = dict(a or {})
    for campo in SKETCH_CAMPOS:
        vb = _num_sketch((b or {}).get(campo))
        if vb > _num_sketch(out.get(campo)):
            out[campo] = vb
    if (b or {}).get("source_page") and not out.get("source_page"):
        out["source_page"] = b.get("source_page")
    return out


def _sumar_grand_totals(rooms):
    totales = {campo: 0.0 for campo in SKETCH_CAMPOS}
    for room in rooms or []:
        for campo in SKETCH_CAMPOS:
            totales[campo] = round(totales[campo] + _num_sketch(room.get(campo)), 2)
    return totales


def _items_desde_cuarto(room, index_inicio=1):
    nombre = str((room or {}).get("room_name") or "Room").strip()
    items = []
    n = index_inicio
    for campo, etiqueta, unidad, accion in SKETCH_LINEAS:
        qty = _num_sketch(room.get(campo))
        if qty <= 0:
            continue
        desc = f"{nombre} — {etiqueta}"
        items.append({
            "item": n,
            "table_key": "room",
            "table_title": nombre,
            "room_name": nombre,
            "measure_key": campo,
            "codigo": "",
            "code": "",
            "CODE": "",
            "description": desc,
            "descripcion": desc,
            "WORK DESCRIPTION": desc,
            "quantity": qty,
            "qty": qty,
            "QTY": qty,
            "unit": unidad,
            "unidad": unidad,
            "UNIT": unidad,
            "action": accion,
            "ACTION": accion,
            "unit_price": 0.0,
            "UNIT PRICE": 0.0,
            "subtotal": 0.0,
            "SUBTOTAL": 0.0,
            "source_page": room.get("source_page"),
        })
        n += 1
    return items


def _normalizar_payload_planos(parsed, paginas=0, paginas_total=0):
    bruto = parsed if isinstance(parsed, dict) else {"rooms": parsed if isinstance(parsed, list) else []}
    resumen = bruto.get("project_summary") if isinstance(bruto.get("project_summary"), dict) else {}
    crudos = bruto.get("rooms")
    if not isinstance(crudos, list):
        crudos = bruto.get("areas") or bruto.get("cuartos") or []
    rooms = []
    indice = {}
    for fila in crudos if isinstance(crudos, list) else []:
        cuarto = _normalizar_cuarto(fila)
        if not cuarto:
            continue
        clave = re.sub(r"\s+", " ", cuarto["room_name"]).strip().lower()
        if clave in indice:
            rooms[indice[clave]] = _fusionar_cuarto(rooms[indice[clave]], cuarto)
        else:
            indice[clave] = len(rooms)
            rooms.append(cuarto)
    grand = _sumar_grand_totals(rooms)
    items = []
    for room in rooms:
        room["items"] = _items_desde_cuarto(room, len(items) + 1)
        items.extend(room["items"])
    from roof_parser import aplicar_precios_reales_catalogo
    aplicar_precios_reales_catalogo(items)
    return {
        "success": True,
        "ok": True,
        "document_type": "architectural_plans",
        "project_type": resumen.get("project_type") or "interior_sketch",
        "address": resumen.get("address"),
        "title": resumen.get("title"),
        "sheet_titles": resumen.get("sheet_titles") or [],
        "project_summary": resumen,
        "rooms": rooms,
        "grand_totals": grand,
        "tables": [],
        "notes": [],
        "items": items,
        "pages_analyzed": paginas,
        "pages_total": paginas_total,
        "count": len(items),
    }


_PLANOS_DIR = Path(tempfile.gettempdir()) / "zipnova-planos"
_PLANOS_JOBS_LOCK = threading.Lock()
_PLANOS_MARCAS = (
    r"\bkitchen\b",
    r"\bbedroom\b",
    r"\bbath(?:room)?\b",
    r"\bliving\b",
    r"\bdining\b",
    r"\bfloor\s*plan\b",
    r"\bplanta\b",
    r"\bcocina\b",
    r"\brec[aá]mara\b",
    r"\bba[nñ]o\b",
    r"\bescala\b",
    r"\bscale\b",
    r"\blaundry\b",
    r"\bgarage\b",
    r"\bcloset\b",
)


def _respuesta_json(cuerpo, codigo=200):
    resp = jsonify(cuerpo)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp, codigo


def _indices_laminas_medidas(pdf_bytes, max_paginas):
    """Elige las láminas que parecen planta. Un PDF escaneado cae en las primeras páginas."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = int(doc.page_count or 0)
        if total <= 0:
            raise RuntimeError("El PDF no tiene páginas")
        puntos = []
        for i in range(total):
            try:
                texto = doc.load_page(i).get_text("text") or ""
            except Exception:
                texto = ""
            bajo = texto.lower()
            score = sum(2 for marca in _PLANOS_MARCAS if re.search(marca, bajo))
            if re.search(r"\d+\s*['′]", texto):
                score += 1
            puntos.append((score, i))
        if not any(score for score, _ in puntos):
            indices = list(range(min(int(max_paginas), total)))
        else:
            mejores = sorted(puntos, key=lambda fila: (-fila[0], fila[1]))[: int(max_paginas)]
            indices = sorted(i for score, i in mejores if score > 0)
            if not indices:
                indices = list(range(min(int(max_paginas), total)))
        return indices, total
    finally:
        doc.close()


def _analizar_plano_vision(pdf_bytes, on_progress=None):
    from claude_pdf import MODELO_CLAUDE, analizar_imagenes_json, pdf_paginas_a_imagenes

    indices, total = _indices_laminas_medidas(pdf_bytes, PLANOS_MAX_PAGINAS)
    rooms = []
    errores = []
    for n, indice in enumerate(indices, start=1):
        numero = indice + 1
        if on_progress:
            on_progress(f"Leyendo lámina {n} de {len(indices)} (página {numero} de {total})")
        parsed = None
        imagenes = None
        try:
            imagenes = pdf_paginas_a_imagenes(
                pdf_bytes,
                dpi=PLANOS_DPI,
                max_paginas=1,
                pagina=numero,
                max_lado=1200,
                max_bytes=700_000,
            )
            prompt = (
                PROMPT_PLANOS_VISION
                + f"\nEsta imagen es solo la página {numero} de {total}. "
                + f"Pon source_page={numero} en cada cuarto. "
                + "Si no es una planta, rooms=[]."
            )
            parsed = analizar_imagenes_json(
                imagenes,
                prompt,
                system=SYSTEM_PROMPT_PLANOS,
                max_tokens=4096,
                modelo=MODELO_CLAUDE,
                modelos=(MODELO_CLAUDE,),
                timeout=50.0,
            )
        except Exception as err:
            errores.append(f"página {numero}: {err}")
            print(f"--> plano página {numero} omitida: {err}")
        imagenes = None
        if parsed is None:
            continue
        combinado = parsed if isinstance(parsed, dict) else {"rooms": parsed if isinstance(parsed, list) else []}
        crudos = combinado.get("rooms") if isinstance(combinado, dict) else None
        if not isinstance(crudos, list):
            crudos = combinado if isinstance(combinado, list) else []
        for room in crudos:
            if isinstance(room, dict):
                room.setdefault("source_page", numero)
                rooms.append(room)
    if not rooms and errores:
        raise RuntimeError(errores[-1])
    return _normalizar_payload_planos({"rooms": rooms}, paginas=len(indices), paginas_total=total)


def _planos_ruta(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(job_id or "")):
        return None
    return _PLANOS_DIR / f"{job_id}.json"


def _planos_leer(job_id):
    ruta = _planos_ruta(job_id)
    if ruta is None or not ruta.is_file():
        return None
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _planos_escribir(job_id, job):
    ruta = _planos_ruta(job_id)
    if ruta is None:
        return
    _PLANOS_DIR.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    temporal.replace(ruta)


def _planos_guardar(job_id, **cambios):
    with _PLANOS_JOBS_LOCK:
        job = _planos_leer(job_id)
        if not job:
            return
        job.update(cambios)
        _planos_escribir(job_id, job)


def _planos_limpiar_jobs():
    if not _PLANOS_DIR.is_dir():
        return
    ahora = time.time()
    for ruta in _PLANOS_DIR.glob("*.json"):
        try:
            if ahora - ruta.stat().st_mtime > 1800:
                ruta.unlink()
        except OSError:
            pass


def _planos_job_worker(job_id, pdf_bytes):
    try:
        payload = _analizar_plano_vision(
            pdf_bytes,
            on_progress=lambda mensaje: _planos_guardar(job_id, progress=mensaje),
        )
        if not payload.get("rooms"):
            _planos_guardar(
                job_id,
                status="error",
                error="No se identificaron cuartos ni áreas en estas láminas.",
                progress="",
            )
            return
        print("========== HTTP /api/import-planos ==========")
        print(json.dumps({
            "rooms": [r.get("room_name") for r in payload.get("rooms") or []],
            "grand_totals": payload.get("grand_totals"),
            "items": len(payload.get("items") or []),
            "pages_analyzed": payload.get("pages_analyzed"),
            "pages_total": payload.get("pages_total"),
        }, ensure_ascii=False)[:2000])
        _planos_guardar(job_id, status="done", result=payload, progress="Medidas listas", error=None)
    except Exception as err:
        import traceback
        traceback.print_exc()
        _planos_guardar(
            job_id,
            status="error",
            error=f"No se pudieron leer los planos: {err}",
            progress="",
        )


@app.route("/api/import-planos", methods=["POST"])
@app.route("/api/process-architectural-plans", methods=["POST"])
def import_planos_vision():
    archivo = request.files.get("file") or request.files.get("pdf")
    if archivo is None:
        return _respuesta_json({"success": False, "error": "No se encontró el archivo PDF del plano"}, 400)
    nombre = str(getattr(archivo, "filename", "") or "")
    if nombre and not nombre.lower().endswith(".pdf"):
        return _respuesta_json({"success": False, "error": "Sube un PDF de planos arquitectónicos"}, 400)
    pdf_bytes = archivo.read()
    if not pdf_bytes:
        return _respuesta_json({"success": False, "error": "El PDF está vacío"}, 400)
    bloqueo = _consumir_extraccion_ia_pdf()
    if bloqueo:
        return bloqueo
    _planos_limpiar_jobs()
    job_id = uuid.uuid4().hex
    _planos_escribir(job_id, {
        "status": "processing",
        "progress": "Preparando las láminas…",
        "created": time.time(),
        "result": None,
        "error": None,
    })
    threading.Thread(
        target=_planos_job_worker,
        args=(job_id, pdf_bytes),
        daemon=True,
        name=f"planos-{job_id[:8]}",
    ).start()
    return _respuesta_json({"success": True, "status": "processing", "job_id": job_id}, 202)


@app.route("/api/import-planos/status/<job_id>", methods=["GET"])
@app.route("/api/process-architectural-plans/status/<job_id>", methods=["GET"])
def import_planos_status(job_id):
    copia = _planos_leer(job_id)
    if not copia:
        return _respuesta_json({
            "success": False,
            "status": "error",
            "error": "Esa lectura ya no está. Vuelve a subir el PDF.",
        })
    cuerpo = {
        "success": copia.get("status") != "error",
        "status": copia.get("status"),
        "progress": copia.get("progress") or "",
        "error": copia.get("error"),
    }
    if copia.get("status") == "done":
        cuerpo["result"] = copia.get("result")
    return _respuesta_json(cuerpo)


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


def _html_marca_blanca(html, nombre_empresa=""):
    texto = str(html or "")
    texto = re.sub(r"Zipnova", "", texto, flags=re.I)
    marca = re.sub(r"[\r\n]+", " ", str(nombre_empresa or "")).strip()
    if marca:
        texto = re.sub(r"Solid Remodeling(?:\s*(?:&amp;|&)\s*Reconstruction\s*LLC)?", marca, texto, flags=re.I)
    return texto


def _pdf_desde_solicitud(data):
    html = str((data or {}).get("html") or "").strip()
    if html:
        nombre = str((data or {}).get("nombre_empresa") or (data or {}).get("company") or "").strip()
        html = _html_marca_blanca(_traducir_servicio_en_html_pdf(html), nombre)
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


def _enviar_correo_alerta_admin(payload):
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formataddr
    payload = payload or {}
    destino = (os.getenv("ALERT_EMAIL") or _ALERTA_ADMIN_EMAIL).strip()
    if not _correo_valido(destino):
        destino = _ALERTA_ADMIN_EMAIL
    variable = payload.get("variable") or "(ninguna)"
    conexion = payload.get("conexion") or "(desconocida)"
    motivo = payload.get("motivo") or "desconocido"
    faltantes = ", ".join(payload.get("faltantes") or []) or "(ninguna)"
    presentes = ", ".join(payload.get("presentes") or []) or "(ninguna)"
    detalle = str(payload.get("detalle") or "").strip() or "(sin detalle)"
    ruta = payload.get("ruta") or "(sin ruta)"
    cuando = payload.get("cuando") or datetime.now(timezone.utc).isoformat()
    texto = (
        "ALERTA CRITICA QuantixBuilds\n\n"
        f"Motivo: {motivo}\n"
        f"Variable: {variable}\n"
        f"Conexion: {conexion}\n"
        f"Ruta: {ruta}\n"
        f"Cuando (UTC): {cuando}\n"
        f"Variables faltantes: {faltantes}\n"
        f"Variables presentes: {presentes}\n\n"
        f"Detalle:\n{detalle}\n"
    )
    html = f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#0f172a;">
<h2 style="color:#b45309;">ALERTA CRITICA QuantixBuilds</h2>
<p>Un usuario no pudo completar el acceso por un fallo interno. El cliente vio un mensaje amable; este correo es solo interno.</p>
<ul>
<li><b>Motivo:</b> {xml_escape(str(motivo))}</li>
<li><b>Variable:</b> {xml_escape(str(variable))}</li>
<li><b>Conexion:</b> {xml_escape(str(conexion))}</li>
<li><b>Ruta:</b> {xml_escape(str(ruta))}</li>
<li><b>Cuando (UTC):</b> {xml_escape(str(cuando))}</li>
<li><b>Variables faltantes:</b> {xml_escape(str(faltantes))}</li>
<li><b>Variables presentes:</b> {xml_escape(str(presentes))}</li>
</ul>
<pre style="background:#f8fafc;padding:12px;border-radius:8px;white-space:pre-wrap;">{xml_escape(str(detalle))}</pre>
</body></html>"""
    _, _, _, _, smtp_from = _credenciales_smtp()
    mensaje = MIMEMultipart("alternative")
    mensaje["Subject"] = f"ALERTA CRITICA QuantixBuilds: {motivo} / {variable}"
    mensaje["From"] = formataddr(("QuantixBuilds Alertas", smtp_from))
    mensaje["To"] = destino
    mensaje.attach(MIMEText(texto, "plain", "utf-8"))
    mensaje.attach(MIMEText(html, "html", "utf-8"))
    _despachar_smtp(mensaje, destino)


def _revisar_entorno_critico():
    if not _FALTA_SERVICE_ROLE and not _ERROR_ARRANQUE_SUPABASE:
        _vaciar_alertas_pendientes()
        return
    detalle = ""
    if _FALTA_SERVICE_ROLE:
        detalle = "Falta la variable SUPABASE_SERVICE_ROLE_KEY en el entorno"
    if _ERROR_ARRANQUE_SUPABASE:
        detalle = (detalle + " | " if detalle else "") + _ERROR_ARRANQUE_SUPABASE
    _alerta_critica(
        "arranque",
        detalle,
        variable="SUPABASE_SERVICE_ROLE_KEY" if _FALTA_SERVICE_ROLE else "",
        conexion="cliente_inicial" if _ERROR_ARRANQUE_SUPABASE else "supabase",
        etapa="arranque",
    )
    _vaciar_alertas_pendientes()


_revisar_entorno_critico()


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
        f"{_FAVICON_HEAD}"
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
        if _error_sin_funcion_tenant(err):
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
        if _error_sin_funcion_tenant(err):
            _aviso_multitenant(err)
            pagina = _html_aceptacion("No se pudo abrir el contrato", _SQL_MULTITENANT)
        else:
            pagina = _html_aceptacion("No se pudo abrir el contrato", "Inténtalo de nuevo en unos minutos.")
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


def _precalentar_busquedas():
    try:
        _precalentar_openai()
    except Exception as err:
        print(f"--> OpenAI keep-alive: {err}")
    try:
        from oficios import todas_filas_catalogo
        print(f"--> Catálogo listo en memoria: {len(todas_filas_catalogo() or [])} ítems")
    except Exception as err:
        print(f"--> Catálogo en memoria: {err}")
    try:
        _filas_factores_regionales()
    except Exception as err:
        print(f"--> Factores regionales: {err}")
    try:
        _motor_uszipcode()
    except Exception as err:
        print(f"--> uszipcode: {err}")


threading.Thread(target=_precalentar_busquedas, daemon=True, name="catalogo-cache").start()

_EMBEDDINGS_AUTO = (os.getenv("EMBEDDINGS_AUTO") or "1").strip().lower() not in ("0", "false", "no", "off")
_RECARGADOR_PADRE = __name__ == "__main__" and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
if _EMBEDDINGS_AUTO and not _RECARGADOR_PADRE:
    threading.Thread(target=_embeddings_automaticos, daemon=True, name="catalogo-embeddings").start()


if __name__ == "__main__":
    app.run(port=5000, debug=True)
