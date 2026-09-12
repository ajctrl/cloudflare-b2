import { XMLParser, XMLValidator } from 'fast-xml-parser';

const parser = new XMLParser({
    parseTagValue: false,
    trimValues: false,
    isArray: name => name === 'Contents' || name === 'CommonPrefixes',
});

function escapeHtml(value) {
    return value.replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[char]);
}

export async function listDirectory(client, upstream, env, request, probe = false) {
    if (String(env.ALLOW_LIST_BUCKET) !== 'true') {
        return new Response(null, { status: 404 });
    }

    const url = new URL(upstream);
    const bucketEnd = env.BUCKET_NAME === '$path' ? url.pathname.indexOf('/', 1) : 0;
    const bucketPath = bucketEnd === -1 ? url.pathname : url.pathname.slice(0, bucketEnd);
    const keyPath = bucketEnd === -1 ? '' : url.pathname.slice(bucketEnd + 1);
    let prefix;
    try {
        prefix = decodeURIComponent(keyPath);
    } catch {
        return new Response('Invalid path encoding', { status: 400 });
    }
    if (prefix && !prefix.endsWith('/')) prefix += '/';
    url.pathname = bucketPath + '/';
    url.search = '';
    url.searchParams.set('list-type', '2');
    url.searchParams.set('delimiter', '/');
    url.searchParams.set('prefix', prefix);
    url.searchParams.set('encoding-type', 'url');

    const entries = new Map();
    const tokens = new Set();
    let exists = prefix === '';
    try {
        while (true) {
            // Listing requests need their own signature and no object-specific headers.
            const response = await fetch(await client.sign(url.toString(), { method: 'GET' }));
            if (!response.ok) return response;
            const xml = await response.text();
            if (XMLValidator.validate(xml) !== true) throw new Error('Invalid XML');
            const result = parser.parse(xml).ListBucketResult;
            if (!result || typeof result !== 'object') throw new Error('Invalid listing');
            const decode = value => result.EncodingType === 'url' ? decodeURIComponent(value) : value;
            for (const item of [...(result.Contents || []), ...(result.CommonPrefixes || [])]) {
                const key = decode(item.Key ?? item.Prefix);
                if (!key.startsWith(prefix)) throw new Error('Invalid listing prefix');
                exists = true;
                const name = key.slice(prefix.length);
                if (!name) continue; // Ignore the directory marker itself.
                const segment = name.endsWith('/') ? name.slice(0, -1) : name;
                if (!segment || segment.includes('/') || segment === '.' || segment === '..') continue;
                entries.set(name, './' + encodeURIComponent(segment) + (name.endsWith('/') ? '/' : ''));
            }
            if (result.IsTruncated !== 'true') break;
            const token = result.NextContinuationToken;
            if (!token || tokens.has(token)) throw new Error('Invalid continuation token');
            tokens.add(token);
            url.searchParams.set('continuation-token', token);
        }
    } catch {
        return new Response('Unable to read B2 directory listing', { status: 502 });
    }

    if (!exists) return new Response(null, { status: 404 });
    const incoming = new URL(request.url);
    if (probe || !incoming.pathname.endsWith('/')) {
        incoming.pathname += '/';
        return new Response(null, { status: 301, headers: { Location: incoming.toString(), 'Cache-Control': 'no-store' } });
    }
    const links = Array.from(entries, ([name, href]) =>
        `<li><a href="${escapeHtml(href)}">${escapeHtml(name)}</a></li>`).join('\n');
    const html = `<!doctype html>\n<html><head><meta charset="utf-8"><title>Index</title></head><body><ul>\n${links}\n</ul></body></html>`;
    return new Response(request.method === 'HEAD' ? null : html, {
        headers: {
            'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
            'X-Content-Type-Options': 'nosniff',
        }
    });
}
