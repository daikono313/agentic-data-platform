-- adp.cleansing により生成（warehouse/cleansing/raw.stores.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'stores') }}
),

cleansed as (
    select
        store_id,
        nullif(trim(store_name), '') as store_name,
        nullif(trim(channel), '') as channel,
        nullif(trim(prefecture), '') as prefecture,
        nullif(trim(region), '') as region,
        opened_on
    from source
)

select * from cleansed
