import ssl
ssl._create_default_https_context = ssl._create_unverified_context

"""
Respalda catalogo_items y traduce descripcion (ES -> EN) en Supabase.

Requisitos:
  pip install supabase python-dotenv httpx

Credenciales en .env:
  SUPABASE_URL=...
  SUPABASE_KEY=...   (o SUPABASE_ANON_KEY / SUPABASE_PUBLISHABLE_KEY)

Uso:
  python traducir_catalogo.py

Traduce SOLO con MyMemoryTranslator (email para cuota ampliada).
Omite los registros ya en inglés o ya procesados (800+) y espera 5 s
entre traducciones. Ante 429 espera 15 s y reintenta con MyMemory.
"""
import csv
import os
import re
import sys
import time
from pathlib import Path

os.environ["PYTHONHTTPSVERIFY"] = "0"
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""
os.environ["SSL_CERT_FILE"] = ""

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests

_original_request = requests.Session.request

def _request_sin_ssl(self, method, url, **kwargs):
    kwargs["verify"] = False
    return _original_request(self, method, url, **kwargs)

requests.Session.request = _request_sin_ssl

from dotenv import load_dotenv

MYMEMORY_URL = "https://api.mymemory.translated.net/get"

TABLA = "catalogo_items"
ARCHIVO_RESPALDO = "respaldo_catalogo.csv"
TAMANO_PAGINA = 1000
PAUSA_ENTRE_FILAS = 5
PAUSA_429 = 15
REINTENTOS = 20
LIMITE_MYMEMORY = 480
EMAIL_MYMEMORY = "israelpinedaurte13081999@gmail.com"
REGISTROS_YA_LISTOS = 800
ARCHIVO_PROGRESO = "catalogo_ids_traducidos.txt"
PALABRAS_ES = re.compile(
    r"\b(instalaci[oó]n|demolici[oó]n|gabinete|azulejo|techo|piso|pintura|suministro|"
    r"retiro|pared|cielo|ducha|ba[nñ]o|cocina|moldura|rodapi|impermeabilizaci|"
    r"mano de obra|alfombra|esponja|reemplazo|huella|contrahuella|escal[oó]n|"
    r"tiras|clavos|perimetrales|transici[oó]n|met[aá]lica|aislamiento|yeso|"
    r"tablaroca|porcelanato|contrapiso|fregadero|despensa)\b",
    re.I,
)
PALABRAS_EN = re.compile(
    r"\b(the|and|of|for|with|install|installation|remove|removal|replace|replacement|"
    r"carpet|padding|stair|door|paint|wall|ceiling|floor|drywall|tile|cabinet|"
    r"kitchen|bathroom|shower|roof|siding)\b",
    re.I,
)

load_dotenv()


def credenciales_supabase():
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or ""
    )
    return url, key


def cliente_supabase():
    url, key = credenciales_supabase()
    if not url or not key:
        print("Faltan SUPABASE_URL y SUPABASE_KEY (o SUPABASE_ANON_KEY) en .env")
        sys.exit(1)
    import httpx
    from supabase import create_client

    try:
        from supabase import ClientOptions
    except ImportError:
        from supabase.lib.client_options import ClientOptions
    http = httpx.Client(verify=False, timeout=60.0)
    try:
        options = ClientOptions(httpx_client=http)
        return create_client(url, key, options=options)
    except TypeError:
        return create_client(url, key)


def leer_catalogo_completo(sb):
    filas = []
    offset = 0
    while True:
        res = (
            sb.table(TABLA)
            .select("*")
            .order("id")
            .range(offset, offset + TAMANO_PAGINA - 1)
            .execute()
        )
        lote = list(res.data or [])
        filas.extend(lote)
        print(f"Leídas {len(filas)} filas de {TABLA}...")
        if len(lote) < TAMANO_PAGINA:
            break
        offset += TAMANO_PAGINA
    return filas


def guardar_respaldo_csv(filas, ruta):
    if not filas:
        print("No hay registros para respaldar.")
        return
    campos = []
    vistos = set()
    for row in filas:
        for k in row.keys():
            if k not in vistos:
                vistos.add(k)
                campos.append(k)
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        writer.writeheader()
        for row in filas:
            writer.writerow({k: row.get(k, "") for k in campos})
    print(f"Respaldo guardado: {ruta} ({len(filas)} registros)")


def clave_fila(row):
    if row.get("id") not in (None, ""):
        return "id", row.get("id")
    if row.get("codigo") not in (None, ""):
        return "codigo", row.get("codigo")
    return None, None


def id_numerico(row):
    try:
        return int(row.get("id"))
    except (TypeError, ValueError):
        return None


def parece_espanol(texto):
    t = str(texto or "").strip()
    if not t:
        return False
    if re.search(r"[áéíóúñüÁÉÍÓÚÑÜ]", t):
        return True
    return len(PALABRAS_ES.findall(t)) >= 2


def ya_en_ingles(texto):
    t = str(texto or "").strip()
    if not t:
        return False
    en = len(PALABRAS_EN.findall(t))
    es = len(PALABRAS_ES.findall(t))
    tiene_tilde = bool(re.search(r"[áéíóúñüÁÉÍÓÚÑÜ]", t))
    if en >= 2 and en > es:
        return True
    if en >= 1 and es == 0 and not tiene_tilde:
        return True
    if not tiene_tilde and es == 0 and PALABRAS_EN.search(t):
        return True
    return False


def cargar_ids_progreso(ruta):
    hechos = set()
    if not ruta.exists():
        return hechos
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        valor = linea.strip()
        if valor:
            hechos.add(valor)
    return hechos


def guardar_id_progreso(ruta, valor_clave):
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(f"{valor_clave}\n")


def cargar_respaldo_por_clave(ruta):
    originales = {}
    if not ruta.exists():
        return originales
    with open(ruta, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            campo, valor = clave_fila(row)
            if campo is None:
                continue
            desc = str(row.get("descripcion") or row.get("description") or "").strip()
            originales[(campo, str(valor))] = desc
    return originales


def ya_traducido(original_db, original_respaldo):
    actual = str(original_db or "").strip()
    if not actual:
        return False
    if ya_en_ingles(actual):
        return True
    respaldo = str(original_respaldo or "").strip()
    if respaldo and actual != respaldo and not parece_espanol(actual):
        return True
    return False


def debe_omitir(i, row, original, respaldo, ids_hechos):
    if not original:
        return True, "vacía"
    campo, valor = clave_fila(row)
    if campo is None:
        return True, "sin id/codigo"
    if str(valor) in ids_hechos:
        return True, "ya procesada (progreso local)"
    n = id_numerico(row)
    if n is not None and n <= REGISTROS_YA_LISTOS:
        return True, f"id<={REGISTROS_YA_LISTOS} ya listo"
    if i <= REGISTROS_YA_LISTOS:
        return True, f"posición {i}<={REGISTROS_YA_LISTOS} ya lista"
    if ya_en_ingles(original):
        return True, "descripción ya en inglés"
    if ya_traducido(original, respaldo):
        return True, "ya traducida vs respaldo"
    return False, ""


class MyMemoryTranslator:
    """Cliente directo de MyMemory. No usa deep-translator ni Google."""

    def __init__(self, source="es", target="en", email=None):
        self.source = source
        self.target = target
        self.email = email

    def translate(self, text):
        params = {
            "q": text,
            "langpair": f"{self.source}|{self.target}",
        }
        if self.email:
            params["de"] = self.email
        resp = requests.get(MYMEMORY_URL, params=params, timeout=30, verify=False)
        if resp.status_code == 429:
            raise RuntimeError("MyMemory 429: cuota o ritmo excedido. Esperar e intentar de nuevo.")
        if resp.status_code >= 400:
            raise RuntimeError(f"MyMemory HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        details = data.get("responseDetails") or ""
        status = data.get("responseStatus")
        if status == 429 or "quota" in str(details).lower():
            raise RuntimeError(f"MyMemory cuota: {details}")
        if status not in (200, "200", None) and not data.get("responseData"):
            raise RuntimeError(f"MyMemory error {status}: {details}")
        traduccion = (data.get("responseData") or {}).get("translatedText") or ""
        if traduccion.lower().startswith("query length limit") or "invalid" in traduccion.lower():
            raise RuntimeError(traduccion)
        return traduccion.strip()


def crear_traductor():
    return MyMemoryTranslator(source="es", target="en", email=EMAIL_MYMEMORY)


def es_error_cuota(err):
    msg = str(err).lower()
    return any(
        t in msg
        for t in ("429", "cuota", "quota", "too many", "according to google", "rate")
    )


def traducir_una(traductor, texto):
    ultimo = None
    for intento in range(1, REINTENTOS + 1):
        try:
            resultado = traductor.translate(texto)
            return str(resultado or "").strip()
        except Exception as err:
            ultimo = err
            espera = PAUSA_429 if es_error_cuota(err) else PAUSA_ENTRE_FILAS
            print(
                f"    MyMemory reintento {intento}/{REINTENTOS} en {espera}s: {err}"
            )
            time.sleep(espera)
            traductor = crear_traductor()
    raise RuntimeError(ultimo)


def actualizar_descripcion(sb, campo_clave, valor_clave, descripcion):
    (
        sb.table(TABLA)
        .update({"descripcion": descripcion})
        .eq(campo_clave, valor_clave)
        .execute()
    )


def main():
    carpeta = Path(__file__).resolve().parent
    ruta_csv = carpeta / ARCHIVO_RESPALDO
    print("Conectando a Supabase...")
    sb = cliente_supabase()

    print(f"1) Respaldo de {TABLA} -> {ruta_csv.name}")
    filas = leer_catalogo_completo(sb)
    if ruta_csv.exists():
        print(f"Respaldo ya existe ({ruta_csv.name}); no se sobrescribe para conservar el original.")
    else:
        guardar_respaldo_csv(filas, ruta_csv)
    if not filas:
        return

    print(
        "2) Traducción ES -> EN EXCLUSIVA con "
        "MyMemoryTranslator(source='es', target='en', "
        f"email='{EMAIL_MYMEMORY}')"
    )
    print("   GoogleTranslator no se usa. 429 -> espera 15s y reintenta MyMemory.")
    print(f"   Se omiten los primeros {REGISTROS_YA_LISTOS} y cualquier descripción ya en inglés.")
    traductor = crear_traductor()
    print(
        f"   Instancia: {type(traductor).__name__} "
        f"source={traductor.source!r} target={traductor.target!r} email={traductor.email!r}"
    )
    originales_respaldo = cargar_respaldo_por_clave(ruta_csv)
    ruta_progreso = carpeta / ARCHIVO_PROGRESO
    ids_hechos = cargar_ids_progreso(ruta_progreso)
    ok = 0
    omitidas = 0
    errores = 0
    total = len(filas)
    saltos_seguidos = 0

    for i, row in enumerate(filas, start=1):
        campo, valor = clave_fila(row)
        codigo = row.get("codigo") or ""
        original = str(row.get("descripcion") or row.get("description") or "").strip()
        respaldo = originales_respaldo.get((campo, str(valor)), "") if campo else ""

        omitir, motivo = debe_omitir(i, row, original, respaldo, ids_hechos)
        if omitir:
            omitidas += 1
            saltos_seguidos += 1
            if omitidas == 1 or omitidas % 100 == 0:
                print(f"[{i}/{total}] omitidos {omitidas} ({motivo})...")
            continue

        if saltos_seguidos:
            print(
                f"Se saltaron {saltos_seguidos} registros listos. "
                f"Continúa en [{i}/{total}] id={valor} codigo={codigo}"
            )
            saltos_seguidos = 0

        try:
            traducida = traducir_una(traductor, original[:LIMITE_MYMEMORY])
            if not traducida:
                omitidas += 1
                print(f"[{i}/{total}] id={valor} codigo={codigo} — traducción vacía, se omite")
            elif traducida == original:
                omitidas += 1
                print(f"[{i}/{total}] id={valor} codigo={codigo} — sin cambios")
            else:
                actualizar_descripcion(sb, campo, valor, traducida)
                guardar_id_progreso(ruta_progreso, valor)
                ids_hechos.add(str(valor))
                ok += 1
                print(
                    f"[{i}/{total}] id={valor} codigo={codigo} — OK (MyMemory)\n"
                    f"    ES: {original[:120]}\n"
                    f"    EN: {traducida[:120]}"
                )
        except Exception as err:
            errores += 1
            print(f"[{i}/{total}] id={valor} codigo={codigo} — ERROR (MyMemory): {err}")
        time.sleep(PAUSA_ENTRE_FILAS)

    if saltos_seguidos:
        print(f"Se saltaron {saltos_seguidos} registros listos al final del catálogo.")
    print(
        f"Listo. Actualizadas={ok}  omitidas={omitidas}  errores={errores}  total={total}"
    )


if __name__ == "__main__":
    main()
