"""Build replacement must not serve stale entry HTML or HTML as a missing JS file."""
from test_pipeline import isolated_database,clean_test_resources,db,admin_client


def test_spa_entry_requires_revalidation_and_missing_chunks_return404(admin_client,tmp_path,monkeypatch):
    from app import main
    (tmp_path/'index.html').write_text('<html>new-build-fixture</html>',encoding='utf-8')
    (tmp_path/'assets').mkdir()
    (tmp_path/'assets'/'fixture.js').write_text('export default 1',encoding='utf-8')
    monkeypatch.setattr(main,'FRONTEND',tmp_path)
    for path in ('/admin/ai-analysis','/admin/settings/network','/index.html'):
        result=admin_client.get(path)
        assert result.status_code==200 and result.headers['Cache-Control']=='no-cache'
        assert 'new-build-fixture' in result.text
    missing=admin_client.get('/assets/old-deleted-build.js')
    assert missing.status_code==404 and 'new-build-fixture' not in missing.text
    present=admin_client.get('/assets/fixture.js')
    assert present.status_code==200 and 'javascript' in present.headers['content-type']
