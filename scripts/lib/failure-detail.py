"""Keep actionable failure lines instead of the last systemd boilerplate."""
import re
import sys


def summarize(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    causes = [line for line in lines if re.search(
        r'UNRESOLVABLE|EMPTY FIELD|READ FAILED|FAILED secrets check|FAIL:|op-session:|error|exception',
        line, re.IGNORECASE)]
    if not causes:
        causes = [line for line in lines if not re.search(
            r'Main process exited|Failed with result|Failed to start|Triggering OnFailure|Consumed .*CPU time', line)]
        causes = causes[-4:]
    # Keep the beginning of each cause: chopping the last 400 bytes used to
    # start alerts halfway through "42 chart(s) checked".
    return '\n'.join(causes)[:400] or 'no application diagnostics available'


if __name__ == '__main__':
    print(summarize(sys.stdin.read()))
