import app
from oficios import _descargar_filas_catalogo, invalidar_catalogo

sb = app._supabase_client()
print("sb", bool(sb), "rol", app._SB_ROL)

# counts via rest
try:
    r = sb.table("catalogo_items").select("id", count="exact").limit(1).execute()
    print("total", getattr(r, "count", None), "sample", len(r.data or []))
except Exception as e:
    print("count_fail", type(e).__name__, str(e)[:240])

try:
    r = sb.table("catalogo_items").select("id", count="exact").eq("activo", True).limit(1).execute()
    print("activo_true", getattr(r, "count", None))
except Exception as e:
    print("activo_fail", type(e).__name__, str(e)[:240])

try:
    r = sb.table("catalogo_items").select("id,descripcion,activo,codigo").order("id", desc=True).limit(8).execute()
    for row in r.data or []:
        desc = str(row.get("descripcion") or "")[:70]
        print("nuevo", row.get("id"), row.get("activo"), row.get("codigo"), desc)
except Exception as e:
    print("latest_fail", type(e).__name__, str(e)[:240])

# embedding null count: fetch pages of ids without embedding is hard; sample
try:
    r = sb.table("catalogo_items").select("id").is_("embedding", "null").limit(5).execute()
    print("sin_embedding_sample", len(r.data or []))
except Exception as e:
    print("emb_fail", type(e).__name__, str(e)[:240])

filas, error = _descargar_filas_catalogo()
print("download", len(filas), "error", error)
if filas:
    print("first", filas[0].get("codigo"), str(filas[0].get("descripcion") or "")[:50])
    print("last", filas[-1].get("codigo"), str(filas[-1].get("descripcion") or "")[:50])
