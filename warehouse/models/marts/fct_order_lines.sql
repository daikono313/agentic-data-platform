-- 粒度: 注文明細 1 行（1 注文の中の 1 商品）
-- 元イベント: 顧客が商品を注文する（discrete → transaction fact）
select
    i.order_item_id as order_line_id,
    o.order_id,
    {{ dim_key('o.customer_id') }} as customer_key,
    {{ dim_key('i.product_id') }} as product_key,
    {{ dim_key('o.store_id') }} as store_key,
    {{ dim_key('o.promotion_id') }} as promotion_key,
    o.ordered_on,
    o.ordered_at,
    o.order_status,
    o.payment_method,
    i.quantity,
    i.unit_price,
    i.discount_amount,
    i.sales_amount
from {{ ref('stg_order_items') }} as i
inner join {{ ref('stg_orders') }} as o on i.order_id = o.order_id
