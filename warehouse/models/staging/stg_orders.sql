select
    order_id,
    customer_id,
    store_id,
    ordered_at,
    cast(ordered_at as date) as ordered_on,
    promotion_id,
    order_status,
    payment_method,
    updated_at
from {{ source('raw', 'orders') }}
