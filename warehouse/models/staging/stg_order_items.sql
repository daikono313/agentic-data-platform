select
    order_item_id,
    order_id,
    product_id,
    quantity,
    unit_price,
    discount_amount
from {{ source('raw', 'order_items') }}
