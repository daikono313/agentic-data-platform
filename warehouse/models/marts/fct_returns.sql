-- adp により生成
-- 元イベント: 顧客が商品を返品する（transaction）
-- 粒度: 返品 1 件につき 1 行
select
    t0.return_id as return_id,
    cast(t0.returned_at as date) as returned_on,
    t0.returned_at as returned_at,
    {{ dim_key('t0.order_item_id') }} as order_line_key,
    t0.order_item_id as order_item_id,
    t0.return_reason as return_reason,
    t0.quantity as returned_quantity
from {{ ref('stg_returns') }} as t0
