-- adp.cleansing により生成（warehouse/cleansing/raw.returns.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'returns') }}
),

cleansed as (
    select
        return_id,
        order_item_id,
        returned_at,
        quantity,
        nullif(trim(return_reason), '') as return_reason
    from source
)

select * from cleansed
