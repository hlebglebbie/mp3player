"""Proxy-aware networking.

Two things live here:

* :class:`Proxy` — parses a proxy URL (http/https CONNECT or socks5/socks5h) and
  can open a TCP stream to an arbitrary host:port through it.  Every BitTorrent
  peer connection goes through :meth:`Proxy.open_tcp`, so tracker discovery,
  peer handshakes and piece traffic all leave the machine through the proxy.
* :func:`http_request` — small HTTP client used for the search API and HTTP
  tracker announces.  It builds its own opener so the proxy decision is
  explicit instead of whatever urllib picks up from the environment.
"""

from __future__ import annotations

import base64
import gzip
import io
import ipaddress
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
import zlib

DEFAULT_UA = "mp3player/1.0 (+terminal; libtorrent-free)"


class ProxyError(OSError):
    pass


class Proxy:
    """A parsed proxy endpoint."""

    def __init__(self, scheme: str, host: str, port: int, username: str = "", password: str = ""):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    # ------------------------------------------------------------------ parse
    @classmethod
    def parse(cls, url: str) -> "Proxy":
        url = url.strip()
        if "://" not in url:
            url = "http://" + url
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "http").lower()
        if scheme not in ("http", "https", "socks5", "socks5h", "socks4", "socks"):
            raise ProxyError(f"unsupported proxy scheme: {scheme}")
        if scheme == "socks":
            scheme = "socks5h"
        host = parts.hostname
        if not host:
            raise ProxyError(f"proxy has no host: {url!r}")
        port = parts.port or (1080 if scheme.startswith("socks") else 8080)
        user = urllib.parse.unquote(parts.username or "")
        pwd = urllib.parse.unquote(parts.password or "")
        return cls(scheme, host, port, user, pwd)

    @property
    def is_socks(self) -> bool:
        return self.scheme.startswith("socks")

    def url(self, hide_password: bool = True) -> str:
        auth = ""
        if self.username:
            pwd = "***" if hide_password else self.password
            auth = f"{urllib.parse.quote(self.username)}:{urllib.parse.quote(pwd)}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.url()

    def __repr__(self) -> str:  # pragma: no cover
        return f"Proxy({self.url()})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Proxy) and (self.scheme, self.host, self.port, self.username) == (
            other.scheme, other.host, other.port, other.username,
        )

    def __hash__(self) -> int:
        return hash((self.scheme, self.host, self.port, self.username))

    # ------------------------------------------------------------------ sockets
    def _connect_proxy(self, timeout: float) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(timeout)
        return sock

    def open_tcp(self, host: str, port: int, timeout: float = 15.0) -> socket.socket:
        """Open a TCP stream to ``host:port`` tunnelled through the proxy."""
        sock = self._connect_proxy(timeout)
        try:
            if self.is_socks:
                self._socks5_handshake(sock, host, port)
            else:
                self._http_connect(sock, host, port)
            sock.settimeout(timeout)
            return sock
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _http_connect(self, sock: socket.socket, host: str, port: int) -> None:
        target = f"{host}:{port}"
        lines = [
            f"CONNECT {target} HTTP/1.1",
            f"Host: {target}",
            "Proxy-Connection: Keep-Alive",
        ]
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        request = "\r\n".join(lines) + "\r\n\r\n"
        sock.sendall(request.encode("ascii"))

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ProxyError("proxy closed the CONNECT tunnel")
            buf += chunk
            if len(buf) > 65536:
                raise ProxyError("proxy CONNECT response too large")
        status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].startswith("2"):
            raise ProxyError(f"proxy refused CONNECT {target}: {status_line}")

    def _socks5_handshake(self, sock: socket.socket, host: str, port: int) -> None:
        methods = b"\x00\x02" if self.username else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        reply = _recv_exact(sock, 2)
        if reply[0:1] != b"\x05":
            raise ProxyError("not a SOCKS5 proxy")
        method = reply[1]
        if method == 0x02:
            if not self.username:
                raise ProxyError("SOCKS5 proxy requires authentication")
            user = self.username.encode()
            pwd = self.password.encode()
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
            auth = _recv_exact(sock, 2)
            if auth[1] != 0x00:
                raise ProxyError("SOCKS5 authentication failed")
        elif method != 0x00:
            raise ProxyError(f"no acceptable SOCKS5 auth method (got {method})")

        # socks5h resolves the name at the proxy; socks5 resolves locally.
        if self.scheme == "socks5h":
            name = host.encode("idna") if host else b""
            request = b"\x05\x01\x00\x03" + bytes([len(name)]) + name
        else:
            request = None
            try:
                addr = ipaddress.ip_address(host)
                if addr.version == 4:
                    request = b"\x05\x01\x00\x01" + addr.packed
                else:
                    request = b"\x05\x01\x00\x04" + addr.packed
            except ValueError:
                name = host.encode("idna")
                request = b"\x05\x01\x00\x03" + bytes([len(name)]) + name
        sock.sendall(request + struct.pack(">H", port))

        head = _recv_exact(sock, 4)
        if head[1] != 0x00:
            raise ProxyError(f"SOCKS5 connect failed (code {head[1]})")
        atyp = head[3]
        if atyp == 0x01:
            _recv_exact(sock, 4)
        elif atyp == 0x04:
            _recv_exact(sock, 16)
        elif atyp == 0x03:
            length = _recv_exact(sock, 1)[0]
            _recv_exact(sock, length)
        else:
            raise ProxyError("SOCKS5: bad address type in reply")
        _recv_exact(sock, 2)


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ProxyError("proxy closed connection during handshake")
        data += chunk
    return data


def make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


# ---------------------------------------------------------------------- HTTP


class HTTPResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str], url: str):
        self.status = status
        self.body = body
        self.headers = headers
        self.url = url

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, "replace")

    def json(self):
        import json

        return json.loads(self.body.decode("utf-8", "replace"))


def http_request(
    url: str,
    proxy: Proxy | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 20.0,
    max_bytes: int = 32 * 1024 * 1024,
    user_agent: str = DEFAULT_UA,
) -> HTTPResponse:
    """Perform an HTTP(S) request, optionally forced through ``proxy``."""
    hdrs = {"User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)

    handlers: list[urllib.request.BaseHandler] = []
    if proxy is not None:
        if proxy.is_socks:
            handlers.append(_SocksHTTPHandler(proxy))
        else:
            proxy_url = proxy.url(hide_password=False)
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
    else:
        handlers.append(urllib.request.ProxyHandler({}))

    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes)
            status = response.status
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        status = exc.code
        resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        final_url = url
    encoding = resp_headers.get("content-encoding", "").lower()
    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                pass
    return HTTPResponse(status, raw, resp_headers, final_url)


class _SocksHTTPHandler(urllib.request.BaseHandler):
    """Route HTTP(S) requests through a SOCKS5 proxy by hand."""

    handler_order = 500

    def __init__(self, proxy: Proxy):
        self.proxy = proxy

    def http_open(self, req):
        return self._do(req, tls=False)

    def https_open(self, req):
        return self._do(req, tls=True)

    def _do(self, req, tls: bool):
        parts = urllib.parse.urlsplit(req.full_url)
        host = parts.hostname or ""
        port = parts.port or (443 if tls else 80)
        timeout = req.timeout or 20
        sock = self.proxy.open_tcp(host, port, timeout=timeout)
        try:
            if tls:
                sock = make_ssl_context().wrap_socket(sock, server_hostname=host)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            head = [f"{req.get_method()} {path} HTTP/1.1", f"Host: {parts.netloc}"]
            for key, value in req.header_items():
                if key.lower() in ("host", "connection"):
                    continue
                head.append(f"{key}: {value}")
            head.append("Connection: close")
            sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
            if req.data:
                sock.sendall(req.data)
            raw = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return _RawHTTPResponse(req.full_url, raw)


class _RawHTTPResponse(io.BytesIO):
    def __init__(self, url: str, raw: bytes):
        head, _, body = raw.partition(b"\r\n\r\n")
        head_lines = head.decode("latin-1", "replace").split("\r\n")
        status_parts = head_lines[0].split()
        status = int(status_parts[1]) if len(status_parts) > 1 and status_parts[1].isdigit() else 599
        headers = {}
        for line in head_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = _dechunk(body)
        super().__init__(body)
        self.status = status
        self.code = status
        self.headers = _HeaderBag(headers)
        self.url = url
        self.msg = ""

    def geturl(self) -> str:
        return self.url

    def info(self):
        return self.headers

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _HeaderBag(dict):
    def getheaders(self) -> list[tuple[str, str]]:  # pragma: no cover
        return list(self.items())

    def get_all(self, name: str, default=None):  # pragma: no cover
        return [self.get(name.lower(), default)]


def _dechunk(data: bytes) -> bytes:
    out = b""
    while data:
        line, sep, rest = data.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        data = rest[size + 2:]
    return out
