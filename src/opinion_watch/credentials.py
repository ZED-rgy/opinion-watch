from __future__ import annotations

from contextlib import suppress

import keyring


class CredentialStore:
    """使用操作系统凭据管理器保存应用 Secret。"""

    SERVICE = "opinion-watch"
    WECOM_SECRET_USERNAME = "wecom-bot-secret"
    LLM_API_KEY_USERNAME = "llm-api-key"

    @classmethod
    def get_wecom_secret(cls) -> str:
        return keyring.get_password(cls.SERVICE, cls.WECOM_SECRET_USERNAME) or ""

    @classmethod
    def set_wecom_secret(cls, secret: str) -> None:
        clean_secret = secret.strip()
        if not clean_secret:
            raise ValueError("企微机器人 Secret 不能为空")
        keyring.set_password(cls.SERVICE, cls.WECOM_SECRET_USERNAME, clean_secret)

    @classmethod
    def delete_wecom_secret(cls) -> None:
        with suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(cls.SERVICE, cls.WECOM_SECRET_USERNAME)

    @classmethod
    def get_llm_api_key(cls) -> str:
        return keyring.get_password(cls.SERVICE, cls.LLM_API_KEY_USERNAME) or ""

    @classmethod
    def set_llm_api_key(cls, api_key: str) -> None:
        clean_api_key = api_key.strip()
        if not clean_api_key:
            raise ValueError("大模型 API Key 不能为空")
        keyring.set_password(cls.SERVICE, cls.LLM_API_KEY_USERNAME, clean_api_key)

    @classmethod
    def delete_llm_api_key(cls) -> None:
        with suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(cls.SERVICE, cls.LLM_API_KEY_USERNAME)
