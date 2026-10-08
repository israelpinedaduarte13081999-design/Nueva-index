-- Búsqueda semántica de catalogo_items (pgvector + OpenAI text-embedding-3-small).
-- Ejecuta este script completo en el SQL Editor de Supabase (una sola vez).
-- Después: python generar_embeddings_catalogo.py

create extension if not exists vector;

alter table public.catalogo_items
  add column if not exists embedding vector(1536);

create index if not exists catalogo_items_embedding_hnsw
  on public.catalogo_items
  using hnsw (embedding vector_cosine_ops);

create or replace function public.match_items(
  query_embedding vector(1536),
  match_threshold double precision default 0.70,
  match_count integer default 8,
  filter_unidad text default null
)
returns table (
  id bigint,
  codigo text,
  descripcion text,
  unidad text,
  precio_base numeric,
  precio_material numeric,
  precio_mano_obra numeric,
  similarity double precision
)
language sql
stable
as $$
  select
    ci.id::bigint,
    ci.codigo,
    ci.descripcion,
    ci.unidad,
    ci.precio_base,
    ci.precio_material,
    ci.precio_mano_obra,
    (1 - (ci.embedding <=> query_embedding))::double precision as similarity
  from public.catalogo_items ci
  where ci.embedding is not null
    and (filter_unidad is null or upper(ci.unidad) = upper(filter_unidad))
    and 1 - (ci.embedding <=> query_embedding) >= match_threshold
  order by ci.embedding <=> query_embedding
  limit greatest(match_count, 1);
$$;

grant execute on function public.match_items(vector, double precision, integer, text)
  to anon, authenticated;

-- Permite que el script de embeddings (clave anon) escriba solo la columna embedding.
create or replace function public.set_item_embedding(
  item_id bigint,
  item_embedding vector(1536)
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.catalogo_items
  set embedding = item_embedding
  where id = item_id;
$$;

grant execute on function public.set_item_embedding(bigint, vector)
  to anon, authenticated;
