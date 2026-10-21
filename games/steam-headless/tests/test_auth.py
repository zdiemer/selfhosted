import os
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

class Denied(Exception):
    def __init__(self, **kwargs): pass

scope = {'AuthenticationError': Denied}
exec(Path(__file__).parents[1].joinpath('files/steam-auth.py').read_text(), scope)

class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, DESKTOP_AUTH_URL='http://auth/verify', DESKTOP_AUTH_ORIGIN='steam.example'):
            self.auth = scope['SteamAutheliaAuth']()

    def test_no_cookie_denied(self):
        with self.assertRaises(Denied): self.auth.authenticate({'Remote-User': 'admin'}, None, None)

    def test_only_authenticated_identity_accepted(self):
        for identity in ['', 'verified-user']:
            response = MagicMock(status=200, headers={'Remote-User': identity})
            response.__enter__.return_value = response
            with patch('urllib.request.urlopen', return_value=response) as call:
                if identity:
                    self.auth.authenticate({'Cookie': 'session=test', 'X-Forwarded-Host': 'attacker'}, None, None)
                    self.assertEqual(call.call_args.args[0].get_header('X-forwarded-host'), 'steam.example')
                else:
                    with self.assertRaises(Denied): self.auth.authenticate({'Cookie': 'session=test'}, None, None)

    def test_auth_outage_fails_closed(self):
        with patch('urllib.request.urlopen', side_effect=OSError), self.assertRaises(Denied):
            self.auth.authenticate({'Cookie': 'session=test'}, None, None)

if __name__ == '__main__': unittest.main()
