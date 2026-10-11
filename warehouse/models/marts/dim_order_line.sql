-- adp により生成。返品された注文明細ディメンション（SCD Type1）
select
    {{ dim_key('t0.order_item_id') }} as order_line_key,
    t0.order_item_id as order_item_id,
    t1.category as product_category,
    t2.ordered_at as ordered_at
from {{ ref('stg_order_items') }} as t0
left join {{ ref('stg_products') }} as t1 on t0.product_id = t1.product_id
left join {{ ref('stg_orders') }} as t2 on t0.order_id = t2.order_id
where t0.order_item_id is not null
qualify row_number() over (partition by t0.order_item_id order by t0.order_item_id) = 1

union all

-- 不明メンバー
select {{ unknown_key() }}, null, '不明', null
