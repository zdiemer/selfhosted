"""Correct the MariaDB driver's docstring escape before RomM starts.

Python 3.14 warns every time this un-cached dependency is compiled. Keep the
upstream image and all warnings enabled; change only the exact docstring line.
This becomes a no-op when the upstream driver fixes it.
"""
import os
from pathlib import Path
import sysconfig

path = Path(sysconfig.get_path("purelib")) / "mariadb" / "connectionpool.py"
source = path.read_text()
old = r"    - **\*\*kwargs** - Optional additional connection arguments, as described in mariadb.connect() method."
new = old.replace("\\", "\\\\")
if old in source:
    if source.count(old) != 1:
        raise RuntimeError("Unexpected MariaDB connectionpool docstring; review patch")
    path.write_text(source.replace(old, new))
    print("Corrected MariaDB connectionpool docstring escape", flush=True)
# Preserve upstream environment loading and nginx template generation.
os.execv("/docker-entrypoint.sh", ["/docker-entrypoint.sh", "/init"])
