"""Additive migration preserves old reports and manual product links."""
from sqlalchemy import text


def migrate_ai_center(connection):
    columns = {
        'ai_settings': {'daily_product_limit': 'INTEGER NOT NULL DEFAULT 1000', 'daily_request_limit': 'INTEGER NOT NULL DEFAULT 20', 'auto_apply': 'BOOLEAN NOT NULL DEFAULT false'},
        'ai_analysis_runs': {'schema_version': 'INTEGER NOT NULL DEFAULT 1', 'stage': "VARCHAR(32) NOT NULL DEFAULT 'queued'", 'planned_count': 'INTEGER NOT NULL DEFAULT 0', 'checked_count': 'INTEGER NOT NULL DEFAULT 0', 'failed_count': 'INTEGER NOT NULL DEFAULT 0', 'stop_requested': 'BOOLEAN NOT NULL DEFAULT false', 'heartbeat_at': 'TIMESTAMPTZ', 'parent_run_id': 'INTEGER'},
        'products': {'mapping_revision': 'INTEGER NOT NULL DEFAULT 0', 'mapping_origin': "VARCHAR(16) NOT NULL DEFAULT 'manual'", 'ai_application_id': 'INTEGER'},
        'canonical_products': {'mapping_revision': 'INTEGER NOT NULL DEFAULT 0'},
        'ai_suggestions': {'proposed_category_path': "VARCHAR(500) NOT NULL DEFAULT ''"},
    }
    for table in ('ai_settings','ai_analysis_runs'):
        columns[table].update(api_format="VARCHAR(32) NOT NULL DEFAULT 'chat_completions'",
                              reasoning_effort="VARCHAR(16) NOT NULL DEFAULT 'auto'",
                              max_output_tokens='INTEGER NOT NULL DEFAULT 4000')
    for table, fields in columns.items():
        for name, definition in fields.items():
            connection.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}'))
