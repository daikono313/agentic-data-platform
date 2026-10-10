select
    store_id,
    store_name,
    channel,
    prefecture,
    region,
    opened_on
from {{ source('raw', 'stores') }}
