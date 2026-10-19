#!/usr/bin/env python3
"""Reconcile arr executable rejection and qBittorrent exclusions via their APIs."""
import argparse
import json
import os
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

EXTENSIONS = 'exe com scr dll msi msix msp bat cmd ps1 ps1xml psm1 vbs vbe js jse wsf wsh hta lnk url sh apk app dmg'.split()
NAMESPACE = os.environ.get('NAMESPACE', 'media')
RELEASE = os.environ.get('RELEASE', 'arr')
PORTS = {'sonarr': 8989, 'radarr': 7878, 'prowlarr': 9696, 'qbittorrent': 8080}


class API:
    def __init__(self, app):
        self.app = app
        self.command = ['kubectl', '-n', NAMESPACE, 'exec', '-i', f'deploy/{RELEASE}-{app}']
        if app == 'qbittorrent':
            self.command += ['-c', 'qbittorrent']
        self.command += ['--']
        self.key = None
        if app != 'qbittorrent':
            xml = subprocess.check_output(self.command + ['cat', '/config/config.xml'])
            self.key = ET.fromstring(xml).findtext('ApiKey')
            if not self.key:
                raise RuntimeError(f'{app}: missing API key')

    def call(self, path, method='GET', body=None, form=False):
        # Credentials and request bodies travel through stdin, never process argv.
        cfg = {'url': f'http://127.0.0.1:{PORTS[self.app]}{path}', 'request': method}
        lines = [f'{k} = {json.dumps(v)}' for k, v in cfg.items()]
        if self.key:
            lines.append('header = ' + json.dumps('X-Api-Key: ' + self.key))
        if body is not None:
            content = urllib.parse.urlencode(body) if form else json.dumps(body)
            lines += ['header = ' + json.dumps('Content-Type: ' + ('application/x-www-form-urlencoded' if form else 'application/json')), 'data = ' + json.dumps(content)]
        result = subprocess.check_output(self.command + ['curl', '-fsS', '--max-time', '60', '--config', '-'], input=('\n'.join(lines)+'\n').encode())
        return json.loads(result) if result and result.strip() != b'Ok.' else None


def desired_fields(obj, prowlarr=False):
    changed = False
    for field in obj['fields']:
        old = field.get('value')
        new = old
        if field['name'] == 'failDownloads':
            new = sorted(set(old or []) | {0, 1})
        elif field['name'] == ('syncRejectBlocklistedTorrentHashesWhileGrabbing' if prowlarr else 'rejectBlocklistedTorrentHashesWhileGrabbing'):
            new = True
        if new != old:
            field['value'] = new
            changed = True
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply changes (default: report drift)')
    args = parser.parse_args()
    drift = 0
    for app in ['prowlarr', 'sonarr', 'radarr']:
        api = API(app)
        path = '/api/v1/applications' if app == 'prowlarr' else '/api/v3/indexer'
        objects = api.call(path)
        changed = 0
        for obj in objects:
            if app == 'prowlarr' and obj.get('implementation') not in ['Sonarr', 'Radarr']:
                continue
            if desired_fields(obj, app == 'prowlarr'):
                changed += 1
                if args.apply:
                    api.call(path+'/'+str(obj['id'])+'?forceSave=true', 'PUT', obj)
        print(f'{app}: {changed}/{len(objects)} settings {"updated" if args.apply else "need updating"}', flush=True)
        drift += changed
        if args.apply:
            assert not any(desired_fields(o, app == 'prowlarr') for o in api.call(path) if app != 'prowlarr' or o.get('implementation') in ['Sonarr', 'Radarr']), f'{app}: verification failed'
    api = API('qbittorrent')
    prefs = api.call('/api/v2/app/preferences')
    existing = prefs.get('excluded_file_names', '').splitlines()
    merged = list(dict.fromkeys(existing + ['*.' + ext for ext in EXTENSIONS]))
    patch = {'excluded_file_names_enabled': True, 'excluded_file_names': '\n'.join(merged)}
    changed = any(prefs.get(k) != v for k, v in patch.items())
    if changed and args.apply:
        api.call('/api/v2/app/setPreferences', 'POST', {'json': json.dumps(patch)}, form=True)
        prefs = api.call('/api/v2/app/preferences')
        assert all(prefs.get(k) == v for k, v in patch.items()), 'qBittorrent: verification failed'
    print(f'qBittorrent: {len(EXTENSIONS)} executable/script/shortcut patterns; ' + ('updated' if changed and args.apply else 'drift' if changed else 'verified'))
    if not args.apply and (drift or changed):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
