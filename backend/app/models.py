from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
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
    encrypted_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_status: Mapped[str] = mapped_column(String(24), default='missing')
    active_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class CanonicalProduct(Base):
    __tablename__ = 'canonical_products'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(80), default='')
    currency: Mapped[str] = mapped_column(String(8), default='CNY')
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


class UpdateRun(Base):
    __tablename__ = 'update_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(24), default='check')
    current_version: Mapped[str] = mapped_column(String(32))
    target_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tag: Mapped[str | None] = mapped_column(String(40), nullable=True)
    repository: Mapped[str] = mapped_column(String(240), default='')
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
