"""免费英译中，优先使用 Edge 翻译并回退到 Bing 网页翻译。

流程：GET edge.microsoft.com/translate/auth 拿短期 JWT（约 10 分钟有效，
本地缓存 8 分钟），再调官方 translate 接口批量翻译。认证通道不可用时，
改用 Bing 翻译页面提供的短期 token。
任何失败都返回原文，绝不向上抛异常——翻译只是锦上添花，不能拖垮查询。
"""

import json
import re
import time

import aiohttp

from astrbot.api import logger

_AUTH_URL = "https://edge.microsoft.com/translate/auth"
_API_URL = (
    "https://api.cognitive.microsofttranslator.com/translate"
    "?api-version=3.0&to=zh-Hans"
)
_TOKEN_TTL = 8 * 60
_BING_PAGE_URL = "https://www.bing.com/translator"
_BING_API_PATH = "/ttranslatev3"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36 Edg/151.0.0.0"
)


class Translator:
    def __init__(self, timeout: int = 30):
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

    @staticmethod
    def _parse_bing_config(page: str) -> tuple[str, str, str, str]:
        ig = re.search(r'IG:"([^"]+)"', page)
        iid = re.search(r'data-iid="([^"]+)"', page)
        raw = re.search(
            r"params_AbusePreventionHelper\s*=\s*(\[[^\]]+\])", page
        )
        if not ig or not iid or not raw:
            raise ValueError("Bing 翻译页面缺少认证参数")
        key, token, _expires = json.loads(raw.group(1))
        return ig.group(1), iid.group(1), str(key), str(token)

    async def _translate_with_bing(
        self, session: aiohttp.ClientSession, texts: list[str]
    ) -> list[str] | None:
        async with session.get(
            _BING_PAGE_URL, headers={"User-Agent": _USER_AGENT}
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[hltv] Bing 翻译页面返回 {resp.status}")
                return None
            page = await resp.text()
            host = str(getattr(resp.url, "host", "") or "")
            origin = (
                f"{resp.url.scheme}://{host}"
                if host == "bing.com" or host.endswith(".bing.com")
                else "https://www.bing.com"
            )
        ig, iid, key, token = self._parse_bing_config(page)
        headers = {"User-Agent": _USER_AGENT, "Referer": f"{origin}/translator"}
        output = []
        for request_index, original in enumerate(texts, start=1):
            if not original.strip():
                output.append(original)
                continue
            async with session.post(
                f"{origin}{_BING_API_PATH}",
                params={
                    "isVertical": "1",
                    "IG": ig,
                    "IID": iid,
                    "SFX": str(request_index),
                    "ref": "TThis",
                    "edgepdftranslator": "1",
                },
                data={
                    "fromLang": "auto-detect",
                    "to": "zh-Hans",
                    "text": original,
                    "token": token,
                    "key": key,
                },
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[hltv] Bing 翻译接口返回 {resp.status}")
                    return None
                data = await resp.json(content_type=None)
            try:
                output.append(data[0]["translations"][0]["text"])
            except Exception:
                output.append(original)
        return output

    async def translate(self, texts: list[str]) -> list[str]:
        """批量英译中。失败（网络/配额/任意异常）时原样返回输入。"""
        texts = [str(t) for t in texts]
        if not any(t.strip() for t in texts):
            return texts
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                token = await self._get_token(session)
                if token:
                    try:
                        async with session.post(
                            _API_URL,
                            json=[{"Text": text} for text in texts],
                            headers={"Authorization": f"Bearer {token}"},
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                output = []
                                for index, original in enumerate(texts):
                                    try:
                                        output.append(
                                            data[index]["translations"][0]["text"]
                                        )
                                    except Exception:
                                        output.append(original)
                                return output
                            logger.warning(f"[hltv] 翻译接口返回 {resp.status}")
                    except Exception as e:
                        logger.warning(f"[hltv] Edge 翻译失败，尝试 Bing: {e!r}")
                try:
                    result = await self._translate_with_bing(session, texts)
                except Exception as e:
                    logger.warning(f"[hltv] Bing 翻译失败，使用原文: {e!r}")
                return result if result is not None else texts
        except Exception as e:
            logger.warning(f"[hltv] 翻译会话失败，使用原文: {e!r}")
            return texts
