-- Pegar en Supabase → SQL Editor → Run
-- RLS multi-tenant: cada usuario solo ve filas con user_id = auth.uid().
--
-- En esta app no hay tablas separadas "estimados", "facturas" ni "clientes".
-- Esos documentos viven en public.historial_estimados (columna tipo:
-- estimate / invoice / contract) y el cliente va en la columna jsonb cliente.
-- El catálogo (catalogo_items, catalogo_trabajos, factores) es compartido:
-- no se aísla por usuario.

alter table public.contratistas
  add column if not exists user_id uuid references auth.users(id) on delete cascade;

alter table public.historial_estimados
  add column if not exists user_id uuid references auth.users(id) on delete cascade;

create unique index if not exists contratistas_user_id_uidx
  on public.contratistas (user_id)
  where user_id is not null;

create index if not exists historial_estimados_user_id_idx
  on public.historial_estimados (user_id);

-- Vincular cuentas ya existentes por email (Auth → contratista → documentos).
update public.contratistas c
set user_id = u.id
from auth.users u
where c.user_id is null
  and lower(c.email) = lower(u.email);

update public.historial_estimados h
set user_id = c.user_id
from public.contratistas c
where h.contratista_id = c.id
  and h.user_id is null
  and c.user_id is not null;

alter table public.contratistas enable row level security;
alter table public.historial_estimados enable row level security;
alter table public.perfiles enable row level security;

revoke all on table public.historial_estimados from anon, public;
revoke all on table public.contratistas from anon, public;

grant select, insert, update, delete on table public.historial_estimados to authenticated;
grant select, insert, update, delete on table public.historial_estimados to service_role;

grant select (id, email, nombre_empresa, user_id, created_at, updated_at)
  on table public.contratistas to authenticated;
grant select, insert, update on table public.contratistas to service_role;

-- El cliente no puede leer el token de sesión de contratistas.
revoke select (token) on table public.contratistas from authenticated, anon;

-- historial_estimados: solo filas del usuario autenticado
drop policy if exists historial_select_own on public.historial_estimados;
drop policy if exists historial_insert_own on public.historial_estimados;
drop policy if exists historial_update_own on public.historial_estimados;
drop policy if exists historial_delete_own on public.historial_estimados;
drop policy if exists historial_service_all on public.historial_estimados;

create policy historial_select_own
  on public.historial_estimados
  for select
  to authenticated
  using (user_id = auth.uid());

create policy historial_insert_own
  on public.historial_estimados
  for insert
  to authenticated
  with check (user_id = auth.uid());

create policy historial_update_own
  on public.historial_estimados
  for update
  to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

create policy historial_delete_own
  on public.historial_estimados
  for delete
  to authenticated
  using (user_id = auth.uid());

create policy historial_service_all
  on public.historial_estimados
  for all
  to service_role
  using (true)
  with check (true);

-- contratistas: el usuario autenticado solo ve su propia fila (sin token)
drop policy if exists contratistas_select_own on public.contratistas;
drop policy if exists contratistas_service_select on public.contratistas;
drop policy if exists contratistas_service_insert on public.contratistas;
drop policy if exists contratistas_service_update on public.contratistas;

create policy contratistas_select_own
  on public.contratistas
  for select
  to authenticated
  using (user_id = auth.uid());

create policy contratistas_service_select
  on public.contratistas
  for select
  to service_role
  using (true);

create policy contratistas_service_insert
  on public.contratistas
  for insert
  to service_role
  with check (
    email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'
    and token is not null
    and char_length(token) >= 32
  );

create policy contratistas_service_update
  on public.contratistas
  for update
  to service_role
  using (true)
  with check (
    email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'
    and token is not null
  );

-- perfiles: id = auth.users.id
drop policy if exists perfiles_own_select on public.perfiles;
drop policy if exists perfiles_own_insert on public.perfiles;
drop policy if exists perfiles_own_update on public.perfiles;
drop policy if exists perfiles_own_delete on public.perfiles;
drop policy if exists perfiles_service_all on public.perfiles;

create policy perfiles_own_select
  on public.perfiles
  for select
  to authenticated
  using (id = auth.uid());

create policy perfiles_own_insert
  on public.perfiles
  for insert
  to authenticated
  with check (id = auth.uid());

create policy perfiles_own_update
  on public.perfiles
  for update
  to authenticated
  using (id = auth.uid())
  with check (id = auth.uid());

create policy perfiles_own_delete
  on public.perfiles
  for delete
  to authenticated
  using (id = auth.uid());

create policy perfiles_service_all
  on public.perfiles
  for all
  to service_role
  using (true)
  with check (true);

-- Fuerza user_id = auth.uid() cuando hay sesión de Auth.
-- Con service_role (Flask) toma el user_id del contratista vinculado.
create or replace function public.forzar_user_id_historial()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.uid() is not null then
    new.user_id := auth.uid();
  elsif new.user_id is null and new.contratista_id is not null then
    select c.user_id into new.user_id
    from public.contratistas c
    where c.id = new.contratista_id;
  end if;
  if tg_op = 'UPDATE' and old.user_id is not null and auth.uid() is not null then
    new.user_id := old.user_id;
  end if;
  return new;
end;
$$;

drop trigger if exists historial_forzar_user_id on public.historial_estimados;
create trigger historial_forzar_user_id
  before insert or update on public.historial_estimados
  for each row execute procedure public.forzar_user_id_historial();

create or replace function public.registrar_contratista(p_email text, p_nombre text, p_user_id uuid default null)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_email text := lower(trim(coalesce(p_email, '')));
  v_nombre text := nullif(trim(coalesce(p_nombre, '')), '');
  v_id uuid;
  v_token text;
  v_uid uuid := p_user_id;
begin
  if v_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' then
    raise exception 'invalid_contractor';
  end if;
  if exists (select 1 from public.contratistas where email = v_email) then
    raise exception 'already_registered';
  end if;
  if v_uid is null then
    select u.id into v_uid from auth.users u where lower(u.email) = v_email limit 1;
  end if;
  v_token := replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '');
  insert into public.contratistas (nombre_empresa, email, token, user_id)
  values (v_nombre, v_email, v_token, v_uid)
  returning id into v_id;
  return jsonb_build_object(
    'id', v_id,
    'email', v_email,
    'nombre_empresa', v_nombre,
    'token', v_token,
    'user_id', v_uid
  );
end;
$$;

create or replace function public.contratista_por_token(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.contratistas%rowtype;
begin
  if coalesce(trim(p_token), '') = '' then
    return null;
  end if;
  select * into v_row from public.contratistas where token = trim(p_token);
  if not found then
    return null;
  end if;
  return jsonb_build_object(
    'id', v_row.id,
    'email', v_row.email,
    'nombre_empresa', v_row.nombre_empresa,
    'user_id', v_row.user_id
  );
end;
$$;

create or replace function public.guardar_historial_tenant(p_token text, p_fila jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cid uuid;
  v_uid uuid;
  v_id uuid;
  v_existente uuid;
  v_token_aceptacion uuid;
  v_tipo text;
  v_row public.historial_estimados%rowtype;
begin
  select id, user_id into v_cid, v_uid
  from public.contratistas
  where token = trim(coalesce(p_token, ''));
  if v_cid is null then
    raise exception 'unauthorized';
  end if;

  v_tipo := lower(coalesce(nullif(p_fila->>'tipo', ''), 'estimate'));
  if coalesce(p_fila->>'id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    v_id := (p_fila->>'id')::uuid;
  end if;

  if v_id is not null then
    select id into v_existente
    from public.historial_estimados
    where id = v_id and contratista_id = v_cid;
  end if;

  if v_existente is null and coalesce(p_fila->>'folio', '') <> '' then
    select id into v_existente
    from public.historial_estimados
    where folio = p_fila->>'folio' and contratista_id = v_cid
    limit 1;
  end if;

  if v_existente is not null then
    select token_aceptacion into v_token_aceptacion
    from public.historial_estimados
    where id = v_existente;
  end if;

  if v_tipo in ('contract', 'contrato') and v_token_aceptacion is null then
    if coalesce(p_fila->>'token_aceptacion', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
      v_token_aceptacion := (p_fila->>'token_aceptacion')::uuid;
    else
      v_token_aceptacion := gen_random_uuid();
    end if;
  end if;

  if v_existente is not null then
    update public.historial_estimados set
      folio = nullif(p_fila->>'folio', ''),
      tipo = v_tipo,
      cliente_nombre = p_fila->>'cliente_nombre',
      fecha = nullif(p_fila->>'fecha', '')::date,
      total = nullif(p_fila->>'total', '')::numeric,
      estado = case
        when lower(coalesce(estado, '')) = 'accepted' then estado
        else coalesce(nullif(p_fila->>'estado', ''), estado, 'pending')
      end,
      cliente = p_fila->'cliente',
      items = coalesce(p_fila->'items', '[]'::jsonb),
      areas = p_fila->'areas',
      snapshot = p_fila->'snapshot',
      notas = p_fila->>'notas',
      terminos = p_fila->>'terminos',
      firmas = coalesce(p_fila->'firmas', '{}'::jsonb),
      payload = p_fila->'payload',
      contratista_id = v_cid,
      user_id = coalesce(v_uid, user_id),
      token_aceptacion = coalesce(v_token_aceptacion, token_aceptacion),
      updated_at = now()
    where id = v_existente and contratista_id = v_cid
    returning * into v_row;
  else
    insert into public.historial_estimados (
      folio, tipo, cliente_nombre, fecha, total, estado,
      cliente, items, areas, snapshot, notas, terminos, firmas, payload,
      contratista_id, user_id, token_aceptacion
    ) values (
      nullif(p_fila->>'folio', ''),
      v_tipo,
      p_fila->>'cliente_nombre',
      nullif(p_fila->>'fecha', '')::date,
      nullif(p_fila->>'total', '')::numeric,
      coalesce(nullif(p_fila->>'estado', ''), 'pending'),
      p_fila->'cliente',
      coalesce(p_fila->'items', '[]'::jsonb),
      p_fila->'areas',
      p_fila->'snapshot',
      p_fila->>'notas',
      p_fila->>'terminos',
      coalesce(p_fila->'firmas', '{}'::jsonb),
      p_fila->'payload',
      v_cid,
      v_uid,
      v_token_aceptacion
    )
    returning * into v_row;
  end if;

  return to_jsonb(v_row);
end;
$$;
