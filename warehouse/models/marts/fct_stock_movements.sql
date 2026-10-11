-- adp により生成
-- 元イベント: 店舗が商品の在庫を動かす（入庫・棚卸・移動出庫・廃棄）（transaction）
-- 粒度: 在庫の動き 1 件（入庫・棚卸・移動出庫・廃棄の記録 1 行）
select
    t0.inventory_id as inventory_id,
    cast(t0.moved_at as date) as moved_on,
    t0.moved_at as moved_at,
    {{ dim_key('t0.store_id') }} as store_key,
    {{ dim_key('t0.product_id') }} as product_key,
    t0.movement_type as movement_type,
    t0.quantity as movement_qty
from {{ ref('stg_inventory') }} as t0
