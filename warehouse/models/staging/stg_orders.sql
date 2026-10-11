-- adp.cleansing により生成（warehouse/cleansing/raw.orders.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'orders') }}
),

cleansed as (
    select
        order_id,
        customer_id,
        store_id,
        ordered_at,
        cast(ordered_at as date) as ordered_on,
        promotion_id,
        nullif(trim(order_status), '') as order_status,
        nullif(trim(payment_method), '') as payment_method,
        updated_at
    from source
)

select * from cleansed
