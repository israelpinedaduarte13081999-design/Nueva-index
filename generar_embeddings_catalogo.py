"""Genera embeddings de catalogo_items con OpenAI (text-embedding-3-small).

Requisitos:
  1. Ejecuta catalogo_vector.sql en el SQL Editor de Supabase.
  2. Añade OPENAI_API_KEY a .env (no se sube a git).

Uso:
  python generar_embeddings_catalogo.py
  python generar_embeddings_catalogo.py --force
"""

import argparse
import sys

import app

TAMANO_LOTE = 80
TAMANO_PAGINA = 500


def _filas_pendientes(sb, forzar):
    offset = 0
    while True:
        query = sb.table(app.TABLA_CATALOGO).select("id,descripcion,unidad,embedding").order("id").range(
            offset, offset + TAMANO_PAGINA - 1
        )
        lote = query.execute().data or []
        if not lote:
            break
        for row in lote:
            if not forzar and row.get("embedding"):
                continue
            if not str(row.get("descripcion") or "").strip():
                continue
            yield row
        if len(lote) < TAMANO_PAGINA:
            break
        offset += TAMANO_PAGINA


def _guardar_embedding(sb, item_id, vector):
    try:
        sb.rpc("set_item_embedding", {"item_id": int(item_id), "item_embedding": vector}).execute()
        return
    except Exception as err_rpc:
        print(f"--> set_item_embedding id={item_id}: {err_rpc}")
    sb.table(app.TABLA_CATALOGO).update({"embedding": vector}).eq("id", item_id).execute()


def main():
    parser = argparse.ArgumentParser(description="Rellena catalogo_items.embedding")
    parser.add_argument("--force", action="store_true", help="Regenera embeddings aunque ya existan")
    args = parser.parse_args()
    if not app._openai_api_key():
        print("Falta OPENAI_API_KEY en .env")
        return 1
    sb = app._supabase_client()
    if sb is None:
        print("No hay cliente Supabase. Revisa SUPABASE_URL y SUPABASE_KEY.")
        return 1
    pendientes = list(_filas_pendientes(sb, args.force))
    print(f"--> {len(pendientes)} filas para embeber")
    ok = 0
    for i in range(0, len(pendientes), TAMANO_LOTE):
        lote = pendientes[i : i + TAMANO_LOTE]
        textos = [app._texto_para_embedding(r.get("descripcion"), r.get("unidad")) for r in lote]
        try:
            vectores = app._embeddings_openai(textos)
        except Exception as err:
            print(f"--> lote {i}: {err}")
            if "insufficient_quota" in str(err) or "credit_balance_exhausted" in str(err):
                print("--> OpenAI sin credito. Anade saldo y vuelve a ejecutar este script.")
                return 1
            continue
        for row, vector in zip(lote, vectores):
            if not vector:
                print(f"--> sin vector id={row.get('id')}")
                continue
            try:
                _guardar_embedding(sb, row["id"], vector)
                ok += 1
            except Exception as err:
                print(f"--> update id={row.get('id')}: {err}")
        print(f"--> progreso {min(i + TAMANO_LOTE, len(pendientes))}/{len(pendientes)}")
    print(f"--> embeddings guardados: {ok}")
    return 0 if ok or not pendientes else 1


if __name__ == "__main__":
    sys.exit(main())
