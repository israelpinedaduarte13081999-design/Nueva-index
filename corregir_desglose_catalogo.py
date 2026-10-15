"""Audita y corrige el desglose de catalogo_items al estilo Xactimate.

precio_base no se toca. Tras el recálculo se cumple:
  precio_material + precio_mano_obra + precio_remocion = precio_base
con exactitud de centavos.

Uso:
  python corregir_desglose_catalogo.py
  python corregir_desglose_catalogo.py --csv auditoria_desglose.csv
  python corregir_desglose_catalogo.py --apply
  python corregir_desglose_catalogo.py --force --apply

Por defecto es dry-run (solo lee y reporta). --apply escribe en Supabase.
Requiere SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY en .env.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import unicodedata
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

TABLA = "catalogo_items"
TAMANO_PAGINA = 1000
TAMANO_UPDATE = 80
CENTAVO = Decimal("0.01")

# Remoción como % del paquete completo (R&R Xactimate).
# material_resto = material / (precio_base - remocion). Labor absorbe el residuo.
# Pintura 20/80 y gabinetes 70/30 sobre el dinero restante, como en campo.
SPLITS = {
    "pintura":      {"demo": Decimal("0.06"), "material_resto": Decimal("0.20"), "nota": "Labor-heavy: prep 6%, resto 20/80"},
    "drywall":      {"demo": Decimal("0.14"), "material_resto": Decimal("0.35"), "nota": "Tear-out + hang/finish; labor ~65% del resto"},
    "piso":         {"demo": Decimal("0.12"), "material_resto": Decimal("0.55"), "nota": "Pull existing floor; LVP/laminate ~55% material"},
    "alfombra":     {"demo": Decimal("0.10"), "material_resto": Decimal("0.50"), "nota": "Carpet + pad; demo 10%"},
    "azulejo":      {"demo": Decimal("0.15"), "material_resto": Decimal("0.42"), "nota": "Demo of tile is slow; labor-heavy set"},
    "techo":        {"demo": Decimal("0.15"), "material_resto": Decimal("0.50"), "nota": "Tear-off high end of 5-15%; shingles ~50/50"},
    "siding":       {"demo": Decimal("0.12"), "material_resto": Decimal("0.52"), "nota": "Remove existing cladding"},
    "gabinetes":    {"demo": Decimal("0.10"), "material_resto": Decimal("0.70"), "nota": "Material-heavy 70/30 after 10% demo"},
    "encimeras":    {"demo": Decimal("0.08"), "material_resto": Decimal("0.72"), "nota": "Slab-dominant"},
    "bano":         {"demo": Decimal("0.12"), "material_resto": Decimal("0.50"), "nota": "Vanity/fixture R&R"},
    "ventanas":     {"demo": Decimal("0.08"), "material_resto": Decimal("0.70"), "nota": "Unit cost is mostly the window"},
    "puertas":      {"demo": Decimal("0.08"), "material_resto": Decimal("0.58"), "nota": "Prehung / slab"},
    "carpinteria":  {"demo": Decimal("0.08"), "material_resto": Decimal("0.45"), "nota": "Trim/base; labor-heavy install"},
    "framing":      {"demo": Decimal("0.12"), "material_resto": Decimal("0.48"), "nota": "Lumber vs carpenter"},
    "deck":         {"demo": Decimal("0.12"), "material_resto": Decimal("0.52"), "nota": "Tear-out decking + framing labor"},
    "canales":      {"demo": Decimal("0.10"), "material_resto": Decimal("0.50"), "nota": "Remove/replace gutters"},
    "plomeria":     {"demo": Decimal("0.08"), "material_resto": Decimal("0.52"), "nota": "Fixture R&R"},
    "electricidad": {"demo": Decimal("0.07"), "material_resto": Decimal("0.38"), "nota": "Device/rough-in is labor-heavy"},
    "hvac":         {"demo": Decimal("0.08"), "material_resto": Decimal("0.60"), "nota": "Equipment-heavy"},
    "aislamiento":  {"demo": Decimal("0.05"), "material_resto": Decimal("0.48"), "nota": "Little demo; batt/blown"},
    "concreto":     {"demo": Decimal("0.10"), "material_resto": Decimal("0.38"), "nota": "Demo slab is costly; pour is labor"},
    "cerca":        {"demo": Decimal("0.12"), "material_resto": Decimal("0.55"), "nota": "Remove posts/panels"},
    "paisajismo":   {"demo": Decimal("0.08"), "material_resto": Decimal("0.50"), "nota": "Plant/hardscape mix"},
    "demolicion":   {"demo": Decimal("1.00"), "material_resto": Decimal("0.00"), "nota": "100% remocion"},
    "general":      {"demo": Decimal("0.10"), "material_resto": Decimal("0.50"), "nota": "Remodel average 10% demo, 50/50 resto"},
}


def _sin_acentos(texto):
    nfd = unicodedata.normalize("NFD", str(texto or ""))
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn").lower()


def _blob(row):
    return _sin_acentos(
        " ".join(
            str(row.get(k) or "")
            for k in ("codigo", "categoria", "descripcion", "descripcion_es", "descripcion_en")
        )
    )


def money(valor):
    return Decimal(str(valor or 0)).quantize(CENTAVO, rounding=ROUND_HALF_UP)


def alcance_exclusivo(row):
    texto = _blob(row)
    codigo = _sin_acentos(row.get("codigo") or "")
    if re.search(r"material only|solo material|supply only|suministro de material", texto) and not re.search(r"instal", texto):
        return "material"
    if re.search(r"labor only|solo mano|solo instalacion|install(?:ation)? only|mano de obra solamente", texto):
        return "labor"
    if re.search(
        r"demolition only|solo demol|solo remoc|removal only|tear[\s-]*(out|off) only|demo only|haul off only",
        texto,
    ):
        return "demo"
    if re.search(r"(^|[-_])(dem|demo|tearoff|removal)([-_]|$)", codigo) and not re.search(
        r"instal|install|material|replace|r&r", texto
    ):
        return "demo"
    if re.search(r"(material|supply|mat)$", codigo) and not re.search(r"instal|install|labor", texto):
        return "material"
    if re.search(r"(labor|lab)$", codigo) and not re.search(r"material|demo|remove", texto):
        return "labor"
    return ""


def oficio_de_fila(row):
    texto = _blob(row)
    codigo = _sin_acentos(row.get("codigo") or "")
    cat = _sin_acentos(row.get("categoria") or "")
    reglas = (
        ("demolicion", r"\bdemolici|\bdemolition|\bhaul off|escombro|tear[\s-]*(out|off)|retiro de"),
        ("pintura", r"\bpintura|\bpaint|\besmalte|\bprimer\b|\bstain\b|\bpoliuretano"),
        ("drywall", r"drywall|tablaroca|sheetrock|\byeso\b|tape and float|durock|cement board"),
        ("alfombra", r"alfombra|\bcarpet\b|\bpadding\b|tack strip"),
        ("azulejo", r"azulejo|\btile\b|porcelanato|ceramica|baldosa|travertine|mosaico"),
        ("techo", r"\broof|\btecho|\bshingle|\bteja|\bsbs\b|\bepdm\b|\btpo\b|ridge cap|ice.?water"),
        ("siding", r"\bsiding\b|hardie|revestimiento|soffit|\bfascia\b"),
        ("gabinetes", r"gabinete|\bcabinet\b|\bvanity\b"),
        ("encimeras", r"encimera|countertop|granito|\bcuarzo\b|\bquartz\b"),
        ("bano", r"\bbano|\bbaño|\bbath|\bducha|\bshower|\binodoro|\btoilet\b"),
        ("ventanas", r"ventana|\bwindow\b"),
        ("puertas", r"\bpuerta\b|\bdoor\b|prehung"),
        ("canales", r"canaleta|\bgutter\b|downspout|bajante"),
        ("deck", r"\bdeck\b|terraza|\bporche\b|\bporch\b"),
        ("framing", r"framing|enmarcado|\bcercha\b|vigueta|\bjoist\b|subfloor"),
        ("carpinteria", r"carpinter|moldura|\btrim\b|baseboard|rodapie|shoe mould"),
        ("plomeria", r"plomer|\bplumb\b|\bpex\b|drenaje|\bfaucet\b|grifer"),
        ("electricidad", r"electric|cableado|tomacorriente|\boutlet\b|\bpanel el"),
        ("hvac", r"\bhvac\b|climatiz|\bducto\b|condensador|furnace"),
        ("aislamiento", r"aislamiento|\binsulation\b|\bbatt\b|blown.?in"),
        ("concreto", r"concreto|\bconcrete\b|\bslab\b|self.?level"),
        ("cerca", r"\bcerca\b|\bfence\b|\bfencing\b"),
        ("paisajismo", r"paisaj|landscape|sod\b|irrigation"),
        ("piso", r"\bpiso\b|\bfloor|\blvt\b|\blvp\b|\bspc\b|laminat|hardwood|vinilo"),
    )
    prefijos = (
        ("techo", ("rfg", "rof", "ext-rof")),
        ("siding", ("sdg", "ext-sdg")),
        ("piso", ("fct", "flr", "fcc", "wdh")),
        ("pintura", ("pnt", "pxt")),
        ("drywall", ("dry",)),
        ("azulejo", ("til",)),
        ("gabinetes", ("cab", "ktr", "kit")),
        ("bano", ("btr", "bth", "stp")),
        ("ventanas", ("wnd",)),
        ("canales", ("gut",)),
        ("plomeria", ("plm",)),
        ("electricidad", ("ele",)),
        ("hvac", ("hvc",)),
        ("framing", ("frm", "tru")),
        ("deck", ("dck", "trz")),
        ("demolicion", ("dem",)),
    )
    for oficio, prefs in prefijos:
        if any(codigo.startswith(p) for p in prefs):
            if oficio != "demolicion" or not re.search(r"instal|install|material|replace", texto):
                return oficio
    for oficio, patron in reglas:
        if re.search(patron, texto) or re.search(patron, cat):
            return oficio
    return "general"


def sin_remocion_logica(row, oficio):
    texto = _blob(row)
    if oficio == "demolicion":
        return False
    if re.search(
        r"new construction|obra nueva|first(?:-|\s)?time|suministro|delivery|permit\b|"
        r"andamiaje|scaffold|drop cloth|plastic cover|protection board|"
        r"ridge cap|starter strip|drip edge|ice.?and.?water|flashing|"
        r"caulk|sellador|silicone|trip charge|minimum charge",
        texto,
    ) and not re.search(r"remove|remover|tear|retiro|demol", texto):
        return True
    if re.search(r"install only|solo instalar|instalacion de", texto) and not re.search(
        r"remove|remover|tear|retiro|replace|r&r|reemplaz", texto
    ):
        return True
    return False


def desglose_objetivo(row):
    base = money(row.get("precio_base"))
    exclusivo = alcance_exclusivo(row)
    if exclusivo == "material":
        return base, money(0), money(0), "material", "exclusivo_material"
    if exclusivo == "labor":
        return money(0), base, money(0), "labor", "exclusivo_labor"
    if exclusivo == "demo":
        return money(0), money(0), base, "demolicion", "exclusivo_demo"

    oficio = oficio_de_fila(row)
    spec = SPLITS[oficio]
    demo_frac = spec["demo"]
    mat_resto = spec["material_resto"]
    if sin_remocion_logica(row, oficio):
        demo_frac = Decimal("0")
        motivo = "sin_tearout"
    elif demo_frac >= 1:
        return money(0), money(0), base, oficio, "oficio_demolicion"
    else:
        motivo = "split_xactimate"

    demo = money(base * demo_frac)
    resto = base - demo
    material = money(resto * mat_resto)
    labor = resto - material
    return material, labor, demo, oficio, motivo


def necesita_ajuste(row, material, labor, demo, motivo, forzar):
    base = money(row.get("precio_base"))
    if base <= 0:
        return False, "sin_precio_base"
    actual_m = money(row.get("precio_material"))
    actual_l = money(row.get("precio_mano_obra"))
    actual_d = money(row.get("precio_remocion"))
    suma = actual_m + actual_l + actual_d
    if forzar and motivo in ("split_xactimate", "sin_tearout", "oficio_demolicion"):
        return True, "force"
    if suma != base:
        return True, "suma_descuadrada"
    if motivo.startswith("exclusivo") and (actual_m, actual_l, actual_d) != (material, labor, demo):
        return True, motivo
    if motivo == "oficio_demolicion" and actual_d != base:
        return True, "demo_incompleto"
    if motivo == "split_xactimate" and actual_d == 0 and demo > 0:
        return True, "remocion_cero"
    if actual_m == 0 and material > 0 and motivo == "split_xactimate":
        return True, "material_cero"
    if actual_l == 0 and labor > 0 and motivo == "split_xactimate":
        return True, "labor_cero"
    if motivo == "split_xactimate":
        resto_actual = actual_m + actual_l
        resto_nuevo = material + labor
        if resto_actual > 0 and resto_nuevo > 0:
            esperado = material / resto_nuevo
            actual = actual_m / resto_actual
            if abs(actual - esperado) > Decimal("0.25"):
                return True, "split_irreal"
    if (actual_m, actual_l, actual_d) != (material, labor, demo) and abs(suma - base) <= CENTAVO:
        if actual_d > money(base * Decimal("0.40")) and motivo == "split_xactimate":
            return True, "remocion_inflada"
        if actual_m > 0 and actual_l > 0 and actual_d == demo == 0:
            return False, "ok_sin_demo"
    if (actual_m, actual_l, actual_d) == (material, labor, demo):
        return False, "ok"
    return False, "ok_identidad"


def cliente_supabase():
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SECRET_KEY")
        or ""
    ).strip()
    if not url or not key:
        print("Faltan SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY en .env")
        sys.exit(1)
    import httpx
    from supabase import create_client

    try:
        from supabase import ClientOptions
    except ImportError:
        from supabase.lib.client_options import ClientOptions
    http = httpx.Client(verify=False, timeout=60.0)
    try:
        return create_client(url, key, options=ClientOptions(httpx_client=http))
    except TypeError:
        return create_client(url, key)


def leer_catalogo(sb, solo_activos):
    filas = []
    offset = 0
    columnas = "id,codigo,descripcion,unidad,categoria,precio_base,precio_material,precio_mano_obra,precio_remocion,activo"
    while True:
        query = sb.table(TABLA).select(columnas).order("id").range(offset, offset + TAMANO_PAGINA - 1)
        if solo_activos:
            query = query.eq("activo", True)
        lote = list((query.execute().data) or [])
        filas.extend(lote)
        print(f"Leídas {len(filas)} filas de {TABLA}...")
        if len(lote) < TAMANO_PAGINA:
            break
        offset += TAMANO_PAGINA
    return filas


def aplicar_updates(sb, cambios):
    escritos = 0
    for i in range(0, len(cambios), TAMANO_UPDATE):
        lote = cambios[i : i + TAMANO_UPDATE]
        for row in lote:
            sb.table(TABLA).update(
                {
                    "precio_material": float(row["nuevo_material"]),
                    "precio_mano_obra": float(row["nuevo_labor"]),
                    "precio_remocion": float(row["nuevo_demo"]),
                }
            ).eq("id", row["id"]).execute()
            escritos += 1
        print(f"Actualizados {escritos}/{len(cambios)}...")
    return escritos


def main():
    parser = argparse.ArgumentParser(description="Corrige desglose Xactimate de catalogo_items")
    parser.add_argument("--apply", action="store_true", help="Escribe en Supabase (sin esto solo audita)")
    parser.add_argument("--force", action="store_true", help="Recalcula también ítems cuya suma ya cierra")
    parser.add_argument("--solo-activos", action="store_true", help="Ignora ítems inactivos")
    parser.add_argument("--limit", type=int, default=0, help="Máximo de filas a corregir")
    parser.add_argument("--csv", default="", help="Ruta CSV de auditoría")
    args = parser.parse_args()

    sb = cliente_supabase()
    filas = leer_catalogo(sb, args.solo_activos)
    cambios = []
    oficios = Counter()
    motivos = Counter()
    ok = 0
    sin_base = 0

    for row in filas:
        base = money(row.get("precio_base"))
        if base <= 0:
            sin_base += 1
            continue
        material, labor, demo, oficio, origen = desglose_objetivo(row)
        if material + labor + demo != base:
            labor = base - material - demo
        ajustar, motivo = necesita_ajuste(row, material, labor, demo, origen, args.force)
        oficios[oficio] += 1
        if not ajustar:
            ok += 1
            continue
        motivos[motivo] += 1
        cambios.append(
            {
                "id": row.get("id"),
                "codigo": row.get("codigo") or "",
                "categoria": row.get("categoria") or "",
                "descripcion": (row.get("descripcion") or "")[:180],
                "oficio": oficio,
                "motivo": motivo,
                "precio_base": f"{base:.2f}",
                "old_material": f"{money(row.get('precio_material')):.2f}",
                "old_labor": f"{money(row.get('precio_mano_obra')):.2f}",
                "old_demo": f"{money(row.get('precio_remocion')):.2f}",
                "nuevo_material": money(material),
                "nuevo_labor": money(labor),
                "nuevo_demo": money(demo),
            }
        )
        if args.limit and len(cambios) >= args.limit:
            break

    print()
    print(f"Ítems leídos: {len(filas)}")
    print(f"Sin precio_base: {sin_base}")
    print(f"Ya coherentes: {ok}")
    print(f"A corregir: {len(cambios)}")
    print("Motivos:", dict(motivos))
    print("Oficios en catálogo:", dict(oficios))
    if cambios:
        muestra = cambios[:8]
        print("\nMuestra:")
        for c in muestra:
            print(
                f"  [{c['codigo']}] {c['descripcion'][:70]} | "
                f"base {c['precio_base']} | "
                f"{c['old_material']}/{c['old_labor']}/{c['old_demo']} -> "
                f"{c['nuevo_material']:.2f}/{c['nuevo_labor']:.2f}/{c['nuevo_demo']:.2f} "
                f"({c['oficio']}, {c['motivo']})"
            )

    if args.csv:
        ruta = Path(args.csv)
        with ruta.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "id",
                    "codigo",
                    "categoria",
                    "descripcion",
                    "oficio",
                    "motivo",
                    "precio_base",
                    "old_material",
                    "old_labor",
                    "old_demo",
                    "nuevo_material",
                    "nuevo_labor",
                    "nuevo_demo",
                ],
            )
            writer.writeheader()
            for c in cambios:
                writer.writerow(
                    {
                        **c,
                        "nuevo_material": f"{c['nuevo_material']:.2f}",
                        "nuevo_labor": f"{c['nuevo_labor']:.2f}",
                        "nuevo_demo": f"{c['nuevo_demo']:.2f}",
                    }
                )
        print(f"CSV: {ruta.resolve()}")

    if not args.apply:
        print("\nDry-run. Revisa el reporte y corre con --apply para escribir.")
        return 0

    escritos = aplicar_updates(sb, cambios)
    print(f"\nEscritos {escritos} ítems. precio_base no se modificó.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
