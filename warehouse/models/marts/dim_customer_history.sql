-- adp により生成。顧客の属性の履歴（SCD Type 2。ADR 0009）
with snapshot_rows as (
    select * from {{ ref('snp_customers') }}
    where customer_id is not null
    -- 同じ自然キー・同じ時刻の版は 1 つにする（stg に同じキーの行が複数ある場合）
    qualify row_number() over (partition by customer_id, dbt_valid_from order by dbt_scd_id) = 1
),

versions as (
    select
        *,
        row_number() over (partition by customer_id order by dbt_valid_from) as version_no
    from snapshot_rows
),

calendar as (
    select
        cast(min(date_day) as timestamp) as starts_at,
        cast(max(date_day) + interval 1 day as timestamp) as ends_at
    from {{ ref('dim_date') }}
)

select
    dbt_scd_id as customer_version_key,
    {{ dim_key('customer_id') }} as customer_key,
    region as region,
    prefecture as prefecture,
    gender as gender,
    case when version_no = 1 then calendar.starts_at else least(greatest(dbt_valid_from, calendar.starts_at), calendar.ends_at) end as valid_from,
    least(coalesce(dbt_valid_to, calendar.ends_at), calendar.ends_at) as valid_to
from versions
cross join calendar

union all

-- 不明メンバー（期間は暦の全体）
select {{ unknown_key() }}, {{ unknown_key() }}, '不明', '不明', '不明', calendar.starts_at, calendar.ends_at
from calendar
