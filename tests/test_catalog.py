"""Catalog contract and retrieval tests (text2sql_V1.md section 6)."""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from text2sql.catalog import (
    Catalog,
    CatalogConsistencyError,
    load_catalog,
    normalize_term,
)
from text2sql.catalog.models import ColumnMeta, MetricSemantic

EXPOSED = {
    "dim_user",
    "mart_user_behavior_summary",
    "mart_user_order_summary",
    "mart_campaign_summary",
    "mart_funnel_summary",
    "mart_retention_cohort",
}


def test_catalog_exposes_exactly_six_business_views(catalog: Catalog):
    assert set(catalog.exposed_tables) == EXPOSED
    assert set(catalog.allowed_tables) == EXPOSED


def test_catalog_never_exposes_internal_objects(catalog: Catalog):
    for blocked in (
        "raw_users",
        "raw_orders",
        "fact_behavior",
        "fact_order",
        "dim_product",
        "dim_campaign",
        "catalog_table",
        "catalog_column",
    ):
        assert blocked not in catalog.allowed_tables
        assert catalog.get_table(blocked) is None


def test_every_table_has_grain_description_and_primary_key(catalog: Catalog):
    for table_name in catalog.exposed_tables:
        table = catalog.get_table(table_name)
        assert table is not None
        assert table.grain, table_name
        assert table.description, table_name
        assert table.primary_key in catalog.column_names(table_name)


def test_metric_columns_have_aggregation_and_semantics(catalog: Catalog):
    for table_name in catalog.exposed_tables:
        for column in catalog.columns_of(table_name).values():
            if column.role in {"metric", "flag"}:
                assert column.aggregation != "none", column.qualified
    metrics = catalog.metric_guidance()
    for expected in ("净消费金额", "活动点击率", "点击后转化率", "漏斗用户数", "留存率"):
        assert expected in metrics


def test_ratio_columns_reference_existing_numerator_and_denominator(catalog: Catalog):
    for table_name in catalog.exposed_tables:
        columns = catalog.columns_of(table_name)
        for column in columns.values():
            if column.is_ratio:
                assert column.ratio_numerator in columns
                assert column.ratio_denominator in columns


def test_join_rules_are_registered_both_ways(catalog: Catalog):
    assert catalog.is_join_allowed("dim_user", "mart_user_order_summary")
    assert catalog.is_join_allowed("mart_user_order_summary", "dim_user")
    assert catalog.join_key("dim_user", "mart_user_order_summary") == ("user_id", "user_id")
    assert not catalog.is_join_allowed("mart_campaign_summary", "dim_user")
    assert not catalog.is_join_allowed("mart_funnel_summary", "mart_user_behavior_summary")


def test_normalize_term_is_unicode_and_case_insensitive():
    assert normalize_term("  Click_Rate ") == "clickrate"
    assert normalize_term("ＣＴＲ") == "ctr"
    assert normalize_term("净消费金额（元）") == "净消费金额元"


# --------------------------------------------------------------- search recall
@pytest.mark.parametrize(
    "question, metrics, dimensions, keywords, expected_top",
    [
        ("按渠道统计用户数", ["用户数"], ["渠道"], [], "dim_user"),
        ("各会员等级的用户数量", ["用户数量"], ["会员等级"], [], "dim_user"),
        ("行为次数最多的 10 个用户", ["行为次数"], [], ["用户"], "mart_user_behavior_summary"),
        (
            "各渠道的有效订单数和净消费金额",
            ["有效订单数", "净消费金额"],
            ["渠道"],
            [],
            "mart_user_order_summary",
        ),
        ("哪些用户是复购用户", [], [], ["复购用户"], "mart_user_order_summary"),
        (
            "各活动的曝光量、点击率和转化率",
            ["曝光次数", "点击率", "转化率"],
            ["活动"],
            [],
            "mart_campaign_summary",
        ),
        ("点击率最高的 5 个活动", ["点击率"], ["活动"], [], "mart_campaign_summary"),
        ("各行为阶段的去重用户数", ["去重用户数"], ["行为阶段"], [], "mart_funnel_summary"),
        ("每个 cohort 的 7 日留存率", ["7日留存率"], ["cohort"], [], "mart_retention_cohort"),
    ],
)
def test_search_hits_expected_table(
    catalog: Catalog, question, metrics, dimensions, keywords, expected_top
):
    result = catalog.search(
        question=question,
        keywords=keywords,
        dimensions=dimensions,
        metrics=metrics,
    )
    tables = [candidate["table"] for candidate in result["candidates"]]
    assert expected_top in tables, tables
    assert result["catalog_version"] == catalog.version
    assert all(name in EXPOSED for name in tables)


def test_search_scores_are_sorted_descending(catalog: Catalog):
    result = catalog.search(
        question="各渠道的有效订单数和净消费金额",
        keywords=[],
        dimensions=["渠道"],
        metrics=["有效订单数", "净消费金额"],
    )
    scores = [candidate["score"] for candidate in result["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert all(score >= 0.30 for score in scores)


def test_search_never_guesses_when_nothing_matches(catalog: Catalog):
    result = catalog.search(
        question="统计各商品的名称和品牌",
        keywords=["商品名称", "品牌"],
        dimensions=["品牌"],
        metrics=["商品名称"],
    )
    assert result["candidates"] == []


def test_search_respects_max_tables_cap(catalog: Catalog):
    result = catalog.search(
        question="渠道 行为次数 净消费金额 活动 漏斗 留存",
        keywords=["渠道", "活动", "漏斗", "留存"],
        dimensions=["渠道", "活动"],
        metrics=["行为次数", "净消费金额", "曝光量", "去重用户数", "7日留存率"],
        max_tables=4,
    )
    assert len(result["candidates"]) <= 4


def test_search_returns_only_registered_join_rules(catalog: Catalog):
    result = catalog.search(
        question="各渠道的净消费金额",
        keywords=[],
        dimensions=["渠道"],
        metrics=["净消费金额"],
    )
    pairs = {(rule["left_table"], rule["right_table"]) for rule in result["join_rules"]}
    registered = {
        (rule["left_table"], rule["right_table"]) for rule in catalog.join_rules
    }
    assert pairs <= registered


def test_metric_terms_outrank_keyword_noise(catalog: Catalog):
    metric_result = catalog.search(
        question="各活动的点击率",
        keywords=[],
        dimensions=[],
        metrics=["点击率"],
    )
    keyword_result = catalog.search(
        question="订单订单订单",
        keywords=["订单"],
        dimensions=[],
        metrics=[],
    )
    assert metric_result["candidates"][0]["score"] > keyword_result["candidates"][0]["score"]


def test_funnel_users_do_not_resolve_to_dim_user_count(catalog: Catalog):
    resolved = catalog.resolve_metric("去重用户数")
    assert resolved is not None
    table, column, _ = resolved
    assert table.table_name == "mart_funnel_summary"
    assert column.column_name == "unique_users"


def test_filter_hint_resolves_to_the_exact_flag_not_a_generic_synonym(catalog: Catalog):
    """"是否复购用户" must win over the 2-character "用户" synonym of user_id."""
    resolved = catalog.resolve_dimension("是否复购用户")
    assert resolved is not None
    table, column, _ = resolved
    assert table.table_name == "mart_user_order_summary"
    assert column.column_name == "is_repeat_buyer"


def test_filter_hint_for_vip_level_resolves_to_dim_user(catalog: Catalog):
    resolved = catalog.resolve_dimension("会员等级")
    assert resolved is not None
    table, column, _ = resolved
    assert (table.table_name, column.column_name) == ("dim_user", "vip_level")


def test_click_through_conversion_is_distinct_from_exposure_conversion(catalog: Catalog):
    metric = next(m for m in catalog._metrics if m.name == "点击后转化率")
    assert "clicked_count" in metric.formula
    assert metric.formula != next(
        m for m in catalog._metrics if m.name == "活动曝光转化率"
    ).formula


# ------------------------------------------------------------ consistency rules
def _write_catalog(tmp_path, mutate) -> Catalog:
    from text2sql.config import CATALOG_PATH

    source = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    mutate(source)
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(source, allow_unicode=True), encoding="utf-8")
    return Catalog.from_yaml(path)


def test_static_consistency_rejects_table_without_grain(tmp_path):
    def mutate(source):
        source["tables"][0]["grain"] = ""

    with pytest.raises(CatalogConsistencyError) as excinfo:
        _write_catalog(tmp_path, mutate)
    assert "grain" in str(excinfo.value)


def test_static_consistency_rejects_unknown_column_in_join_rule(tmp_path):
    def mutate(source):
        source["join_rules"][0]["left_column"] = "not_a_column"

    with pytest.raises(CatalogConsistencyError) as excinfo:
        _write_catalog(tmp_path, mutate)
    assert "not_a_column" in str(excinfo.value)


def test_static_consistency_rejects_metric_without_formula(tmp_path):
    def mutate(source):
        source["metrics"][0]["formula"] = ""

    with pytest.raises(CatalogConsistencyError) as excinfo:
        _write_catalog(tmp_path, mutate)
    assert "formula" in str(excinfo.value)


def test_static_consistency_rejects_conflicting_synonyms(tmp_path):
    def mutate(source):
        source["metrics"].append(
            {
                "name": "冲突指标",
                "table": "dim_user",
                "column": "user_id",
                "aggregation": "count",
                "business_name": "冲突指标",
                "synonyms": ["用户数"],
                "description": "与用户数同义词冲突",
                "formula": "COUNT(user_id)",
            }
        )

    with pytest.raises(CatalogConsistencyError) as excinfo:
        _write_catalog(tmp_path, mutate)
    assert "同义词" in str(excinfo.value)


def test_live_consistency_check_detects_missing_column(catalog: Catalog):
    live = {
        "dim_user": ["user_id", "gender"],
        "mart_user_behavior_summary": sorted(catalog.column_names("mart_user_behavior_summary")),
        "mart_user_order_summary": sorted(catalog.column_names("mart_user_order_summary")),
        "mart_campaign_summary": sorted(catalog.column_names("mart_campaign_summary")),
        "mart_funnel_summary": sorted(catalog.column_names("mart_funnel_summary")),
        "mart_retention_cohort": sorted(catalog.column_names("mart_retention_cohort")),
    }
    with pytest.raises(CatalogConsistencyError) as excinfo:
        catalog.verify_live(live)
    assert "dim_user.city_tier" in str(excinfo.value)


def test_live_consistency_check_passes_against_real_database(catalog: Catalog, executor):
    catalog.verify_live(executor.live_columns(list(EXPOSED)))


def test_catalog_version_mismatch_is_rejected(settings):
    from text2sql.catalog import load_catalog as load

    with pytest.raises(CatalogConsistencyError):
        load(replace(settings, catalog_version="v9"))
