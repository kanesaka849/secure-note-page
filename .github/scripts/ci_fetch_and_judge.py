"""
ci_fetch_and_judge.py
GitHub Actions専用スクリプト。以下を実行する：
  1) IMAP経由でkanesaka@activia.co.jp / trial@agniyoga.jp / school@agniyoga.jp / kanesaka@agniyoga.jp を取得
  2) Gmail API経由でkanesaka.agni@gmail.com / agniyoga.ad@gmail.comの未読メールを取得（フィルタなし）
  2b) POP3経由でinfo@zipyoga.jp（ZIPシステム問い合わせ先）を取得（school@agniyoga.jpとは無関係の別アカウント）
  3) kanesaka@activia.co.jp / kanesaka.agni@gmail.com / agniyoga.ad@gmail.com / info@zipyoga.jp /
     kanesaka@agniyoga.jp の5アカウント全てを「①要対応 ②お知らせ程度 ③宛先不明 ④スパムか不明」の
     4分類＋非表示（明らかな広告・スパム）で統一判定する。
     方式：sender_rules.json（アカウントごとの送信元→カテゴリ辞書）で確定するものはAI不要。
     未確定の送信元だけAnthropic APIに判定させ、結果を学習してルールに追記する。
     判断に迷う場合は「表示する」側（unclear）に倒す（見落とし防止）。
  4) mail_unified.json（4アカウント統合・表示用） / mail_trial_agniyoga.json / mail_school_agniyoga.json を
     ci_input/ に書き出す（このディレクトリはコミットしない＝平文メール内容を公開リポジトリに残さない）
  5) sender_rules.json（送信元識別子とカテゴリのみ・メール内容は含まない）と
     api_usage_log.json（トークン数・概算コストのみ）はコミット対象。
"""
import imaplib, poplib, ssl, email, json, os, sys, re, base64, hashlib, html, urllib.request, urllib.parse
from email.header import decode_header
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

JST = timezone(timedelta(hours=9))

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
    'kanesaka_agniyoga': {'user': os.environ.get('MAIL_KANESAKA_AGNIYOGA', ''),
                          'pass': os.environ.get('MAIL_KANESAKA_AGNIYOGA_PASS', '')},
}

# info@zipyoga.jp（ZIPシステム問い合わせ先）— GMO独自ドメインメール、POP3で取得。
# school@agniyoga.jpとは無関係の別アカウント。KEEP_ON_SERVER前提（DELEは送らない＝サーバー上のメールは消さない）。
ZIPYOGA_POP_SERVER = os.environ.get('ZIPYOGA_POP_SERVER', 'pop17.gmoserver.jp')
ZIPYOGA_POP_PORT = 995
POP3_ACCOUNTS = {
    'zipyoga_info': {'user': os.environ.get('ZIPYOGA_INFO_USER', 'info@zipyoga.jp'),
                      'pass': os.environ.get('ZIPYOGA_INFO_PASS', '')},
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

# AIアドバイス（🤖ボタン・メール1通のスパム/本物判定＋対応提案）は精度優先で上位モデル＋Web検索を使う。
# 一括仕分け（haiku）とは別軸のオンデマンド機能のため件数は少なく、1件あたり概算$0.05〜0.15。
ADVICE_MODEL = os.environ.get('ADVICE_MODEL', 'claude-opus-4-8')
ADVICE_PRICE_PER_M_INPUT  = 5.0    # USD / 1M input tokens（claude-opus-4-8目安）
ADVICE_PRICE_PER_M_OUTPUT = 25.0   # USD / 1M output tokens
ADVICE_PRICE_PER_SEARCH   = 0.01   # USD / Web検索1回（$10/1000回）
DASHBOARD_PW = os.environ.get('DASHBOARD_PW', '')  # advice_store.enc の暗号化に使用（公開リポジトリにメール内容を平文で置かない）

ACCOUNT_DISPLAY_TO = {
    'kanesaka_activia': 'kanesaka@activia.co.jp',
    'kanesaka_agni': 'kanesaka.agni@gmail.com',
    'agniyoga_ad': 'agniyoga.ad@gmail.com',
    'zipyoga': 'info@zipyoga.jp',
    'kanesaka_agniyoga': 'kanesaka@agniyoga.jp',
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


def _stable_msg_id(msg):
    """メールヘッダーのMessage-IDから変わらない識別子を作る。
    IMAP連番/POP3メッセージ番号はメールボックスの中身（削除・整理）が変わるとズレて
    別のメールを指してしまうため使わない（過去に done_state.json の完了記録が
    無関係なメールにズレて表示される実バグを起こした）。Message-IDが無い場合のみ
    Subject+Date+Fromで代替する。"""
    raw = msg.get('Message-ID', '') or msg.get('Message-Id', '') or msg.get('message-id', '')
    if not raw:
        raw = (msg.get('Subject', '') or '') + '|' + (msg.get('Date', '') or '') + '|' + (msg.get('From', '') or '')
    return hashlib.sha1(raw.encode('utf-8', errors='replace')).hexdigest()[:20]


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
                'id': _stable_msg_id(msg),
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


def fetch_pop3_account(user, password, server, port, n):
    """POP3でメールを取得する。RETRのみでDELEは送らない＝サーバー上のメールは削除しない。"""
    results = []
    if not password:
        print(f'[{user}] パスワード未設定のためスキップ')
        return results
    try:
        pop = poplib.POP3_SSL(server, port)
        pop.user(user)
        pop.pass_(password)
        count, _ = pop.stat()
        _, msg_list, _ = pop.list()
        ids = [int(line.split()[0]) for line in msg_list]
        print(f'[{user}] {count}件中、最新{n}件取得')
        for mid in ids[-n:][::-1]:
            _, lines, _ = pop.retr(mid)
            msg = email.message_from_bytes(b'\n'.join(lines))
            from_str = dec(msg.get('From', ''))
            results.append({
                'id': _stable_msg_id(msg),
                'subject': dec(msg.get('Subject', '')),
                'from': from_str,
                'domain': _extract_domain(from_str),
                'date': msg.get('Date', ''),
                'body': get_body(msg),
            })
        pop.quit()
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
"phishing_suspected": trueにした場合は"recommend"に**必ず**、具体的にどこが不審なのか
（例：表示名と実際の送信元ドメインが一致しない／リンク先がサービスと無関係なドメイン／
短い期限で急かしている、等）を40〜60字程度で簡潔に日本語で説明してください（空文字列にしない）。

加えて、セキュリティ通知（ログイン警告・アクセス許可通知・パスワード変更通知等）や、
判断に迷いそうな内容の場合も"recommend"に一言アドバイスを日本語で付けてください
（例：「心当たりがあれば対応不要」「不審な場合はパスワード変更を推奨」）。
ルーチン的な業務連絡・広告等でアドバイスが特に無い場合は"recommend"は空文字列にしてください
（毎回無理に埋めない。ただしphishing_suspected: trueの場合は必ず埋める）。

以下のJSON配列だけを出力してください（説明文・コードブロック記法は不要、JSON配列そのもの）:
[
  {{"index": <元のメールの[i]番号>, "category": "action"または"info"または"unclear"または"maybe_spam"または"hide", "phishing_suspected": trueまたはfalse, "icon": "絵文字1つ", "title": "件名を要約した短いタイトル（30字程度）", "sub": "内容の要約（60字程度）", "recommend": "一言アドバイス（40字程度、無ければ空文字列）"}}
]

対象メール一覧：
{joined}
"""
    return _call_anthropic(prompt)


def parse_unified_judge_output(text, mails):
    """戻り値: [(mail, category, icon, title, sub, phishing_suspected, recommend), ...]。
    パース失敗時は安全側に倒してunclearとする。"""
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        print('⚠️ AI応答からJSON配列を抽出できませんでした。安全側に倒して全件unclearとします。')
        return [(mail, 'unclear', '❓', mail['subject'][:30], '', False, '') for mail in mails]
    try:
        items = json.loads(m.group(0))
    except Exception as e:
        print(f'⚠️ JSON parse失敗: {e}。安全側に倒して全件unclearとします。')
        return [(mail, 'unclear', '❓', mail['subject'][:30], '', False, '') for mail in mails]

    by_index = {it.get('index'): it for it in items if it.get('index') is not None}
    results = []
    for i, mail in enumerate(mails):
        it = by_index.get(i)
        if it is None:
            results.append((mail, 'unclear', '❓', mail['subject'][:30], '', False, ''))
            continue
        category = it.get('category', 'unclear')
        if category not in ('action', 'info', 'unclear', 'maybe_spam', 'hide'):
            category = 'unclear'
        results.append((mail, category, it.get('icon', CATEGORY_ICON.get(category, '📧')),
                         it.get('title', mail['subject'][:30]), it.get('sub', ''),
                         bool(it.get('phishing_suspected', False)), it.get('recommend', '')))
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
    'zipyoga': {},
    'kanesaka_agniyoga': {},
}


def load_sender_rules():
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, encoding='utf-8') as f:
                rules = json.load(f)
            for acct in ('kanesaka_activia', 'kanesaka_agni', 'agniyoga_ad', 'zipyoga', 'kanesaka_agniyoga'):
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


# 内容によって重要度が毎回大きく変わるセキュリティ通知系ドメイン。
# サーバー側のカテゴリキャッシュを効かせず、常にAIに判定させる（recommendの
# 一言アドバイスも毎回生成されるようにするため）。
ALWAYS_FRESH_DOMAINS = {'accounts.google.com'}

def apply_sender_rules(mails, account_rules):
    """(mail, category)確定済みリストと、AI判定が必要な未確定リストに振り分ける。
    ルールの値が文字列＝AIが一意に確定済みの送信元（AI不要）。
    ルールの値が{"hide_categories": [...]}＝△ボタンで一部カテゴリのみ非表示指定された送信元
    （カテゴリ自体は毎回変わりうるため、AI判定は省略せず実行しhide_categoriesで事後フィルタする）。
    ALWAYS_FRESH_DOMAINSに該当する送信元は、学習済みルールがあっても毎回AI判定する
    （Google等、通知内容の重要度が毎回異なるため）。
    ルールで確定した送信元はphishing_suspectedを再判定しない（既知の送信元のため）。"""
    decided = []
    undecided = []
    for m in mails:
        addr = _sender_address(m['from'])
        domain = m.get('domain', '')
        if domain in ALWAYS_FRESH_DOMAINS:
            undecided.append(m)
            continue
        rule = account_rules.get(addr) or account_rules.get(domain)
        if isinstance(rule, str) and rule:
            decided.append((m, rule, CATEGORY_ICON.get(rule, '📧'), m['subject'][:30], '', False, ''))
        else:
            undecided.append(m)
    return decided, undecided


def _hide_categories_for(mail, account_rules):
    """△ボタンで送信元ごとに部分非表示指定されたカテゴリの集合を返す。
    actionカテゴリも指定可能（クライアント側で確認ダイアログを表示してから設定される想定）。"""
    addr = _sender_address(mail['from'])
    domain = mail.get('domain', '')
    rule = account_rules.get(addr) or account_rules.get(domain)
    if isinstance(rule, dict):
        return set(rule.get('hide_categories', []))
    return set()


def log_cost(usage, model=None, in_rate=None, out_rate=None, extra_cost=0.0):
    """トークン数と概算コストのみを記録（メール内容は含めない）。
    model/単価を指定しない場合は一括仕分け（haiku）の設定を使う。"""
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
    in_rate = PRICE_PER_M_INPUT if in_rate is None else in_rate
    out_rate = PRICE_PER_M_OUTPUT if out_rate is None else out_rate
    cost = (in_tok / 1_000_000 * in_rate) + (out_tok / 1_000_000 * out_rate) + extra_cost

    log.append({
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'model': model or ANTHROPIC_MODEL,
        'input_tokens': in_tok,
        'output_tokens': out_tok,
        'estimated_cost_usd': round(cost, 5),
    })
    log = log[-500:]  # 直近500件のみ保持（無限増殖を防ぐ）
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    total = sum(x['estimated_cost_usd'] for x in log)
    print(f'今回のAPI利用: input={in_tok} output={out_tok} 概算${cost:.5f} ｜ 累計(直近500件)概算${total:.4f}')


# ── AIアドバイス（🤖ボタン → ci_trigger/advice_requests.json 経由の依頼） ─────
# ダッシュボードの🤖ボタンが依頼をコミット→push起動の本ワークフローで生成→
# 結果はDASHBOARD_PWで暗号化した advice_store.enc に保存（公開リポジトリに平文を置かない）→
# rebuild_all.py が復号して該当メールの下に表示する。
ADVICE_REQ_FILE   = os.path.join(WORKSPACE, 'ci_trigger', 'advice_requests.json')
ADVICE_STORE_FILE = os.path.join(WORKSPACE, 'advice_store.enc')


def _advice_fernet(salt):
    import base64
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(DASHBOARD_PW.encode())))


def load_advice_store():
    import base64
    if not (DASHBOARD_PW and os.path.exists(ADVICE_STORE_FILE)):
        return {}
    try:
        with open(ADVICE_STORE_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        salt = base64.b64decode(raw['salt'])
        return json.loads(_advice_fernet(salt).decrypt(raw['token'].encode()))
    except Exception as e:
        print(f'advice_store読み込み失敗（新規作成します）: {e}')
        return {}


def save_advice_store(store):
    import base64
    salt = os.urandom(16)
    token = _advice_fernet(salt).encrypt(json.dumps(store, ensure_ascii=False).encode())
    with open(ADVICE_STORE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'salt': base64.b64encode(salt).decode(), 'token': token.decode()}, f)


def _call_anthropic_advice(mail):
    """1通のメールについて、Web検索も使ってスパム/本物の判定と対応アドバイスを生成する。
    サーバー側Web検索ツールがpause_turnで中断した場合は続きを再送する。"""
    prompt = f"""以下のメールについて、受信者（金坂）向けに安全性の判定とアドバイスを作成してください。

【最重要の判定事項】
① スパム・フィッシング詐欺の可能性（高・中・低）
② 本物（正規の送信元からの正当なメール）かどうか

必要に応じてWeb検索を使い、送信元ドメインが実在する企業・サービスの正規ドメインか、
同様の詐欺・フィッシング事例が報告されていないかを確認してください。

出力は以下のJSONオブジェクトのみ（説明文・コードブロック記法は不要）:
{{"spam_risk": "高"または"中"または"低", "genuine": "本物の可能性が高い"または"判断つかず"または"偽物・詐欺の疑い", "summary": "結論を1文（40字程度）", "advice": "推奨する対応（80字程度・具体的に）", "evidence": "判定根拠（Web検索で確認した事実を含む・80字程度）"}}

【対象メール】
差出人情報: {mail.get('from_info', '')}
件名: {mail.get('title', '')}
要約: {mail.get('sub', '')}
本文冒頭:
{mail.get('detail', '')}
"""
    messages = [{'role': 'user', 'content': prompt}]
    total = {'input_tokens': 0, 'output_tokens': 0, 'web_searches': 0}
    result = {}
    for _ in range(4):
        body = json.dumps({
            'model': ADVICE_MODEL,
            'max_tokens': 3000,
            'tools': [{'type': 'web_search_20260209', 'name': 'web_search', 'max_uses': 4}],
            'messages': messages,
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages', data=body,
            headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01',
                     'content-type': 'application/json'},
            method='POST')
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                result = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f'Anthropic API error {e.code}: {e.read().decode("utf-8", errors="replace")}')
            raise
        usage = result.get('usage', {})
        total['input_tokens'] += usage.get('input_tokens', 0)
        total['output_tokens'] += usage.get('output_tokens', 0)
        total['web_searches'] += usage.get('server_tool_use', {}).get('web_search_requests', 0)
        if result.get('stop_reason') == 'pause_turn':
            # サーバー側ツールの反復上限で中断→userプロンプト＋途中までのassistant応答を再送すると続きから再開される
            messages = [messages[0], {'role': 'assistant', 'content': result.get('content', [])}]
            continue
        break
    text = ''.join(b.get('text', '') for b in result.get('content', []) if b.get('type') == 'text')
    return text, total


def process_advice_requests(unified_mails):
    """ダッシュボードの🤖ボタンで登録されたアドバイス依頼を処理する。"""
    if not os.path.exists(ADVICE_REQ_FILE):
        return
    try:
        with open(ADVICE_REQ_FILE, encoding='utf-8') as f:
            reqs = json.load(f).get('requests', [])
    except Exception as e:
        print(f'アドバイス依頼ファイル読み込みエラー: {e}')
        reqs = []
    if not reqs:
        return
    if not DASHBOARD_PW:
        print('DASHBOARD_PW未設定のためAIアドバイスをスキップ（依頼は保留のまま残します）')
        return

    by_id = {m['id']: m for m in unified_mails}
    store = load_advice_store()
    now_str = datetime.now(JST).strftime('%m/%d %H:%M')
    ok = ng = 0
    for r in reqs[:10]:  # 1回の実行で最大10件（コスト暴走ガード）
        mid = r.get('id', '')
        mail = by_id.get(mid)
        if mail is None:
            store[mid] = {'error': 'メールが見つかりません（完了・非表示化された可能性）', 'checked': now_str}
            ng += 1
            continue
        try:
            text, usage = _call_anthropic_advice(mail)
            m2 = re.search(r'\{.*\}', text, re.DOTALL)
            data = json.loads(m2.group(0)) if m2 else {}
            store[mid] = {
                'spam_risk': str(data.get('spam_risk', '不明'))[:10],
                'genuine': str(data.get('genuine', '判定できず'))[:30],
                'summary': str(data.get('summary', text[:80]))[:140],
                'advice': str(data.get('advice', ''))[:220],
                'evidence': str(data.get('evidence', ''))[:220],
                'checked': now_str,
            }
            log_cost(usage, model=ADVICE_MODEL,
                     in_rate=ADVICE_PRICE_PER_M_INPUT, out_rate=ADVICE_PRICE_PER_M_OUTPUT,
                     extra_cost=usage.get('web_searches', 0) * ADVICE_PRICE_PER_SEARCH)
            print(f'🤖 アドバイス生成OK: {mid}（検索{usage.get("web_searches",0)}回）')
            ok += 1
        except Exception as e:
            print(f'🤖 アドバイス生成失敗 {mid}: {e}')
            store[mid] = {'error': f'生成に失敗しました（{type(e).__name__}）。もう一度🤖を押すと再試行します', 'checked': now_str}
            ng += 1

    # 直近100件のみ保持（無限増殖を防ぐ）
    if len(store) > 100:
        store = dict(sorted(store.items(), key=lambda kv: kv[1].get('checked', ''), reverse=True)[:100])
    save_advice_store(store)
    with open(ADVICE_REQ_FILE, 'w', encoding='utf-8') as f:
        json.dump({'requests': []}, f, ensure_ascii=False, indent=2)
    print(f'🤖 AIアドバイス処理完了: 成功{ok}件 / 失敗{ng}件（依頼ファイルをクリア）')


def judge_account(account_key, account_label, mails, rules):
    """1アカウント分のメールを判定し、統合フォーマットのdictリストを返す。"""
    account_rules = rules.setdefault(account_key, {})
    decided, undecided = apply_sender_rules(mails, account_rules)
    print(f'[{account_key}] ルールで確定{len(decided)}件・AI判定が必要{len(undecided)}件')

    if undecided:
        text, usage = call_anthropic_judge_unified(account_label, undecided)
        ai_results = parse_unified_judge_output(text, undecided)
        log_cost(usage)
        for mail, category, icon, title, sub, phishing, recommend in ai_results:
            hidden = _hide_categories_for(mail, account_rules)
            if category in hidden:
                category = 'hide'
            decided.append((mail, category, icon, title, sub, phishing, recommend))
            addr = _sender_address(mail['from'])
            # 新規送信元（△の部分非表示ルールが無い）のみAI判定結果を文字列としてキャッシュする。
            # 既存の{"hide_categories":...}ルールは上書きしない（毎回AI判定が必要なため）。
            # ALWAYS_FRESH_DOMAINSは意図的に毎回AI判定するためキャッシュ自体を作らない。
            if addr not in account_rules and mail.get('domain', '') not in ALWAYS_FRESH_DOMAINS:
                account_rules[addr] = category

    unified = []
    for mail, category, icon, title, sub, phishing, recommend in decided:
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
            'recommend': recommend,
            'domain': mail.get('domain', ''),
            'date': mail.get('date', ''),
            'to': ACCOUNT_DISPLAY_TO.get(account_key, ''),
            'from_info': f"差出人：{mail['from']}　→　{ACCOUNT_DISPLAY_TO.get(account_key,'')}　｜　{mail['date'][:20]}",
            'detail': mail['body'][:600],
        })
    return unified


def main():
    # 1) IMAP: trial（KPI集計専用・分類対象外）/ school（KPI集計に加えて統合AI判定にも使用）
    trial_mails   = fetch_imap_account(IMAP_ACCOUNTS['trial']['user'],   IMAP_ACCOUNTS['trial']['pass'],   FETCH_N)
    school_mails  = fetch_imap_account(IMAP_ACCOUNTS['school']['user'],  IMAP_ACCOUNTS['school']['pass'],  FETCH_N)
    with open(os.path.join(INPUT_DIR, 'mail_trial_agniyoga.json'), 'w', encoding='utf-8') as f:
        json.dump(trial_mails, f, ensure_ascii=False, indent=2)
    with open(os.path.join(INPUT_DIR, 'mail_school_agniyoga.json'), 'w', encoding='utf-8') as f:
        json.dump(school_mails, f, ensure_ascii=False, indent=2)

    # 2) 5アカウントを取得
    activia_mails = fetch_imap_account(IMAP_ACCOUNTS['activia']['user'], IMAP_ACCOUNTS['activia']['pass'], FETCH_N)
    kanesaka_agniyoga_mails = fetch_imap_account(
        IMAP_ACCOUNTS['kanesaka_agniyoga']['user'], IMAP_ACCOUNTS['kanesaka_agniyoga']['pass'], FETCH_N)
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

    # info@zipyoga.jp（ZIPシステム問い合わせ先）自体の受信箱をPOP3で取得。school@agniyoga.jpとは
    # 無関係の別アカウントで、school側は引き続きKPI集計専用のまま統合判定には含めない。
    zipyoga_info_mails = fetch_pop3_account(
        POP3_ACCOUNTS['zipyoga_info']['user'], POP3_ACCOUNTS['zipyoga_info']['pass'],
        ZIPYOGA_POP_SERVER, ZIPYOGA_POP_PORT, FETCH_N)

    # 3) 統合判定
    rules = load_sender_rules()
    unified_mails = []
    unified_mails += judge_account('kanesaka_activia', 'kanesaka@activia.co.jp', activia_mails, rules)
    unified_mails += judge_account('kanesaka_agni', 'kanesaka.agni@gmail.com（個人アカウント）', kanesaka_agni_mails, rules)
    unified_mails += judge_account('agniyoga_ad', 'agniyoga.ad@gmail.com（広告運用アカウント）', agniyoga_ad_mails, rules)
    unified_mails += judge_account('zipyoga', 'info@zipyoga.jp（ZIPシステム問い合わせ先）', zipyoga_info_mails, rules)
    unified_mails += judge_account('kanesaka_agniyoga', 'kanesaka@agniyoga.jp', kanesaka_agniyoga_mails, rules)
    save_sender_rules(rules)

    with open(os.path.join(INPUT_DIR, 'mail_unified.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'updated': datetime.now(JST).strftime('%Y-%m-%d %H:%M'),
            'mails': unified_mails,
        }, f, ensure_ascii=False, indent=2)

    n_action = sum(1 for m in unified_mails if m['category'] == 'action')
    print(f'✅ 統合メール判定完了：計{len(unified_mails)}件（うち要対応{n_action}件）')

    # 4) 🤖ボタンで依頼されたAIアドバイス（Web検索つき・opus）を生成
    process_advice_requests(unified_mails)

    # rebuild_all.pyがkpi_note/training_count読み込みのために存在を前提とするため、
    # 空のスタブとして書き出す（urgent_mailsはmail_unified.jsonに統合されたため含めない）
    with open(os.path.join(INPUT_DIR, 'mail_judgment.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'updated': datetime.now(JST).strftime('%Y-%m-%d'),
            'kpi_note': '',
            'training_count': 0,
        }, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
