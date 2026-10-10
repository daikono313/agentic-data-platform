select
    promotion_id,
    promotion_name,
    promotion_type,
    discount_rate,
    start_date,
    end_date
from {{ source('raw', 'promotions') }}
