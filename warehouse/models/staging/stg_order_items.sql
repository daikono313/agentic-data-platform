select
    order_item_id,
    order_id,
    product_id,
    quantity,
    unit_price,
    discount_amount,
    quantity * unit_price - discount_amount as sales_amount
from {{ source('raw', 'order_items') }}
