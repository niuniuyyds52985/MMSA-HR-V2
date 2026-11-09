"""Trusted local launcher and an independent Windows user-bound operator capability.

Only the CLI creates the capability. HTTP handlers never mint or return it.
Do not print the URL: its fragment grants access to the local workbench.
"""
from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlencode, urlsplit
import webbrowser

from interview_credentials import CredentialError, _dpapi

DEFAULT_FILE = Path(__file__).resolve().parent / '.secrets-user' / 'workbench-token.dpapi'


def token_path():
    return Path(os.environ.get('INTERVIEW_WORKBENCH_TOKEN_FILE', DEFAULT_FILE))


def load_operator_token():
    path = token_path()
    if not path.is_file():
        raise CredentialError('请从本机工作台启动器打开管理页面。')
    value = _dpapi(path.read_bytes(), decrypt=True).decode('ascii')
    if len(value) < 40:
        raise CredentialError('本机工作台凭据无效。')
    return value


def ensure_operator_token():
    path = token_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = _dpapi(secrets.token_urlsafe(48).encode('ascii'))
        try:
            with path.open('xb') as stream:
                stream.write(encrypted)
        except FileExistsError:
            pass
    return load_operator_token()


def operator_token_valid(value):
    try:
        return isinstance(value, str) and hmac.compare_digest(value, load_operator_token())
    except (CredentialError, OSError, UnicodeError, ValueError, TypeError):
        return False


def workbench_url(base_url, token, session_id=None):
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('工作台只支持本机 HTTP 服务地址。')
    fragment = {'access': token}
    if session_id:
        fragment['session'] = session_id
    return base_url.rstrip('/') + '/workbench#' + urlencode(fragment)


def main():
    parser = argparse.ArgumentParser(description='Open the trusted local interview workbench.')
    parser.add_argument('command', choices=('ensure', 'open'))
    parser.add_argument('--base-url', default='http://127.0.0.1:8766')
    parser.add_argument('--session')
    args = parser.parse_args()
    try:
        token = ensure_operator_token()
        if args.command == 'open':
            if not webbrowser.open(workbench_url(args.base_url, token, args.session)):
                raise RuntimeError('browser unavailable')
        print('Local workbench ready.' if args.command == 'ensure' else 'Local workbench opened.')
        return 0
    except Exception:
        print('Local workbench could not be opened by this Windows account; no credentials were printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
