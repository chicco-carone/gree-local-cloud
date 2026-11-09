import struct
import sys
from pathlib import Path

from capstone import (  # ty: ignore[import-not-found] # Optional firmware-analysis dependency.
    CS_ARCH_ARM,
    CS_MODE_LITTLE_ENDIAN,
    CS_MODE_THUMB,
    Cs,
)

D = Path(".private/firmware-1.38.bin").read_bytes()
BASE = 0x9AFF8000
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN)
md.detail = True
for text in sys.argv[1:]:
    p = D.find(text.encode())
    print(text, "offset", hex(p), "address", hex(p + 0x9B768000))
    for off in range(0x8140, 0x96AE0, 2):
        # Thumb ldr literal narrow, or wide T2
        hw = struct.unpack_from("<H", D, off)[0]
        lit = None
        if hw & 0xF800 == 0x4800:
            lit = ((off + BASE + 4) & ~3) + ((hw & 255) * 4) - BASE
        elif hw == 0xF8DF:
            lit = (
                ((off + BASE + 4) & ~3)
                + (struct.unpack_from("<H", D, off + 2)[0] & 4095)
                - BASE
            )
        if (
            lit is not None
            and 0 <= lit <= len(D) - 4
            and struct.unpack_from("<I", D, lit)[0] == p + 0x9B768000
        ):
            print("XREF", hex(off), hex(off + BASE))
            for ins in md.disasm(
                D[max(0, off - 48) : off + 64], BASE + max(0, off - 48)
            ):
                print(f"{ins.address:08x}: {ins.mnemonic:8} {ins.op_str}")
