-- Pegar en Supabase → SQL Editor → Run
-- Extiende perfiles con nombre, plan y fecha de inicio de la prueba.

alter table public.perfiles add column if not exists nombre text not null default '';
alter table public.perfiles add column if not exists plan_seleccionado text not null default '';
alter table public.perfiles add column if not exists trial_inicio timestamptz;
alter table public.perfiles add column if not exists fecha_fin_prueba timestamptz;

grant select, insert, update on table public.perfiles to service_role;
grant select, insert, update on table public.perfiles to authenticated;

drop policy if exists perfiles_service_all on public.perfiles;
create policy perfiles_service_all
  on public.perfiles
  for all
  to service_role
  using (true)
  with check (true);

drop policy if exists perfiles_own_select on public.perfiles;
create policy perfiles_own_select
  on public.perfiles
  for select
  to authenticated
  using (id = auth.uid());

drop policy if exists perfiles_own_update on public.perfiles;
create policy perfiles_own_update
  on public.perfiles
  for update
  to authenticated
  using (id = auth.uid())
  with check (id = auth.uid());
