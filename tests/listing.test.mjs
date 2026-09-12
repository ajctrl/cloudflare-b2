import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../index.js';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const env = {
    PROXY_TOKEN: 'test-token', B2_APPLICATION_KEY_ID: 'test-id',
    B2_APPLICATION_KEY: 'test-key', B2_ENDPOINT: 's3.us-west-004.backblazeb2.com',
    BUCKET_NAME: 'bucket', ALLOW_LIST_BUCKET: 'true',
};
const request = (path = '/', method = 'GET') => new Request('https://proxy.example' + path, {
    method, headers: { 'x-proxy-token': env.PROXY_TOKEN },
});
const xml = (keys = [], prefixes = [], extra = '') => new Response(
    '<ListBucketResult><EncodingType>url</EncodingType>' +
    keys.map(key => `<Contents><Key>${encodeURIComponent(key)}</Key></Contents>`).join('') +
    prefixes.map(prefix => `<CommonPrefixes><Prefix>${encodeURIComponent(prefix)}</Prefix></CommonPrefixes>`).join('') +
    extra + '</ListBucketResult>', { headers: { 'Content-Type': 'application/xml' } });

test('root listing follows pagination and emits escaped relative file and directory links', async () => {
    let calls = 0;
    globalThis.fetch = async signed => {
        const url = new URL(signed.url);
        assert.equal(url.hostname, 'bucket.' + env.B2_ENDPOINT);
        assert.equal(url.pathname, '/');
        assert.equal(url.searchParams.get('delimiter'), '/');
        assert.equal(url.searchParams.get('prefix'), '');
        assert.equal(signed.headers.has('x-proxy-token'), false);
        assert.ok(signed.headers.get('authorization'));
        if (++calls === 1) return xml(['001', 'a & <b>#%.txt'], ['sub/'],
            '<IsTruncated>true</IsTruncated><NextContinuationToken>a&amp;b</NextContinuationToken>');
        assert.equal(url.searchParams.get('continuation-token'), 'a&b');
        return xml(['last.txt'], ['sub/'], '<IsTruncated>false</IsTruncated>');
    };
    const response = await worker.fetch(request(), env);
    assert.equal(response.status, 200);
    assert.match(response.headers.get('content-type'), /^text\/html/);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    const html = await response.text();
    assert.match(html, /href="\.\/001"/);
    assert.match(html, /href="\.\/a%20%26%20%3Cb%3E%23%25.txt"/);
    assert.match(html, /a &amp; &lt;b&gt;#%.txt/);
    assert.match(html, /href="\.\/last.txt"/);
    assert.equal(html.match(/href="\.\/sub\/"/g).length, 1);
    assert.equal(calls, 2);
});

for (const [mode, path, upstreamPath, prefix, rclone] of [
    ['bucket', '/sub%20dir/', '/', 'sub dir/', false],
    ['$host', '/sub/', '/', 'sub/', false],
    ['$path', '/bucket/sub/', '/bucket/', 'sub/', false],
    ['bucket', '/file/bucket/sub/', '/', 'sub/', true],
    ['$path', '/file/bucket/sub/', '/bucket/', 'sub/', true],
]) {
    test(`nested listing: ${mode} ${path}`, async () => {
        globalThis.fetch = async signed => {
            const url = new URL(signed.url);
            assert.equal(url.pathname, upstreamPath);
            assert.equal(url.searchParams.get('prefix'), prefix);
            return xml([prefix, prefix + '\u65e5\u672c.txt'], [prefix + 'child/']);
        };
        const response = await worker.fetch(request(path), { ...env, BUCKET_NAME: mode, RCLONE_DOWNLOAD: String(rclone) });
        const html = await response.text();
        assert.equal(response.status, 200);
        assert.match(html, /href="\.\/%E6%97%A5%E6%9C%AC.txt"/);
        assert.match(html, /href="\.\/child\/"/);
        assert.equal((html.match(/<a /g) || []).length, 2);
    });
}

test('authentication and listing permission prevent upstream access', async () => {
    globalThis.fetch = () => { throw new Error('Must not fetch'); };
    assert.equal((await worker.fetch(new Request('https://proxy.example/'), env)).status, 401);
    for (const path of ['/', '/sub/']) {
        assert.equal((await worker.fetch(request(path), { ...env, ALLOW_LIST_BUCKET: 'false' })).status, 404);
    }
});

test('HEAD listing has HTML headers and no body, including upstream errors', async () => {
    globalThis.fetch = async () => xml(['file']);
    const response = await worker.fetch(request('/', 'HEAD'), env);
    assert.match(response.headers.get('content-type'), /^text\/html/);
    assert.equal(await response.text(), '');
    globalThis.fetch = async () => new Response('<Error/>', { status: 403 });
    const error = await worker.fetch(request('/', 'HEAD'), env);
    assert.equal(error.status, 403);
    assert.equal(await error.text(), '');
});

test('file downloads, HEAD metadata and range responses are preserved', async () => {
    globalThis.fetch = async () => new Response('file', {
        headers: { 'Content-Type': 'image/png', 'Content-Length': '4' },
    });
    assert.equal(await (await worker.fetch(request('/file'), env)).text(), 'file');
    const head = await worker.fetch(request('/file', 'HEAD'), env);
    assert.equal(head.headers.get('content-length'), '4');
    assert.equal(await head.text(), '');
    globalThis.fetch = async () => new Response('fi', { status: 206, headers: { 'Content-Range': 'bytes 0-1/4' } });
    const rangeRequest = request('/file');
    rangeRequest.headers.set('range', 'bytes=0-1');
    const range = await worker.fetch(rangeRequest, env);
    assert.equal(range.status, 206);
    assert.equal(await range.text(), 'fi');
});

test('directory without slash redirects after object miss; missing file keeps its 404', async () => {
    globalThis.fetch = async signed => new URL(signed.url).searchParams.has('list-type')
        ? xml(['sub/file']) : new Response('missing', { status: 404 });
    const response = await worker.fetch(request('/sub', 'HEAD'), env);
    assert.equal(response.status, 301);
    assert.equal(response.headers.get('location'), 'https://proxy.example/sub/');
    globalThis.fetch = async signed => new URL(signed.url).searchParams.has('list-type')
        ? xml() : new Response('missing', { status: 404 });
    const missing = await worker.fetch(request('/missing'), env);
    assert.equal(missing.status, 404);
    assert.equal(await missing.text(), 'missing');
});

test('empty bucket succeeds and missing directory returns 404', async () => {
    globalThis.fetch = async () => xml();
    assert.equal((await worker.fetch(request(), env)).status, 200);
    assert.equal((await worker.fetch(request('/missing/'), env)).status, 404);
});

test('invalid XML and broken pagination fail instead of silently returning a partial list', async () => {
    for (const body of ['<ListBucketResult>', '<Error/>',
        '<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>']) {
        globalThis.fetch = async () => new Response(body);
        assert.equal((await worker.fetch(request(), env)).status, 502);
    }
    let calls = 0;
    globalThis.fetch = async () => {
        calls++;
        return xml([], [], '<IsTruncated>true</IsTruncated><NextContinuationToken>same</NextContinuationToken>');
    };
    assert.equal((await worker.fetch(request(), env)).status, 502);
    assert.equal(calls, 2);
});
