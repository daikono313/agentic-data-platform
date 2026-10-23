-- adp により生成。店舗の属性の履歴（SCD Type 2。ADR 0009）
with calendar as (
    select cast(min(date_day) as timestamp) as starts_at, cast(max(date_day) + interval 1 day as timestamp) as ends_at
    from {{ ref('dim_date') }}
),

snapshot_rows as (
    select * from {{ ref('snp_stores') }}
    where store_id is not null
    -- 同じ自然キー・同じ時刻の版は 1 つにする（stg に同じキーの行が複数ある場合）
    qualify row_number() over (partition by store_id, dbt_valid_from order by dbt_scd_id) = 1
),

-- 版の期間を暦の範囲に収め、長さの無くなった版（暦の外の版）を除く
clamped as (
    select
        snapshot_rows.*,
        calendar.*,
        least(greatest(dbt_valid_from, starts_at), ends_at) as from_at,
        least(greatest(coalesce(dbt_valid_to, ends_at), starts_at), ends_at) as to_at
    from snapshot_rows
    cross join calendar
),

-- 最初の版は暦の始まりから（源泉の更新日時より前の出来事も結合できるように）
versions as (
    select
        *,
        row_number() over (partition by store_id order by from_at) as version_no
    from clamped
    where from_at < to_at
)

select
    dbt_scd_id as store_version_key,
    {{ dim_key('store_id') }} as store_key,
    store_name as store_name,
    case when version_no = 1 then starts_at else from_at end as valid_from,
    to_at as valid_to
from versions

union all

-- 不明メンバー（期間は暦の全体）
select {{ unknown_key() }}, {{ unknown_key() }}, '不明', starts_at, ends_at
from calendar
