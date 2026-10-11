-- adp により生成。fct_stock_movements の件数と measure の合計が、ベースのソースと一致すること
-- （結合による行の重複・欠落の検出）
with s as (
    select count(*) as n, sum(t0.quantity) as movement_qty
    from {{ ref('stg_inventory') }} as t0
),

f as (
    select count(*) as n, sum(movement_qty) as movement_qty
    from {{ ref('fct_stock_movements') }}
)

select *
from s, f
where s.n <> f.n or abs(coalesce(s.movement_qty, 0) - coalesce(f.movement_qty, 0)) > 1e-9 * greatest(1, abs(coalesce(s.movement_qty, 0)))
