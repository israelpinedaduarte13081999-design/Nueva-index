-- Pegar en Supabase → SQL Editor → Run
-- Guarda el cliente de Stripe y marca si el alta aún debe capturar tarjeta.

alter table public.perfiles
  add column if not exists pago_cliente_id text not null default '';

alter table public.perfiles
  add column if not exists requiere_pago boolean not null default false;

-- Cuentas ya existentes siguen entraando al panel sin pedir tarjeta.
update public.perfiles
set requiere_pago = false
where requiere_pago is distinct from true;
