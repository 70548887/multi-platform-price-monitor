"""Text-only cache stays immutable and never caches live product eligibility."""
from types import SimpleNamespace

from app.ai_suggestions import (semantic_components, semantic_signature, matches_target,
                                _cached_semantic_components)


def test_semantic_cache_normalizes_text_and_returns_immutable_parts():
    _cached_semantic_components.cache_clear()
    first=semantic_components('DY 视频 点赞')
    second=semantic_components('ＤＹ 视频 点赞')
    assert first is second
    assert first==(frozenset({'抖音'}),frozenset({'点赞'}),frozenset({'视频'}))
    assert all(isinstance(value,frozenset) for value in first)
    assert _cached_semantic_components.cache_info().hits==1
    assert _cached_semantic_components.cache_info().maxsize==16384


def test_cached_text_does_not_hide_changed_source_or_target_signature():
    product=SimpleNamespace(name='DY视频点赞',category_name='抖音业务')
    assert matches_target(product,('抖音','点赞','视频')) is True
    product.name='快手视频点赞'
    assert matches_target(product,('抖音','点赞','视频')) is False
    assert matches_target(product,('快手','点赞','视频')) is False  # conflicting source category
    product.category_name='快手业务'
    assert matches_target(product,('快手','点赞','视频')) is True
    product.name='DY视频点赞评论套餐'
    assert semantic_signature(product.name+' '+product.category_name) is None


def test_oversized_labels_are_scanned_but_not_kept_in_cache():
    _cached_semantic_components.cache_clear()
    result=semantic_components('抖音视频点赞'+'描述'*3000)
    assert result[0]==frozenset({'抖音'})
    assert _cached_semantic_components.cache_info().currsize==0
