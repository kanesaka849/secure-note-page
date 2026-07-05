"""
rebuild_all.py  ─  金坂ダッシュボード ビルド&デプロイスクリプト
実行: python rebuild_all.py
  → mail_judgment.json を読んで要対応メール生成
  → mail_*.json から今月のKPIを自動集計
  → 暗号化HTML生成 → output/ に保存 → git push でデプロイ
"""
import base64, json, sys, os, re, shutil, subprocess
from datetime import datetime, date, timedelta, timezone
from email.utils import parsedate_to_datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# CI（GitHub Actions）はUTCで動くため、datetime.now()の類は必ずJSTを明示指定すること
# （指定しないとホストのタイムゾーンに引きずられ、カレンダー時刻等が最大9時間ずれる）
JST = timezone(timedelta(hours=9))

# ── CI mode detection ────────────────────────────────────────────────────
# GitHub Actions実行時（GITHUB_ACTIONS=true）はパス・パスワード・git操作を
# 環境変数/リポジトリ直書き込みに切り替える。ローカル実行時の挙動は一切変更しない。
CI_MODE = os.environ.get('GITHUB_ACTIONS') == 'true'

# ── Paths ───────────────────────────────────────────────────────────────
if CI_MODE:
    WORKSPACE     = os.environ.get('GITHUB_WORKSPACE', '.')
    PROJECT_DIR   = WORKSPACE
    INPUT_DIR     = os.path.join(WORKSPACE, 'ci_input')   # AI判定・メール取得結果の一時置き場（コミットしない）
    OUTPUT_DIR    = WORKSPACE                              # リポジトリ直下＝そのままコミット対象
    REPO_DIR      = WORKSPACE
else:
    PROJECT_DIR   = r"C:\barikata\claude\kaneSaka"
    INPUT_DIR     = os.path.join(PROJECT_DIR, "input")
    OUTPUT_DIR    = os.path.join(PROJECT_DIR, "output")
    REPO_DIR      = os.path.join(PROJECT_DIR, "repo")
REPO_URL      = "https://github.com/kanesaka849/secure-note-page.git"

JUDGMENT_FILE      = os.path.join(INPUT_DIR, "mail_judgment.json")
TRIAL_FILE         = os.path.join(INPUT_DIR, "mail_trial_agniyoga.json")
SCHOOL_FILE        = os.path.join(INPUT_DIR, "mail_school_agniyoga.json")
UNIFIED_MAIL_FILE = os.path.join(INPUT_DIR, "mail_unified.json")
CALENDAR_FILE = os.path.join(INPUT_DIR, "calendar_events.json")
ADVICE_STORE_FILE = os.path.join(REPO_DIR, "advice_store.enc")


def _load_advice_store():
    """🤖AIアドバイス結果（ci_fetch_and_judge.pyがDASHBOARD_PWで暗号化保存）を復号する。
    PWが無い環境（ローカル実行等）では空dict＝アドバイス非表示。"""
    pw = os.environ.get('DASHBOARD_PW', '')
    if not (pw and os.path.exists(ADVICE_STORE_FILE)):
        return {}
    try:
        import base64
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        with open(ADVICE_STORE_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        salt = base64.b64decode(raw['salt'])
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480000)
        fernet = Fernet(base64.urlsafe_b64encode(kdf.derive(pw.encode())))
        return json.loads(fernet.decrypt(raw['token'].encode()))
    except Exception as e:
        print(f'advice_store復号失敗（アドバイス表示をスキップ）: {e}')
        return {}


ADVICE_STORE = _load_advice_store()

OUT_MAIN  = os.path.join(OUTPUT_DIR, "kanesaka_tasks_secure.html" if not CI_MODE else "kanesaka-tasks.html")
OUT_MAIL  = os.path.join(OUTPUT_DIR, "kanesaka-mail-all-secure.html" if not CI_MODE else "kanesaka-mail-all.html")
OUT_HIST  = os.path.join(OUTPUT_DIR, "kanesaka-task-history.html")
OUT_RULES = os.path.join(OUTPUT_DIR, "kanesaka-mail-rules.html")
REPO_MAIN  = os.path.join(REPO_DIR, "kanesaka-tasks.html")
REPO_MAIL  = os.path.join(REPO_DIR, "kanesaka-mail-all.html")
REPO_HIST  = os.path.join(REPO_DIR, "kanesaka-task-history.html")
REPO_RULES = os.path.join(REPO_DIR, "kanesaka-mail-rules.html")
TOKEN_FILE = os.path.join(INPUT_DIR, "github_done_token.txt")

# ── Encryption ──────────────────────────────────────────────────────────
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

UID = "kanesaka"
# このファイルは公開リポジトリにコミットされるため、パスワードは絶対にハードコードしない。
# GitHub Actions（Secrets の DASHBOARD_PW）からのみ実行される想定。
PW  = UID + ":" + os.environ['DASHBOARD_PW']

def encrypt(html_str):
    raw     = html_str.encode('utf-8')
    salt    = os.urandom(16)
    iv      = os.urandom(16)
    kdf     = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000, backend=default_backend())
    key     = kdf.derive(PW.encode('utf-8'))
    pad_len = 16 - (len(raw) % 16)
    padded  = raw + bytes([pad_len] * pad_len)
    enc     = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).encryptor()
    ct      = enc.update(padded) + enc.finalize()
    return base64.b64encode(salt).decode(), base64.b64encode(iv).decode(), base64.b64encode(ct).decode()

def make_wrapper(title, SALT, IV, CT, back_link=None):
    back_btn = f'<a href="{back_link}" style="color:#94a3b8;font-size:13px;text-decoration:none;">← 戻る</a>' if back_link else ''
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:'Segoe UI','Hiragino Sans','Meiryo',sans-serif;background:#0f172a;min-height:100vh;display:flex;align-items:center;justify-content:center;}}
#lock-screen{{background:#1e293b;border-radius:16px;padding:40px 48px;width:100%;max-width:400px;box-shadow:0 25px 60px rgba(0,0,0,.5);border:1px solid #334155;}}
.lock-icon{{text-align:center;font-size:48px;margin-bottom:20px;}}
h1{{text-align:center;color:#e2e8f0;font-size:20px;font-weight:600;margin-bottom:6px;}}
.subtitle{{text-align:center;color:#64748b;font-size:13px;margin-bottom:28px;}}
label{{display:block;color:#94a3b8;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;}}
input[type=text],input[type=password]{{width:100%;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px 16px;color:#e2e8f0;font-size:15px;outline:none;transition:border-color .2s;}}
input[type=text]:focus,input[type=password]:focus{{border-color:#3b82f6;}}
button{{width:100%;margin-top:16px;background:#3b82f6;color:white;border:none;border-radius:8px;padding:13px;font-size:15px;font-weight:600;cursor:pointer;transition:background .2s;}}
button:hover{{background:#2563eb;}}button:disabled{{background:#374151;cursor:not-allowed;}}
#status{{margin-top:12px;text-align:center;font-size:13px;color:#ef4444;min-height:20px;}}
#status.working{{color:#94a3b8;}}#login-form{{display:none;}}
</style></head><body>
<div id="lock-screen">
  <div class="lock-icon">&#x1F510;</div>
  <h1 id="lock-title">&#x8AAD;&#x307F;&#x8FBC;&#x307F;&#x4E2D;&#x2026;</h1>
  <p class="subtitle" id="lock-sub">{back_btn}</p>
  <div id="login-form">
    <form onsubmit="unlock();return false;" autocomplete="on">
      <label for="uid-input">ID</label>
      <input type="text" id="uid-input" name="username" autocomplete="username" placeholder="ID" style="margin-bottom:16px" autofocus>
      <label for="pw-input">&#x30D1;&#x30B9;&#x30EF;&#x30FC;&#x30C9;</label>
      <input type="password" id="pw-input" name="password" autocomplete="current-password" placeholder="Password">
      <button id="unlock-btn" type="submit">&#x30ED;&#x30C3;&#x30AF;&#x89E3;&#x9664;</button>
    </form>
    <div id="status"></div>
  </div>
</div>
<script>
const SALT="{SALT}",IV="{IV}",CT="{CT}",ITER=100000;
const SK_UID='ks_uid',SK_PW='ks_pw';
function b64ToBuffer(b64){{const bin=atob(b64),buf=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)buf[i]=bin.charCodeAt(i);return buf.buffer;}}
async function decryptWith(uid,pw){{
  const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(uid+':'+pw),'PBKDF2',false,['deriveKey']);
  const key=await crypto.subtle.deriveKey({{name:'PBKDF2',salt:b64ToBuffer(SALT),iterations:ITER,hash:'SHA-256'}},km,{{name:'AES-CBC',length:256}},false,['decrypt']);
  const dec=await crypto.subtle.decrypt({{name:'AES-CBC',iv:b64ToBuffer(IV)}},key,b64ToBuffer(CT));
  return new TextDecoder('utf-8').decode(dec);
}}
function renderDecrypted(html){{
  [SK_UID,SK_PW].forEach(function(k){{var v=sessionStorage.getItem(k);if(v)localStorage.setItem(k,v);}});
  document.documentElement.style.cssText='height:100%;margin:0;padding:0;overflow:hidden;';
  document.body.innerHTML='';
  document.body.style.cssText='height:100%;margin:0;padding:0;overflow:hidden;';
  var f=document.createElement('iframe');
  f.style.cssText='border:none;width:100%;height:100%;display:block;';
  f.srcdoc=html;
  document.body.appendChild(f);
}}
async function unlock(){{
  const uid=document.getElementById('uid-input').value.trim(),pw=document.getElementById('pw-input').value;
  if(!uid||!pw)return;
  const btn=document.getElementById('unlock-btn'),st=document.getElementById('status');
  btn.disabled=true;st.className='working';st.textContent='\\u5FA9\\u53F7\\u4E2D\\u2026';
  try{{const html=await decryptWith(uid,pw);sessionStorage.setItem(SK_UID,uid);sessionStorage.setItem(SK_PW,pw);localStorage.setItem(SK_UID,uid);localStorage.setItem(SK_PW,pw);renderDecrypted(html);}}
  catch(e){{sessionStorage.removeItem(SK_UID);sessionStorage.removeItem(SK_PW);localStorage.removeItem(SK_UID);localStorage.removeItem(SK_PW);st.className='';st.textContent='\\u30D1\\u30B9\\u30EF\\u30FC\\u30C9\\u304C\\u9055\\u3044\\u307E\\u3059';btn.disabled=false;}}
}}
(async()=>{{
  const uid=sessionStorage.getItem(SK_UID)||localStorage.getItem(SK_UID),pw=sessionStorage.getItem(SK_PW)||localStorage.getItem(SK_PW);
  if(uid&&pw){{try{{const html=await decryptWith(uid,pw);renderDecrypted(html);return;}}catch(e){{sessionStorage.removeItem(SK_UID);sessionStorage.removeItem(SK_PW);localStorage.removeItem(SK_UID);localStorage.removeItem(SK_PW);}}}}
  document.getElementById('lock-title').textContent='\\u30A2\\u30AF\\u30BB\\u30B9\\u5236\\u9650';
  document.getElementById('lock-sub').innerHTML=`{back_btn}`;
  document.getElementById('login-form').style.display='block';
  document.getElementById('uid-input').focus();
}})();
</script></body></html>"""

# ── KPI Auto-Counting ───────────────────────────────────────────────────
def parse_mail_date(date_str):
    try:
        clean = re.sub(r'\s*\([A-Z]+\)\s*$', '', date_str.strip())
        return parsedate_to_datetime(clean)
    except:
        return None

def is_this_month(date_str, year, month):
    dt = parse_mail_date(date_str)
    return dt and dt.year == year and dt.month == month

def count_trials(trial_data, year, month):
    dummy_emails = {'manin@agniyoga.jp', 'kyoko.watanabe@agniyoga.jp'}
    dummy_names  = {'満員', 'テスト'}
    test_keywords = ('テスト', '金坂')  # 社内テスト予約（氏名に含まれる場合はカウント対象外）
    results = []
    for item in trial_data:
        if '体験受付:' not in item.get('from', ''):
            continue
        if not is_this_month(item['date'], year, month):
            continue
        body = item.get('body', '')
        name_m = re.search(r'【氏名】(.+)', body)
        mail_m = re.search(r'【メールアドレス】(.+)', body)
        if name_m:
            name = name_m.group(1).strip()
            mail = mail_m.group(1).strip() if mail_m else ''
            if (name not in dummy_names and mail not in dummy_emails
                    and not any(k in name for k in test_keywords)):
                results.append({
                    'subject':  item['subject'],
                    'date':     item.get('date', ''),
                    'from':     item.get('from', ''),
                    'body':     body,
                })
    return results

def count_setsumeikai(school_data, year, month):
    results = []
    for item in school_data:
        if '説明会予約受付:' not in item.get('from', ''):
            continue
        if not is_this_month(item['date'], year, month):
            continue
        results.append({
            'subject': item['subject'],
            'date':    item.get('date', ''),
            'from':    item.get('from', ''),
            'body':    item.get('body', ''),
        })
    return results

def count_training_contracts(unified_mails, year, month):
    """クラウドサイン等で届く養成講座申込書の合意締結完了通知をKPI「養成講座」としてカウントする。
    統合メール一覧（AI仕分け済み）のtitle/sub/detailに「講座」を含み、
    差出人ドメインがcloudsign.jpのものを対象とする。"""
    results = []
    for m in unified_mails:
        if m.get('domain') != 'cloudsign.jp':
            continue
        text = f"{m.get('title','')} {m.get('sub','')} {m.get('detail','')}"
        if '講座' not in text:
            continue
        if not is_this_month(m.get('date', ''), year, month):
            continue
        results.append({
            'subject': m.get('title', ''),
            'date':    m.get('date', ''),
            'from':    m.get('from_info', ''),
            'body':    m.get('detail', ''),
        })
    return results

def count_shiryo(school_data, year, month):
    results = []
    for item in school_data:
        subj = item.get('subject', '')
        if not subj.startswith('受付日'):
            continue
        if not is_this_month(item['date'], year, month):
            continue
        results.append({
            'subject': subj,
            'date':    item.get('date', ''),
            'from':    item.get('from', ''),
            'body':    item.get('body', ''),
        })
    return results

# ── Routine Schedule ────────────────────────────────────────────────────
WEEKDAY_JP = ['月','火','水','木','金','土','日']

# type: weekly / monthly_day / monthly_weekday / yearly
ROUTINES = [
    {'id':'wmtg',    'name':'wmtg（AGNIYOGA社内）14:00〜',     'icon':'🔄', 'badge':'badge-blue',
     'type':'weekly','weekday':4,  # 金曜
     'detail':'毎週金曜 14:00〜（Google Meet: meet.google.com/bqh-gtwr-wxh）'},
    {'id':'shakaihoken', 'name':'社会保険料データDL（e-Gov電子申請・前月分）', 'icon':'📋', 'badge':'badge-orange',
     'type':'monthly_day','day':5,'detail':'e-Gov電子申請サイトから前月分をダウンロード'},
    {'id':'zip',         'name':'ZIP精算（前月分）',                          'icon':'💴', 'badge':'badge-orange',
     'type':'monthly_day','day':5,'detail':'ZIPシステムで前月分を精算する'},
    {'id':'payjp',       'name':'PAY.JPでシステム請求書精算（定期精算メンテ）','icon':'💳', 'badge':'badge-orange',
     'type':'monthly_day','day':5,'detail':'PAY.JPにログインしてシステム請求書を確認・精算'},
    {'id':'kintai',      'name':'出勤簿の提出',                               'icon':'📝', 'badge':'badge-orange',
     'type':'monthly_day','day':5,'detail':'前月分の出勤簿を提出する'},
    {'id':'shotokuzei',  'name':'所得税支払い（前月分）',                      'icon':'💰', 'badge':'badge-orange',
     'type':'monthly_day','day':5,'detail':'前月分の所得税を納付書または電子納税で支払う'},
    {'id':'juminzei',    'name':'住民税支払い（前月分）',                      'icon':'💰', 'badge':'badge-orange',
     'type':'monthly_day','day':5,'detail':'前月分の住民税を支払う'},
    {'id':'yukyu',       'name':'有給管理',                                   'icon':'📅', 'badge':'badge-orange',
     'type':'monthly_day','day':5,'detail':'有給取得状況を確認・更新する'},
    {'id':'tax',     'name':'税金支払い（所得・住民税）',         'icon':'💰', 'badge':'badge-orange',
     'type':'monthly_day','day':9,
     'detail':'毎月9日頃。納付書または電子納税で支払う。'},
    {'id':'kure',    'name':'クレ対応・CB/AMEXダウンロード',    'icon':'💳', 'badge':'badge-orange',
     'type':'monthly_day','day':10,
     'detail':'毎月10日。CB・AMEXをDLして精算。VISAは現金小口。（CB/AMEXは1,4,7,10月のみ）'},
    {'id':'advmtg',  'name':'広告MTG 13:00〜',                 'icon':'📊', 'badge':'badge-blue',
     'type':'monthly_weekday','week':2,'weekday':4,  # 第2金曜
     'detail':'毎月第2金曜 13:00〜 AGNIYOGA広告MTG'},
    {'id':'hoshin',  'name':'アクティビア方針まとめ 14:30〜',   'icon':'📋', 'badge':'badge-blue',
     'type':'monthly_weekday','week':1,'weekday':2,  # 第1水曜
     'detail':'毎月第1水曜 14:30〜 各自確認してWMTGスレッドに返信'},
    {'id':'aws',     'name':'AWS Savings Plans 確認',           'icon':'☁️', 'badge':'badge-red',
     'type':'yearly','month':8,'day':3,
     'detail':'毎年8月3日頃。更新・確認。次回 8/20に作り直し予定。'},
]

def _nth_weekday(year, month, weekday, n):
    """月の第N weekday (0=月) の date を返す"""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7*(n-1))

def upcoming_routines(today_dt, days=7):
    end_dt = today_dt + timedelta(days=days)
    results = []
    for r in ROUTINES:
        t = r['type']
        if t == 'weekly':
            diff = (r['weekday'] - today_dt.weekday()) % 7
            due = today_dt + timedelta(days=diff)
            if due <= end_dt:
                results.append((due, r))
        elif t == 'monthly_day':
            for dm in [0, 1]:
                m = today_dt.month + dm
                y = today_dt.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                try:
                    due = date(y, m, r['day'])
                except ValueError:
                    continue
                if today_dt <= due <= end_dt:
                    results.append((due, r))
                    break
        elif t == 'monthly_weekday':
            for dm in [0, 1]:
                m = today_dt.month + dm
                y = today_dt.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                due = _nth_weekday(y, m, r['weekday'], r['week'])
                if today_dt <= due <= end_dt:
                    results.append((due, r))
                    break
        elif t == 'yearly':
            for dy in [0, 1]:
                try:
                    due = date(today_dt.year + dy, r['month'], r['day'])
                except ValueError:
                    continue
                if today_dt <= due <= end_dt:
                    results.append((due, r))
                    break
    results.sort(key=lambda x: x[0])
    return results

def generate_routine_tasks(today_dt):
    items = upcoming_routines(today_dt)
    if not items:
        return ''
    parts = []
    for due, r in items:
        rid = f'r-{r["id"]}-{due.strftime("%Y%m%d")}'
        if due == today_dt:
            label = f'今日（{WEEKDAY_JP[due.weekday()]}）'
        elif due == today_dt + timedelta(days=1):
            label = f'明日（{WEEKDAY_JP[due.weekday()]}）'
        else:
            label = f'{due.month}/{due.day}（{WEEKDAY_JP[due.weekday()]}）'
        detail_lines = he(r.get('detail','')).replace('\n','<br>')
        parts.append(f"""        <div class="task-card routine" id="{rid}">
          <div class="task-header" onclick="toggleDetail('{rid}')">
            <div class="task-title"><span class="badge {r['badge']}">🔄</span> {label} {he(r['name'])}</div>
            <button class="archive-btn" onclick="event.stopPropagation();archiveItem('{rid}')">✓</button>
          </div>
          <div class="task-detail">{detail_lines}</div>
        </div>""")
    return '\n'.join(parts)

def _schedule_category(summary):
    """予定タイトルから種類を推定し、(ラベル, バッジ色キー) を返す（カレンダー色分け用）。
    裁判関連は最重要として別扱いする（呼び出し側でis_criticalとして強調表示）。"""
    s = summary or ''
    if '裁判' in s:
        return ('裁判', 'red')
    if 'リボーン' in s or '審判' in s or 'サッカー' in s:
        return ('サッカー', 'green')
    if 'mtg' in s.lower() or '会議' in s or 'ミーティング' in s:
        return ('ミーティング', 'blue')
    if '✈️' in s or '出張' in s:
        return ('出張', 'teal')
    return ('プライベート', 'purple')

def _parse_event_dt(ev):
    """カレンダーイベントのstart文字列をJSTのdatetimeに変換（終日イベントは日付のみ）。パース不可はNone。"""
    start = ev.get('start', '')
    if not start:
        return None
    try:
        if ev.get('all_day'):
            return datetime.strptime(start[:10], '%Y-%m-%d')
        return datetime.fromisoformat(start.replace('Z', '+00:00')).astimezone(JST).replace(tzinfo=None)
    except Exception:
        return None

def generate_schedule_section(events, today_dt):
    if not events:
        return '    <div class="card"><div style="font-size:12px;color:var(--sub);padding:6px 0;">予定なし、または未取得</div></div>'

    by_month = {}
    for ev in events:
        if '定休日' in ev.get('summary', ''):
            continue  # 「金坂定休日」等の定休日表示は不要（ユーザー指示）
        dt = _parse_event_dt(ev)
        if dt is None:
            continue
        by_month.setdefault((dt.year, dt.month), []).append((dt, ev))

    parts = []
    for (year, month), items in sorted(by_month.items()):
        parts.append(f'    <div class="card">\n      <div class="card-title">🗓 {month}月</div>')
        for dt, ev in items:
            is_today = dt.date() == today_dt
            date_cls = 'schedule-date today' if is_today else 'schedule-date'
            date_label = f'{dt.month}/{dt.day}（{WEEKDAY_JP[dt.weekday()]}）'
            time_label = '' if ev.get('all_day') else dt.strftime('%H:%M〜')
            loc = f'　@{he(ev["location"])}' if ev.get('location') else ''
            summary_text = ev.get('summary', '')
            cat_label, cat_color = _schedule_category(summary_text)
            is_critical = (cat_label == '裁判')
            tag_html = f'<span class="schedule-tag badge-{cat_color}">{cat_label}</span> '
            summary_html = f'<strong style="color:var(--red);">⚠️ {he(summary_text)}</strong>' if is_critical else he(summary_text)
            item_cls = 'schedule-item schedule-critical' if is_critical else 'schedule-item'
            ev_id_js = he_attr(ev.get('id', '').replace('\\', '\\\\').replace("'", "\\'"))
            summary_js = he_attr(summary_text.replace('\\', '\\\\').replace("'", "\\'"))
            del_btn = (f'<button class="archive-btn cal-del-btn" title="Googleカレンダーからも削除する" '
                       f'onclick="calDeleteEvent(\'{ev_id_js}\',\'{summary_js}\',this)">🗑</button>') if ev.get('id') else ''
            parts.append(f"""      <div class="{item_cls}">
        <div class="{date_cls}">{date_label}</div>
        <div class="schedule-content">{tag_html}{summary_html} {time_label}{loc}</div>
        {del_btn}
      </div>""")
        parts.append('    </div>')
    return '\n'.join(parts)

def generate_today_calendar_tasks(events, today_dt):
    """今日のカレンダー予定を「タスク一覧」にもタスクカードとして追加表示する
    （ユーザー要望：カレンダーの今日の予定はタスク一覧にも入れて）。
    ✓ボタンはarchiveItem()経由でdone_state.jsonに記録される既存の仕組みをそのまま使う。"""
    todays = []
    for ev in events:
        if '定休日' in ev.get('summary', ''):
            continue
        dt = _parse_event_dt(ev)
        if dt is None or dt.date() != today_dt:
            continue
        todays.append((dt, ev))
    if not todays:
        return ''
    todays.sort(key=lambda x: x[0])
    parts = []
    for dt, ev in todays:
        eid = ev.get('id', '')
        tid = f'cal-{eid}' if eid else f'cal-{dt.strftime("%Y%m%d%H%M")}'
        summary_text = ev.get('summary', '')
        cat_label, cat_color = _schedule_category(summary_text)
        is_critical = (cat_label == '裁判')
        time_label = '' if ev.get('all_day') else dt.strftime('%H:%M〜')
        tag_html = f'<span class="schedule-tag badge-{cat_color}">{cat_label}</span> '
        title_html = f'<strong style="color:var(--red);">⚠️ {he(summary_text)}</strong>' if is_critical else he(summary_text)
        cls = 'task-card extra schedule-critical' if is_critical else 'task-card extra'
        parts.append(f"""        <div class="{cls}" id="{tid}">
          <div class="task-header">
            <div class="task-title">{tag_html}{title_html} {time_label}</div>
            <button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('{tid}')">✓</button>
          </div>
        </div>""")
    return '\n'.join(parts)

# ── HTML Generators ─────────────────────────────────────────────────────
def he(s):
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;').replace('\n','<br>')

def he_attr(s):
    """HTML属性値埋め込み用（改行は<br>化せずそのまま保持＝コピー機能のdata属性等に使用）"""
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def _extract_sender_label(from_info):
    """from_info（"差出人：ノジマ <info@...>　→　..."）から表示名部分だけを抜き出す
    （非表示リストで送信元を人間が分かる名前で表示するため）。"""
    m = re.search(r'差出人：(.+?)\s*<', from_info or '')
    if m:
        return m.group(1).strip()
    m2 = re.search(r'差出人：(\S+)', from_info or '')
    return m2.group(1).strip() if m2 else ''

ACCOUNT_LABELS = {
    'kanesaka_activia': ('Activia', 'acct-activia'),
    'kanesaka_agni': ('個人', 'acct-kanesaka-agni'),
    'agniyoga_ad': ('広告用', 'acct-agniyoga-ad'),
    'zipyoga': ('ZIP問合せ', 'acct-zipyoga'),
    'kanesaka_agniyoga': ('agniyoga', 'acct-kanesaka-agniyoga'),
}
CATEGORY_CLASS = {
    'action': 'mail-urgent',
    'info': 'mail-info',
    'unclear': 'mail-unclear',
    'maybe_spam': 'mail-maybe-spam',
}
CATEGORY_ORDER = {'action': 0, 'info': 1, 'unclear': 2, 'maybe_spam': 3}

def _format_mail_datetime(date_str):
    dt = parse_mail_date(date_str)
    if dt:
        return f'{dt.month}/{dt.day} {dt.strftime("%H:%M")}'
    return he(date_str[:16]) if date_str else ''

def generate_unified_mail_section(mails, filter_categories=None):
    """全アカウント統合のメール一覧をレンダリングする。
    filter_categoriesを指定すると該当カテゴリのみ（ファーストビューの「要対応」抜粋・カテゴリ別の枠等に使用）。"""
    items = mails
    if filter_categories:
        items = [m for m in items if m.get('category') in filter_categories]
    if not items:
        return '<div style="font-size:12px;color:var(--sub);padding:6px 0;">該当メールなし</div>'
    items = sorted(items, key=lambda m: CATEGORY_ORDER.get(m.get('category'), 9))
    parts = []
    for m in items:
        mid = m['id']
        cls = CATEGORY_CLASS.get(m.get('category'), 'mail-info')
        acct_label, acct_cls = ACCOUNT_LABELS.get(m.get('account', ''), ('?', ''))
        domain = he(m.get('domain', ''))
        account = he(m.get('account', ''))
        category = he(m.get('category', 'unclear'))
        sender_label = he_attr(_extract_sender_label(m.get('from_info', '')).replace('\\', '\\\\').replace("'", "\\'"))
        title_js = he_attr(m.get('title', '').replace('\\', '\\\\').replace("'", "\\'"))
        meta = f'{_format_mail_datetime(m.get("date",""))}　→　{he(m.get("to",""))}'
        block_btn = (f'<button class="archive-btn btn-x" title="今後この送信元を完全に非表示にする（全カテゴリ）" '
                     f'onclick="event.stopPropagation();blockUnifiedSender(\'{account}\',\'{domain}\',\'{category}\',\'{sender_label}\')">✕</button>'
                     if domain else '')
        block_cat_btn = (f'<button class="archive-btn btn-triangle" title="今後この送信元の「{category}」カテゴリだけ非表示にする" '
                          f'onclick="event.stopPropagation();blockCategoryUnifiedSender(\'{account}\',\'{domain}\',\'{category}\',\'{sender_label}\')">△</button>'
                          if domain else '')
        add_task_btn = (f'<button class="archive-btn btn-add-task" title="タスク一覧に追加" '
                         f'onclick="event.stopPropagation();addMailToTaskList(\'{mid}\',\'{title_js}\',this)">📋+</button>')
        hide_btn = (f'<button class="archive-btn btn-hide" title="この1通だけ非表示にする（AI判定・学習には影響しません）" '
                     f'onclick="event.stopPropagation();justHideMail(\'{mid}\')">隠す</button>')
        recommend = he(m.get('recommend', ''))
        recommend_html = f'<div class="mail-recommend">💡 {recommend}</div>' if recommend else ''
        adv = ADVICE_STORE.get(mid)
        advice_btn = ('' if adv and not adv.get('error') else
                      f'<button class="archive-btn btn-advice" title="AIがWeb調査してスパムか本物か・どう対応すべきかをアドバイスします（依頼後、次のメール反映時に生成）" '
                      f'onclick="event.stopPropagation();requestAdvice(\'{mid}\',this)">🤖AI</button>')
        if adv:
            if adv.get('error'):
                advice_html = (f'<div class="mail-advice">🤖 AIアドバイス（{he(adv.get("checked",""))}）：'
                               f'{he(adv.get("error",""))}</div>')
            else:
                advice_html = (f'<div class="mail-advice">🤖 <b>AIアドバイス</b>（{he(adv.get("checked",""))}）　'
                               f'スパム可能性：<b>{he(adv.get("spam_risk",""))}</b>　／　{he(adv.get("genuine",""))}<br>'
                               f'{he(adv.get("summary",""))}<br>'
                               f'<b>対応：</b>{he(adv.get("advice",""))}<br>'
                               f'<span class="advice-evidence">根拠：{he(adv.get("evidence",""))}</span></div>')
        else:
            advice_html = ''
        copy_text = he_attr(f"{m.get('from_info','')}\n\n{m.get('detail','')}")
        parts.append(f"""        <div class="mail-item {cls}" id="{mid}" data-domain="{domain}" data-account="{account}" data-category="{category}">
          <div class="mail-header" onclick="toggleDetail('{mid}')">
            <div class="mail-btns">
            {block_btn}
            {block_cat_btn}
            {advice_btn}
            {add_task_btn}
            {hide_btn}
            <button class="archive-btn btn-check" title="この1件だけ完了・非表示にする（送信元は今後も表示されます）" onclick="event.stopPropagation();archiveItem('{mid}')">✓</button>
            </div>
            <div class="mail-title"><span class="acct-badge {acct_cls}">{acct_label}</span> {he(m.get('title',''))}</div>
          </div>
          <div class="mail-to">{meta}</div>
          <div class="mail-sub">{he(m.get('sub',''))}</div>
          {recommend_html}
          {advice_html}
          <div class="mail-detail"><button class="copy-btn" title="メール内容をコピー" onclick="event.stopPropagation();copyMailText(this)" data-copy="{copy_text}">📋 コピー</button><div class="detail-from">{he(m.get('from_info',''))}</div><div class="detail-body">{he(m.get('detail',''))}</div></div>
        </div>""")
    return '\n'.join(parts)

def _mail_card(item):
    """メール1件分のポップアップHTML"""
    subj = he(item.get('subject',''))
    date = he(item.get('date','')[:16])  # "Thu, 01 Jul 2026 12:34" くらいまで
    frm  = he(item.get('from',''))
    # 本文の【キー】値 を行ごとにパース → 見やすく整形
    body = item.get('body','')
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'【(.+?)】\s*(.+)', line)
        if m:
            rows.append(f'<tr><th>{he(m.group(1))}</th><td>{he(m.group(2).strip())}</td></tr>')
        else:
            rows.append(f'<tr><td colspan="2" class="pop-raw">{he(line)}</td></tr>')
    tbl = f'<table class="pop-table">{"".join(rows)}</table>' if rows else ''
    return (f'<div class="pop-mail-card">'
            f'<div class="pop-mail-subj">{subj}</div>'
            f'<div class="pop-mail-meta">{date}　{frm}</div>'
            f'{tbl}'
            f'</div>')

def _extract_name(item):
    body = item.get('body', '')
    for pat in [r'【氏名】(.+)', r'【お名前】(.+)', r'【名前】(.+)', r'【申込者】(.+)']:
        m = re.search(pat, body)
        if m:
            return m.group(1).strip()[:14]
    return item.get('subject', '')[:20]

def _extract_studio(item):
    m = re.search(r'【スタジオ】(.+)', item.get('body', ''))
    if m:
        return m.group(1).strip().replace('スタジオ', '')
    m = re.search(r'体験受付:(.+?)<', item.get('from', ''))
    return m.group(1).strip().replace('スタジオ', '') if m else ''

def _extract_utm_source(item):
    """[Last/First Touch]ブロックのsource値を取得。
    sourceが空欄の場合、旧実装は\\sが改行もまたいでマッチしてしまい次の行の
    "medium:"という文字列自体を誤ってsource値として拾ってしまうバグがあった
    （[ \\t]*かつ行末[^\\r\\n]*に限定して同一行内のみを見るよう修正）。"""
    body = item.get('body', '')
    m = re.search(r'\[Last Touch\][^\[]*?source:[ \t]*([^\r\n]*)', body)
    val = m.group(1).strip() if m else ''
    if not val:
        m2 = re.search(r'\[First Touch\][^\[]*?source:[ \t]*([^\r\n]*)', body)
        val = m2.group(1).strip() if m2 else ''
    return val

def _extract_date_short(item):
    dt = parse_mail_date(item.get('date', ''))
    return f'{dt.month}/{dt.day}' if dt else '?'

def generate_kpi_section(trials, shiryo_list, setsumeikai_list, n_training, kpi_note, today, month):
    n_trial       = len(trials)
    n_shiryo      = len(shiryo_list)
    n_setsumeikai = len(setsumeikai_list)

    def mail_rows(items, prefix, show_source=False):
        if not items:
            return '<div class="kpi-mail-empty">今月はまだありません</div>'
        parts = []
        for i, item in enumerate(items):
            rid = f'{prefix}-{i}'
            name = he(_extract_name(item))
            if show_source:
                studio = _extract_studio(item)
                if studio:
                    name += he(f'／{studio}')
                utm_source = _extract_utm_source(item)
                source_labels = {
                    'google': 'Google', 'google-ads': 'Google広告', 'googleads': 'Google広告',
                    'instagram': 'Instagram', 'facebook': 'Facebook', 'ig': 'Instagram',
                    'line': 'LINE', 'yahoo': 'Yahoo', 'email': 'メール', 'mail': 'メール',
                    'direct': '直接', 'organic': '自然検索', 'referral': '紹介サイト',
                }
                # source欄が空＝流入元記録なし＝直接アクセス(直接)として明示表示する
                label = source_labels.get(utm_source.lower(), utm_source) if utm_source else '直接'
                name += f' <span style="font-size:12px;font-weight:bold;color:var(--blue);">【{he(label)}】</span>'
            ds   = he(_extract_date_short(item))
            detail = _mail_card(item)
            parts.append(
                f'<div class="kpi-mail-row" onclick="toggleKpiDetail(\'{rid}\')">'
                f'<span class="kpi-mail-date">{ds}</span>'
                f'<span class="kpi-mail-name">{name}</span>'
                f'<span class="kpi-mail-chevron">▸</span></div>'
                f'<div class="kpi-mail-detail" id="{rid}">{detail}</div>'
            )
        return ''.join(parts)

    t_rows = mail_rows(trials,       'km-trial', show_source=True)
    s_rows = mail_rows(shiryo_list,  'km-shiryo', show_source=True)
    e_rows = mail_rows(setsumeikai_list, 'km-setsu', show_source=True)

    return f"""      <div class="kpi-grid">
        <div class="card kpi-card"><div class="kpi-label">📧 体験</div><div class="kpi-val" style="color:var(--teal);">{n_trial}</div><div class="kpi-unit">件</div></div>
        <div class="card kpi-card"><div class="kpi-label">📄 資料請求</div><div class="kpi-val" style="color:var(--blue);">{n_shiryo}</div><div class="kpi-unit">件</div></div>
        <div class="card kpi-card"><div class="kpi-label">📅 説明会</div><div class="kpi-val" style="color:var(--orange);">{n_setsumeikai}</div><div class="kpi-unit">件</div></div>
        <div class="card kpi-card"><div class="kpi-label">🎓 養成講座</div><div class="kpi-val" style="color:var(--purple);">{n_training}</div><div class="kpi-unit">件</div></div>
      </div>
      <div style="font-size:11px;color:var(--sub);margin:4px 0 6px;text-align:center;">{today}時点　{he(kpi_note)}</div>
      <div class="card kpi-mail-list">
        <div class="kpi-cat-head">📧 体験メール（{n_trial}件）</div>
        {t_rows}
        <div class="kpi-cat-head">📄 資料請求（{n_shiryo}件）</div>
        {s_rows}
        <div class="kpi-cat-head">📅 説明会予約（{n_setsumeikai}件）</div>
        {e_rows}
      </div>"""

# ── Dashboard HTML Template ─────────────────────────────────────────────
# ###MAIL_SECTION### と ###KPI_SECTION### が動的に置換される
# ###UPDATED### = 更新日時、 ###MONTH### = 今月(例:7月)
DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>金坂 タスク管理</title>
<style>
  :root {
    --bg: #f4f5f7; --card: #ffffff;
    --red: #c0392b; --orange: #e67e22; --green: #27ae60;
    --blue: #2980b9; --gray: #7f8c8d; --purple: #8e44ad;
    --teal: #16a085; --text: #2c3e50; --sub: #636e72; --border: #dfe6e9;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Hiragino Sans','Meiryo',sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }
  header { background: #2c3e50; color: white; padding: 14px 20px; display: flex; flex-direction: column; gap: 10px; }
  header h1 { font-size: 16px; font-weight: bold; white-space: normal; word-break: break-word; }
  .header-buttons { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .btn-header { padding: 8px 14px; font-size: 12px; font-weight: bold; background: #34495e; color: white; border: 1px solid #5d6d7b; border-radius: 6px; cursor: pointer; transition: background 0.2s; text-decoration: none; display: inline-block; }
  .btn-header:hover { background: #455a64; }
  header .updated { font-size: 11px; color: #b2bec3; }
  header .rebuild-btn { font-size: 12px; background: #3b82f6; color: white; border: none; border-radius: 6px; padding: 5px 12px; cursor: pointer; text-decoration: none; display: inline-block; }
  header .rebuild-btn:hover { background: #2563eb; }
  .container { max-width: 1100px; margin: 0 auto; padding: 14px; }
  .first-view { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
  .fv-col { display: flex; flex-direction: column; height: 440px; }
  .scroll-card { flex: 1; min-height: 0; overflow-y: auto; }
  .fv-col .kpi-mail-list { flex: 1; min-height: 0; overflow-y: auto; }
  .kpi-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .kpi-card { text-align: center; padding: 12px 8px; }
  .kpi-label { font-size: 12px; color: var(--sub); margin-bottom: 4px; }
  .kpi-val { font-size: 30px; font-weight: bold; line-height: 1.1; }
  .kpi-unit { font-size: 11px; color: var(--sub); margin-top: 2px; }
  .archive-btn { flex-shrink: 0; background: transparent; border: 1px solid rgba(0,0,0,0.15); border-radius: 4px; padding: 1px 7px; font-size: 11px; cursor: pointer; color: var(--sub); transition: all 0.15s; }
  .archive-btn:hover { background: var(--green); color: white; border-color: var(--green); }
  .archive-btn.btn-check.pressed { background: var(--green); color: white; border-color: var(--green); font-weight: bold; }
  .archive-btn.btn-x.pressed { background: #dc2626; color: white; border-color: #dc2626; font-weight: bold; }
  .archive-btn.btn-triangle.pressed { background: #d97706; color: white; border-color: #d97706; font-weight: bold; }
  .archive-btn.btn-advice:hover { background: #6366f1; color: white; border-color: #6366f1; }
  .mail-advice { font-size: 11px; line-height: 1.8; margin-top: 6px; padding: 8px 10px; border-radius: 6px; background: #eef2ff; border: 1px solid #c7d2fe; color: #3730a3; }
  .mail-advice .advice-evidence { color: #6366f1; font-size: 10px; }
  .mail-header { display: flex; flex-direction: column; align-items: stretch; gap: 3px; margin-bottom: 3px; cursor: pointer; }
  .mail-btns { display: flex; justify-content: flex-end; gap: 6px; flex-wrap: wrap; }
  .mail-header:hover .mail-title { color: var(--blue); }
  .mail-item { padding: 8px 10px; border-radius: 6px; margin-bottom: 2px; border-left: 4px solid; position: relative; }
  .mail-urgent { background: #ffeaa7; border-color: #e17055; }
  .mail-info   { background: #e8f4fd; border-color: var(--blue); }
  .mail-unclear { background: #f3e8ff; border-color: #a78bfa; }
  .mail-maybe-spam { background: #fefce8; border-color: #eab308; border-left-style: dashed; }
  .mail-item .mail-title { font-weight: bold; font-size: 12px; line-height: 1.3; transition: color 0.15s; }
  .mail-item .mail-sub   { font-size: 11px; color: var(--sub); margin-top: 2px; line-height: 1.4; }
  .mail-to { font-size: 10px; color: var(--sub); margin-top: 1px; }
  .mail-recommend { font-size: 11px; color: #92400e; background: #fffbeb; border: 1px solid #fde68a; border-radius: 5px; padding: 4px 7px; margin-top: 5px; line-height: 1.4; }
  .acct-badge { display: inline-block; font-size: 9px; font-weight: bold; padding: 1px 5px; border-radius: 8px; margin-right: 4px; vertical-align: middle; }
  .acct-activia { background: #dbeafe; color: #1d4ed8; }
  .acct-kanesaka-agni { background: #dcfce7; color: #15803d; }
  .acct-agniyoga-ad { background: #f3e8ff; color: #7e22ce; }
  .acct-zipyoga { background: #ffe4e6; color: #be123c; }
  .acct-kanesaka-agniyoga { background: #fff7ed; color: #c2410c; }
  .mail-detail { display: none; max-height: 260px; overflow-y: auto; font-size: 11px; line-height: 1.7; margin-top: 8px; padding: 8px 10px; border: 1px solid rgba(0,0,0,0.12); border-radius: 6px; background: #f8fafc; color: var(--text); }
  .mail-item.open .mail-detail { display: block; }
  .mail-detail .detail-from { color: var(--sub); margin-bottom: 4px; }
  .mail-detail .detail-body { margin-top: 4px; }
  .copy-btn { float: right; font-size: 10px; padding: 3px 8px; border: 1px solid var(--border); border-radius: 5px; background: #fff; color: var(--sub); cursor: pointer; }
  .copy-btn:hover { background: #f1f5f9; }
  .mail-all-link { margin-top: 8px; text-align: right; border-top: 1px solid var(--border); padding-top: 7px; font-size: 12px; }
  .mail-all-link a { color: var(--blue); text-decoration: none; }
  .task-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 6px; cursor: pointer; }
  .task-header:hover .task-title { color: var(--blue); }
  .task-card { margin-bottom: 8px; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border); }
  .task-card.active  { border-left: 4px solid var(--green);  background: #f6fffc; }
  .task-card.paused  { border-left: 4px solid var(--gray);   background: #fafafa; }
  .task-card.done    { border-left: 4px solid var(--blue);   background: #f0f8ff; opacity: 0.7; }
  .task-card.routine { border-left: 4px solid var(--orange); background: #fff8f0; }
  .task-card.extra   { border-left: 4px solid var(--teal);   background: #f7fffe; }
  .task-card.extra.urgency-urgent { border-left-color: var(--red);    background: #fff5f5; }
  .task-card.extra.urgency-soon   { border-left-color: var(--orange); background: #fff8f0; }
  .btn-add-task { color: var(--teal); border-color: var(--teal); }
  .btn-add-task.pressed { background: var(--teal); color: white; }
  .btn-hide { color: #6b7280; border-color: #9ca3af; font-size: 10px; }
  .cal-del-btn { margin-left: auto; flex: none; color: #9ca3af; border-color: #d1d5db; font-size: 11px; }
  .task-title { font-size: 13px; font-weight: bold; margin-bottom: 4px; display: flex; align-items: center; gap: 6px; transition: color 0.15s; }
  .task-next  { font-size: 11px; color: var(--sub); line-height: 1.4; margin-top: 4px; }
  .task-next strong { color: var(--orange); }
  .task-detail { display: none; max-height: 260px; overflow-y: auto; font-size: 11px; line-height: 1.7; margin-top: 8px; padding: 8px 10px; border: 1px solid rgba(0,0,0,0.1); border-radius: 6px; background: #f8fafc; color: var(--text); white-space: pre-wrap; }
  .task-card.open .task-detail { display: block; }
  .routine-label { font-size: 10px; color: var(--sub); text-align: right; margin-bottom: 4px; }
  .grid   { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
  .full   { grid-column: 1 / -1; }
  .card   { background: var(--card); border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
  .routine-cards { display: flex; gap: 20px; flex-wrap: wrap; }
  .routine-cards .routine-col { flex: 1; min-width: 220px; }
  .card-title { font-size: 12px; font-weight: bold; color: var(--sub); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: bold; }
  .badge-red    { background: #ffeaea; color: var(--red); }
  .badge-orange { background: #fff3e0; color: var(--orange); }
  .badge-green  { background: #e8f5e9; color: var(--green); }
  .badge-blue   { background: #e3f2fd; color: var(--blue); }
  .badge-gray   { background: #f1f2f6; color: var(--gray); }
  .badge-purple { background: #f3e5f5; color: var(--purple); }
  .badge-teal   { background: #e0f2f1; color: var(--teal); }
  .section-head { font-size: 13px; font-weight: bold; color: var(--sub); margin: 14px 0 8px; display: flex; align-items: center; gap: 6px; }
  .section-head::after { content:''; flex:1; height:1px; background:var(--border); }
  .fv-col .section-head { margin-top: 0; }
  .schedule-item { display: flex; gap: 10px; align-items: flex-start; padding: 6px 0; border-bottom: 1px solid var(--border); }
  .schedule-item:last-child { border-bottom: none; }
  .schedule-date { min-width: 60px; font-size: 12px; color: var(--sub); font-weight: bold; }
  .schedule-date.today { color: var(--red); }
  .schedule-date.soon  { color: var(--orange); }
  .schedule-content { flex: 1; font-size: 12px; line-height: 1.5; }
  .schedule-tag { font-size: 10px; padding: 1px 5px; border-radius: 8px; margin-right: 2px; }
  .schedule-critical { background: #fff5f5; border-left: 3px solid var(--red); padding-left: 6px; margin-left: -6px; border-radius: 4px; }
  .countdown-bar { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .countdown-item { text-align: center; background: #f8f9fa; border-radius: 8px; padding: 8px 12px; min-width: 85px; }
  .countdown-item .days  { font-size: 22px; font-weight: bold; color: var(--teal); }
  .countdown-item .label { font-size: 10px; color: var(--sub); margin-top: 2px; }
  .countdown-item .date  { font-size: 10px; color: var(--gray); }
  .countdown-item.urgent   .days { color: var(--orange); }
  .countdown-item.critical .days { color: var(--red); }
  .routine-item { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid var(--border); }
  .routine-item:last-child { border-bottom: none; }
  .routine-name { font-size: 12px; }
  .routine-freq { font-size: 11px; color: var(--sub); text-align: right; min-width: 120px; }
  .log-item { font-size: 11px; color: var(--sub); padding: 3px 0; border-bottom: 1px dotted var(--border); }
  .log-item:last-child { border-bottom: none; }
  .archived { opacity: 1; background: #e5e7eb !important; border-color: #9ca3af !important; }
  .archived .task-title, .archived .mail-title, .archived .routine-name { color: #4b5563; }
  .archived .mail-sub, .archived .task-next, .archived .routine-freq, .archived .detail-from, .archived .detail-body { color: #6b7280; }
  .done-time-badge { font-size: 10px; font-weight: normal; color: #6b7280; margin-left: 6px; }
  .undo-inline-btn { font-size: 10px; padding: 1px 6px; margin-left: 4px; border: 1px solid #9ca3af; border-radius: 4px; background: #fff; color: #374151; cursor: pointer; }
  .undo-inline-btn:hover { background: #f3f4f6; }
  .undo-btn { display:none; font-size:10px; color:var(--blue); background:none; border:1px solid var(--blue); border-radius:3px; cursor:pointer; padding:1px 5px; margin-left:6px; vertical-align:middle; }
  .archived .undo-btn { display:inline; }
  .hist-item { display:flex; gap:8px; padding:5px 0; border-bottom:1px solid var(--border); font-size:12px; color:var(--sub); align-items:baseline; }
  .hist-item:last-child { border-bottom:none; }
  .hist-date { min-width:70px; flex-shrink:0; font-size:11px; }
  .hist-text { flex:1; }
  .kpi-clickable { cursor: pointer; transition: box-shadow .15s, transform .1s; }
  .kpi-clickable:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.15); transform: translateY(-1px); }
  .pop-overlay { display:none; position:fixed; inset:0; background:rgba(15,23,42,0.55); z-index:1000; align-items:center; justify-content:center; }
  .pop-overlay.open { display:flex; }
  .pop-box { background:#fff; border-radius:12px; padding:20px 22px; min-width:280px; max-width:420px; width:90%; max-height:65vh; overflow-y:auto; box-shadow:0 20px 50px rgba(0,0,0,0.3); }
  .pop-title { font-size:14px; font-weight:bold; color:var(--text); margin-bottom:12px; padding-bottom:8px; border-bottom:2px solid var(--border); }
  .pop-item { font-size:12px; color:var(--text); padding:7px 4px; border-bottom:1px solid var(--border); line-height:1.5; }
  .pop-item:last-child { border-bottom:none; }
  .pop-empty { font-size:12px; color:var(--sub); text-align:center; padding:16px 0; }
  .pop-mail-card { border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:10px; background:#fafbfc; }
  .pop-mail-card:last-child { margin-bottom:0; }
  .pop-mail-subj { font-size:12px; font-weight:bold; color:var(--text); margin-bottom:3px; line-height:1.4; }
  .pop-mail-meta { font-size:10px; color:var(--sub); margin-bottom:8px; }
  .pop-table { width:100%; border-collapse:collapse; font-size:12px; }
  .pop-table th { width:90px; text-align:left; color:var(--sub); font-weight:normal; padding:2px 6px 2px 0; vertical-align:top; white-space:nowrap; }
  .pop-table td { color:var(--text); padding:2px 0; line-height:1.5; }
  .pop-table .pop-raw { color:var(--sub); font-size:11px; }
  .kpi-mail-list { padding: 10px 12px; }
  .kpi-cat-head { font-size: 11px; font-weight: bold; color: var(--sub); margin: 10px 0 4px; text-transform: uppercase; letter-spacing: 0.04em; }
  .kpi-cat-head:first-child { margin-top: 0; }
  .kpi-mail-row { display: flex; align-items: center; gap: 6px; padding: 5px 2px; border-bottom: 1px solid var(--border); cursor: pointer; border-radius: 4px; transition: background .1s; }
  .kpi-mail-row:hover { background: #f0f4f8; }
  .kpi-mail-date { min-width: 32px; font-size: 11px; color: var(--sub); flex-shrink: 0; }
  .kpi-mail-name { flex: 1; font-size: 12px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kpi-mail-chevron { font-size: 10px; color: var(--sub); flex-shrink: 0; }
  .kpi-mail-detail { display: none; max-height: 260px; overflow-y: auto; padding: 8px 10px; margin: 4px 0 6px; border: 1px solid var(--border); border-radius: 6px; background: #f8fafc; }
  .kpi-mail-detail.open { display: block; }
  .kpi-mail-empty { font-size: 11px; color: var(--sub); padding: 4px 2px; }
  .rt-add-row { display: flex; justify-content: flex-end; margin-top: 6px; }
  .rt-toggle-btn { font-size: 11px; color: var(--blue); background: transparent; border: 1px solid var(--blue); border-radius: 4px; padding: 2px 10px; cursor: pointer; }
  .rt-form { display: none; background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-top: 8px; }
  .rt-form.open { display: block; }
  .rt-form input, .rt-form select { width: 100%; border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; font-size: 12px; margin-bottom: 6px; outline: none; font-family: inherit; }
  .rt-form input:focus, .rt-form select:focus { border-color: var(--blue); }
  .rt-form-btns { display: flex; gap: 6px; }
  .rt-save-btn { background: var(--blue); color: white; border: none; border-radius: 4px; padding: 5px 14px; font-size: 12px; cursor: pointer; }
  .rt-cancel-btn { background: transparent; color: var(--sub); border: 1px solid var(--border); border-radius: 4px; padding: 5px 10px; font-size: 12px; cursor: pointer; }
  .rt-del-btn { background: transparent; border: none; color: #ccc; cursor: pointer; font-size: 13px; padding: 0 2px; line-height:1; }
  .rt-del-btn:hover { color: var(--red); }
  @media (max-width: 900px) { .first-view { grid-template-columns: 1fr; } .fv-col { height: auto; max-height: 440px; } }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } .grid-3 { grid-template-columns: 1fr; } .routine-cards { flex-direction: column; } }
</style>
</head>
<body>

<script>
const DONE_KEY='ks_done_v1';
function _gd(){try{return JSON.parse(localStorage.getItem(DONE_KEY)||'[]');}catch{return[];}}
function _sd(list){localStorage.setItem(DONE_KEY,JSON.stringify(list));}
const GH_TOKEN='###GITHUB_TOKEN###';
const GH_REPO='kanesaka849/secure-note-page';
const GH_DONE='done_state.json';
async function ghGet(){try{const r=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${GH_DONE}`,{headers:{'Authorization':`token ${GH_TOKEN}`,'Accept':'application/vnd.github.v3+json'}});if(!r.ok)return{list:[],sha:null};const d=await r.json();const l=JSON.parse(decodeURIComponent(escape(atob(d.content.replace(/\\n/g,'')))));return{list:Array.isArray(l)?l:[],sha:d.sha};}catch(e){return{list:[],sha:null};}}
async function ghPut(list,sha){for(let i=0;i<3;i++){try{const b=btoa(unescape(encodeURIComponent(JSON.stringify(list))));const body={message:'sync',content:b};if(sha)body.sha=sha;const r=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${GH_DONE}`,{method:'PUT',headers:{'Authorization':`token ${GH_TOKEN}`,'Content-Type':'application/json','Accept':'application/vnd.github.v3+json'},body:JSON.stringify(body)});if(r.status===200||r.status===201)return;}catch(e){}const g=await ghGet();list=mergeDone(list,g.list);sha=g.sha;_sd(list);}}
async function reflectMail(){
  const btn=document.getElementById('reflect-mail-btn');
  if(!GH_TOKEN){alert('GitHubトークンが設定されていません');return;}
  btn.disabled=true;btn.textContent='⏳ 実行中…';
  try{
    // 埋め込みトークンはContents権限のみ（2026-07-05権限分離）のため
    // workflow_dispatch(要Actions権限)ではなくトリガーファイルのコミットpushで起動する
    const path='ci_trigger/reflect.json';
    let sha=null;
    try{const g=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${path}`,{headers:{'Authorization':`token ${GH_TOKEN}`,'Accept':'application/vnd.github.v3+json'}});if(g.ok)sha=(await g.json()).sha;}catch(e){}
    const body={message:'trigger reflect-mail (dashboard button)',content:btoa(unescape(encodeURIComponent(JSON.stringify({requested:new Date().toISOString()}))))};
    if(sha)body.sha=sha;
    const r=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${path}`,{method:'PUT',headers:{'Authorization':`token ${GH_TOKEN}`,'Content-Type':'application/json','Accept':'application/vnd.github.v3+json'},body:JSON.stringify(body)});
    btn.textContent=(r.status===200||r.status===201)?'✅ 反映開始（数分後に自動更新）':`⚠️ 失敗（${r.status}）`;
  }catch(e){btn.textContent='⚠️ エラー';}
  setTimeout(function(){btn.disabled=false;btn.textContent='🔄 メール反映';},10000);
}
async function loadApiCost(){
  try{
    const r=await fetch(`https://raw.githubusercontent.com/${GH_REPO}/main/api_usage_log.json?_=${Date.now()}`);
    if(!r.ok)return;
    const log=await r.json();
    const total=log.reduce(function(s,x){return s+(x.estimated_cost_usd||0);},0);
    const el=document.getElementById('api-cost-note');
    if(el)el.textContent=`API概算利用料：$${total.toFixed(4)}（直近${log.length}回・目安値。正確な金額はconsole.anthropic.comで確認）`;
  }catch(e){}
}
// 統合メールの手動show/hideは、AIが使うsender_rules.json自体を直接書き換える
// （AIの自動学習と同じ場所に人間の判断も記録し、次回の「メール反映」にも一貫して反映される）
const GH_SENDER_RULES='sender_rules.json';
async function ghGetSenderRules(){try{const r=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${GH_SENDER_RULES}`,{headers:{'Authorization':`token ${GH_TOKEN}`,'Accept':'application/vnd.github.v3+json'}});if(!r.ok)return{rules:{},sha:null};const d=await r.json();return{rules:JSON.parse(decodeURIComponent(escape(atob(d.content.replace(/\\n/g,''))))),sha:d.sha};}catch(e){return{rules:{},sha:null};}}
async function ghPutSenderRules(rules,sha){try{const b=btoa(unescape(encodeURIComponent(JSON.stringify(rules))));const body={message:'update sender rules (manual)',content:b};if(sha)body.sha=sha;const r=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${GH_SENDER_RULES}`,{method:'PUT',headers:{'Authorization':`token ${GH_TOKEN}`,'Content-Type':'application/json','Accept':'application/vnd.github.v3+json'},body:JSON.stringify(body)});return r.status===200||r.status===201;}catch(e){return false;}}
// 送信元ルールの更新は直列キュー＋リトライで行う（✕/△の連打で古いshaのPUTが409になり
// ルールが黙って消える競合を防ぐ。失敗が続いた場合のみ警告を出す）
let _srQueue=Promise.resolve();
function updateSenderRules(mutate){_srQueue=_srQueue.then(function(){return _updateSenderRulesOnce(mutate,3);});return _srQueue;}
async function _updateSenderRulesOnce(mutate,tries){
  for(let i=0;i<tries;i++){
    const r=await ghGetSenderRules();
    const rules=r.rules||{};
    mutate(rules);
    const ok=await ghPutSenderRules(rules,r.sha);
    if(ok)return true;
    await new Promise(function(res){setTimeout(res,700);});
  }
  alert('送信元ルールの保存に失敗しました（通信エラー）。時間をおいてもう一度お試しください');
  return false;
}
// 🤖AIアドバイス依頼：ci_trigger/advice_requests.jsonに依頼を追記コミット→push起動のreflect-mailが
// Web検索つきAI判定（スパムか本物か・推奨対応）を生成し、次回ビルドで該当メールの下に表示される
const GH_ADVICE_REQ='ci_trigger/advice_requests.json';
async function requestAdvice(mid,btn){
  if(!GH_TOKEN){alert('GitHubトークンが設定されていません');return;}
  if(btn){btn.disabled=true;btn.textContent='⏳';}
  try{
    let cur={requests:[]},sha=null;
    try{const g=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${GH_ADVICE_REQ}`,{headers:{'Authorization':`token ${GH_TOKEN}`,'Accept':'application/vnd.github.v3+json'}});if(g.ok){const d=await g.json();sha=d.sha;cur=JSON.parse(decodeURIComponent(escape(atob(d.content.replace(/\\n/g,'')))));}}catch(e){}
    if(!Array.isArray(cur.requests))cur.requests=[];
    if(!cur.requests.some(function(r){return r.id===mid;}))cur.requests.push({id:mid,requested:new Date().toISOString()});
    const body={message:'advice request (dashboard)',content:btoa(unescape(encodeURIComponent(JSON.stringify(cur))))};
    if(sha)body.sha=sha;
    const r=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${GH_ADVICE_REQ}`,{method:'PUT',headers:{'Authorization':`token ${GH_TOKEN}`,'Content-Type':'application/json','Accept':'application/vnd.github.v3+json'},body:JSON.stringify(body)});
    if(r.status===200||r.status===201){if(btn)btn.textContent='依頼済';alert('AIアドバイスを依頼しました。自動でメール反映が実行され、数分後に「🔃 更新」で表示されます。');}
    else{if(btn){btn.disabled=false;btn.textContent='🤖';}alert('依頼に失敗しました（'+r.status+'）');}
  }catch(e){if(btn){btn.disabled=false;btn.textContent='🤖';}alert('依頼に失敗しました');}
}
function _gub(){try{return JSON.parse(localStorage.getItem('ks_unified_hidden_v1')||'[]');}catch{return[];}}  // [{key:"account|domain",label:"ノジマ"}, ...] 完全非表示（×）
function _sub(list){localStorage.setItem('ks_unified_hidden_v1',JSON.stringify(list));}
// 個別非表示：この端末のlocalStorageのみ・sender_rules/done_stateに書かない＝AI判定に影響ゼロ
function _gjh(){try{return JSON.parse(localStorage.getItem('ks_mail_justhidden_v1')||'[]');}catch{return[];}}
function _sjh(l){localStorage.setItem('ks_mail_justhidden_v1',JSON.stringify(l));}
function justHideMail(mid){const l=_gjh();if(l.indexOf(mid)<0){l.push(mid);_sjh(l);}document.querySelectorAll('.mail-item').forEach(function(el){if(el.id===mid)el.style.display='none';});_updateJustHiddenBtn();}
function applyJustHidden(){const l=_gjh();if(l.length){document.querySelectorAll('.mail-item').forEach(function(el){if(l.indexOf(el.id)>=0)el.style.display='none';});}_updateJustHiddenBtn();}
function _updateJustHiddenBtn(){const n=_gjh().length;document.querySelectorAll('.jh-restore-btn').forEach(function(b){b.style.display=n?'':'none';b.textContent='個別非表示 '+n+'件を戻す';});}
function restoreJustHidden(){if(!confirm('個別非表示にしたメールを全て再表示しますか？'))return;_sjh([]);location.reload();}
// カレンダー予定の追加/削除：依頼を ci_trigger/calendar_requests.json にコミット
// → push で reflect-mail 起動 → Actions が Google カレンダーへ反映して再ビルド
function toggleCalForm(){const f=document.getElementById('cal-form');if(f)f.style.display=(f.style.display==='block')?'none':'block';}
async function _calPutRequest(reqObj){
  if(!GH_TOKEN){alert('GitHubトークンが設定されていません');return false;}
  const path='ci_trigger/calendar_requests.json';
  let sha=null,cur={requests:[]};
  try{const g=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${path}`,{headers:{'Authorization':`token ${GH_TOKEN}`,'Accept':'application/vnd.github.v3+json'}});if(g.ok){const d=await g.json();sha=d.sha;try{cur=JSON.parse(decodeURIComponent(escape(atob(d.content.replace(/\\n/g,'')))));}catch(e){}}}catch(e){}
  if(!cur.requests)cur.requests=[];
  cur.requests.push(reqObj);
  const body={message:'calendar request (dashboard)',content:btoa(unescape(encodeURIComponent(JSON.stringify(cur))))};
  if(sha)body.sha=sha;
  try{const r=await fetch(`https://api.github.com/repos/${GH_REPO}/contents/${path}`,{method:'PUT',headers:{'Authorization':`token ${GH_TOKEN}`,'Content-Type':'application/json','Accept':'application/vnd.github.v3+json'},body:JSON.stringify(body)});return r.ok;}catch(e){return false;}
}
async function calAddEvent(btn){
  const title=document.getElementById('cal-title').value.trim();
  const date=document.getElementById('cal-date').value;
  const time=document.getElementById('cal-time').value;
  if(!title||!date){alert('予定名と日付を入力してください');return;}
  btn.disabled=true;btn.textContent='送信中…';
  const ok=await _calPutRequest({action:'add',summary:title,date:date,time:time});
  btn.textContent=ok?'✅ 送信済み（数分後にカレンダーへ反映）':'⚠️ 失敗';
  if(ok)document.getElementById('cal-title').value='';
  setTimeout(function(){btn.disabled=false;btn.textContent='Googleカレンダーに追加';},6000);
}
async function calDeleteEvent(eid,summary,btn){
  if(!confirm('「'+summary+'」をGoogleカレンダーからも削除します。よろしいですか？'))return;
  btn.disabled=true;btn.textContent='…';
  const ok=await _calPutRequest({action:'delete',event_id:eid});
  if(ok){const item=btn.closest('.schedule-item');if(item)item.style.opacity='0.35';btn.textContent='✅';}
  else{btn.textContent='⚠️';btn.disabled=false;}
}
function _gubCat(){try{return JSON.parse(localStorage.getItem('ks_unified_hidden_cat_v1')||'[]');}catch{return[];}}  // [{key:"account|domain|category",label:"ノジマ"}, ...] カテゴリ限定非表示（△）
function _subCat(list){localStorage.setItem('ks_unified_hidden_cat_v1',JSON.stringify(list));}
function _entryKey(e){return typeof e==='string'?e:e.key;}
function _entryLabel(e){return typeof e==='string'?'':(e.label||'');}
function applyUnifiedBlocklist(){
  // ✕/△済み送信元のメールは（この端末では）リロード後も即座に画面から隠す。
  // sender_rules反映後の次回ビルドで完全に除外されるまでの橋渡し
  // （以前は12時間グレー表示のみ→「更新すると復活して見える」問題があった）。
  const keys=_gub().map(_entryKey),catKeys=_gubCat().map(_entryKey);
  if(keys.length||catKeys.length){
    document.querySelectorAll('.mail-item').forEach(function(el){
      const a=el.getAttribute('data-account'),d=el.getAttribute('data-domain'),c=el.getAttribute('data-category');
      if(!d)return;
      if(keys.indexOf(a+'|'+d)>=0||catKeys.indexOf(a+'|'+d+'|'+c)>=0)el.style.display='none';
    });
  }
  renderUnifiedBlocklistUI();
}
function toggleUnifiedBlocklistUI(){
  const wrap=document.getElementById('unified-blocklist-card');
  if(!wrap)return;
  const show=wrap.style.display==='none'||!wrap.style.display;
  wrap.style.display=show?'':'none';
}
function _archiveMatching(selector,reason,meta){
  document.querySelectorAll(selector).forEach(function(el){
    if(!el.classList.contains('archived'))archiveItem(el.id,reason,meta);
  });
}
function blockUnifiedSender(account,domain,category,label){
  if(!domain)return;
  if(category==='action'){alert('現在「要対応」のメールです。送信元を丸ごと非表示にすると今後の要対応メールも見えなくなるため、代わりに△（このカテゴリだけ非表示）をお使いください');return;}
  const key=account+'|'+domain;
  const local=_gub();if(!local.find(function(e){return _entryKey(e)===key;})){local.push({key:key,label:label||''});_sub(local);}
  _archiveMatching('.mail-item[data-account="'+account+'"][data-domain="'+domain+'"]','block',{account:account,domain:domain});
  applyUnifiedBlocklist();
  if(GH_TOKEN){updateSenderRules(function(rules){rules[account]=rules[account]||{};rules[account][domain]='hide';});}
}
function blockCategoryUnifiedSender(account,domain,category,label){
  if(!domain||!category)return;
  if(category==='action'){
    if(!confirm('「'+domain+'」からの「要対応」カテゴリを今後すべて非表示にします。\\n同じ送信元・同じカテゴリの別の重要な連絡も自動的に見えなくなる可能性があります。\\n本当によろしいですか？'))return;
  }
  const key=account+'|'+domain+'|'+category;
  const local=_gubCat();if(!local.find(function(e){return _entryKey(e)===key;})){local.push({key:key,label:label||''});_subCat(local);}
  _archiveMatching('.mail-item[data-account="'+account+'"][data-domain="'+domain+'"][data-category="'+category+'"]','blockCategory',{account:account,domain:domain,category:category});
  applyUnifiedBlocklist();
  if(GH_TOKEN){updateSenderRules(function(rules){
    rules[account]=rules[account]||{};
    const cur=rules[account][domain];
    const cats=(cur&&typeof cur==='object'&&Array.isArray(cur.hide_categories))?cur.hide_categories.slice():[];
    if(!cats.includes(category))cats.push(category);
    rules[account][domain]={hide_categories:cats};
  });}
}
function unblockUnifiedSender(account,domain){
  const key=account+'|'+domain;
  const local=_gub().filter(function(e){return _entryKey(e)!==key;});_sub(local);
  document.querySelectorAll('.mail-item[data-account="'+account+'"][data-domain="'+domain+'"]').forEach(function(el){el.style.display='';});
  renderUnifiedBlocklistUI();
  if(GH_TOKEN){updateSenderRules(function(rules){if(rules[account])delete rules[account][domain];});}
}
function unblockCategoryUnifiedSender(account,domain,category){
  const key=account+'|'+domain+'|'+category;
  const local=_gubCat().filter(function(e){return _entryKey(e)!==key;});_subCat(local);
  document.querySelectorAll('.mail-item[data-account="'+account+'"][data-domain="'+domain+'"][data-category="'+category+'"]').forEach(function(el){el.style.display='';});
  renderUnifiedBlocklistUI();
  if(GH_TOKEN){updateSenderRules(function(rules){
    const cur=rules[account]&&rules[account][domain];
    if(cur&&typeof cur==='object'&&Array.isArray(cur.hide_categories)){
      cur.hide_categories=cur.hide_categories.filter(function(c){return c!==category;});
      if(!cur.hide_categories.length)delete rules[account][domain];
    }
  });}
}
function renderUnifiedBlocklistUI(){
  const el=document.getElementById('unified-blocklist-list');if(!el)return;
  const emptyEl=document.getElementById('unified-blocklist-empty');
  const list=_gub(),listCat=_gubCat();
  if(emptyEl)emptyEl.style.display=(list.length||listCat.length)?'none':'';
  const fullRows=list.map(function(e){
    const key=_entryKey(e),label=_entryLabel(e);
    const parts=key.split('|'),account=parts[0],domain=parts.slice(1).join('|');
    const nameText=label?`${_esc(label)}（${_esc(domain)}）`:_esc(domain);
    return `<div class="routine-item"><div class="routine-name">✕ ${nameText}<span style="color:var(--sub);font-size:10px;"> （${_esc(account)}・全カテゴリ）</span></div><div class="routine-freq"><button class="rt-del-btn" onclick="unblockUnifiedSender('${_esc(account)}','${_esc(domain)}')" title="また表示する">↩</button></div></div>`;
  });
  const catRows=listCat.map(function(e){
    const key=_entryKey(e),label=_entryLabel(e);
    const parts=key.split('|'),account=parts[0],domain=parts[1],category=parts[2];
    const nameText=label?`${_esc(label)}（${_esc(domain)}）`:_esc(domain);
    return `<div class="routine-item"><div class="routine-name">△ ${nameText}<span style="color:var(--sub);font-size:10px;"> （${_esc(account)}・${_esc(category)}のみ）</span></div><div class="routine-freq"><button class="rt-del-btn" onclick="unblockCategoryUnifiedSender('${_esc(account)}','${_esc(domain)}','${_esc(category)}')" title="また表示する">↩</button></div></div>`;
  });
  el.innerHTML=fullRows.concat(catRows).join('');
}
function mergeDone(a,b){const m={};[...a,...b].forEach(function(i){const c=m[i.id];if(!c||i.completedAt>c.completedAt){m[i.id]=Object.assign({},i);if(c&&c.cleaned)m[i.id].cleaned=true;}else if(i.cleaned){c.cleaned=true;}});return Object.values(m);}
function fmtDoneTime(ts){
  var d=new Date(ts);
  return (d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
}
var DONE_BADGE_LABEL={done:'✓完了',block:'✕非表示（送信元）',blockCategory:'△非表示（カテゴリ）'};
var DONE_BTN_CLASS={done:'btn-check',block:'btn-x',blockCategory:'btn-triangle'};
function addDoneBadge(el,completedAt,reason){
  var titleEl=el.querySelector('.mail-title,.task-title,.routine-name');
  if(!titleEl)return;
  var badge=titleEl.querySelector('.done-time-badge');
  if(!badge){badge=document.createElement('span');badge.className='done-time-badge';titleEl.appendChild(badge);}
  var label=DONE_BADGE_LABEL[reason]||DONE_BADGE_LABEL.done;
  badge.textContent=' '+label+' '+fmtDoneTime(completedAt)+' ';
  var undo=badge.querySelector('.undo-inline-btn');
  if(!undo){
    undo=document.createElement('button');undo.className='undo-inline-btn';undo.textContent='取消';undo.title='この操作を取り消す';
    undo.onclick=function(e){e.stopPropagation();undoItem(el.id);};
    badge.appendChild(undo);
  }
  el.querySelectorAll('.archive-btn.pressed').forEach(function(b){b.classList.remove('pressed');});
  var pressedCls=DONE_BTN_CLASS[reason]||DONE_BTN_CLASS.done;
  var pressedBtn=el.querySelector('.'+pressedCls);
  if(pressedBtn)pressedBtn.classList.add('pressed');
}
function applyDoneState(list){
  var now=Date.now(),H12=12*3600*1000,H7D=7*24*3600*1000;
  var doneBase={};
  list.forEach(function(item){if(!item.id.startsWith('r-'))return;var base=item.id.replace(/-\\d{8}$/,'');if(!doneBase[base]||item.completedAt>doneBase[base])doneBase[base]=item.completedAt;});
  list.forEach(function(item){
    var el=document.getElementById(item.id);if(!el)return;
    el.classList.remove('archived');el.style.display='';
    if(!item.cleaned&&now-item.completedAt<H12){
      el.classList.add('archived');
      addDoneBadge(el,item.completedAt,item.reason);
    }else{
      el.style.display='none';
    }
  });
  document.querySelectorAll('[id^="r-"]').forEach(function(el){if(el.style.display==='none'||el.classList.contains('archived'))return;var base=el.id.replace(/-\\d{8}$/,'');if(doneBase[base]&&now-doneBase[base]<H7D)el.style.display='none';});
}
function cleanupDoneNow(){
  const now=Date.now(),H12=12*3600*1000;
  const list=_gd();
  const pending=list.filter(function(d){return !d.cleaned&&now-d.completedAt<H12;});
  if(!pending.length){alert('片づける完了済みアイテムはありません（グレー表示のものが対象です）');return;}
  if(!confirm('完了・非表示にした '+pending.length+' 件をいますぐ画面から片づけます（完了履歴には残ります）。よろしいですか？'))return;
  pending.forEach(function(d){d.cleaned=true;});
  _sd(list);applyDoneState(list);renderExtraTasks();
  if(GH_TOKEN){ghGet().then(function(r){const merged=mergeDone(list,r.list);_sd(merged);ghPut(merged,r.sha);});}
}
function toggleKpiDetail(id){const el=document.getElementById(id);if(!el)return;const open=el.classList.contains('open');document.querySelectorAll('.kpi-mail-detail.open').forEach(e=>e.classList.remove('open'));if(!open)el.classList.add('open');}
function openPop(id){document.querySelectorAll('.pop-overlay').forEach(e=>e.classList.remove('open'));var el=document.getElementById(id);if(el)el.classList.add('open');}
function closePop(e){if(e.target.classList.contains('pop-overlay'))e.target.classList.remove('open');}
const RT_KEY='ks_custom_routines_v1';
function _gr(){try{return JSON.parse(localStorage.getItem(RT_KEY)||'[]');}catch{return[];}}
function _sr(list){localStorage.setItem(RT_KEY,JSON.stringify(list));}
function _esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function toggleRtForm(){const f=document.getElementById('rt-form');if(f)f.classList.toggle('open');}
function addRoutine(){const n=document.getElementById('rt-name').value.trim(),f=document.getElementById('rt-freq').value.trim();if(!n)return;const list=_gr();list.push({id:'cr-'+Date.now(),name:n,freq:f});_sr(list);renderCustomRoutines();document.getElementById('rt-name').value='';document.getElementById('rt-freq').value='';document.getElementById('rt-form').classList.remove('open');}
function deleteRoutine(id){if(!confirm('このルーチンを削除しますか？（後で元に戻せます）'))return;const r=_gr().find(function(x){return x.id===id;});_sr(_gr().filter(function(x){return x.id!==id;}));if(r){const list=_gtr();list.push({id:r.id,kind:'routine',name:r.name,freq:r.freq,deletedAt:Date.now()});_str(list);}renderCustomRoutines();renderTrash();}
function renderCustomRoutines(){const el=document.getElementById('custom-routines');if(!el)return;const list=_gr();el.innerHTML=list.length===0?'':list.map(r=>`<div class="routine-item" id="${r.id}"><div class="routine-name">${_esc(r.name)}</div><div class="routine-freq">${_esc(r.freq)}<button class="rt-del-btn" onclick="deleteRoutine('${r.id}')" title="削除">✕</button></div></div>`).join('');}
const XT_KEY='ks_extra_tasks_v1';
function _gxt(){try{return JSON.parse(localStorage.getItem(XT_KEY)||'[]');}catch{return[];}}
function _sxt(list){localStorage.setItem(XT_KEY,JSON.stringify(list));}
const URGENCY_ICON={urgent:'🔴',soon:'🟡'};
const URGENCY_ORDER={urgent:0,soon:1,'':2};
function addMailToTaskList(mailId,title,btn){
  const taskId='mt-'+mailId;
  const list=_gxt();
  if(!list.find(t=>t.id===taskId)){
    let detail='';
    try{const src=document.getElementById(mailId);if(src){const f=src.querySelector('.detail-from'),b=src.querySelector('.detail-body');detail=((f?f.textContent:'')+String.fromCharCode(10,10)+(b?b.textContent:'')).trim();}}catch(e){}
    list.push({id:taskId,name:title,source:'mail',mailId,urgency:'',detail:detail,addedAt:Date.now()});_sxt(list);renderExtraTasks();}
  if(btn){btn.textContent='✓追加済';btn.disabled=true;btn.classList.add('pressed');}
}
function toggleExtraTaskForm(){const f=document.getElementById('extra-task-form');if(f)f.classList.toggle('open');}
function toggleTaskDetail(taskId){
  const el=document.getElementById('td-'+taskId);if(!el)return;
  if(el.style.display==='none'){
    const t=_gxt().find(function(x){return x.id===taskId;});
    let detail=(t&&t.detail)||'';
    if(!detail&&t&&t.mailId){const src=document.getElementById(t.mailId);if(src){const f=src.querySelector('.detail-from'),b=src.querySelector('.detail-body');detail=((f?f.textContent:'')+String.fromCharCode(10,10)+(b?b.textContent:'')).trim();}}
    el.textContent=detail||'（メール本文が見つかりませんでした。メール一覧から消えた古いメールの可能性があります）';
    el.style.display='block';
  }else{el.style.display='none';}
}
function addExtraTask(){
  const n=document.getElementById('extra-task-name').value.trim();
  const u=document.getElementById('extra-task-urgency').value;
  if(!n)return;
  const list=_gxt();
  list.push({id:'xt-'+Date.now(),name:n,source:'manual',urgency:u,addedAt:Date.now()});
  _sxt(list);renderExtraTasks();
  document.getElementById('extra-task-name').value='';
  document.getElementById('extra-task-form').classList.remove('open');
}
function deleteExtraTask(id){
  if(!confirm('このタスクを削除しますか？（後で元に戻せます）'))return;
  const t=_gxt().find(function(x){return x.id===id;});
  _sxt(_gxt().filter(function(x){return x.id!==id;}));
  if(t){const list=_gtr();list.push({id:t.id,kind:'extra',name:t.name,source:t.source,urgency:t.urgency||'',mailId:t.mailId,detail:t.detail||'',deletedAt:Date.now()});_str(list);}
  renderExtraTasks();renderTrash();
}
function renderExtraTasks(){
  const el=document.getElementById('custom-extra-tasks');if(!el)return;
  const now=Date.now(),H12=12*3600*1000;
  const doneMap={};_gd().forEach(function(d){doneMap[d.id]=d.completedAt;});
  const list=_gxt().slice().sort(function(a,b){
    const ua=URGENCY_ORDER[a.urgency||'']??2,ub=URGENCY_ORDER[b.urgency||'']??2;
    return ua!==ub?ua-ub:a.addedAt-b.addedAt;
  });
  el.innerHTML=list.map(t=>{
    const doneAt=doneMap[t.id];
    if(doneAt&&(now-doneAt>=H12))return '';
    const urgCls=t.urgency?' urgency-'+t.urgency:'';
    const cls=(doneAt?'task-card extra archived':'task-card extra')+urgCls;
    const icon=t.source==='mail'?'📧':(URGENCY_ICON[t.urgency]||'📝');
    const isMail=t.source==='mail';
    const titleClick=isMail?` onclick="toggleTaskDetail('${t.id}')" style="cursor:pointer;" title="クリックでメール本文を表示"`:'';
    const detailDiv=isMail?`<div class="task-mail-detail" id="td-${t.id}" style="display:none;white-space:pre-wrap;word-break:break-word;font-size:12px;color:var(--sub);margin-top:6px;border-top:1px dashed var(--border);padding-top:6px;"></div>`:'';
    return `<div class="${cls}" id="${t.id}"><div class="task-header"><div class="task-title"${titleClick}><span class="badge badge-blue">${icon}</span> ${_esc(t.name)}</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('${t.id}')">✓</button><button class="rt-del-btn" onclick="event.stopPropagation();deleteExtraTask('${t.id}')" title="削除">✕</button></div>${detailDiv}</div>`;
  }).join('');
}
const TR_KEY='ks_trash_v1';
function _gtr(){try{return JSON.parse(localStorage.getItem(TR_KEY)||'[]');}catch{return[];}}
function _str(list){localStorage.setItem(TR_KEY,JSON.stringify(list));}
function trashPurgeOld(){const now=Date.now(),D30=30*24*3600*1000;_str(_gtr().filter(function(t){return now-t.deletedAt<D30;}));}
function renderTrash(){
  const el=document.getElementById('trash-list');if(!el)return;
  const wrap=document.getElementById('trash-card');
  const list=_gtr().slice().sort(function(a,b){return b.deletedAt-a.deletedAt;});
  if(wrap)wrap.style.display=list.length?'':'none';
  el.innerHTML=list.map(function(t){return `<div class="routine-item" id="trash-${t.id}"><div class="routine-name">${_esc(t.name)}</div><div class="routine-freq">${_esc(t.freq||'')}<button class="rt-del-btn" onclick="restoreTrashItem('${t.id}')" title="元に戻す">↩</button></div></div>`;}).join('');
}
function restoreTrashItem(id){
  const list=_gtr();const idx=list.findIndex(function(t){return t.id===id;});if(idx<0)return;
  const item=list[idx];list.splice(idx,1);_str(list);
  if(item.kind==='routine'){const rl=_gr();rl.push({id:item.id,name:item.name,freq:item.freq});_sr(rl);renderCustomRoutines();}
  else if(item.kind==='extra'){const xl=_gxt();xl.push({id:item.id,name:item.name,source:item.source||'manual',urgency:item.urgency||'',mailId:item.mailId,detail:item.detail||'',addedAt:Date.now()});_sxt(xl);renderExtraTasks();}
  else if(item.kind==='builtin'){const el2=document.getElementById(item.id);if(el2)el2.style.display='';}
  renderTrash();
}
function deleteBuiltinRoutine(id){
  if(!confirm('このルーチンを削除しますか？（後で元に戻せます）'))return;
  const el=document.getElementById(id);if(!el)return;
  const name=(el.querySelector('.routine-name')||{textContent:''}).textContent.trim();
  const freq=(el.querySelector('.routine-freq')||{textContent:''}).textContent.replace('✕','').trim();
  const list=_gtr();list.push({id,kind:'builtin',name,freq,deletedAt:Date.now()});_str(list);
  el.style.display='none';
  renderTrash();
}
function applyBuiltinRoutineDismissed(){
  _gtr().filter(function(t){return t.kind==='builtin';}).forEach(function(t){const el=document.getElementById(t.id);if(el)el.style.display='none';});
}
function archiveItem(id,reason,meta){
  const el=document.getElementById(id);
  if(!el||el.classList.contains('archived'))return;
  const textEl=el.querySelector('.task-next,.task-title,.mail-title');
  const text=textEl?textEl.textContent.trim().slice(0,60):'';
  const item={id,text,completedAt:Date.now(),reason:reason||'done'};
  if(meta)item.meta=meta;
  const list=_gd();if(!list.find(i=>i.id===id)){list.push(item);_sd(list);}
  el.classList.add('archived');
  addDoneBadge(el,item.completedAt,item.reason);
  if(GH_TOKEN){ghGet().then(function(r){const merged=mergeDone(_gd(),r.list);_sd(merged);ghPut(merged,r.sha);});}
}
function undoItem(id){
  const list=_gd();const item=list.find(function(i){return i.id===id;});
  const el=document.getElementById(id);
  _sd(list.filter(function(i){return i.id!==id;}));
  if(el){el.classList.remove('archived');el.style.display='';const b=el.querySelector('.done-time-badge');if(b)b.remove();el.querySelectorAll('.archive-btn.pressed').forEach(function(btn){btn.classList.remove('pressed');});}
  if(GH_TOKEN){ghGet().then(function(r){const merged=_gd().length?mergeDone(_gd(),r.list.filter(function(i){return i.id!==id;})):r.list.filter(function(i){return i.id!==id;});ghPut(merged,r.sha);});}
  if(item&&item.meta){
    const m=item.meta;
    if(item.reason==='block')unblockUnifiedSender(m.account,m.domain);
    else if(item.reason==='blockCategory')unblockCategoryUnifiedSender(m.account,m.domain,m.category);
  }
}
function toggleDetail(id){const el=document.getElementById(id);if(el)el.classList.toggle('open');}
function copyMailText(btn){
  const text=btn.getAttribute('data-copy')||'';
  const done=function(){const orig=btn.textContent;btn.textContent='✓ コピーしました';setTimeout(function(){btn.textContent=orig;},1500);};
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done).catch(function(){fallbackCopy(text,done);});}
  else{fallbackCopy(text,done);}
}
function fallbackCopy(text,done){
  const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.focus();ta.select();
  try{document.execCommand('copy');}catch(e){}
  document.body.removeChild(ta);done();
}
</script>

<header>
  <h1>📋 金坂 タスク管理ダッシュボード</h1>
  <div class="header-buttons">
    <button class="btn-header" id="reflect-mail-btn" onclick="reflectMail()" title="GitHub Actionsでメール反映を実行">🔄 メール反映</button>
    <a class="btn-header" href="kanesaka-task-history.html" title="完了したタスクの履歴">📋 完了履歴</a>
    <a class="btn-header" href="kanesaka-mail-rules.html" title="送信元ルール・AI判断ロジックを確認">🔍 判定ルール</a>
    <button class="btn-header" onclick="top.location.href=top.location.pathname+'?_='+Date.now()" title="最新の状態に再読み込み（F5相当・キャッシュ回避）">🔃 更新</button>
    <button class="btn-header" onclick="cleanupDoneNow()" title="完了・非表示済み（グレー表示）のメールを12時間待たずにいますぐ履歴へ移動する">🧹 完了分を片づけ</button>
  </div>
  <div class="updated">最終更新：###UPDATED### ／ Claude　<span id="api-cost-note" style="font-size:11px;color:var(--sub);"></span></div>
</header>

<div class="container">

  <!-- ═══ ファーストビュー 3分割 ═══ -->
  <div class="first-view">

    <!-- LEFT: 要対応メール（統合メール一覧のうち①要対応のみ抜粋） -->
    <div class="fv-col">
      <div class="section-head">📬 要対応メール</div>
      <div class="card scroll-card">
###ACTION_MAIL_SECTION###
        <div class="mail-all-link">
          <a href="#unified-mail">📬 全メール一覧（AI仕分け）を見る↓</a>
        </div>
      </div>
    </div>

    <!-- MIDDLE: タスク一覧 -->
    <div class="fv-col">
      <div class="section-head">📋 タスク一覧 <button class="rt-toggle-btn" onclick="toggleExtraTaskForm()">＋ 追加</button></div>
      <div class="card scroll-card">
        <div id="custom-extra-tasks"></div>
        <div class="rt-form" id="extra-task-form">
          <input type="text" id="extra-task-name" placeholder="タスク名">
          <select id="extra-task-urgency">
            <option value="urgent">🔴 急ぎ</option>
            <option value="soon" selected>🟡 近いうち</option>
            <option value="">指定なし</option>
          </select>
          <div class="rt-form-btns">
            <button class="rt-save-btn" onclick="addExtraTask()">追加</button>
            <button class="rt-cancel-btn" onclick="toggleExtraTaskForm()">キャンセル</button>
          </div>
        </div>
###TODAY_CALENDAR_TASKS###
###ROUTINE_TASKS###
        <div class="task-card extra urgency-urgent" id="a-1">
          <div class="task-header"><div class="task-title">🔴 ✈️ ANA搭乗の最終確認（予約番号 EU4DAG）</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-1')">✓</button></div>
          <div class="task-next">近日搭乗</div>
        </div>
        <div class="task-card extra urgency-urgent" id="a-2">
          <div class="task-header"><div class="task-title">🔴 📚 JASSO奨学金 息子に書類提出確認</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-2')">✓</button></div>
          <div class="task-next">期日要確認</div>
        </div>
        <div class="task-card extra urgency-urgent" id="a-3">
          <div class="task-header"><div class="task-title">🔴 🎓 OC予約 7/25 → OCANs</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-3')">✓</button></div>
          <div class="task-next">枠が埋まる前に早急</div>
        </div>
        <div class="task-card extra urgency-urgent" id="a-4">
          <div class="task-header"><div class="task-title">🔴 💳 PayPal カード更新（末尾2063）</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-4')">✓</button></div>
          <div class="task-next">現在利用不可</div>
        </div>
        <div class="task-card extra urgency-urgent" id="a-5">
          <div class="task-header"><div class="task-title">🔴 ⚖️ ベリーベスト法律事務所 ZEUS支払い</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-5')">✓</button></div>
          <div class="task-next">期日内に要対応</div>
        </div>
        <div class="task-card extra urgency-soon" id="a-6">
          <div class="task-header"><div class="task-title">🟡 Microsoft アカウント PW変更（旧役員ログアウト）</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-6')">✓</button></div>
        </div>
        <div class="task-card extra urgency-soon" id="a-7">
          <div class="task-header"><div class="task-title">🟡 ⚽ 合宿移動方針 → 河合先生返答待ち</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-7')">✓</button></div>
        </div>
        <div class="task-card extra urgency-soon" id="a-8">
          <div class="task-header"><div class="task-title">🟡 🏦 武蔵野銀行 電子交付移行（8月〜）確認</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-8')">✓</button></div>
        </div>
        <div class="task-card extra urgency-soon" id="a-9">
          <div class="task-header"><div class="task-title">🟡 🏦 三菱UFJ BizSTATION アプリ更新・認証再登録</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-9')">✓</button></div>
        </div>
        <div class="task-card extra urgency-soon" id="a-10">
          <div class="task-header"><div class="task-title">🟡 GDrive オーナー変更（kanesaka.agni → fubokai 6ファイル）</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-10')">✓</button></div>
        </div>
        <div class="task-card extra urgency-soon" id="a-11">
          <div class="task-header"><div class="task-title">🟡 OneDrive 解約（GDrive移行完了後）</div><button class="archive-btn btn-check" title="完了" onclick="event.stopPropagation();archiveItem('a-11')">✓</button></div>
        </div>
        <div class="task-card active" id="t-5">
          <div class="task-header" onclick="toggleDetail('t-5')">
            <div class="task-title"><span class="badge badge-green">▶</span> #5 大学受験サポート</div>
            <button class="archive-btn" onclick="event.stopPropagation();archiveItem('t-5')">✓</button>
          </div>
          <div class="task-next"><strong>OC予約（7/25・早急）</strong> ／ 自己推薦書本人記入 ／ 実技②実測（7月中）</div>
          <div class="task-detail">【筑波大 体育学群 体育専門学群 AC トラック（総合型選抜）】

息子（幕張総合高校3年）の大学受験サポート。
三者面談で先生の全面協力確定。

① OC予約 → 7/25 OCANs サイトから（早急！）
② 自己推薦書：本人が競技記録表から実数値を記入
③ 実技②：陸上部で実測してもらう（7月中）
④ 受験相談メール：本人が担当先生に送る

8/22〜  出願登録開始（〜9/3）
9/3    出願書類 郵送締切
9月下旬  1次結果発表
10月    2次面接
11月上旬  合格発表</div>
        </div>
        <div class="task-card paused" id="t-1">
          <div class="task-header" onclick="toggleDetail('t-1')">
            <div class="task-title"><span class="badge badge-gray">⏸</span> #1 総会準備</div>
            <button class="archive-btn" onclick="event.stopPropagation();archiveItem('t-1')">✓</button>
          </div>
          <div class="task-next">残り：<strong>Microsoft PW変更</strong>（旧役員ログアウト）</div>
          <div class="task-detail">総会 6/20 完了・議事録配布済。
残り：Microsoft アカウントのパスワード変更（旧役員が引き続きログインできる状態）</div>
        </div>
        <div class="task-card paused" id="t-4">
          <div class="task-header" onclick="toggleDetail('t-4')">
            <div class="task-title"><span class="badge badge-gray">⏸</span> #4 父母会相談</div>
            <button class="archive-btn" onclick="event.stopPropagation();archiveItem('t-4')">✓</button>
          </div>
          <div class="task-next">河合先生返答待ち・部費未払い様子見</div>
          <div class="task-detail">① 合宿移動問題 → 河合先生（顧問）の返答待ち
② 上野さん 部費未払い → 様子見継続</div>
        </div>
        <div class="task-card paused" id="t-2">
          <div class="task-header" onclick="toggleDetail('t-2')">
            <div class="task-title"><span class="badge badge-gray">⏸</span> #2 プロジェクト設定</div>
            <button class="archive-btn" onclick="event.stopPropagation();archiveItem('t-2')">✓</button>
          </div>
          <div class="task-next"><strong>オーナー変更（6ファイル）・GDrive確認・OneDrive解約</strong></div>
          <div class="task-detail">GDrive オーナー変更（6ファイル）→ GDrive 目視確認 → OneDrive 解約
※ OneDriveを先に解約しないこと（データ消失リスク）</div>
        </div>
        <div class="task-card paused" id="t-3">
          <div class="task-header" onclick="toggleDetail('t-3')">
            <div class="task-title"><span class="badge badge-gray">⏸</span> #3 マニュアル整備</div>
            <button class="archive-btn" onclick="event.stopPropagation();archiveItem('t-3')">✓</button>
          </div>
          <div class="task-next"><strong>画像追加・総務役員への共有</strong></div>
          <div class="task-detail">マニュアル2本作成済。残り：画像追加→総務役員に共有</div>
        </div>
      </div>
    </div>

    <!-- RIGHT: 今月の実績 -->
    <div class="fv-col">
      <div class="section-head">📊 今月の実績（###MONTH###）</div>
###KPI_SECTION###
    </div>

  </div><!-- /first-view -->

  <!-- ═══ 全メール一覧（AI仕分け・全アカウント統合） ═══ -->
  <div class="section-head" id="unified-mail">📬 全メール一覧（AI仕分け・全アカウント統合）<span style="font-size:10px;font-weight:normal;margin-left:8px;color:var(--sub);">###UNIFIED_MAIL_FETCHED###</span>
    <button class="rebuild-btn" style="font-size:10px;padding:3px 10px;margin-left:auto;" onclick="toggleUnifiedBlocklistUI()">🚫 非表示リスト</button>
    <button class="rebuild-btn jh-restore-btn" style="font-size:10px;padding:3px 10px;display:none;" onclick="restoreJustHidden()">個別非表示を戻す</button>
  </div>
  <div class="first-view">
    <div class="fv-col">
      <div class="section-head">📧 お知らせ</div>
      <div class="card scroll-card">
###MAIL_INFO_SECTION###
      </div>
    </div>
    <div class="fv-col">
      <div class="section-head">❓ 宛先不明</div>
      <div class="card scroll-card">
###MAIL_UNCLEAR_SECTION###
      </div>
    </div>
    <div class="fv-col">
      <div class="section-head">⚠️ スパムか不明</div>
      <div class="card scroll-card">
###MAIL_MAYBE_SPAM_SECTION###
      </div>
    </div>
  </div>
  <div class="card full" id="unified-blocklist-card" style="padding:12px 16px;display:none;margin-top:8px;">
    <div class="card-title" style="margin-bottom:6px;">✕ 今後表示しない送信元</div>
    <div id="unified-blocklist-list"></div>
    <div id="unified-blocklist-empty" style="font-size:12px;color:var(--sub);display:none;">非表示にしている送信元はありません</div>
  </div>

  <!-- ═══ 近日の予定 ═══ -->
  <div class="section-head">📅 近日の予定（Googleカレンダー連携）<button class="rt-toggle-btn" onclick="toggleCalForm()">＋ 予定追加</button><span style="font-size:10px;font-weight:normal;margin-left:8px;color:var(--sub);">###CALENDAR_FETCHED###</span></div>
  <div class="rt-form" id="cal-form" style="margin:0 0 8px;">
    <input type="text" id="cal-title" placeholder="予定名">
    <input type="date" id="cal-date">
    <input type="time" id="cal-time">
    <span style="font-size:11px;color:var(--sub);">時刻を空にすると終日予定</span>
    <div class="rt-form-btns">
      <button class="rt-save-btn" onclick="calAddEvent(this)">Googleカレンダーに追加</button>
      <button class="rt-cancel-btn" onclick="toggleCalForm()">キャンセル</button>
    </div>
  </div>
  <div class="grid">
###SCHEDULE_SECTION###
  </div>

  <!-- ═══ 息子の大学受験 ═══ -->
  <div class="section-head">🎓 息子の大学受験（筑波大 体育学群）</div>
  <div class="card full">
    <div class="card-title">📍 ACトラック（総合型選抜）</div>
    <div class="countdown-bar">
      <div class="countdown-item urgent">
        <div class="days">50</div><div class="label">出願登録開始</div><div class="date">8/22（土）</div>
      </div>
      <div class="countdown-item urgent">
        <div class="days">62</div><div class="label">出願郵送締切</div><div class="date">9/3（水）</div>
      </div>
      <div class="countdown-item">
        <div class="days">〜</div><div class="label">1次結果</div><div class="date">9月下旬</div>
      </div>
      <div class="countdown-item">
        <div class="days">〜</div><div class="label">2次面接</div><div class="date">10月</div>
      </div>
      <div class="countdown-item">
        <div class="days">〜</div><div class="label">合格発表</div><div class="date">11月上旬</div>
      </div>
    </div>
    <div style="margin-top:10px;font-size:12px;color:var(--sub);">
      ▶ 次にやること：①OC予約（7/25・要早急） ②自己推薦書本人記入（実数値） ③実技②陸上部実測（7月中） ④受験相談メール（本人）
    </div>
  </div>

  <!-- ═══ ルーチン業務 ═══ -->
  <div class="section-head">🔄 ルーチン業務</div>
  <div class="card routine-cards">
    <div class="routine-col">
      <div class="card-title">毎月5日前後</div>
      <div class="routine-item" id="rd-shakaihoken"><div class="routine-name">社会保険料データDL（e-Gov電子申請・前月分）</div><div class="routine-freq">毎月5日前後<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-shakaihoken')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-zip"><div class="routine-name">ZIP精算（前月分）</div><div class="routine-freq">毎月5〜7日<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-zip')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-payjp"><div class="routine-name">定期精算メンテ（システム請求書 PAY.JP）</div><div class="routine-freq">毎月5日前後<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-payjp')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-kintai"><div class="routine-name">出勤簿の提出</div><div class="routine-freq">毎月5日前後<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-kintai')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-shotokuzei"><div class="routine-name">所得税支払い（前月分）</div><div class="routine-freq">毎月5日前後<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-shotokuzei')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-juminzei"><div class="routine-name">住民税支払い（前月分）</div><div class="routine-freq">毎月5日前後<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-juminzei')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-yukyu"><div class="routine-name">有給管理</div><div class="routine-freq">毎月5日前後<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-yukyu')" title="削除">✕</button></div></div>
    </div>
    <div class="routine-col">
      <div class="card-title">毎月10日 / 毎週 / その他</div>
      <div class="routine-item" id="rd-zeikin"><div class="routine-name">税金支払い（所得・住民）</div><div class="routine-freq">毎月9日頃<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-zeikin')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-credit"><div class="routine-name">クレジット精算・CB/AMEXダウンロード</div><div class="routine-freq">毎月10日（1,4,7,10月のみ）<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-credit')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-credit-note" style="font-size:11px;color:var(--sub);"><div>└ VISAは現金小口で精算</div></div>
      <div class="routine-item" id="rd-kure"><div class="routine-name">クレ対応</div><div class="routine-freq">毎月10日頃<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-kure')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-santei"><div class="routine-name">算定</div><div class="routine-freq">月末<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-santei')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-koukoku-mtg"><div class="routine-name">広告MTG（AGNIYOGA）</div><div class="routine-freq">毎月第2金曜 13:00〜<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-koukoku-mtg')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-wmtg"><div class="routine-name">wmtg（AGNIYOGA社内）</div><div class="routine-freq">毎週金曜 14:00〜<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-wmtg')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-activia-houshin"><div class="routine-name">アクティビア方針まとめ 各自確認</div><div class="routine-freq">毎月第1水曜 14:30〜<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-activia-houshin')" title="削除">✕</button></div></div>
      <div class="routine-item" id="rd-aws-savings"><div class="routine-name">AWS Savings Plans 確認</div><div class="routine-freq">年1回・8月（次回 8/3）<button class="rt-del-btn" onclick="deleteBuiltinRoutine('rd-aws-savings')" title="削除">✕</button></div></div>
    </div>
  </div>

  <!-- カスタムルーチン -->
  <div class="card full" style="padding:12px 16px;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
      <div class="card-title" style="margin-bottom:0;">✏️ マイルーチン（追加分）</div>
      <button class="rt-toggle-btn" onclick="toggleRtForm()">＋ 追加</button>
    </div>
    <div id="custom-routines"></div>
    <div class="rt-form" id="rt-form">
      <input type="text" id="rt-name" placeholder="ルーチン名（例：月末請求書確認）">
      <input type="text" id="rt-freq" placeholder="タイミング（例：毎月末日）">
      <div class="rt-form-btns">
        <button class="rt-save-btn" onclick="addRoutine()">追加</button>
        <button class="rt-cancel-btn" onclick="toggleRtForm()">キャンセル</button>
      </div>
    </div>
  </div>

  <!-- 削除済み（元に戻す） -->
  <div class="card full" id="trash-card" style="padding:12px 16px;display:none;">
    <div class="card-title" style="margin-bottom:6px;">🗑 削除済み（元に戻せます・30日で自動消去）</div>
    <div id="trash-list"></div>
  </div>

  <!-- ═══ 更新ログ ═══ -->
  <div class="section-head">📝 更新ログ</div>
  <div class="card full">
    <div class="log-item">###UPDATED### — 完了タスク履歴ページ追加（12時間グレー→別ページ移行）</div>
    <div class="log-item">2026-07-03 — メール反映自動化・ルーチン業務追加（月5日・10日）</div>
    <div class="log-item">2026-07-03 — タスク展開・ルーチン1週間表示・アーカイブ復元修正</div>
    <div class="log-item">2026-07-03 — メールクリック展開・体験KPI・ANA便カレンダー反映</div>
    <div class="log-item">2026-07-03 — ファーストビュー3分割・アーカイブボタン・4KPI・メール一覧HTML</div>
    <div class="log-item">2026-06-25 — ダッシュボード新規作成</div>
  </div>

</div>

<script>
(function(){
  var now=Date.now(),H13=13*3600*1000;
  // 旧ks_arch_v1移行
  try{var old=JSON.parse(localStorage.getItem('ks_arch_v1')||'[]');if(old.length){var cur=_gd(),curIds=cur.map(function(i){return i.id;});old.forEach(function(id){if(!curIds.includes(id))cur.push({id:id,text:'',completedAt:now-H13});});_sd(cur);localStorage.removeItem('ks_arch_v1');}}catch(e){}
  // ローカルを即座に適用
  applyDoneState(_gd());
  trashPurgeOld();
  applyBuiltinRoutineDismissed();
  renderCustomRoutines();renderExtraTasks();renderTrash();
  loadApiCost();
  applyUnifiedBlocklist();
  applyJustHidden();
  if(GH_TOKEN){ghGetSenderRules().then(function(r){
    const hidden=[];
    Object.keys(r.rules||{}).forEach(function(acct){
      Object.keys(r.rules[acct]||{}).forEach(function(domain){
        if(r.rules[acct][domain]==='hide')hidden.push(acct+'|'+domain);
      });
    });
    _sub(hidden);applyUnifiedBlocklist();
  });}
  // GitHubと同期して再適用
  if(GH_TOKEN){ghGet().then(function(r){const merged=mergeDone(_gd(),r.list);_sd(merged);applyDoneState(merged);renderExtraTasks();const ids=function(l){return l.map(function(i){return i.id;}).sort().join(',');};if(ids(merged)!==ids(r.list))ghPut(merged,r.sha);});}
})();
</script>
</body>
</html>"""

# ── Task History HTML (reads localStorage – no encryption needed) ────────
TASK_HISTORY_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>完了済みタスク履歴</title>
<style>
:root{--blue:#2563EB;--red:#DC2626;--sub:#6B7280;--border:#E5E7EB;--bg:#F9FAFB;--text:#111827;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Helvetica Neue',Arial,sans-serif;background:var(--bg);color:var(--text);padding:20px;}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding-bottom:12px;border-bottom:2px solid var(--border);}
header h1{font-size:18px;font-weight:700;}
.hdr-btns{display:flex;gap:8px;align-items:center;}
.back-link{font-size:13px;color:var(--blue);text-decoration:none;border:1px solid var(--blue);border-radius:6px;padding:4px 12px;}
.back-link:hover{background:#eff6ff;}
.clear-btn{font-size:12px;color:var(--sub);background:none;border:1px solid var(--border);border-radius:6px;padding:4px 12px;cursor:pointer;}
.clear-btn:hover{border-color:var(--red);color:var(--red);}
.sync-status{font-size:11px;color:var(--sub);margin-bottom:12px;}
.section{background:white;border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:14px;}
.section-title{font-size:12px;font-weight:600;color:var(--sub);margin-bottom:10px;text-transform:uppercase;letter-spacing:.04em;}
.hist-item{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px;}
.hist-item:last-child{border-bottom:none;}
.hist-date{min-width:90px;flex-shrink:0;font-size:11px;color:var(--sub);}
.hist-text{flex:1;color:var(--text);}
.restore-btn{font-size:11px;color:var(--blue);background:none;border:1px solid var(--blue);border-radius:4px;padding:2px 8px;cursor:pointer;flex-shrink:0;}
.restore-btn:hover{background:#eff6ff;}
.restore-btn:disabled{opacity:.4;cursor:default;}
.empty{font-size:13px;color:var(--sub);padding:12px 0;}
</style>
</head>
<body>
<header>
  <h1>✅ 完了済みタスク履歴</h1>
  <div class="hdr-btns">
    <button class="clear-btn" onclick="clearHistory()">全件削除</button>
    <a class="back-link" href="kanesaka-tasks.html">← ダッシュボードへ</a>
  </div>
</header>
<div class="sync-status" id="sync-status">🔄 GitHubと同期中...</div>
<div id="hist-container"><p class="empty">読み込み中...</p></div>
<script>
const DONE_KEY='ks_done_v1';
const GH_TOKEN='###GITHUB_TOKEN###';
const GH_REPO='kanesaka849/secure-note-page';
const GH_DONE='done_state.json';
function _gd(){try{return JSON.parse(localStorage.getItem(DONE_KEY)||'[]');}catch{return[];}}
function _sd(list){localStorage.setItem(DONE_KEY,JSON.stringify(list));}
function _esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmtDate(ts){var d=new Date(ts);return(d.getMonth()+1)+'/'+d.getDate()+' '+d.getHours()+':'+String(d.getMinutes()).padStart(2,'0');}
async function ghGet(){try{const r=await fetch('https://api.github.com/repos/'+GH_REPO+'/contents/'+GH_DONE,{headers:{'Authorization':'token '+GH_TOKEN,'Accept':'application/vnd.github.v3+json'}});if(!r.ok)return{list:[],sha:null};const d=await r.json();const l=JSON.parse(decodeURIComponent(escape(atob(d.content.replace(/\\n/g,'')))));return{list:Array.isArray(l)?l:[],sha:d.sha};}catch(e){return{list:[],sha:null};}}
async function ghPut(list,sha){for(let i=0;i<3;i++){try{const b=btoa(unescape(encodeURIComponent(JSON.stringify(list))));const body={message:'sync',content:b};if(sha)body.sha=sha;const r=await fetch('https://api.github.com/repos/'+GH_REPO+'/contents/'+GH_DONE,{method:'PUT',headers:{'Authorization':'token '+GH_TOKEN,'Content-Type':'application/json','Accept':'application/vnd.github.v3+json'},body:JSON.stringify(body)});if(r.status===200||r.status===201)return;}catch(e){}const g=await ghGet();list=mergeDone(list,g.list);sha=g.sha;_sd(list);}}
function mergeDone(a,b){const m={};[...a,...b].forEach(function(i){const c=m[i.id];if(!c||i.completedAt>c.completedAt){m[i.id]=Object.assign({},i);if(c&&c.cleaned)m[i.id].cleaned=true;}else if(i.cleaned){c.cleaned=true;}});return Object.values(m);}
var _ghSha=null;
function render(list){
  var el=document.getElementById('hist-container');
  list=list.slice().sort(function(a,b){return b.completedAt-a.completedAt;});
  if(!list.length){el.innerHTML='<p class="empty">完了済みタスクはありません</p>';return;}
  var now=Date.now(),H12=12*3600*1000;
  var recent=list.filter(function(i){return now-i.completedAt<H12;});
  var old=list.filter(function(i){return now-i.completedAt>=H12;});
  var html='';
  function rows(items,label){
    html+='<div class="section"><div class="section-title">'+label+'</div>';
    items.forEach(function(item){html+='<div class="hist-item"><span class="hist-date">'+fmtDate(item.completedAt)+'</span><span class="hist-text">'+_esc(item.text||item.id)+'</span><button class="restore-btn" id="rb-'+item.id.replace(/[^a-z0-9]/gi,'-')+'" onclick="restore(\\''+item.id+'\\')">戻す</button></div>';});
    html+='</div>';
  }
  if(recent.length)rows(recent,'⏳ 12時間以内（ダッシュボードにグレー表示中）');
  if(old.length)rows(old,'📁 完了済み（ダッシュボードから非表示）');
  el.innerHTML=html;
}
async function restore(id){
  var btn=document.getElementById('rb-'+id.replace(/[^a-z0-9]/gi,'-'));
  if(btn)btn.disabled=true;
  var list=_gd().filter(function(i){return i.id!==id;});
  _sd(list);
  var r=await ghGet();
  var merged=r.list.filter(function(i){return i.id!==id;});
  _sd(merged);await ghPut(merged,r.sha);_ghSha=null;
  document.getElementById('sync-status').textContent='✅ 同期完了';
  render(_gd());
}
async function clearHistory(){
  if(!confirm('全件削除してGitHubにも反映しますか？'))return;
  _sd([]);
  var r=await ghGet();await ghPut([],r.sha);
  document.getElementById('sync-status').textContent='✅ 全件削除・同期完了';
  render([]);
}
(async function(){
  var r=await ghGet();
  var merged=mergeDone(_gd(),r.list);
  _sd(merged);_ghSha=r.sha;
  document.getElementById('sync-status').textContent='✅ GitHub同期済み（'+merged.length+'件）';
  render(merged);
})();
</script>
</body>
</html>"""

# ── Mail List HTML (static) ─────────────────────────────────────────────
MAIL_LIST_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>金坂 メール一覧</title>
<style>
  :root{--bg:#f4f5f7;--card:#fff;--red:#c0392b;--orange:#e67e22;--green:#27ae60;--blue:#2980b9;--gray:#7f8c8d;--purple:#8e44ad;--teal:#16a085;--text:#2c3e50;--sub:#636e72;--border:#dfe6e9;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Hiragino Sans','Meiryo',sans-serif;background:var(--bg);color:var(--text);font-size:14px;}
  header{background:#2c3e50;color:white;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;gap:16px;}
  header h1{font-size:16px;font-weight:bold;}
  header a{color:#94a3b8;font-size:13px;text-decoration:none;}
  header a:hover{color:white;}
  .updated{font-size:11px;color:#94a3b8;}
  .container{max-width:900px;margin:0 auto;padding:14px;}
  .card{background:var(--card);border-radius:10px;padding:14px 16px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:8px;}
  .section-head{font-size:13px;font-weight:bold;color:var(--sub);margin:16px 0 8px;display:flex;align-items:center;gap:6px;}
  .section-head::after{content:'';flex:1;height:1px;background:var(--border);}
  .mail-row{display:flex;align-items:flex-start;gap:10px;padding:9px 0;border-bottom:1px solid var(--border);}
  .mail-row:last-child{border-bottom:none;}
  .mail-date{min-width:65px;font-size:11px;color:var(--sub);padding-top:2px;}
  .mail-badge{flex-shrink:0;}
  .mail-body{flex:1;}
  .mail-subject{font-size:13px;font-weight:bold;margin-bottom:2px;}
  .mail-from{font-size:11px;color:var(--sub);margin-bottom:2px;}
  .mail-snippet{font-size:11px;color:#7f8c8d;line-height:1.4;}
  .badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:bold;}
  .badge-red{background:#ffeaea;color:var(--red);}
  .badge-orange{background:#fff3e0;color:var(--orange);}
  .badge-blue{background:#e3f2fd;color:var(--blue);}
  .badge-gray{background:#f1f2f6;color:var(--gray);}
  .badge-green{background:#e8f5e9;color:var(--green);}
  .kanasaka-highlight{background:#fffbea;border-left:3px solid #f39c12;padding-left:8px;border-radius:0 4px 4px 0;}
</style>
</head>
<body>
<header>
  <h1>📬 メール一覧（整理済み）</h1>
  <a href="kanesaka-tasks.html">← ダッシュボードへ戻る</a>
  <div class="updated">集計日：###UPDATED###</div>
</header>
<div class="container">
  <div class="section-head">🔴 要対応・重要</div>
  <div class="card">
    <div class="mail-row kanasaka-highlight">
      <div class="mail-date">7/3 09:56</div>
      <div class="mail-badge"><span class="badge badge-red">🔐 緊急</span></div>
      <div class="mail-body">
        <div class="mail-subject">【弥生】弥生IDロックのお知らせ（金坂二郎）</div>
        <div class="mail-from">mypage@yayoi-kk.co.jp → kanesaka@activia.co.jp</div>
        <div class="mail-snippet">連続ログイン失敗でアカウント一時ロック。パスワード再発行・変更完了済。不正アクセスの可能性あり</div>
      </div>
    </div>
    <div class="mail-row">
      <div class="mail-date">7/3 01:40</div>
      <div class="mail-badge"><span class="badge badge-red">⚖️ 法務</span></div>
      <div class="mail-body">
        <div class="mail-subject">【ZEUS】お支払い手続きご案内（ベリーベスト法律事務所）</div>
        <div class="mail-from">mailinfo@cardservice.co.jp → kanesaka@activia.co.jp</div>
        <div class="mail-snippet">法律顧問費用のクレジットカード支払いリンク。期日内に支払いURLへアクセスを</div>
      </div>
    </div>
    <div class="mail-row">
      <div class="mail-date">7/3 03:23</div>
      <div class="mail-badge"><span class="badge badge-orange">👥 社内</span></div>
      <div class="mail-body">
        <div class="mail-subject">8月ヘルプ募集中①（AGNIYOGA フロント）</div>
        <div class="mail-from">frontsv@agniyoga.jp → agni_frontall@googlegroups.com</div>
        <div class="mail-snippet">内田より全フロントスタッフへ。8月シフトヘルプ希望者はfrontsv@agniyoga.jpへ返信</div>
      </div>
    </div>
  </div>
  <div class="section-head">💼 ビジネス・金融</div>
  <div class="card">
    <div class="mail-row">
      <div class="mail-date">7/2 23:00</div>
      <div class="mail-badge"><span class="badge badge-blue">🏦 銀行</span></div>
      <div class="mail-body">
        <div class="mail-subject">BizSTATION アプリ更新・認証情報再登録のお願い（三菱UFJ）</div>
        <div class="mail-from">mail@bizstation-ml2.bk.mufg.jp → kanesaka@activia.co.jp</div>
        <div class="mail-snippet">アプリを最新版に更新後、認証情報の再登録が必要</div>
      </div>
    </div>
    <div class="mail-row">
      <div class="mail-date">7/2 18:58</div>
      <div class="mail-badge"><span class="badge badge-orange">📧 HubSpot</span></div>
      <div class="mail-body">
        <div class="mail-subject">Eメールトラッキングの同意要件が変更されます</div>
        <div class="mail-from">noreply@notifications.hubspot.com → kanesaka@activia.co.jp</div>
        <div class="mail-snippet">フランス・イタリアへのトラッキング同意要件変更（2026年8月〜）</div>
      </div>
    </div>
    <div class="mail-row">
      <div class="mail-date">7/1</div>
      <div class="mail-badge"><span class="badge badge-gray">📄 請求</span></div>
      <div class="mail-body">
        <div class="mail-subject">【クラウドサイン】利用明細送付（2026年06月ご利用分）</div>
        <div class="mail-from">billing_support@cloudsign.jp → kanesaka@activia.co.jp</div>
        <div class="mail-snippet">株式会社アクティビア 6月分クラウドサイン利用明細</div>
      </div>
    </div>
  </div>
  <div class="section-head">📰 ニュースレター・DM（要対応なし）</div>
  <div class="card">
    <div style="font-size:12px;color:var(--sub);line-height:1.8;padding:4px 0;">
      ITトレンド / bizocean / さくらインターネット / GAP / ぐるなび / Netflix / J.LEAGUE /
      明治安田生命 / デザインAC / お名前.com / その他マーケティングメール … など多数
    </div>
  </div>
</div>
</body>
</html>"""

# ── Mail Rules / AI Logic Viewer ─────────────────────────────────────────
MAIL_RULES_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>送信元ルール・AI判断ロジック</title>
<style>
  :root{--bg:#f4f5f7;--card:#fff;--blue:#2980b9;--text:#2c3e50;--sub:#636e72;--border:#dfe6e9;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Hiragino Sans','Meiryo',sans-serif;background:var(--bg);color:var(--text);font-size:14px;}
  header{background:#2c3e50;color:white;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;gap:16px;}
  header h1{font-size:16px;font-weight:bold;}
  header a{color:#94a3b8;font-size:13px;text-decoration:none;}
  header a:hover{color:white;}
  .container{max-width:1000px;margin:0 auto;padding:14px;}
  .card{background:var(--card);border-radius:10px;padding:16px 18px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px;}
  .card-title{font-size:14px;font-weight:bold;margin-bottom:10px;display:flex;align-items:center;gap:8px;}
  .text-sm{font-size:13px;line-height:1.9;}
  .text-sm strong{color:var(--blue);}
  .cat-list{list-style:none;margin:6px 0 12px;}
  .cat-list li{padding:4px 0 4px 10px;border-left:3px solid var(--border);margin-bottom:4px;}
  .search-box{width:100%;padding:9px 14px;border:1px solid var(--border);border-radius:8px;margin-bottom:12px;font-size:13px;}
  table{width:100%;border-collapse:collapse;font-size:12px;}
  th,td{padding:6px 8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top;}
  th{color:var(--sub);font-size:11px;text-transform:uppercase;letter-spacing:.03em;}
  tr:hover td{background:#f8fafc;}
  .acct-pill{display:inline-block;font-size:10px;font-weight:bold;padding:2px 7px;border-radius:8px;background:#eef2ff;color:#4338ca;white-space:nowrap;}
  .cat-pill{display:inline-block;font-size:10px;font-weight:bold;padding:2px 7px;border-radius:8px;}
  .cat-action{background:#fee2e2;color:#b91c1c;}
  .cat-info{background:#dbeafe;color:#1d4ed8;}
  .cat-unclear{background:#f3e8ff;color:#7e22ce;}
  .cat-maybe_spam{background:#fef9c3;color:#a16207;}
  .cat-hide{background:#f1f5f9;color:#64748b;}
  .cat-partial{background:#ffedd5;color:#c2410c;}
  #rules-status{font-size:12px;color:var(--sub);padding:8px 0;}
</style>
</head>
<body>
<header>
  <h1>🔍 送信元ルール・AI判断ロジック</h1>
  <a href="kanesaka-tasks.html">← ダッシュボードへ戻る</a>
</header>
<div class="container">
  <div class="card">
    <div class="card-title">📖 AIはどう判断しているか</div>
    <div class="text-sm">
      統合メール一覧は、送信元ごとに学習した<strong>ルール（下の表）</strong>で確定するものはAIを呼ばず即決定し、
      まだ確定していない新しい送信元だけAnthropic APIに判定させる仕組みです。<br><br>
      <strong>4分類＋非表示</strong>
      <ul class="cat-list">
        <li><span class="cat-pill cat-action">action 要対応</span> 支払期限・重要な手続き・セキュリティ警告など対応が必要な連絡</li>
        <li><span class="cat-pill cat-info">info お知らせ</span> 対応不要だが知っておくべき業務連絡・システム通知</li>
        <li><span class="cat-pill cat-unclear">unclear 宛先不明</span> 本人宛とは分かるが重要度が読み取りにくく、本人の判断が必要</li>
        <li><span class="cat-pill cat-maybe_spam">maybe_spam スパムか不明</span> 広告・営業の可能性が高いが確信は持てない</li>
        <li><span class="cat-pill cat-hide">hide 非表示</span> 明らかな広告・スパムと確信できる場合のみ、一覧から除外</li>
      </ul>
      <strong>判断に迷う場合は必ずunclear（表示する側）に倒します</strong>（見落としより誤表示の方が害が少ないため）。<br>
      正規の通知を装ったフィッシングの疑いがある場合は<strong>phishing_suspected</strong>フラグが立ち、
      カテゴリに関わらず「⚠️【フィッシング注意】」を付けて必ず表示します（非表示にはしません）。<br>
      セキュリティ通知など判断の参考になる場合は、AIが<strong>💡一言アドバイス（recommend）</strong>も添えます。<br><br>
      <strong>✕（送信元を完全非表示）</strong>は今後そのアカウントからの全カテゴリを非表示にします。
      <strong>△（カテゴリ限定非表示）</strong>はそのカテゴリだけを非表示にします（下表で
      <span class="cat-pill cat-partial">一部非表示</span>と表示されているものが△で設定した送信元です）。
      いずれも「要対応(action)」を対象にする場合は、重要な連絡を見落とすリスクがあるため確認ダイアログが出ます。
    </div>
  </div>
  <div class="card">
    <div class="card-title">📋 学習済みの送信元ルール一覧 <span id="rule-count" style="font-weight:normal;color:var(--sub);font-size:12px;"></span></div>
    <input type="text" class="search-box" id="rule-search" placeholder="送信元・アカウント・カテゴリで検索…" oninput="filterRules()">
    <div id="rules-status">読み込み中…</div>
    <table id="rules-table" style="display:none;">
      <thead><tr><th>アカウント</th><th>送信元</th><th>判定</th></tr></thead>
      <tbody id="rules-tbody"></tbody>
    </table>
  </div>
</div>
<script>
const GH_REPO='kanesaka849/secure-note-page';
const ACCOUNT_LABELS_JS={
  kanesaka_activia:'Activia', kanesaka_agni:'個人', agniyoga_ad:'広告用',
  zipyoga:'ZIP問合せ', kanesaka_agniyoga:'agniyoga'
};
let ALL_ROWS=[];
function _esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function loadRules(){
  try{
    const r=await fetch('https://raw.githubusercontent.com/'+GH_REPO+'/main/sender_rules.json?_='+Date.now());
    if(!r.ok){document.getElementById('rules-status').textContent='読み込み失敗（'+r.status+'）';return;}
    const rules=await r.json();
    renderRules(rules);
  }catch(e){document.getElementById('rules-status').textContent='エラー：'+e.message;}
}
function catPillHtml(v){
  if(typeof v==='string'){
    return `<span class="cat-pill cat-${_esc(v)}">${_esc(v)}</span>`;
  }
  if(v&&Array.isArray(v.hide_categories)){
    return `<span class="cat-pill cat-partial">一部非表示: ${_esc(v.hide_categories.join('・'))}</span>`;
  }
  return _esc(JSON.stringify(v));
}
function renderRules(rules){
  const rows=[];
  Object.keys(rules).forEach(function(acct){
    const accLabel=ACCOUNT_LABELS_JS[acct]||acct;
    Object.keys(rules[acct]||{}).forEach(function(sender){
      rows.push({acct:accLabel,acctKey:acct,sender:sender,cat:rules[acct][sender]});
    });
  });
  rows.sort(function(a,b){return a.acct.localeCompare(b.acct)||a.sender.localeCompare(b.sender);});
  ALL_ROWS=rows;
  document.getElementById('rule-count').textContent='（'+rows.length+'件）';
  document.getElementById('rules-status').style.display='none';
  document.getElementById('rules-table').style.display='';
  filterRules();
}
function filterRules(){
  const q=(document.getElementById('rule-search').value||'').toLowerCase();
  const tbody=document.getElementById('rules-tbody');
  const filtered=ALL_ROWS.filter(function(r){
    if(!q)return true;
    return r.sender.toLowerCase().includes(q)||r.acct.toLowerCase().includes(q)||JSON.stringify(r.cat).toLowerCase().includes(q);
  });
  tbody.innerHTML=filtered.map(function(r){
    return `<tr><td><span class="acct-pill">${_esc(r.acct)}</span></td><td>${_esc(r.sender)}</td><td>${catPillHtml(r.cat)}</td></tr>`;
  }).join('');
}
loadRules();
</script>
</body>
</html>"""

# ── Git Operations ──────────────────────────────────────────────────────
GIT = r"C:\Program Files\Git\bin\git.exe"
if not os.path.exists(GIT):
    GIT = 'git'

GIT_ENV = {**os.environ,
           'GIT_AUTHOR_NAME': 'kanesaka',
           'GIT_AUTHOR_EMAIL': 'kanesaka.agni@gmail.com',
           'GIT_COMMITTER_NAME': 'kanesaka',
           'GIT_COMMITTER_EMAIL': 'kanesaka.agni@gmail.com'}

def setup_repo():
    if not os.path.exists(os.path.join(REPO_DIR, '.git')):
        print(f"リポジトリをクローン中: {REPO_URL}")
        subprocess.run([GIT, 'clone', REPO_URL, REPO_DIR], check=True)
        subprocess.run([GIT, '-C', REPO_DIR, 'config', 'user.email', 'kanesaka.agni@gmail.com'], check=True)
        subprocess.run([GIT, '-C', REPO_DIR, 'config', 'user.name', 'kanesaka'], check=True)

def check_network():
    """GitHubに到達できるか事前確認。失敗したら早期終了。"""
    import socket
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo('github.com', 443)
    except socket.gaierror:
        print("⚠️ ネットワークエラー：github.com に接続できません（DNS失敗）")
        print("   → ルーターを再起動するか、DNSを 8.8.8.8 に変更してください")
        print("   → HTMLは output/ に保存済みです。接続回復後に deploy.bat を実行してください")
        return False
    except OSError:
        print("⚠️ ネットワークエラー：インターネット接続を確認してください")
        return False
    return True

def git_push(today):
    subprocess.run([GIT, '-C', REPO_DIR, 'add', '-A'], check=True, env=GIT_ENV)
    result = subprocess.run([GIT, '-C', REPO_DIR, 'status', '--porcelain'],
                            capture_output=True, text=True)
    if not result.stdout.strip():
        print("変更なし。pushをスキップ")
        return
    subprocess.run([GIT, '-C', REPO_DIR, 'commit', '-m', f'update {today}'],
                   check=True, env=GIT_ENV)
    if not check_network():
        return
    subprocess.run([GIT, '-C', REPO_DIR, 'push'], check=True, env=GIT_ENV)
    print("✅ GitHub Pages にデプロイ完了")
    print("→ https://kanesaka849.github.io/secure-note-page/kanesaka-tasks.html")

# ── Main ────────────────────────────────────────────────────────────────
now   = datetime.now(JST)
today = now.strftime('%Y-%m-%d %H:%M')
year  = now.year
month = now.month

# GitHub トークン読み込み（クライアント側done_state.json同期用・ワークフロー自体の認証とは別物）
gh_token = os.environ.get('DASHBOARD_SYNC_TOKEN', '') if CI_MODE else ''
if not CI_MODE and os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, encoding='utf-8') as f:
        gh_token = f.read().strip()
if gh_token:
    print(f"✅ GitHubトークン読み込み済み ({gh_token[:12]}...)")
else:
    print("⚠️ GitHubトークンが見つかりません（同期機能は無効）")

print(f"=== 金坂ダッシュボード ビルド ({today}) ===")

# 1) Load judgment file
if not os.path.exists(JUDGMENT_FILE):
    print("⚠️ mail_judgment.json が見つかりません。Claudeに「メール反映して」と指示してください。")
    sys.exit(1)

with open(JUDGMENT_FILE, encoding='utf-8') as f:
    judgment = json.load(f)

# 2) Count KPIs from mail JSONs
trials = setsumeikai_list = shiryo_list = []
try:
    with open(TRIAL_FILE, encoding='utf-8') as f:
        trial_data = json.load(f)
    trials = count_trials(trial_data, year, month)
    print(f"体験予約（今月）: {len(trials)}件")
except Exception as e:
    print(f"trial JSON読み込みエラー: {e}")

try:
    with open(SCHOOL_FILE, encoding='utf-8') as f:
        school_data = json.load(f)
    setsumeikai_list = count_setsumeikai(school_data, year, month)
    shiryo_list      = count_shiryo(school_data, year, month)
    print(f"説明会（今月）: {len(setsumeikai_list)}件 / 資料請求（今月）: {len(shiryo_list)}件")
except Exception as e:
    print(f"school JSON読み込みエラー: {e}")

kpi_note   = judgment.get('kpi_note', '')

# 2b) 統合メール一覧（kanesaka_activia / kanesaka.agni / agniyoga.ad / zipyoga / kanesaka_agniyoga の
# 5アカウントをAIが仕分け済み）
# ※このファイルは「メール反映」ボタン（ci_fetch_and_judge.py）が更新する。
#   ローカルでrebuild_all.pyを実行してもここでは再取得しない
#   （手元の古いコピーでライブのAI判定済みデータを上書きしないため）。
unified_mails = []
unified_mail_fetched_at = ''
try:
    with open(UNIFIED_MAIL_FILE, encoding='utf-8') as f:
        um = json.load(f)
    unified_mails = um.get('mails', [])
    unified_mail_fetched_at = um.get('updated', '')
    print(f"統合メール: {len(unified_mails)}件（要対応{sum(1 for m in unified_mails if m.get('category')=='action')}件）")
except Exception as e:
    print(f"統合メールJSON読み込みエラー（「メール反映」未実行の可能性）: {e}")

training_list = count_training_contracts(unified_mails, year, month)
n_training = len(training_list)
print(f"養成講座契約（今月）: {n_training}件")

# 2d) Googleカレンダー（kanesaka.agni@gmail.com プライマリ）
calendar_events = []
calendar_fetched_at = ''
try:
    import subprocess as _sp2
    _calendar_script = (os.path.join(PROJECT_DIR, '.github', 'scripts', 'check_calendar.py')
                         if CI_MODE else os.path.join(INPUT_DIR, 'check_calendar.py'))
    _sp2.run([sys.executable, _calendar_script], check=True, capture_output=True)
except Exception as e:
    print(f"カレンダー取得エラー（スキップ）: {e}")
try:
    with open(CALENDAR_FILE, encoding='utf-8') as f:
        cal = json.load(f)
    calendar_events = cal.get('events', [])
    calendar_fetched_at = cal.get('fetched_at', '')
    print(f"カレンダー予定: {len(calendar_events)}件")
except Exception as e:
    print(f"カレンダーJSON読み込みエラー: {e}")

# 3) Generate dynamic sections
action_mail_html = generate_unified_mail_section(unified_mails, filter_categories={'action'})
info_mail_html = generate_unified_mail_section(unified_mails, filter_categories={'info'})
unclear_mail_html = generate_unified_mail_section(unified_mails, filter_categories={'unclear'})
maybe_spam_mail_html = generate_unified_mail_section(unified_mails, filter_categories={'maybe_spam'})
kpi_html         = generate_kpi_section(
    trials, shiryo_list, setsumeikai_list, n_training,
    kpi_note, today, month
)
routine_html = generate_routine_tasks(now.date())
schedule_html = generate_schedule_section(calendar_events, now.date())
today_calendar_tasks_html = generate_today_calendar_tasks(calendar_events, now.date())

# 4) Assemble Dashboard HTML
dashboard_html = (DASHBOARD_TEMPLATE
    .replace('###ACTION_MAIL_SECTION###', action_mail_html)
    .replace('###MAIL_INFO_SECTION###', info_mail_html)
    .replace('###MAIL_UNCLEAR_SECTION###', unclear_mail_html)
    .replace('###MAIL_MAYBE_SPAM_SECTION###', maybe_spam_mail_html)
    .replace('###UNIFIED_MAIL_FETCHED###', f'取得: {unified_mail_fetched_at}')
    .replace('###SCHEDULE_SECTION###', schedule_html)
    .replace('###CALENDAR_FETCHED###', f'取得: {calendar_fetched_at}')
    .replace('###KPI_SECTION###', kpi_html)
    .replace('###ROUTINE_TASKS###', routine_html)
    .replace('###TODAY_CALENDAR_TASKS###', today_calendar_tasks_html)
    .replace('###MONTH###', f'{month}月')
    .replace('###UPDATED###', today)
    .replace('###GITHUB_TOKEN###', gh_token))

# 5) Assemble Mail List HTML
mail_list_html = MAIL_LIST_HTML.replace('###UPDATED###', today)

# 5b) Assemble History HTML (with token, then encrypt)
hist_html = TASK_HISTORY_HTML.replace('###GITHUB_TOKEN###', gh_token)

# 5c) Assemble Mail Rules / AI Logic viewer HTML（送信元ルールは公開リポジトリのraw経由で読むためtoken不要）
rules_html = MAIL_RULES_HTML

# 6) Encrypt
print("暗号化中...")
S1, I1, C1 = encrypt(dashboard_html)
S2, I2, C2 = encrypt(mail_list_html)
S3, I3, C3 = encrypt(hist_html)
S4, I4, C4 = encrypt(rules_html)
main_wrapper  = make_wrapper("金坂 タスク管理", S1, I1, C1)
mail_wrapper  = make_wrapper("金坂 メール一覧", S2, I2, C2, back_link="kanesaka-tasks.html")
hist_wrapper  = make_wrapper("完了済みタスク履歴", S3, I3, C3, back_link="kanesaka-tasks.html")
rules_wrapper = make_wrapper("送信元ルール・AI判断ロジック", S4, I4, C4, back_link="kanesaka-tasks.html")

# 7) Write output
os.makedirs(OUTPUT_DIR, exist_ok=True)
for path, html in [(OUT_MAIN, main_wrapper), (OUT_MAIL, mail_wrapper), (OUT_HIST, hist_wrapper), (OUT_RULES, rules_wrapper)]:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ {os.path.basename(path)} ({len(html):,} chars)")

# 8) Git push
# CI_MODEではOUTPUT_DIR=REPO_DIR=ワークスペース直下（同一パス）なので
# コピー不要・git操作もワークフロー側のステップに任せる（GITHUB_TOKENで認証）。
if not CI_MODE:
    try:
        setup_repo()
        os.makedirs(os.path.dirname(REPO_MAIN), exist_ok=True)
        shutil.copy2(OUT_MAIN, REPO_MAIN)
        shutil.copy2(OUT_MAIL, REPO_MAIL)
        shutil.copy2(OUT_HIST, REPO_HIST)
        shutil.copy2(OUT_RULES, REPO_RULES)
        print("リポジトリへコピー完了")
        git_push(today)
    except Exception as e:
        print(f"⚠️ git操作エラー: {e}")
        print("手動でrepository にコピーしてgit pushしてください")

print("\n=== 完了 ===")
