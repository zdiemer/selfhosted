import concurrent.futures
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from ipc import call, chat_chunks

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('terraria-claude')
ROOT = Path(os.environ.get('BRIDGE_DIR', '/bridge'))
STATE = Path(os.environ.get('HOME', '/state'))
LOCK = threading.Lock()
SYSTEM = Path('/app/system.txt')

def run_claude(request, session=None):
    args = ['claude', '-p', '--output-format', 'json', '--model', os.environ.get('CLAUDE_MODEL', 'sonnet'),
            '--settings', '/app/settings.json', '--setting-sources', '',
            '--tools', 'WebSearch,WebFetch', '--allowedTools',
            'WebSearch,WebFetch,mcp__terraria__get_context,mcp__terraria__find_recipes,'
            'mcp__terraria__teleport_to_player,mcp__terraria__teleport_home,mcp__terraria__teleport_back',
            '--strict-mcp-config', '--mcp-config', '/app/mcp.json',
            '--append-system-prompt', SYSTEM.read_text()]
    if session:
        args += ['--resume', session]
    env = dict(os.environ, TURN_ID=request['id'])
    # Pass player text via stdin, never through a shell or command arguments.
    prompt = json.dumps({'caller': request['player'], 'request': request['prompt'], 'context': request['context']})
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(prompt, timeout=300)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise TimeoutError('Claude took too long. Please try a shorter question.')
    if process.returncode:
        log.error('Claude exited %s: %s', process.returncode, stderr[-400:])
        raise RuntimeError('Claude is unavailable right now. Try again shortly.')
    result = json.loads(stdout)
    if result.get('is_error'):
        raise RuntimeError('Claude could not finish. Try /claude clear, then ask again.')
    return result

def handle(request):
    turn = request['id']
    # Names never become paths. Terraria has character names, not Minecraft UUIDs.
    key = hashlib.sha256(request['player'].encode()).hexdigest()
    session_file = STATE / 'sessions' / (key + '.json')
    def send(text):
        for chunk in chat_chunks(text):
            call('reply', {'text': chunk}, turn=turn)
    try:
        if request['prompt'].strip().lower() in ('clear', 'reset'):
            session_file.unlink(missing_ok=True)
            send('Conversation cleared.'); return
        with LOCK:
            session = json.loads(session_file.read_text()).get('session_id') if session_file.exists() else None
        result = run_claude(request, session)
        send(result.get('result') or 'I could not produce a reply. Please try again.')
        if result.get('session_id'):
            with LOCK:
                temporary = session_file.with_suffix('.tmp')
                temporary.write_text(json.dumps({'session_id': result['session_id']}))
                temporary.replace(session_file)
    except Exception as e:
        log.error('Turn failed (%s)', type(e).__name__)
        try:
            send(str(e) if isinstance(e, (RuntimeError, TimeoutError)) else 'Something went wrong. Please try again.')
        except Exception:
            log.info('Player disconnected or game unavailable; reply dropped.')
    finally:
        try:
            call('done', turn=turn)
        except Exception:
            pass

def main():
    (STATE / 'sessions').mkdir(parents=True, exist_ok=True)
    config = STATE / '.claude'
    config.mkdir(exist_ok=True)
    credentials = config / '.credentials.json'
    if not credentials.is_symlink():
        credentials.symlink_to('/host-claude/.credentials.json')
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
        pending = set()
        while True:
            pending = {f for f in pending if not f.done()}
            if len(pending) < 3:
                for path in sorted((ROOT / 'requests').glob('*.json'))[:3 - len(pending)]:
                    try:
                        request = json.loads(path.read_text())
                        path.unlink()
                        pending.add(workers.submit(handle, request))
                    except (FileNotFoundError, json.JSONDecodeError):
                        pass
            time.sleep(.2)

if __name__ == '__main__':
    main()
