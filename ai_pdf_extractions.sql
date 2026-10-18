-- Free-tier cap for AI PDF extractions (measurements, line items, architectural plans).
-- Premium (plan_seleccionado = 'premium') skips the counter in Flask.
-- Run this in the Supabase SQL editor. Do not disable RLS.

alter table public.perfiles
  add column if not exists ai_pdf_extractions_count integer not null default 0;

alter table public.contratistas
  add column if not exists ai_pdf_extractions_count integer not null default 0;

create or replace function public.consumir_ai_pdf_extraction(
  p_user_id uuid default null,
  p_email text default ''
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  n int;
  pid uuid;
  mail text := lower(trim(coalesce(p_email, '')));
begin
  pid := p_user_id;
  if pid is null and mail <> '' then
    select id into pid
    from public.perfiles
    where lower(email) = mail
    limit 1;
  end if;

  if pid is not null then
    update public.perfiles
       set ai_pdf_extractions_count = coalesce(ai_pdf_extractions_count, 0) + 1,
           updated_at = now()
     where id = pid
       and coalesce(ai_pdf_extractions_count, 0) < 5
     returning ai_pdf_extractions_count into n;
    if n is not null then
      update public.contratistas
         set ai_pdf_extractions_count = n,
             updated_at = now()
       where user_id = pid
          or (mail <> '' and lower(email) = mail);
      return jsonb_build_object('ok', true, 'count', n);
    end if;
    select coalesce(ai_pdf_extractions_count, 0) into n
    from public.perfiles
    where id = pid;
    return jsonb_build_object('ok', false, 'count', coalesce(n, 5));
  end if;

  if mail = '' then
    return jsonb_build_object('ok', false, 'count', 5);
  end if;

  update public.contratistas
     set ai_pdf_extractions_count = coalesce(ai_pdf_extractions_count, 0) + 1,
         updated_at = now()
   where lower(email) = mail
     and coalesce(ai_pdf_extractions_count, 0) < 5
   returning ai_pdf_extractions_count into n;
  if n is not null then
    return jsonb_build_object('ok', true, 'count', n);
  end if;
  select coalesce(ai_pdf_extractions_count, 0) into n
  from public.contratistas
  where lower(email) = mail
  limit 1;
  return jsonb_build_object('ok', false, 'count', coalesce(n, 5));
end;
$$;

revoke all on function public.consumir_ai_pdf_extraction(uuid, text) from public;
grant execute on function public.consumir_ai_pdf_extraction(uuid, text) to service_role;
grant execute on function public.consumir_ai_pdf_extraction(uuid, text) to authenticated;
