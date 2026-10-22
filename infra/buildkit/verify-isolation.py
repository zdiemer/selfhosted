#!/usr/bin/env python3
"""Exercise the shared service through its normal authenticated buildctl client."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile

workers = json.loads(subprocess.check_output(['buildctl', 'debug', 'workers', '--format', '{{json .}}']))
assert workers, 'No workers'
for worker in workers:
    labels = worker.get('Labels', worker.get('labels', {}))
    assert labels.get('org.mobyproject.buildkit.worker.oci.process-mode') == 'sandbox', labels

DOCKERFILE = r'''FROM alpine:3.22 AS build
RUN set -eu; test "$$" -eq 1; \
    test ! -e /proc/1/root/etc/buildkit/tls/tls.key; \
    for p in /proc/[0-9]*/comm; do \
      case "$(cat "$p" 2>/dev/null || true)" in buildkitd|rootlesskit) exit 1;; esac; \
    done; \
    apk add --no-cache build-base; \
    printf 'int main(){return 0;}\n' >/tmp/test.c; cc /tmp/test.c -o /tmp/test; /tmp/test
RUN --mount=type=cache,id=isolation-probe,target=/tmp/cache echo cached >/tmp/cache/probe
RUN --mount=type=cache,id=isolation-probe,target=/tmp/cache test "$(cat /tmp/cache/probe)" = cached
RUN --mount=type=secret,id=probe test "$(cat /run/secrets/probe)" = harmless-test
RUN test ! -e /run/secrets/probe; mkdir /out; chown 65534:65534 /out
USER 65534:65534
RUN test "$(id -u)" = 65534; echo userns-ok >/out/result
FROM scratch
COPY --from=build /out/result /result
'''
with tempfile.TemporaryDirectory(prefix='buildkit-isolation-') as directory:
    root = Path(directory)
    context = root / 'context'
    context.mkdir()
    (context / 'Dockerfile').write_text(DOCKERFILE)
    (root / 'secret').write_text('harmless-test')
    command = ['buildctl', 'build', '--frontend', 'dockerfile.v0', '--local', f'context={context}',
               '--local', f'dockerfile={context}', '--secret', f'id=probe,src={root / "secret"}',
               '--output', f'type=local,dest={root / "result"}', '--progress', 'plain']
    subprocess.run(command + ['--no-cache'], check=True, timeout=300)
    assert (root / 'result' / 'result').read_text() == 'userns-ok\n'
    repeat = subprocess.run(command, check=True, timeout=300, capture_output=True, text=True)
    assert 'CACHED' in repeat.stderr, 'Repeat build did not reuse cache'
# Two overlapping RUNs must have separate PID namespaces and cannot read the
# other RUN's environment via procfs. Only harmless synthetic markers are used.
def peer_build(marker):
    other = 'B' if marker == 'A' else 'A'
    with tempfile.TemporaryDirectory(prefix='buildkit-peer-') as directory:
        root = Path(directory)
        (root / 'Dockerfile').write_text(f"""FROM alpine:3.22 AS test
ENV ISOLATION_PROBE={marker}
RUN set -eu; test \"$$\" -eq 1; sleep 10; for p in /proc/[0-9]*/environ; do if tr '\\0' '\\n' < \"$p\" 2>/dev/null | grep -qx 'ISOLATION_PROBE={other}'; then exit 1; fi; done; mkdir /out; readlink /proc/self/ns/pid >/out/pidns
FROM scratch
COPY --from=test /out/pidns /pidns
""")
        subprocess.run(['buildctl', 'build', '--frontend', 'dockerfile.v0', '--no-cache',
                        '--local', f'context={root}', '--local', f'dockerfile={root}',
                        '--output', f'type=local,dest={root / "out"}', '--progress', 'plain'],
                       check=True, timeout=120, capture_output=True)
        return (root / 'out' / 'pidns').read_text().strip()

with ThreadPoolExecutor(max_workers=2) as pool:
    namespaces = list(pool.map(peer_build, ['A', 'B']))
assert len(set(namespaces)) == 2, namespaces
print('PASS: worker sandbox, PID 1, daemon/sibling hidden, compilation, UID 65534, secret/cache mounts, export and cache reuse')
