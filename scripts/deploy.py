#!/usr/bin/env python3
"""Interactive Cloudflare deployment (Python 3.11+, Node.js 22+)."""
import argparse
import csv
import getpass
import io
import json
import os
from pathlib import Path
import re
import secrets as secure_secrets
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / '.wrangler-deploy.json'
WRANGLER = ROOT / 'node_modules/wrangler/bin/wrangler.js'

# Common B2 clusters; retain manual entry for new or unlisted endpoints.
B2_REGIONS = (
    ('us-west-001', 'US West'),
    ('us-west-002', 'US West'),
    ('us-west-004', 'US West'),
    ('us-east-005', 'US East'),
    ('eu-central-003', 'Europe Central'),
    ('ca-east-006', 'Canada East'),
)


def ask(label, default='', valid=lambda value: bool(value)):
    default = str(default)
    if default.startswith('<'):
        default = ''
    while True:
        value = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip() or default
        if valid(value):
            return value
        print('Invalid value. Please try again.')


def secret(label):
    while True:
        value = getpass.getpass(f'{label} (hidden): ')
        if value and not any(c in value for c in '\r\n\x00'):
            return value
        print('Enter a non-empty value without line breaks or null characters.')


def select_endpoint(saved=''):
    endpoints = [f's3.{region}.backblazeb2.com' for region, _ in B2_REGIONS]
    valid = lambda v: bool(re.fullmatch(r's3\.[a-z0-9-]+\.backblazeb2\.com', v))
    print('Select the endpoint that matches your B2 bucket.')
    for number, (endpoint, (_, label)) in enumerate(zip(endpoints, B2_REGIONS), 1):
        print(f'  {number}. {endpoint} ({label})')
    if saved and valid(saved) and saved not in endpoints:
        endpoints.append(saved)
        print(f'  {len(endpoints)}. {saved} (saved)')
    manual = str(len(endpoints) + 1)
    print(f'  {manual}. Other (enter manually)')
    default = str(endpoints.index(saved) + 1) if saved in endpoints else ''
    choice = ask('Endpoint number', default,
                 lambda v: v.isascii() and v.isdigit() and 1 <= int(v) <= len(endpoints) + 1)
    if int(choice) == int(manual):
        return ask('B2 S3 endpoint (hostname only)', saved if valid(saved) else '', valid)
    return endpoints[int(choice) - 1]


def run(*args, capture_output=False):
    if capture_output:
        output = []
        with subprocess.Popen(['node', str(WRANGLER), *args], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in process.stdout:
                print(line, end='', flush=True)
                output.append(line)
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, process.args)
        return ''.join(output)
    subprocess.run(['node', str(WRANGLER), *args], cwd=ROOT, check=True)


def print_rclone_config(output, variables, token):
    # Wrangler prints the deployed URL on its own line. Strip terminal colors first.
    plain = re.sub(r'\x1b\[[0-9;]*m', '', output or '')
    urls = re.findall(r'^\s*(https://[a-zA-Z0-9.-]+(?:/[^\s]*)?)\s*$', plain, re.MULTILINE)
    url = urls[-1].rstrip('/') + '/' if urls else 'https://YOUR_WORKER_HOST/'
    if variables['BUCKET_NAME'] == '$path':
        url += 'YOUR_BUCKET_NAME/'
    elif variables['BUCKET_NAME'] == '$host':
        url = 'https://YOUR_BUCKET_NAME.YOUR_CUSTOM_DOMAIN/'
    headers = io.StringIO()
    csv.writer(headers, lineterminator='').writerow(['x-proxy-token', token])
    print('\nAdd this section to your rclone config file (find it with: rclone config file):')
    if not urls or variables['BUCKET_NAME'] in ('$path', '$host'):
        print('Replace the uppercase URL placeholders with your deployed host and bucket name.')
    if variables.get('ALLOW_LIST_BUCKET') != 'true':
        print('To use rclone ls, redeploy with Allow bucket listing set to true.')
    if variables.get('RCLONE_DOWNLOAD') == 'true':
        print('For this HTTP remote, redeploy with B2 backend URL conversion set to false.')
    print('\n[b2proxy]\ntype = http')
    print(f'url = {url}')
    print(f'headers = {headers.getvalue()}')
    print('\nList files: rclone ls b2proxy:')


def get_identity():
    result = subprocess.run(
        ['node', str(WRANGLER), 'whoami', '--json'],
        cwd=ROOT, check=False, stdout=subprocess.PIPE, text=True,
    )
    try:
        identity = json.loads(result.stdout)
    except ValueError:
        raise SystemExit('Could not check Cloudflare authentication status. Review the Wrangler error above.')
    if not isinstance(identity, dict):
        raise SystemExit('Could not read Cloudflare authentication information.')
    if identity.get('loggedIn') is False:
        return None
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args)
    if identity.get('loggedIn') is not True:
        raise SystemExit('Could not read Cloudflare authentication information.')
    return identity


def authenticated_identity():
    identity = get_identity()
    if identity is None:
        print('Open the Cloudflare authentication URL and approve the displayed code.', flush=True)
        run('login', '--device', '--browser=false')
        identity = get_identity()
        if identity is None:
            raise SystemExit('Cloudflare authentication is incomplete.')
    else:
        print('Using your existing Cloudflare login.')
    return identity


def select_account(identity, saved_id=None):
    try:
        accounts = identity['accounts']
        if not isinstance(accounts, list) or any(
            not isinstance(account, dict)
            or not re.fullmatch(r'[a-fA-F0-9]{32}', str(account.get('id', '')))
            for account in accounts
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise SystemExit('Could not read Cloudflare account information.')
    if not accounts:
        raise SystemExit('No Cloudflare accounts are available. Check your account permissions.')
    if len(accounts) == 1:
        account = accounts[0]
    else:
        print('Select the Cloudflare account to deploy to.')
        for number, account in enumerate(accounts, 1):
            print(f"  {number}. {account.get('name', account['id'])} ({account['id']})")
        default = next((str(i) for i, account in enumerate(accounts, 1) if account['id'] == saved_id), '')
        choice = ask('Account number', default,
                     lambda v: v.isascii() and v.isdigit() and 1 <= int(v) <= len(accounts))
        account = accounts[int(choice) - 1]
    print(f"Cloudflare account: {account.get('name', account['id'])} ({account['id']})")
    return account['id']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Validate the build without authentication or deployment')
    args = parser.parse_args()
    if not WRANGLER.exists():
        raise SystemExit('Run npm install first.')
    if not os.isatty(0):
        raise SystemExit('Run npm run deploy in an interactive terminal.')
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else tomllib.loads((ROOT / 'wrangler.toml').read_text())
    if not args.dry_run:
        config['account_id'] = select_account(authenticated_identity(), config.get('account_id'))
    config['name'] = ask('Worker name', config.get('name', 'cloudflare-b2'),
                         lambda v: bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', v)))
    variables = config.setdefault('vars', {})
    variables['BUCKET_NAME'] = ask('B2 bucket name ($path / $host also supported)', variables.get('BUCKET_NAME', ''),
                                   lambda v: v in ('$path', '$host') or bool(re.fullmatch(r'[A-Za-z0-9-]{6,63}', v)))
    variables['B2_ENDPOINT'] = select_endpoint(variables.get('B2_ENDPOINT', ''))
    variables['B2_APPLICATION_KEY_ID'] = ask('B2 Application Key ID', variables.get('B2_APPLICATION_KEY_ID', ''))
    for key, label in [('ALLOW_LIST_BUCKET', 'Allow bucket listing'), ('RCLONE_DOWNLOAD', 'B2 backend URL conversion (normally disabled)')]:
        if key == 'RCLONE_DOWNLOAD':
            print('For the rclone HTTP backend, select false. Listing and downloading work with this disabled.')
            print('Select true only when using the B2 backend with --b2-download-url.')
        default = variables.get(key, 'false')
        variables[key] = ask(f'{label} (true / false)', default if default in ('true', 'false') else 'false',
                             lambda v: v in ('true', 'false'))
    variables.pop('B2_APPLICATION_KEY', None)
    variables.pop('PROXY_TOKEN', None)
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n')
    print(f'Settings saved to {CONFIG.name}. Press Enter at each prompt next time to reuse them.')
    if args.dry_run:
        run('deploy', '--config', str(CONFIG), '--dry-run')
        return

    b2_key = secret('B2 Application Key')
    print('Choose how to set PROXY_TOKEN.')
    print('  1. Generate automatically (replaces the existing token; displayed after successful deployment)')
    print('  2. Enter manually (choose this to keep your existing token)')
    generate_proxy_token = ask('Token setup method', '1', lambda v: v in ('1', '2')) == '1'
    secrets = {'B2_APPLICATION_KEY': b2_key,
               'PROXY_TOKEN': secure_secrets.token_urlsafe(32) if generate_proxy_token
               else secret('PROXY_TOKEN (token used to access the Worker)')}
    print(f"Deploying to: {config['account_id']} / {config['name']}", flush=True)
    # Private temporary file, removed on success, failure, or Ctrl+C.
    with tempfile.TemporaryDirectory(prefix='cloudflare-b2-') as directory:
        fd, path = tempfile.mkstemp(suffix='.json', dir=directory)
        with os.fdopen(fd, 'w') as stream:
            json.dump(secrets, stream)
        output = run('deploy', '--config', str(CONFIG), '--secrets-file', path, capture_output=True)
    print('Deployment complete. Include the x-proxy-token header when accessing the Worker.')
    if generate_proxy_token:
        print(f"PROXY_TOKEN: {secrets['PROXY_TOKEN']}")
    print_rclone_config(output, variables, secrets['PROXY_TOKEN'])


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit('\nCancelled.')
    except subprocess.CalledProcessError as error:
        raise SystemExit(f'Wrangler failed (exit code {error.returncode}).')
