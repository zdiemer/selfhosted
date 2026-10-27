"""Request-scoped communication with the authoritative game thread."""
import json
import os
from pathlib import Path
import re
import time
import uuid

def call(op, args=None, *, turn=None, root=None, timeout=10):
    turn = turn or os.environ['TURN_ID']
    if not re.fullmatch(r'[0-9a-f]{32}', turn):
        raise ValueError('Invalid turn capability')
    root = Path(root or os.environ.get('BRIDGE_DIR', '/bridge'))
    task = uuid.uuid4().hex
    action = root / 'actions' / (task + '.json')
    result = root / 'results' / (task + '.json')
    temporary = action.with_suffix('.tmp')
    deadline = time.time() + timeout
    temporary.write_text(json.dumps(dict(turn=turn, op=op, args=args or {}, deadline=deadline)))
    temporary.replace(action)
    try:
        while time.time() < deadline:
            if result.exists():
                response = json.loads(result.read_text())
                if not response['ok']:
                    raise RuntimeError(response['error'])
                return response['value']
            time.sleep(.05)
        raise TimeoutError('The game did not respond in time.')
    finally:
        action.unlink(missing_ok=True)
        result.unlink(missing_ok=True)

def chat_chunks(text, width=240, limit=4):
    # Strip chat markup and terminal/control characters, then bound chat volume.
    text = ' '.join(''.join(c for c in text if c.isprintable() and c not in '[]').split())
    chunks = []
    while text and len(chunks) < limit:
        if len(text) <= width:
            chunks.append(text); break
        cut = text.rfind(' ', 0, width + 1)
        if cut < width // 2:
            cut = width
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
        if len(chunks) == limit:
            chunks[-1] = chunks[-1][:-1] + '…'
    return chunks
