"""Bounded public downloads with DNS pinned through redirects and TLS verification."""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx

LIMIT = 50_000_000


async def public_target(value):
    try:
        url = httpx.URL(value)
        if (url.scheme not in ("http", "https") or not url.host or url.userinfo
                or url.port not in (None, 80, 443)):
            raise ValueError("unsupported URL")
        host = url.host.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("local hostname")
        addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, url.port or (443 if url.scheme == "https" else 80), type=socket.SOCK_STREAM,
        ), 5)
        ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_unspecified
                          or (getattr(ip, "ipv4_mapped", None) is not None) for ip in ips):
            raise ValueError("non-public address")
    except (ValueError, OSError, TimeoutError, httpx.InvalidURL) as exc:
        raise ValueError("下载地址必须是公网 HTTP(S) 地址，不能含凭证或指向本机、私网及特殊用途地址。") from exc
    # The connection origin is the validated IP. Host and SNI retain the public hostname,
    # so the transport cannot resolve a second, potentially rebound DNS answer.
    return url, url.copy_with(host=str(ips[0]), fragment=None)


async def download(url, max_bytes=LIMIT):
    if not 1 <= max_bytes <= LIMIT:
        raise ValueError("download limit must be between 1 byte and 50 MB")
    try:
        async with asyncio.timeout(45), httpx.AsyncClient(
            timeout=15, follow_redirects=False, trust_env=False,
        ) as client:
            for _ in range(5):
                original, pinned = await public_target(url)
                host = original.netloc.decode("ascii")
                async with client.stream("GET", pinned, headers={
                    "Host": host, "Accept-Encoding": "identity", "User-Agent": "EasyAgent/0.1",
                }, extensions={"sni_hostname": original.host}) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("下载重定向缺少目标地址。")
                        url = str(original.join(location))
                        if original.scheme == "https" and httpx.URL(url).scheme != "https":
                            raise ValueError("拒绝将 HTTPS 下载重定向到非加密地址。")
                        client.cookies.clear()
                        continue
                    if response.status_code != 200:
                        raise ValueError(f"下载服务器返回 HTTP {response.status_code}，未保存为附件。")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("下载服务器未提供未压缩传输，无法保证文件大小限制。")
                    length = response.headers.get("content-length")
                    if length and (not length.isdigit() or int(length) > max_bytes):
                        raise ValueError("下载文件超过大小限制。")
                    content = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=64_000):
                        if len(content) + len(chunk) > max_bytes:
                            raise ValueError("下载文件超过大小限制。")
                        content.extend(chunk)
                    if not content:
                        raise ValueError("下载文件为空。")
                    return bytes(content), response.headers.get("content-type", "application/octet-stream").split(";")[0]
            raise ValueError("下载重定向次数超过限制。")
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError) as exc:
        # Provider errors can include signed URLs; do not persist their raw messages.
        raise ValueError("公网文件下载失败或超时；请检查链接及网络。") from exc
