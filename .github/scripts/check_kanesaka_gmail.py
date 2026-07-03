"""
check_kanesaka_gmail.py
kanesaka.agni@gmail.com（重要ドメインのみ）と agniyoga.ad@gmail.com（フィルタなし・全件）の
未読メールを取得 → mail_kanesaka_gmail.json / mail_agniyoga_ad_gmail.json に保存
Task Scheduler から10分ごとに呼び出す。
"""
import sys, json, os, re, base64, html
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

CI_MODE = os.environ.get('GITHUB_ACTIONS') == 'true'

if CI_MODE:
    # GitHub Actions: 認証情報はSecrets経由の環境変数から直接取得（keystore不使用）
    CLIENT_ID     = os.environ['GMAIL_CLIENT_ID']
    CLIENT_SECRET = os.environ['GMAIL_CLIENT_SECRET']
    KANESAKA_REFRESH_TOKEN  = os.environ['KANESAKA_GMAIL_REFRESH_TOKEN']
    AGNIYOGA_AD_REFRESH_TOKEN = os.environ.get('AGNIYOGA_AD_GMAIL_REFRESH_TOKEN', '')
    INPUT_DIR = os.environ.get('CI_INPUT_DIR', os.path.join(os.environ.get('GITHUB_WORKSPACE', '.'), 'ci_input'))
else:
    sys.path.insert(0, 'Z:/claude/shared/apikeys')
    from keystore import load as _ks_load
    _secrets = _ks_load(os.environ.get('KEYSTORE_PASSWORD'))
    CLIENT_ID     = _secrets['GMAIL_CLIENT_ID']
    CLIENT_SECRET = _secrets['GMAIL_CLIENT_SECRET']
    KANESAKA_REFRESH_TOKEN    = _secrets['KANESAKA_GMAIL_REFRESH_TOKEN']
    AGNIYOGA_AD_REFRESH_TOKEN = _secrets.get('GMAIL_REFRESH_TOKEN', '')
    INPUT_DIR = os.path.join(os.path.dirname(__file__))

import urllib.request, urllib.parse

os.makedirs(INPUT_DIR, exist_ok=True)

# 重要ドメイン（kanesaka.agni@gmail.com はこれ以外表示しない）
IMPORTANT_DOMAINS = {
    'mamail.jp',          # 幕張総合Net
    'chiba-c.ed.jp',      # 千葉県立高校（サッカー部顧問）
    'mail.rakuten-bank.co.jp',   # 楽天銀行
    'cardservice.co.jp',  # ZEUS決済
    'bizcomfort.jp',      # BIZcomfort
    'noreply@bizcomfort.jp',
    '0101.co.jp',         # 丸井
    'timerex.net',        # TimeRex（打ち合わせ）
    'github.com',         # GitHub
}

ACCOUNTS = [
    {
        'address': 'kanesaka.agni@gmail.com',
        'refresh_token': KANESAKA_REFRESH_TOKEN,
        'domain_filter': IMPORTANT_DOMAINS,  # Noneなら無フィルタ
        'out_file': os.path.join(INPUT_DIR, 'mail_kanesaka_gmail.json'),
    },
    {
        'address': 'agniyoga.ad@gmail.com',
        'refresh_token': AGNIYOGA_AD_REFRESH_TOKEN,
        'domain_filter': None,  # 2026-07-04時点：ユーザー希望によりフィルタなし・全件表示
        'out_file': os.path.join(INPUT_DIR, 'mail_agniyoga_ad_gmail.json'),
    },
]


def get_access_token(refresh_token):
    data = urllib.parse.urlencode({
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token'
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())['access_token']


def gmail_api(token, path):
    req = urllib.request.Request(
        f'https://gmail.googleapis.com/gmail/v1/users/me/{path}',
        headers={'Authorization': f'Bearer {token}'}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def extract_domain(from_str):
    m = re.search(r'<(.+?)>', from_str)
    addr = m.group(1) if m else from_str.strip()
    return addr.split('@')[-1].lower() if '@' in addr else ''


def _decode_part(data):
    if not data:
        return ''
    padded = data.replace('-', '+').replace('_', '/')
    padded += '=' * (-len(padded) % 4)
    try:
        return base64.b64decode(padded).decode('utf-8', errors='replace')
    except Exception:
        return ''


def _strip_html(s):
    s = re.sub(r'(?is)<(script|style).*?</\1>', '', s)
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = re.sub(r'(?i)</p>', '\n\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def extract_body(payload, max_len=3000):
    """Gmail APIのfull payloadから本文を抽出。text/plain優先、なければtext/htmlをタグ除去。"""
    plain, htmlbody = '', ''

    def walk(part):
        nonlocal plain, htmlbody
        mime = part.get('mimeType', '')
        body = part.get('body', {})
        if mime == 'text/plain' and body.get('data') and not plain:
            plain = _decode_part(body['data'])
        elif mime == 'text/html' and body.get('data') and not htmlbody:
            htmlbody = _decode_part(body['data'])
        for sub in part.get('parts', []) or []:
            walk(sub)

    walk(payload)
    text = plain.strip() or _strip_html(htmlbody)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + '\n…（以下省略）'
    return text


def fetch_account(account):
    if not account['refresh_token']:
        print(f"[{account['address']}] refresh_tokenが未設定のためスキップ")
        return
    token = get_access_token(account['refresh_token'])

    q = urllib.parse.quote(f"is:unread in:inbox to:{account['address']}")
    result = gmail_api(token, f'messages?q={q}&maxResults=50')
    messages = result.get('messages', [])

    mails = []
    for m in messages:
        detail = gmail_api(token, f'messages/{m["id"]}?format=metadata'
            '&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date&metadataHeaders=To')
        hdrs = {h['name']: h['value'] for h in detail.get('payload', {}).get('headers', [])}
        from_str = hdrs.get('From', '')
        domain = extract_domain(from_str)

        if account['domain_filter'] is not None and domain not in account['domain_filter']:
            continue

        subj = hdrs.get('Subject', '（件名なし）')
        date_str = hdrs.get('Date', '')
        snippet = detail.get('snippet', '')[:120]

        full = gmail_api(token, f'messages/{m["id"]}?format=full')
        body = extract_body(full.get('payload', {})) or snippet

        try:
            from email.utils import parsedate_to_datetime
            clean = re.sub(r'\s*\([A-Z]+\)\s*$', '', date_str.strip())
            dt = parsedate_to_datetime(clean)
            date_disp = dt.strftime('%m/%d %H:%M')
            date_sort = dt.isoformat()
        except Exception:
            date_disp = date_str[:16]
            date_sort = ''

        to_str = hdrs.get('To', '')
        mails.append({
            'id': m['id'],
            'from': from_str,
            'to': to_str,
            'domain': domain,
            'subject': subj,
            'date': date_disp,
            'date_sort': date_sort,
            'snippet': snippet,
            'body': body,
        })

    mails.sort(key=lambda x: x.get('date_sort', ''), reverse=True)

    out = {
        'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'count': len(mails),
        'mails': mails
    }
    with open(account['out_file'], 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[{account['address']}] {len(mails)}件保存 → {account['out_file']}")


if __name__ == '__main__':
    for acct in ACCOUNTS:
        try:
            fetch_account(acct)
        except Exception as e:
            print(f"[{acct['address']}] ERROR: {e}")
