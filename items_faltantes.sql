-- Ejecutar en Supabase → SQL Editor (una sola vez).
create table if not exists public.items_faltantes (
  id uuid primary key default gen_random_uuid(),
  termino text not null,
  termino_norm text not null unique,
  oficio text,
  zip_code text,
  veces integer not null default 1,
  fecha date not null default (now() at time zone 'utc')::date,
  primera_busqueda timestamptz not null default now(),
  ultima_busqueda timestamptz not null default now()
);

create index if not exists items_faltantes_fecha_idx on public.items_faltantes (fecha desc);
create index if not exists items_faltantes_ultima_idx on public.items_faltantes (ultima_busqueda desc);

alter table public.items_faltantes enable row level security;

drop policy if exists items_faltantes_select on public.items_faltantes;
drop policy if exists items_faltantes_insert on public.items_faltantes;
drop policy if exists items_faltantes_update on public.items_faltantes;

create policy items_faltantes_select on public.items_faltantes
  for select to anon, authenticated using (true);
create policy items_faltantes_insert on public.items_faltantes
  for insert to anon, authenticated with check (true);
create policy items_faltantes_update on public.items_faltantes
  for update to anon, authenticated using (true) with check (true);
