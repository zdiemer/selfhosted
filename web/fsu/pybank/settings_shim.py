# -*- coding: utf-8 -*-
# Python 2 assumes ASCII source without this line, and the prose below
# uses em dashes. PEP 263, and a reminder of which interpreter this is.
# Settings overrides for running the 2016 pybank unmodified.
#
# The repository is frozen. Everything here is an OVERRIDE applied on top of
# pybank.settings, never an edit to it. This module lives in /opt/pybank-shim,
# a SIBLING of the project rather than inside it, so `from pybank.settings
# import *` below resolves to the original module and can never accidentally
# import itself.

from pybank.settings import *  # noqa: F401,F403
import os

# Django 1.9 with DEBUG=True and ALLOWED_HOSTS=[] permits only localhost,
# 127.0.0.1 and [::1]. Behind an Ingress every request arrives with a real Host
# header and Django answers 400. '*' rather than the real hostname because the
# hostname lives in values.yaml and duplicating it here would be a second place
# to forget it; the host is tailnet-only and Traefik is the only thing that can
# reach this port at all (see templates/networkpolicy.yaml).
ALLOWED_HOSTS = ['*']

# DEBUG stays True, deliberately and not by omission:
#
#   * it is what the original ran as, and the SECRET_KEY has been sitting in a
#     public GitHub repo since 2016, so there is no secret left to protect;
#   * runserver's StaticFilesHandler only serves /static/ when DEBUG is on, and
#     bolting a staticfiles pipeline onto a 2016 project is not the exhibit;
#   * the yellow Django error page IS part of the exhibit — five bare
#     `except: pass` blocks in views.py mean most failures never reach it, and
#     the ones that do are worth seeing.
#
# What makes that a considered choice rather than a careless one: the host
# resolves only on the tailnet, the container has a read-only root filesystem
# and a NetworkPolicy with `egress: []`, and the entire database is fake money
# recreated on every pod start.
DEBUG = True

# ---------------------------------------------------------------------------
# THE /pybank PREFIX, AND WHY IT IS DONE THIS WAY
#
# The site is one hostname, and pybank has to live under /pybank. The obvious
# way to do that is a Traefik StripPrefix middleware plus FORCE_SCRIPT_NAME.
# That does not work, and the failure is silent rather than loud, so here is the
# reasoning written down before someone "fixes" it back:
#
#   * FORCE_SCRIPT_NAME affects reverse() only. URL resolution goes through
#     resolver.resolve(request.path_info), and path_info is the raw WSGI
#     PATH_INFO — so without stripping, /pybank/main/ never matches ^main/.
#
#   * Add StripPrefix and the static files break unfixably. runserver wraps the
#     app in StaticFilesHandler, whose __call__ gates on
#     get_path_info(environ) — the RAW PATH_INFO, not request.path. With
#     STATIC_URL='/static/' the browser requests a path outside /pybank and
#     nginx 404s it; with STATIC_URL='/pybank/static/' StripPrefix has already
#     rewritten the inbound path to /static/... before _should_handle runs, so
#     Django 404s it. No value of STATIC_URL satisfies both.
#
#   * X-Forwarded-Prefix does not help: Django has no code path that reads it.
#     Mapping it onto SCRIPT_NAME is a uWSGI/gunicorn feature and runserver
#     cannot do it.
#
# So: NO StripPrefix anywhere and NO FORCE_SCRIPT_NAME. The prefix lives in the
# URLconf, which is the thing reverse() actually walks, and browser, Traefik,
# Django and StaticFilesHandler all see the same /pybank/... path.
ROOT_URLCONF = 'urls_shim'

# {% static %} is a plain urljoin against this value with no script prefix
# applied, so it has to be written out in full. It is also what
# StaticFilesHandler compares the inbound path against, which is the same string
# for the same reason.
STATIC_URL = '/pybank/static/'

# BASE_DIR is inside the image, which is read-only. The database is created
# fresh into an emptyDir on every start — there is nothing worth persisting in a
# demo bank, and one that resets to a known state is easier to reason about than
# one that accumulates whatever visitors did to it.
DATABASES['default']['NAME'] = os.path.join(  # noqa: F405
    os.environ.get('PYBANK_DB_DIR', '/data'), 'db.sqlite3')

# Deliberately NOT setting SECURE_PROXY_SSL_HEADER. Setting it makes
# request.is_secure() true, which switches CsrfViewMiddleware into its strict
# HTTPS Referer check — an extra failure mode behind a proxy, for no benefit
# here: every form in these templates posts to action="" (the same URL), so
# nothing depends on Django knowing the scheme.
