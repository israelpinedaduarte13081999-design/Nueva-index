-- Pegar en Supabase → SQL Editor → Run
-- Control de la prueba gratuita de 14 días en perfiles.

alter table public.perfiles
  add column if not exists fecha_fin_prueba timestamptz;

-- Cuentas ya existentes: 14 días desde el inicio de prueba (o created_at).
update public.perfiles
set fecha_fin_prueba = coalesce(trial_inicio, created_at, now()) + interval '14 days'
where fecha_fin_prueba is null;

-- Altas nuevas: now() + 14 días, sin pisar una fecha ya guardada.
create or replace function public.handle_nuevo_usuario()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  begin
    insert into public.perfiles (
      id, email, nombre, nombre_empresa, plan_seleccionado, trial_inicio, fecha_fin_prueba
    )
    values (
      new.id,
      new.email,
      coalesce(new.raw_user_meta_data->>'nombre', ''),
      coalesce(new.raw_user_meta_data->>'nombre_empresa', ''),
      coalesce(new.raw_user_meta_data->>'plan_seleccionado', ''),
      now(),
      now() + interval '14 days'
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
          fecha_fin_prueba = coalesce(public.perfiles.fecha_fin_prueba, excluded.fecha_fin_prueba),
          updated_at = now();
  exception
    when others then
      raise warning 'handle_nuevo_usuario no bloquea auth.users: %', sqlerrm;
  end;
  return new;
end;
$$;
