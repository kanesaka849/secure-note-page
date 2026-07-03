"""
ci_fetch_and_judge.py
GitHub Actions専用スクリプト。以下を実行する：
  1) IMAP経由でkanesaka@activia.co.jp / trial@agniyoga.jp / school@agniyoga.jp を取得
  2) Gmail API経由でkanesaka.agni@gmail.comの未読メールを取得（ドメインホワイトリストなし）
  3) 1と2の受信内容をAnthropic APIに送り、それぞれ「表示すべきメール」を判定
  4) mail_judgment.json / mail_trial_agniyoga.json / mail_school_agniyoga.json / mail_kanesaka_gmail.json を
     ci_input/ に書き出す（このディレクトリはコミットしない＝平文メール内容を公開リポジトリに残さない）
  5) API利用量（トークン数・概算コスト）を api_usage_log.json に追記する（こちらはコミット対象・
     メール本文などの機微情報は一切含まない）
"""
import imaplib, ssl, email, json, os, sys, re, base64, html, urllib.request, urllib.parse
from email.header import decode_header
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = os.environ.get('GITHUB_WORKSPACE', '.')
INPUT_DIR = os.path.join(WORKSPACE, 'ci_input')
os.makedirs(INPUT_DIR, exist_ok=True)

MAIL_SERVER = 'mail.agniyoga.jp'
MAIL_PORT   = 993
FETCH_N     = 60  # 月次KPI集計の取りこぼしを避けるため多めに取得

ACCOUNTS = {
    'activia': {'user': os.environ['MAIL_KANESAKA_ACTIVIA'], 'pass': os.environ['MAIL_KANESAKA_ACTIVIA_PASS']},
    'trial':   {'user': os.environ['MAIL_TRIAL_AGNIYOGA'],   'pass': os.environ['MAIL_TRIAL_AGNIYOGA_PASS']},
    'school':  {'user': os.environ['MAIL_SCHOOL_AGNIYOGA'],  'pass': os.environ['MAIL_SCHOOL_AGNIYOGA_PASS']},
}

GMAIL_CLIENT_ID     = os.environ.get('GMAIL_CLIENT_ID', '')
GMAIL_CLIENT_SECRET = os.environ.get('GMAIL_CLIENT_SECRET', '')
KANESAKA_GMAIL_REFRESH_TOKEN = os.environ.get('KANESAKA_GMAIL_REFRESH_TOKEN', '')

ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
ANTHROPIC_MODEL    = os.environ.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001')

# claude-haiku-4-5 の公表単価目安（1Mトークンあたり、2026-07時点の把握）。
# 為替・価格改定で変わり得るため、正確な金額は console.anthropic.com で必ず確認すること。
PRICE_PER_M_INPUT  = float(os.environ.get('PRICE_PER_M_INPUT', '1.0'))   # USD / 1M input tokens
PRICE_PER_M_OUTPUT = float(os.environ.get('PRICE_PER_M_OUTPUT', '5.0'))  # USD / 1M output tokens


def dec(s):
    if not s:
        return ''
    parts = decode_header(s)
    out = []
    for b, enc in parts:
        if isinstance(b, bytes):
            out.append(b.decode(enc or 'utf-8', errors='replace'))
        else:
            out.append(b)
    return ''.join(out)


def get_body(msg, limit=600):
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get('Content-Disposition', ''))
            if ct == 'text/plain' and 'attachment' not in disp:
                cs = part.get_content_charset() or 'utf-8'
                try:
                    return part.get_payload(decode=True).decode(cs, errors='replace')[:limit]
                except Exception:
                    pass
    else:
        cs = msg.get_content_charset() or 'utf-8'
        try:
            return msg.get_payload(decode=True).decode(cs, errors='replace')[:limit]
        except Exception:
            pass
    return ''


def fetch_account(user, password, n):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    results = []
    try:
        imap = imaplib.IMAP4_SSL(MAIL_SERVER, MAIL_PORT, ssl_context=ctx)
        imap.login(user, password)
        imap.select('INBOX')
        _, data = imap.search(None, 'ALL')
        ids = data[0].split()
        print(f'[{user}] {len(ids)}件中、最新{n}件取得')
        for uid in ids[-n:][::-1]:
            _, raw = imap.fetch(uid, '(RFC822)')
            msg = email.message_from_bytes(raw[0][1])
            results.append({
                'num':     int(uid),
                'subject': dec(msg.get('Subject', '')),
                'from':    dec(msg.get('From', '')),
                'date':    msg.get('Date', ''),
                'body':    get_body(msg),
            })
        imap.logout()
    except Exception as e:
        print(f'[{user}] ERROR: {e}')
    return results


def _call_anthropic(prompt):
    body = json.dumps({
        'model': ANTHROPIC_MODEL,
        'max_tokens': 4096,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req) as r:
            result = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f'Anthropic API error {e.code}: {e.read().decode("utf-8", errors="replace")}')
        raise

    text = ''.join(block.get('text', '') for block in result.get('content', []) if block.get('type') == 'text')
    usage = result.get('usage', {})
    return text, usage


def call_anthropic_judge(mails):
    """kanesaka@activia.co.jp の受信メールをAIに渡し、要対応メールを判定させる。"""
    digest = []
    for i, m in enumerate(mails):
        digest.append(
            f"[{i}] 件名: {m['subject']}\n差出人: {m['from']}\n日時: {m['date']}\n本文冒頭: {m['body'][:400]}"
        )
    joined = '\n---\n'.join(digest)

    prompt = f"""以下はkanesaka@activia.co.jp宛に届いた最新メール一覧です。
「要対応メール」として残すべきものだけを判定してください。判定基準：

- urgent（緊急）: セキュリティ通知・支払い期限のあるもの・重要な契約手続き・家族/学校からの対応が必要な連絡など、早めの行動が必要なもの
- info（参考）: すぐの対応は不要だが知っておくべき業務連絡・システム通知など
- 上記以外（広告・ニュースレター・スパム・自動配信の販促メール等）は除外してください

以下のJSON配列だけを出力してください（説明文・コードブロック記法は不要、JSON配列そのもの）:
[
  {{"index": <元のメールの[i]番号>, "type": "urgent"または"info", "icon": "絵文字1つ", "title": "件名を要約した短いタイトル（30字程度）", "sub": "対応内容の要約（60字程度）", "reason": "urgent/infoと判定した理由（20字程度）"}}
]

対象メール一覧：
{joined}
"""
    return _call_anthropic(prompt)


def call_anthropic_judge_personal(mails):
    """kanesaka.agni@gmail.com（個人）の未読メールをAIに渡し、表示すべきものだけ判定させる。"""
    digest = []
    for i, m in enumerate(mails):
        digest.append(
            f"[{i}] 件名: {m['subject']}\n差出人: {m['from']}\n日時: {m['date']}\n本文冒頭: {m['body'][:400]}"
        )
    joined = '\n---\n'.join(digest)

    prompt = f"""以下はkanesaka.agni@gmail.com（個人アカウント）宛の未読メール一覧です。
ダッシュボードに表示する価値があるものだけを判定してください。判定基準：

- 表示する: 学校（幕張総合高校）・サッカー関連・銀行/カード等の重要な通知やセキュリティ警告・
  打ち合わせ調整（TimeRex等）・GitHub関連・その他個人的に対応/確認が必要そうな連絡
- 表示しない: 広告・ニュースレター・ポイント/セール告知・SNS通知（Facebook等）・スパム・
  自動配信の販促メール全般

以下のJSON配列だけを出力してください（説明文・コードブロック記法は不要、JSON配列そのもの）:
[
  {{"index": <元のメールの[i]番号>, "reason": "表示すると判定した理由（20字程度）"}}
]
表示しないメールはこの配列に含めないでください。

対象メール一覧：
{joined}
"""
    return _call_anthropic(prompt)


def parse_judge_output(text, mails):
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        print('⚠️ AI応答からJSON配列を抽出できませんでした。urgent_mailsは空になります。')
        print(text[:500])
        return []
    try:
        items = json.loads(m.group(0))
    except Exception as e:
        print(f'⚠️ JSON parse失敗: {e}')
        return []

    urgent_mails = []
    for it in items:
        idx = it.get('index')
        if idx is None or idx >= len(mails):
            continue
        src = mails[idx]
        mid = 'm-ci-' + re.sub(r'[^a-z0-9]', '', src['from'].lower())[:12] + '-' + str(src['num'])
        urgent_mails.append({
            'id': mid,
            'type': it.get('type', 'info'),
            'icon': it.get('icon', '📧'),
            'title': it.get('title', src['subject'][:40]),
            'sub': it.get('sub', ''),
            'from_info': f"差出人：{src['from']}　→　kanesaka@activia.co.jp　｜　{src['date'][:16]}",
            'detail': src['body'][:600],
        })
    return urgent_mails


def _gmail_access_token():
    data = urllib.parse.urlencode({
        'client_id': GMAIL_CLIENT_ID,
        'client_secret': GMAIL_CLIENT_SECRET,
        'refresh_token': KANESAKA_GMAIL_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())['access_token']


def _gmail_api(token, path):
    req = urllib.request.Request(
        f'https://gmail.googleapis.com/gmail/v1/users/me/{path}',
        headers={'Authorization': f'Bearer {token}'}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _extract_domain(from_str):
    m = re.search(r'<(.+?)>', from_str)
    addr = m.group(1) if m else from_str.strip()
    return addr.split('@')[-1].lower() if '@' in addr else ''


def _gmail_decode_part(data):
    if not data:
        return ''
    padded = data.replace('-', '+').replace('_', '/')
    padded += '=' * (-len(padded) % 4)
    try:
        return base64.b64decode(padded).decode('utf-8', errors='replace')
    except Exception:
        return ''


def _gmail_strip_html(s):
    s = re.sub(r'(?is)<(script|style).*?</\1>', '', s)
    s = re.sub(r'(?i)<br\s*/?>', '\n', s)
    s = re.sub(r'(?i)</p>', '\n\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()


def _gmail_extract_body(payload, max_len=3000):
    plain, htmlbody = '', ''

    def walk(part):
        nonlocal plain, htmlbody
        mime = part.get('mimeType', '')
        body = part.get('body', {})
        if mime == 'text/plain' and body.get('data') and not plain:
            plain = _gmail_decode_part(body['data'])
        elif mime == 'text/html' and body.get('data') and not htmlbody:
            htmlbody = _gmail_decode_part(body['data'])
        for sub in part.get('parts', []) or []:
            walk(sub)

    walk(payload)
    text = (plain.strip() or _gmail_strip_html(htmlbody)).strip()
    if len(text) > max_len:
        text = text[:max_len] + '\n…（以下省略）'
    return text


def fetch_kanesaka_gmail_unread():
    """kanesaka.agni@gmail.com の未読メールを全件取得（フィルタなし・AIが後で判定）"""
    if not KANESAKA_GMAIL_REFRESH_TOKEN:
        print('KANESAKA_GMAIL_REFRESH_TOKEN未設定のためスキップ')
        return []
    token = _gmail_access_token()
    q = urllib.parse.quote('is:unread in:inbox to:kanesaka.agni@gmail.com')
    result = _gmail_api(token, f'messages?q={q}&maxResults=50')
    messages = result.get('messages', [])

    mails = []
    for m in messages:
        detail = _gmail_api(token, f'messages/{m["id"]}?format=metadata'
            '&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date&metadataHeaders=To')
        hdrs = {h['name']: h['value'] for h in detail.get('payload', {}).get('headers', [])}
        from_str = hdrs.get('From', '')
        subj = hdrs.get('Subject', '（件名なし）')
        date_str = hdrs.get('Date', '')
        snippet = detail.get('snippet', '')[:120]
        full = _gmail_api(token, f'messages/{m["id"]}?format=full')
        body = _gmail_extract_body(full.get('payload', {})) or snippet
        try:
            from email.utils import parsedate_to_datetime
            clean = re.sub(r'\s*\([A-Z]+\)\s*$', '', date_str.strip())
            dt = parsedate_to_datetime(clean)
            date_disp = dt.strftime('%m/%d %H:%M')
            date_sort = dt.isoformat()
        except Exception:
            date_disp = date_str[:16]
            date_sort = ''
        mails.append({
            'id': m['id'], 'from': from_str, 'to': hdrs.get('To', ''),
            'domain': _extract_domain(from_str), 'subject': subj,
            'date': date_disp, 'date_sort': date_sort, 'snippet': snippet, 'body': body,
        })
    mails.sort(key=lambda x: x.get('date_sort', ''), reverse=True)
    return mails


def parse_personal_judge_output(text, mails):
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        print('⚠️ 個人メールAI応答からJSON配列を抽出できませんでした。')
        return []
    try:
        items = json.loads(m.group(0))
    except Exception as e:
        print(f'⚠️ JSON parse失敗: {e}')
        return []
    kept = []
    for it in items:
        idx = it.get('index')
        if idx is None or idx >= len(mails):
            continue
        kept.append(mails[idx])
    return kept


def log_cost(usage):
    """トークン数と概算コストのみを記録（メール内容は含めない）"""
    log_path = os.path.join(WORKSPACE, 'api_usage_log.json')
    log = []
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding='utf-8') as f:
                log = json.load(f)
        except Exception:
            log = []

    in_tok = usage.get('input_tokens', 0)
    out_tok = usage.get('output_tokens', 0)
    cost = (in_tok / 1_000_000 * PRICE_PER_M_INPUT) + (out_tok / 1_000_000 * PRICE_PER_M_OUTPUT)

    log.append({
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'model': ANTHROPIC_MODEL,
        'input_tokens': in_tok,
        'output_tokens': out_tok,
        'estimated_cost_usd': round(cost, 5),
    })
    # 直近500件のみ保持（無限増殖を防ぐ）
    log = log[-500:]
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    total = sum(x['estimated_cost_usd'] for x in log)
    print(f'今回のAPI利用: input={in_tok} output={out_tok} 概算${cost:.5f} ｜ 累計(直近500件)概算${total:.4f}')


def main():
    # 1) 3アカウントを取得
    activia_mails = fetch_account(ACCOUNTS['activia']['user'], ACCOUNTS['activia']['pass'], FETCH_N)
    trial_mails   = fetch_account(ACCOUNTS['trial']['user'],   ACCOUNTS['trial']['pass'],   FETCH_N)
    school_mails  = fetch_account(ACCOUNTS['school']['user'],  ACCOUNTS['school']['pass'],  FETCH_N)

    with open(os.path.join(INPUT_DIR, 'mail_trial_agniyoga.json'), 'w', encoding='utf-8') as f:
        json.dump(trial_mails, f, ensure_ascii=False, indent=2)
    with open(os.path.join(INPUT_DIR, 'mail_school_agniyoga.json'), 'w', encoding='utf-8') as f:
        json.dump(school_mails, f, ensure_ascii=False, indent=2)

    # 2) AI判定（活動用メール）
    urgent_mails = []
    if activia_mails:
        text, usage = call_anthropic_judge(activia_mails)
        urgent_mails = parse_judge_output(text, activia_mails)
        log_cost(usage)
    else:
        print('⚠️ activiaメールが0件のためAI判定をスキップ')

    # 3) mail_judgment.json 書き出し（kpi_noteは空のまま＝手動追記用の余地を残す）
    judgment = {
        'updated': datetime.now().strftime('%Y-%m-%d'),
        'kpi_note': '',
        'training_count': 0,
        'urgent_mails': urgent_mails,
    }
    with open(os.path.join(INPUT_DIR, 'mail_judgment.json'), 'w', encoding='utf-8') as f:
        json.dump(judgment, f, ensure_ascii=False, indent=2)
    print(f'✅ 要対応メール {len(urgent_mails)}件 判定完了')

    # 4) kanesaka.agni@gmail.com（個人）の未読メールをAI判定
    try:
        personal_mails = fetch_kanesaka_gmail_unread()
        kept_mails = []
        if personal_mails:
            text2, usage2 = call_anthropic_judge_personal(personal_mails)
            kept_mails = parse_personal_judge_output(text2, personal_mails)
            log_cost(usage2)
        with open(os.path.join(INPUT_DIR, 'mail_kanesaka_gmail.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'fetched_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'count': len(kept_mails),
                'mails': kept_mails,
            }, f, ensure_ascii=False, indent=2)
        print(f'✅ 個人メール {len(personal_mails)}件中{len(kept_mails)}件を表示対象と判定')
    except Exception as e:
        print(f'個人メールAI判定エラー（スキップ）: {e}')


if __name__ == '__main__':
    main()
