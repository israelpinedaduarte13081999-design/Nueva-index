create extension if not exists pgcrypto;

create table if not exists public.contratistas (
  id uuid primary key default gen_random_uuid(),
  nombre_empresa text,
  email text not null unique,
  token text not null unique,
  user_id uuid references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.contratistas add column if not exists user_id uuid references auth.users(id) on delete cascade;

-- white-label: historial_estimados stores line-item WORK DESCRIPTION in jsonb
-- (items, areas, snapshot) — unlimited text, not VARCHAR.

create table if not exists public.historial_estimados (
  id uuid primary key default gen_random_uuid(),
  contratista_id uuid not null references public.contratistas(id),
  user_id uuid references auth.users(id) on delete cascade,
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
  token_aceptacion uuid unique,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists historial_estimados_updated_idx
  on public.historial_estimados (updated_at desc);

create index if not exists historial_estimados_folio_idx
  on public.historial_estimados (folio);

create index if not exists historial_estimados_tipo_idx
  on public.historial_estimados (tipo);

create index if not exists historial_estimados_contratista_idx
  on public.historial_estimados (contratista_id, updated_at desc);

alter table public.historial_estimados
  add column if not exists user_id uuid references auth.users(id) on delete cascade;

create unique index if not exists contratistas_user_id_uidx
  on public.contratistas (user_id)
  where user_id is not null;

create index if not exists historial_estimados_user_id_idx
  on public.historial_estimados (user_id);

alter table public.contratistas alter column nombre_empresa drop not null;

alter table public.historial_estimados enable row level security;
alter table public.contratistas enable row level security;

revoke all on table public.historial_estimados from anon, public;
revoke all on table public.contratistas from anon, public;

-- contratistas: RLS queda activo. anon/authenticated no tienen DML directo.
-- El 42501 "new row violates row-level security policy" aparece cuando el
-- INSERT REST no corre como service_role: no hay política que acepte la fila
-- (deny-all implícito) o una política authenticated con auth.uid() = id falla
-- porque contratistas.id no es auth.users.id.
grant select, insert, update, delete on table public.historial_estimados to authenticated;
grant select, insert, update, delete on table public.historial_estimados to service_role;
grant select (id, email, nombre_empresa, user_id, created_at, updated_at)
  on table public.contratistas to authenticated;
grant select, insert, update on table public.contratistas to service_role;
revoke select (token) on table public.contratistas from authenticated, anon;

drop policy if exists contratistas_select_own on public.contratistas;
drop policy if exists contratistas_service_select on public.contratistas;
drop policy if exists contratistas_service_insert on public.contratistas;
drop policy if exists contratistas_service_update on public.contratistas;
drop policy if exists "Enable insert for authenticated users only" on public.contratistas;
drop policy if exists "Enable read access for all users" on public.contratistas;
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

create or replace function public.actualizar_contratista(p_token text, p_email text, p_nombre text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_email text := nullif(lower(trim(coalesce(p_email, ''))), '');
  v_nombre text := nullif(trim(coalesce(p_nombre, '')), '');
  v_row public.contratistas%rowtype;
begin
  if v_email is not null and v_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' then
    raise exception 'invalid_contractor';
  end if;
  update public.contratistas
    set email = coalesce(v_email, email),
        nombre_empresa = coalesce(v_nombre, nombre_empresa),
        updated_at = now()
    where token = trim(coalesce(p_token, ''))
    returning * into v_row;
  if not found then
    raise exception 'unauthorized';
  end if;
  return jsonb_build_object(
    'id', v_row.id,
    'email', v_row.email,
    'nombre_empresa', v_row.nombre_empresa
  );
end;
$$;

create or replace function public.listar_historial_tenant(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cid uuid;
begin
  select id into v_cid from public.contratistas where token = trim(coalesce(p_token, ''));
  if v_cid is null then
    raise exception 'unauthorized';
  end if;
  return coalesce((
    select jsonb_agg(to_jsonb(h))
    from (
      select *
      from public.historial_estimados
      where contratista_id = v_cid
      order by updated_at desc
      limit 100
    ) h
  ), '[]'::jsonb);
end;
$$;

create or replace function public.obtener_historial_tenant(p_token text, p_ident text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cid uuid;
  v_row public.historial_estimados%rowtype;
  v_ident text := trim(coalesce(p_ident, ''));
  v_found boolean := false;
begin
  select id into v_cid from public.contratistas where token = trim(coalesce(p_token, ''));
  if v_cid is null then
    raise exception 'unauthorized';
  end if;
  if v_ident = '' then
    return null;
  end if;
  if v_ident ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    select * into v_row
    from public.historial_estimados
    where id = v_ident::uuid and contratista_id = v_cid;
    v_found := found;
  end if;
  if not v_found then
    select * into v_row
    from public.historial_estimados
    where folio = v_ident and contratista_id = v_cid
    limit 1;
    v_found := found;
  end if;
  if not v_found then
    return null;
  end if;
  return to_jsonb(v_row);
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

create or replace function public.leer_contrato_publico(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_doc public.historial_estimados%rowtype;
  v_contratista public.contratistas%rowtype;
begin
  if coalesce(p_token, '') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    return null;
  end if;
  select * into v_doc
  from public.historial_estimados
  where token_aceptacion = p_token::uuid
  limit 1;
  if not found then
    return null;
  end if;
  if lower(coalesce(v_doc.tipo, '')) not in ('contract', 'contrato') then
    return null;
  end if;
  select * into v_contratista from public.contratistas where id = v_doc.contratista_id;
  return jsonb_build_object(
    'id', v_doc.id,
    'folio', v_doc.folio,
    'tipo', v_doc.tipo,
    'cliente_nombre', v_doc.cliente_nombre,
    'total', v_doc.total,
    'estado', v_doc.estado,
    'fecha', v_doc.fecha,
    'cliente_email', coalesce(v_doc.cliente->>'email', ''),
    'contratista_id', v_doc.contratista_id,
    'contratista_email', v_contratista.email,
    'nombre_empresa', v_contratista.nombre_empresa
  );
end;
$$;

create or replace function public.marcar_contrato_aceptado(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_doc public.historial_estimados%rowtype;
  v_contratista public.contratistas%rowtype;
begin
  if coalesce(p_token, '') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    return null;
  end if;
  select * into v_doc
  from public.historial_estimados
  where token_aceptacion = p_token::uuid
    and lower(coalesce(tipo, '')) in ('contract', 'contrato')
  limit 1;
  if not found then
    return null;
  end if;
  select * into v_contratista from public.contratistas where id = v_doc.contratista_id;

  update public.historial_estimados
    set estado = 'accepted',
        updated_at = now()
    where id = v_doc.id
      and contratista_id = v_doc.contratista_id
      and lower(coalesce(estado, '')) <> 'accepted';

  return jsonb_build_object(
    'id', v_doc.id,
    'folio', v_doc.folio,
    'cliente_nombre', v_doc.cliente_nombre,
    'cliente_email', coalesce(v_doc.cliente->>'email', ''),
    'total', v_doc.total,
    'contratista_email', v_contratista.email,
    'nombre_empresa', v_contratista.nombre_empresa,
    'already', not found,
    'estado', 'accepted'
  );
end;
$$;

create or replace function public.revertir_aceptacion_contrato(p_token text, p_estado text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_estado text := lower(trim(coalesce(p_estado, 'pending')));
  v_row public.historial_estimados%rowtype;
begin
  if v_estado not in ('pending', 'open', 'sent') then
    v_estado := 'pending';
  end if;
  if coalesce(p_token, '') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    return null;
  end if;
  update public.historial_estimados
    set estado = v_estado,
        updated_at = now()
    where token_aceptacion = p_token::uuid
      and lower(coalesce(estado, '')) = 'accepted'
    returning * into v_row;
  if not found then
    return jsonb_build_object('reverted', false);
  end if;
  return jsonb_build_object('reverted', true, 'estado', v_row.estado);
end;
$$;

revoke all on function public.registrar_contratista(text, text) from public;
revoke all on function public.contratista_por_token(text) from public;
revoke all on function public.actualizar_contratista(text, text, text) from public;
revoke all on function public.listar_historial_tenant(text) from public;
revoke all on function public.obtener_historial_tenant(text, text) from public;
revoke all on function public.guardar_historial_tenant(text, jsonb) from public;
revoke all on function public.leer_contrato_publico(text) from public;
revoke all on function public.marcar_contrato_aceptado(text) from public;
revoke all on function public.revertir_aceptacion_contrato(text, text) from public;

grant execute on function public.registrar_contratista(text, text) to anon, authenticated, service_role;
grant execute on function public.contratista_por_token(text) to anon, authenticated, service_role;
grant execute on function public.actualizar_contratista(text, text, text) to anon, authenticated, service_role;
grant execute on function public.listar_historial_tenant(text) to anon, authenticated, service_role;
grant execute on function public.obtener_historial_tenant(text, text) to anon, authenticated, service_role;
grant execute on function public.guardar_historial_tenant(text, jsonb) to anon, authenticated, service_role;
grant execute on function public.leer_contrato_publico(text) to anon, authenticated, service_role;
grant execute on function public.marcar_contrato_aceptado(text) to anon, authenticated, service_role;
grant execute on function public.revertir_aceptacion_contrato(text, text) to anon, authenticated, service_role;

-- Perfiles opcionales (Supabase Auth). La marca blanca operativa usa contratistas.nombre_empresa.
create table if not exists public.perfiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  nombre text not null default '',
  nombre_empresa text not null default '',
  plan_seleccionado text not null default '',
  trial_inicio timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.perfiles add column if not exists nombre text not null default '';
alter table public.perfiles add column if not exists plan_seleccionado text not null default '';
alter table public.perfiles add column if not exists trial_inicio timestamptz;

alter table public.perfiles enable row level security;

create or replace function public.handle_nuevo_usuario()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- El perfil es auxiliar. Un fallo aquí no puede revertir auth.users.
  begin
    insert into public.perfiles (id, email, nombre, nombre_empresa, plan_seleccionado, trial_inicio)
    values (
      new.id,
      new.email,
      coalesce(new.raw_user_meta_data->>'nombre', ''),
      coalesce(new.raw_user_meta_data->>'nombre_empresa', ''),
      coalesce(new.raw_user_meta_data->>'plan_seleccionado', ''),
      now()
    )
    on conflict (id) do update
      set email = excluded.email,
          nombre = case when excluded.nombre <> '' then excluded.nombre else public.perfiles.nombre end,
          nombre_empresa = case
            when excluded.nombre_empresa <> '' then excluded.nombre_empresa
            else public.perfiles.nombre_empresa
          end,
          plan_seleccionado = case
            when excluded.plan_seleccionado <> '' then excluded.plan_seleccionado
            else public.perfiles.plan_seleccionado
          end,
          trial_inicio = coalesce(public.perfiles.trial_inicio, excluded.trial_inicio),
          updated_at = now();
  exception
    when others then
      raise warning 'handle_nuevo_usuario no bloquea auth.users: %', sqlerrm;
  end;
  return new;
end;
$$;

revoke all on function public.handle_nuevo_usuario() from public;
grant execute on function public.handle_nuevo_usuario() to supabase_auth_admin;
grant execute on function public.handle_nuevo_usuario() to postgres;
grant execute on function public.handle_nuevo_usuario() to service_role;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert or update on auth.users
  for each row execute procedure public.handle_nuevo_usuario();

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

