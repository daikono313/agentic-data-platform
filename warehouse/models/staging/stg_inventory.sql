-- adp.cleansing により生成（warehouse/cleansing/raw.inventory.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('adp_inventory', 'inventory') }}
),

cleansed as (
    select
        inventory_id,
        store_id,
        product_id,
        moved_at,
        nullif(trim(movement_type), '') as movement_type,
        quantity,
        updated_at
    from source
)

select * from cleansed
