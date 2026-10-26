"""Stable, source-independent collector input and output types."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
import re


class CollectorError(Exception):
    """The source could not be read or its response was invalid."""


class AuthExpired(CollectorError):
    """The source requires the operator to reconnect the account."""


class IncompleteCollection(CollectorError):
    """A batch cannot be asserted to contain the complete requested catalogue."""


_SAFE_INCOMPLETE_COLLECTION_REASONS = {
    "Source site identity is missing or invalid": "源站标识缺失或无效",
    "Source category list is missing or invalid": "分类列表缺失或无效",
    "Source category tree is missing or invalid": "分类树缺失或无效",
    "Source category tree exceeds the observed three levels": "分类树超出已验证的三级结构",
    "Source category is missing its identifier or name": "分类编号或名称缺失",
    "Source category tree repeated an identifier": "分类树出现重复编号",
    "Source category children are invalid": "分类子节点无效",
    "A configured source category is missing": "配置的分类已不存在",
    "Source returned no categories for a per-category collection": "按分类采集时未返回分类",
    "Source product page is missing or invalid": "商品分页数据缺失或无效",
    "Source product page has no trustworthy total count": "商品总数缺失或无效",
    "Source product total count is invalid": "商品总数无效",
    "Source product total count is not a nonnegative integer": "商品总数不是非负整数",
    "Source product count changed during pagination": "分页期间商品总数发生变化",
    "Source pagination repeated product identifiers": "商品分页出现重复编号",
    "Source returned more products than its declared count": "实际商品数超过源站声明总数",
    "Source pagination ended before its declared count": "商品分页提前结束，未达到源站声明总数",
    "Source pagination exceeded its configured page limit": "商品分页超出配置页数上限",
}


def safe_collection_error(exc: BaseException) -> str:
    """Explain only exact, local completeness guards; never expose other text.

    Authentication errors remain the caller's separate AuthExpired branch.
    Inspect args directly so an unknown exception's __str__ is never invoked.
    """
    if type(exc) is IncompleteCollection and len(exc.args) == 1 and type(exc.args[0]) is str:
        reason = _SAFE_INCOMPLETE_COLLECTION_REASONS.get(exc.args[0])
        if reason is not None:
            return f"采集不完整：{reason}"
    if type(exc) is CollectorError and len(exc.args) == 1 and type(exc.args[0]) is str:
        safe = {
            'Source proxy configuration is invalid': '采集代理配置无效，请检查系统设置',
            'Source proxy request failed after limited retries': '采集代理连接失败，有限重试后仍未恢复',
            'Source network request failed after limited retries': '采集网络请求失败，有限重试后仍未恢复',
            '采集代理路由无效，请检查平台代理设置': '采集代理路由无效，请检查平台代理设置',
            '平台指定的采集代理尚未配置或未启用': '平台指定的采集代理尚未配置或未启用',
            '采集代理配置无法读取，请在系统设置中重新保存': '采集代理配置无法读取，请在系统设置中重新保存',
        }
        if exc.args[0] in safe:
            return safe[exc.args[0]]
        match = re.fullmatch(r'Source HTTP (429|5[0-9]{2}) after limited retries', exc.args[0])
        if match:
            return f'源站 HTTP {match[1]}，有限重试后仍未恢复'
    return f"采集执行异常：{type(exc).__name__}"


@dataclass(slots=True)
class SourceProduct:
    source_id: str
    name: str
    category_id: str
    category_name: str
    price: Decimal
    unit: str
    display_quantity: Decimal
    status: str
    stock: Decimal | None
    stock_unlimited: bool
    raw: dict[str, Any]
    quality: list[str] = field(default_factory=list)
