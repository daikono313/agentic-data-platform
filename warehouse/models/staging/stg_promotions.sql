-- adp.cleansing により生成（warehouse/cleansing/raw.promotions.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'promotions') }}
),

cleansed as (
    select
        promotion_id,
        nullif(trim(promotion_name), '') as promotion_name,
        nullif(trim(promotion_type), '') as promotion_type,
        discount_rate,
        start_date,
        end_date
    from source
)

select * from cleansed
