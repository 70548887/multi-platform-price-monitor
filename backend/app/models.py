from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default='viewer')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Platform(Base):
    __tablename__ = 'platforms'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(500))
    platform_type: Mapped[str] = mapped_column(String(40), default='yile', server_default='yile')
    adapter: Mapped[str] = mapped_column(String(40), default='xiaohong66')
    account_label: Mapped[str] = mapped_column(String(120), default='主采集账号')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    request_interval: Mapped[Decimal] = mapped_column(Numeric, default=Decimal('1'))
    proxy_route: Mapped[str] = mapped_column(String(16), default='direct', server_default='direct')
    encrypted_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_status: Mapped[str] = mapped_column(String(24), default='missing')
    active_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformSubmission(Base):
    __tablename__ = 'platform_submissions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(500))
    notes: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    admin_note: Mapped[str] = mapped_column(Text, default='')
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserPlatformAccess(Base):
    """Explicit reader grants. Administrators obtain access through their role."""
    __tablename__ = 'user_platform_access'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey('platforms.id', ondelete='CASCADE'), primary_key=True)


class Rule(Base):
    __tablename__ = 'rules'
    __table_args__ = (UniqueConstraint('platform_id', 'version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey('platforms.id'))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default='draft')
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default='')
    tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    test_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemNetworkSettings(Base):
    __tablename__ = 'system_network_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    tunnel_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    tunnel_encrypted_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    overseas_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    overseas_encrypted_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionRun(Base):
    __tablename__ = 'collection_runs'
    __table_args__ = (UniqueConstraint('platform_id', 'scheduled_at'), Index('ix_run_status', 'status', 'scheduled_at'))
    id: Mapped[int] = mapped_column(primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey('platforms.id'))
    rule_id: Mapped[int] = mapped_column(ForeignKey('rules.id'))
    account_label: Mapped[str] = mapped_column(String(120))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default='queued')
    products_seen: Mapped[int] = mapped_column(Integer, default=0)
    expected_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class ComparisonCategory(Base):
    __tablename__ = 'comparison_categories'
    __table_args__ = (Index('uq_comparison_categories_parent_name', text('COALESCE(parent_id, 0)'), 'name', unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey('comparison_categories.id'), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default='', server_default='')
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CanonicalProduct(Base):
    __tablename__ = 'canonical_products'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(80), default='')
    currency: Mapped[str] = mapped_column(String(8), default='CNY')
    category_id: Mapped[int | None] = mapped_column(ForeignKey('comparison_categories.id'), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default='', server_default='')
    match_mode: Mapped[str] = mapped_column(String(16), default='exact', server_default='exact')
    mapping_revision: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Product(Base):
    __tablename__ = 'products'
    __table_args__ = (UniqueConstraint('platform_id', 'account_label', 'source_id'), Index('ix_product_platform_category', 'platform_id', 'category_name'))
    id: Mapped[int] = mapped_column(primary_key=True)
    platform_id: Mapped[int] = mapped_column(ForeignKey('platforms.id'))
    account_label: Mapped[str] = mapped_column(String(120))
    source_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(Text)
    category_id: Mapped[str] = mapped_column(String(100), default='')
    category_name: Mapped[str] = mapped_column(String(200), default='')
    price: Mapped[Decimal] = mapped_column(Numeric)
    display_quantity: Mapped[Decimal] = mapped_column(Numeric, default=Decimal('1'))
    unit: Mapped[str] = mapped_column(String(80), default='')
    currency: Mapped[str] = mapped_column(String(8), default='CNY')
    status: Mapped[str] = mapped_column(String(24), default='active')
    stock: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    stock_unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    quality: Mapped[list] = mapped_column(JSON, default=list)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_id: Mapped[int] = mapped_column(ForeignKey('collection_runs.id'))
    canonical_id: Mapped[int | None] = mapped_column(ForeignKey('canonical_products.id'), nullable=True)
    mapping_revision: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    mapping_origin: Mapped[str] = mapped_column(String(16), default='manual', server_default='manual')
    ai_application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Observation(Base):
    __tablename__ = 'observations'
    __table_args__ = (UniqueConstraint('run_id', 'product_id'), Index('ix_observation_product_time', 'product_id', 'observed_at'))
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'))
    run_id: Mapped[int] = mapped_column(ForeignKey('collection_runs.id'))
    rule_id: Mapped[int] = mapped_column(ForeignKey('rules.id'))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    name: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric)
    display_quantity: Mapped[Decimal] = mapped_column(Numeric)
    unit: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(8), default='CNY')
    status: Mapped[str] = mapped_column(String(24))
    stock: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    stock_unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    quality: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class DailyAggregate(Base):
    __tablename__ = 'daily_aggregates'
    __table_args__ = (UniqueConstraint('product_id', 'day', 'unit', 'currency', 'display_quantity'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'))
    day: Mapped[str] = mapped_column(String(10))
    unit: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(8))
    display_quantity: Mapped[Decimal] = mapped_column(Numeric)
    minimum: Mapped[Decimal] = mapped_column(Numeric)
    maximum: Mapped[Decimal] = mapped_column(Numeric)
    average: Mapped[Decimal] = mapped_column(Numeric)
    count: Mapped[int] = mapped_column(Integer)


class Event(Base):
    __tablename__ = 'events'
    __table_args__ = (UniqueConstraint('dedupe_key'), Index('ix_event_time', 'created_at'))
    id: Mapped[int] = mapped_column(primary_key=True)
    platform_id: Mapped[int | None] = mapped_column(ForeignKey('platforms.id'), nullable=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey('products.id'), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey('collection_runs.id'), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(16), default='info')
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(250), unique=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ErrorCapture(Base):
    __tablename__ = 'error_captures'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('collection_runs.id'))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AISettings(Base):
    __tablename__ = 'ai_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    base_url: Mapped[str] = mapped_column(String(500), default='')
    encrypted_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(120), default='')
    api_format: Mapped[str] = mapped_column(String(32),default='chat_completions',server_default='chat_completions')
    reasoning_effort: Mapped[str] = mapped_column(String(16),default='auto',server_default='auto')
    max_output_tokens: Mapped[int] = mapped_column(Integer,default=4000,server_default='4000')
    daily_time: Mapped[str] = mapped_column(String(5), default='09:00')
    proxy_route: Mapped[str] = mapped_column(String(16), default='direct')
    sample_limit: Mapped[int] = mapped_column(Integer, default=100)
    daily_product_limit: Mapped[int] = mapped_column(Integer, default=1000, server_default='1000')
    daily_request_limit: Mapped[int] = mapped_column(Integer, default=20, server_default='20')
    auto_apply: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    json_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIAnalysisRun(Base):
    __tablename__ = 'ai_analysis_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    trigger: Mapped[str] = mapped_column(String(16), default='manual')
    actor_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    settings_revision: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(120), default='')
    api_format: Mapped[str] = mapped_column(String(32),default='chat_completions',server_default='chat_completions')
    reasoning_effort: Mapped[str] = mapped_column(String(16),default='auto',server_default='auto')
    max_output_tokens: Mapped[int] = mapped_column(Integer,default=4000,server_default='4000')
    purpose: Mapped[str] = mapped_column(String(16),default='review',server_default='review')
    requested_limit: Mapped[int | None] = mapped_column(Integer,nullable=True)
    target_group_id: Mapped[int | None] = mapped_column(Integer,nullable=True)
    auto_apply: Mapped[bool] = mapped_column(Boolean,default=False,server_default='false')
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=2, server_default='1')
    stage: Mapped[str] = mapped_column(String(32), default='queued', server_default='queued')
    planned_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    checked_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AIAnalysisBatch(Base):
    __tablename__ = 'ai_analysis_batches'
    __table_args__ = (UniqueConstraint('run_id', 'ordinal'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('ai_analysis_runs.id'), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    product_ids: Mapped[list] = mapped_column(JSON, default=list)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    diagnostic: Mapped[dict] = mapped_column(JSON,default=dict,server_default='{}')
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(String(500), default='')
    request_day: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIDailyQuota(Base):
    __tablename__ = 'ai_daily_quotas'
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    products_used: Mapped[int] = mapped_column(Integer, default=0)
    requests_used: Mapped[int] = mapped_column(Integer, default=0)


class AIProductCheck(Base):
    __tablename__ = 'ai_product_checks'
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_batch_id: Mapped[int] = mapped_column(ForeignKey('ai_analysis_batches.id'))


class AISuggestion(Base):
    __tablename__ = 'ai_suggestions'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('ai_analysis_runs.id'), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('ai_analysis_batches.id'))
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposed_category_path: Mapped[str] = mapped_column(String(500), default='', server_default='')
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_name: Mapped[str] = mapped_column(String(200), default='')
    reason: Mapped[str] = mapped_column(Text, default='')
    blocked_reason: Mapped[str] = mapped_column(Text, default='')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    products: Mapped[list] = mapped_column(JSON, default=list)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIApplication(Base):
    __tablename__ = 'ai_applications'
    id: Mapped[int] = mapped_column(primary_key=True)
    suggestion_id: Mapped[int] = mapped_column(ForeignKey('ai_suggestions.id'), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default='applied')
    actor_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    group_id: Mapped[int] = mapped_column(Integer)
    group_name: Mapped[str] = mapped_column(String(200))
    category_path: Mapped[str] = mapped_column(String(1000), default='')
    products: Mapped[list] = mapped_column(JSON, default=list)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UpdateRun(Base):
    __tablename__ = 'update_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(24), default='check')
    current_version: Mapped[str] = mapped_column(String(32))
    target_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tag: Mapped[str | None] = mapped_column(String(40), nullable=True)
    repository: Mapped[str] = mapped_column(String(240), default='')
    pending_job: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default='checking', index=True)
    message: Mapped[str] = mapped_column(Text, default='')
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    release: Mapped[dict] = mapped_column(JSON, default=dict)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    package: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backup_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    backup_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    actor: Mapped[str] = mapped_column(String(80), default='system')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
