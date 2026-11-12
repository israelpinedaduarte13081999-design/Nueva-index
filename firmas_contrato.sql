-- Firmas remotas del cliente + Realtime.
-- El Flask hace UPDATE a historial_estimados al firmar; este RPC es el fallback
-- security definer cuando RLS bloquea el UPDATE directo (clave anon).
-- Ejecuta en el SQL Editor de Supabase.

alter table public.historial_estimados
  add column if not exists firma_ip text,
  add column if not exists firma_fecha timestamptz;

-- Opcional: columnas top-level que el frontend/Realtime también pueden leer.
alter table public.historial_estimados
  add column if not exists client_signature text,
  add column if not exists status text;

-- Canonical keys inside firmas jsonb:
--   cliente / client_signature     = client PNG only
--   contratista / contractor_signature = contractor PNG only
-- Never copy contractor_signature onto the client line.

create or replace function public.marcar_contrato_firmado(
  p_token text,
  p_firma text,
  p_ip text default '',
  p_metodo text default 'remoto'
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_doc public.historial_estimados%rowtype;
  v_firmas jsonb;
  v_ahora timestamptz := now();
  v_cli text;
begin
  if coalesce(p_token, '') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then
    return null;
  end if;
  if coalesce(p_firma, '') not like 'data:image/png;base64,%' then
    raise exception 'invalid_signature';
  end if;

  select * into v_doc
  from public.historial_estimados
  where token_aceptacion = p_token::uuid
    and lower(coalesce(tipo, '')) in ('contract', 'contrato')
  limit 1;
  if not found then
    return null;
  end if;

  v_firmas := coalesce(v_doc.firmas, '{}'::jsonb);
  v_cli := coalesce(v_firmas->>'cliente', v_firmas->>'client_signature', '');
  if lower(coalesce(v_doc.estado, '')) in ('signed', 'firmado', 'accepted')
     and v_cli like 'data:image/%' then
    return jsonb_build_object(
      'ok', true,
      'already', true,
      'id', v_doc.id,
      'estado', 'Signed',
      'status', 'Signed',
      'updated_at', v_doc.updated_at,
      'firmas', v_firmas
    );
  end if;

  v_firmas := (v_firmas
    || jsonb_build_object(
      'cliente', p_firma,
      'client_signature', p_firma,
      'clienteFecha', v_ahora,
      'clienteIp', left(coalesce(p_ip, ''), 64),
      'clienteMetodo', left(coalesce(p_metodo, 'remoto'), 32)
    )) - 'client';

  -- UPDATE obligatorio: sin esto Realtime del contratista no se dispara.
  begin
    update public.historial_estimados
      set estado = 'Signed',
          firmas = v_firmas,
          updated_at = v_ahora,
          firma_ip = left(coalesce(p_ip, ''), 64),
          firma_fecha = v_ahora,
          client_signature = p_firma,
          status = 'Signed',
          snapshot = case
            when snapshot is null then snapshot
            else jsonb_set(coalesce(snapshot, '{}'::jsonb), '{firmas}', v_firmas, true)
          end
      where id = v_doc.id
      returning * into v_doc;
  exception
    when undefined_column then
      update public.historial_estimados
        set estado = 'Signed',
            firmas = v_firmas,
            updated_at = v_ahora
        where id = v_doc.id
        returning * into v_doc;
  end;

  if not found then
    raise exception 'update_failed';
  end if;

  return jsonb_build_object(
    'ok', true,
    'already', false,
    'id', v_doc.id,
    'estado', 'Signed',
    'status', 'Signed',
    'updated_at', v_doc.updated_at,
    'firmas', v_firmas
  );
end;
$$;

revoke all on function public.marcar_contrato_firmado(text, text, text, text) from public;
grant execute on function public.marcar_contrato_firmado(text, text, text, text) to anon, authenticated, service_role;
