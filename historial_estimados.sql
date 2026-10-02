-- Tabla base del historial.
-- Las descripciones de partidas se guardan dentro de items/areas/snapshot (jsonb),
-- que no tiene límite VARCHAR: el texto largo del WORK DESCRIPTION se persiste completo.
-- Después de crearla, ejecuta multitenant.sql (contratista_id, RLS y funciones).
--
-- Si alguna instalación antigua usó VARCHAR corto en un campo de descripción:
--   alter table public.historial_estimados
--     alter column items type jsonb using items::jsonb,
--     alter column areas type jsonb using areas::jsonb,
--     alter column snapshot type jsonb using snapshot::jsonb,
--     alter column notas type text,
--     alter column terminos type text;
create table if not exists public.historial_estimados (
  id uuid primary key default gen_random_uuid(),
  folio text,
  tipo text not null default 'estimate',
  cliente_nombre text,
  fecha date,
  total numeric,
  estado text default 'pending',
  cliente jsonb,
  items jsonb,
  areas jsonb,
  snapshot jsonb,
  notas text,
  terminos text,
  firmas jsonb,
  payload jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists historial_estimados_updated_idx on public.historial_estimados (updated_at desc);
create index if not exists historial_estimados_folio_idx on public.historial_estimados (folio);
create index if not exists historial_estimados_tipo_idx on public.historial_estimados (tipo);

alter table public.historial_estimados
  add column if not exists contratista_id uuid;

alter table public.historial_estimados
  add column if not exists token_aceptacion uuid;

create index if not exists historial_estimados_contratista_idx
  on public.historial_estimados (contratista_id);
