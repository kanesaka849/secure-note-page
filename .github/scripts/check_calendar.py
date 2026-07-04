"""
check_calendar.py
kanesaka.agni@gmail.com のGoogleカレンダー（プライマリ）から今後の予定を取得
→ calendar_events.json に保存
"""
import sys, json, os, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

JST = timezone(timedelta(hours=9))

CI_MODE = os.environ.get('GITHUB_ACTIONS') == 'true'

if CI_MODE:
    CLIENT_ID     = os.environ['GMAIL_CLIENT_ID']
    CLIENT_SECRET = os.environ['GMAIL_CLIENT_SECRET']
    REFRESH_TOKEN = os.environ.get('KANESAKA_CALENDAR_REFRESH_TOKEN', '')
    OUT_DIR = os.environ.get('CI_INPUT_DIR', os.path.join(os.environ.get('GITHUB_WORKSPACE', '.'), 'ci_input'))
else:
    sys.path.insert(0, 'Z:/claude/shared/apikeys')
    from keystore import load as _ks_load
    _secrets = _ks_load(os.environ.get('KEYSTORE_PASSWORD'))
    CLIENT_ID     = _secrets['GMAIL_CLIENT_ID']
    CLIENT_SECRET = _secrets['GMAIL_CLIENT_SECRET']
    REFRESH_TOKEN = _secrets.get('KANESAKA_CALENDAR_REFRESH_TOKEN', '')
    OUT_DIR = os.path.join(os.path.dirname(__file__))

os.makedirs(OUT_DIR, exist_ok=True)
OUT_FILE = os.path.join(OUT_DIR, 'calendar_events.json')

DAYS_AHEAD = 35
MAX_RESULTS = 40


def get_access_token():
    data = urllib.parse.urlencode({
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())['access_token']
    except urllib.error.HTTPError as e:
        print(f'OAuth token error {e.code}: {e.read().decode("utf-8", errors="replace")}')
        raise


def fetch_events():
    if not REFRESH_TOKEN:
        print('KANESAKA_CALENDAR_REFRESH_TOKEN未設定のためスキップ')
        with open(OUT_FILE, 'w', encoding='utf-8') as f:
            json.dump({'fetched_at': '', 'events': []}, f, ensure_ascii=False, indent=2)
        return

    token = get_access_token()
    now = datetime.now(timezone.utc)
    time_min = now.isoformat().replace('+00:00', 'Z')
    time_max = (now + timedelta(days=DAYS_AHEAD)).isoformat().replace('+00:00', 'Z')

    params = urllib.parse.urlencode({
        'timeMin': time_min,
        'timeMax': time_max,
        'maxResults': MAX_RESULTS,
        'singleEvents': 'true',
        'orderBy': 'startTime',
    })
    req = urllib.request.Request(
        f'https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}',
        headers={'Authorization': f'Bearer {token}'}
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())

    events = []
    for item in result.get('items', []):
        if item.get('status') == 'cancelled':
            continue
        start = item.get('start', {})
        start_dt = start.get('dateTime') or start.get('date')
        all_day = 'date' in start and 'dateTime' not in start
        events.append({
            'id': item.get('id', ''),
            'summary': item.get('summary', '（タイトルなし）'),
            'start': start_dt,
            'all_day': all_day,
            'location': item.get('location', ''),
        })

    out = {
        'fetched_at': datetime.now(JST).strftime('%Y-%m-%d %H:%M'),
        'events': events,
    }
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f'{len(events)}件の予定を保存 → {OUT_FILE}')


if __name__ == '__main__':
    try:
        fetch_events()
    except Exception as e:
        print(f'カレンダー取得エラー: {e}')
