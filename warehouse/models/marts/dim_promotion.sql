select
    {{ dim_key('promotion_id') }} as promotion_key,
    promotion_id,
    promotion_name,
    promotion_type,
    discount_rate
from {{ ref('stg_promotions') }}

union all

-- プロモーションが適用されていない注文はこの行を参照する
select {{ unknown_key() }}, null, 'プロモーションなし', 'none', 0.0
