"""Keep browser work from exhausting FlareSolverr's HTTP worker pool."""
import json
import os
import runpy
import sys
import threading


class BrowserAdmission:
    def __init__(self, app, capacity):
        self.app = app
        self.slots = threading.BoundedSemaphore(capacity)

    def __call__(self, environ, start_response):
        if environ.get('PATH_INFO') != '/v1':
            yield from self.app(environ, start_response)
            return
        if not self.slots.acquire(blocking=False):
            body = json.dumps({'status': 'error', 'message':
                'FlareSolverr is busy; retry after 5 seconds.'}).encode()
            start_response('503 Service Unavailable', [
                ('Content-Type', 'application/json'), ('Retry-After', '5'),
                ('Content-Length', str(len(body))),
            ])
            yield body
            return
        # Acquire when iteration starts, so a response abandoned before its
        # first byte cannot leak capacity. Release on errors and disconnects.
        result = None
        try:
            result = self.app(environ, start_response)
            yield from result
        finally:
            try:
                if result is not None and hasattr(result, 'close'):
                    result.close()
            finally:
                self.slots.release()


def main():
    import waitress
    capacity = int(os.environ.get('MAX_BROWSER_REQUESTS', '4'))
    if capacity < 1:
        raise ValueError('MAX_BROWSER_REQUESTS must be positive')
    serve = waitress.serve

    def bounded_serve(app, **kwargs):
        kwargs['threads'] = capacity + 4
        return serve(BrowserAdmission(app, capacity), **kwargs)

    waitress.serve = bounded_serve
    sys.path.insert(0, '/app')
    runpy.run_path('/app/flaresolverr.py', run_name='__main__')


if __name__ == '__main__':
    main()
