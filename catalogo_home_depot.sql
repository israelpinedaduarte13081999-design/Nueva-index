-- Foto y frescura de precio para catalogo_items (Home Depot / Apify).
-- Ejecuta este script en el SQL Editor de Supabase si las columnas aún no existen.

alter table public.catalogo_items
  add column if not exists image_url text,
  add column if not exists last_updated timestamptz,
  add column if not exists proveedor text;

comment on column public.catalogo_items.image_url is
  'URL de la foto del material (Home Depot u otra fuente).';
comment on column public.catalogo_items.last_updated is
  'Última actualización de precio o foto desde el scraper.';
comment on column public.catalogo_items.proveedor is
  'Catálogo de origen: Home Depot, ABC Supply, Sherwin-Williams, Catalog, etc.';
