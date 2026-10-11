-- adp により生成。fct_returns の件数と measure の合計が、ベースのソースと一致すること
-- （結合による行の重複・欠落の検出）
with s as (
    select count(*) as n, sum(t0.quantity) as returned_quantity
    from {{ ref('stg_returns') }} as t0
),

f as (
    select count(*) as n, sum(returned_quantity) as returned_quantity
    from {{ ref('fct_returns') }}
)

select *
from s, f
where s.n <> f.n or abs(coalesce(s.returned_quantity, 0) - coalesce(f.returned_quantity, 0)) > 1e-9 * greatest(1, abs(coalesce(s.returned_quantity, 0)))
