from fastapi.testclient import TestClient
from app.main import app


def test_health_and_login_and_csrf_protected_logout():
    client = TestClient(app)
    assert client.get('/healthz').status_code == 200
    bad = client.post('/api/auth/login', json={'username':'admin','password':'wrong'})
    assert bad.status_code == 401
    # The default credential is for a local-only development install.
    good = client.post('/api/auth/login', json={'username':'admin','password':'change-this-local-password'})
    assert good.status_code == 200
    me = client.get('/api/auth/me')
    assert me.status_code == 200
    csrf = good.json()['csrf_token']
    denied = client.post('/api/auth/logout')
    assert denied.status_code == 403
    allowed = client.post('/api/auth/logout', headers={'X-CSRF-Token': csrf})
    assert allowed.status_code == 200


def test_backtest_accepts_all_universe_symbol():
    client = TestClient(app)
    login = client.post('/api/auth/login', json={
        'username': 'admin', 'password': 'change-this-local-password'
    })
    assert login.status_code == 200
    csrf = login.json()['csrf_token']
    response = client.post('/api/backtests', json={'symbol': 'ALL', 'days': 30},
                           headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200
    assert response.json()['type'] == 'backtest'
    assert response.json()['status'] == 'queued'
