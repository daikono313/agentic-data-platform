-- adp により生成。fct_order_lines の件数と measure の合計が、ベースのソースと一致すること
-- （結合による行の重複・欠落の検出）
with s as (
    select count(*) as n, sum(t0.quantity * t0.unit_price - t0.discount_amount) as sales_amount
    from {{ ref('stg_order_items') }} as t0
),

f as (
    select count(*) as n, sum(sales_amount) as sales_amount
    from {{ ref('fct_order_lines') }}
)

select *
from s, f
where s.n <> f.n or abs(coalesce(s.sales_amount, 0) - coalesce(f.sales_amount, 0)) > 1e-9 * greatest(1, abs(coalesce(s.sales_amount, 0)))
