# Cloudflare Worker for Backblaze B2

Access private Backblaze B2 buckets through a Cloudflare Worker. The Worker validates the request's `x-proxy-token` header, signs the upstream request, and retrieves the file from B2. It supports `GET` and `HEAD` requests.

Run `npm run deploy` to walk through Cloudflare authentication, bucket configuration, secret registration, and deployment. You do not normally need to edit `wrangler.toml` manually.

## Requirements

- Node.js 22 or later and npm
- Python 3.11 or later
- A Cloudflare account
- A Backblaze B2 bucket and an Application Key ID / Application Key with read access to that bucket
- The bucket's S3 endpoint, shown on the Backblaze bucket page

The script uses an existing B2 bucket. Create the bucket and Application Key before starting.

## Deploy interactively

```bash
git clone https://github.com/ajctrl/cloudflare-b2.git
cd cloudflare-b2
npm install
npm run deploy
```

No deployment flags are required. Authentication, account selection, endpoint
selection, and proxy token generation are handled by the interactive script.
Values in square brackets are defaults; press Enter to accept them. Secret
input is hidden and has no saved default.

Follow the English terminal prompts in this order.

1. **Cloudflare authentication:** An existing login is reused. If you are not authenticated, open the displayed URL in your browser and approve the device code. This also works from Codespaces or an SSH session.
2. **Account selection:** The Account ID is retrieved automatically. If multiple accounts are available, select one by number from the list of names.
3. **Worker name:** The default is `cloudflare-b2`.
4. **B2 bucket name:** Enter the bucket you want to use.
5. **B2 endpoint:** Select the numbered endpoint that matches your bucket's endpoint. Choose the final option to enter an unlisted endpoint manually.
6. **B2 Application Key ID:** Enter your key ID.
7. **Additional settings:** Set bucket listing and B2 backend URL conversion to `true` or `false`. Both default to `false`. For the rclone HTTP backend, enable bucket listing and leave URL conversion disabled; listing and downloading both work with URL conversion disabled. Enable URL conversion only when using the B2 backend's `--b2-download-url` option.
8. **B2 Application Key:** Enter the key at the hidden prompt.
9. **PROXY_TOKEN:** Choose automatic generation or manual input.

The proxy token menu offers these choices:

| Choice | Behavior |
| --- | --- |
| `1` or Enter | Generate a token using 256 bits of cryptographically secure randomness. Replace the existing token and display the new value after a successful deployment. |
| `2` | Enter a token at a hidden prompt. Use this to keep an existing token. |

Once input is complete, the script registers the B2 key and proxy token as Cloudflare Secrets and deploys them together with the Worker. Save the Worker URL printed by Wrangler and, if generated, the displayed `PROXY_TOKEN`.

Automatic token generation is selected at the prompt, not through a command-line
option. If deployment fails, the script does not display the generated token as
a successfully deployed credential.

## Accessing files

Include the `x-proxy-token` header when requesting a file from the deployed Worker:

```bash
curl -H 'x-proxy-token: YOUR_PROXY_TOKEN' \
  'https://YOUR_WORKER.YOUR_SUBDOMAIN.workers.dev/path/to/file.jpg' \
  -o file.jpg
```

If the configured token is missing, or the request token is missing or incorrect, the Worker returns `401 Unauthorized` with `Cache-Control: no-store`. The `x-proxy-token` header is never forwarded to B2. Authenticated requests using methods other than `GET` or `HEAD` receive `405`.

## Using the rclone HTTP backend

Set `ALLOW_LIST_BUCKET` to `true` in the deployment prompts, then redeploy.
Directory URLs return HTML links that rclone can traverse, including nested
directories and all pages of B2 results. Files retain their original responses.
Leave `RCLONE_DOWNLOAD` set to `false` for a standard HTTP remote; that option
is for B2's `--b2-download-url` path format.

Add a remote to your rclone configuration:

```ini
[b2proxy]
type = http
url = https://YOUR_WORKER.YOUR_SUBDOMAIN.workers.dev/
headers = x-proxy-token,YOUR_PROXY_TOKEN
```

```bash
rclone ls b2proxy:
rclone ls b2proxy:folder/
rclone copy b2proxy:folder/ ./downloads
```

Use a trailing `/` for directory paths. With `BUCKET_NAME=$path`, include the
bucket in the remote URL, such as `https://YOUR_WORKER.workers.dev/my-bucket/`.
Listing is still restricted by `x-proxy-token` and `ALLOW_LIST_BUCKET`.
Directory responses now use HTML instead of the raw S3 XML listing.

## Redeploying

```bash
npm run deploy
```

The script reuses your Cloudflare login and offers saved settings as prompt defaults. Press Enter to keep a setting. If multiple accounts are available, the previously selected account is the default choice.

Enter the B2 Application Key on each run. To keep your existing proxy token, choose **`2` (manual input)** and enter the same value. **Choosing `1` (automatic generation) changes the token, so you must also update your clients.**

## Configuration and secret storage

| File or location | Contents |
| --- | --- |
| `wrangler.toml` | Initial configuration defaults for the first run |
| `.wrangler-deploy.json` | Saved Worker name, Account ID, bucket, endpoint, key ID, and other settings; ignored by Git |
| Cloudflare Secrets | `B2_APPLICATION_KEY` and `PROXY_TOKEN` |
| `.dev.vars` | Local development secrets; create when needed; ignored by Git |

Subsequent runs read `.wrangler-deploy.json`. Changes to `wrangler.toml` are not imported automatically. Update ordinary settings, such as the bucket, through the prompts. Add advanced Wrangler settings directly to `.wrangler-deploy.json`.

During deployment, secrets are written to a temporary file with owner-only permissions. The file is removed on success, error, or Ctrl+C. Secrets are not written to the saved configuration file.

Use `npm run deploy` for subsequent deployments too. Running `npx wrangler deploy` without a configuration argument does not use `.wrangler-deploy.json`.

## Checking without publishing

| Command | Purpose |
| --- | --- |
| `npm run deploy` | Configure and deploy interactively |
| `npm run deploy -- --dry-run` | Save settings and validate the build without publishing |
| `npm run deploy -- --help` | Show supported command-line options |

```bash
npm run deploy -- --dry-run
```

This prompts for and saves configuration, then checks the build and packaging with Wrangler. It does not authenticate with Cloudflare, request or generate secrets, or publish the Worker. It does not verify B2 connectivity or live authentication.

Run the deployment script tests with:

```bash
python3 -B -m unittest discover -s scripts -p 'test_*.py'
```

## Local development

Create `.wrangler-deploy.json` first, for example by running `npm run deploy -- --dry-run`. Then create `.dev.vars` in the project root with:

```dotenv
B2_APPLICATION_KEY="your-b2-application-key"
PROXY_TOKEN="your-local-proxy-token"
```

Start the local server:

```bash
npx wrangler dev --config .wrangler-deploy.json
```

Secrets registered with Cloudflare are not automatically retrieved for local development. Local requests also need an `x-proxy-token` header matching the value in `.dev.vars`.

## Additional settings

### Bucket selection modes

| `BUCKET_NAME` | Behavior |
| --- | --- |
| A bucket name | Forward all requests to the specified bucket |
| `$path` | Use the first URL path segment as the bucket name, for example `/my-bucket/file.jpg` |
| `$host` | Use the first hostname segment as the bucket name, for example `my-bucket.example.com` |

For a standard `workers.dev` URL, specify a bucket name or use `$path`. For `$host`, configure routes or custom domains for the corresponding bucket hostnames separately.

### Restricting forwarded headers

To restrict forwarded headers, add an `ALLOWED_HEADERS` array under `vars` in `.wrangler-deploy.json`. Include `range` if you use Range requests.

The Worker filters out `x-proxy-token`, `cf-*`, `x-forwarded-proto`, `x-real-ip`, `accept-encoding`, and conditional request headers before forwarding requests.

### B2 backend URL conversion (normally disabled)

`RCLONE_DOWNLOAD` controls URL conversion for the B2 backend's `--b2-download-url` option, not whether rclone can download files. Leave it set to `false` for the HTTP backend. Set it to `true` only when using `--b2-download-url`, to strip the `file/` or `file/{bucket}/` prefix from download URLs. Requests from rclone must also send the `x-proxy-token` header. Enabling path conversion alone does not satisfy authentication.

### Range requests

If a successful B2 response lacks `content-range`, the Worker makes up to three attempts for the Range request. It sends `HEAD` requests upstream as `GET` and removes the response body before returning it to the client.

## Credits

- [backblaze-b2-samples/cloudflare-b2](https://github.com/backblaze-b2-samples/cloudflare-b2)
- [obezuk/worker-signed-s3-template](https://github.com/obezuk/worker-signed-s3-template)
