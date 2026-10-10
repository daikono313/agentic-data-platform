-- adp により生成
-- 元イベント: 顧客が商品を注文する（transaction）
-- 粒度: 注文の中の商品1つ(注文明細1行)
select
    t0.order_item_id as order_item_id,
    cast(t1.ordered_at as date) as ordered_on,
    t1.ordered_at as ordered_at,
    {{ dim_key('t1.store_id') }} as store_key,
    {{ dim_key('t0.product_id') }} as product_key,
    {{ dim_key('t1.customer_id') }} as customer_key,
    {{ dim_key('t1.promotion_id') }} as promotion_key,
    t1.order_status as order_status,
    t0.quantity * t0.unit_price - t0.discount_amount as sales_amount
from {{ ref('stg_order_items') }} as t0
left join {{ ref('stg_orders') }} as t1 on t0.order_id = t1.order_id
