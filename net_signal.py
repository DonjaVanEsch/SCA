"""Extracts genuinely on-the-wire signals from a captured client-fingerprint
pcap -- independent of anything the client itself claims in its self-report.

Two signals, depending on which port the capture was aimed at:
  - HTTP User-Agent header (plain-HTTP clients: http.client/requests/httpx/
    urllib3) -- present in cleartext in the captured TCP payload.
  - TLS ClientHello JA3 fingerprint (raw-TLS clients: pyopenssl-raw/
    m2crypto-raw) -- the cipher/extension/curve profile the crypto library
    itself puts on the wire during the handshake, before anything is
    encrypted.

No third-party pcap/TLS library is used (the project's venv is intentionally
bare -- just Flask) -- pcap/Ethernet/IP/TCP/TLS framing is simple enough to
hand-parse with stdlib `struct`, and `tarfile` already handles the
`docker cp ... -` tar wrapper.
"""

import base64
import hashlib
import io
import re
import struct
import tarfile

# RFC 8701 GREASE values (0x0a0a, 0x1a1a, ..., 0xfafa) -- reserved probe
# values a client may randomly insert into its cipher/extension/group lists
# to test server tolerance for unknown values. JA3 excludes them, since
# they're randomized per-connection and would otherwise make every capture
# from the same client look like a different fingerprint.
_GREASE = frozenset((b << 8) | b for b in range(0x0A, 0x100, 0x10))


def _is_grease(value: int) -> bool:
    return value in _GREASE


def _parse_pcap(data: bytes):
    """Returns (linktype, [frame_bytes, ...]) for a classic (non-pcapng)
    pcap file, or (None, []) if the format isn't recognised."""
    if len(data) < 24:
        return None, []
    magic = data[0:4]
    if magic == b"\xa1\xb2\xc3\xd4" or magic == b"\xa1\xb2\x3c\x4d":
        endian = ">"
    elif magic == b"\xd4\xc3\xb2\xa1" or magic == b"\x4d\x3c\xb2\xa1":
        endian = "<"
    else:
        return None, []  # unrecognised format (e.g. pcapng)
    linktype = struct.unpack_from(endian + "I", data, 20)[0]

    frames = []
    offset = 24
    while offset + 16 <= len(data):
        incl_len = struct.unpack_from(endian + "I", data, offset + 8)[0]
        offset += 16
        if offset + incl_len > len(data):
            break
        frames.append(data[offset:offset + incl_len])
        offset += incl_len
    return linktype, frames


_LINKTYPE_ETHERNET = 1
_LINKTYPE_LINUX_SLL = 113   # "Linux cooked capture" v1
_LINKTYPE_LINUX_SLL2 = 276  # "Linux cooked capture" v2


def _ip_start_offset(frame: bytes, linktype: int):
    """Returns the offset where the IPv4 packet starts, or None if this
    frame isn't IPv4 (or is a link-layer type not handled here). Every
    fingerprint capture runs `tcpdump -i any`, which uses Linux "cooked
    capture" framing (SLL/SLL2) rather than plain Ethernet -- there's no
    real Ethernet header to skip, just a fixed-size cooked-capture header
    with the protocol field already broken out."""
    if linktype == _LINKTYPE_ETHERNET:
        if len(frame) < 14:
            return None
        ethertype = struct.unpack_from(">H", frame, 12)[0]
        return 14 if ethertype == 0x0800 else None
    if linktype == _LINKTYPE_LINUX_SLL2:
        if len(frame) < 20:
            return None
        protocol = struct.unpack_from(">H", frame, 0)[0]
        return 20 if protocol == 0x0800 else None
    if linktype == _LINKTYPE_LINUX_SLL:
        if len(frame) < 16:
            return None
        protocol = struct.unpack_from(">H", frame, 14)[0]
        return 16 if protocol == 0x0800 else None
    return None


def _tcp_payload(frame: bytes, linktype: int):
    """Returns (src_port, dst_port, payload) for an IPv4/TCP frame, or None
    for anything else (ARP, IPv6, non-TCP, truncated)."""
    ip_start = _ip_start_offset(frame, linktype)
    if ip_start is None or len(frame) < ip_start + 20:
        return None
    ihl = (frame[ip_start] & 0x0F) * 4
    proto = frame[ip_start + 9]
    if proto != 6:
        return None
    tcp_start = ip_start + ihl
    if len(frame) < tcp_start + 20:
        return None
    src_port, dst_port = struct.unpack_from(">HH", frame, tcp_start)
    data_offset = (frame[tcp_start + 12] >> 4) * 4
    payload_start = tcp_start + data_offset
    return src_port, dst_port, frame[payload_start:]


def _payload_to_port(pcap_bytes: bytes, dst_port: int) -> bytes:
    """Concatenates every packet's payload addressed to dst_port, in capture
    order -- i.e. everything the client sent to the target on that port."""
    linktype, frames = _parse_pcap(pcap_bytes)
    if linktype is None:
        return b""
    chunks = []
    for frame in frames:
        parsed = _tcp_payload(frame, linktype)
        if parsed and parsed[1] == dst_port and parsed[2]:
            chunks.append(parsed[2])
    return b"".join(chunks)


def _find_client_hello(payload: bytes):
    """Scans reassembled TCP payload bytes for a TLS Handshake record
    carrying a ClientHello, returns its body (without the 4-byte handshake
    header) or None."""
    i = 0
    while i + 5 <= len(payload):
        if payload[i] == 0x16 and payload[i + 1] == 0x03:
            rec_len = struct.unpack_from(">H", payload, i + 3)[0]
            body = payload[i + 5:i + 5 + rec_len]
            if len(body) == rec_len and body and body[0] == 0x01:
                hs_len = int.from_bytes(body[1:4], "big")
                hs_body = body[4:4 + hs_len]
                if len(hs_body) == hs_len:
                    return hs_body
            i += 5 + rec_len
        else:
            i += 1
    return None


def _ja3_from_client_hello(hs_body: bytes):
    """Parses a ClientHello body into the standard JA3 tuple (SSL version,
    cipher suites, extensions, elliptic curves, EC point formats -- GREASE
    values excluded from all four lists) and returns (md5_hash, ja3_string),
    or None if the body is too short/malformed to parse safely."""
    if len(hs_body) < 35:
        return None
    client_version = struct.unpack_from(">H", hs_body, 0)[0]
    pos = 2 + 32
    session_id_len = hs_body[pos]
    pos += 1 + session_id_len
    if pos + 2 > len(hs_body):
        return None

    cs_len = struct.unpack_from(">H", hs_body, pos)[0]
    pos += 2
    cipher_bytes = hs_body[pos:pos + cs_len]
    if len(cipher_bytes) != cs_len or cs_len % 2:
        return None
    ciphers = [v for v in struct.unpack(f">{cs_len // 2}H", cipher_bytes) if not _is_grease(v)]
    pos += cs_len

    if pos >= len(hs_body):
        return None
    comp_len = hs_body[pos]
    pos += 1 + comp_len

    extensions, curves, point_formats = [], [], []
    if pos + 2 <= len(hs_body):
        ext_total_len = struct.unpack_from(">H", hs_body, pos)[0]
        pos += 2
        end = min(pos + ext_total_len, len(hs_body))
        while pos + 4 <= end:
            ext_type, ext_len = struct.unpack_from(">HH", hs_body, pos)
            pos += 4
            ext_data = hs_body[pos:pos + ext_len]
            pos += ext_len
            if not _is_grease(ext_type):
                extensions.append(ext_type)
            if ext_type == 0x000A and len(ext_data) >= 2:  # supported_groups
                list_len = struct.unpack_from(">H", ext_data, 0)[0]
                group_bytes = ext_data[2:2 + list_len]
                if len(group_bytes) == list_len and list_len % 2 == 0:
                    curves = [v for v in struct.unpack(f">{list_len // 2}H", group_bytes) if not _is_grease(v)]
            elif ext_type == 0x000B and len(ext_data) >= 1:  # ec_point_formats
                fmt_len = ext_data[0]
                point_formats = list(ext_data[1:1 + fmt_len])

    ja3_string = "{},{},{},{},{}".format(
        client_version,
        "-".join(str(c) for c in ciphers),
        "-".join(str(e) for e in extensions),
        "-".join(str(c) for c in curves),
        "-".join(str(p) for p in point_formats),
    )
    return hashlib.md5(ja3_string.encode()).hexdigest(), ja3_string


_UA_RE = re.compile(rb"User-Agent:\s*([^\r\n]+)", re.IGNORECASE)


def extract_network_signals(pcap_raw_b64: str, https_port: int, http_port: int) -> dict:
    """Given the base64 `docker cp ... -` tar blob saved during a client
    fingerprint capture, extract whatever on-the-wire signal is actually
    available: a JA3 TLS fingerprint if the capture targeted the HTTPS port,
    an HTTP User-Agent header if it targeted the plain HTTP port. Always
    returns all three keys; unavailable ones are None -- callers don't need
    to know which port was used."""
    result = {"user_agent": None, "ja3_hash": None, "ja3_string": None}
    if not pcap_raw_b64:
        return result
    try:
        raw = base64.b64decode(pcap_raw_b64)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            member = tar.getmembers()[0]
            extracted = tar.extractfile(member)
            pcap_bytes = extracted.read() if extracted else b""
    except Exception:
        return result

    for port in (https_port, http_port):
        payload = _payload_to_port(pcap_bytes, port)
        if not payload:
            continue
        hello = _find_client_hello(payload)
        if hello:
            parsed = _ja3_from_client_hello(hello)
            if parsed:
                result["ja3_hash"], result["ja3_string"] = parsed
        m = _UA_RE.search(payload)
        if m:
            result["user_agent"] = m.group(1).decode("latin-1", errors="replace").strip()
    return result
