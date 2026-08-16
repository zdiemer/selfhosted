# syntax=docker/dockerfile:1
#
# luke.diemer.codes — a static page with no build step. There is nothing to
# compile: site/ is hand-written HTML, one stylesheet, one script, a folder of
# generated GIFs and a WAV. So this is a single stage that copies the files into
# the same nginx-unprivileged base the rest of web/ uses.

FROM nginxinc/nginx-unprivileged:1.29-alpine

# Replaces the stock default.conf. The base image's /docker-entrypoint.d scripts
# test writability before touching this file, so a read-only rootfs is fine.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Left root-owned on purpose: nginx runs as 101 and only ever reads.
COPY site/ /usr/share/nginx/html/

# Fail here rather than serving a half-built page. The failure this guards is
# silent at runtime — a missing stylesheet or GIF returns 404 and the page still
# renders 200, just wrong.
RUN set -eux; \
    cd /usr/share/nginx/html; \
    test -s index.html; \
    test -s geo.css; \
    test -s geo.js; \
    test -s hbd.wav; \
    test -s img/og.jpg; \
    grep -q 'HAPPY 30th BIRTHDAY LUKE' index.html; \
    grep -q 'og:image' index.html; \
    for f in $(grep -oE '(src|href)="[^"]+"' index.html \
               | sed 's/.*="//;s/"$//' \
               | grep -v '^http' | grep -v '^#' | sort -u); do \
      test -s "$f" || { echo "missing asset: $f"; exit 1; }; \
    done

# The base image already sets USER 101 and CMD ["nginx", "-g", "daemon off;"].
EXPOSE 8080
