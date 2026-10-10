select
    product_id,
    product_name,
    category,
    subcategory,
    brand,
    list_price,
    unit_cost,
    launched_on
from {{ source('raw', 'products') }}
