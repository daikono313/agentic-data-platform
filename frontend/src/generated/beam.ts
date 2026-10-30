/* backend の adp.beam から生成。手で編集しない（pnpm gen:types） */

/**
 * 足し上げてよいか。単価は non_additive、残高は時間方向に semi_additive。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Additivity".
 */
export type Additivity = "additive" | "semi_additive" | "non_additive";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Aggregation".
 */
export type Aggregation = "sum" | "count" | "count_distinct" | "average" | "min" | "max";
/**
 * ソースデータで作れるか。
 */
export type Availability = "unknown" | "available" | "unavailable";
/**
 * ソースデータで作れるか。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Availability".
 */
export type Availability1 = "unknown" | "available" | "unavailable";
/**
 * イベントの種類。fct の種類を決める。
 */
export type StoryType = "discrete" | "recurring" | "evolving";
export type Aggregation1 = "sum" | "count" | "count_distinct" | "average" | "min" | "max";
/**
 * 足し上げてよいか。単価は non_additive、残高は時間方向に semi_additive。
 */
export type Additivity1 = "additive" | "semi_additive" | "non_additive";
/**
 * ソースデータで作れるか。
 */
export type Availability2 = "unknown" | "available" | "unavailable";
/**
 * last_value（記録が時点の値そのもの）か running_total（増減を積み上げる）
 */
export type StateMethod = "last_value" | "running_total";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "FilterOperator".
 */
export type FilterOperator = "=" | "!=" | "in" | "not_in";
export type LagUnit = "day" | "hour";
/**
 * How many 以外の 6W。How many は measure として別に持つ。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "W".
 */
export type W = "who" | "what" | "when" | "where" | "why" | "how";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "TimeGrain".
 */
export type TimeGrain = "day" | "week" | "month" | "quarter" | "year";
/**
 * 具体例の型（typical: 典型 / variant: 別パターン / repeat: 繰り返し / missing: 欠損 / group: グループ）
 */
export type ExampleKind = "typical" | "variant" | "repeat" | "missing" | "group";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Level".
 */
export type Level = "high" | "medium" | "low";
export type MetricType = "simple" | "ratio" | "derived" | "cumulative";
export type MetricStatus = "draft" | "confirmed" | "blocked";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "GapKind".
 */
export type GapKind = "data_unavailable" | "unanswered" | "conflict" | "deferred";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "FactType".
 */
export type FactType = "transaction" | "periodic_snapshot" | "accumulating_snapshot";
/**
 * 具体例（event story）の型（#142）。典型の例のほかに、数え方を確かめる例を集める。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "ExampleKind".
 */
export type ExampleKind1 = "typical" | "variant" | "repeat" | "missing" | "group";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "LagUnit".
 */
export type LagUnit1 = "day" | "hour";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "MetricStatus".
 */
export type MetricStatus1 = "draft" | "confirmed" | "blocked";
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "MetricType".
 */
export type MetricType1 = "simple" | "ratio" | "derived" | "cumulative";
/**
 * 定期的な状態（recurring）の値の求め方（ADR 0008）。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "StateMethod".
 */
export type StateMethod1 = "last_value" | "running_total";
/**
 * イベントの種類。fct の種類を決める。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "StoryType".
 */
export type StoryType1 = "discrete" | "recurring" | "evolving";

export interface BEAM {
  [k: string]: unknown;
}
/**
 * ディメンションの属性。dim のカラムになる。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Attribute".
 */
export interface Attribute {
  name: string;
  label: string;
  description?: string;
  examples?: string[];
  source?: SourceRef | null;
  availability?: Availability;
  /**
   * 値が変わったとき、過去の出来事を当時の値で見たいか（true なら、当時の値で切れる履歴（SCD Type 2）も作る。今の値の切り口はそのまま残る）
   */
  history_required?: boolean;
}
/**
 * ソースのどこから作るか。column か expr のどちらかを指定する。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "SourceRef".
 */
export interface SourceRef {
  /**
   * ソーステーブル（例: raw.orders）
   */
  table: string;
  column?: string | null;
  /**
   * column で表せない場合の SQL 式
   */
  expr?: string | null;
}
/**
 * BEAM 成果物のルート。beam/<name>.yml に保存される。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "BeamModel".
 */
export interface BeamModel {
  schema_version?: number;
  name: string;
  title: string;
  /**
   * ユーザーの最初の要求文
   */
  request: string;
  events?: BusinessEvent[];
  metrics?: MetricRequest[];
  gaps?: Gap[];
  finalized_at?: string | null;
}
/**
 * 「誰が・何をする」のビジネスイベント。fct 1 つに対応する。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "BusinessEvent".
 */
export interface BusinessEvent {
  /**
   * 例: customer_orders_product
   */
  name: string;
  /**
   * fct テーブル名の名詞部分。粒度を表す複数形（例: order_lines）
   */
  table_name?: string | null;
  /**
   * 例: 顧客が商品を注文する
   */
  label: string;
  /**
   * Who（主語）
   */
  subject: string;
  /**
   * 動詞
   */
  verb: string;
  /**
   * What（目的語）
   */
  object: string;
  story_type?: StoryType;
  /**
   * 1 行が何を表すか（日本語）
   */
  grain?: string | null;
  /**
   * 粒度を一意にするキー。縮退ディメンション名または dimension ref
   */
  grain_keys?: string[];
  measures?: Measure[];
  dimensions?: Dimension[];
  degenerate_dimensions?: DegenerateDimension[];
  /**
   * 指標を時間で集計するときの When の ref。When が複数あるときは必須
   */
  agg_time_dimension?: string | null;
  examples?: EventExample[];
  priority?: Level | null;
  impact?: Level | null;
  effort?: Level | null;
  source_tables?: string[];
}
/**
 * How many。fct の数値カラムになる。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Measure".
 */
export interface Measure {
  name: string;
  /**
   * 表示名（日本語）
   */
  label: string;
  description?: string;
  aggregation?: Aggregation1;
  additivity?: Additivity1;
  /**
   * 単位（円、個 など）
   */
  unit?: string | null;
  /**
   * semi_additive のとき、足し上げできない When の ref（例: 残高の date）
   */
  non_additive_dimension?: string | null;
  /**
   * 集計する列。1 行を 1 件と数える件数（count）なら書かない
   */
  source?: SourceRef | null;
  availability?: Availability2;
  /**
   * 定期的な状態（recurring）のイベントのとき、記録からの値の求め方
   */
  state?: StateSpec | null;
  /**
   * 段階を経て進む出来事（evolving）で、2 つの段階の間の時間を measure にするとき（source は書かない）
   */
  lag?: LagSpec | null;
}
/**
 * 定期的な状態（story_type: recurring）の measure を、記録からどう求めるか（#138、ADR 0008）。
 *
 * スナップショットは When の最小粒度（time_grain）の期間ごとに、粒度のキーから When を除いた
 * 区切り（店舗 × 商品など）で作る。記録の無い期間は前の期間の値を引き継ぐ。同じ時刻の記録は
 * 源泉の主キーの順に処理する。SQL は生成器が決まった形で作る（LLM にも人にも書かせない）。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "StateSpec".
 */
export interface StateSpec {
  method: StateMethod;
  /**
   * running_total のとき、値を数え直した数に置き換える行の条件（例: movement_type が count の棚卸）。最初のこの行より前の期間は値を出さない
   */
  reset?: MetricFilter[];
}
/**
 * 指標の絞り込み条件。Semantic Layer の filter に変換される。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "MetricFilter".
 */
export interface MetricFilter {
  /**
   * `<dimension ref>.<attribute>` または縮退ディメンション名
   */
  ref: string;
  operator: FilterOperator;
  /**
   * @minItems 1
   */
  values: [string | number | number | boolean, ...(string | number | number | boolean)[]];
}
/**
 * 段階を経て進む出来事（evolving）の、2 つの段階の間の時間（ラグ。#140）。
 *
 * 生成器が段階の日時の列から `date_diff(unit, start, end)` で計算する（日・時の境目を数える）。
 * 終わりの段階にまだ達していない行は NULL。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "LagSpec".
 */
export interface LagSpec {
  /**
   * 始まりの段階の When の ref
   */
  start: string;
  /**
   * 終わりの段階の When の ref（start より後の段階）
   */
  end: string;
  unit?: LagUnit;
}
/**
 * 6W のいずれかに対応するディメンション。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Dimension".
 */
export interface Dimension {
  w: W;
  /**
   * ディメンション名（例: customer）。dim_<name> になる
   */
  name: string;
  label: string;
  description?: string;
  /**
   * 同じディメンションを複数の役割で使う場合の役割名（例: ship_to）
   */
  role?: string | null;
  attributes?: Attribute[];
  /**
   * 粗い→細かいの順の属性名（例: region, prefecture）
   */
  hierarchy?: string[];
  /**
   * When の最小粒度
   */
  time_grain?: TimeGrain | null;
  /**
   * 段階を経て進む出来事（evolving）の When の、段階の順番（1 から。例: 注文 1・出荷 2・配達 3）
   */
  stage?: number | null;
  /**
   * ディメンション自身のソース上の自然キー。When は不要（カレンダーを生成）
   */
  business_key?: SourceRef | null;
  /**
   * イベント側のソースで、このディメンションを指すカラム（例: raw.orders.customer_id、When なら raw.orders.ordered_at）。役割ごとに異なる
   */
  fact_key?: SourceRef | null;
  /**
   * 1 つの出来事に複数の値が付く切り口（例: 1 注文に複数のプロモーション）のとき、値の源泉の表。金額・件数を付いた値の数で按分した、別の出来事（別の table_name）として記録する。1 回ごとの出来事だけ・1 つの出来事に 1 つまで・役割の名前（role）が要る。具体例の金額は、按分する前の元の 1 行の値を書く
   */
  bridge?: Bridge | null;
}
/**
 * 1 つの出来事に複数の値が付く切り口（多対多）の、値の源泉（ブリッジ。ADR 0010）。
 *
 * 出来事の 1 行に付いた値の数で、加算できる値（金額・件数）を均等に按分した fct を作る（割合の
 * 列 weight があれば、その割合で按分する）。
 * ディメンションの fact_key は、この表の値の列（例: raw.order_promotions.promotion_id）にする。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Bridge".
 */
export interface Bridge {
  /**
   * 値の源泉の表（例: raw.order_promotions。1 行が、注文などの 1 件に付いた値 1 つ）
   */
  table: string;
  event_key: SourceRef1;
  /**
   * ブリッジの側で、出来事を指す列（例: order_id）
   */
  bridge_key: string;
  /**
   * ブリッジの側で、値ごとの割合の列（例: share）。出来事ごとの合計で割って重みにする（合計が 0 か、NULL・負の値があれば均等）。無ければ付いた値の数で均等に割る
   */
  weight?: string | null;
}
/**
 * ソースのどこから作るか。column か expr のどちらかを指定する。
 */
export interface SourceRef1 {
  /**
   * ソーステーブル（例: raw.orders）
   */
  table: string;
  column?: string | null;
  /**
   * column で表せない場合の SQL 式
   */
  expr?: string | null;
}
/**
 * dim テーブルを持たない、fct に直接置く識別子（例: 注文番号）。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "DegenerateDimension".
 */
export interface DegenerateDimension {
  name: string;
  label: string;
  source: SourceRef;
}
/**
 * インタビューで集めた具体例（event story）。unit test の期待値になる。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "EventExample".
 */
export interface EventExample {
  kind?: ExampleKind;
  /**
   * 典型とどこが違うか（典型なら空でよい）
   */
  note?: string;
  /**
   * キーは measure 名、縮退ディメンション名、`<dimension ref>.<attribute>`、または When の dimension ref（値は日付・日時の文字列）
   */
  values: {
    [k: string]: string | number | boolean | null;
  };
}
/**
 * 「How many ○○ by △△」の形にした指標の要求。Semantic Layer の metric になる。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "MetricRequest".
 */
export interface MetricRequest {
  name: string;
  label: string;
  /**
   * ユーザーの元の言葉
   */
  question: string;
  /**
   * How many の形に言い換えた問い
   */
  framed_question: string;
  /**
   * この数字を見て何を判断・行動するか（目的を聞いたときのユーザーの答え）
   */
  purpose?: string | null;
  type?: MetricType;
  event: string;
  /**
   * simple / cumulative のとき
   */
  measure?: string | null;
  /**
   * ratio のとき（metric 名）
   */
  numerator?: string | null;
  /**
   * ratio のとき（metric 名）
   */
  denominator?: string | null;
  /**
   * derived のとき。他の metric 名を使った四則演算のみ
   */
  expr?: string | null;
  /**
   * cumulative のとき（例: '7 days'）。無ければ全期間の累計
   */
  window?: string | null;
  /**
   * 絞り込み条件の日本語の説明
   */
  filter?: string | null;
  /**
   * 絞り込み条件（すべて AND）。生成に使う
   */
  filters?: MetricFilter[];
  /**
   * `<dimension ref>.<attribute>`、縮退ディメンション名、または時間軸 `<When ref>.<day|week|month|quarter|year>` の一覧
   */
  group_by?: string[];
  status?: MetricStatus;
}
/**
 * 未解決事項。PR 本文に書かれ、data_unavailable は issue も起票される。
 *
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "Gap".
 */
export interface Gap {
  kind: GapKind;
  description: string;
  event?: string | null;
  w?: W | null;
  /**
   * 関係する measure / 属性の参照
   */
  related?: string | null;
}
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "EventMatrix".
 */
export interface EventMatrix {
  columns: MatrixColumn[];
  rows: MatrixRow[];
}
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "MatrixColumn".
 */
export interface MatrixColumn {
  name: string;
  label: string;
  w: W;
  conformed: boolean;
}
/**
 * This interface was referenced by `BEAM`'s JSON-Schema
 * via the `definition` "MatrixRow".
 */
export interface MatrixRow {
  event: string;
  label: string;
  fact_type: FactType;
  grain: string | null;
  priority: Level | null;
  impact: Level | null;
  effort: Level | null;
  measures: string[];
  dimensions: {
    [k: string]: string[];
  };
}
