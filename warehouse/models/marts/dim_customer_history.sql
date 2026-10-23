-- adp により生成。顧客の属性の履歴（SCD Type 2。ADR 0009）
with versions as (
    select
        *,
        row_number() over (partition by customer_id order by dbt_valid_from) as version_no
    from {{ ref('snp_customers') }}
    where customer_id is not null
),

calendar as (
    select
        cast(min(date_day) as timestamp) as starts_at,
        cast(max(date_day) + interval 1 day as timestamp) as ends_at
    from {{ ref('dim_date') }}
)

select
    md5(cast(customer_id as varchar) || '|' || cast(dbt_valid_from as varchar)) as customer_version_key,
    {{ dim_key('customer_id') }} as customer_key,
    region as region,
    prefecture as prefecture,
    gender as gender,
    case when version_no = 1 then calendar.starts_at else dbt_valid_from end as valid_from,
    coalesce(dbt_valid_to, calendar.ends_at) as valid_to
from versions
cross join calendar

union all

-- 不明メンバー（期間は暦の全体）
select md5('__unknown__'), {{ unknown_key() }}, '不明', '不明', '不明', calendar.starts_at, calendar.ends_at
from calendar
