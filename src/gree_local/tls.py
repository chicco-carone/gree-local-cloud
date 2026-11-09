"""TLS 1.2 experiment scoped to the owner's WB04BRT13 firmware.

The device verifies the genuine Gree certificate but its tick-seeded RNG
makes the RSA premaster reproducible. No vendor private key is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import ssl
import time
from pathlib import Path

from Crypto.Cipher import AES

from gree_local.weak_rng import observe, rng_bytes, tick_mask


def prf(secret: bytes, label: bytes, seed: bytes, size: int) -> bytes:
    seed = label + seed
    a = seed
    result = b""
    while len(result) < size:
        a = hmac.digest(secret, a, "sha384")
        result += hmac.digest(secret, a + seed, "sha384")
    return result[:size]


def handshake(kind: int, data: bytes) -> bytes:
    return bytes([kind]) + len(data).to_bytes(3, "big") + data


def record(kind: int, data: bytes) -> bytes:
    return bytes([kind]) + b"\x03\x03" + len(data).to_bytes(2, "big") + data


class Connection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        certificate: str = "certs/gree-device.pem",
    ) -> None:
        self.certificate = certificate
        self.reader = reader
        self.writer = writer
        self.in_seq = 0
        self.out_seq = 0
        self.buffer = b""

    async def read_record(self) -> tuple[int, bytes]:
        head = await self.reader.readexactly(5)
        if head[1:3] not in (b"\x03\x01", b"\x03\x03"):
            raise ValueError("Unexpected TLS version")
        length = int.from_bytes(head[3:5], "big")
        if length > 18432:
            raise ValueError("Oversize TLS record")
        return head[0], await self.reader.readexactly(length)

    def decrypt(self, kind: int, data: bytes) -> bytes:
        nonce = self.client_iv + data[:8]
        cipher = AES.new(self.client_key, AES.MODE_GCM, nonce=nonce)
        cipher.update(
            self.in_seq.to_bytes(8, "big")
            + bytes([kind])
            + b"\x03\x03"
            + (len(data) - 24).to_bytes(2, "big")
        )
        plain = cipher.decrypt_and_verify(data[8:-16], data[-16:])
        self.in_seq += 1
        return plain

    async def send(self, kind: int, plain: bytes) -> None:
        explicit = self.out_seq.to_bytes(8, "big")
        cipher = AES.new(self.server_key, AES.MODE_GCM, nonce=self.server_iv + explicit)
        cipher.update(
            explicit + bytes([kind]) + b"\x03\x03" + len(plain).to_bytes(2, "big")
        )
        data, tag = cipher.encrypt_and_digest(plain)
        self.writer.write(record(kind, explicit + data + tag))
        await self.writer.drain()
        self.out_seq += 1

    def keys(self, master: bytes, cr: bytes, sr: bytes) -> None:
        kb = prf(master, b"key expansion", sr + cr, 72)
        self.client_key, self.server_key, self.client_iv, self.server_iv = (
            kb[:32],
            kb[32:64],
            kb[64:68],
            kb[68:72],
        )

    async def accept(self) -> Connection:
        kind, ch = await self.read_record()
        if kind != 22 or ch[0] != 1:
            raise ValueError("Expected ClientHello")
        cr = ch[6:38]
        state, ticks = observe(cr)
        sid_len = ch[38]
        suite_off = 39 + sid_len
        suite_len = int.from_bytes(ch[suite_off : suite_off + 2], "big")
        suites = ch[suite_off + 2 : suite_off + 2 + suite_len]
        if b"\x00\x9d" not in [suites[i : i + 2] for i in range(0, len(suites), 2)]:
            raise ValueError("Expected RSA AES256 GCM")
        sr = os.urandom(32)
        sh = handshake(
            2, b"\x03\x03" + sr + b"\x00\x00\x9d\x00" + b"\x00\x05\xff\x01\x00\x01\x00"
        )
        cert = ssl.PEM_cert_to_DER_cert(Path(self.certificate).read_text())
        chain = len(cert).to_bytes(3, "big") + cert
        server_handshake = (
            sh
            + handshake(11, len(chain).to_bytes(3, "big") + chain)
            + handshake(14, b"")
        )
        transcript = ch + server_handshake
        self.writer.write(record(22, server_handshake))
        await self.writer.drain()
        got_ccs = False
        while not got_ccs:
            kind, payload = await self.read_record()
            if kind == 22:
                transcript += payload
            elif kind == 20:
                got_ccs = True
            elif kind == 21:
                raise ValueError("Client TLS alert " + payload.hex())
            else:
                raise ValueError("Unexpected handshake record " + str(kind))
        kind, encrypted = await self.read_record()
        if kind != 22:
            raise ValueError("Expected encrypted Finished")
        start = time.monotonic()
        masks = set()
        # 4 seconds at 1kHz. Distinct masks are far fewer than tick candidates.
        for t in ticks:
            for delta in range(4001):
                masks.add(tick_mask((t + delta) & 0xFFFFFFFF))
        # Try nearest changes first, then the remainder.
        ordered = list(
            dict.fromkeys(
                [tick_mask((t + d) & 0xFFFFFFFF) for d in range(32) for t in ticks]
            )
        )
        ordered.extend(masks - set(ordered))
        for n, mask in enumerate(ordered):
            pms = b"\x03\x03" + rng_bytes(state, mask, 46)
            master = prf(pms, b"master secret", cr + sr, 48)
            self.keys(master, cr, sr)
            try:
                finished = self.decrypt(22, encrypted)
            except ValueError:
                continue
            expected = handshake(
                20,
                prf(
                    master, b"client finished", hashlib.sha384(transcript).digest(), 12
                ),
            )
            if finished != expected:
                raise ValueError("Finished transcript mismatch")
            transcript += finished
            sf = handshake(
                20,
                prf(
                    master, b"server finished", hashlib.sha384(transcript).digest(), 12
                ),
            )
            self.writer.write(record(20, b"\x01"))
            await self.send(22, sf)
            logging.getLogger(__name__).info(
                "Device TLS established (%s RNG candidates, %.2fs)",
                n + 1,
                time.monotonic() - start,
            )
            return self
        raise ValueError(
            "Unable to reproduce device premaster; tried "
            + str(len(ordered))
            + " masks"
        )

    async def read(self) -> bytes:
        while True:
            kind, data = await self.read_record()
            plain = self.decrypt(kind, data)
            if kind == 23:
                return plain
            if kind == 21:
                return b""

    async def write(self, data: bytes) -> None:
        await self.send(23, data)
