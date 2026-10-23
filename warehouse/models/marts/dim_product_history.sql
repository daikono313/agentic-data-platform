-- adp により生成。商品の属性の履歴（SCD Type 2。ADR 0009）
with versions as (
    select
        *,
        row_number() over (partition by product_id order by dbt_valid_from) as version_no
    from {{ ref('snp_products') }}
    where product_id is not null
),

calendar as (
    select
        cast(min(date_day) as timestamp) as starts_at,
        cast(max(date_day) + interval 1 day as timestamp) as ends_at
    from {{ ref('dim_date') }}
)

select
    md5(cast(product_id as varchar) || '|' || cast(dbt_valid_from as varchar)) as product_version_key,
    {{ dim_key('product_id') }} as product_key,
    category as category,
    product_name as product_name,
    case when version_no = 1 then calendar.starts_at else dbt_valid_from end as valid_from,
    coalesce(dbt_valid_to, calendar.ends_at) as valid_to
from versions
cross join calendar

union all

-- 不明メンバー（期間は暦の全体）
select md5('__unknown__'), {{ unknown_key() }}, '不明', '不明', calendar.starts_at, calendar.ends_at
from calendar
