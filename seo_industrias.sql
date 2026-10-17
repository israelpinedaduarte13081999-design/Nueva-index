-- Optional Supabase table for programmatic SEO landings.
-- If empty, Flask falls back to seo_industrias.json.

create table if not exists public.seo_industrias (
  slug text primary key,
  name text not null,
  contractor text,
  job text,
  title text,
  h1 text,
  meta text,
  lead text,
  keywords jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.seo_industrias enable row level security;

drop policy if exists seo_industrias_public_read on public.seo_industrias;
create policy seo_industrias_public_read
  on public.seo_industrias
  for select
  using (true);

grant select on public.seo_industrias to anon, authenticated;
