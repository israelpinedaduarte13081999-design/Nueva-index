-- Calificaciones y comentarios del panel privado.
-- Ejecuta este script en el SQL Editor de Supabase.

create table if not exists public.feedback (
  id uuid primary key default gen_random_uuid(),
  contratista_id uuid,
  user_id uuid,
  email text,
  calificacion integer not null check (calificacion >= 1 and calificacion <= 5),
  comentario text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists feedback_created_idx
  on public.feedback (created_at desc);

create index if not exists feedback_contratista_idx
  on public.feedback (contratista_id, created_at desc);

alter table public.feedback enable row level security;

comment on table public.feedback is
  'Ratings 1-5 and comments from signed-in contractors. Writes go through Flask with the service role.';

grant all on public.feedback to service_role;
