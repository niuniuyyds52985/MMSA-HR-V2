"""Fixed, verified cloud configuration for the local V2 delivery (stdlib only).

Status inspection never decrypts a stored key or contacts a model provider.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


TEXT_MODEL = "qwen3.7-plus"
ASR_MODEL = "qwen3-asr-flash"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
HOST_REGIONS = {"dashscope.aliyuncs.com": "cn-beijing", "dashscope-intl.aliyuncs.com": "ap-southeast-1"}


class CloudPolicyError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def validate_endpoint(value):
    """Return the exact configured HTTPS base, without its trailing slash."""
    try:
        if not isinstance(value, str) or value != value.strip() or any(c.isspace() for c in value):
            raise ValueError()
        base = value.rstrip("/")
        parsed = urlsplit(base)
        workspace = re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com", parsed.hostname or "")
        if (parsed.scheme != "https" or (parsed.hostname not in HOST_REGIONS and not workspace)
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/compatible-mode/v1"):
            raise ValueError()
        return base
    except (ValueError, TypeError, AttributeError):
        raise CloudPolicyError("unsupported_endpoint", "仅支持百炼北京或新加坡官方 HTTPS 兼容接口；请用本机配置入口重新设置。") from None


def validate_text_settings(settings, *, environ=None):
    environment = os.environ if environ is None else environ
    if (environment.get("INTERVIEW_LLM_MODEL") not in (None, "", TEXT_MODEL)
            or settings.get("model") != TEXT_MODEL):
        raise CloudPolicyError("unsupported_model", "本版本文本评分固定使用 qwen3.7-plus；当前模型配置不匹配，未发送请求。")
    return {"model": TEXT_MODEL, "base_url": validate_endpoint(settings.get("base_url"))}


def endpoint_region(base):
    host = urlsplit(validate_endpoint(base)).hostname
    return HOST_REGIONS.get(host) or ("cn-beijing" if ".cn-beijing." in host else "ap-southeast-1")


def configuration_status():
    """Safe metadata for the setup UI. Presence is not a successful API test."""
    legacy = Path(__file__).resolve().parents[2] / "suit-demo"
    if str(legacy) not in sys.path:
        sys.path.insert(0, str(legacy))
    from interview_credentials import public_settings, credential_available, CredentialError
    result = {"configured": False, "credential_present": False, "policy_compliant": False,
              "api_verified": False, "text_model": TEXT_MODEL, "asr_model": ASR_MODEL,
              "endpoint_region": None, "error": None,
              "environment_overrides": any(os.environ.get(k) for k in
                  ("INTERVIEW_LLM_MODEL", "INTERVIEW_LLM_BASE_URL", "DASHSCOPE_API_KEY", "INTERVIEW_CREDENTIALS_FILE"))}
    try:
        settings = public_settings()
        result["credential_present"] = credential_available()
        allowed = validate_text_settings(settings)
        result.update(policy_compliant=True, endpoint_region=endpoint_region(allowed["base_url"]))
        result["configured"] = bool(result["credential_present"])
    except CloudPolicyError as exc:
        result["error"] = exc.code
    except (CredentialError, OSError, ValueError, TypeError):
        result["error"] = "configuration_unavailable"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", required=True)
    parser.parse_args(argv)
    print(json.dumps(configuration_status(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
