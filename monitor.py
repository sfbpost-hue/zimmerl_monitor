#!/usr/bin/env python3
"""Tablein availability notifier. Standard library only."""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = 'https://backend.app.tablein.com/api/v2/booking/available-slots'
BOOK = 'https://widget.tablein.com/de/widget/zimmerl-0?widget=5798'
RESTAURANT = '21710858'
STATE_TITLE = '[zimmerl-monitor] state — do not edit'
MONTHS_AHEAD = int(os.getenv('MONTHS_AHEAD', '24'))
WINDOW_DAYS = int(os.getenv('WINDOW_DAYS', '30'))
TOKEN = os.environ['GITHUB_TOKEN']
REPO = os.environ['GITHUB_REPOSITORY']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']


def request(url, method='GET', data=None, headers=None):
    h = {'User-Agent': 'ZimmerlAvailabilityMonitor/1.0', 'Accept': 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as res:
                return json.loads(res.read().decode('utf-8'))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt == 2 or isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 403, 404):
                raise RuntimeError(f'HTTP/Netzwerkfehler bei {url.split("?")[0]}: {e}') from e
            time.sleep(2 ** attempt)


def github(path, method='GET', obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    return request('https://api.github.com/repos/' + REPO + path, method, data, {
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        **({'Content-Type': 'application/json'} if data is not None else {})
    })


def add_months(date, months):
    y = date.year + (date.month - 1 + months) // 12
    m = (date.month - 1 + months) % 12 + 1
    import calendar
    return dt.date(y, m, min(date.day, calendar.monthrange(y, m)[1]))


def fetch_slots():
    start = dt.datetime.now(ZoneInfo('Europe/Vienna')).date()
    end = add_months(start, MONTHS_AHEAD)
    found = {}
    day = start
    while day < end:
        last = min(day + dt.timedelta(days=WINDOW_DAYS), end)
        qs = urllib.parse.urlencode({'start_date': day.isoformat(), 'end_date': last.isoformat(),
                                     'restaurant_id': RESTAURANT, 'guests': 2})
        result = request(BASE + '?' + qs)
        if not isinstance(result, list):
            raise ValueError('Unerwartete API-Antwort: erwartete Liste mit Tagen. Kein Status gespeichert.')
        for item in result:
            if not isinstance(item, dict) or 'date' not in item or 'times' not in item:
                raise ValueError('Unbekanntes Tagesformat. Kein Status gespeichert.')
            date = str(item['date'])[:10]
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                raise ValueError('Ungültiges Datum in API-Antwort')
            if date < start.isoformat() or date > end.isoformat():
                continue
            times = item['times']
            if not isinstance(times, list):
                raise ValueError('Unbekanntes times-Format. Kein Status gespeichert.')
            if item.get('is_closed') is True or not times:
                continue
            # No known positive example yet. Treat each entry as a *possible* bookable slot.
            # Never claim confirmed bookability; link to the booking widget.
            for t in times:
                if isinstance(t, str):
                    label = t
                elif isinstance(t, dict):
                    label = str(t.get('time') or t.get('from') or t.get('start') or t.get('value') or json.dumps(t, sort_keys=True, ensure_ascii=False))
                else:
                    label = str(t)
                key = date + '|' + json.dumps(t, sort_keys=True, ensure_ascii=False)
                found[key] = (date, label)
        day = last
    return found


def get_state_issue():
    issues = github('/issues?state=open&per_page=100')
    for issue in issues:
        if issue.get('title') == STATE_TITLE and 'pull_request' not in issue:
            return issue
    return None


def send_telegram(message):
    data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': message,
                                   'disable_web_page_preview': 'true'}).encode()
    result = request('https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage', 'POST', data,
                     {'Content-Type': 'application/x-www-form-urlencoded'})
    if result.get('ok') is not True:
        raise RuntimeError('Telegram hat die Nachricht abgelehnt')


def main():
    current = fetch_slots()
    issue = get_state_issue()
    if issue:
        try:
            previous = set(json.loads(issue['body'] or '{}')['keys'])
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('Gespeicherter Zustand beschädigt; bitte Issue prüfen.')
    else:
        previous = set()
    new = sorted(set(current) - previous)
    if new:
        for i in range(0, len(new), 15):
            part = new[i:i+15]
            lines = [f'• {current[k][0]} — {current[k][1]}' for k in part]
            send_telegram('🍽️ Zimmerl: mögliche neue Termine für 2 Personen!\n\n'
                          + '\n'.join(lines) + '\n\nJetzt prüfen/buchen:\n' + BOOK
                          + '\n\nHinweis: Verfügbarkeit ist nicht garantiert.')
    # Only commit after successful queries and notifications. Previously vanished slots
    # are removed, so a reappearing cancellation can trigger a fresh alert.
    body = json.dumps({'keys': sorted(current), 'last_checked': dt.datetime.now(dt.timezone.utc).isoformat()}, ensure_ascii=False)
    if issue:
        github('/issues/' + str(issue['number']), 'PATCH', {'body': body})
    else:
        github('/issues', 'POST', {'title': STATE_TITLE, 'body': body})
    print(f'OK: {len(current)} mögliche Slots; {len(new)} neu.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('FEHLER:', str(exc).replace(BOT_TOKEN, '[REDACTED]'), file=sys.stderr)
        sys.exit(1)
