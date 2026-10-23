-- Precios públicos de proveedores (Home Depot, ABC Supply, Sherwin-Williams, etc.).
-- No es caché por usuario: una sola fila por proveedor + producto + presentación + unidad + ZIP + tienda.
-- Ejecuta este script en el SQL Editor de Supabase.

create table if not exists public.catalogo_proveedor_precios (
  id uuid primary key default gen_random_uuid(),
  proveedor text not null,
  producto_id text not null default '',
  consulta_clave text not null default '',
  presentacion text not null default '',
  unidad_original text not null default '',
  unidad_normalizada text not null default '',
  zip_code text not null,
  tienda_id text not null default '',
  tienda_nombre text,
  nombre text,
  product_url text,
  image_url text,
  precio_original numeric,
  moneda text not null default 'USD',
  precio_normalizado numeric,
  cobertura_sf numeric,
  last_updated timestamptz,
  image_updated timestamptz,
  created_at timestamptz not null default now()
);

create unique index if not exists catalogo_proveedor_precios_unico
  on public.catalogo_proveedor_precios (
    proveedor, producto_id, presentacion, unidad_normalizada, zip_code, tienda_id
  );

create index if not exists catalogo_proveedor_precios_consulta
  on public.catalogo_proveedor_precios (proveedor, consulta_clave, zip_code, last_updated desc);

create table if not exists public.catalogo_proveedor_locks (
  clave text primary key,
  locked_at timestamptz not null default now(),
  expires_at timestamptz not null
);

alter table public.catalogo_proveedor_precios enable row level security;
alter table public.catalogo_proveedor_locks enable row level security;

comment on table public.catalogo_proveedor_precios is
  'Precio público de material por proveedor, producto, presentación, unidad, ZIP y tienda. Lectura no renueva last_updated.';
comment on column public.catalogo_proveedor_precios.last_updated is
  'Frescura del precio (15 días). No se toca al leer.';
comment on column public.catalogo_proveedor_precios.image_updated is
  'Frescura de la imagen (independiente del precio).';
comment on table public.catalogo_proveedor_locks is
  'Candado entre instancias para una sola consulta externa por producto+ZIP.';

grant select on public.catalogo_proveedor_precios to anon, authenticated, service_role;
grant all on public.catalogo_proveedor_precios to service_role;
grant all on public.catalogo_proveedor_locks to service_role;

drop policy if exists lectura_precios_proveedor on public.catalogo_proveedor_precios;
create policy lectura_precios_proveedor
  on public.catalogo_proveedor_precios
  for select
  using (true);
