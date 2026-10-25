"""Create the explicitly selected first platform without writing source credentials."""
from pathlib import Path
import httpx
from dotenv import dotenv_values


def main():
    values = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
    with httpx.Client(base_url='http://127.0.0.1:28671', timeout=30, trust_env=False) as client:
        response = client.post('/api/auth/login', json={'username': values['ADMIN_USERNAME'], 'password': values['ADMIN_PASSWORD']})
        response.raise_for_status()
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        rows = client.get('/api/platforms').json()['items']
        existing = next((p for p in rows if p['base_url'] == 'https://www.xiaohong66.com'), None)
        if existing:
            print(f'Platform already configured: {existing["id"]}')
            return
        response = client.post('/api/platforms', json={'name': '亿乐海外运营学习站', 'base_url': 'https://www.xiaohong66.com',
            'adapter': 'xiaohong66', 'account_label': '主采集账号', 'enabled': True,
            'interval_seconds': 3600, 'request_interval': 1})
        response.raise_for_status()
        print(f'Platform configured: {response.json()["id"]}')


if __name__ == '__main__':
    main()
