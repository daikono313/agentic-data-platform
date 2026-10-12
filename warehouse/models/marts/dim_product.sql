select
    {{ dim_key('product_id') }} as product_key,
    product_id,
    product_name,
    category,
    subcategory,
    brand,
    list_price
from {{ ref('stg_products') }}

union all

select {{ unknown_key() }}, null, '不明', '不明', '不明', '不明', null
