"""User-bound Windows DPAPI credentials. Never log secret values.

The optional environment credential remains supported. Stored credentials are
bound to an exact HTTPS API base URL and are decrypted only for an API call.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

DEFAULT_MODEL = 'qwen3.7-plus'
DEFAULT_FILE = Path(__file__).resolve().parent / '.secrets-user' / 'bailian.dpapi.json'


class CredentialError(Exception):
    pass


def credential_path():
    return Path(os.environ.get('INTERVIEW_CREDENTIALS_FILE', DEFAULT_FILE))


def _record():
    path = credential_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('format') != 'windows-dpapi-current-user.v1':
            raise ValueError()
        return value
    except (OSError, ValueError, AttributeError):
        raise CredentialError('本机加密凭据文件不可读取，请重新配置。') from None


def public_settings():
    record = _record()
    return {'provider': 'dashscope',
            'model': os.environ.get('INTERVIEW_LLM_MODEL') or record.get('model') or DEFAULT_MODEL,
            'base_url': (os.environ.get('INTERVIEW_LLM_BASE_URL') or record.get('base_url') or '').rstrip('/')}


def credential_available():
    return bool(os.environ.get('DASHSCOPE_API_KEY') or _record().get('encrypted_key'))


def cloud_inputs_allowed(answers):
    """Do not implicitly send answers recorded before this credential was enabled."""
    record = _record()
    if not record:
        return True  # Existing explicitly configured environment deployments.
    try:
        cutoff = datetime.fromisoformat(record['enabled_at'])
        return bool(answers) and all(datetime.fromisoformat(a['created_at']) >= cutoff for a in answers)
    except (KeyError, TypeError, ValueError):
        return False


def _dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise CredentialError('此加密凭据只能由原 Windows 账户读取。')
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buf)
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately NOT CRYPTPROTECT_LOCAL_MACHINE.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise CredentialError('Windows 账户凭据加解密失败，请在原账户重新配置。')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.memset(target.pbData, 0, target.cbData)
        kernel.LocalFree(target.pbData)
        ctypes.memset(buf, 0, len(data))


def get_api_key(base_url):
    environment_key = os.environ.get('DASHSCOPE_API_KEY')
    if environment_key:
        return environment_key
    record = _record()
    if not record.get('encrypted_key'):
        raise CredentialError('尚未配置模型密钥。')
    if base_url.rstrip('/') != record.get('base_url'):
        raise CredentialError('模型地址与密钥绑定地址不一致；拒绝发送凭据。')
    try:
        return _dpapi(base64.b64decode(record['encrypted_key'], validate=True), decrypt=True).decode('utf-8')
    except (ValueError, UnicodeError):
        raise CredentialError('加密凭据已损坏，请重新配置。') from None


def save_credential(key, base_url, model=DEFAULT_MODEL):
    base_url = base_url.rstrip('/')
    parsed = urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CredentialError('Base URL 必须是不含凭据或查询参数的 HTTPS 地址。')
    if not key or key != key.strip() or any(c.isspace() for c in key):
        raise CredentialError('密钥格式不合法。')
    path = credential_path()
    # The setup command creates and restricts this directory before writing.
    if not path.parent.is_dir():
        raise CredentialError('请先运行 configure-interview.ps1 创建受保护的凭据目录。')
    previous = _record()
    record = {'format': 'windows-dpapi-current-user.v1', 'base_url': base_url,
              'enabled_at': previous.get('enabled_at') or datetime.now(timezone.utc).isoformat(),
              'model': model, 'encrypted_key': base64.b64encode(_dpapi(key.encode('utf-8'))).decode('ascii')}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--stdin', action='store_true')
    args = parser.parse_args()
    try:
        key = sys.stdin.readline().rstrip('\r\n') if args.stdin else getpass.getpass('Bailian API Key (hidden): ')
        save_credential(key, args.base_url, args.model)
        del key
        print('Credential saved with Windows CurrentUser DPAPI encryption.')
    except Exception:
        print('Credential configuration failed; no secret values are printed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
