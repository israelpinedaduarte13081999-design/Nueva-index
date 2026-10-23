"""Pruebas locales de caché compartida, lock y frescura. No imprime secretos."""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

from proveedores_precios import (
    DIAS_PRECIO,
    TiendaPrecios,
    ahora_utc,
    consultar_con_lock,
    dinero_documentado,
    fila_desde_publico,
    precio_fresco,
    zip_obra,
)


class _Tabla:
    def __init__(self, filas, uniques=None):
        self.filas = filas
        self.uniques = uniques or []
        self.filtros = []
        self._orden = None
        self._limite = None
        self._payload = None
        self._conflict = None

    def select(self, *_a, **_k):
        return self

    def eq(self, campo, valor):
        self.filtros.append((campo, valor))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limite = n
        return self

    def execute(self):
        data = [dict(f) for f in self.filas if all(str(f.get(c)) == str(v) for c, v in self.filtros)]
        if self._limite:
            data = data[: self._limite]
        return type("R", (), {"data": data})()

    def upsert(self, payload, on_conflict=""):
        self._payload = payload
        self._conflict = on_conflict
        keys = [k.strip() for k in (on_conflict or "").split(",") if k.strip()]
        for i, fila in enumerate(self.filas):
            if keys and all(str(fila.get(k)) == str(payload.get(k)) for k in keys):
                self.filas[i] = dict(payload)
                return self
        self.filas.append(dict(payload))
        return self

    def insert(self, payload):
        clave = payload.get("clave")
        if clave and any(f.get("clave") == clave for f in self.filas):
            raise RuntimeError("duplicate key 23505")
        self.filas.append(dict(payload))
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def delete(self):
        keep = []
        for fila in self.filas:
            if all(str(fila.get(c)) == str(v) for c, v in self.filtros):
                continue
            keep.append(fila)
        self.filas[:] = keep
        return self


class _ClienteFalso:
    def __init__(self):
        self.precios = []
        self.locks = []

    def table(self, nombre):
        if nombre == "catalogo_proveedor_locks":
            return _Tabla(self.locks)
        return _Tabla(self.precios)


def _ok(nombre, cond, detalle=""):
    estado = "OK" if cond else "FAIL"
    extra = f" · {detalle}" if detalle else ""
    print(f"[{estado}] {nombre}{extra}")
    return bool(cond)


def probar_reglas():
    fallos = 0
    if not _ok("zip 5 dígitos conserva ceros", zip_obra("02108") == "02108"):
        fallos += 1
    if not _ok("zip obra luego cliente", zip_obra("", "30318") == "30318"):
        fallos += 1
    if not _ok("sin ZIP predeterminado", zip_obra("", None, "abc") == ""):
        fallos += 1
    if not _ok("centavos solo si el campo lo documenta", dinero_documentado(1999, "priceInCents") == 19.99):
        fallos += 1
    if not _ok("price no se divide por magnitud", dinero_documentado(1999, "price") == 1999):
        fallos += 1
    return fallos


def probar_lock_y_zips():
    fallos = 0
    cliente = _ClienteFalso()
    tienda = TiendaPrecios(cliente)
    llamadas = []

    def persistir(productos, zip5, _terminos):
        item = (productos or [{}])[0]
        tienda.guardar(fila_desde_publico({
            "proveedor": "Home Depot",
            "producto_id": item.get("productId") or "HD-OAK-34",
            "precio_material": float(item.get("price") or 3.49),
            "unidad": "SF",
            "presentacion": "plank",
            "store_id": item.get("storeId") or "1177",
            "store_name": item.get("storeName") or "",
        }, zip5, consulta="oak flooring"))

    def scrape(terminos, zip5):
        llamadas.append((tuple(terminos), zip5))
        time.sleep(0.15)
        return [{
            "name": "Oak flooring 3/4",
            "price": 3.49,
            "storeId": "1177",
            "storeName": "Home Depot Midtown",
            "zipCode": zip5,
            "productId": "HD-OAK-34",
        }]

    def buscar(zip5="30318"):
        return consultar_con_lock(tienda, "Home Depot", zip5, ["oak flooring"], scrape, persistir_fn=persistir)

    hilos = [threading.Thread(target=buscar) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    if not _ok("un scrape para dos contratistas mismo ZIP", len(llamadas) == 1, f"llamadas={len(llamadas)}"):
        fallos += 1

    fila = fila_desde_publico({
        "proveedor": "Home Depot",
        "producto_id": "HD-OAK-34",
        "precio_material": 3.49,
        "unidad": "SF",
        "presentacion": "plank",
        "store_id": "1177",
        "store_name": "Home Depot Midtown",
    }, "30318", consulta="oak flooring")
    tienda.guardar(fila)
    fecha1 = fila["last_updated"]
    time.sleep(0.05)
    hits = tienda.fresco("Home Depot", "30318", consulta="oak flooring")
    if not _ok("lectura no reinicia last_updated", hits and hits[0].get("last_updated") == fecha1):
        fallos += 1

    consultar_con_lock(tienda, "Home Depot", "30318", ["oak flooring"], scrape, persistir_fn=persistir)
    if not _ok("segunda búsqueda usa caché", len(llamadas) == 1, f"llamadas={len(llamadas)}"):
        fallos += 1

    fila_b = fila_desde_publico({
        "proveedor": "Home Depot",
        "producto_id": "HD-OAK-34",
        "precio_material": 3.89,
        "unidad": "SF",
        "presentacion": "plank",
        "store_id": "2211",
        "store_name": "Home Depot Buckhead",
    }, "30327", consulta="oak flooring")
    tienda.guardar(fila_b)
    a = tienda.buscar("Home Depot", "30318", consulta="oak flooring")
    b = tienda.buscar("Home Depot", "30327", consulta="oak flooring")
    if not _ok("dos ZIP se guardan aparte", len(a) == 1 and len(b) == 1 and a[0]["precio_normalizado"] != b[0]["precio_normalizado"]):
        fallos += 1

    vencida = dict(a[0])
    vencida["last_updated"] = (ahora_utc() - timedelta(days=DIAS_PRECIO + 1)).isoformat()
    cliente.precios[:] = [vencida, dict(b[0])]
    consultas_renovacion = []

    def scrape_renovar(terminos, zip5):
        consultas_renovacion.append(zip5)
        return [{"name": "Oak flooring 3/4", "price": 3.55, "storeId": "1177", "storeName": "Midtown", "zipCode": zip5, "productId": "HD-OAK-34"}]

    consultar_con_lock(tienda, "Home Depot", "30318", ["oak flooring"], scrape_renovar, persistir_fn=persistir)
    consultar_con_lock(tienda, "Home Depot", "30318", ["oak flooring"], scrape_renovar, persistir_fn=persistir)
    if not _ok("vencido renueva una sola vez", len(consultas_renovacion) == 1, f"renovaciones={len(consultas_renovacion)}"):
        fallos += 1

    if not _ok("cero no se guarda", tienda.guardar(fila_desde_publico({"precio_material": 0, "proveedor": "Home Depot"}, "30318") or {"proveedor": "x", "precio_normalizado": 0, "zip_code": "30318"}) is False):
        fallos += 1
    return fallos


def probar_supabase_real():
    from app import _supabase_client
    client = _supabase_client()
    if client is None:
        print("[SKIP] Supabase cliente no disponible")
        return 0, False
    tienda = TiendaPrecios(client)
    try:
        client.table("catalogo_proveedor_precios").select("id").limit(1).execute()
    except Exception as err:
        print(f"[BLOQUEO] Falta catalogo_proveedor_precios: {err}")
        print("[BLOQUEO] Ejecuta catalogo_proveedores.sql en el SQL Editor de Supabase.")
        return 1, False
    marca = ahora_utc().isoformat()
    ok = tienda.guardar({
        "proveedor": "Home Depot",
        "producto_id": "TEST-LOCAL-OAK",
        "consulta_clave": "oak flooring test",
        "presentacion": "plank",
        "unidad_original": "box",
        "unidad_normalizada": "SF",
        "zip_code": "30318",
        "tienda_id": "TEST-1177",
        "tienda_nombre": "Test store",
        "nombre": "Test oak flooring",
        "precio_original": 48.0,
        "precio_normalizado": 3.21,
        "moneda": "USD",
        "last_updated": marca,
    })
    if not ok:
        print("[FAIL] no se pudo guardar fila de prueba en Supabase")
        return 1, True
    leida = tienda.buscar("Home Depot", "30318", consulta="oak flooring test")
    misma = leida and leida[0].get("last_updated")
    fresco = bool(leida and precio_fresco(leida[0]))
    print(f"[{'OK' if fresco else 'FAIL'}] Supabase local: lectura compartida fresco={fresco} misma_fecha={bool(misma)}")
    otra = tienda.guardar({
        "proveedor": "Home Depot",
        "producto_id": "TEST-LOCAL-OAK",
        "consulta_clave": "oak flooring test",
        "presentacion": "plank",
        "unidad_original": "box",
        "unidad_normalizada": "SF",
        "zip_code": "30327",
        "tienda_id": "TEST-2211",
        "tienda_nombre": "Test store B",
        "nombre": "Test oak flooring",
        "precio_original": 52.0,
        "precio_normalizado": 3.77,
        "moneda": "USD",
    })
    a = tienda.buscar("Home Depot", "30318", consulta="oak flooring test")
    b = tienda.buscar("Home Depot", "30327", consulta="oak flooring test")
    separados = bool(a and b and a[0].get("zip_code") != b[0].get("zip_code"))
    print(f"[{'OK' if otra and separados else 'FAIL'}] Supabase local: ZIP 30318 y 30327 separados")
    return 0 if fresco and separados else 1, True


def intentar_migracion():
    from app import _supabase_client
    client = _supabase_client()
    if client is None:
        return False, "sin cliente supabase"
    try:
        client.table("catalogo_proveedor_precios").select("id").limit(1).execute()
        client.table("catalogo_proveedor_locks").select("clave").limit(1).execute()
        return True, "tablas ya existen"
    except Exception:
        pass
    sql_path = os.path.join(os.path.dirname(__file__), "catalogo_proveedores.sql")
    with open(sql_path, encoding="utf-8") as fh:
        sql = fh.read()
    for rpc in ("exec_sql", "execute_sql", "sql"):
        try:
            client.rpc(rpc, {"query": sql}).execute()
            return True, f"migración vía rpc {rpc}"
        except Exception:
            continue
    return False, "PostgREST no crea tablas; hay que pegar catalogo_proveedores.sql en el SQL Editor"


def main():
    fallos = probar_reglas()
    fallos += probar_lock_y_zips()
    ok_mig, detalle_mig = intentar_migracion()
    print(f"[{'OK' if ok_mig else 'BLOQUEO'}] migración: {detalle_mig}")
    extra, hay_tabla = probar_supabase_real()
    fallos += extra
    print(f"resultado local: fallos={fallos} tablas={'si' if hay_tabla else 'no'}")
    return 0 if fallos == 0 and ok_mig else 1


if __name__ == "__main__":
    sys.exit(main())
