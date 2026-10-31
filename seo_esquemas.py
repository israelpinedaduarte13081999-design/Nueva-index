"""JSON-LD helpers for QuantixBuilds feature pages, HowTo, and VideoObject."""
from __future__ import annotations

import os
import re

_SLUG_OK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

PASOS_FLUJO_CORE = (
    {
        "name": "Step 1: Upload your plan",
        "text": "Upload your PDF construction plan to QuantixBuilds so the job is ready for takeoff.",
    },
    {
        "name": "Step 2: Extract measurements with AI",
        "text": "QuantixBuilds extracts measurements from the drawings with AI and builds quantities for the estimate.",
    },
    {
        "name": "Step 3: Export the signed PDF contract",
        "text": "Apply local ZIP-code pricing, then export the construction contract as a signed PDF from the same job.",
    },
)


def _pasos(items):
    out = []
    for raw in items or ():
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        text = str(raw.get("text") or "").strip()
        if name and text:
            out.append({"name": name, "text": text})
    return out


def schema_howto(name, description, url, pasos=None):
    steps = _pasos(pasos) or list(PASOS_FLUJO_CORE)
    return {
        "@type": "HowTo",
        "name": str(name or "").strip(),
        "description": str(description or "").strip(),
        "url": str(url or "").strip(),
        "inLanguage": "en-US",
        "step": [
            {
                "@type": "HowToStep",
                "position": indice,
                "name": paso["name"],
                "text": paso["text"],
            }
            for indice, paso in enumerate(steps, 1)
        ],
    }


def howto_visible(name, description, pasos=None):
    steps = _pasos(pasos) or list(PASOS_FLUJO_CORE)
    return {
        "name": str(name or "").strip(),
        "description": str(description or "").strip(),
        "pasos": steps,
    }


def howto_industria(fila, canonical):
    nombre = str((fila or {}).get("name") or "construction").strip()
    clave = nombre.lower()
    pasos = (
        {
            "name": "Step 1: Upload your plan",
            "text": f"Upload the PDF {clave} plan into QuantixBuilds so the takeoff starts from the drawings.",
        },
        {
            "name": "Step 2: Extract measurements with AI",
            "text": f"QuantixBuilds extracts measurements from the {clave} plans with AI and turns them into estimate quantities.",
        },
        {
            "name": "Step 3: Export the signed PDF contract",
            "text": f"Price the {clave} scope with local ZIP-code databases, then export the contract as a signed PDF.",
        },
    )
    return howto_visible(
        f"How to create a {clave} estimate with QuantixBuilds",
        f"Create a {clave} estimate in QuantixBuilds: upload the plan, extract measurements with AI, and export a signed PDF contract.",
        pasos,
    )


def schema_video_demo(origin):
    base = str(origin or "https://www.quantixbuilds.com").rstrip("/")
    content = (os.environ.get("QUANTIX_DEMO_VIDEO_URL") or "").strip() or f"{base}/static/demo-dashboard.mp4"
    embed = (os.environ.get("QUANTIX_DEMO_VIDEO_EMBED") or "").strip()
    bloque = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": "QuantixBuilds Dashboard Demo",
        "description": (
            "A visual walkthrough of the QuantixBuilds contractor dashboard: "
            "AI plan takeoff, ZIP-code price databases, estimates, digital signatures, and PDF invoices."
        ),
        "thumbnailUrl": f"{base}/static/apple-touch-icon.png",
        "uploadDate": "2026-10-30",
        "contentUrl": content,
        "publisher": {
            "@type": "Organization",
            "name": "QuantixBuilds",
            "url": base,
        },
    }
    if embed:
        bloque["embedUrl"] = embed
    return bloque


def _fila_caracteristica(raw):
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("slug") or "").strip().lower()
    if not slug or not _SLUG_OK.match(slug):
        return None
    path = str(raw.get("path") or f"/{slug}").strip()
    if not path.startswith("/"):
        path = "/" + path
    pasos = _pasos(raw.get("pasos")) or list(PASOS_FLUJO_CORE)
    return {
        "slug": slug,
        "path": path,
        "title": str(raw.get("title") or "").strip(),
        "h1": str(raw.get("h1") or "").strip(),
        "meta": str(raw.get("meta") or "").strip(),
        "lead": str(raw.get("lead") or "").strip(),
        "kicker": str(raw.get("kicker") or "QuantixBuilds feature").strip(),
        "card_title": str(raw.get("card_title") or "").strip(),
        "card_body": str(raw.get("card_body") or "").strip(),
        "howto_name": str(raw.get("howto_name") or "").strip(),
        "howto_description": str(raw.get("howto_description") or "").strip(),
        "pasos": pasos,
        "priority": str(raw.get("priority") or "0.9"),
        "changefreq": str(raw.get("changefreq") or "weekly"),
    }


_CARACTERISTICAS_RAW = (
    {
        "slug": "pricing",
        "path": "/pricing",
        "kicker": "Pricing",
        "title": "Construction Software Pricing | QuantixBuilds",
        "h1": "QuantixBuilds pricing for contractor estimating software",
        "meta": "See QuantixBuilds pricing for construction estimating, ZIP-code databases, AI takeoff, contracts, and digital signatures. Start a 14-day free trial.",
        "lead": "QuantixBuilds pricing is built around the contractor workflow: upload a plan, extract measurements with AI, apply local ZIP-code prices, and export signed PDF contracts and invoices. Start with a 14-day free trial. No card required today.",
        "card_title": "Plans for every contractor workflow",
        "card_body": "Choose Basic, Professional, or Premium when you create your account. Catalog pricing, AI takeoff, and document automation unlock with the matching plan.",
        "howto_name": "How to start QuantixBuilds and export a signed construction contract",
        "howto_description": "Start QuantixBuilds, upload a plan, extract measurements with AI, and export a signed PDF contract from the same job.",
    },
    {
        "slug": "construction-estimates",
        "path": "/construction-estimates",
        "kicker": "Estimates",
        "title": "Construction Estimating Software | QuantixBuilds",
        "h1": "Create construction estimates from plans in QuantixBuilds",
        "meta": "QuantixBuilds construction estimating software extracts measurements from PDF plans with AI, prices by ZIP code, and exports invoices and signed contracts.",
        "lead": "Build a client-ready construction estimate without a spreadsheet. QuantixBuilds reads the plan, prices the scope from local databases, and keeps the contract and invoice on the same job.",
        "card_title": "From takeoff to billed invoice",
        "card_body": "Quantities, labor, materials, tax, and a grand total stay together so the approved estimate becomes the contract and the invoice.",
        "howto_name": "How to create a construction estimate with QuantixBuilds",
        "howto_description": "Create a construction estimate in QuantixBuilds: upload the plan, extract measurements with AI, and export a signed PDF contract.",
    },
    {
        "slug": "digital-signatures",
        "path": "/digital-signatures",
        "kicker": "Digital signatures",
        "title": "Construction Contract Digital Signatures | QuantixBuilds",
        "h1": "Send construction contracts for digital signature",
        "meta": "QuantixBuilds generates construction contracts from the approved estimate and sends them for digital signature, then exports signed PDFs and invoices.",
        "lead": "After the scope is priced, QuantixBuilds turns the estimate into a construction contract you can send for e-signature and export as a signed PDF.",
        "card_title": "Contracts that match the approved scope",
        "card_body": "Digital signature sits in the same workflow as AI takeoff and ZIP-code pricing, so the signed contract matches the billed invoice.",
        "howto_name": "How to send a construction contract for digital signature",
        "howto_description": "Upload the plan, extract measurements with AI, then export the construction contract as a signed PDF in QuantixBuilds.",
    },
    {
        "slug": "zip-code-pricing",
        "path": "/zip-code-pricing",
        "kicker": "ZIP-code databases",
        "title": "ZIP Code Construction Pricing | QuantixBuilds",
        "h1": "Local construction pricing by ZIP code",
        "meta": "QuantixBuilds prices remodeling and construction jobs from local ZIP-code databases, after AI plan takeoff, then exports estimates, contracts, and PDF invoices.",
        "lead": "Enter the job ZIP code and QuantixBuilds applies local material and labor databases to the quantities extracted from your plans.",
        "card_title": "Local databases, not a national average",
        "card_body": "ZIP-code pricing stays attached to the estimate so the signed contract and PDF invoice reflect the same local costs.",
        "howto_name": "How to price a remodeling job by ZIP code in QuantixBuilds",
        "howto_description": "Upload the plan, extract measurements with AI, apply the ZIP-code price database, and export a signed PDF contract.",
    },
)

_CARACTERISTICAS = tuple(
    fila for fila in (_fila_caracteristica(raw) for raw in _CARACTERISTICAS_RAW) if fila
)


def listar_caracteristicas():
    return _CARACTERISTICAS


def caracteristica_por_slug(slug):
    clave = str(slug or "").strip().lower().lstrip("/")
    if not _SLUG_OK.match(clave or ""):
        return None
    for fila in _CARACTERISTICAS:
        if fila["slug"] == clave:
            return fila
    return None


def urls_sitemap_caracteristicas():
    return tuple((fila["path"], fila["changefreq"], fila["priority"]) for fila in _CARACTERISTICAS)
