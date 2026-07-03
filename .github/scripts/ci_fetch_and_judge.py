"""
ci_fetch_and_judge.py
GitHub Actions専用スクリプト。以下を実行する：
  1) IMAP経由でkanesaka@activia.co.jp / trial@agniyoga.jp / school@agniyoga.jp を取得
  2) kanesaka@activia.co.jpの受信内容をAnthropic APIに送り「要対応メール」を判定
  3) mail_judgment.json / mail_trial_agniyoga.json / mail_school_agniyoga.json を
     ci_input/ に書き出す（このディレクトリはコミットしない＝平文メール内容を公開リポジトリに残さない）
  4) API利用量（トークン数・概算コスト）を api_usage_log.json に追記する（こちらはコミット対象・
     メール本文などの機微情報は一切含まない）
"""
import imaplib, ssl, email, json, os, sys, re, urllib.request
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

    # 2) AI判定
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


if __name__ == '__main__':
    main()
