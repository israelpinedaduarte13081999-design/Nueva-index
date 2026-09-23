-- Pegar en Supabase → SQL Editor → Run
-- Tabla que lee index.html (catálogo de ramas / subtareas)

create table if not exists public.catalogo_trabajos (
  id bigint generated always as identity primary key,
  categoria text not null,
  descripcion text not null,
  unidad text not null default 'SF',
  precio numeric(12,2) not null default 0,
  precio_labor numeric(12,2) default null,
  precio_material numeric(12,2) default null,
  precio_demo numeric(12,2) default null,
  orden int default 0
);

alter table if exists public.catalogo_trabajos add column if not exists precio_labor numeric(12,2);
alter table if exists public.catalogo_trabajos add column if not exists precio_material numeric(12,2);
alter table if exists public.catalogo_trabajos add column if not exists codigo text;

alter table public.catalogo_trabajos enable row level security;

drop policy if exists "catalogo_trabajos_lectura_publica" on public.catalogo_trabajos;
create policy "catalogo_trabajos_lectura_publica"
  on public.catalogo_trabajos
  for select
  to anon, authenticated
  using (true);

insert into public.catalogo_trabajos (categoria, descripcion, unidad, precio, orden) values
  ('ALFOMBRA', 'Remover piso existente (alfombra / vinil)', 'SF', 0.45, 1),
  ('ALFOMBRA', 'Instalación de alfombra nueva', 'SF', 2.75, 2),
  ('ALFOMBRA', 'Instalación de padding / bajo alfombra', 'SF', 0.60, 3),
  ('ALFOMBRA', 'Instalación de tack strips (tiras de clavos)', 'LF', 1.25, 4),
  ('ALFOMBRA', 'Transición metálica para alfombra', 'LF', 4.50, 5),
  ('PISO', 'Instalación de piso LVT / Vinil Click', 'SF', 2.25, 1),
  ('PISO', 'Remoción de alfombra / piso existente', 'SF', 0.50, 2),
  ('PISO', 'Nivelación de contrapiso (Self-Leveler)', 'BAG', 45.00, 3),
  ('PISO', 'Remoción de baldosa / cerámica existente', 'SF', 1.85, 4),
  ('PISO', 'Instalación de shoe moulding / cuarto bocel', 'LF', 1.10, 5),
  ('PISO', 'Instalación de baseboard (rodapié)', 'LF', 1.65, 6),
  ('PISO', 'Lijado y pulido de piso de madera', 'SF', 3.00, 7),
  ('PISO', 'Tinción / Stain y aplicación de poliuretano', 'SF', 1.75, 8),
  ('TECHO', 'Demolición y remoción de drywall en techo', 'SF', 0.85, 1),
  ('TECHO', 'Colgado de drywall 1/2" o 5/8" en techo', 'SF', 1.65, 2),
  ('TECHO', 'Remoción de textura popcorn en techo', 'SF', 1.75, 3),
  ('TECHO', 'Acabado de drywall (Tape, Bed & Skim Coat)', 'SF', 1.20, 4),
  ('TECHO', 'Pintura de techo (Primer + 2 manos)', 'SF', 0.95, 5),
  ('TECHO', 'Instalación de membrana SBS autoadherible (Flat Roof)', 'SQ', 140.00, 6),
  ('DRYWALL', 'Colgado de paneles de sheetrock 1/2"', 'SF', 1.45, 1),
  ('DRYWALL', 'Encinte y acabado Nivel 4', 'SF', 1.15, 2),
  ('DRYWALL', 'Parche de drywall menor / reparación', 'EA', 75.00, 3),
  ('DRYWALL', 'Instalación de Durock / Cement board en zonas húmedas', 'SF', 2.35, 4),
  ('PINTURA', 'Pintura interior de paredes (2 manos)', 'SF', 0.90, 1),
  ('PINTURA', 'Preparación, masillado y lijado de paredes', 'SF', 0.40, 2),
  ('PINTURA', 'Pintura de molduras y rodapiés', 'LF', 0.85, 3),
  ('PINTURA', 'Pintura / Esmalte en puertas y marcos', 'EA', 45.00, 4),
  ('BAÑO', 'Demolición completa de baño existente', 'EA', 650.00, 1),
  ('BAÑO', 'Instalación de shower pan liner e impermeabilización', 'EA', 350.00, 2),
  ('BAÑO', 'Instalación de azulejo / tile en ducha (paredes)', 'SF', 12.00, 3),
  ('BAÑO', 'Instalación de piso de mosaico en ducha', 'SF', 14.00, 4),
  ('BAÑO', 'Desmontaje y reinstalación de inodoro (Toilet Reset)', 'EA', 120.00, 5),
  ('BAÑO', 'Instalación de vanitorio / vanity con grifería', 'EA', 220.00, 6),
  ('BAÑO', 'Aplicación de lechada (Grout) y sellador', 'SF', 1.10, 7),
  ('DECK', 'Instalación de postes de soporte 6x6', 'EA', 65.00, 1),
  ('DECK', 'Estructura de joists / viguetas tratadas 2x10 o 2x12', 'LF', 5.50, 2),
  ('DECK', 'Instalación de decking / duela de madera tratada', 'SF', 4.25, 3),
  ('DECK', 'Instalación de barandales y balusteres 2x2', 'LF', 18.00, 4);
