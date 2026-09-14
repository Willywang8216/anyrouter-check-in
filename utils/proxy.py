"""代理配置:读取环境变量并供浏览器 / HTTP 客户端使用。"""

from __future__ import annotations

import os
from urllib.parse import unquote


def get_proxy_server(*, use_proxy: bool = True) -> str | None:
	"""按平台配置读取 CHECKIN_PROXY_URL;use_proxy=False 时不返回代理地址。

	返回完整 URL(可含 user:pass@),httpx 能自行解析内嵌凭据。
	"""
	if not use_proxy:
		return None
	server = os.getenv('CHECKIN_PROXY_URL', '').strip()
	return server or None


def get_playwright_proxy(*, use_proxy: bool = True) -> dict[str, str] | None:
	"""返回 Playwright 可用的代理配置。

	Chromium 的 --proxy-server 不接受 URL 内嵌凭据(http://user:pass@host:port),
	若直接传入会拿这整个字符串当 host,导致代理回应 407 而报
	ERR_INVALID_AUTH_CREDENTIALS。因此把 user:pass 拆成 username/password。
	顺带避免密码被印进日志(browser.py 的 debug 输出只打 server)。
	"""
	server = get_proxy_server(use_proxy=use_proxy)
	if not server:
		return None
	if '@' not in server:
		return {'server': server}

	before, _, after = server.rpartition('@')
	if '://' in before:
		scheme, _, userinfo = before.partition('://')
		userinfo = unquote(userinfo)
	else:
		scheme, userinfo = '', before

	username, sep, password = userinfo.partition(':')
	if not sep:
		return {'server': server}

	hostport = f'{scheme}://{after}' if scheme else after
	config: dict[str, str] = {'server': hostport}
	if username:
		config['username'] = username
	if password:
		config['password'] = password
	return config