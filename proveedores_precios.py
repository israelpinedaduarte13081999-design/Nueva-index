"""Caché pública de precios de proveedores, compartida entre contratistas e instancias."""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone

DIAS_PRECIO = 15
DIAS_IMAGEN = 60
LOCK_SEGUNDOS = 100
ESPERA_LOCK_SEG = 90

TABLA_PRECIOS = "catalogo_proveedor_precios"
TABLA_LOCKS = "catalogo_proveedor_locks"

CAMPOS_CENTAVOS = frozenset({
    "priceincents",
    "price_cents",
    "pricecents",
    "salepricecents",
    "regularpricecents",
    "clearancepricecents",
})


def ahora_utc():
    return datetime.now(timezone.utc)


def parsear_fecha(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    texto = str(valor).strip().replace("Z", "+00:00")
    if not texto:
        return None
    try:
        fecha = datetime.fromisoformat(texto)
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def zip_obra(*valores):
    """ZIP de 5 dígitos con ceros a la izquierda. Sin valor por defecto."""
    for valor in valores:
        if valor is None:
            continue
        texto = str(valor).strip()
        if not texto:
            continue
        if not re.fullmatch(r"\d{5}", texto):
            solo = re.sub(r"\D", "", texto)
            if len(solo) == 5:
                texto = solo
            elif len(solo) > 5 and re.fullmatch(r"\d{5}\d*", solo):
                texto = solo[:5]
            else:
                continue
        if re.fullmatch(r"\d{5}", texto):
            return texto
    return ""


def clave_consulta(texto):
    return re.sub(r"\s+", " ", str(texto or "").strip().lower())[:90]


def clave_lock(proveedor, zip5, terminos):
    cuerpo = "|".join([str(proveedor or "").lower(), zip5, *sorted(clave_consulta(t) for t in terminos if t)])
    return hashlib.sha256(cuerpo.encode("utf-8")).hexdigest()[:48]


def dinero_documentado(valor, campo=""):
    """Convierte a dólares solo si el nombre del campo documenta centavos."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    if numero <= 0:
        return 0.0
    nombre = re.sub(r"[^a-z0-9_]", "", str(campo or "").lower())
    if nombre in CAMPOS_CENTAVOS or nombre.endswith("cents"):
        return round(numero / 100.0, 2)
    return round(numero, 2)


def precio_fresco(row, dias=DIAS_PRECIO):
    if not isinstance(row, dict):
        return False
    try:
        precio = float(row.get("precio_normalizado") or 0)
    except (TypeError, ValueError):
        precio = 0.0
    if precio <= 0:
        return False
    fecha = parsear_fecha(row.get("last_updated"))
    if fecha is None:
        return False
    return (ahora_utc() - fecha) <= timedelta(days=int(dias or DIAS_PRECIO))


def imagen_fresca(row, dias=DIAS_IMAGEN):
    if not isinstance(row, dict):
        return False
    url = str(row.get("image_url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return False
    fecha = parsear_fecha(row.get("image_updated") or row.get("last_updated"))
    if fecha is None:
        return False
    return (ahora_utc() - fecha) <= timedelta(days=int(dias or DIAS_IMAGEN))


class TiendaPrecios:
    """Acceso a las tablas públicas. Si faltan, last_error lo indica."""

    def __init__(self, client=None):
        self.client = client
        self.last_error = ""
        self.disponible = client is not None

    def _ok(self):
        return self.client is not None and self.disponible

    def _fallo_esquema(self, err):
        texto = str(err or "")
        if "PGRST205" in texto or "does not exist" in texto or "42P01" in texto:
            self.disponible = False
            self.last_error = "Ejecuta catalogo_proveedores.sql en el editor SQL de Supabase."
            print("-->", self.last_error)
            return True
        return False

    def buscar(self, proveedor, zip5, consulta="", producto_id=""):
        if not self._ok() or not zip5:
            return []
        try:
            q = self.client.table(TABLA_PRECIOS).select("*").eq("proveedor", proveedor).eq("zip_code", zip5)
            if producto_id:
                q = q.eq("producto_id", str(producto_id))
            elif consulta:
                q = q.eq("consulta_clave", clave_consulta(consulta))
            else:
                return []
            data = q.order("last_updated", desc=True).limit(8).execute().data or []
            return [row for row in data if isinstance(row, dict)]
        except Exception as err:
            if not self._fallo_esquema(err):
                print(f"--> proveedor precios buscar: {err}")
            return []

    def fresco(self, proveedor, zip5, consulta="", producto_id=""):
        return [row for row in self.buscar(proveedor, zip5, consulta, producto_id) if precio_fresco(row)]

    def ultimo(self, proveedor, zip5, consulta="", producto_id=""):
        filas = self.buscar(proveedor, zip5, consulta, producto_id)
        return filas[0] if filas else None

    def imagen_producto(self, proveedor, producto_id):
        if not self._ok() or not producto_id:
            return ""
        try:
            data = (
                self.client.table(TABLA_PRECIOS)
                .select("image_url,image_updated,last_updated")
                .eq("proveedor", proveedor)
                .eq("producto_id", str(producto_id))
                .limit(12)
                .execute()
                .data
                or []
            )
        except Exception as err:
            self._fallo_esquema(err)
            return ""
        for row in data:
            if imagen_fresca(row):
                return str(row.get("image_url") or "")
        return ""

    def guardar(self, fila):
        if not self._ok() or not isinstance(fila, dict):
            return False
        zip5 = zip_obra(fila.get("zip_code"))
        precio = fila.get("precio_normalizado")
        try:
            precio_n = float(precio)
        except (TypeError, ValueError):
            precio_n = 0.0
        if not zip5 or precio_n <= 0:
            return False
        payload = {
            "proveedor": str(fila.get("proveedor") or "").strip(),
            "producto_id": str(fila.get("producto_id") or "").strip(),
            "consulta_clave": clave_consulta(fila.get("consulta_clave") or fila.get("consulta") or ""),
            "presentacion": str(fila.get("presentacion") or "").strip(),
            "unidad_original": str(fila.get("unidad_original") or "").strip(),
            "unidad_normalizada": str(fila.get("unidad_normalizada") or "").strip(),
            "zip_code": zip5,
            "tienda_id": str(fila.get("tienda_id") or "").strip(),
            "tienda_nombre": str(fila.get("tienda_nombre") or "").strip() or None,
            "nombre": str(fila.get("nombre") or "").strip() or None,
            "product_url": str(fila.get("product_url") or "").strip() or None,
            "image_url": str(fila.get("image_url") or "").strip() or None,
            "precio_original": fila.get("precio_original"),
            "moneda": str(fila.get("moneda") or "USD").strip() or "USD",
            "precio_normalizado": round(precio_n, 2),
            "cobertura_sf": fila.get("cobertura_sf"),
            "last_updated": (fila.get("last_updated") or ahora_utc().isoformat()),
        }
        if payload["image_url"]:
            payload["image_updated"] = fila.get("image_updated") or payload["last_updated"]
        if not payload["proveedor"]:
            return False
        try:
            self.client.table(TABLA_PRECIOS).upsert(
                payload,
                on_conflict="proveedor,producto_id,presentacion,unidad_normalizada,zip_code,tienda_id",
            ).execute()
            return True
        except Exception as err:
            if not self._fallo_esquema(err):
                print(f"--> proveedor precios guardar: {err}")
            return False

    def adquirir_lock(self, clave):
        if not self._ok() or not clave:
            return False
        ahora = ahora_utc()
        expira = ahora + timedelta(seconds=LOCK_SEGUNDOS)
        try:
            existentes = self.client.table(TABLA_LOCKS).select("*").eq("clave", clave).limit(1).execute().data or []
            if existentes:
                fin = parsear_fecha(existentes[0].get("expires_at"))
                if fin and fin > ahora:
                    return False
                self.client.table(TABLA_LOCKS).delete().eq("clave", clave).execute()
            self.client.table(TABLA_LOCKS).insert({
                "clave": clave,
                "locked_at": ahora.isoformat(),
                "expires_at": expira.isoformat(),
            }).execute()
            return True
        except Exception as err:
            texto = str(err or "")
            if "23505" in texto or "duplicate" in texto.lower():
                return False
            if not self._fallo_esquema(err):
                print(f"--> proveedor lock: {err}")
            return False

    def soltar_lock(self, clave):
        if not self._ok() or not clave:
            return
        try:
            self.client.table(TABLA_LOCKS).delete().eq("clave", clave).execute()
        except Exception as err:
            self._fallo_esquema(err)

    def esperar_precio(self, proveedor, zip5, consulta, timeout=ESPERA_LOCK_SEG):
        fin = time.time() + max(3, int(timeout or ESPERA_LOCK_SEG))
        while time.time() < fin:
            filas = self.fresco(proveedor, zip5, consulta=consulta)
            if filas:
                return filas
            time.sleep(1.2)
        return self.buscar(proveedor, zip5, consulta=consulta)


def cobertura_sf_explicita(item):
    if not isinstance(item, dict):
        return None
    for key in ("coverageSf", "coverage_sf", "squareFeetPerCase", "sqft_per_case", "coverageSqFt"):
        try:
            valor = float(item.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if valor > 0:
            return round(valor, 4)
    return None


def consultar_con_lock(tienda, proveedor, zip5, terminos, scrape_fn, persistir_fn=None):
    """Una sola llamada externa por producto+ZIP entre instancias. No reintenta si falla."""
    zip5 = zip_obra(zip5)
    if not zip5 or not callable(scrape_fn):
        return [], "missing_zip", False
    unicos = []
    vistos = set()
    for termino in terminos or []:
        clave = clave_consulta(termino)
        if not clave or clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(str(termino).strip())
    if not unicos:
        return [], "", False
    frescos = []
    faltan = []
    for termino in unicos:
        hits = tienda.fresco(proveedor, zip5, consulta=termino) if tienda else []
        if hits:
            frescos.extend(hits)
        else:
            faltan.append(termino)
    if not faltan:
        return frescos, "cache", False
    clave = clave_lock(proveedor, zip5, faltan)
    puede_lock = bool(tienda and getattr(tienda, "disponible", False))
    soy_lider = tienda.adquirir_lock(clave) if puede_lock else True
    if not soy_lider:
        for termino in faltan:
            frescos.extend(tienda.esperar_precio(proveedor, zip5, termino) or [])
        return frescos, "shared", False
    try:
        still = []
        for termino in faltan:
            hits = tienda.fresco(proveedor, zip5, consulta=termino) if tienda else []
            if hits:
                frescos.extend(hits)
            else:
                still.append(termino)
        if not still:
            return frescos, "cache", False
        productos = scrape_fn(still, zip5)
        if not isinstance(productos, list):
            productos = []
        if persistir_fn:
            persistir_fn(productos, zip5, still)
        return productos, "scraped", True
    except Exception as err:
        print(f"--> proveedor consulta {proveedor}: {err}")
        return frescos, f"error:{err}", False
    finally:
        if puede_lock:
            tienda.soltar_lock(clave)


PROVEEDORES = {}


def registrar_proveedor(nombre, scrape_fn):
    clave = str(nombre or "").strip()
    if not clave or not callable(scrape_fn):
        return
    PROVEEDORES[clave] = scrape_fn


def scrape_de(nombre):
    return PROVEEDORES.get(str(nombre or "").strip())


def fila_desde_publico(publico, zip5, consulta="", proveedor=""):
    if not isinstance(publico, dict):
        return None
    try:
        precio = float(publico.get("precio_normalizado") or publico.get("precio_material") or 0)
    except (TypeError, ValueError):
        precio = 0.0
    if precio <= 0:
        return None
    zip_ok = zip_obra(zip5 or publico.get("zip_code"))
    if not zip_ok:
        return None
    return {
        "proveedor": str(proveedor or publico.get("proveedor") or "").strip(),
        "producto_id": str(publico.get("producto_id") or publico.get("sku") or "").strip(),
        "consulta_clave": clave_consulta(consulta or publico.get("consulta_clave") or publico.get("consulta") or ""),
        "presentacion": str(publico.get("presentacion") or "").strip(),
        "unidad_original": str(publico.get("unidad_original") or "").strip(),
        "unidad_normalizada": str(publico.get("unidad_normalizada") or publico.get("unidad") or "").strip(),
        "zip_code": zip_ok,
        "tienda_id": str(publico.get("tienda_id") or publico.get("store_id") or "").strip(),
        "tienda_nombre": str(publico.get("tienda_nombre") or publico.get("store_name") or "").strip(),
        "nombre": str(publico.get("nombre") or "").strip(),
        "product_url": str(publico.get("product_url") or publico.get("productUrl") or "").strip(),
        "image_url": str(publico.get("image_url") or publico.get("imageUrl") or "").strip(),
        "precio_original": publico.get("precio_original"),
        "moneda": str(publico.get("moneda") or "USD").strip() or "USD",
        "precio_normalizado": round(precio, 2),
        "cobertura_sf": publico.get("cobertura_sf"),
        "last_updated": publico.get("last_updated") or ahora_utc().isoformat(),
    }


def es_fila_cache(row):
    return isinstance(row, dict) and ("precio_normalizado" in row or row.get("fuente") == "supabase_proveedor")


def fila_publica_cache(row):
    if not isinstance(row, dict):
        return None
    return {
        "proveedor": row.get("proveedor") or "",
        "nombre": row.get("nombre") or "",
        "producto_id": row.get("producto_id") or "",
        "product_url": row.get("product_url") or "",
        "image_url": row.get("image_url") or "",
        "precio_material": row.get("precio_normalizado") or 0,
        "precio_original": row.get("precio_original"),
        "unidad_original": row.get("unidad_original") or "",
        "unidad": row.get("unidad_normalizada") or "",
        "presentacion": row.get("presentacion") or "",
        "zip_code": row.get("zip_code") or "",
        "store_id": row.get("tienda_id") or "",
        "store_name": row.get("tienda_nombre") or "",
        "moneda": row.get("moneda") or "USD",
        "last_updated": row.get("last_updated") or "",
        "image_updated": row.get("image_updated") or "",
        "fuente": "supabase_proveedor",
    }
