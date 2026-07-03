"""
ci_fetch_and_judge.py
GitHub Actions専用スクリプト。以下を実行する：
  1) IMAP経由でkanesaka@activia.co.jp / trial@agniyoga.jp / school@agniyoga.jp を取得
  2) Gmail API経由でkanesaka.agni@gmail.com / agniyoga.ad@gmail.comの未読メールを取得（フィルタなし）
  3) kanesaka@activia.co.jp / kanesaka.agni@gmail.com / agniyoga.ad@gmail.com の3アカウント全てを
     「①要対応 ②お知らせ程度 ③宛先不明 ④スパムか不明」の4分類＋非表示（明らかな広告・スパム）で統一判定する。
     方式：sender_rules.json（アカウントごとの送信元→カテゴリ辞書）で確定するものはAI不要。
     未確定の送信元だけAnthropic APIに判定させ、結果を学習してルールに追記する。
     判断に迷う場合は「表示する」側（unclear）に倒す（見落とし防止）。
  4) mail_unified.json（3アカウント統合・表示用） / mail_trial_agniyoga.json / mail_school_agniyoga.json を
     ci_input/ に書き出す（このディレクトリはコミットしない＝平文メール内容を公開リポジトリに残さない）
  5) sender_rules.json（送信元識別子とカテゴリのみ・メール内容は含まない）と
     api_usage_log.json（トークン数・概算コストのみ）はコミット対象。
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

IMAP_ACCOUNTS = {
    'activia': {'user': os.environ['MAIL_KANESAKA_ACTIVIA'], 'pass': os.environ['MAIL_KANESAKA_ACTIVIA_PASS']},
    'trial':   {'user': os.environ['MAIL_TRIAL_AGNIYOGA'],   'pass': os.environ['MAIL_TRIAL_AGNIYOGA_PASS']},
    'school':  {'user': os.environ['MAIL_SCHOOL_AGNIYOGA'],  'pass': os.environ['MAIL_SCHOOL_AGNIYOGA_PASS']},
}

GMAIL_CLIENT_ID     = os.environ.get('GMAIL_CLIENT_ID', '')
GMAIL_CLIENT_SECRET = os.environ.get('GMAIL_CLIENT_SECRET', '')
GMAIL_ACCOUNTS = {
    'kanesaka_agni': {
        'address': 'kanesaka.agni@gmail.com',
        'refresh_token': os.environ.get('KANESAKA_GMAIL_REFRESH_TOKEN', ''),
    },
    'agniyoga_ad': {
        'address': 'agniyoga.ad@gmail.com',
        'refresh_token': os.environ.get('AGNIYOGA_AD_GMAIL_REFRESH_TOKEN', ''),
    },
}

ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
ANTHROPIC_MODEL    = os.environ.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001')

# claude-haiku-4-5 の公表単価目安（1Mトークンあたり、2026-07時点の把握）。
# 為替・価格改定で変わり得るため、正確な金額は console.anthropic.com で必ず確認すること。
PRICE_PER_M_INPUT  = float(os.environ.get('PRICE_PER_M_INPUT', '1.0'))   # USD / 1M input tokens
PRICE_PER_M_OUTPUT = float(os.environ.get('PRICE_PER_M_OUTPUT', '5.0'))  # USD / 1M output tokens

ACCOUNT_DISPLAY_TO = {
    'kanesaka_activia': 'kanesaka@activia.co.jp',
    'kanesaka_agni': 'kanesaka.agni@gmail.com',
    'agniyoga_ad': 'agniyoga.ad@gmail.com',
}

CATEGORY_ICON = {
    'action': '🔴', 'info': '📧', 'unclear': '❓', 'maybe_spam': '⚠️',
}


# ── IMAP（kanesaka@activia.co.jp / trial / school） ─────────────────────
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


def _extract_domain(from_str):
    m = re.search(r'<(.+?)>', from_str)
    addr = m.group(1) if m else from_str.strip()
    return addr.split('@')[-1].lower() if '@' in addr else ''


def _sender_address(from_str):
    m = re.search(r'<(.+?)>', from_str)
    return (m.group(1) if m else from_str.strip()).lower()


def fetch_imap_account(user, password, n):
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
            from_str = dec(msg.get('From', ''))
            results.append({
                'id': str(int(uid)),
                'subject': dec(msg.get('Subject', '')),
                'from': from_str,
                'domain': _extract_domain(from_str),
                'date': msg.get('Date', ''),
                'body': get_body(msg),
            })
        imap.logout()
    except Exception as e:
        print(f'[{user}] ERROR: {e}')
    return results


# ── Gmail API（kanesaka.agni / agniyoga.ad） ────────────────────────────
def _gmail_access_token(refresh_token):
    data = urllib.parse.urlencode({
        'client_id': GMAIL_CLIENT_ID,
        'client_secret': GMAIL_CLIENT_SECRET,
        'refresh_token': refresh_token,
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


def fetch_gmail_unread(address, refresh_token):
    if not refresh_token:
        print(f'[{address}] refresh_token未設定のためスキップ')
        return []
    token = _gmail_access_token(refresh_token)
    q = urllib.parse.quote(f'is:unread in:inbox to:{address}')
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
        mails.append({
            'id': m['id'], 'subject': subj, 'from': from_str,
            'domain': _extract_domain(from_str), 'date': date_str[:30], 'body': body,
        })
    return mails


# ── Anthropic API ────────────────────────────────────────────────────────
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


def call_anthropic_judge_unified(account_label, mails):
    """未確定の送信元からのメールを5分類のいずれかで判定させる。"""
    digest = []
    for i, m in enumerate(mails):
        digest.append(
            f"[{i}] 件名: {m['subject']}\n差出人: {m['from']}\n日時: {m['date']}\n本文冒頭: {m['body'][:400]}"
        )
    joined = '\n---\n'.join(digest)

    prompt = f"""以下は{account_label}宛のメールのうち、送信元ルールでまだ確定していないものです。
1件ずつ次の5分類のいずれかで判定してください：

- action: 要対応（支払い期限・重要な手続き・セキュリティ警告・家族/学校/仕事からの対応が必要な連絡）
- info: お知らせ程度（対応不要だが知っておくべき業務連絡・システム通知）
- unclear: 宛先本人（金坂）宛であることは分かるが内容の重要度が読み取りにくく、本人の判断が必要
- maybe_spam: 広告・営業・スパムの可能性が高いが、確信が持てるほど明確ではない
- hide: 明らかな広告・ニュースレター・スパム・自動配信の販促メール（確信が持てる場合のみ）

判断に迷う場合はunclearを選んでください（見落としより誤表示の方が害が少ないため）。

さらに、正規の通知を装ったフィッシング詐欺の疑いがある場合（支払い情報の更新を急かす・
リンククリックを煽る・差出人アドレスが不自然、等）は"phishing_suspected": trueも付けてください
（この場合カテゴリはhideにせず、必ずaction等のまま警告表示に回します）。

以下のJSON配列だけを出力してください（説明文・コードブロック記法は不要、JSON配列そのもの）:
[
  {{"index": <元のメールの[i]番号>, "category": "action"または"info"または"unclear"または"maybe_spam"または"hide", "phishing_suspected": trueまたはfalse, "icon": "絵文字1つ", "title": "件名を要約した短いタイトル（30字程度）", "sub": "内容の要約（60字程度）"}}
]

対象メール一覧：
{joined}
"""
    return _call_anthropic(prompt)


def parse_unified_judge_output(text, mails):
    """戻り値: [(mail, category, icon, title, sub, phishing_suspected), ...]。
    パース失敗時は安全側に倒してunclearとする。"""
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        print('⚠️ AI応答からJSON配列を抽出できませんでした。安全側に倒して全件unclearとします。')
        return [(mail, 'unclear', '❓', mail['subject'][:30], '', False) for mail in mails]
    try:
        items = json.loads(m.group(0))
    except Exception as e:
        print(f'⚠️ JSON parse失敗: {e}。安全側に倒して全件unclearとします。')
        return [(mail, 'unclear', '❓', mail['subject'][:30], '', False) for mail in mails]

    by_index = {it.get('index'): it for it in items if it.get('index') is not None}
    results = []
    for i, mail in enumerate(mails):
        it = by_index.get(i)
        if it is None:
            results.append((mail, 'unclear', '❓', mail['subject'][:30], '', False))
            continue
        category = it.get('category', 'unclear')
        if category not in ('action', 'info', 'unclear', 'maybe_spam', 'hide'):
            category = 'unclear'
        results.append((mail, category, it.get('icon', CATEGORY_ICON.get(category, '📧')),
                         it.get('title', mail['subject'][:30]), it.get('sub', ''),
                         bool(it.get('phishing_suspected', False))))
    return results


# ── 送信元ルール（アカウントごと・送信元→カテゴリの辞書、学習型） ────────
RULES_FILE = os.path.join(WORKSPACE, 'sender_rules.json')

DEFAULT_SENDER_RULES = {
    'kanesaka_activia': {},
    'kanesaka_agni': {
        'mamail.jp': 'action', 'chiba-c.ed.jp': 'action',
        'mail.rakuten-bank.co.jp': 'info', 'musashinobank.co.jp': 'info',
        'cardservice.co.jp': 'action', 'bizcomfort.jp': 'info',
        'noreply@bizcomfort.jp': 'info', '0101.co.jp': 'info',
        'timerex.net': 'info', 'github.com': 'info',
        'no-reply@accounts.google.com': 'info',
        'attendance.officestation.jp': 'action',
        'sg.newsletter.agoda-emails.com': 'hide', 'marketing.klook.com': 'hide',
        'zozo.jp': 'hide', 'mail-noreply@google.com': 'hide',
        'chiba-bazooka2nd.com': 'hide', 'cityheaven.net': 'hide',
        'mail.cityheaven.net': 'hide', 'bijouluna.jp': 'hide',
        'lensspeed.jp': 'hide', 'devo.jp': 'hide', 'backofficeforce.jp': 'hide',
        'money.note.com': 'hide', 'email.claude.com': 'hide',
        'mail.7cs-card.co.jp': 'hide',
    },
    'agniyoga_ad': {},
}


def load_sender_rules():
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, encoding='utf-8') as f:
                rules = json.load(f)
            for acct in ('kanesaka_activia', 'kanesaka_agni', 'agniyoga_ad'):
                rules.setdefault(acct, {})
            # 旧スキーマ（always_show/always_hideのフラットリスト）の残骸を除去
            rules.pop('always_show', None)
            rules.pop('always_hide', None)
            return rules
        except Exception:
            pass
    return {k: dict(v) for k, v in DEFAULT_SENDER_RULES.items()}


def save_sender_rules(rules):
    with open(RULES_FILE, 'w', encoding='utf-8') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


def apply_sender_rules(mails, account_rules):
    """(mail, category)確定済みリストと、AI判定が必要な未確定リストに振り分ける。
    ルールで確定した送信元はphishing_suspectedを再判定しない（既知の送信元のため）。"""
    decided = []
    undecided = []
    for m in mails:
        addr = _sender_address(m['from'])
        domain = m.get('domain', '')
        category = account_rules.get(addr) or account_rules.get(domain)
        if category:
            decided.append((m, category, CATEGORY_ICON.get(category, '📧'), m['subject'][:30], '', False))
        else:
            undecided.append(m)
    return decided, undecided


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
    log = log[-500:]  # 直近500件のみ保持（無限増殖を防ぐ）
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    total = sum(x['estimated_cost_usd'] for x in log)
    print(f'今回のAPI利用: input={in_tok} output={out_tok} 概算${cost:.5f} ｜ 累計(直近500件)概算${total:.4f}')


def judge_account(account_key, account_label, mails, rules):
    """1アカウント分のメールを判定し、統合フォーマットのdictリストを返す。"""
    account_rules = rules.setdefault(account_key, {})
    decided, undecided = apply_sender_rules(mails, account_rules)
    print(f'[{account_key}] ルールで確定{len(decided)}件・AI判定が必要{len(undecided)}件')

    if undecided:
        text, usage = call_anthropic_judge_unified(account_label, undecided)
        ai_results = parse_unified_judge_output(text, undecided)
        log_cost(usage)
        for mail, category, icon, title, sub, phishing in ai_results:
            decided.append((mail, category, icon, title, sub, phishing))
            addr = _sender_address(mail['from'])
            if addr not in account_rules:
                account_rules[addr] = category

    unified = []
    for mail, category, icon, title, sub, phishing in decided:
        if category == 'hide':
            continue
        mid = f'm-{account_key}-' + re.sub(r'[^a-z0-9]', '', mail['id'])[:24]
        if phishing:
            title = '⚠️【フィッシング注意】' + title
        unified.append({
            'id': mid,
            'account': account_key,
            'category': category,
            'icon': icon,
            'title': title,
            'sub': sub,
            'domain': mail.get('domain', ''),
            'from_info': f"差出人：{mail['from']}　→　{ACCOUNT_DISPLAY_TO.get(account_key,'')}　｜　{mail['date'][:20]}",
            'detail': mail['body'][:600],
        })
    return unified


def main():
    # 1) IMAP: trial/school（KPI集計専用・分類対象外）
    trial_mails   = fetch_imap_account(IMAP_ACCOUNTS['trial']['user'],   IMAP_ACCOUNTS['trial']['pass'],   FETCH_N)
    school_mails  = fetch_imap_account(IMAP_ACCOUNTS['school']['user'],  IMAP_ACCOUNTS['school']['pass'],  FETCH_N)
    with open(os.path.join(INPUT_DIR, 'mail_trial_agniyoga.json'), 'w', encoding='utf-8') as f:
        json.dump(trial_mails, f, ensure_ascii=False, indent=2)
    with open(os.path.join(INPUT_DIR, 'mail_school_agniyoga.json'), 'w', encoding='utf-8') as f:
        json.dump(school_mails, f, ensure_ascii=False, indent=2)

    # 2) 3アカウントを取得
    activia_mails = fetch_imap_account(IMAP_ACCOUNTS['activia']['user'], IMAP_ACCOUNTS['activia']['pass'], FETCH_N)
    try:
        kanesaka_agni_mails = fetch_gmail_unread(
            GMAIL_ACCOUNTS['kanesaka_agni']['address'], GMAIL_ACCOUNTS['kanesaka_agni']['refresh_token'])
    except Exception as e:
        print(f'kanesaka.agni Gmail取得エラー: {e}')
        kanesaka_agni_mails = []
    try:
        agniyoga_ad_mails = fetch_gmail_unread(
            GMAIL_ACCOUNTS['agniyoga_ad']['address'], GMAIL_ACCOUNTS['agniyoga_ad']['refresh_token'])
    except Exception as e:
        print(f'agniyoga.ad Gmail取得エラー: {e}')
        agniyoga_ad_mails = []

    # 3) 統合判定
    rules = load_sender_rules()
    unified_mails = []
    unified_mails += judge_account('kanesaka_activia', 'kanesaka@activia.co.jp', activia_mails, rules)
    unified_mails += judge_account('kanesaka_agni', 'kanesaka.agni@gmail.com（個人アカウント）', kanesaka_agni_mails, rules)
    unified_mails += judge_account('agniyoga_ad', 'agniyoga.ad@gmail.com（広告運用アカウント）', agniyoga_ad_mails, rules)
    save_sender_rules(rules)

    with open(os.path.join(INPUT_DIR, 'mail_unified.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'mails': unified_mails,
        }, f, ensure_ascii=False, indent=2)

    n_action = sum(1 for m in unified_mails if m['category'] == 'action')
    print(f'✅ 統合メール判定完了：計{len(unified_mails)}件（うち要対応{n_action}件）')

    # rebuild_all.pyがkpi_note/training_count読み込みのために存在を前提とするため、
    # 空のスタブとして書き出す（urgent_mailsはmail_unified.jsonに統合されたため含めない）
    with open(os.path.join(INPUT_DIR, 'mail_judgment.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'updated': datetime.now().strftime('%Y-%m-%d'),
            'kpi_note': '',
            'training_count': 0,
        }, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
