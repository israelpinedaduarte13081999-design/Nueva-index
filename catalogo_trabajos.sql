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

insert into public.catalogo_trabajos (categoria, codigo, descripcion, unidad, precio, orden) values
  ('ALFOMBRA', 'ZN-DEM-PIS-ALF-VIN', 'Remover piso existente (alfombra / vinil)', 'SF', 0.45, 1),
  ('ALFOMBRA', 'ZN-INS-ALF', 'Instalación de alfombra nueva', 'SF', 2.75, 2),
  ('ALFOMBRA', 'ZN-INS-PAD-BAJ-ALF', 'Instalación de padding / bajo alfombra', 'SF', 0.60, 3),
  ('ALFOMBRA', 'ZN-INS-TCK-TIR-CLA', 'Instalación de tack strips (tiras de clavos)', 'LF', 1.25, 4),
  ('ALFOMBRA', 'ZN-TRN-MET-ALF', 'Transición metálica para alfombra', 'LF', 4.50, 5),
  ('PISO', 'ZN-INS-PIS-LVT-VIN', 'Instalación de piso LVT / Vinil Click', 'SF', 2.25, 1),
  ('PISO', 'ZN-DEM-ALF-PIS', 'Remoción de alfombra / piso existente', 'SF', 0.50, 2),
  ('PISO', 'ZN-NIV-CTP-SLV', 'Nivelación de contrapiso (Self-Leveler)', 'BAG', 45.00, 3),
  ('PISO', 'ZN-DEM-BAL-CER', 'Remoción de baldosa / cerámica existente', 'SF', 1.85, 4),
  ('PISO', 'ZN-INS-SHO', 'Instalación de shoe moulding / cuarto bocel', 'LF', 1.10, 5),
  ('PISO', 'ZN-INS-BAS-ROD', 'Instalación de baseboard (rodapié)', 'LF', 1.65, 6),
  ('PISO', 'ZN-SND-PUL-PIS-MAD', 'Lijado y pulido de piso de madera', 'SF', 3.00, 7),
  ('PISO', 'ZN-APL-TIN-POL', 'Tinción / Stain y aplicación de poliuretano', 'SF', 1.75, 8),
  ('TECHO', 'ZN-DEM-DRY-TEC', 'Demolición y remoción de drywall en techo', 'SF', 0.85, 1),
  ('TECHO', 'ZN-INS-DRY-TEC', 'Colgado de drywall 1/2" o 5/8" en techo', 'SF', 1.65, 2),
  ('TECHO', 'ZN-DEM-TEX-POP-TEC', 'Remoción de textura popcorn en techo', 'SF', 1.75, 3),
  ('TECHO', 'ZN-ACA-DRY-TAP-SKI', 'Acabado de drywall (Tape, Bed & Skim Coat)', 'SF', 1.20, 4),
  ('TECHO', 'ZN-PNT-TEC-PRI-MAN', 'Pintura de techo (Primer + 2 manos)', 'SF', 0.95, 5),
  ('TECHO', 'ZN-INS-MEM-AUT-FLA', 'Instalación de membrana SBS autoadherible (Flat Roof)', 'SQ', 140.00, 6),
  ('DRYWALL', 'ZN-INS-PNL-DRY', 'Colgado de paneles de sheetrock 1/2"', 'SF', 1.45, 1),
  ('DRYWALL', 'ZN-ENC-ACA-NIV', 'Encinte y acabado Nivel 4', 'SF', 1.15, 2),
  ('DRYWALL', 'ZN-RPR-DRY-MEN', 'Parche de drywall menor / reparación', 'EA', 75.00, 3),
  ('DRYWALL', 'ZN-INS-CEM-HUM', 'Instalación de Durock / Cement board en zonas húmedas', 'SF', 2.35, 4),
  ('PINTURA', 'ZN-PNT-PAR-MAN', 'Pintura interior de paredes (2 manos)', 'SF', 0.90, 1),
  ('PINTURA', 'ZN-SND-PRE-MAS-PAR', 'Preparación, masillado y lijado de paredes', 'SF', 0.40, 2),
  ('PINTURA', 'ZN-PNT-MOL-ROD', 'Pintura de molduras y rodapiés', 'LF', 0.85, 3),
  ('PINTURA', 'ZN-PNT-ESM-PTE-MRC', 'Pintura / Esmalte en puertas y marcos', 'EA', 45.00, 4),
  ('BAÑO', 'ZN-DEM-BAN', 'Demolición completa de baño existente', 'EA', 650.00, 1),
  ('BAÑO', 'ZN-INS-SPN-LIN-IMP', 'Instalación de shower pan liner e impermeabilización', 'EA', 350.00, 2),
  ('BAÑO', 'ZN-INS-AZU-TIL-DUC', 'Instalación de azulejo / tile en ducha (paredes)', 'SF', 12.00, 3),
  ('BAÑO', 'ZN-INS-PIS-MOS-DUC', 'Instalación de piso de mosaico en ducha', 'SF', 14.00, 4),
  ('BAÑO', 'ZN-DEM-INO-TLT-RES', 'Desmontaje y reinstalación de inodoro (Toilet Reset)', 'EA', 120.00, 5),
  ('BAÑO', 'ZN-INS-VAN-GRF', 'Instalación de vanitorio / vanity con grifería', 'EA', 220.00, 6),
  ('BAÑO', 'ZN-APL-LEC-GRO-SEL', 'Aplicación de lechada (Grout) y sellador', 'SF', 1.10, 7),
  ('DECK', 'ZN-INS-PST-SOP', 'Instalación de postes de soporte 6x6', 'EA', 65.00, 1),
  ('DECK', 'ZN-EST-JOI-VIG-TRA', 'Estructura de joists / viguetas tratadas 2x10 o 2x12', 'LF', 5.50, 2),
  ('DECK', 'ZN-INS-DCK-DUE-MAD', 'Instalación de decking / duela de madera tratada', 'SF', 4.25, 3),
  ('DECK', 'ZN-INS-BRD-BAL', 'Instalación de barandales y balusteres 2x2', 'LF', 18.00, 4);
