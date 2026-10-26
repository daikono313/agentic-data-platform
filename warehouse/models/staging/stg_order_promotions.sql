-- adp.cleansing により生成（warehouse/cleansing/raw.order_promotions.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'order_promotions') }}
),

cleansed as (
    select
        order_promotion_id,
        order_id,
        promotion_id
    from source
)

select * from cleansed
