-- adp により生成。顧客の属性の履歴（SCD Type 2。ADR 0009）
with calendar as (
    select cast(min(date_day) as timestamp) as starts_at, cast(max(date_day) + interval 1 day as timestamp) as ends_at
    from {{ ref('dim_date') }}
),

snapshot_rows as (
    select customer_id, region, prefecture, gender, dbt_scd_id, dbt_valid_from, dbt_valid_to
    from {{ ref('snp_customers') }}
    where customer_id is not null
    -- 同じ自然キー・同じ時刻の版は 1 つにする（stg に同じキーの行が複数ある場合）
    qualify row_number() over (partition by customer_id, dbt_valid_from order by dbt_scd_id) = 1
),

-- 版の期間を暦の範囲に収め、長さの無くなった版（暦の外の版）を除く
clamped as (
    select
        snapshot_rows.customer_id, snapshot_rows.region, snapshot_rows.prefecture, snapshot_rows.gender, snapshot_rows.dbt_scd_id,
        least(greatest(snapshot_rows.dbt_valid_from, calendar.starts_at), calendar.ends_at) as adp_from_at,
        least(greatest(coalesce(snapshot_rows.dbt_valid_to, calendar.ends_at), calendar.starts_at), calendar.ends_at) as adp_to_at
    from snapshot_rows
    cross join calendar
),

-- 最初の版は暦の始まりから（源泉の更新日時より前の出来事も結合できるように）
versions as (
    select
        *,
        row_number() over (partition by customer_id order by adp_from_at) as adp_version_no
    from clamped
    where adp_from_at < adp_to_at
)

select
    versions.dbt_scd_id as customer_version_key,
    {{ dim_key('versions.customer_id') }} as customer_key,
    versions.region as region,
    versions.prefecture as prefecture,
    versions.gender as gender,
    case when adp_version_no = 1 then calendar.starts_at else adp_from_at end as valid_from,
    adp_to_at as valid_to
from versions
cross join calendar

union all

-- 不明メンバー（期間は暦の全体）
select {{ unknown_key() }}, {{ unknown_key() }}, '不明', '不明', '不明', calendar.starts_at, calendar.ends_at
from calendar
