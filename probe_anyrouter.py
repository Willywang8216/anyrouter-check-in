#!/usr/bin/env python3
"""anyrouter.top LLM 上游可用性探測器（動態 model 清單）

每輪先 GET /v1/models 拿「當前存在的全部 model」，逐一發極小請求(max_tokens=1)測可用性。
claude* 走 anthropic /v1/messages（帶「Claude Code 偽裝」headers）；
其它（gpt/gemini/codex…）先試 openai /v1/chat/completions，回「不支持所选模型」再試 /v1/responses。

可用清單(HTTP 200)一變化就 Telegram 通知；全掛時安靜。

偽裝關鍵 header（Claude Code 自動帶、一般工具不會）：
  anthropic-beta: context-1m-2025-08-07
  anthropic-dangerous-direct-browser-access: true
  User-Agent: claude-cli/... (Claude Code)

env：ANYROUTER_API_KEY(必需)、TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID、FORCE_NOTIFY=1、ANYROUTER_BASE
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

# fallback：若 /v1/models 抓不到時的 model 清單
FALLBACK_MODELS = [
    'claude-opus-4-7', 'claude-opus-4-6', 'claude-opus-4-5-20251101',
    'claude-opus-4-20250514', 'claude-sonnet-4-5-20250929', 'claude-sonnet-4-20250514',
    'claude-fable-5-1', 'claude-opus-4-1-20250805', 'claude-haiku-4-5-20251001',
    'gpt-5-codex',
]

AN_HEADERS = {
    'Content-Type': 'application/json',
    'anthropic-version': '2023-06-01',
    'anthropic-beta': 'context-1m-2025-08-07',
    'anthropic-dangerous-direct-browser-access': 'true',
    'User-Agent': 'claude-cli/2.1.0 (Claude Code)',
}
OA_HEADERS = {
    'Content-Type': 'application/json',
    'User-Agent': 'codex_cli_rs/0.114.0',
}


def fetch_models(client, key):
    """動態取得站上全部 model id。"""
    try:
        r = client.get(f'{DOMAIN}/v1/models',
                       headers={'Authorization': f'Bearer {key}', 'User-Agent': 'curl/8.0'}, timeout=30)
        if r.status_code == 200:
            data = r.json().get('data', [])
            ids = []
            for m in data:
                if isinstance(m, dict):
                    ids.append(m.get('id') or m.get('name') or '')
                else:
                    ids.append(str(m))
            ids = sorted(set(i for i in ids if i))
            if ids:
                return ids
    except Exception as e:  # noqa: BLE001
        print(f'[WARN] fetch /v1/models failed: {str(e)[:80]}')
    print(f'[WARN] use fallback model list ({len(FALLBACK_MODELS)})')
    return FALLBACK_MODELS


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


def classify(code, body_text, model):
    if code == 200:
        return 'UP'
    if code == 429:
        return 'LIMIT'
    if code >= 500:
        return 'DOWN'
    if '已下线' in body_text:
        return 'OFFLINE'
    if '1m 上下文' in body_text or '启用 1m' in body_text or '啟用 1m' in body_text:
        return 'NEED-BETA'
    if '不支持所选模型' in body_text or code == 404:
        return 'N/A'
    return f'ERR{code}'


def try_anthropic(client, key, model):
    body = {'model': model, 'max_tokens': 1,
            'messages': [{'role': 'user', 'content': 'ping'}], 'stream': False}
    r = client.post(f'{DOMAIN}/v1/messages',
                    headers={**AN_HEADERS, 'Authorization': f'Bearer {key}'},
                    json=body, timeout=60)
    return r


def try_chat(client, key, model):
    body = {'model': model, 'max_tokens': 1, 'messages': [{'role': 'user', 'content': 'ping'}]}
    r = client.post(f'{DOMAIN}/v1/chat/completions',
                    headers={**OA_HEADERS, 'Authorization': f'Bearer {key}'},
                    json=body, timeout=60)
    return r


def try_responses(client, key, model):
    body = {'model': model, 'input': [{'role': 'user', 'content': 'ping'}], 'store': False}
    r = client.post(f'{DOMAIN}/v1/responses',
                    headers={**OA_HEADERS, 'Authorization': f'Bearer {key}'},
                    json=body, timeout=60)
    return r


def extract_msg(r):
    try:
        j = r.json()
        if isinstance(j, dict):
            err = j.get('error')
            if isinstance(err, dict):
                return err.get('message') or ''
            if isinstance(err, str):
                return err
            return j.get('message') or ''
    except Exception:  # noqa: BLE001
        pass
    return r.text[:100]


def check_one(client, key, model):
    """對單一 model 探測。claude→anthropic；其它→chat，404 才補 responses。回 (code,cls,note)。"""
    low = model.lower()
    try:
        if 'claude' in low:
            r = try_anthropic(client, key, model)
            note = extract_msg(r)
            return r.status_code, classify(r.status_code, note, model), note[:70]
        # 非 claude：先 chat/completions
        r = try_chat(client, key, model)
        note = extract_msg(r)
        code = r.status_code
        if code == 200:
            return 200, 'UP', ''
        # 若 404「不支持」(例如 codex model 需 responses)，補試 responses
        if code == 404 and ('不支持所选模型' in note or 'responses' in note.lower() or 'codex' in low):
            r2 = try_responses(client, key, model)
            note2 = extract_msg(r2)
            if r2.status_code == 200:
                return 200, 'UP', ''
            return r2.status_code, classify(r2.status_code, note2, model), note2[:70]
        return code, classify(code, note, model), note[:70]
    except Exception as e:  # noqa: BLE001
        return 0, 'ERR', str(e)[:70]


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

    client = httpx.Client(http2=True, timeout=60)
    try:
        models = fetch_models(client, key)
        print(f'[INFO] probing {len(models)} models')
        results = {}
        available = []
        for m in models:
            code, cls, note = check_one(client, key, m)
            results[m] = {'code': code, 'cls': cls}
            print(f'[{m}] {cls} (HTTP {code}) {note[:50]}')
            if cls == 'UP':
                available.append(m)
            time.sleep(0.35)
    finally:
        client.close()

    state = load_state()
    prev_available = set(state.get('available', []))
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    changed = set(available) != prev_available
    save_state({'available': available, 'all': results, 'ts': now})

    if not changed and not force:
        print(f'[INFO] no change (UP={len(available)}), skip notify')
        return

    lines = []
    for m in models:
        r = results[m]
        sym = SYM.get(r['cls'], '❓')
        lines.append(f'{sym} {m}')
    if not available:
        lines.append('')
        lines.append('全部無可用上游，等站方補貨中')

    text = '\n'.join(lines) + f'\n\n可用 {len(available)}/{len(models)}'
    if 'claude-opus-4-7' not in models:
        text += '\n(站上無 claude-opus-4-8；4-6 已被下線)'
    title = f'anyrouter 上游探測 {now}'
    ok = tg_send(bot, chat, title, text) if (bot and chat) else False
    print(f'[DONE] UP={available} changed={changed} tg={ok if (bot and chat) else "no-env"}')


if __name__ == '__main__':
    main()
