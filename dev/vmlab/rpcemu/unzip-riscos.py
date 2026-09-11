"""Extract a RISC OS zip, keeping the filetypes.

RISC OS has no filename extensions: a file's type is a 12-bit number held
alongside it by the filesystem, and it is what makes a module loadable, an Obey
file runnable and a font a font. ROOL's zips carry that number in an Acorn extra
field (id 0x4341, "ARC0" followed by the load and exec addresses), which Linux
unzip silently discards.

The symptom, if you skip this, is a RISC OS that boots as far as !Boot and then
fails every RMEnsure in it, because every module in the boot sequence arrived as
a text file. That is exactly what the first HardDisc4 extraction here did.

RPCEmu's HostFS reads the type back off a ",xxx" suffix on the host filename,
which is the convention this writes.
"""

from __future__ import annotations

import os
import struct
import sys
import zipfile

ACORN_EXTRA = 0x4341

# A load address of 0xFFFxxxxx means "typed file", with the type in bits 8-19.
# Anything else is a real load address from a pre-RISC-OS-2 file and carries no
# type at all.
TYPED_MASK = 0xFFF00000


def filetype(extra: bytes) -> int | None:
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = struct.unpack("<HH", extra[offset:offset + 4])
        body = extra[offset + 4:offset + 4 + size]
        if header_id == ACORN_EXTRA and body[:4] == b"ARC0" and len(body) >= 8:
            load = struct.unpack("<I", body[4:8])[0]
            if load & TYPED_MASK == TYPED_MASK:
                return (load >> 8) & 0xFFF
            return None
        offset += 4 + size
    return None


def main() -> int:
    archive, prefix, dest = sys.argv[1], sys.argv[2], sys.argv[3]
    typed = untyped = 0

    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if not info.filename.startswith(prefix):
                continue
            relative = info.filename[len(prefix):].lstrip("/")
            if not relative:
                continue
            target = os.path.join(dest, relative)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue

            kind = filetype(info.extra)
            if kind is None:
                untyped += 1
            else:
                typed += 1
                target = f"{target},{kind:03x}"

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())

    print(f"unzip-riscos: {typed} typed, {untyped} untyped", flush=True)
    # A HardDisc4 with no typed files at all is the failure this exists to
    # prevent, and it is silent otherwise: RISC OS boots and then falls over
    # inside !Boot with errors that name none of this.
    if typed == 0:
        print("unzip-riscos: no filetypes found - the archive is not a RISC OS zip")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
