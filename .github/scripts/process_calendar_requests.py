"""
process_calendar_requests.py
ダッシュボードの「＋予定追加」「🗑削除」ボタンが ci_trigger/calendar_requests.json に
コミットした依頼を Google カレンダー（kanesaka.agni@gmail.com primary）へ反映する。
処理後は依頼ファイルを空にする（後段の Commit ステップで一緒に push される）。
CI（GitHub Actions）専用。
"""
import json, os, sys, urllib.request, urllib.parse
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

if os.environ.get('GITHUB_ACTIONS') != 'true':
    print('CI専用スクリプトです（ローカルでは何もしません）')
    sys.exit(0)

WORKSPACE = os.environ.get('GITHUB_WORKSPACE', '.')
REQ_FILE = os.path.join(WORKSPACE, 'ci_trigger', 'calendar_requests.json')

CLIENT_ID     = os.environ['GMAIL_CLIENT_ID']
CLIENT_SECRET = os.environ['GMAIL_CLIENT_SECRET']
REFRESH_TOKEN = os.environ.get('KANESAKA_CALENDAR_REFRESH_TOKEN', '')


def get_access_token():
    data = urllib.parse.urlencode({
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN,
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())['access_token']


def main():
    if not os.path.exists(REQ_FILE):
        print('依頼ファイルなし・スキップ')
        return
    try:
        with open(REQ_FILE, encoding='utf-8') as f:
            reqs = json.load(f).get('requests', [])
    except Exception as e:
        print(f'依頼ファイル読み込みエラー: {e}')
        reqs = []
    if not reqs:
        print('カレンダー依頼0件')
        return
    if not REFRESH_TOKEN:
        print('KANESAKA_CALENDAR_REFRESH_TOKEN未設定・依頼は保留のまま残します')
        return

    token = get_access_token()
    ok = ng = 0
    for r in reqs:
        try:
            if r.get('action') == 'add':
                summary = (r.get('summary') or '').strip()
                date = (r.get('date') or '').strip()      # YYYY-MM-DD
                t = (r.get('time') or '').strip()          # HH:MM または空（終日）
                if not summary or not date:
                    raise ValueError('summary/date必須')
                if t:
                    sdt = datetime.fromisoformat(f'{date}T{t}:00+09:00')
                    edt = sdt + timedelta(hours=1)
                    body = {'summary': summary,
                            'start': {'dateTime': sdt.isoformat(), 'timeZone': 'Asia/Tokyo'},
                            'end':   {'dateTime': edt.isoformat(), 'timeZone': 'Asia/Tokyo'}}
                else:
                    d = datetime.fromisoformat(date).date()
                    body = {'summary': summary,
                            'start': {'date': date},
                            'end':   {'date': (d + timedelta(days=1)).isoformat()}}
                req = urllib.request.Request(
                    'https://www.googleapis.com/calendar/v3/calendars/primary/events',
                    data=json.dumps(body).encode(), method='POST',
                    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(req) as resp:
                    json.load(resp)
                print(f'追加OK: {summary} {date} {t or "(終日)"}')
                ok += 1
            elif r.get('action') == 'delete':
                eid = (r.get('event_id') or '').strip()
                if not eid:
                    raise ValueError('event_id必須')
                req = urllib.request.Request(
                    f'https://www.googleapis.com/calendar/v3/calendars/primary/events/{urllib.parse.quote(eid)}',
                    method='DELETE', headers={'Authorization': f'Bearer {token}'})
                try:
                    urllib.request.urlopen(req)
                except urllib.error.HTTPError as e:
                    if e.code not in (404, 410):
                        raise
                print(f'削除OK: {eid}')
                ok += 1
            else:
                raise ValueError(f'不明なaction: {r.get("action")}')
        except Exception as e:
            print(f'失敗: {r} → {e}')
            ng += 1

    with open(REQ_FILE, 'w', encoding='utf-8') as f:
        json.dump({'requests': []}, f, ensure_ascii=False, indent=2)
    print(f'完了: 成功{ok}件 / 失敗{ng}件（依頼ファイルをクリア）')


main()
