select
    customer_id,
    customer_name,
    gender,
    birth_date,
    prefecture,
    region,
    membership_rank,
    signup_date,
    updated_at
from {{ source('raw', 'customers') }}
