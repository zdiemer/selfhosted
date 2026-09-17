# -*- coding: utf-8 -*-
# Python 2 assumes ASCII source without this line, and the prose below
# uses em dashes. PEP 263, and a reminder of which interpreter this is.
# The outer URLconf: mounts the whole 2016 project under /pybank.
#
# See settings_shim.py for why the prefix lives here rather than in a Traefik
# StripPrefix plus FORCE_SCRIPT_NAME. In short: putting it in the URLconf is the
# only arrangement where request resolution, reverse() and runserver's
# StaticFilesHandler all agree on what the path is.

from django.conf.urls import include, url
from django.contrib.auth.views import logout
from django.views.generic import RedirectView

urlpatterns = [
    # SHADOWS main:logout, and must stay ABOVE the include to do it.
    #
    # main/urls.py hardcodes kwargs={'next_page': '/main'} — an absolute path
    # that, under the /pybank mount, lands on the static site's catch-all and
    # 404s. Registering the same path first means an incoming logout matches
    # this pattern and this next_page.
    #
    # reverse() is unaffected: the templates say {% url 'main:logout' %}, which
    # resolves inside the 'main' namespace to exactly this URL anyway. So the
    # link the page renders and the route that serves it agree, and neither
    # required touching main/urls.py.
    url(r'^pybank/main/logout/$', logout, {'next_page': '/pybank/main/'},
        name='logout-shim'),

    # pybank/urls.py mounts main at ^main/ and admin at ^admin/ and defines
    # nothing for ^$, so the project root has always been a 404 — in 2016 too.
    # A redirect is a kindness to anyone who trims the URL, and it is added
    # here rather than in the project's own URLconf.
    url(r'^pybank/$', RedirectView.as_view(url='/pybank/main/', permanent=False)),

    url(r'^pybank/', include('pybank.urls')),
]
