"""Pure official-release gates, fixture archives and first-install configuration."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from init_env import prepare_update_config
from package_release import build_release
from validate_release import strict_tag, validate_project, verify_assets, render_notes, changelog_entry, revision_for

REVISION = 'a' * 40


def fixture(root: Path, version='1.2.0'):
    values = {
        'backend/app/version.py': f'VERSION = "{version}"\n',
        'backend/app/main.py': '# fixture\n',
        'backend/requirements.txt': 'httpx==0.28.1\n',
        'frontend/package.json': json.dumps({'name': 'fixture', 'version': version}),
        'frontend/package-lock.json': json.dumps({'version': version, 'lockfileVersion': 3, 'packages': {'': {'version': version}}}),
        'frontend/dist/index.html': '<h1>fixture</h1>',
        'docker-compose.yml': 'services: {}\n',
        'scripts/update_host.py': '# fixture\n',
        'config/update.example.yaml': 'update:\n  enabled: true\n',
        'CHANGELOG.md': f'# 更新记录\n\n## [{version}] - 2026-10-26\n\n' + '\n\n'.join(f'### {title}\n\n- fixture' for title in ('概要', '新增', '修复', '升级说明', '回滚说明')),
    }
    for name, content in values.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def metadata(self, **kwargs):
        with patch('validate_release.subprocess.check_output', return_value=REVISION + '\n'), patch('validate_release.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            return validate_project(self.root, **kwargs)

    def test_release_revision_rejects_dirty_application_checkout(self):
        def output(command, **kwargs):
            return REVISION + '\n' if command[-2:] == ['rev-parse', 'HEAD'] else ''
        with patch('validate_release.subprocess.check_output', side_effect=output), patch('validate_release.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError, 'dirty'):
                revision_for(self.root)
        with patch('validate_release.subprocess.check_output', side_effect=[REVISION + '\n', 'backend/app/untracked.py\n']), patch('validate_release.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(ValueError, 'dirty'):
                revision_for(self.root)

    def test_strict_tags_reject_loose_glob_matches(self):
        self.assertEqual(strict_tag('v1.2.0'), '1.2.0')
        for tag in ('1.2.0', 'v01.2.0', 'v1.2.0-rc.1', 'v1.2.0+build', 'v1.2.0.1', 'v1.2', 'v1x2x0', 'V1.2.0', 'v1.2.0\n'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                strict_tag(tag)

    def test_versions_tag_revision_and_changelog_are_bound(self):
        metadata = self.metadata(tag='v1.2.0', revision=REVISION)
        self.assertEqual(metadata['version'], '1.2.0')
        self.assertEqual(metadata['revision'], REVISION)
        with self.assertRaises(ValueError):
            self.metadata(tag='v1.2.1')
        with self.assertRaises(ValueError):
            self.metadata(revision='b' * 40)
        for name in ('frontend/package.json', 'frontend/package-lock.json'):
            original = (self.root / name).read_text()
            broken = json.loads(original)
            broken['version'] = '1.1.0'
            (self.root / name).write_text(json.dumps(broken))
            with self.subTest(file=name), self.assertRaises(ValueError):
                self.metadata()
            (self.root / name).write_text(original)
        lock = self.root / 'frontend/package-lock.json'
        broken = json.loads(lock.read_text())
        broken['packages']['']['version'] = '1.1.0'
        lock.write_text(json.dumps(broken))
        with self.assertRaises(ValueError):
            self.metadata()

    def test_changelog_missing_empty_duplicate_and_future_dates_rejected(self):
        path = self.root / 'CHANGELOG.md'
        original = path.read_text()
        cases = [original.replace('### 修复', '### 其他'), original.replace('- fixture', '', 1),
                 original + '\n## [1.2.0] - 2026-10-26\n',
                 original.replace('2026-10-26', (date.today() + timedelta(days=1)).isoformat())]
        for content in cases:
            path.write_text(content, encoding='utf-8')
            with self.subTest(content=content), self.assertRaises(ValueError):
                changelog_entry(self.root, '1.2.0')
        path.unlink()
        with self.assertRaises(ValueError):
            changelog_entry(self.root, '1.2.0')

    def build(self):
        metadata = self.metadata()
        package, checksum = build_release(self.root, self.root / 'dist/releases', revision=REVISION)
        return metadata, package, checksum

    def test_verified_artifacts_generate_complete_chinese_notes(self):
        metadata, package, checksum = self.build()
        result = verify_assets(package.parent, metadata)
        self.assertEqual(result['sha256'], hashlib.sha256(package.read_bytes()).hexdigest())
        notes = render_notes(metadata, result)
        for text in ('概要', '新增', '修复', '升级说明', '回滚说明', 'SHA256', REVISION, package.name):
            self.assertIn(text, notes)

    def test_private_config_name_case_does_not_bypass_package_allowlist(self):
        private = self.root / 'backend/app/.ENV.production'
        private.write_text('PRIVATE_FIXTURE_SECRET')
        _, package, _ = self.build()
        with tarfile.open(package, 'r:gz') as archive:
            self.assertNotIn('backend/app/.ENV.production', archive.getnames())

    def test_checksum_filename_and_unexpected_artifacts_rejected(self):
        metadata, package, checksum = self.build()
        original = checksum.read_text()
        checksum.write_text(original.replace(package.name, 'other.tar.gz'))
        with self.assertRaises(ValueError):
            verify_assets(package.parent, metadata)
        checksum.write_text(original)
        (package.parent / 'private.json').write_text('fixture')
        with self.assertRaises(ValueError):
            verify_assets(package.parent, metadata)

    def test_revision_and_file_tampering_rejected_even_after_outer_rehash(self):
        metadata, package, checksum = self.build()
        with tarfile.open(package, 'r:gz') as archive:
            contents = {member.name: archive.extractfile(member).read() for member in archive}
        original = dict(contents)
        for change in ('revision', 'file'):
            contents = dict(original)
            if change == 'revision':
                manifest = json.loads(contents['release-manifest.json'])
                manifest['revision'] = 'b' * 40
                contents['release-manifest.json'] = json.dumps(manifest).encode()
            else:
                contents['frontend/dist/index.html'] = b'tampered'
            with tarfile.open(package, 'w:gz') as archive:
                for name, payload in contents.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
            checksum.write_text(f'{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n')
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_assets(package.parent, metadata)

    def test_release_initialization_creates_yaml_and_preserves_existing_settings(self):
        metadata, package, _ = self.build()
        extracted = self.root / 'install'
        extracted.mkdir()
        # This is our own fixture archive; every path was allowlisted above.
        with tarfile.open(package, 'r:gz') as archive:
            archive.extractall(extracted, filter='data')
        self.assertFalse((extracted / 'config/update.yaml').exists())
        (extracted / '.env').write_text('PRIVATE_FIXTURE_SECRET')
        self.assertTrue(prepare_update_config(extracted))
        target = extracted / 'config/update.yaml'
        self.assertEqual(target.read_bytes(), (extracted / 'config/update.example.yaml').read_bytes())
        target.write_text('update:\n  enabled: false\n')
        self.assertFalse(prepare_update_config(extracted))
        self.assertEqual(target.read_text(), 'update:\n  enabled: false\n')
        self.assertEqual((extracted / '.env').read_text(), 'PRIVATE_FIXTURE_SECRET')

    def test_config_only_cli_keeps_existing_secrets(self):
        (self.root / '.env').write_text('PRIVATE_FIXTURE_SECRET')
        result = subprocess.run([sys.executable, str(SCRIPTS / 'init_env.py'), '--output', str(self.root / '.env'), '--prepare-config-only'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / '.env').read_text(), 'PRIVATE_FIXTURE_SECRET')
        self.assertNotIn('PRIVATE_FIXTURE_SECRET', result.stdout)


if __name__ == '__main__':
    unittest.main()
