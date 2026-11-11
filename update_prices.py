"""Actualización mensual de precios de Home Depot ya guardados en Supabase.

Uso:
  python update_prices.py
  python update_prices.py --dry-run
  python update_prices.py --limit 40

También expuesto en Flask como POST/GET /api/cron/update-prices
con cabecera X-Cron-Secret (o Authorization: Bearer) igual a CRON_SECRET.
"""

from __future__ import annotations

import json
import sys


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    dry_run = "--dry-run" in args
    limite = 0
    if "--limit" in args:
        idx = args.index("--limit")
        try:
            limite = int(args[idx + 1])
        except (IndexError, ValueError):
            limite = 0
    from app import actualizar_precios_home_depot
    resumen = actualizar_precios_home_depot(dry_run=dry_run, limite=limite)
    print(json.dumps(resumen, ensure_ascii=True))
    return 0 if resumen.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
