select
    {{ dim_key('customer_id') }} as customer_key,
    customer_id,
    customer_name,
    gender,
    birth_date,
    prefecture,
    region,
    membership_rank,
    signup_date
from {{ ref('stg_customers') }}

union all

select
    {{ unknown_key() }},
    null, '不明', '不明', null, '不明', '不明', '不明', null
