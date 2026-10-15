-- Auditoría y corrección de desglose en catalogo_items (estilo Xactimate).
-- precio_base NO se modifica.
-- Tras el UPDATE: precio_material + precio_mano_obra + precio_remocion = precio_base.
--
-- 1) Corre el SELECT de auditoría primero.
-- 2) Si el recuento te convence, descomenta el UPDATE final.
-- Ítems "only" (material / labor / demo) se dejan en una sola columna = precio_base.

-- Splits sobre el paquete completo (R&R):
--   remocion 5–15% según oficio
--   del resto: pintura 20/80 material/labor; gabinetes 70/30; etc.

with clasificado as (
  select
    id,
    codigo,
    categoria,
    descripcion,
    precio_base,
    coalesce(precio_material, 0) as precio_material,
    coalesce(precio_mano_obra, 0) as precio_mano_obra,
    coalesce(precio_remocion, 0) as precio_remocion,
    lower(coalesce(codigo, '') || ' ' || coalesce(categoria, '') || ' ' || coalesce(descripcion, '')) as blob,
    case
      when descripcion ~* 'material only|solo material|supply only'
           and descripcion !~* 'instal' then 'material'
      when descripcion ~* 'labor only|solo mano|install(ation)? only' then 'labor'
      when descripcion ~* 'demolition only|solo demol|removal only|tear[[:space:]-]*(out|off) only|demo only'
           or categoria ~* 'demolic' then 'demo'
      else 'paquete'
    end as alcance
  from public.catalogo_items
  where coalesce(precio_base, 0) > 0
),
oficio as (
  select
    c.*,
    case
      when alcance = 'demo' or blob ~ 'demolici|demolition|haul off|escombro' then 'demolicion'
      when blob ~ 'pintura|paint|esmalte|\yprimer\y' then 'pintura'
      when blob ~ 'drywall|tablaroca|sheetrock' then 'drywall'
      when blob ~ 'alfombra|carpet' then 'alfombra'
      when blob ~ 'azulejo|\ytile\y|porcelanato|ceramica' then 'azulejo'
      when blob ~ 'roof|techo|shingle|teja|\ysbs\y|epdm|\ytpo\y' then 'techo'
      when blob ~ 'siding|hardie|soffit|fascia' then 'siding'
      when blob ~ 'gabinete|cabinet' then 'gabinetes'
      when blob ~ 'encimera|countertop|granito|cuarzo' then 'encimeras'
      when blob ~ 'bano|baño|bath|ducha|shower|vanity' then 'bano'
      when blob ~ 'ventana|window' then 'ventanas'
      when blob ~ 'puerta|\ydoor\y' then 'puertas'
      when blob ~ 'canaleta|gutter' then 'canales'
      when blob ~ '\ydeck\y|terraza|porche' then 'deck'
      when blob ~ 'framing|enmarcado|cercha|joist' then 'framing'
      when blob ~ 'plomer|plumb|\ypex\y' then 'plomeria'
      when blob ~ 'electric|tomacorriente' then 'electricidad'
      when blob ~ 'hvac|ducto|climatiz' then 'hvac'
      when blob ~ 'piso|floor|lvt|lvp|laminat|hardwood' then 'piso'
      else 'general'
    end as oficio
  from clasificado c
),
pct as (
  select
    o.*,
    case oficio
      when 'demolicion' then 1.00
      when 'pintura' then 0.06
      when 'drywall' then 0.14
      when 'piso' then 0.12
      when 'alfombra' then 0.10
      when 'azulejo' then 0.15
      when 'techo' then 0.15
      when 'siding' then 0.12
      when 'gabinetes' then 0.10
      when 'encimeras' then 0.08
      when 'bano' then 0.12
      when 'ventanas' then 0.08
      when 'puertas' then 0.08
      when 'canales' then 0.10
      when 'deck' then 0.12
      when 'framing' then 0.12
      when 'plomeria' then 0.08
      when 'electricidad' then 0.07
      when 'hvac' then 0.08
      else 0.10
    end as demo_pct,
    case oficio
      when 'pintura' then 0.20
      when 'drywall' then 0.35
      when 'piso' then 0.55
      when 'alfombra' then 0.50
      when 'azulejo' then 0.42
      when 'techo' then 0.50
      when 'siding' then 0.52
      when 'gabinetes' then 0.70
      when 'encimeras' then 0.72
      when 'bano' then 0.50
      when 'ventanas' then 0.70
      when 'puertas' then 0.58
      when 'canales' then 0.50
      when 'deck' then 0.52
      when 'framing' then 0.48
      when 'plomeria' then 0.52
      when 'electricidad' then 0.38
      when 'hvac' then 0.60
      when 'demolicion' then 0.00
      else 0.50
    end as material_resto
  from oficio o
),
nuevo as (
  select
    p.*,
    case alcance
      when 'material' then round(precio_base, 2)
      when 'labor' then 0
      when 'demo' then 0
      else round((precio_base - round(precio_base * demo_pct, 2)) * material_resto, 2)
    end as nuevo_material,
    case alcance
      when 'material' then 0
      when 'labor' then round(precio_base, 2)
      when 'demo' then 0
      else round(precio_base, 2)
           - round(precio_base * demo_pct, 2)
           - round((precio_base - round(precio_base * demo_pct, 2)) * material_resto, 2)
    end as nuevo_labor,
    case alcance
      when 'material' then 0
      when 'labor' then 0
      when 'demo' then round(precio_base, 2)
      else round(precio_base * demo_pct, 2)
    end as nuevo_demo
  from pct p
)
select
  oficio,
  count(*) as items,
  count(*) filter (
    where round(precio_material + precio_mano_obra + precio_remocion, 2) <> round(precio_base, 2)
  ) as suma_descuadrada,
  count(*) filter (where precio_remocion = 0 and nuevo_demo > 0) as remocion_en_cero,
  round(avg(precio_base), 2) as precio_base_promedio
from nuevo
group by oficio
order by items desc;

-- Verificación de identidad de la propuesta (debe devolver 0 filas):
-- select id, codigo, precio_base, nuevo_material, nuevo_labor, nuevo_demo
-- from nuevo
-- where round(nuevo_material + nuevo_labor + nuevo_demo, 2) <> round(precio_base, 2);

-- UPDATE (descomenta para aplicar).
--
-- update public.catalogo_items c
-- set
--   precio_material = n.nuevo_material,
--   precio_mano_obra = n.nuevo_labor,
--   precio_remocion = n.nuevo_demo
-- from nuevo n
-- where c.id = n.id
--   and (
--     round(c.precio_material + c.precio_mano_obra + c.precio_remocion, 2) <> round(c.precio_base, 2)
--     or (coalesce(c.precio_remocion, 0) = 0 and n.nuevo_demo > 0)
--     or (coalesce(c.precio_material, 0) = 0 and n.nuevo_material > 0)
--     or (coalesce(c.precio_mano_obra, 0) = 0 and n.nuevo_labor > 0)
--   );
