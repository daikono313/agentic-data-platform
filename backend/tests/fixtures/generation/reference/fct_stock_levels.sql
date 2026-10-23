-- 手書きのリファレンス（生成器とは別の考え方で書く。ADR 0008、#138）。
-- 月末ごとに、その日までの最後の棚卸の数に、その棚卸より後・月末までの増減を足す。
-- 区切り（店舗 × 商品）ごとに、最初に棚卸のあった月から、記録の最後の月まで行を作る。
with moves as (
    select
        {{ dim_key('store_id') }} as store_key,
        {{ dim_key('product_id') }} as product_key,
        moved_at,
        inventory_id,
        movement_type,
        quantity
    from {{ ref('stg_inventory') }}
),

counts as (
    select * from moves where movement_type = 'count'
),

month_ends as (
    select date_day as snapshot_on
    from {{ ref('dim_date') }}
    where date_day = last_day(date_day)
        and date_day <= (select last_day(cast(max(moved_at) as date)) from moves)
),

keys as (
    select store_key, product_key, min(last_day(cast(moved_at as date))) as first_on
    from counts
    group by store_key, product_key
),

last_counts as (
    select
        k.store_key,
        k.product_key,
        e.snapshot_on,
        c.moved_at,
        c.inventory_id,
        c.quantity,
        row_number() over (
            partition by k.store_key, k.product_key, e.snapshot_on
            order by c.moved_at desc, c.inventory_id desc
        ) as rn
    from keys as k
    inner join month_ends as e on e.snapshot_on >= k.first_on
    inner join counts as c
        on c.store_key = k.store_key
        and c.product_key = k.product_key
        and cast(c.moved_at as date) <= e.snapshot_on
)

select
    l.store_key,
    l.product_key,
    l.snapshot_on,
    l.quantity + coalesce((
        select sum(m.quantity)
        from moves as m
        where m.store_key = l.store_key
            and m.product_key = l.product_key
            and m.movement_type <> 'count'
            and cast(m.moved_at as date) <= l.snapshot_on
            and (m.moved_at > l.moved_at or (m.moved_at = l.moved_at and m.inventory_id > l.inventory_id))
    ), 0) as stock_qty
from last_counts as l
where l.rn = 1
