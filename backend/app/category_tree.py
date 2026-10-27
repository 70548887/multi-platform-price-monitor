"""Small bounded category trees and an additive migration for legacy catalogues."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import inspect, select, text

from .models import ComparisonCategory


MAX_CATEGORY_LEVELS = 8


def migrate_category_hierarchy(connection):
    """Keep old flat rows as roots and replace global name uniqueness with siblings."""
    connection.execute(text('ALTER TABLE comparison_categories ADD COLUMN IF NOT EXISTS parent_id INTEGER REFERENCES comparison_categories(id)'))
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote
    for constraint in inspector.get_unique_constraints('comparison_categories'):
        if constraint['column_names'] == ['name']:
            connection.execute(text('ALTER TABLE comparison_categories DROP CONSTRAINT ' + quote(constraint['name'])))
    # Cover installations which used a standalone unique index rather than the
    # original SQLAlchemy unique constraint. Quote names returned by PostgreSQL.
    for index in inspect(connection).get_indexes('comparison_categories'):
        if index.get('unique') and index.get('column_names') == ['name']:
            connection.execute(text('DROP INDEX ' + quote(index['name'])))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_comparison_categories_parent_id ON comparison_categories(parent_id)'))
    connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_comparison_categories_parent_name ON comparison_categories(COALESCE(parent_id, 0), name)'))


class CategoryTree:
    def __init__(self, rows):
        self.by_id = {row.id: row for row in rows}
        self.children = {}
        for row in self.by_id.values():
            self.children.setdefault(row.parent_id, []).append(row)
        for children in self.children.values():
            children.sort(key=lambda item: (item.sort_order, item.id))

    @classmethod
    def load(cls, db):
        return cls(db.scalars(select(ComparisonCategory)).all())

    def path(self, category_id):
        if category_id is None:
            return []
        current = category_id
        seen, result = set(), []
        while current is not None:
            if current in seen or current not in self.by_id:
                raise HTTPException(409, '比价类目结构异常，请联系管理员')
            seen.add(current)
            row = self.by_id[current]
            result.append(row)
            current = row.parent_id
        return list(reversed(result))

    def path_dicts(self, category_id):
        return [{'id': row.id, 'name': row.name} for row in self.path(category_id)]

    def descendants(self, category_id):
        if category_id not in self.by_id:
            return set()
        pending, result = [category_id], set()
        while pending:
            current = pending.pop()
            if current in result:
                raise HTTPException(409, '比价类目结构异常，请联系管理员')
            result.add(current)
            pending.extend(row.id for row in self.children.get(current, []))
        return result

    def ordered_ids(self):
        result, seen, pending = [], set(), list(reversed(self.children.get(None, [])))
        while pending:
            row = pending.pop()
            if row.id in seen:
                raise HTTPException(409, '比价类目结构异常，请联系管理员')
            seen.add(row.id)
            result.append(row.id)
            pending.extend(reversed(self.children.get(row.id, [])))
        if len(result) != len(self.by_id):
            raise HTTPException(409, '比价类目结构异常，请联系管理员')
        return result

    def validate_parent(self, category_id, parent_id):
        if parent_id is not None and parent_id not in self.by_id:
            raise HTTPException(422, '父类目不存在')
        subtree = self.descendants(category_id) if category_id is not None else set()
        if parent_id in subtree:
            raise HTTPException(422, '不能将类目移到自身或子类目下')
        parent_levels = len(self.path(parent_id))
        subtree_levels = 1
        if category_id is not None:
            original_levels = len(self.path(category_id))
            subtree_levels += max((len(self.path(identifier)) - original_levels for identifier in subtree), default=0)
        if parent_levels + subtree_levels > MAX_CATEGORY_LEVELS:
            raise HTTPException(422, f'比价类目最多支持 {MAX_CATEGORY_LEVELS} 层')
