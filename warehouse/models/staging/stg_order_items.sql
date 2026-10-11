-- adp.cleansing により生成（warehouse/cleansing/raw.order_items.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'order_items') }}
),

cleansed as (
    select
        order_item_id,
        order_id,
        product_id,
        quantity,
        unit_price,
        discount_amount
    from source
)

select * from cleansed
