"""管理员登录：密钥验证。

密钥来源：环境变量 INDUSTRY_BOOM_ADMIN_KEY 优先，其次 data/admin_secret.txt；
文件缺失时自动生成随机密钥写入（请勿提交到版本库）。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

SECRET_FILE = Path(__file__).resolve().parent / "data" / "admin_secret.txt"
ENV_KEY = "INDUSTRY_BOOM_ADMIN_KEY"


def load_admin_secret() -> str:
    """返回管理员密钥；环境变量优先，其次密钥文件（缺失时自动生成）。"""
    env_secret = os.environ.get(ENV_KEY, "").strip()
    if env_secret:
        return env_secret
    try:
        existing = SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    secret = secrets.token_hex(6)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_FILE.write_text(secret + "\n", encoding="utf-8")
    return secret


def verify_admin_key(key: str) -> bool:
    """常数时间比较密钥，防止计时攻击。"""
    return secrets.compare_digest(str(key).strip(), load_admin_secret())
