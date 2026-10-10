-- 突合テスト: ファクトの合計がソースの合計と一致すること（行の欠落・重複の検出）
with source as (
    select count(*) as n, sum(quantity * unit_price - discount_amount) as amount
    from {{ source('raw', 'order_items') }}
),

fact as (
    select count(*) as n, sum(sales_amount) as amount
    from {{ ref('fct_order_lines') }}
)

select *
from source, fact
where source.n <> fact.n or source.amount <> fact.amount
