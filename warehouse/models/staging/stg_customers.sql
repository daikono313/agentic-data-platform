-- adp.cleansing により生成（warehouse/cleansing/raw.customers.yml）。手で編集しない（make cleansing）
with source as (
    select * from {{ source('raw', 'customers') }}
),

cleansed as (
    select
        customer_id,
        nullif(trim(customer_name), '') as customer_name,
        nullif(trim(gender), '') as gender,
        birth_date,
        nullif(trim(prefecture), '') as prefecture,
        nullif(trim(region), '') as region,
        nullif(trim(membership_rank), '') as membership_rank,
        signup_date,
        updated_at
    from source
)

select * from cleansed
