-- adp.cleansing により生成（warehouse/cleansing/raw.products.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'products') }}
),

cleansed as (
    select
        product_id,
        nullif(trim(product_name), '') as product_name,
        nullif(trim(category), '') as category,
        nullif(trim(subcategory), '') as subcategory,
        nullif(trim(brand), '') as brand,
        list_price,
        unit_cost,
        launched_on
    from source
)

select * from cleansed
