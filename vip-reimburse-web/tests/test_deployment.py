"""Exercise deployment health and proxy rate limits with synthetic data only."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import test_backend as fixtures
from server import App


class DeploymentTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BackendTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BackendTests.tearDownClass.__func__)
    client = fixtures.BackendTests.client
    req = fixtures.BackendTests.req

    def setUp(self):
        self.previous_trust = self.server.app.trust_proxy_headers
        self.server.app.trust_proxy_headers = False
        with self.server.app.lock:self.server.app.rates.clear()

    def tearDown(self):
        self.server.app.trust_proxy_headers = self.previous_trust

    def lookup(self, address=None):
        headers = {'X-Real-IP': address} if address is not None else {}
        return self.req(self.client(), '/api/guest/lookup', {'mobile':'13800000000'}, headers=headers)[0]

    def test_health_get_head_and_database_failure(self):
        self.assertEqual(self.req(self.client(), '/healthz'), (200, {'ok':True}))
        request = urllib.request.Request(self.base+'/healthz', method='HEAD')
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'')
            self.assertEqual(int(response.headers['Content-Length']), len(json.dumps({'ok':True}).encode()))
        with patch.object(self.server.app, 'db', side_effect=RuntimeError('private path')):
            status, body = self.req(self.client(), '/healthz')
        self.assertEqual(status, 500)
        self.assertNotIn('private path', json.dumps(body))

    def test_direct_connections_ignore_forged_proxy_headers(self):
        for index in range(8):self.assertEqual(self.lookup(f'192.0.2.{index+1}'), 200)
        self.assertEqual(self.lookup('192.0.2.100'), 429)

    def test_proxy_clients_have_independent_limits(self):
        self.server.app.trust_proxy_headers = True
        for _ in range(8):self.assertEqual(self.lookup('192.0.2.1'), 200)
        self.assertEqual(self.lookup('192.0.2.1'), 429)
        self.assertEqual(self.lookup('192.0.2.2'), 200)
        # Equivalent IPv6 representations must share one bucket.
        for _ in range(8):self.assertEqual(self.lookup('2001:db8::1'), 200)
        self.assertEqual(self.lookup('2001:0db8:0:0:0:0:0:1'), 429)

    def test_invalid_proxy_headers_fall_back_to_peer(self):
        self.server.app.trust_proxy_headers = True
        for index in range(8):self.assertEqual(self.lookup(f'bad-{index}'), 200)
        self.assertEqual(self.lookup('192.0.2.1, 192.0.2.2'), 429)
        self.assertEqual(self.lookup(), 429)

    def test_proxy_trust_is_explicitly_enabled(self):
        for value, expected in [(None,False), ('0',False), ('1',True)]:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
                os.environ.pop('TRUST_PROXY_HEADERS', None)
                if value is not None:os.environ['TRUST_PROXY_HEADERS'] = value
                self.assertEqual(App(directory).trust_proxy_headers, expected)
