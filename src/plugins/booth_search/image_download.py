"""受限图片下载：逐跳白名单、公网地址检查、固定连接 IP、TLS 主机校验。"""
import asyncio
import http.client
import ipaddress
import socket
import ssl
import urllib.parse

DEFAULT_HOSTS = (
    "multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn",
    "booth.pximg.net", "booth.pm",
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ImageDownloadError(ValueError):
    pass


def _target(url, allowed_hosts):
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in (None, 443) or host not in allowed_hosts):
            raise ImageDownloadError("图片地址不在允许的 HTTPS 来源中")
        addresses = list(dict.fromkeys(
            info[4][0] for info in socket.getaddrinfo(
                host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ImageDownloadError("图片地址解析到非公网目标")
        return parts, addresses
    except (OSError, ValueError) as exc:
        if isinstance(exc, ImageDownloadError):
            raise
        raise ImageDownloadError("图片地址无法安全解析") from exc


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _is_image(data):
    return (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")
            or data.startswith((b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"))


def _download_sync(url, allowed_hosts, timeout, max_bytes):
    allowed_hosts = {str(host).lower() for host in allowed_hosts}
    for _ in range(4):
        parts, addresses = _target(url, allowed_hosts)
        conn = _PinnedHTTPSConnection(parts.hostname, addresses[0], timeout)
        try:
            path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            conn.request("GET", path, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://booth.pm/",
                "Accept-Encoding": "identity",
            })
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise ImageDownloadError("图片重定向缺少目标")
                url = urllib.parse.urljoin(url, location)
                continue
            if not 200 <= resp.status < 300:
                raise ImageDownloadError(f"图片下载失败（HTTP {resp.status}）")
            length = resp.getheader("Content-Length")
            if length and (int(length) < 0 or int(length) > max_bytes):
                raise ImageDownloadError("图片大小超过允许范围")
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ImageDownloadError("图片大小超过允许范围")
            if len(data) < 100 or not _is_image(data):
                raise ImageDownloadError("下载内容不是受支持的图片")
            return data
        except (OSError, ValueError, http.client.HTTPException) as exc:
            if isinstance(exc, ImageDownloadError):
                raise
            raise ImageDownloadError("图片下载或格式校验失败") from exc
        finally:
            conn.close()
    raise ImageDownloadError("图片重定向次数过多")


async def download_image(url, *, allowed_hosts=DEFAULT_HOSTS, timeout=30,
                         max_bytes=MAX_IMAGE_BYTES):
    return await asyncio.to_thread(_download_sync, url, allowed_hosts, timeout, max_bytes)
