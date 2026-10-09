from app import app


def ver(q):
    resp = app.test_client().get("/api/buscar-catalogo?q=" + q)
    data = resp.get_json(silent=True) or {}
    items = data.get("items") or []
    print(f"\n{q}: {resp.status_code} idioma={data.get('idioma')} n={len(items)}")
    vistos = 0
    for it in items:
        otro = it.get("descripcion_en") if data.get("idioma") == "es" else it.get("descripcion_es")
        if not otro or otro == it.get("desc"):
            continue
        print(" ", str(it.get("desc") or "")[:110])
        print("   <-", str(otro)[:110])
        vistos += 1
        if vistos >= 4:
            break


if __name__ == "__main__":
    ver("flor")
    ver("pizos")
