"""Device-scoped offline DNS: no upstream resolver is contacted."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import cast

from dnslib import QTYPE, RCODE, RR, A, DNSRecord

_LOG = logging.getLogger(__name__)


class DeviceDNS(asyncio.DatagramProtocol):
    def __init__(self, device_ip: str, server_ip: str, names: Iterable[str]) -> None:
        self.device_ip = device_ip
        self.server_ip = server_ip
        self.names = {n.rstrip(".").lower() for n in names}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr[0] not in (self.device_ip, "127.0.0.1"):
            return
        try:
            req = DNSRecord.parse(data)
            reply = req.reply()
            name = str(req.q.qname).rstrip(".").lower()
            if name in self.names:
                if req.q.qtype in (QTYPE.A, QTYPE.ANY):
                    reply.add_answer(
                        RR(req.q.qname, QTYPE.A, ttl=60, rdata=A(self.server_ip))
                    )
                _LOG.info("Local DNS: %s -> %s", name, self.server_ip)
            else:
                reply.header.rcode = RCODE.NXDOMAIN
                _LOG.info("Unmapped device DNS query: %s", name)
            self.transport.sendto(reply.pack(), addr)
        except Exception as exc:
            _LOG.debug("Discarding malformed DNS packet: %s", exc)
