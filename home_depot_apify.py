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
MAX_RESULTADOS = 5

_LABOR = re.compile(
    r"\b("
    r"installation(?:\s+of)?|install(?:ing|ation)?(?:\s+only)?|"
    r"instalaci[oó]n(?:\s+de)?|intalaci[oó]n(?:\s+de)?|"
    r"reemplazo(?:\s+de)?|replacement(?:\s+of)?|replace(?:ment|ing)?|"
    r"repair(?:ing|s)?(?:\s+of)?|reparaci[oó]n(?:\s+de)?|"
    r"tear-?off(?:\s+(?:and\s+replace|de))?|retiro(?:\s+de)?|"
    r"demolici[oó]n|demolition|remove(?:al)?|"
    r"labor(?:\s+only)?|mano\s+de\s+obra|"
    r"solo\s+(?:instalar|mano\s+de\s+obra)|"
    r"supply\s+and\s+install|suministro(?:\s+e?\s*instalaci[oó]n)?|"
    r"instalar(?:\s+m[aá]s\s+materiales)?"
    r")\b",
    re.I,
)
_CATEGORIA_PREFIJO = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 /]{2,40}\s+[—\-–]\s+")
_ALIAS_EN = (
    (r"\bsiding de vinilo\b", "vinyl siding"),
    (r"\bsiding de fibrocemento\b", "fiber cement siding"),
    (r"\bpiso de vinilo de lujo|\bpiso de vinilo\b", "luxury vinyl plank"),
    (r"\bpiso de madera|\bpiso de roble\b", "hardwood flooring"),
    (r"\bvinilo\b", "vinyl"),
    (r"\bfibrocemento\b", "fiber cement"),
    (r"\bmadera tratada\b", "pressure treated lumber"),
    (r"\bteja asf[aá]ltica\b", "architectural shingles"),
    (r"\best[aá]ndar residencial\b", ""),
    (r"\bsistema clic\b", "click lock"),
    (r"\br[ií]gido\b", "rigid"),
    (r"\bmembrana\b", "underlayment"),
)
_PREFIJO_OBRA = re.compile(
    r"^(remover,\s*instalar m[aá]s materiales:?|instalar m[aá]s materiales:?|"
    r"remover e instalar|retiro\s*/\s*demolici[oó]n(?:\s*de)?|"
    r"solo demolici[oó]n(?:\s*/\s*remover)?|intalaci[oó]n de|"
    r"instalaci[oó]n de|instalacion de|solo instalar|retiro de|"
    r"tear-?off(?:\s+de)?|suministro de material|material only|"
    r"labor only|solo mano de obra|suministro de)\s+",
    re.I,
)
_MEDIDA = re.compile(
    r"\d+\s*-\s*\d+\s*/\s*\d+\s*(?:in(?:ch(?:es)?)?|\"|mm|cm)?|"
    r"\d+\s*/\s*\d+\s*(?:in(?:ch(?:es)?)?|\"|mc|awg)?|"
    r"\d+(?:\.\d+)?\s*(?:in(?:ch(?:es)?)?|\"|mm|cm|ft|ga|gauge)|"
    r"\d+\s*/\s*\d+\s*mc|"
    r"\d+\s*x\s*\d+(?:\s*x\s*\d+)?|"
    r"#?\d+\s*(?:gauge|ga)\b",
    re.I,
)


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


def _num(valor):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return numero if numero > 0 else 0.0


def dinero_usd(valor, campo=""):
    """Convierte centavos a dólares solo si el nombre del campo lo documenta."""
    from proveedores_precios import dinero_documentado
    return dinero_documentado(valor, campo)


def sanitizar_url(raw, *, imagen=False):
    texto = str(raw or "").strip()
    if texto.startswith("//"):
        texto = "https:" + texto
    if not texto.lower().startswith(("http://", "https://")):
        return ""
    if imagen and re.search(r"homedepot\.com/p/", texto, re.I):
        if not re.search(r"\.(jpe?g|png|webp|gif)(?:\?|$)", texto, re.I):
            return ""
    return texto


def _urls_desde_valor(valor, *, imagen=False):
    halladas = []
    if valor is None or valor == "":
        return halladas
    if isinstance(valor, str):
        url = sanitizar_url(valor, imagen=imagen)
        if url:
            halladas.append(url)
        return halladas
    if isinstance(valor, (list, tuple)):
        for item in valor:
            halladas.extend(_urls_desde_valor(item, imagen=imagen))
        return halladas
    if isinstance(valor, dict):
        for key in (
            "imageUrl",
            "image_url",
            "url",
            "src",
            "href",
            "thumbnail",
            "thumbnailUrl",
            "primary",
        ):
            if valor.get(key):
                halladas.extend(_urls_desde_valor(valor.get(key), imagen=imagen))
        for key in ("images", "media", "photos"):
            if valor.get(key):
                halladas.extend(_urls_desde_valor(valor.get(key), imagen=imagen))
    return halladas


def url_imagen_home_depot(item):
    if not isinstance(item, dict):
        return ""
    for key in (
        "imageUrl",
        "image_url",
        "image",
        "thumbnail",
        "thumbnailUrl",
        "primaryImage",
        "images",
        "media",
        "photos",
    ):
        for url in _urls_desde_valor(item.get(key), imagen=True):
            return url
    return ""


def url_producto_home_depot(item):
    if not isinstance(item, dict):
        return ""
    for key in ("url", "productUrl", "product_url", "itemUrl", "link", "canonicalUrl"):
        url = sanitizar_url(item.get(key), imagen=False)
        if url:
            return url
    pid = str(item.get("productId") or item.get("parentId") or "").strip()
    if pid.isdigit():
        return "https://www.homedepot.com/p/" + pid
    return ""


def descripcion_material(row):
    if not isinstance(row, dict):
        return ""
    textos = row.get("descripciones") if isinstance(row.get("descripciones"), dict) else {}
    return str(
        textos.get("en")
        or row.get("descripcion_en")
        or row.get("descripcion")
        or row.get("desc")
        or row.get("nombre")
        or textos.get("es")
        or ""
    ).strip()


def extraer_especificaciones(texto):
    return {re.sub(r"\s+", "", m.group(0).lower()) for m in _MEDIDA.finditer(str(texto or ""))}


def extraer_consulta_material(row):
    texto = descripcion_material(row)
    texto = re.sub(r"\s*[—\-]\s*(per square|por escuadra).*$", "", texto, flags=re.I)
    texto = _CATEGORIA_PREFIJO.sub("", texto).strip()
    for _ in range(4):
        nuevo = _PREFIJO_OBRA.sub("", texto).strip()
        if nuevo == texto:
            break
        texto = nuevo
    texto = _LABOR.sub(" ", texto)
    texto = re.sub(r"^(?:and|e|y|de|of)\s+", "", texto, flags=re.I)
    for patron, dest in _ALIAS_EN:
        texto = re.sub(patron, dest, texto, flags=re.I)
    texto = re.sub(r"\s+", " ", texto).strip(" ,.-")
    return texto[:90]


def consulta_home_depot(row):
    return extraer_consulta_material(row)


def precio_stale(row, dias=DIAS_FRESCURA):
    return necesita_consulta(row, dias=dias)


def zip_consulta(valor, fallback=""):
    from proveedores_precios import zip_obra
    return zip_obra(valor, fallback)


def necesita_consulta(row, dias=DIAS_FRESCURA):
    if not isinstance(row, dict):
        return True
    foto = sanitizar_url(
        row.get("image_url") or row.get("imageUrl") or "",
        imagen=True,
    )
    producto = sanitizar_url(row.get("product_url") or row.get("productUrl") or "", imagen=False)
    fecha = parsear_fecha(row.get("last_updated"))
    fresca = fecha is not None and (_ahora_utc() - fecha) <= timedelta(days=int(dias or DIAS_FRESCURA))
    if (foto or producto) and fresca:
        return False
    return True


def unidad_catalogo(row):
    return str((row or {}).get("unidad") or (row or {}).get("unit") or "SF").upper()


def _blob_producto(item):
    if not isinstance(item, dict):
        return ""
    return " ".join(
        str(item.get(key) or "")
        for key in ("name", "title", "unitMeasure", "unit_measure", "unitOfMeasure", "category")
    ).lower()


def unidad_producto(item):
    blob = _blob_producto(item)
    if re.search(r"sq\.?\s*ft|square\s*foot|pie cuadrado", blob):
        return "SF"
    if re.search(r"\bsquare\b|escuadra", blob) and not re.search(r"sq\.?\s*ft", blob):
        return "SQ"
    if re.search(r"lin(?:ear)?\.?\s*ft|pie lineal", blob):
        return "LF"
    if re.search(r"\bgallon|galón|galon\b", blob):
        return "GAL"
    if re.search(r"\bbag|saco|bulto\b", blob):
        return "BAG"
    if re.search(r"\beach\b|\bpiece\b|\bbox\b|\bkit\b|unidad", blob):
        return "EA"
    return ""


def presentacion_de(texto):
    t = str(texto or "").lower()
    if re.search(r"\bbag|saco|bulto\b", t):
        return "bag"
    if re.search(r"\bbox|caja|case\b", t):
        return "box"
    if re.search(r"\bsheet|hoja\b", t):
        return "sheet"
    if re.search(r"\bplank|tabla|tablón|tablon\b", t):
        return "plank"
    if re.search(r"\broll|rollo\b", t):
        return "roll"
    if re.search(r"\bgallon|galón|galon\b", t):
        return "gallon"
    if re.search(r"\bbundle|paquete|bulto\b", t):
        return "bundle"
    if re.search(r"\bbucket|cubeta\b", t):
        return "bucket"
    return ""


def presentacion_catalogo(row):
    blob = descripcion_material(row) + " " + unidad_catalogo(row)
    hallada = presentacion_de(blob)
    if hallada:
        return hallada
    unidad = unidad_catalogo(row)
    if unidad in ("SF", "SQ"):
        return "coverage"
    if unidad == "LF":
        return "linear"
    return "each"


def presentacion_producto(item):
    hallada = presentacion_de(_blob_producto(item))
    if hallada:
        return hallada
    try:
        cajas = float(item.get("unitsPerCase") or 0)
    except (TypeError, ValueError):
        cajas = 0.0
    if cajas > 1:
        return "box"
    unidad = unidad_producto(item)
    if unidad == "SF":
        return "coverage"
    if unidad == "LF":
        return "linear"
    return "each"


def unidades_compatibles(row, item):
    unidad = unidad_catalogo(row)
    hd = unidad_producto(item)
    unitario = 0.0
    for key in ("unitPrice", "unit_price"):
        unitario = dinero_usd((item or {}).get(key), key)
        if unitario:
            break
    medida = str((item or {}).get("unitMeasure") or (item or {}).get("unit_measure") or "").lower()
    if unidad in ("SF", "SQ"):
        return bool(unitario > 0 and re.search(r"sq\.?\s*ft|square\s*foot|pie cuadrado", medida))
    if unidad == "LF":
        return bool(unitario > 0 and re.search(r"lin(?:ear)?\.?\s*ft|pie lineal", medida or _blob_producto(item)))
    if unidad in ("EA", "PC", "BAG", "GAL"):
        return hd in ("", "EA", "BAG", "GAL") or unidad == hd
    return hd in ("", unidad)


def presentaciones_compatibles(row, item):
    cat = presentacion_catalogo(row)
    prod = presentacion_producto(item)
    if cat == prod:
        return True
    if cat == "coverage" and prod in ("coverage", "plank", "sheet", "roll", "box", "bundle"):
        return unidades_compatibles(row, item)
    if cat == "each" and prod in ("each", "box", "bag", "gallon", "bundle", "bucket"):
        return True
    if cat in ("box", "bag", "gallon") and prod in (cat, "each"):
        return True
    return False


def producto_compatible(row, item):
    if not isinstance(item, dict):
        return False
    if not unidades_compatibles(row, item):
        return False
    if not presentaciones_compatibles(row, item):
        return False
    specs = extraer_especificaciones(extraer_consulta_material(row))
    if specs:
        halladas = extraer_especificaciones(_blob_producto(item))
        if halladas and not (specs & halladas):
            return False
    return True


def verificar_tienda(item, zip5):
    if not isinstance(item, dict):
        return False
    pedido = str(zip5 or "")
    if not re.fullmatch(r"\d{5}", pedido):
        return False
    for key in ("zipCode", "zip_code", "storeZip", "store_zip", "postalCode"):
        crudo = str(item.get(key) or "").strip()
        if re.fullmatch(r"\d{5}", crudo) and crudo != pedido:
            return False
    return bool(str(item.get("storeId") or item.get("storeName") or "").strip())


def precio_original_home_depot(item):
    if not isinstance(item, dict):
        return None, ""
    for key in ("price", "clearancePrice", "regularPrice"):
        valor = dinero_usd(item.get(key), key)
        if valor > 0:
            return valor, key
    for key in ("priceInCents", "price_cents"):
        valor = dinero_usd(item.get(key), key)
        if valor > 0:
            return valor, key
    return None, ""


def precio_desde_home_depot(item, unidad=""):
    if not isinstance(item, dict):
        return None
    from proveedores_precios import cobertura_sf_explicita
    unidad = str(unidad or "").upper()
    unitario = 0.0
    for key in ("unitPrice", "unit_price"):
        unitario = dinero_usd(item.get(key), key)
        if unitario:
            break
    medida = str(item.get("unitMeasure") or item.get("unit_measure") or "").lower()
    precio, _campo = precio_original_home_depot(item)
    if unidad in ("SF", "") and unitario > 0 and re.search(r"sq\.?\s*ft|square\s*foot|pie cuadrado", medida):
        return round(unitario, 2)
    if unidad == "SQ" and unitario > 0 and re.search(r"sq\.?\s*ft|square\s*foot|pie cuadrado", medida):
        return round(unitario * 100, 2)
    if unidad == "LF" and unitario > 0 and re.search(r"lin(?:ear)?\.?\s*ft|pie lineal", medida or _blob_producto(item)):
        return round(unitario, 2)
    cobertura = cobertura_sf_explicita(item)
    if unidad in ("SF", "") and cobertura and precio and precio > 0:
        return round(precio / cobertura, 2)
    if unidad == "SQ" and cobertura and precio and precio > 0:
        return round((precio / cobertura) * 100, 2)
    if unidad in ("EA", "PC", "BAG", "GAL") and precio and precio > 0:
        return round(precio, 2)
    return None


def producto_publico(item, unidad=""):
    if not isinstance(item, dict):
        return None
    nombre = str(item.get("name") or item.get("title") or "").strip()
    foto = url_imagen_home_depot(item)
    enlace = url_producto_home_depot(item)
    if not (nombre or foto or enlace):
        return None
    from proveedores_precios import cobertura_sf_explicita
    original, campo = precio_original_home_depot(item)
    return {
        "nombre": nombre,
        "image_url": foto,
        "imageUrl": foto,
        "product_url": enlace,
        "productUrl": enlace,
        "precio_material": precio_desde_home_depot(item, unidad) or 0,
        "precio_original": original,
        "unidad_original": str(item.get("unitMeasure") or item.get("unit_measure") or campo or "").strip(),
        "unidad": unidad_producto(item),
        "presentacion": presentacion_producto(item),
        "cobertura_sf": cobertura_sf_explicita(item),
        "moneda": "USD",
        "store_id": str(item.get("storeId") or "").strip(),
        "store_name": str(item.get("storeName") or "").strip(),
        "proveedor": "Home Depot",
        "sku": str(item.get("productId") or item.get("storeSku") or item.get("upc") or "").strip(),
        "producto_id": str(item.get("productId") or item.get("storeSku") or "").strip(),
    }


def _tokens(texto):
    return set(re.findall(r"[a-z0-9]{3,}", str(texto or "").lower()))


def parecido(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta))


def _score_producto(consulta, item, row=None):
    nombre = " ".join(
        str(item.get(key) or "")
        for key in ("name", "title", "brand", "modelNumber", "category")
    )
    score = parecido(consulta, nombre)
    specs = extraer_especificaciones(consulta)
    if specs:
        halladas = extraer_especificaciones(nombre)
        if specs & halladas:
            score += 0.18
        elif halladas:
            score -= 0.08
    if row and producto_compatible(row, item):
        score += 0.12
    return score


def analizar_coincidencias(row, productos):
    consulta = extraer_consulta_material(row)
    unidad = unidad_catalogo(row)
    ranked = []
    for item in productos or []:
        if not isinstance(item, dict):
            continue
        if not producto_compatible(row, item):
            continue
        zip_row = str((row or {}).get("zip_consulta") or "")
        if zip_row and not verificar_tienda(item, zip_row):
            continue
        ranked.append((_score_producto(consulta, item, row), item))
    ranked.sort(key=lambda par: par[0], reverse=True)
    candidatos = []
    for score, item in ranked[:3]:
        publico = producto_publico(item, unidad)
        if publico:
            publico["score"] = round(score, 3)
            candidatos.append(publico)
    if not ranked or ranked[0][0] < 0.32:
        return {"status": "none", "match": None, "score": 0.0, "candidatos": candidatos}
    mejor, segundo = ranked[0][0], ranked[1][0] if len(ranked) > 1 else 0.0
    if segundo and (mejor - segundo) < 0.12:
        return {"status": "ambiguous", "match": None, "score": mejor, "candidatos": candidatos}
    if precio_desde_home_depot(ranked[0][1], unidad) is None:
        return {"status": "ambiguous", "match": None, "score": mejor, "candidatos": candidatos}
    return {"status": "confident", "match": ranked[0][1], "score": mejor, "candidatos": []}


def emparejar_producto(row, productos):
    info = analizar_coincidencias(row, productos)
    return info["match"] if info["status"] == "confident" else None


def _actor_path(actor):
    return str(actor or ACTOR_DEFAULT).strip().replace("/", "~")


def scrapear_home_depot(terminos, zip_code):
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
    zip5 = zip_consulta(zip_code)
    if len(zip5) != 5:
        raise RuntimeError("Falta el ZIP del contratista para consultar la tienda de Home Depot")
    token = token_apify()
    if not token:
        raise RuntimeError("Falta APIFY_API_TOKEN en .env")
    cuerpo = {
        "searchTerms": busquedas,
        "zipCode": zip5,
        "maxResults": MAX_RESULTADOS,
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
    productos = data if isinstance(data, list) else []
    return [item for item in productos if verificar_tienda(item, zip5)] if productos else []


try:
    from proveedores_precios import registrar_proveedor
    registrar_proveedor("Home Depot", scrapear_home_depot)
except Exception:
    pass
