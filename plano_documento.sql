-- Opcional: columnas dedicadas del plano en historial_estimados.
-- Flask ya guarda el JSON editable y la imagen Base64 dentro de snapshot.plano (jsonb).
-- Ejecuta esto en Supabase si quieres columnas sueltas además del JSON.

alter table public.historial_estimados
  add column if not exists plano_json jsonb,
  add column if not exists plano_imagen text;
