"""Pure failure summaries: no app, database, Redis or source requests."""

import pytest

from app.collectors.base import AuthExpired, CollectorError, IncompleteCollection, safe_collection_error


@pytest.mark.parametrize("message,reason", [
    ("Source site identity is missing or invalid", "源站标识缺失或无效"),
    ("Source category list is missing or invalid", "分类列表缺失或无效"),
    ("Source category tree is missing or invalid", "分类树缺失或无效"),
    ("Source category tree exceeds the observed three levels", "分类树超出已验证的三级结构"),
    ("Source category is missing its identifier or name", "分类编号或名称缺失"),
    ("Source category tree repeated an identifier", "分类树出现重复编号"),
    ("Source category children are invalid", "分类子节点无效"),
    ("A configured source category is missing", "配置的分类已不存在"),
    ("Source returned no categories for a per-category collection", "按分类采集时未返回分类"),
    ("Source product page is missing or invalid", "商品分页数据缺失或无效"),
    ("Source product page has no trustworthy total count", "商品总数缺失或无效"),
    ("Source product total count is invalid", "商品总数无效"),
    ("Source product total count is not a nonnegative integer", "商品总数不是非负整数"),
    ("Source product count changed during pagination", "分页期间商品总数发生变化"),
    ("Source pagination repeated product identifiers", "商品分页出现重复编号"),
    ("Source returned more products than its declared count", "实际商品数超过源站声明总数"),
    ("Source pagination ended before its declared count", "商品分页提前结束，未达到源站声明总数"),
    ("Source pagination exceeded its configured page limit", "商品分页超出配置页数上限"),
])
def test_known_local_completeness_reason_is_explained(message, reason):
    assert safe_collection_error(IncompleteCollection(message)) == f"采集不完整：{reason}"


@pytest.mark.parametrize("message", [
    "fixture-secret",
    "Source product count changed during pagination fixture-secret",
    "fixture-secret Source product count changed during pagination",
    "Source product count changed during pagination\nfixture-secret",
    "Source product count changed during pagination ",
    "Source Product count changed during pagination",
    "",
])
def test_unknown_or_inexact_message_is_not_disclosed(message):
    rendered = safe_collection_error(IncompleteCollection(message))
    assert rendered == "采集执行异常：IncompleteCollection"
    assert "fixture-secret" not in rendered


@pytest.mark.parametrize("error", [
    ValueError("constructor fixture-secret"),
    ValueError("decrypt fixture-secret"),
    RuntimeError("close fixture-secret"),
    CollectorError("upstream fixture-secret"),
    RuntimeError("Source product count changed during pagination"),
    CollectorError("Source product count changed during pagination"),
    IncompleteCollection("Source product count changed during pagination", "fixture-secret"),
    IncompleteCollection({"message": "fixture-secret"}),
])
def test_other_types_or_arguments_keep_only_exception_type(error):
    rendered = safe_collection_error(error)
    assert rendered == f"采集执行异常：{type(error).__name__}"
    assert "fixture-secret" not in rendered


@pytest.mark.parametrize("message", [
    "Source account login has expired",
    "Source product count changed during pagination",
    "fixture-secret",
])
def test_authentication_errors_remain_for_the_existing_auth_branch(message):
    error = AuthExpired(message)
    assert safe_collection_error(error) == "采集执行异常：AuthExpired"
    assert isinstance(error, AuthExpired)
    assert error.args == (message,)


def test_unrecognized_subclass_cannot_reuse_a_trusted_message():
    class ExternalIncomplete(IncompleteCollection):
        pass

    error = ExternalIncomplete("Source product count changed during pagination")
    assert safe_collection_error(error) == "采集执行异常：ExternalIncomplete"


def test_unknown_exception_string_conversion_is_never_called():
    class SensitiveError(RuntimeError):
        def __str__(self):
            raise AssertionError("fixture-secret __str__ must not run")

    assert safe_collection_error(SensitiveError("fixture-secret")) == "采集执行异常：SensitiveError"
