"""A stand-in for `requests` that only ever answers from a recording.

Both Homework 4 scripts `import requests` and call requests.get(url).text, and
nothing else. Neither can reach its real target in 2026: cs.fsu.edu has been
redesigned since April 2016, and imgur's /user/<name>/index/newest/page/N/
hit.json endpoint no longer exists. So this module is put first on PYTHONPATH
in Dockerfile.web, and get() is answered from files on disk:

  * faculty_directory.py gets Wayback Machine captures of the 2016 pages,
    fetched at image build time (pyhw4/wayback.tsv pins each one).
  * imgur_battle.py gets pyhw4/imgur/, which is INVENTED data. Its only purpose
    is to make the scoring bug reproducible; see pyhw4/imgur/README.

A URL with no recording is an error, not an empty page. Silently answering ''
would let the faculty scraper print "N/A" for every field of a page that simply
failed to download, which would look exactly like the scraper's own behaviour.

Python 2, like the scripts it serves.
"""

import os
import sys

_ROOT = os.environ["FSU_RECORDED"]
_MAP = {}

with open(os.path.join(_ROOT, "index.tsv")) as f:
    for line in f:
        line = line.rstrip("\n")
        if line:
            url, name = line.split("\t")
            _MAP[url] = os.path.join(_ROOT, name)


class Response(object):
    def __init__(self, url, text):
        self.url = url
        self.text = text
        self.status_code = 200


def get(url, *args, **kwargs):
    path = _MAP.get(url)
    if path is None:
        sys.stderr.write("requests shim: no recording for %s\n" % url)
        raise SystemExit(3)
    with open(path, "rb") as f:
        body = f.read()
    # The real library decodes using the response's charset, falling back to
    # ISO-8859-1 for text/html with none. These captures are UTF-8, and
    # decoding them as such is the one liberty taken; an ASCII page reads the
    # same either way.
    return Response(url, body.decode("utf-8", "replace"))
