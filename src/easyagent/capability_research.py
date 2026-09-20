"""Bounded public service discovery; retrieved pages are evidence, never instructions."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

from .development import DocumentText


async def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Public documentation must use HTTPS without credentials')
    addresses = await asyncio.wait_for(
        asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM), timeout=10)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Documentation cannot access private or local addresses')
    return url


async def fetch_document(url):
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        for _ in range(4):
            await public_url(url)
            async with client.stream('GET', url, headers={'User-Agent': 'EasyAgent/0.1 public-document-reader'}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers['location'])
                    continue
                response.raise_for_status()
                content_type = response.headers.get('content-type', '')
                if not any(t in content_type for t in ('text/', 'json', 'xml')):
                    raise ValueError('Documentation must be text, JSON or HTML')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 500_000:
                        raise ValueError('Documentation exceeds the read budget')
                raw = data.decode('utf-8', errors='replace')
                parser = DocumentText()
                if 'html' in content_type:
                    parser.feed(raw)
                    text = ' '.join(parser.parts)
                else:
                    text = raw
                return {'url': url, 'text': text[:24000], 'untrusted_reference': True, '_html': raw}
    raise ValueError('Too many documentation redirects')


class SearchLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results, self.current = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and ('result__a' in attrs.get('class', '') or 'result-link' in attrs.get('class', '')):
            url = attrs.get('href', '')
            url = parse_qs(urlsplit(url).query).get('uddg', [url])[0]
            if url.startswith('https://'):
                self.current = {'url': url, 'title': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['title'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.results.append(self.current)
            self.current = None


async def public_search(query):
    page = await fetch_document('https://html.duckduckgo.com/html/?q=' + quote(query))
    parser = SearchLinks()
    parser.feed(page['_html'])
    if not parser.results:
        raise ValueError('Public search returned no usable results; a search connection may be required')
    return {'results': parser.results[:5]}


async def research(hub, ctx, queries):
    evidence = []
    available = [t['name'] for t in hub.available_tools() if t['name'].startswith('search.') and t['effect'] == 'read']
    for index, query in enumerate(queries[:2]):
        try:
            if available:
                result = await hub.tools.invoke(hub.store, ctx.job, available[0], {'query': query}, slot=100 + index)
            else:
                result = await public_search(query)
            rows = result.get('results', [])[:3]
            evidence.append({'query': query, 'results': rows, 'untrusted_reference': True})
            for row in rows[:2]:
                try:
                    document = await fetch_document(row['url'])
                    document.pop('_html', None)
                    evidence.append(document)
                except (ValueError, OSError, httpx.HTTPError):
                    evidence.append({'url': row.get('url'), 'error': 'document could not be read'})
        except (ValueError, OSError, httpx.HTTPError) as exc:
            evidence.append({'query': query, 'error': type(exc).__name__, 'message': 'Search could not complete'})
    return evidence
