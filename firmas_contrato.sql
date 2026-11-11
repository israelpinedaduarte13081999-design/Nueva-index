-- Opcional: columnas dedicadas de firma en historial_estimados.
-- El Flask ya guarda imagen, fecha e IP dentro de firmas (jsonb) y pone estado = 'Firmado'.
-- Ejecuta esto en Supabase si quieres columnas sueltas además del JSON.

alter table public.historial_estimados
  add column if not exists firma_ip text,
  add column if not exists firma_fecha timestamptz;

-- Canonical keys inside firmas jsonb:
--   cliente / client_signature     = client PNG only
--   contratista / contractor_signature = contractor PNG only
-- Never copy contractor_signature onto the client line.
