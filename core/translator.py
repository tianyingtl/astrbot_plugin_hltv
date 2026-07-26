"""微软翻译（Edge 免费认证通道）。

流程：GET edge.microsoft.com/translate/auth 拿短期 JWT（约 10 分钟有效，
本地缓存 8 分钟），再调官方 translate 接口批量翻译。
任何失败都返回原文，绝不向上抛异常——翻译只是锦上添花，不能拖垮查询。
"""

import time

import aiohttp

from astrbot.api import logger

_AUTH_URL = "https://edge.microsoft.com/translate/auth"
_API_URL = (
    "https://api.cognitive.microsofttranslator.com/translate"
    "?api-version=3.0&to=zh-Hans"
)
_TOKEN_TTL = 8 * 60


class Translator:
    def __init__(self, timeout: int = 10):
        self._timeout = timeout
        self._token: str | None = None
        self._token_ts = 0.0

    async def _get_token(self, session: aiohttp.ClientSession) -> str | None:
        if self._token and time.monotonic() - self._token_ts < _TOKEN_TTL:
            return self._token
        try:
            async with session.get(_AUTH_URL) as resp:
                if resp.status == 200:
                    self._token = (await resp.text()).strip()
                    self._token_ts = time.monotonic()
                    return self._token
                logger.warning(f"[hltv] 翻译认证接口返回 {resp.status}")
        except Exception as e:
            logger.warning(f"[hltv] 获取翻译 token 失败: {e!r}")
        return None

    async def translate(self, texts: list[str]) -> list[str]:
        """批量英译中。失败（网络/配额/任意异常）时原样返回输入。"""
        texts = [str(t) for t in texts]
        if not any(t.strip() for t in texts):
            return texts
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                token = await self._get_token(session)
                if not token:
                    return texts
                async with session.post(
                    _API_URL,
                    json=[{"Text": t} for t in texts],
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[hltv] 翻译接口返回 {resp.status}")
                        return texts
                    data = await resp.json()
        except Exception as e:
            logger.warning(f"[hltv] 翻译失败，使用原文: {e!r}")
            return texts
        out: list[str] = []
        for i, original in enumerate(texts):
            try:
                out.append(data[i]["translations"][0]["text"])
            except Exception:
                out.append(original)
        return out
