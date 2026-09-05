#!/usr/bin/env python3
"""anyrouter.top Claude 上游可用性探測器

用「Claude Code 偽裝」headers 對候選 claude model 發極小請求(max_tokens=1)，
判斷哪些現在真的能用(HTTP 200)。上游時好時壞(全 503 常見)，這支定期探測，
可用清單一變化就 Telegram 通知，讓你知道「現在可以走哪個 model」。

偽裝關鍵 header（即 Claude Code 自動帶、一般 API 工具不會帶的）：
  anthropic-beta: context-1m-2025-08-07        ← anyrouter 檢查「啟用 1m 上下文」
  anthropic-dangerous-direct-browser-access: true
  User-Agent: claude-cli/... (Claude Code)

env：
  ANYROUTER_API_KEY  必需（anyrouter 帳號 sk- token）
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  通知用（沒設則不發）
  FORCE_NOTIFY=1     強制發通知（測試用）
  ANYROUTER_BASE     預設 https://anyrouter.top
"""
import json
import os
import sys
import time
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True, encoding='utf-8', errors='replace')

DOMAIN = os.environ.get('ANYROUTER_BASE', 'https://anyrouter.top').rstrip('/')
STATE_FILE = 'probe_state.json'

# (model id, kind)  kind: an=anthropic messages / oa=openai chat
MODELS = [
    ('claude-opus-4-7', 'an'),
    ('claude-opus-4-6', 'an'),
    ('claude-opus-4-5-20251101', 'an'),
    ('claude-opus-4-20250514', 'an'),
    ('claude-sonnet-4-5-20250929', 'an'),
    ('claude-sonnet-4-20250514', 'an'),
    ('claude-fable-5-1', 'an'),
    ('claude-opus-4-1-20250805', 'an'),
    ('claude-haiku-4-5-20251001', 'an'),
    ('gpt-5-codex', 'oa'),
]

AN_HEADERS = {
    'Content-Type': 'application/json',
    'anthropic-version': '2023-06-01',
    'anthropic-beta': 'context-1m-2025-08-07',
    'anthropic-dangerous-direct-browser-access': 'true',
    'User-Agent': 'claude-cli/2.1.0 (Claude Code)',
}


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        print(f'[WARN] save state failed: {e}')


def classify(model, code, body_text):
    if code == 200:
        return 'UP'
    if code == 429:
        return 'LIMIT'
    if code >= 500:
        return 'DOWN'
    # 4xx：看訊息歸類
    if '已下线' in body_text:
        return 'OFFLINE'
    if '1m 上下文' in body_text or '启用 1m' in body_text or '啟用 1m' in body_text:
        return 'NEED-BETA'
    if '不支持所选模型' in body_text or code == 404:
        return 'N/A'
    return f'ERR{code}'


def check_one(client, key, model, kind):
    try:
        if kind == 'an':
            body = {'model': model, 'max_tokens': 1,
                    'messages': [{'role': 'user', 'content': 'ping'}], 'stream': False}
            r = client.post(f'{DOMAIN}/v1/messages', headers={**AN_HEADERS, 'Authorization': f'Bearer {key}'},
                            json=body, timeout=60)
        else:
            body = {'model': model, 'max_tokens': 1,
                    'messages': [{'role': 'user', 'content': 'ping'}]}
            h = {'Content-Type': 'application/json', 'Authorization': f'Bearer {key}',
                 'User-Agent': 'codex_cli_rs/0.114.0'}
            r = client.post(f'{DOMAIN}/v1/chat/completions', headers=h, json=body, timeout=60)
        code = r.status_code
        try:
            j = r.json()
            msg = ''
            if isinstance(j, dict):
                err = j.get('error')
                if isinstance(err, dict):
                    msg = err.get('message') or ''
                elif isinstance(err, str):
                    msg = err
                else:
                    msg = j.get('message') or ''
            body_text = (msg or r.text)
        except Exception:  # noqa: BLE001
            body_text = r.text[:100]
        return code, classify(model, code, body_text), body_text[:80]
    except Exception as e:  # noqa: BLE001
        return 0, 'ERR', str(e)[:80]


def tg_send(bot, chat, title, text):
    try:
        r = httpx.post(f'https://api.telegram.org/bot{bot}/sendMessage',
                       json={'chat_id': chat, 'text': f'<b>{title}</b>\n\n{text}',
                             'parse_mode': 'HTML'}, timeout=25)
        print(f'[Telegram] HTTP {r.status_code}')
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f'[Telegram] send failed: {e}')
        return False


SYM = {
    'UP': '✅', 'LIMIT': '⚠️', 'DOWN': '❌', 'OFFLINE': '⛔',
    'NEED-BETA': '🧪', 'N/A': '🚫', 'ERR': '❓',
}


def main():
    key = os.environ.get('ANYROUTER_API_KEY', '').strip()
    if not key:
        print('[FAIL] ANYROUTER_API_KEY not set')
        sys.exit(1)
    bot = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    force = os.environ.get('FORCE_NOTIFY', '').lower() in ('1', 'true', 'yes')

    state = load_state()
    prev_available = set(state.get('available', []))

    client = httpx.Client(http2=True, timeout=60)
    results = {}
    available = []
    try:
        for model, kind in MODELS:
            code, cls, note = check_one(client, key, model, kind)
            results[model] = {'code': code, 'cls': cls}
            print(f'[{model}] {cls} (HTTP {code}) {note[:60]}')
            if cls == 'UP':
                available.append(model)
            time.sleep(0.4)
    finally:
        client.close()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    changed = set(available) != prev_available
    save_state({'available': available, 'all': results, 'ts': now})

    if not changed and not force:
        print(f'[INFO] no change (UP={len(available)}), skip notify')
        if not available:
            print('[INFO] all models down, quiet')
        return

    lines = []
    for model, kind in MODELS:
        r = results[model]
        sym = SYM.get(r['cls'], '❓')
        extra = ' (站方已下線，叫你用 4-7)' if model == 'claude-opus-4-6' else ''
        lines.append(f'{sym} {model}{extra}')
    if not available:
        lines.append('')
        lines.append('全部 503 / 無可用上游，等站方補貨中')

    status_line = f'可用 {len(available)}/{len(MODELS)}'
    note = ('\n(站上無 claude-opus-4-8，最高 opus-4-7；4-6 已被站方下線)'
            if 'claude-opus-4-7' in available else '')
    text = '\n'.join(lines) + '\n\n' + status_line + note
    title = f'anyrouter 上游探測 {now}'
    ok = tg_send(bot, chat, title, text) if (bot and chat) else False
    if bot and chat and not ok:
        print('[WARN] telegram send failed')
    print(f'[DONE] UP={available} changed={changed} tg={ok if (bot and chat) else "no-env"}')


if __name__ == '__main__':
    main()
