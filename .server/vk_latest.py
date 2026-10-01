#!/usr/bin/env python3
"""Ближайшая экскурсия и свежий пост из группы ВКонтакте -> data/vk.json для сайта turkovrov.ru.

Запускается по cron на сервере (раз в 10 минут). Берёт последние посты сообщества через
VK API wall.get с сервисным ключом, ищет анонс экскурсии с будущей датой и кладёт рядом с
сайтом data/vk.json (+ фото поста). Страница сама решает, что показать: билет ближайшей
экскурсии или карточку свежего поста.

Ключ читается из /etc/turkovrov/vk_token (или переменной VK_TOKEN) и наружу не попадает.
Папка .server/ закрыта в nginx правилом для «точечных» путей.

Проверка без сети:  python vk_latest.py --fixture wall.json --out ./data --now 2026-10-01T12:00:00+03:00
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

GROUP = 'turkovrov'
GROUP_URL = 'https://vk.ru/turkovrov'
API_HOSTS = ('api.vk.com', 'api.vk.ru')
API_VERSION = '5.199'
POSTS_TO_SCAN = 15
MSK = timezone(timedelta(hours=3))  # в Москве нет перехода на летнее время с 2014 года
TOKEN_FILE = '/etc/turkovrov/vk_token'

MONTHS = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6, 'июля': 7,
          'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12}
WEEKDAYS = {'понедельник': 0, 'вторник': 1, 'среду': 2, 'четверг': 3, 'пятницу': 4, 'субботу': 5, 'воскресенье': 6}

# названия маршрутов: (что искать в тексте, как показывать на сайте)
ROUTES = [('сердце старого коврова', 'Сердце старого Коврова'), ('ковров базарный', 'Ковров базарный'),
          ('тайны старого парка', 'Тайны старого парка'), ('треумов', 'Треумовы: фабричная империя'),
          ('былинн', 'Былинный сказ о дубе том…'), ('рельсы времени', 'Рельсы времени'),
          ('белокаменн', 'Белокаменные церкви Коврова')]

# подписи строк в анонсах Виктории: «Когда: …», «Старт: …», «Запись: …»
LABELS = {'когда': 'when', 'дата': 'when', 'время': 'time', 'сбор': 'time',
          'старт маршрута': 'place', 'точка старта': 'place', 'старт': 'place', 'место встречи': 'place',
          'где встречаемся': 'place', 'запись': 'signup', 'стоимость': 'price', 'цена': 'price'}
LABEL_NAMES = sorted(LABELS, key=len, reverse=True)  # длинные раньше: «старт маршрута» прежде «старт»
LABEL_RE = re.compile(r'^(' + '|'.join(LABEL_NAMES) + r')\s*:\s*(.+)$', re.I)
LABEL_SPLIT_RE = re.compile(r'\s+(?=(?:' + '|'.join(n.capitalize() for n in LABEL_NAMES) + r')\s*:)')

DATE_RE = re.compile(r'(?<!\d)(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(\d{4}))?', re.I)
TIME_RE = re.compile(r'(?<!\d)([01]?\d|2[0-3])[:.\-]([0-5]\d)(?!\d)')
REL_DAY_RE = re.compile(r'(?:^|\s)(?:в|на)\s+(?:эту|это|этот|ближайшую|ближайшее|ближайший)\s+(' + '|'.join(WEEKDAYS) + r')', re.I)
CLOSED_RE = re.compile(r'бронь закрыта|мест нет|места закончились|запись закрыта|набор закрыт', re.I)

EMOJI_RE = re.compile('[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿'
                      '■-◿︎️‍⃣]')
MENTION_RE = re.compile(r'\[(?:id|club|public|event)\d+\|([^\]]+)\]')


def clean_lines(text):
    """Текст поста -> список чистых строк: без эмодзи, «**», упоминаний-ссылок, хэштегов и разделителей."""
    text = MENTION_RE.sub(r'\1', text or '')
    text = EMOJI_RE.sub('', text).replace('*', '')
    lines = []
    for raw in text.split('\n'):
        # несколько подписей в одной строке («Когда: … Старт: …») разносим по строкам
        for part in LABEL_SPLIT_RE.split(raw):
            line = re.sub(r'\s+', ' ', part).strip(' \t-—–•·')
            if not line or not re.search(r'[0-9A-Za-zА-Яа-яЁё]', line):
                continue  # пустые строки и разделители вроде «-—»
            if all(w.startswith('#') for w in line.split()):
                continue  # строка из одних хэштегов
            lines.append(line)
    return lines


def labeled_fields(lines):
    fields = {}
    for line in lines:
        m = LABEL_RE.match(line)
        if m:
            key = LABELS[m.group(1).lower()]
            fields.setdefault(key, m.group(2).strip())
    return fields


def find_time(text):
    m = TIME_RE.search(text or '')
    return time(int(m.group(1)), int(m.group(2))) if m else None


def explicit_date(text, published):
    """«3 октября» -> date; год берём от даты поста."""
    m = DATE_RE.search(text or '')
    if not m:
        return None
    day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
    year = int(m.group(3)) if m.group(3) else published.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    # «5 января» в декабрьском посте — это следующий год; а «26 сентября» в посте от 28 сентября —
    # прошедшая дата этого года (отчёт о прогулке), её не переносим
    if not m.group(3) and d < published.date() - timedelta(days=180):
        d = date(year + 1, month, day)
    return d


def relative_date(text, published):
    """«в эту субботу» -> ближайшая суббота начиная с дня публикации."""
    m = REL_DAY_RE.search(text or '')
    if not m:
        return None
    wd = WEEKDAYS[m.group(1).lower()]
    return published.date() + timedelta(days=(wd - published.weekday()) % 7)


def route_name(lines):
    head = lines[0].lower()  # только заголовок: в тексте могут перечисляться все маршруты (голосование)
    hits = [(head.find(key), name) for key, name in ROUTES if key in head]
    return min(hits)[1] if hits else None


def event_of(post, lines, fields):
    """Анонс экскурсии в посте -> (начало, есть ли время) или None."""
    published = datetime.fromtimestamp(post['date'], MSK)
    when = fields.get('when', '')
    d = explicit_date(when, published) or relative_date(when, published)
    t = find_time(when) or find_time(fields.get('time', ''))
    if not d:
        for line in lines[:12]:  # дата без подписи «Когда:» — ищем в начале поста
            d = explicit_date(line, published)
            if d:
                t = t or find_time(line)
                break
    if not d:
        # «в эту субботу» — только из заголовка: в отчётах «в эту субботу мы провели…» это уже прошлое
        d = relative_date(lines[0], published)
    if not d:
        return None
    if not t:
        t = find_time(' '.join(lines[:8]))
    return datetime.combine(d, t or time(0, 0), MSK), t is not None


def best_photo(post):
    for att in post.get('attachments') or []:
        if att.get('type') == 'photo':
            sizes = att['photo'].get('sizes') or []
            fitting = [s for s in sizes if s.get('width', 0) <= 1080] or sizes
            if fitting:
                return max(fitting, key=lambda s: s.get('width', 0)).get('url')
    return None


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(' ', 1)[0].rstrip(' ,;:—-')
    return cut + '…'


def build(items, now):
    tours, latest = [], None
    for post in items:
        if post.get('copy_history'):
            continue  # репосты чужих записей пропускаем
        lines = clean_lines(post.get('text', ''))
        if not lines:
            continue
        fields = labeled_fields(lines)
        url = f"https://vk.ru/wall{post['owner_id']}_{post['id']}"
        published = datetime.fromtimestamp(post['date'], MSK)
        if latest is None or published > latest['_published']:
            body = ' '.join(l for l in lines[1:])
            latest = {'post_id': post['id'], 'url': url, 'title': shorten(lines[0], 90),
                      'excerpt': shorten(body, 240), 'published': published.isoformat(),
                      '_published': published, '_photo_url': best_photo(post)}
        ev = event_of(post, lines, fields)
        if not ev or CLOSED_RE.search(' '.join(lines[:3])):
            continue
        start, has_time = ev
        # без времени не знаем, прошла ли экскурсия сегодня, — такие показываем только до её дня
        upcoming = start > now if has_time else start.date() > now.date()
        if not upcoming:
            continue
        place = fields.get('place')
        if place:
            place = re.sub(r'\s*\(([^()]*)\)\s*$', r', \1', place)  # «Музей (ул. Фёдорова, 6)» -> «Музей, ул. Фёдорова, 6»
        tours.append({'post_id': post['id'], 'url': url,
                      'title': route_name(lines) or shorten(lines[0], 90),
                      'start': start.isoformat(), 'has_time': has_time,
                      'when': fields.get('when'), 'place': place,
                      'signup': shorten(fields['signup'], 140) if fields.get('signup') else None,
                      'price': fields.get('price'),
                      '_start': start, '_published': published, '_photo_url': best_photo(post)})
    # ближайшая дата; если на одну дату несколько постов (например, голосование «какой маршрут
    # выберем на эту субботу?» и потом сам анонс) — побеждает пост с временем и местом, затем самый свежий
    tours.sort(key=lambda t: (t['_start'].date(), -(t['has_time'] + bool(t['place'])), -t['_published'].timestamp()))
    return (tours[0] if tours else None), latest


def fetch_wall(token):
    query = urllib.parse.urlencode({'domain': GROUP, 'count': POSTS_TO_SCAN, 'filter': 'owner',
                                    'v': API_VERSION, 'access_token': token})
    last_error = None
    for host in API_HOSTS:
        try:
            with urllib.request.urlopen(f'https://{host}/method/wall.get?{query}', timeout=20) as r:
                data = json.load(r)
        except Exception as e:  # сеть недоступна — пробуем второй адрес API
            last_error = e
            continue
        if 'error' in data:
            raise RuntimeError(f"VK API: {data['error'].get('error_code')} {data['error'].get('error_msg')}")
        return data['response']['items']
    raise RuntimeError(f'VK API недоступен: {last_error}')


def save_photo(entry, out_dir):
    """Фото поста кладём рядом с сайтом: без запросов к ВКонтакте из браузера посетителя."""
    url = entry.get('_photo_url')
    for key in [k for k in entry if k.startswith('_')]:
        del entry[key]  # служебные поля в json не пишем
    entry['photo'] = None
    if not url:
        return
    name = f"vk-{entry['post_id']}.jpg"
    path = out_dir / name
    if not path.exists():
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = r.read()
            tmp = path.with_suffix('.tmp')
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except Exception as e:
            print(f'фото {name} не скачалось: {e}', file=sys.stderr)
            return
    entry['photo'] = f'data/{name}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(Path(__file__).resolve().parent.parent / 'data'))
    ap.add_argument('--fixture', help='файл с ответом wall.get вместо запроса к API (для проверки)')
    ap.add_argument('--now', help='текущее время в ISO (для проверки)')
    ap.add_argument('--no-photos', action='store_true')
    args = ap.parse_args()

    now = datetime.fromisoformat(args.now) if args.now else datetime.now(MSK)
    if args.fixture:
        raw = json.loads(Path(args.fixture).read_text(encoding='utf-8'))
        items = raw['response']['items'] if 'response' in raw else raw['items']
    else:
        token = os.environ.get('VK_TOKEN') or (Path(TOKEN_FILE).read_text(encoding='utf-8').strip()
                                               if Path(TOKEN_FILE).exists() else '')
        if not token:
            print(f'нет ключа ВКонтакте: положите сервисный ключ в {TOKEN_FILE}', file=sys.stderr)
            return 1
        items = fetch_wall(token)

    tour, latest = build(items, now)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for entry in (tour, latest):
        if entry is None:
            continue
        if args.no_photos:
            entry['_photo_url'] = None
        save_photo(entry, out_dir)

    result = {'updated': now.isoformat(), 'group': GROUP_URL, 'tour': tour, 'latest': latest}
    tmp = out_dir / 'vk.json.tmp'
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, out_dir / 'vk.json')  # атомарно: страница никогда не увидит недописанный файл

    keep = {Path(e['photo']).name for e in (tour, latest) if e and e.get('photo')}
    for old in out_dir.glob('vk-*.jpg'):
        if old.name not in keep:
            old.unlink()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # старый vk.json при ошибке остаётся как был
        print(f'{datetime.now(MSK):%Y-%m-%d %H:%M} ошибка: {e}', file=sys.stderr)
        sys.exit(1)
