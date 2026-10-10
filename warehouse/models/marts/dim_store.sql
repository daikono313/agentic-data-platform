select
    {{ dim_key('store_id') }} as store_key,
    store_id,
    store_name,
    channel,
    prefecture,
    region
from {{ ref('stg_stores') }}

union all

select {{ unknown_key() }}, null, '不明', '不明', null, null
