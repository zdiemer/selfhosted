
class SteamAutheliaAuth:
    """Require a verified Authelia session even when ingress is bypassed."""
    def __init__(self, src=None):
        import os
        self.url = os.environ['DESKTOP_AUTH_URL']
        self.origin = os.environ['DESKTOP_AUTH_ORIGIN']

    def authenticate(self, headers, target_host, target_port):
        import urllib.request
        import urllib.error
        cookie = headers.get('Cookie', '')
        if not cookie:
            raise AuthenticationError(response_code=401)
        request = urllib.request.Request(self.url, headers={
            'Cookie': cookie,
            'X-Forwarded-Method': 'GET',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': self.origin,
            'X-Forwarded-Uri': '/',
        })
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200 and response.headers.get('Remote-User'):
                    return
        except (OSError, urllib.error.URLError):
            pass
        raise AuthenticationError(response_code=401)
