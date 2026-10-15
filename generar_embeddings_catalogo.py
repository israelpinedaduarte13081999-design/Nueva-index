"""Genera embeddings de catalogo_items con OpenAI (text-embedding-3-small).

El backend ya rellena solo los ítems sin vector (al arrancar y cada 10 minutos).
Este script sirve para hacerlo al momento o para regenerarlos todos.

Requisitos:
  1. Ejecuta catalogo_vector.sql en el SQL Editor de Supabase.
  2. Añade OPENAI_API_KEY a .env (no se sube a git).

Uso:
  python generar_embeddings_catalogo.py
  python generar_embeddings_catalogo.py --force
"""

import argparse
import os
import sys

os.environ["EMBEDDINGS_AUTO"] = "0"

import app


def main():
    parser = argparse.ArgumentParser(description="Rellena catalogo_items.embedding")
    parser.add_argument("--force", action="store_true", help="Regenera embeddings aunque ya existan")
    args = parser.parse_args()
    if not app._openai_api_key():
        print("Falta OPENAI_API_KEY en .env")
        return 1
    if app._supabase_client() is None:
        print("No hay cliente Supabase. Revisa SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY.")
        return 1
    ok, pendientes = app.rellenar_embeddings_catalogo(forzar=args.force)
    if not pendientes:
        print("--> Todos los ítems ya tienen embedding")
        return 0
    return 0 if ok == pendientes else 1


if __name__ == "__main__":
    sys.exit(main())
