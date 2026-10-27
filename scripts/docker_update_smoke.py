"""Explicit, real Linux/Docker upgrade and rollback acceptance in a disposable project.

Run only with --run. No production .env, platforms, sessions or databases are
copied. A new Compose project, fresh volumes and loopback port are used. This
script is intentionally outside pytest's automatic test collection.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(SCRIPT_ROOT.parent / 'backend'))

from app.update_common import atomic_write, read_signed_json, write_signed_json  # noqa: E402
from package_release import build_release, collect_files, version_from_source  # noqa: E402
from update_host import HostProbe, HostUpdater  # noqa: E402


class SmokeFailure(Exception):
    pass


DATA_FINGERPRINT_SCRIPT = '''
import hashlib, json
from decimal import Decimal
from sqlalchemy import select, func
from app.db import SessionLocal
from app.models import Platform, Rule, CollectionRun, Product, Observation, utcnow
from app.security import encrypt_session, decrypt_session
from app.tasks import redis_client
with SessionLocal() as db:
    if SEED:
        if db.scalar(select(func.count()).select_from(Platform)):
            raise RuntimeError('Isolated database was not empty')
        now = utcnow()
        platform = Platform(name='Docker smoke fixture', base_url='https://source.invalid',
                            enabled=False, account_label='smoke-fixture-account', auth_status='connected',
                            encrypted_session=encrypt_session('fixture-session-no-upstream-request'))
        db.add(platform)
        db.flush()
        rule = Rule(platform_id=platform.id, version=1, status='published', config={})
        db.add(rule)
        db.flush()
        platform.active_rule_id = rule.id
        run = CollectionRun(platform_id=platform.id, rule_id=rule.id, account_label=platform.account_label,
                            scheduled_at=now, started_at=now, completed_at=now, status='success',
                            products_seen=1, expected_total=1)
        db.add(run)
        db.flush()
        product = Product(platform_id=platform.id, account_label=platform.account_label, source_id='9585',
                          name='High precision fixture', price=Decimal('0.007486'), display_quantity=Decimal('1000'),
                          unit='person', status='active', last_run_id=run.id, last_observed_at=now, last_scheduled_at=now)
        db.add(product)
        db.flush()
        db.add(Observation(product_id=product.id, run_id=run.id, rule_id=rule.id, scheduled_at=now,
                           observed_at=now, name=product.name, price=Decimal('0.007486'),
                           display_quantity=Decimal('1000'), unit='person', status='active',
                           raw={'price':'0.007486','price_show_num':'1000'}))
        db.commit()
        redis_client().set('pm:docker-smoke:retained', 'fixed-redis-fixture-no-credentials')
    platform = db.scalar(select(Platform).where(Platform.account_label=='smoke-fixture-account'))
    if not platform or platform.enabled:
        raise RuntimeError('Disabled fixture platform was not preserved')
    product = db.scalar(select(Product).where(Product.platform_id==platform.id, Product.source_id=='9585'))
    observation = db.scalar(select(Observation).where(Observation.product_id==product.id)) if product else None
    if not product or not observation or product.price!=Decimal('0.007486') or observation.price!=Decimal('0.007486'):
        raise RuntimeError('Exact price fixture was not preserved')
    session = decrypt_session(platform.encrypted_session)
    if session!='fixture-session-no-upstream-request':
        raise RuntimeError('Encrypted fixture session was not preserved')
    redis_value = redis_client().get('pm:docker-smoke:retained')
    if redis_value!='fixed-redis-fixture-no-credentials':
        raise RuntimeError('Redis fixture was not preserved')
    payload = {'platform_count':db.scalar(select(func.count()).select_from(Platform)),
               'product_count':db.scalar(select(func.count()).select_from(Product)),
               'observation_count':db.scalar(select(func.count()).select_from(Observation)),
               'rule_id':observation.rule_id, 'run_id':observation.run_id,
               'price':str(product.price), 'observed_price':str(observation.price),
               'display_quantity':str(product.display_quantity), 'raw':observation.raw,
               'encrypted_session':platform.encrypted_session, 'session':session, 'redis_fixture':redis_value}
    print(hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest())
'''


# These records are deliberately inactive and use only fixed fixture values.
# No model endpoint, source account or production configuration is copied.
AI_SEED_SCRIPT = '''
from sqlalchemy import select, func
from app.db import SessionLocal
from app.models import (AISettings, AIAnalysisRun, AIAnalysisBatch, AIDailyQuota,
    AIProductCheck, AISuggestion, AIApplication, ComparisonCategory, CanonicalProduct,
    Product, utcnow)
from app.security import encrypt_session
with SessionLocal() as db:
    if db.scalar(select(func.count()).select_from(AIAnalysisRun)):
        raise RuntimeError('AI smoke fixture database was not empty')
    product = db.scalar(select(Product).where(Product.source_id=='9585'))
    if product is None:
        raise RuntimeError('Base price fixture is required')
    now = utcnow()
    category = ComparisonCategory(name='AI smoke fixture category')
    db.add(category); db.flush()
    group = CanonicalProduct(name='AI smoke fixture group', category_id=category.id,
        unit=product.unit, currency=product.currency, match_mode='similar', mapping_revision=1)
    db.add(group); db.flush()
    db.add(AISettings(id=1, enabled=False, auto_apply=False,
        base_url='https://model.invalid/v1', model='fixture-no-network',
        encrypted_key=encrypt_session('fixed-fixture-key-no-network'),
        daily_product_limit=1000, daily_request_limit=20))
    run = AIAnalysisRun(dedupe_key='docker-smoke-completed', status='succeeded',
        stage='succeeded', trigger='manual', model='fixture-no-network',
        settings_revision=1, planned_count=1, checked_count=1,
        snapshot={'fixture_only':True,'sample_count':1},
        report={'summary':'Fixed AI smoke report','findings':[]},
        started_at=now, completed_at=now)
    db.add(run); db.flush()
    snapshot = {'id':product.id,'name':product.name,'platform_id':product.platform_id,
                'price':str(product.price),'display_quantity':str(product.display_quantity)}
    batch = AIAnalysisBatch(run_id=run.id, ordinal=1, status='succeeded',
        product_ids=[product.id], snapshot={'products':[snapshot]},
        report={'summary':'Fixed AI batch report','findings':[]},
        usage={'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},
        request_day='2026-01-01', started_at=now, completed_at=now)
    db.add(batch); db.flush()
    db.add(AIDailyQuota(day='2026-01-01', products_used=1, requests_used=1))
    db.add(AIProductCheck(product_id=product.id, fingerprint='a'*64,
                         last_checked_at=now, last_batch_id=batch.id))
    suggestion = AISuggestion(run_id=run.id, batch_id=batch.id, kind='group',
        status='applied', category_id=category.id, proposed_category_path=category.name,
        group_id=group.id, group_name=group.name, reason='Fixed fixture only',
        products=[snapshot], dedupe_key='b'*64)
    db.add(suggestion); db.flush()
    application = AIApplication(suggestion_id=suggestion.id, mode='auto', status='applied',
        group_id=group.id, group_name=group.name, category_path=category.name,
        products=[snapshot], before={'fixture_only':True,'canonical_id':None},
        after={'fixture_only':True,'canonical_id':group.id})
    db.add(application); db.flush()
    product.canonical_id, product.mapping_origin = group.id, 'ai'
    product.mapping_revision, product.ai_application_id = 1, application.id
    db.commit()
print('fixture_seeded')
'''


# Raw SQL is intentional: after rollback the old application has no AI ORM
# classes, but additive tables and their exact records must still be preserved.
AI_FINGERPRINT_SCRIPT = '''
import hashlib, json
from sqlalchemy import text
from app.db import engine
tables = {
    'ai_settings':'id', 'ai_analysis_runs':'id', 'ai_analysis_batches':'id',
    'ai_daily_quotas':'day', 'ai_product_checks':'product_id', 'ai_suggestions':'id',
    'ai_applications':'id', 'comparison_categories':'id', 'canonical_products':'id'}
with engine.connect() as conn:
    conn.execute(text('SET TRANSACTION READ ONLY'))
    payload = {}
    for table, ordering in tables.items():
        rows = conn.execute(text('SELECT row_to_json(t)::text FROM '+table+' t ORDER BY '+ordering)).scalars().all()
        if not rows:
            raise RuntimeError('An AI fixture table was empty after upgrade or rollback')
        payload[table] = [json.loads(row) for row in rows]
    payload['product_mapping'] = [dict(row) for row in conn.execute(text(
        "SELECT id, canonical_id, mapping_revision, mapping_origin, ai_application_id "
        "FROM products WHERE source_id='9585' ORDER BY id")).mappings()]
    if len(payload['product_mapping'])!=1 or payload['product_mapping'][0]['mapping_origin']!='ai':
        raise RuntimeError('AI association provenance was not preserved')
    print(hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest())
'''


class IsolatedRunner:
    def __init__(self, environment: dict[str, str]):
        self.environment = environment
        self.last_error = ''

    def __call__(self, arguments: list[str], *, cwd: Path, timeout: int = 600) -> str:
        try:
            completed = subprocess.run(arguments, cwd=cwd, env=self.environment, capture_output=True,
                                       text=True, encoding='utf-8', errors='replace', timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise SmokeFailure('Docker command unavailable or timed out') from None
        if completed.returncode:
            # Compose interpolation errors can contain values from .env. Never
            # publish raw command output, environment values or exception text.
            details = (completed.stdout + '\n' + completed.stderr)[-6000:]
            for name, value in self.environment.items():
                if value and any(word in name.upper() for word in ('PASSWORD', 'SECRET', 'TOKEN', 'ENCRYPTION_KEY', 'PROXY')):
                    details = details.replace(value, '[redacted]')
            self.last_error = details[-4000:]
            raise SmokeFailure('Docker command failed; inspect the sanitized diagnostic')
        return completed.stdout.strip()


def unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def data_fingerprint(runner, compose, root, *, seed=False) -> str:
    output = runner(compose + ['exec', '-T', 'api', 'python', '-c',
                              'SEED=' + repr(seed) + '\n' + DATA_FINGERPRINT_SCRIPT], cwd=root, timeout=60)
    if len(output) != 64 or any(character not in '0123456789abcdef' for character in output):
        raise SmokeFailure('Isolated database fingerprint response was invalid')
    return output


def ai_data_fingerprint(runner, compose, root, *, seed=False) -> str:
    if seed:
        output = runner(compose + ['exec', '-T', 'api', 'python', '-c', AI_SEED_SCRIPT], cwd=root, timeout=60)
        if output != 'fixture_seeded':
            raise SmokeFailure('AI fixture seeding response was invalid')
    output = runner(compose + ['exec', '-T', 'api', 'python', '-c', AI_FINGERPRINT_SCRIPT], cwd=root, timeout=60)
    if len(output) != 64 or any(character not in '0123456789abcdef' for character in output):
        raise SmokeFailure('AI database fingerprint response was invalid')
    return output


def copy_release_tree(source: Path, target: Path, *, require_dist=True) -> None:
    for name, path in collect_files(source, require_dist=require_dist).items():
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)


def change_fixture_version(root: Path, version: str) -> None:
    # These are disposable acceptance fixtures, not edits to the calling repo.
    atomic_write(root / 'backend/app/version.py', f'VERSION = "{version}"\n')
    for name in ('frontend/package.json', 'frontend/package-lock.json'):
        path = root / name
        value = json.loads(path.read_text(encoding='utf-8'))
        value['version'] = version
        if isinstance(value.get('packages', {}).get(''), dict):
            value['packages']['']['version'] = version
        atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def availability_problem() -> str | None:
    if sys.platform != 'linux':
        return 'Real Docker acceptance requires Linux'
    if os.getuid() == 0 or os.getgid() == 0:
        return 'Run under a non-root Docker-enabled Linux user so APP_UID/APP_GID match the host'
    if not shutil.which('docker'):
        return 'Docker CLI is unavailable'
    for command in (['docker', 'info', '--format', '{{.ServerVersion}}'], ['docker', 'compose', 'version', '--short']):
        try:
            if subprocess.run(command, capture_output=True, timeout=20).returncode:
                return 'Docker engine or Docker Compose v2 is unavailable'
        except (OSError, subprocess.TimeoutExpired):
            return 'Docker engine or Docker Compose v2 is unavailable'
    return None


def exercise(source: Path, report: dict, *, baseline_root: Path | None = None) -> None:
    source = source.resolve()
    baseline = baseline_root.resolve() if baseline_root else source
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if len(revision) != 40 or any(char not in '0123456789abcdef' for char in revision):
        raise SmokeFailure('A full source revision is required')
    baseline_revision = subprocess.check_output(['git','-C',str(baseline),'rev-parse','HEAD'],text=True).strip() if baseline_root else revision
    if len(baseline_revision)!=40 or any(char not in '0123456789abcdef' for char in baseline_revision):
        raise SmokeFailure('A full baseline revision is required')
    base_version = version_from_source(baseline)
    major, minor, patch = (int(part) for part in base_version.split('.'))
    source_version = version_from_source(source)
    candidate = f'{major}.{minor}.{patch + 1}'
    target_version = source_version if tuple(map(int,source_version.split('.'))) > (major,minor,patch) else candidate
    project = 'price-monitor-smoke-' + secrets.token_hex(8)
    root = Path(tempfile.mkdtemp(prefix=project + '-', dir=os.getenv('RUNNER_TEMP') or None)).resolve()
    workspace_parent = root.parent
    if not root.name.startswith(project + '-') or not root.is_relative_to(workspace_parent) or root == source:
        raise SmokeFailure('Invalid isolated workspace')
    port = unused_loopback_port()
    environment = os.environ.copy()
    values = {
        'COMPOSE_PROJECT_NAME': project,
        'POSTGRES_USER': 'price_monitor', 'POSTGRES_DB': 'price_monitor',
        'POSTGRES_PASSWORD': secrets.token_hex(32),
        'ADMIN_USERNAME': 'smoke-admin', 'ADMIN_PASSWORD': secrets.token_hex(24),
        'SESSION_SECRET': secrets.token_hex(48),
        'ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'WEB_BIND': '127.0.0.1', 'WEB_PORT': str(port), 'COOKIE_SECURE': 'false',
        'APP_UID': str(os.getuid()), 'APP_GID': str(os.getgid()),
        'UPDATE_GITHUB_TOKEN': '', 'UPDATE_PROXY': '',
    }
    environment.update(values)
    runner = IsolatedRunner(environment)
    compose = ['docker', 'compose', '--project-name', project, '--project-directory', str(root),
               '--env-file', str(root / '.env'), '-f', str(root / 'docker-compose.yml')]
    attempted_start = False
    report.update(project=project, source_revision=revision, initial_version=base_version, target_version=target_version,
                  fixture_only=True, baseline_mode='old_release' if baseline_root else 'current_source')
    if baseline_root:
        report['baseline_revision'] = baseline_revision
    try:
        report['phase'] = 'prepare_isolated_installation'
        copy_release_tree(baseline, root, require_dist=not bool(baseline_root))
        atomic_write(root / '.env', '\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
        initial_env_hash = hashlib.sha256((root / '.env').read_bytes()).hexdigest()
        (root / 'config/update.yaml').write_bytes((root / 'config/update.example.yaml').read_bytes())
        (root / 'config/update.yaml').chmod(0o644)
        for path in (root / 'data/update', root / 'data/backups'):
            path.mkdir(parents=True, mode=0o700)
        change_fixture_version(root, base_version)
        secret = values['SESSION_SECRET']
        probe = HostProbe(f'http://127.0.0.1:{port}/api/health', secret)
        updater = HostUpdater(root, root / 'data/update', root / 'data/backups', mode='compose',
                              secret=secret, runner=runner, probe=probe, health_timeout=300)
        report['phase'] = 'start_real_docker_services'
        attempted_start = True
        runner(compose + ['up', '-d', '--build', '--wait', '--wait-timeout', '300'], cwd=root, timeout=1800)
        run_id = 1000000000 + secrets.randbelow(1000000000)
        base_job = {'protocol': 1, 'run_id': run_id, 'action': 'apply', 'current_version': base_version,
                    'target_version': target_version, 'tag': 'v' + target_version,
                    'repository': 'acceptance-fixture/price-monitor', 'auto_restart': True}
        updater._wait_health(base_job, base_version)
        report['initial_health'] = True
        report['initial_services'] = list(updater._app_services())
        baseline_data = data_fingerprint(runner, compose, root, seed=True)
        report['baseline_data_fingerprint'] = baseline_data
        baseline_ai = None
        if 'ai-worker' in updater._app_services():
            baseline_ai = ai_data_fingerprint(runner,compose,root,seed=True)
            report['baseline_ai_data_fingerprint'] = baseline_ai

        report['phase'] = 'build_target_fixture_release'
        target = root / 'data/target-source'
        target.mkdir()
        copy_release_tree(source, target)
        change_fixture_version(target, target_version)
        package_dir = updater.data_dir / 'packages' / str(run_id)
        package, _ = build_release(target, package_dir, version=target_version, revision=revision)
        job = {**base_job, 'package': package.relative_to(updater.data_dir).as_posix(),
               'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}
        write_signed_json(updater.data_dir / 'jobs' / f'{run_id}.json', job, secret)
        report['phase'] = 'apply_real_update'
        applied = updater.run_once()
        if not applied or applied.get('status') != 'succeeded' or not updater._healthy(job, target_version):
            report['apply_status'] = (applied or {}).get('status')
            report['error_code'] = (applied or {}).get('error_code')
            raise SmokeFailure('Real upgrade did not pass target-version health checks')
        report.update(apply_status='succeeded', upgraded_health=True)
        report['upgraded_services'] = list(updater._app_services())
        if 'ai-worker' not in updater._app_services():
            raise SmokeFailure('Target release did not start the independent AI worker')
        if data_fingerprint(runner, compose, root) != baseline_data:
            raise SmokeFailure('Upgrade changed the encrypted session or exact price history fixture')
        report['upgrade_data_preserved'] = True
        if hashlib.sha256((root / '.env').read_bytes()).hexdigest() != initial_env_hash:
            raise SmokeFailure('Upgrade changed the isolated environment file')
        upgraded_ai = ai_data_fingerprint(runner,compose,root,seed=baseline_ai is None)
        if baseline_ai and upgraded_ai != baseline_ai:
            raise SmokeFailure('Upgrade changed the AI report, quota or association fixture')
        report['ai_data_fingerprint'] = upgraded_ai
        report['ai_upgrade_data_preserved'] = baseline_ai is not None
        report['ai_tables_created_by_upgrade'] = baseline_ai is None
        rollback_id = run_id + 2000000000
        rollback_job = {'protocol': 1, 'run_id': rollback_id, 'action': 'rollback',
                        'current_version': target_version, 'target_version': base_version, 'tag': 'v' + base_version,
                        'repository': job['repository'], 'backup_id': applied['backup_id'], 'auto_restart': True}
        write_signed_json(updater.data_dir / 'jobs' / f'{rollback_id}.json', rollback_job, secret)
        report['phase'] = 'apply_real_rollback'
        rolled_back = updater.run_once()
        if not rolled_back or rolled_back.get('status') != 'rolled_back' or not updater._healthy(rollback_job, base_version):
            report['rollback_status'] = (rolled_back or {}).get('status')
            report['error_code'] = (rolled_back or {}).get('error_code')
            raise SmokeFailure('Real rollback did not restore initial-version health checks')
        persisted = read_signed_json(updater.data_dir / 'results' / f'{rollback_id}.json', secret)
        if persisted.get('version') != base_version:
            raise SmokeFailure('Signed rollback result did not record the restored version')
        if data_fingerprint(runner, compose, root) != baseline_data:
            raise SmokeFailure('Rollback changed the encrypted session or exact price history fixture')
        report.update(rollback_status='rolled_back', restored_health=True, data_preserved=True,
                      rollback_data_preserved=True)
        report['restored_services'] = list(updater._app_services())
        if ai_data_fingerprint(runner,compose,root) != upgraded_ai:
            raise SmokeFailure('Rollback changed the AI report, quota or association fixture')
        report['ai_rollback_data_preserved'] = True
        if hashlib.sha256((root / '.env').read_bytes()).hexdigest() != initial_env_hash:
            raise SmokeFailure('Rollback changed the isolated environment file')
        report['environment_and_redis_preserved'] = True
        report.update(status='passed', phase='complete')
    finally:
        if report.get('status') != 'passed' and runner.last_error:
            report['diagnostic'] = runner.last_error
        report['cleanup'] = 'not_required'
        if attempted_start:
            try:
                # Explicit project name, private compose file and private env:
                # production containers/volumes can never match this command.
                runner(compose + ['down', '--volumes', '--remove-orphans', '--timeout', '90'], cwd=root, timeout=180)
                report['cleanup'] = 'passed'
            except SmokeFailure:
                report['cleanup'] = 'failed'
                report['status'] = 'failed'
                report['isolated_workspace'] = str(root)
        if report['cleanup'] != 'failed':
            # root was created exclusively above and remains under its original
            # temp parent; links inside the isolated test tree are not followed.
            if root.parent != workspace_parent or not root.name.startswith(project + '-'):
                raise SmokeFailure('Refusing cleanup outside the isolated workspace')
            shutil.rmtree(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly permit disposable real Docker containers and volumes')
    parser.add_argument('--root', type=Path, default=SCRIPT_ROOT.parent)
    parser.add_argument('--baseline-root',type=Path,help='Optional audited old checkout used for the initial installation')
    parser.add_argument('--report', type=Path, help='Write a sanitized acceptance report outside the temporary installation')
    args = parser.parse_args()
    report = {'kind': 'docker_update_acceptance', 'at': datetime.now(timezone.utc).isoformat(), 'status': 'not_executed'}
    reason = 'Use --run to explicitly execute real Docker acceptance' if not args.run else availability_problem()
    if reason:
        report['reason'] = reason
        code = 2
    else:
        try:
            exercise(args.root, report, baseline_root=args.baseline_root)
            code = 0 if report.get('status') == 'passed' else 1
        except Exception:
            report['status'] = 'failed'
            report['reason'] = 'Acceptance failed; inspect the recorded phase and sanitized error code'
            code = 1
    if args.report:
        atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
