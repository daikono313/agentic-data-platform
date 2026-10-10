with spine as (
    {{ dbt_utils.date_spine(
        datepart="day",
        start_date="cast('" ~ var('adp_date_start') ~ "' as date)",
        end_date="cast('" ~ var('adp_date_end') ~ "' as date)"
    ) }}
)

select
    cast(date_day as date) as date_day,
    extract(year from date_day) as year,
    extract(quarter from date_day) as quarter,
    extract(month from date_day) as month,
    cast(date_trunc('month', date_day) as date) as month_start,
    cast(date_trunc('week', date_day) as date) as week_start,
    isodow(date_day) as day_of_week,
    case isodow(date_day)
        when 1 then '月' when 2 then '火' when 3 then '水' when 4 then '木'
        when 5 then '金' when 6 then '土' else '日'
    end as day_of_week_name,
    isodow(date_day) in (6, 7) as is_weekend
from spine
