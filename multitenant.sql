create extension if not exists pgcrypto;

create table if not exists public.contratistas (
  id uuid primary key default gen_random_uuid(),
  nombre_empresa text not null,
  email text not null unique,
  token text not null unique,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.historial_estimados (
  id uuid primary key default gen_random_uuid(),
  contratista_id uuid not null references public.contratistas(id),
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

alter table public.historial_estimados enable row level security;
alter table public.contratistas enable row level security;

revoke all on table public.historial_estimados from anon, authenticated;
revoke all on table public.contratistas from anon, authenticated;

create or replace function public.registrar_contratista(p_email text, p_nombre text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_email text := lower(trim(coalesce(p_email, '')));
  v_nombre text := trim(coalesce(p_nombre, ''));
  v_id uuid;
  v_token text;
begin
  if v_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' or v_nombre = '' then
    raise exception 'invalid_contractor';
  end if;
  if exists (select 1 from public.contratistas where email = v_email) then
    raise exception 'already_registered';
  end if;
  v_token := replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '');
  insert into public.contratistas (nombre_empresa, email, token)
  values (v_nombre, v_email, v_token)
  returning id into v_id;
  return jsonb_build_object(
    'id', v_id,
    'email', v_email,
    'nombre_empresa', v_nombre,
    'token', v_token
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
    'nombre_empresa', v_row.nombre_empresa
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
  v_id uuid;
  v_existente uuid;
  v_token_aceptacion uuid;
  v_tipo text;
  v_row public.historial_estimados%rowtype;
begin
  select id into v_cid from public.contratistas where token = trim(coalesce(p_token, ''));
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
      token_aceptacion = coalesce(v_token_aceptacion, token_aceptacion),
      updated_at = now()
    where id = v_existente and contratista_id = v_cid
    returning * into v_row;
  else
    insert into public.historial_estimados (
      folio, tipo, cliente_nombre, fecha, total, estado,
      cliente, items, areas, snapshot, notas, terminos, firmas, payload,
      contratista_id, token_aceptacion
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
