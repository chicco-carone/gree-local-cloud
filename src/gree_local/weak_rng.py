"""Reproduce the RNG in this owner's U-WB04BRT13 v1.38 firmware."""

from __future__ import annotations

import struct

MASK_A = 0x3F807F80
MASK_B = 0x000F80FF


def tick_mask(tick: int) -> int:
    return ((tick << 13) ^ (tick >> 9)) & 0xFFFFFFFF


def next_word(state: int, mask: int) -> int:
    return ((state << 7) & MASK_A) ^ ((state >> 8) & MASK_B) ^ mask


def tick_candidates(mask: int) -> list[int]:
    basis = {}
    null = []
    for i in range(32):
        v = tick_mask(1 << i)
        x = 1 << i
        while v:
            k = v.bit_length() - 1
            if k not in basis:
                basis[k] = (v, x)
                break
            a, b = basis[k]
            v ^= a
            x ^= b
        if not v:
            null.append(x)
    v = mask
    x = 0
    while v:
        k = v.bit_length() - 1
        if k not in basis:
            raise ValueError("Inconsistent RNG observation")
        a, b = basis[k]
        v ^= a
        x ^= b
    results = [x]
    for n in null:
        results += [v ^ n for v in results]
    return results


def observe(random: bytes) -> tuple[int, list[int]]:
    words = struct.unpack("<8I", random)
    masks = [b ^ next_word(a, 0) for a, b in zip(words, words[1:])]
    if len(set(masks)) != 1:
        raise ValueError("Client random is not the expected constant-tick RNG sequence")
    return words[-1], tick_candidates(masks[0])


def rng_bytes(state: int, mask: int, length: int) -> bytes:
    out = bytearray()
    while len(out) < length:
        state = next_word(state, mask)
        out.extend(struct.pack("<I", state))
    return bytes(out[:length])
