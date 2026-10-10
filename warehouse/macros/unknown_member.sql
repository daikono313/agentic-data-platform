{#- ディメンションに該当行が無い（NULL）ときに使う不明メンバーのキー。dim には同じキーの行を1行入れる -#}
{% macro unknown_key() -%}
    {{ dbt_utils.generate_surrogate_key(["'-1'"]) }}
{%- endmacro %}

{#- 自然キーから dim のサロゲートキーを作る。NULL は不明メンバーのキーになる -#}
{% macro dim_key(column) -%}
    {{ dbt_utils.generate_surrogate_key(["coalesce(cast(" ~ column ~ " as varchar), '-1')"]) }}
{%- endmacro %}
