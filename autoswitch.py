#!/usr/bin/env python3
# autoswitch v0.5 — автопереключатель раскладки для Linux (us/ru)
#
# Гибридный детектор: СЛОВАРЬ (точность) + БИГРАММЫ (страховка) + set (скорость).
# Полное владение раскладкой: перехватывает и ручной Ctrl+Shift, поэтому знание
# о текущей раскладке не расходится с реальностью.
#
# Поддержка окружений:
#   X11 (любое DE)   — libX11 напрямую, окно через xlib
#   GNOME Wayland    — расширение autoswitch@shah-man.github.io (D-Bus)
#   sway / Hyprland  — штатный IPC композитора (swaymsg / hyprctl)
#   KDE Wayland      — раскладка через D-Bus org.kde.keyboard;
#                      исключения приложений недоступны (нужен KWin-скрипт)
#
# Логика решения по слову:
#   1) есть в ru-словаре и нет в en -> ru;  есть в en и нет в ru -> us
#   2) оба/ни один -> сравнение по частотным биграммам
#   3) на границе слова: если язык != текущая раскладка -> стереть, переключить,
#      напечатать заново (регистр сохраняем), затем разделитель один раз
#
# Запуск: sudo python3 autoswitch.py [--start-ru]    Выход: Ctrl+C
# Требует словарей: sudo apt install hunspell-ru hunspell-en-us

import os
import sys
import time
from evdev import InputDevice, UInput, categorize, ecodes
from evdev import ecodes as e

import dictionaries

# --- определение активного окна для исключения терминалов/игр ---
# X11: через xlib. Wayland: через D-Bus мост расширения
#   autoswitch@shah-man.github.io (метод GetFocusedClass).
# Тип сессии и вся работа с раскладкой — в модуле layout.
import re
import glob
import json
import subprocess

# Всё, что касается раскладки и активного окна, вынесено в layout.py:
# там живёт совместимость с X11/GNOME/KDE/sway/Hyprland, а здесь —
# только логика определения языка.
import layout
from layout import active_window_class, read_real_layout, switch_layout

# вывод сразу в journal (без буферизации), чтобы логи были видны в реальном времени
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

KBD = None  # определяется автоматически (см. find_keyboard); можно задать --device


def find_keyboard():
    """Найти основную клавиатуру среди /dev/input/event*.

    Настоящая клавиатура имеет полный буквенный ряд, цифры, модификаторы.
    Отсекаем мыши, тачпады, кнопки питания и «спутники» клавиатур
    (Consumer/System Control), которые тоже сообщают часть клавиш.
    Возвращает путь или None. Автоопределение — чтобы конфиг мог быть пустым
    и приложение работало на любой машине без ручной привязки.
    """
    from evdev import list_devices, InputDevice
    # Узнаём «основные» узлы по by-id: ссылка *-event-kbd БЕЗ 'if' в имени —
    # главный интерфейс клавиатуры. Вторичные (*-if01-event-kbd) — макро/
    # мультимедиа узлы, их берём в последнюю очередь.
    primary_nodes = set()
    try:
        byid = '/dev/input/by-id'
        for link in os.listdir(byid):
            if link.endswith('-event-kbd') and '-if' not in link:
                primary_nodes.add(os.path.realpath(os.path.join(byid, link)))
    except Exception:
        pass
    # обязательный минимум: буквы по углам раскладки + пробел + Enter
    need = {ecodes.KEY_A, ecodes.KEY_Z, ecodes.KEY_M, ecodes.KEY_Q,
            ecodes.KEY_P, ecodes.KEY_L, ecodes.KEY_SPACE, ecodes.KEY_ENTER}
    # признаки настоящей клавиатуры (за каждый — плюс к рейтингу)
    bonus = {ecodes.KEY_LEFTSHIFT, ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT,
             ecodes.KEY_TAB, ecodes.KEY_BACKSPACE, ecodes.KEY_CAPSLOCK,
             ecodes.KEY_1, ecodes.KEY_0, ecodes.KEY_DOT, ecodes.KEY_COMMA}
    candidates = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except Exception:
            continue
        keyset = set(dev.capabilities().get(ecodes.EV_KEY, []))
        if not need.issubset(keyset):
            continue
        score = 0
        # главный сигнал — полнота буквенного ряда: у основной клавиатуры есть
        # все буквы A-Z, у вторичных интерфейсов (if01, макро-узлы) — не все.
        alphabet = [getattr(ecodes, f'KEY_{c}') for c in
                    'QWERTYUIOPASDFGHJKLZXCVBNM']
        score += sum(2 for k in alphabet if k in keyset)
        # цифровой ряд — тоже признак полноценной клавиатуры
        digits = [getattr(ecodes, f'KEY_{d}') for d in '1234567890']
        score += sum(1 for k in digits if k in keyset)
        # модификаторы и редактирование
        score += sum(3 for k in bonus if k in keyset)
        # штраф устройствам, которые ещё и мышь
        if ecodes.BTN_LEFT in keyset or ecodes.BTN_MOUSE in keyset:
            score -= 1000
        # штраф «спутникам»: Consumer/System Control сообщают буквы, но это
        # не основная клавиатура (по имени)
        name = (dev.name or '').lower()
        if any(w in name for w in ('consumer', 'system control', 'control')):
            score -= 500
        # штраф игровым мышам: у них есть под-устройство "Mouse Keyboard"
        # для макросов, которое сообщает буквы, но это не клавиатура
        if 'mouse' in name:
            score -= 800
        # лёгкий бонус тому, у кого имя явно про клавиатуру (но не мышь)
        if ('keyboard' in name or 'kbd' in name) and 'mouse' not in name:
            score += 5
        # сильный бонус основному узлу по by-id (*-event-kbd без if):
        # это решает случай мультиинтерфейсных клавиатур (SEMICO даёт
        # event-kbd и if01-event-kbd — берём первый)
        if path in primary_nodes:
            score += 100
        candidates.append((score, path, dev.name))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]

LAYOUT = {
    'KEY_Q': ('q', 'й'), 'KEY_W': ('w', 'ц'), 'KEY_E': ('e', 'у'),
    'KEY_R': ('r', 'к'), 'KEY_T': ('t', 'е'), 'KEY_Y': ('y', 'н'),
    'KEY_U': ('u', 'г'), 'KEY_I': ('i', 'ш'), 'KEY_O': ('o', 'щ'),
    'KEY_P': ('p', 'з'), 'KEY_LEFTBRACE': ('[', 'х'), 'KEY_RIGHTBRACE': (']', 'ъ'),
    'KEY_A': ('a', 'ф'), 'KEY_S': ('s', 'ы'), 'KEY_D': ('d', 'в'),
    'KEY_F': ('f', 'а'), 'KEY_G': ('g', 'п'), 'KEY_H': ('h', 'р'),
    'KEY_J': ('j', 'о'), 'KEY_K': ('k', 'л'), 'KEY_L': ('l', 'д'),
    'KEY_SEMICOLON': (';', 'ж'), 'KEY_APOSTROPHE': ("'", 'э'),
    'KEY_Z': ('z', 'я'), 'KEY_X': ('x', 'ч'), 'KEY_C': ('c', 'с'),
    'KEY_V': ('v', 'м'), 'KEY_B': ('b', 'и'), 'KEY_N': ('n', 'т'),
    'KEY_M': ('m', 'ь'), 'KEY_COMMA': (',', 'б'), 'KEY_DOT': ('.', 'ю'),
    'KEY_SLASH': ('/', '.'), 'KEY_GRAVE': ('`', 'ё'),
}

# Клавиши, по которым запускается проверка и исправление слова.
# Только пробел/Enter/Tab: знаки препинания слово не «фиксируют», иначе
# исправление срабатывало бы сразу после "!" вместо пробела.
WORD_BREAK = {'KEY_SPACE', 'KEY_ENTER', 'KEY_KPENTER', 'KEY_TAB'}

# Клавиши, которые прерывают набор слова, но НЕ запускают исправление:
# цифровой ряд и знаки, которых нет в LAYOUT. Слово перед ними остаётся
# в буфере и будет проверено, когда пользователь нажмёт пробел.
PUNCT_KEYS = {
    'KEY_1', 'KEY_2', 'KEY_3', 'KEY_4', 'KEY_5',
    'KEY_6', 'KEY_7', 'KEY_8', 'KEY_9', 'KEY_0',
    'KEY_MINUS', 'KEY_EQUAL', 'KEY_BACKSLASH',
}

EN_LETTERS = set('abcdefghijklmnopqrstuvwxyz')
RU_LETTERS = set('абвгдежзийклмнопрстуфхцчшщъыьэюяё')
EN_BIGRAMS = {'th','he','in','er','an','re','on','at','en','nd','ti','es','or',
'te','of','ed','is','it','al','ar','st','to','nt','ng','se','ha','as','ou','io',
'le','ve','co','me','de','hi','ri','ro','ic','ne','ea','ra','ce','li','ch','ll',
'be','ma','si','om','ur','ca','el','ta','la','ns','di','fo','ho','pe','ec','pr',
'ad','wh','ge','ie','wi','em','ss','ci','et','ac','ay','sh','ei','od','nc','fi',
'll','ke','ut','ai','ol','ll','so','ap','us','mo','pl','ap','gr','fr','tr','bl',
'cl','sp','sw','sc','sk','sl','sm','sn','br','cr','dr','pr','wr','tw','qu'}
RU_BIGRAMS = {'ст','но','то','на','ен','ов','ни','ра','во','ко','ер','ал','ет',
'ор','ол','ро','ли','не','пр','ва','ан','ре','ос','ла','ел','по','од','ес','ем',
'го','ог','ом','де','те','ин','ль','ри','ка','ти','ло','ит','ск','ей','ту','ат',
'ме','ся','св','мо','пе','ви','да','па','ап','пк','ку','уб','бк','аб','об','из',
'ыл','ыв','вы','вс','сь','чт','кт','кл','кр','гр','тр','др','бр','пл','сл','сп',
'ст','сн','см','зн','зд','жд','нн','мм','лл','тт','фф','кк','пп','сс','ды','ты',
'мы','бы','ры','лы','ги','ки','хи','чи','ши','щи','жи','це','че','ще','ша','жа',
'ху','фу','цу','шу','щу','юу','яб','яв','яд','яз','юг','юд','юр','ём','ёл','ёт',
'аг','ад','аж','аз','ай','ак','ам','ах','ац','аш','ощ','оч','оф','ох','оц',
'уг','уд','уж','уз','ук','ум','ун','ух','уч','уш','эт','эл','эк','эн','эр'}
RU_TRIGRAMS = {'ово','ени','ста','ост','ани','его','тор','про','при','что','как',
'пап','апк','пку','тьс','ать','ять','ний','ной','ные','ых ','ого','ому','ыми',
'ани','ени','ени','иче','ест','ост','льн','ель','тел','ает','ают','ить','ила'}

SWITCH_KEYS = [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT]

# Короткие слова, которые статистика распознаёт плохо (1-3 буквы).
# Нужны, чтобы приоритет раскладки не перебивал очевидные случаи:
# 'is'/'шы', 'a'/'ф' и т.п. Русские двойники английских здесь не слова,
# поэтому приоритет RU от этого не страдает.
# ── раннее переключение: невозможные сочетания клавиш ─────────────────
# Как в Caramba/Punto: не ждём конца слова, а решаем уже на 2-3 букве.
# Принцип — не «редко», а «не бывает вообще»: пары клавиш, чей результат
# невозможен в текущей раскладке, значит пользователь печатает на другом
# языке. Таблицы построены по РЕАЛЬНЫМ словарям (146k рус., 76k англ.) и
# проверены на ложные срабатывания — ни одно из сочетаний не встречается
# в словах «своего» языка.
#
# IMPOSSIBLE_IN_RU: набрано в RU-раскладке -> на экране сочетание, какого
# в русском не бывает => это английский текст, переключаем на US.
IMPOSSIBLE_IN_RU = {
    'aq', 'aw', 'bm', 'bs', 'cq', 'em', 'es', 'fs', 'gu', 'ii', 'il', 'io',
    'ip', 'iq', 'is', 'iu', 'iz', 'me', 'mf', 'mh', 'mm', 'ms', 'nq', 'oa',
    'oc', 'og', 'oi', 'ok', 'ol', 'on', 'oo', 'op', 'oq', 'or', 'os', 'ou',
    'ow', 'ox', 'oz', 'po', 'ro', 'rq', 'rz', 'sa', 'sf', 'sm', 'ss', 'tm',
    'ts', 'um', 'uo', 'uz', 'wa', 'wm', 'wo', 'xa', 'xo', 'xp', 'xu',
}
# IMPOSSIBLE_IN_EN: набрано в US-раскладке -> сочетание, невозможное в
# английском => это русский текст, переключаем на RU.
IMPOSSIBLE_IN_EN = {
    'fq', 'fv', 'fx', 'fz', 'hx', 'jb', 'jl', 'jq', 'jx', 'jz', 'kx', 'kz',
    'mx', 'qg', 'qh', 'qj', 'qv', 'qx', 'qy', 'sx', 'vm', 'vw', 'vx', 'xj',
    'xk', 'zx',
}

SHORT_EN = {'a', 'i', 'is', 'it', 'in', 'at', 'on', 'of', 'to', 'an', 'if',
            'no', 'so', 'me', 'we', 'he', 'be', 'do', 'up', 'my', 'by', 'as',
            'or', 'us', 'am', 'go', 'ok', 'hi', 'the', 'and', 'you', 'for',
            'are', 'but', 'not', 'all', 'can', 'her', 'was', 'one', 'our',
            'out', 'day', 'get', 'has', 'him', 'his', 'how', 'its', 'new',
            'now', 'old', 'see', 'two', 'way', 'who', 'boy', 'did', 'yes'}
SHORT_RU = {'а', 'и', 'в', 'к', 'с', 'я', 'о', 'у', 'не', 'на', 'он', 'по',
            'от', 'до', 'за', 'из', 'но', 'мы', 'вы', 'да', 'ты', 'же', 'ли',
            'бы', 'то', 'их', 'ее', 'ей', 'ну', 'уж', 'об', 'во', 'со', 'что',
            'как', 'все', 'она', 'они', 'был', 'для', 'так', 'вот', 'еще',
            'уже', 'или', 'без', 'под', 'над', 'при', 'про', 'сам', 'там',
            'тут', 'где', 'кто', 'чем', 'вас', 'нас', 'нет', 'мне', 'тебе'}
SHIFT_KEYS = {'KEY_LEFTSHIFT', 'KEY_RIGHTSHIFT'}
CTRL_KEYS = {'KEY_LEFTCTRL', 'KEY_RIGHTCTRL'}
OTHER_MODS = {'KEY_LEFTALT','KEY_RIGHTALT','KEY_LEFTMETA','KEY_RIGHTMETA','KEY_CAPSLOCK'}

# словари загрузим в main()
FIX_TWO_CAPS = True      # починка «ПРивет»; выключается в конфиге
RU_WORDS = set()
EN_WORDS = set()
TYPE_DELAY = 0.008  # устар.: раньше пауза между нажатиями; rewrite теперь
# шлёт события залпом (анти-гонка), задержка не используется. Оставлена как
# константа на случай возврата пошаговой печати; из конфига больше не грузится.
SHORT_WORDS = True  # учитывать список коротких слов (is/a/не/на...)
USE_CONTEXT = True  # в спорных случаях смотреть на язык предыдущего слова
USE_BIGRAMS = True  # догадываться по частотным сочетаниям букв, из конфига
MIN_WORD_LEN = 1    # слова короче не трогаем; 1 = не отсекать ничего
EARLY_SWITCH = True # раннее переключение по невозможным сочетаниям (Caramba-
# эффект: правит уже на 2-3 букве, не дожидаясь пробела). Из конфига
# early_switch. Работает только на «железных» сочетаниях, ложных нет.
SHIFT_SWITCHES_LAYOUT = True  # одиночный Shift переключает раскладку
# (как в Caramba). Заглавные буквы не задевает: Shift+буква работает как
# обычно, потому что переключение срабатывает только когда Shift нажали и
# отпустили, не тронув других клавиш. Из конфига shift_switches_layout.
VERSION = '1.0'       # версия программы (видна в журнале и в «О программе»)
PASSWORD_MIN_LEN = 6  # от скольких знаков набор может считаться паролем
# (как в Caramba). Пароли не конвертируются и НИКОГДА не логируются.
DIGIT_KEYS = {'KEY_1','KEY_2','KEY_3','KEY_4','KEY_5','KEY_6','KEY_7',
              'KEY_8','KEY_9','KEY_0','KEY_MINUS','KEY_EQUAL'}
PREFIX_MIN = 3      # с какой буквы начинаем решать о языке слова
PREFIX_LEN = 4      # и до какой пробуем. Проверяем на каждой длине 3..4:
# на 3-й ловятся явные случаи (быстро, как в Caramba), на 4-й — те, чьё
# начало совпало с настоящим словом («hello» -> «руд» как в «рудольф»).
# Вместе дают покрытие ~96-98% без ожидания пробела.
RU_PREFIX = {}      # {длина: множество начал русских слов} (строится в main)
EN_PREFIX = {}      # то же для английских
TECH_PREFIX = set()  # начала ТЕХТЕРМИНОВ (kvm, nvme, jq, vmware) длиной
# 2..PREFIX_LEN. Нужны, чтобы невозможные пары (vm, jq, sx) не дёргали
# переключение на рабочих словах — в обычном английском таких сочетаний нет
DEBUG = False       # подробный лог с НАБРАННЫМИ СЛОВАМИ (из конфига debug_log).
# По умолчанию ВЫКЛЮЧЕН: иначе каждое напечатанное слово (в т.ч. пароли и
# seed-фразы) осело бы в journald открытым текстом — недопустимо для
# инструмента, читающего всю клавиатуру. Включается вручную для отладки.


# ── собственный файл журнала ──────────────────────────────────────────
# Пишем НЕ в системный журнал, а в свой файл. Причина — приватность: при
# включённой отладке в лог попадают набранные слова, и они не должны
# оседать в общесистемном журнале (его читают админы, он уходит в системы
# сбора логов и в бэкапы). Свой файл мы полностью контролируем: его видно
# в asc → Журнал и можно стереть одной кнопкой, не трогая чужие записи —
# journald удалять выборочно по службе не умеет в принципе.
# Журнал лежит рядом с исходниками, в домашней папке пользователя: так его
# видно и можно открыть, скопировать или удалить без sudo. Точный путь
# вычисляется при старте (см. _resolve_log_path).
LOG_FILE = ''


LOG_LANG = 'en'   # язык сообщений журнала, из конфига ui_lang


def lg(key, **kw):
    """Сообщение журнала на языке интерфейса (ui_lang).

    Человек выбрал английский — значит и журнал должен быть английским:
    иначе он открывает файл и видит вперемешку два языка. Ключи короткие,
    подстановки именованные.
    """
    ru = {
        'revert_keep':   "  [ручная отмена: {w} остаётся {lang}]",
        'revert_nolearn': "  [ручная отмена: {w} откачено, правило не создано (словарное слово)]",
        'skip_learn':    "  [пропуск обучения: {w} — словарное слово, правило не нужно]",
        'learned':       "  [учту ({why}): {w} остаётся {lang}]",
        'layout_unknown': "  [раскладка неизвестна — не трогаем слово]",
        'two_caps':      "  две заглавные: {w}",
        'no_window_class': "  [активное окно не определяется — слова не "
                           "обрабатываются]",
        'manual':        "  [вручную -> {lang}]",
        'shift':         "  [Shift -> {lang}]",
        'early':         "  раннее: {a} -> {b}",
        'fix':           "  {a} -> {b}",
        'ok':            "  ok ({lang}): {w}",
    }
    en = {
        'revert_keep':   "  [undo: {w} stays {lang}]",
        'revert_nolearn': "  [undo: {w} reverted, no rule created (dictionary word)]",
        'skip_learn':    "  [learning skipped: {w} is a dictionary word, rule not needed]",
        'learned':       "  [learned ({why}): {w} stays {lang}]",
        'layout_unknown': "  [layout unknown — leaving the word alone]",
        'two_caps':      "  two capitals: {w}",
        'no_window_class': "  [active window cannot be detected — words are "
                           "not processed]",
        'manual':        "  [manual -> {lang}]",
        'shift':         "  [Shift -> {lang}]",
        'early':         "  early: {a} -> {b}",
        'fix':           "  {a} -> {b}",
        'ok':            "  ok ({lang}): {w}",
    }
    table = ru if LOG_LANG == 'ru' else en
    try:
        return table[key].format(**kw)
    except Exception:
        return key


def env_summary():
    """Одна строка об окружении — для журнала при старте.

    Когда человек присылает лог с проблемой, из неё сразу видно, на чём он
    работает: дистрибутив, оболочка, версия Python. Иначе пришлось бы
    переспрашивать. Имя машины сюда НЕ попадает: в нём часто личные данные
    (имя владельца, название отдела), а для разбора оно бесполезно.
    """
    distro = ''
    try:
        with open('/etc/os-release', encoding='utf-8') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    distro = line.split('=', 1)[1].strip().strip('"')
                    break
    except Exception:
        pass
    de = (os.environ.get('XDG_CURRENT_DESKTOP')
          or os.environ.get('DESKTOP_SESSION') or '')
    parts = [f"v{VERSION}"] if 'VERSION' in globals() else []
    if distro:
        parts.append(distro)
    if de:
        parts.append(de)
    parts.append('Python %d.%d.%d' % sys.version_info[:3])
    return '[autoswitch] ' + ' | '.join(parts)


def _resolve_log_path():
    """Путь к файлу журнала: ~/Applications/autoswitch.logs/autoswitch.log
    того пользователя, для которого работает служба. Если домашний каталог
    определить не удалось, файл не ведётся — вывод идёт в stdout."""
    global LOG_FILE
    try:
        uid = layout._WL_UID if hasattr(layout, '_WL_UID') else ''
        if uid and str(uid).isdigit() and int(uid) >= 1000:
            import pwd
            home = pwd.getpwuid(int(uid)).pw_dir
            if home and os.path.isdir(home):
                LOG_FILE = os.path.join(home, 'Applications',
                                        'autoswitch.logs', 'autoswitch.log')
    except Exception:
        pass
    return LOG_FILE
LOG_MAX_BYTES = 5 * 1024 * 1024   # из конфига log_max_size (МБ), 0 = без лимита
_log_fh = None


def _log_open():
    """Открыть файл журнала на дозапись. При неудаче остаёмся на stdout."""
    global _log_fh
    if _log_fh is not None:
        return _log_fh
    try:
        d = os.path.dirname(LOG_FILE)
        os.makedirs(d, exist_ok=True)
        # Пустая строка перед новым запуском: иначе перезапуски сливаются
        # в сплошную стену и не видно, где кончился один и начался другой.
        # Только если в файле уже что-то есть — в начале пустого журнала
        # отступ не нужен.
        _need_gap = False
        try:
            _need_gap = os.path.getsize(LOG_FILE) > 0
        except OSError:
            _need_gap = False
        _log_fh = open(LOG_FILE, 'a', encoding='utf-8')
        if _need_gap:
            try:
                _log_fh.write('\n')
                _log_fh.flush()
            except Exception:
                pass
        # Служба работает от root, но файл должен принадлежать пользователю:
        # иначе он не смог бы открыть, скопировать или удалить свой же
        # журнал без sudo. Права 640 — посторонние на машине не читают.
        try:
            uid = layout._WL_UID if hasattr(layout, '_WL_UID') else ''
            if os.geteuid() == 0 and str(uid).isdigit() and int(uid) >= 1000:
                import pwd
                gid = pwd.getpwuid(int(uid)).pw_gid
                os.chown(d, int(uid), gid)
                os.chown(LOG_FILE, int(uid), gid)
        except Exception:
            pass
        try:
            os.chmod(LOG_FILE, 0o640)
        except OSError:
            pass
    except Exception:
        _log_fh = None
    return _log_fh


def _log_rotate_if_needed():
    """Не дать файлу разрастись: при превышении лимита оставляем свежую
    половину, старое отбрасываем. Своя ротация вместо logrotate — чтобы
    работало на любой системе без внешних настроек."""
    global _log_fh
    if LOG_MAX_BYTES <= 0 or _log_fh is None:
        return
    try:
        if _log_fh.tell() < LOG_MAX_BYTES:
            return
        _log_fh.close()
        _log_fh = None
        with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            data = f.read()
        keep = data[len(data) // 2:]
        # начинаем с целой строки, а не с середины
        nl = keep.find('\n')
        if nl >= 0:
            keep = keep[nl + 1:]
        with open(LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('--- журнал обрезан по размеру / log truncated by size ---\n')
            f.write(keep)
        _log_open()
    except Exception:
        _log_open()


def log(msg):
    """Записать строку в журнал autoswitch (с отметкой времени)."""
    fh = _log_open()
    line = time.strftime('%b %d %H:%M:%S ') + str(msg)
    if fh is None:
        print(line, flush=True)      # запасной путь: в stdout, как раньше
        return
    try:
        fh.write(line + '\n')
        fh.flush()
        _log_rotate_if_needed()
    except Exception:
        print(line, flush=True)


def dbg(msg):
    """Печать только в режиме отладки (debug_log=on). Служебные сообщения
    (старт, ошибки) печатаются напрямую через log и идут всегда."""
    if DEBUG:
        log(msg)

# ── обучение на отменах ───────────────────────────────────────────────
# Каждая строка learned.txt: <US-форма><TAB>ru|us  (нормализованная запись
# слова в US-раскладке в нижнем регистре + язык, в котором его нужно
# оставлять). Пример:  ghbdtn<TAB>ru  = «привет» набирается в RU и не
# трогается. Файл живёт в /etc/autoswitch, service пишет напрямую (root),
# GUI/asc — через asc-helper. Заменяет собой ручной «уклон раскладки»:
# программа сама запоминает личные исключения пользователя.
LEARNED_FILE = '/etc/autoswitch/learned.txt'
LEARNED = {}        # us_form -> 'ru'|'us'
_learned_mtime = 0  # для горячей перечитки при внешней правке
# приложения, где autoswitch НЕ вмешивается (по WM_CLASS, нижний регистр)
# точное совпадение класса
EXCLUDE_APPS = {
    # Единственное, что исключено ЖЁСТКО, — окно самой программы: в нём
    # набирают слова для «Моего словаря» и классы приложений, и подменять
    # этот ввод нельзя. Два имени — одно и то же окно: композиторы отдают
    # класс либо по StartupWMClass, либо по имени бинарника.
    # Всё остальное (включая терминалы) решает сам пользователь через
    # exclude_apps: захочет автопереключение в консоли — его право.
    'autoswitch', 'asc-gui',
}
# префиксы: класс исключается, если начинается на один из них.
# Нужно для Steam — клиент и любые игры (нативные/Proton/Flatpak) отдают
# steam*, steam_app_*, com.valvesoftware.steam* — одна галочка ловит всё.
EXCLUDE_PREFIXES = set()


def _cls_variants(name):
    """Варианты записи одного и того же класса окна.

    На X11 приложение зовётся коротко — konsole, dolphin. На Wayland то же
    приложение отдаёт обратный доменный идентификатор из .desktop:
    org.kde.konsole. Список исключений собирается из .desktop-файлов и с
    рук, поэтому в нём может лежать любая из форм. Сверяем обе, иначе
    исключение, добавленное на X11, не срабатывает на Wayland и наоборот.
    """
    if not name:
        return set()
    # регистр не важен: разные композиторы отдают класс по-разному
    # (autoswitch / Autoswitch), а в списке исключений он записан как угодно
    name = name.strip().lower()
    out = {name}
    tail = name.rsplit('.', 1)[-1]
    if tail:
        out.add(tail)
    return out


def is_excluded(wcls):
    """True, если окно с классом wcls нужно пропустить (не вмешиваться)."""
    if not wcls:
        return False
    names = _cls_variants(wcls)
    for item in EXCLUDE_APPS:
        if names & _cls_variants(item):
            return True
    for pref in EXCLUDE_PREFIXES:
        if not pref:
            continue
        if any(n.startswith(pref) for n in names):
            return True
    return False


def _to_us_form(word):
    """Привести слово к US-форме (латинские клавиши).

    В learned.txt ключ должен быть US-формой (`gfgre`), но при ручном
    вводе через GUI пользователь набирает слово в своей раскладке —
    кириллицей (`папку`). Переводим кириллицу обратно в клавиши по
    таблице LAYOUT, чтобы ключ совпадал с тем, что строит build(buffer,0).
    Латиница возвращается как есть.
    """
    if not word:
        return word
    # обратная карта: кириллический символ -> латинский (US) по клавише
    global _RU2US
    try:
        _RU2US
    except NameError:
        _RU2US = {}
        for _us, _ru in LAYOUT.values():
            if _ru:
                _RU2US[_ru.lower()] = _us.lower()
    out = []
    for ch in word.lower():
        out.append(_RU2US.get(ch, ch))
    return ''.join(out)


def _worth_learning(us_form, keep_lang):
    """Стоит ли запоминать правило «оставлять us_form в keep_lang»?

    Отсекает мусор, который налипал при случайном Backspace: если слово —
    это нормальное словарное слово в ДРУГОМ языке, оставлять его в keep_lang
    бессмысленно (напр. `exit`, `programs` — валидные английские, незачем
    их держать русскими). Такие правила рождались из случайных откатов и
    ломали разбор. Ручной ввод через GUI этот фильтр не проходит — там
    намерение пользователя явное.

    Возвращает False, если правило вредное (учить не нужно).
    """
    as_us = us_form.lower()
    # правильные формы получаем прямой перестройкой из US-форм
    ru_form = ''.join(_us_to_ru_char(c) for c in as_us)
    if keep_lang == 'ru':
        # хотим держать РУССКИМ. Плохо, если это нормальное EN-слово,
        # а как русское — не слово (т.е. на деле пользователь писал по-англ.)
        if as_us in EN_WORDS and ru_form not in RU_WORDS:
            return False
    else:  # keep_lang == 'us'
        # хотим держать АНГЛИЙСКИМ. Плохо, если это нормальное RU-слово,
        # а как английское — не слово.
        if ru_form in RU_WORDS and as_us not in EN_WORDS:
            return False
    return True


def _us_to_ru_char(ch):
    """Один символ US-раскладки -> кириллица (для проверки словаря)."""
    global _US2RU_MAP
    try:
        _US2RU_MAP
    except NameError:
        _US2RU_MAP = {}
        for _us, _ru in LAYOUT.values():
            if _us:
                _US2RU_MAP[_us.lower()] = _ru.lower()
    return _US2RU_MAP.get(ch, ch)


def load_learned():
    """Прочитать learned.txt в LEARNED. Тихо переживает отсутствие файла."""
    global LEARNED, _learned_mtime
    d = {}
    try:
        _learned_mtime = os.path.getmtime(LEARNED_FILE)
        with open(LEARNED_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '\t' in line:
                    form, lang = line.split('\t', 1)
                else:
                    parts = line.split()
                    if len(parts) != 2:
                        continue
                    form, lang = parts
                form = _to_us_form(form.strip())
                lang = lang.strip().lower()
                if form and lang in ('ru', 'us'):
                    d[form] = lang
    except FileNotFoundError:
        _learned_mtime = 0
    except Exception:
        pass
    LEARNED = d


def _refresh_learned():
    """Перечитать learned.txt, если его изменили извне (GUI/asc)."""
    try:
        m = os.path.getmtime(LEARNED_FILE)
    except OSError:
        m = 0
    if m != _learned_mtime:
        load_learned()


def save_learned_rule(us_form, lang):
    """Дописать/обновить одно правило в LEARNED и в файл. service — root,
    пишет напрямую; дубликаты не плодим (переписываем весь файл)."""
    us_form = _to_us_form((us_form or '').strip())
    if not us_form or lang not in ('ru', 'us'):
        return
    if LEARNED.get(us_form) == lang:
        return
    LEARNED[us_form] = lang
    global _learned_mtime
    try:
        os.makedirs(os.path.dirname(LEARNED_FILE), exist_ok=True)
        tmp = LEARNED_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('# autoswitch: выученные слова (US-форма<TAB>ru|us).\n')
            f.write('# Пополняется автоматически при отмене исправления.\n')
            for form in sorted(LEARNED):
                f.write(f'{form}\t{LEARNED[form]}\n')
        os.replace(tmp, LEARNED_FILE)
        try:
            os.chmod(LEARNED_FILE, 0o644)
        except OSError:
            pass
        _learned_mtime = os.path.getmtime(LEARNED_FILE)
    except Exception as ex:
        log(f'[autoswitch] learned save failed: {ex}')


def score_bigram(word, letters, bigrams, trigrams=None):
    if not word:
        return 0.0
    w = word.lower()
    valid = sum(1 for ch in w if ch in letters)
    ratio = valid / len(w)
    if ratio < 0.5:
        return ratio * 0.3
    big = sum(1 for i in range(len(w)-1) if w[i:i+2] in bigrams) / max(1, len(w)-1)
    tri = 0.0
    if trigrams:
        tri = sum(1 for i in range(len(w)-2) if w[i:i+3] in trigrams) / max(1, len(w)-2)
    return ratio*0.5 + big*0.4 + tri*0.1


def build(buffer, index):
    return ''.join(LAYOUT[kc][index] if kc in LAYOUT else '?' for kc, _ in buffer)


def looks_like_password(buffer):
    """Похож ли набор на пароль — такие не трогаем и НИКОГДА не логируем.

    Признаки (как в Caramba): длина от 6 знаков И одновременное присутствие
    разных классов символов — букв с цифрами, смешанного регистра или
    спецсимволов. Обычные слова под это не подпадают.

    Пароль не конвертируется ни в какую сторону: человек мог набрать его и
    латиницей, и намеренно кириллицей — любая правка движка его сломает.
    Поэтому движок просто отходит в сторону.
    """
    if len(buffer) < PASSWORD_MIN_LEN:
        return False

    # Обычная капитализация — не признак пароля. «Привет», «Проверка»,
    # «ПРивет» (залипший Shift), «ПРИВЕТ» — во всех заглавные идут только
    # В НАЧАЛЕ слова, так пишут люди, а не так выглядят пароли. Пароль
    # выдаёт заглавная ПОСЛЕ строчных, как в «myPassword».
    #
    # Без этой поправки любое слово от шести знаков с заглавной первой
    # буквой считалось паролем, и на границе слова его не трогали. Обычные
    # слова спасало раннее переключение (оно срабатывает на коротком
    # буфере, до порога), а вот починка двух заглавных упиралась в это
    # намертво: «ПРивет» ровно шесть знаков.
    lead = 0
    for _, shift in buffer:
        if shift:
            lead += 1
        else:
            break
    tail_upper = any(shift for _, shift in buffer[lead:])
    regular_case = not tail_upper and (lead <= 2 or lead == len(buffer))

    has_lower = has_upper = has_digit = has_sym = False
    for kc, shift in buffer:
        pair = LAYOUT.get(kc)
        ch = pair[0] if pair else None
        if ch and ch.isalpha():
            if shift:
                has_upper = True
            else:
                has_lower = True
        elif kc in DIGIT_KEYS:
            has_digit = True
        elif ch and not ch.isalnum():
            has_sym = True
        elif shift:
            # цифровой ряд с Shift даёт знаки (!@#$…)
            has_sym = True
    if regular_case:
        # заглавные только в начале — это не отдельный «класс символов»,
        # иначе любое слово с большой буквы выглядело бы паролем
        has_upper = False
    classes = sum((has_lower, has_upper, has_digit, has_sym))
    # буквы вместе с цифрами/знаками, либо НЕОБЫЧНЫЙ смешанный регистр
    return classes >= 2 and (has_lower or has_upper)


def two_caps_typo(buffer):
    """Похоже ли слово на «ПРивет» — залипший Shift на второй букве.

    Правило нарочно строгое, потому что ошибиться тут дороже, чем
    промолчать: испорченное правильно набранное слово раздражает сильнее,
    чем неисправленная опечатка.

    Условия все сразу:
      * первые две буквы с Shift, а дальше НИ ОДНОЙ заглавной —
        «ПРиВет» не трогаем, там замысел пользователя непонятен;
      * первые три знака — буквы;
      * исправленное слово есть в словаре. Это главная защита: «привет»
        в словаре есть, а «ids» нет, поэтому английские сокращения во
        множественном числе (IDs, PCs, TVs) остаются целыми;
      * отдельная страховка на тот же случай — три знака, где третий «s».
    """
    if len(buffer) < 3:
        return False
    if not (buffer[0][1] and buffer[1][1]):
        return False
    if any(shift for _, shift in buffer[2:]):
        return False
    for kc, _ in buffer[:3]:
        pair = LAYOUT.get(kc)
        if not pair or not pair[0].isalpha():
            return False
    us = build(buffer, 0)
    ru = build(buffer, 1)
    if len(buffer) == 3 and us.endswith('s'):
        return False
    return us in EN_WORDS or ru in RU_WORDS


def mixed_case_inside(buffer):
    """Есть ли заглавная ПОСЛЕ строчной — «ПаРоЛь», «myPassword».

    Признак виден уже на третьей букве, в отличие от разбора пароля,
    которому нужно шесть знаков. Люди так слова не пишут, а пароли и
    служебные имена — сплошь и рядом.

    Нужен для раннего переключения: обычная проверка пароля требует шести
    знаков, а этот признак виден сразу. Увидев его в русской раскладке,
    уводим набор в латиницу — пароли почти всегда английские, и человек,
    начавший его по-русски, ошибся раскладкой.
    """
    seen_lower = False
    for kc, shift in buffer:
        pair = LAYOUT.get(kc)
        if not pair or not pair[0].isalpha():
            continue
        if shift and seen_lower:
            return True
        if not shift:
            seen_lower = True
    return False


def early_switch_needed(buffer, current):
    """Нужно ли переключить раскладку ПРЯМО СЕЙЧАС, не дожидаясь конца слова.

    Два уровня, как в Caramba/Punto:

    1) Невозможные пары клавиш — срабатывают уже на 2-й букве. Сочетания,
       которых в языке не бывает вовсе (`шы`, `jl`), проверены по словарям.

    2) Начало слова (PREFIX_LEN букв) — основной механизм, срабатывает на
       3-й букве. Если НИ ОДНО слово текущего языка не начинается с такого
       сочетания, значит печатают на другом языке. Покрывает ~87-91% слов.

    Проверка идёт только пока слово короткое (ровно PREFIX_LEN букв): решать
    по началу имеет смысл в начале, дальше работает обычный разбор по слову.

    current: 0 = US-раскладка, 1 = RU.
    Возврат: True, если надо переключиться на противоположную.
    """
    n = len(buffer)
    if n < 2:
        return False

    # 1) железные невозможные пары — самый быстрый сигнал
    pair = build(buffer[-2:], 0).lower()
    if len(pair) == 2:
        typed_us = build(buffer, 0).lower()
        # Не трогаем начала известных ТЕХТЕРМИНОВ (kvm, nvme, jq, vmware):
        # в обычном английском таких сочетаний нет, а слова рабочие.
        # Проверяем именно по списку техслов, а НЕ по всем английским
        # началам — иначе глушится половина переключений на русский.
        if current == 0 and TECH_PREFIX and typed_us in TECH_PREFIX:
            pass
        elif current == 1 and pair in IMPOSSIBLE_IN_RU:
            return True
        elif current == 0 and pair in IMPOSSIBLE_IN_EN:
            return True

    # 2) начало слова: пробуем на каждой длине от PREFIX_MIN до PREFIX_LEN.
    # Так быстрые случаи ловятся уже на 3-й букве, а слова, чьё начало
    # совпадает с реальным словом другого языка («hello» -> «руд» как в
    # «рудольф»), доловятся на 4-й — вместо ожидания пробела.
    if n < PREFIX_MIN or n > PREFIX_LEN:
        return False
    # то, что видит пользователь на экране в текущей раскладке
    shown = build(buffer, current).lower()
    if len(shown) != n:
        return False
    # выученные слова не трогаем: пользователь уже сказал, как их писать
    if LEARNED.get(build(buffer, 0).lower()):
        return False
    mine = RU_PREFIX if current == 1 else EN_PREFIX
    other_set = EN_PREFIX if current == 1 else RU_PREFIX
    other = build(buffer, 0 if current == 1 else 1).lower()
    # решаем только если для этой длины таблицы построены
    if not mine.get(n) or not other_set.get(n):
        return False
    if shown in mine[n]:
        return False            # такое начало в нашем языке бывает
    return other in other_set[n]  # а в другом — да, значит переключаем


def detect_lang(buffer, prev_lang=None):
    """Гибрид: выученное -> словарь -> короткие слова -> контекст -> биграммы.
    prev_lang — язык предыдущего распознанного слова (контекст).
    Возврат: 'us','ru','unknown'."""
    as_us = build(buffer, 0).lower()
    as_ru = build(buffer, 1).lower()

    # 0) ВЫУЧЕННОЕ ПРАВИЛО — сильнее всего. Если пользователь однажды
    #    отменил исправление этого слова, оставляем язык, который он выбрал.
    #    Ключ — US-форма (раскладко-независимая запись клавиш).
    lrn = LEARNED.get(as_us)
    if lrn in ('ru', 'us'):
        return lrn

    # слишком короткие слова не трогаем вовсе (min_word_length в конфиге)
    if MIN_WORD_LEN > 1 and len(as_us) < MIN_WORD_LEN:
        return 'unknown'

    en_short = as_us in SHORT_EN
    ru_short = as_ru in SHORT_RU

    # одиночные символы
    if len(as_us) <= 1:
        if SHORT_WORDS:
            if en_short and not ru_short:
                return 'us'
            if ru_short and not en_short:
                return 'ru'
            if en_short and ru_short:
                # настоящая коллизия ('а' vs 'a') — решает контекст
                if USE_CONTEXT and prev_lang in ('us', 'ru'):
                    return prev_lang
        return 'unknown'

    in_en = as_us in EN_WORDS
    in_ru = as_ru in RU_WORDS

    # 1) словарь — самый надёжный сигнал
    if in_ru and not in_en:
        return 'ru'
    if in_en and not in_ru:
        return 'us'

    # 2) короткие слова — явный список коротких слов
    if SHORT_WORDS and len(as_us) <= 3:
        if en_short and not ru_short:
            return 'us'
        if ru_short and not en_short:
            return 'ru'
        if en_short and ru_short and USE_CONTEXT and prev_lang in ('us', 'ru'):
            return prev_lang

    # 3) оба знают ИЛИ оба молчат.
    # При use_bigrams=no догадки по сочетаниям букв отключены: решает
    # только словарь и контекст, а в неясном случае слово остаётся как есть.
    if not USE_BIGRAMS:
        if USE_CONTEXT and prev_lang in ('us', 'ru'):
            return prev_lang
        return 'unknown'

    su = score_bigram(as_us, EN_LETTERS, EN_BIGRAMS)
    sr = score_bigram(as_ru, RU_LETTERS, RU_BIGRAMS, RU_TRIGRAMS)

    if abs(su - sr) < 0.08:
        # ничья — пробуем контекст
        if USE_CONTEXT and prev_lang in ('us', 'ru'):
            return prev_lang
        return 'unknown'
    return 'ru' if sr > su else 'us'


def tap(ui, code, shift=False):
    if shift:
        ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1); ui.syn()
    ui.write(e.EV_KEY, code, 1)
    ui.write(e.EV_KEY, code, 0)
    ui.syn()
    if shift:
        ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0); ui.syn()


def _drain_input(dev):
    """Прочитать и выбросить все накопившиеся события клавиатуры.

    Во время rewrite (стирание+перепечатка) пользователь при быстрой печати
    продолжает жать клавиши. Эти живые нажатия копятся в дескрипторе и, если
    их не убрать, смешиваются с эмулируемыми бэкспейсами → курсор уезжает,
    новый текст ложится поверх старого. Глотаем их, пока идёт автозамена.
    Вызывается ДО и ПОСЛЕ перепечатки. Без блокировки (select с 0)."""
    try:
        import select
        while True:
            r, _, _ = select.select([dev.fd], [], [], 0)
            if not r:
                break
            got = False
            for _ev in dev.read():
                got = True
            if not got:
                break
    except Exception:
        pass


def rewrite(ui, buffer, target=None, shift_held=False, tail_keys=None,
            kbd=None, keep_layout=False):
    """Перепечатать слово в другой раскладке.
    tail_keys — клавиши, напечатанные ПОСЛЕ слова (знаки препинания):
    их стираем вместе со словом и печатаем заново в конце.
    kbd — устройство ввода; если задано, живой ввод во время перепечатки
    проглатывается, чтобы быстрая печать не смешивалась с автозаменой."""
    tail_keys = tail_keys or []
    # Примечание: раньше здесь звался _drain_input(kbd), но он читал fd
    # напрямую и НЕ видел события, уже вынутые read_loop во внутреннюю
    # очередь генератора — потому перескок и оставался. Защита перенесена
    # в главный цикл (swallow_until): там события глотаются по факту чтения,
    # что надёжно. Параметр kbd оставлен для совместимости вызова.
    _ = kbd
    # Если Shift физически зажат (набирают "слово!"), на время перепечатки
    # его надо отпустить — иначе слово выйдет заглавными буквами.
    if shift_held:
        ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
        ui.write(e.EV_KEY, e.KEY_RIGHTSHIFT, 0)
        ui.syn()
    # Стирание слова: шлём бэкспейсы залпом, одним syn. Чем короче окно
    # автозамены, тем меньше шанс, что живой ввод в него вклинится.
    n_del = len(buffer) + len(tail_keys)
    for _ in range(n_del):
        ui.write(e.EV_KEY, e.KEY_BACKSPACE, 1)
        ui.write(e.EV_KEY, e.KEY_BACKSPACE, 0)
    ui.syn()
    # keep_layout — перепечатка без смены раскладки (правка регистра).
    # Без этого switch_layout(None) ПЕРЕКЛЮЧИЛ бы раскладку: при target=None
    # он трактует вызов как «смени на противоположную».
    if not keep_layout:
        switch_layout(ui, target)
        # На Wayland ожидание уже внутри switch_layout: там раскладку
        # переспрашивают, пока она не применится. На X11 группа ставится
        # напрямую через libX11, но серверу всё равно нужно мгновение —
        # иначе первые буквы уходят в старой раскладке.
        if layout.session() != 'wayland':
            time.sleep(0.12)
    # Перепечатка слова: тоже залпом.
    for kc, shift in buffer:
        if shift:
            ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
        ui.write(e.EV_KEY, e.ecodes[kc], 1)
        ui.write(e.EV_KEY, e.ecodes[kc], 0)
        if shift:
            ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
    # вернуть знаки препинания после слова
    for code, shift in tail_keys:
        if shift:
            ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
        ui.write(e.EV_KEY, code, 1)
        ui.write(e.EV_KEY, code, 0)
        if shift:
            ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
    ui.syn()
    if shift_held:
        ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
        ui.syn()
    # Заморозка живого ввода после этой правки выставляется в главном цикле
    # (swallow_until) — она надёжнее прежнего _drain_input здесь.


def load_config(path):
    """Прочитать конфиг ключ=значение. Вернуть dict. Отсутствие файла — не ошибка."""
    cfg = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def main():
    global RU_WORDS, EN_WORDS, SHORT_WORDS, USE_CONTEXT, DEBUG, EARLY_SWITCH, FIX_TWO_CAPS
    global SHIFT_SWITCHES_LAYOUT, LOG_MAX_BYTES, LOG_LANG
    global RU_PREFIX, EN_PREFIX, TECH_PREFIX
    global USE_BIGRAMS, MIN_WORD_LEN
    _resolve_log_path()
    # модуль layout пишет свои сообщения через наш журнал: у службы stdout
    # закрыт, и без этого они пропадали
    layout.set_logger(log)
    log(layout.session_summary())
    log(env_summary())
    log("Loading dictionaries...")
    t0 = time.time()
    RU_WORDS, EN_WORDS = dictionaries.load()
    # Префиксы длиной PREFIX_LEN: с каких сочетаний реально начинаются слова
    # каждого языка. Это ядро раннего переключения «как в Caramba»: если
    # набранное начало слова не встречается НИ В ОДНОМ слове текущего языка,
    # значит печатают на другом — переключаем уже на 3-й букве. Покрытие по
    # словарям ~87-91% слов против ~16-61% у одних лишь невозможных пар.
    RU_PREFIX = {}
    EN_PREFIX = {}
    for k in range(PREFIX_MIN, PREFIX_LEN + 1):
        RU_PREFIX[k] = {w[:k] for w in RU_WORDS if len(w) >= k}
        EN_PREFIX[k] = {w[:k] for w in EN_WORDS if len(w) >= k}
    TECH_PREFIX = set()
    try:
        from tech_words import TECH_WORDS
        for w in TECH_WORDS:
            w = w.lower()
            for k in range(2, min(len(w), PREFIX_LEN) + 1):
                TECH_PREFIX.add(w[:k])
    except Exception:
        pass
    log(f"Dictionaries: RU={len(RU_WORDS)}, EN={len(EN_WORDS)} in {time.time()-t0:.2f}s")
    load_learned()
    log(f"Learned rules: {len(LEARNED)}")

    # --- конфиг (значения по умолчанию перебиваются аргументами) ---
    cfg = {}
    if '--config' in sys.argv:
        i = sys.argv.index('--config')
        if i + 1 < len(sys.argv):
            cfg = load_config(sys.argv[i + 1])

    # улучшенное распознавание коротких слов и контекст предыдущего слова
    SHORT_WORDS = cfg.get('short_words', 'on').lower() not in ('off', '0', 'no', 'false')
    USE_CONTEXT = cfg.get('use_context', 'on').lower() not in ('off', '0', 'no', 'false')

    # догадки по сочетаниям букв для слов, которых нет в словаре
    USE_BIGRAMS = cfg.get('use_bigrams', 'yes').lower() not in ('off', '0', 'no', 'false')

    # подробный лог с набранными словами — ТОЛЬКО если явно включён.
    # По умолчанию off ради приватности (пароли/seed-фразы в journald).
    DEBUG = cfg.get('debug_log', 'off').lower() in ('on', '1', 'yes', 'true')
    # Починка залипшего Shift («ПРивет» → «Привет»). Включена по умолчанию:
    # человек ставит программу и ждёт, что она просто работает, как Caramba
    # или Punto. Риск невелик — правило чинит слово, только если после
    # починки оно становится словарным, поэтому IDs и PCs остаются целыми.
    FIX_TWO_CAPS = cfg.get('fix_two_capitals', 'on').lower() \
        not in ('off', '0', 'no', 'false')

    # язык сообщений журнала — тот же, что у интерфейса
    _ul = (cfg.get('ui_lang', '') or '').strip().lower()
    LOG_LANG = 'ru' if _ul == 'ru' else 'en'

    # предельный размер файла журнала, МБ (0 = не ограничивать)
    try:
        LOG_MAX_BYTES = max(0, int(cfg.get('log_max_size', '5'))) * 1024 * 1024
    except ValueError:
        LOG_MAX_BYTES = 5 * 1024 * 1024

    # раннее переключение по невозможным сочетаниям (эффект Caramba)
    EARLY_SWITCH = cfg.get('early_switch', 'on').lower() not in ('off', '0', 'no', 'false')

    # одиночный Shift переключает раскладку (совместимость с Caramba)
    SHIFT_SWITCHES_LAYOUT = cfg.get('shift_switches_layout', 'on').lower() \
        not in ('off', '0', 'no', 'false')

    # слова короче этой длины не трогаем совсем; 1 = не отсекать ничего
    try:
        MIN_WORD_LEN = max(1, int(cfg.get('min_word_length', '1')))
    except ValueError:
        MIN_WORD_LEN = 1

    # доп. исключённые приложения из конфига (через запятую), к дефолтным.
    # Запись с '*' на конце → префикс (напр. steam*, com.valvesoftware.steam*).
    # Иначе → точное совпадение класса.
    extra = cfg.get('exclude_apps', '').strip()
    if extra:
        for app in extra.split(','):
            app = app.strip().lower()
            if not app:
                continue
            if app.endswith('*'):
                EXCLUDE_PREFIXES.add(app[:-1])
            else:
                EXCLUDE_APPS.add(app)

    # С какими настройками работаем. ВАЖНО: строка печатается только
    # ЗДЕСЬ, после разбора конфига. Раньше она стояла в начале main(),
    # до чтения файла, и показывала значения по умолчанию — что бы ни
    # было в конфиге. По ней нельзя было судить ни об одной настройке:
    # правишь debug_log=on, а в журнале по-прежнему off.
    log('[autoswitch] settings: '
        + ' '.join([
            f"early_switch={'on' if EARLY_SWITCH else 'off'}",
            f"shift_switches_layout={'on' if SHIFT_SWITCHES_LAYOUT else 'off'}",
            f"min_word_length={MIN_WORD_LEN}",
            f"use_bigrams={'yes' if USE_BIGRAMS else 'no'}",
            f"short_words={'on' if SHORT_WORDS else 'off'}",
            f"use_context={'on' if USE_CONTEXT else 'off'}",
            f"debug_log={'on' if DEBUG else 'off'}",
            f"fix_two_capitals={'on' if FIX_TWO_CAPS else 'off'}",
        ]))

    # стартовая раскладка: аргумент > конфиг > us
    if '--start-ru' in sys.argv:
        current = 1
    else:
        current = 1 if cfg.get('start_layout', 'us').lower() == 'ru' else 0

    # определить устройство клавиатуры
    device = None
    if '--device' in sys.argv:
        i = sys.argv.index('--device')
        if i + 1 < len(sys.argv):
            device = sys.argv[i + 1]
    if not device:
        cfg_kbd = cfg.get('keyboard', 'auto')
        device = None if cfg_kbd in ('', 'auto') else cfg_kbd
    # Заданный путь может исчезнуть (устройство переехало/отключено) —
    # тогда откатываемся на автопоиск, чтобы не падать. Пути by-id
    # (симлинки) разрешаем в реальный узел.
    if device:
        real = os.path.realpath(device)
        if not os.path.exists(real):
            log(f"Configured keyboard '{device}' not found, auto-detecting...")
            device = None
        else:
            device = real
    if not device:
        device = find_keyboard()
    if not device:
        log("Error: keyboard not found. Specify manually: --device /dev/input/eventN")
        log("List devices: python3 -c \"from evdev import list_devices,InputDevice; "
              "[log(p, InputDevice(p).name) for p in list_devices()]\"")
        return
    log(f"Keyboard: {device}")

    try:
        kbd = InputDevice(device)
    except Exception as ex:
        # устройство есть в пути, но не открывается — пробуем автопоиск
        log(f"Cannot open {device} ({ex}), auto-detecting...")
        device = find_keyboard()
        if not device:
            log("Error: keyboard not found.")
            return
        kbd = InputDevice(device)
    ui = UInput.from_device(kbd, name='autoswitch-virtual-kbd')
    time.sleep(0.2)
    # Захват клавиатуры. Если прежний экземпляр службы ещё доживает после
    # перезапуска, устройство занято — ждём, а не работаем параллельно с ним:
    # два процесса одновременно исправляли одно слово, и первое слово после
    # установки выходило удвоенным («сттак» вместо «так»).
    for attempt in range(20):
        try:
            kbd.grab()
            break
        except OSError:
            if attempt == 0:
                log("Keyboard busy (previous instance?), waiting...")
            time.sleep(0.25)
    else:
        log("Error: keyboard is busy, another autoswitch is running?")
        return
    # Выбрасываем всё, что пользователь успел настучать до захвата: эти
    # события уже ушли в приложение напрямую, обрабатывать их нельзя.
    try:
        import select as _sel
        while _sel.select([kbd.fd], [], [], 0)[0]:
            for _ in kbd.read():
                pass
    except Exception:
        pass
    log(f"autoswitch started. Keyboard grabbed ({kbd.name}).")
    log(f"Initial layout: {'RU' if current else 'US'}. Ctrl+C to exit.\n")

    buffer = []
    tail_keys = []     # знаки препинания, набранные после слова
    prev_lang = None   # язык предыдущего распознанного слова (контекст)
    shift_down = False
    ctrl_down = False

    # ── состояние обучения на отменах ──────────────────────────────────
    # last_fix хранит последнее АВТО-исправление, пока пользователь может
    # его откатить. Отмена ловится двумя простыми и надёжными сигналами,
    # пока не начато новое слово (buffer пустой) и не вышел таймаут:
    #   • первый Backspace после правки — «стираю, не то»;
    #   • ручная смена раскладки после правки — «оставляю как было».
    #   'us_form'   — US-форма исправленного слова (ключ обучения)
    #   'from_lang' — язык, из которого мы переключили (что набрал юзер)
    #   'to_layout' — раскладка (0/1), которую мы выставили правкой
    #   'time'      — момент правки (для таймаута)
    last_fix = None
    UNDO_TIMEOUT = 6.0   # сек: позже этого откат уже не связываем с правкой

    # ── защита от «перескока курсора» при быстрой печати ───────────────
    # Клавиатура захвачена (grab), поэтому физические нажатия, сделанные
    # ПОКА идёт rewrite, ждут нас в очереди evdev и читаются уже ПОСЛЕ
    # перепечатки. Если их сразу пробросить, композитор ещё не успел
    # применить наши буквы — живой ввод ложится поверх, курсор «прыгает».
    # После каждой правки выставляем окно, в течение которого входящие
    # печатные события проглатываются (как в Punto/Caramba — на время
    # автозамены ввод заморожен). Служебные модификаторы не глотаем.
    SWALLOW_WINDOW = 0.045   # сек: сколько глотать живой ввод после правки
    swallow_until = 0.0
    early_done = False       # раннее переключение уже сработало в этом слове
    layout_known = True      # удалось ли прочитать реальную раскладку; при
    # сбое моста GNOME правка идёт вслепую и портит текст, поэтому молчим
    _no_wcls_at = [0.0]      # когда последний раз жаловались на окно
    _wcls_ok = [False]       # окно хоть раз успешно прочиталось
    _started_at = time.time()
    complex_input = False    # в текущем наборе была цифра/символ (пароль,
    # код, артикул) — до конца ввода ничего не конвертируем и не логируем
    shift_clean = False      # Shift нажат «сам по себе» (без других клавиш)
    last_shift_time = 0.0    # для детекта двойного Shift
    DOUBLE_SHIFT_GAP = 0.4   # сек: максимум между нажатиями Shift

    def _reset_word():
        """Сбросить всё состояние текущего слова.

        Раньше это делалось вручную в девяти местах, и в шести из них
        сбрасывали не всё: buffer с tail_keys чистили, а флаги early_done и
        complex_input оставались висеть от прошлого слова. Отсюда странные
        эффекты вроде лишней буквы («wworld») — движок считал, что раннее
        переключение уже было, или что в наборе есть цифра, хотя слово
        началось заново. Теперь сброс один и полный.
        """
        nonlocal buffer, tail_keys, early_done, complex_input
        buffer = []
        tail_keys = []
        early_done = False
        complex_input = False

    def _apply_fix(from_lang, to_lang, early, event_code=None):
        """Единственное место, где происходит исправление слова.

        Раньше это делали два разных куска кода — один для правки по концу
        слова, другой для раннего переключения. Они разошлись в деталях, и
        каждый баг приходилось чинить дважды (терялась буква «is»->«i»,
        оставалась лишняя «wworld», съедался Backspace). Теперь путь один,
        а различия сведены к флагу early.

        early=True  — слово ещё набирается (переключаем на 3-4 букве):
                      надо отпустить удерживаемую клавишу и дать приложению
                      мгновение на отрисовку, а заморозку ввода НЕ ставить —
                      пользователь продолжает печатать это же слово.
        early=False — слово завершено (правка по пробелу): клавиша уже
                      отпущена, зато нужна заморозка, чтобы набранное во
                      время автозамены не легло поверх текста.
        """
        nonlocal current, last_fix, swallow_until, prev_lang, early_done
        target = 1 - current
        dbg(lg('early' if early else 'fix',
               a=build(buffer, current), b=build(buffer, target)))
        if early:
            # Мы между НАЖАТИЕМ буквы и её отпусканием: если начать
            # перепечатку с «зажатой» клавишей, бэкспейсы уйдут поверх
            # удерживаемого символа и буква потеряется.
            if event_code is not None:
                ui.write(e.EV_KEY, event_code, 0)
                ui.syn()
            # дать приложению отрисовать последнюю букву: при правке по
            # пробелу эта пауза набегает сама, а здесь её нет
            time.sleep(0.02)
        rewrite(ui, buffer, target, shift_down, tail_keys, kbd=kbd)
        if not early:
            # заморозить живой ввод: набранное во время автозамены не должно
            # лечь поверх перепечатанного слова (перескок курсора)
            swallow_until = time.time() + SWALLOW_WINDOW
        else:
            early_done = True      # в этом слове больше не дёргаем
        last_fix = {
            'us_form': build(buffer, 0).lower(),
            'from_lang': from_lang,
            'to_lang': to_lang,
            'to_layout': target,
            'time': time.time(),
            # для ручной отмены (двойной Shift / Pause):
            'buf': list(buffer),
            'tail': [] if early else list(tail_keys),
            'spaced': not early,   # был ли уже напечатан разделитель
        }
        current = target
        prev_lang = to_lang

    def _manual_revert():
        """Ручная отмена последней конвертации (двойной Shift или Pause).

        Возвращает слово в том виде, как его набирал пользователь, и
        запоминает правило — как в Caramba: одним жестом и откат, и
        исключение. Быстрее, чем стирать длинное слово бэкспейсами.
        Возврат: True, если было что отменять.
        """
        nonlocal last_fix, current, buffer, tail_keys, prev_lang
        if not last_fix or not last_fix.get('buf'):
            return False
        if time.time() - last_fix['time'] > UNDO_TIMEOUT:
            return False
        buf = last_fix['buf']
        tail = last_fix.get('tail') or []
        back = last_fix['from_lang']            # куда возвращаем
        target = 0 if back == 'us' else 1
        # Shift сейчас физически зажат (им же и вызвали отмену) — отпускаем,
        # иначе перепечатанное слово выйдет ЗАГЛАВНЫМИ буквами.
        ui.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
        ui.write(e.EV_KEY, e.KEY_RIGHTSHIFT, 0)
        ui.syn()
        time.sleep(0.02)
        # если правка была по концу слова, после неё напечатан разделитель —
        # стираем и его, потом вернём обратно
        extra = 1 if last_fix.get('spaced') else 0
        for _ in range(extra):
            ui.write(e.EV_KEY, e.KEY_BACKSPACE, 1)
            ui.write(e.EV_KEY, e.KEY_BACKSPACE, 0)
        ui.syn()
        rewrite(ui, buf, target, False, tail, kbd=kbd)
        if extra:
            tap(ui, e.KEY_SPACE)
        current = target
        # Запомнить правило — но только если оно осмысленно. Caramba делает
        # так же: «наиболее употребимые слова и аббревиатуры в правила внести
        # нельзя, это ломает языковую модель». Само слово при этом всё равно
        # откачено — просто исключение не создаётся, чтобы не копить мусор
        # вроде «руддщ = русское» (это же просто латиница не в той раскладке).
        uf = last_fix['us_form']
        if _worth_learning(uf, back):
            save_learned_rule(uf, back)
            dbg(lg('revert_keep', w=uf, lang=back.upper()))
        else:
            dbg(lg('revert_nolearn', w=uf))
        prev_lang = back
        last_fix = None
        _reset_word()
        return True

    def _learn_undo(reason):
        """Зафиксировать отмену: слово остаётся в исходном языке."""
        nonlocal last_fix, prev_lang
        if not last_fix:
            return
        uf = last_fix['us_form']
        keep = last_fix['from_lang']
        # фильтр: не запоминать словарный мусор от случайных откатов
        if not _worth_learning(uf, keep):
            dbg(lg('skip_learn', w=uf))
            last_fix = None
            return
        save_learned_rule(uf, keep)
        dbg(lg('learned', why=reason, w=uf, lang=keep.upper()))
        prev_lang = keep
        last_fix = None

    try:
        for event in kbd.read_loop():
            if event.type != ecodes.EV_KEY:
                continue

            data = categorize(event)
            kc = data.keycode
            if isinstance(kc, list):
                kc = kc[0]

            # Окно заморозки после правки по концу слова: глотаем живой ввод,
            # накопившийся пока шла автозамена (см. SWALLOW_WINDOW выше).
            # Модификаторы (Shift/Ctrl/Alt/Meta) пропускаем всегда — иначе
            # «залипнут». Backspace тоже пропускаем: им пользователь либо
            # правит ввод, либо отменяет наше исправление (сигнал обучения),
            # и съедать его нельзя. Остальные печатные клавиши глотаем
            # целиком (нажатие+отпускание), чтобы не осталось «отпускание
            # без нажатия».
            if swallow_until:
                if time.time() < swallow_until:
                    if (kc not in SHIFT_KEYS and kc not in CTRL_KEYS
                            and kc not in OTHER_MODS
                            and kc != 'KEY_BACKSPACE'):
                        continue
                else:
                    swallow_until = 0.0

            # Любая клавиша кроме Shift помечает текущее нажатие Shift как
            # «грязное»: им набирали заглавную букву, а не переключали
            # раскладку. Делать это НАДО ДО всех веток с continue (окно в
            # исключениях, модификаторы, Ctrl) — иначе в терминале, который
            # исключён, флаг оставался чистым, и отпускание Shift после
            # заглавной буквы переключало раскладку прямо посреди пароля.
            if kc not in SHIFT_KEYS and event.value == 1:
                last_shift_time = 0.0
                shift_clean = False

            if kc in SHIFT_KEYS:
                ui.write(e.EV_KEY, event.code, event.value); ui.syn()
                if event.value == 1:
                    shift_down = True
                    # начали нажатие: пока «чистое» — если до отпускания не
                    # придёт другая клавиша, значит Shift нажали сам по себе
                    shift_clean = True
                    if ctrl_down:
                        # Ctrl+Shift — ручная смена раскладки (как было)
                        shift_clean = False
                        current = 1 - current
                        _reset_word()
                        dbg(lg('manual', lang='RU' if current else 'US'))
                elif event.value == 0:
                    shift_down = False
                    # Отпустили. Если между нажатием и отпусканием НЕ было
                    # других клавиш — это самостоятельное нажатие Shift:
                    #   • второе подряд (быстро) — отмена конвертации;
                    #   • одиночное — переключение раскладки (как в Caramba).
                    # Если клавиши были — просто набирали заглавную букву,
                    # ничего не делаем.
                    if shift_clean:
                        now = time.time()
                        if now - last_shift_time <= DOUBLE_SHIFT_GAP:
                            last_shift_time = 0.0
                            if _manual_revert():
                                shift_clean = False
                                continue
                        else:
                            last_shift_time = now
                            if SHIFT_SWITCHES_LAYOUT:
                                switch_layout(ui, 1 - current)
                                current = 1 - current
                                _reset_word()
                                dbg(lg('shift', lang='RU' if current else 'US'))
                    shift_clean = False
                continue

            if kc in CTRL_KEYS:
                ctrl_down = (event.value != 0)
                ui.write(e.EV_KEY, event.code, event.value); ui.syn()
                if event.value == 1 and shift_down:
                    current = 1 - current
                    _reset_word()
                    dbg(f"  [manual -> {'RU' if current else 'US'}]")
                continue

            if kc in OTHER_MODS:
                ui.write(e.EV_KEY, event.code, event.value); ui.syn()
                continue

            # Окно в списке исключений (игры, терминалы): полностью не
            # вмешиваемся — пробрасываем событие как есть, ничего не копим.
            # Важно для игр: Tab/пробел не должны переотправляться, иначе
            # ломается удержание и тайминг.
            _wcls = active_window_class()
            if _wcls:
                _wcls_ok[0] = True
            if is_excluded(_wcls):
                ui.write(e.EV_KEY, event.code, event.value); ui.syn()
                if buffer or tail_keys:
                    _reset_word()
                continue
            if not _wcls:
                # Класс окна определить не удалось (мост GNOME не ответил,
                # композитор занят). Мы не знаем, не терминал ли это, а
                # значит копить буфер опасно: буквы из исключённого окна
                # приклеивались к следующему слову («conghjdthrf» вместо
                # «ghjdthrf»). Пропускаем клавишу как есть.
                #
                # Об этом пишем в журнал: раньше движок в таком состоянии
                # молчал полностью, и снаружи это выглядело как «работает,
                # но ничего не переключает».
                #
                # Но НЕ пока рабочий стол не поднялся.
                #
                # Служба стартует раньше входа в систему — намеренно: на
                # экране входа человек печатает пароль, и клавиатура должна
                # быть захвачена заранее, иначе подключение посреди набора
                # грозит потерянными нажатиями. Но оболочки в этот момент
                # ещё нет, и окно честно не читается. Это нормальный этап
                # загрузки, а не поломка — раньше мы писали о нём в журнал
                # при каждом включении компьютера.
                #
                # Поэтому: пока окно не прочиталось НИ РАЗУ — молчим.
                # Как только прочиталось хоть однажды, рабочий стол точно
                # есть, и любое пропадание уже настоящая ошибка.
                #
                # Страховка на случай, если не прочиталось никогда: через
                # пять минут говорим всё равно. За пять минут заканчивается
                # любая загрузка, значит дело не в ней.
                # Не чаще раза в 10 секунд, чтобы не залить журнал.
                _now = time.time()
                if (_wcls_ok[0] or _now - _started_at > 300) \
                        and _now - _no_wcls_at[0] > 10:
                    _no_wcls_at[0] = _now
                    log(lg('no_window_class'))
                ui.write(e.EV_KEY, event.code, event.value); ui.syn()
                if buffer or tail_keys:
                    _reset_word()
                continue

            if ctrl_down:
                ui.write(e.EV_KEY, event.code, event.value); ui.syn()
                if event.value == 1:
                    _reset_word()
                continue

            # Pause/Break — отменить последнюю конвертацию (как в Caramba,
            # альтернатива двойному Shift).
            if kc in ('KEY_PAUSE', 'KEY_BREAK'):
                if event.value == 1:
                    _manual_revert()
                continue

            if kc in WORD_BREAK:
                if event.value != 1:
                    continue
                if buffer and not complex_input and not looks_like_password(buffer):
                    _refresh_learned()  # подхватить правки из GUI/asc
                    # синхронизируем реальную раскладку: пользователь мог
                    # сменить её мышью/хоткеем помимо движка
                    real = read_real_layout()
                    if real in (0, 1) and real != current:
                        current = real
                    cur_name = 'us' if current == 0 else 'ru'

                    # Если раскладку прочитать НЕ УДАЛОСЬ (мост GNOME отвалился,
                    # композитор перезапустился, D-Bus недоступен), мы не знаем,
                    # в какой раскладке пользователь на самом деле печатает.
                    # Исправлять вслепую нельзя: движок перепечатывает слово не
                    # в ту сторону, а переключить раскладку всё равно не может —
                    # текст портится, курсор уезжает. Пропускаем слово.
                    if real is None:
                        dbg(lg('layout_unknown'))
                        _reset_word()
                        tap(ui, event.code)
                        continue

                    # Залипший Shift на второй букве. Правим ДО решения о
                    # раскладке: снимаем флаг Shift со второй клавиши, и
                    # если слово всё равно пойдёт на перепечатку из-за
                    # раскладки, регистр починится той же перепечаткой —
                    # одно стирание вместо двух подряд.
                    caps_fix = FIX_TWO_CAPS and two_caps_typo(buffer)
                    if caps_fix:
                        buffer[1] = (buffer[1][0], False)

                    lang = detect_lang(buffer, prev_lang)
                    if lang in ('us', 'ru'):
                        prev_lang = lang
                    if lang in ('us', 'ru') and lang != cur_name:
                        _apply_fix(cur_name, lang, early=False)
                        if caps_fix:
                            log(lg('two_caps', w=build(buffer, current)))
                    elif caps_fix:
                        # раскладка верная, чинить надо только регистр
                        w = build(buffer, current)
                        rewrite(ui, buffer, shift_held=shift_down,
                                tail_keys=tail_keys, kbd=kbd, keep_layout=True)
                        log(lg('two_caps', w=w))
                        last_fix = None
                    else:
                        dbg(lg('ok', lang=lang, w=build(buffer, current)))
                        last_fix = None
                # пробел/перевод строки после слова: если это НЕ откат,
                # правка «зафиксирована» — но last_fix держим до таймаута,
                # чтобы поймать Backspace через пробел («идее » → стереть).
                _reset_word()
                tap(ui, event.code)
                continue

            # Знаки препинания (Shift+цифра и т.п.): пропускаем символ как есть,
            # слово в буфере НЕ трогаем — исправление сработает по пробелу.
            if kc in PUNCT_KEYS:
                ui.write(e.EV_KEY, event.code, event.value); ui.syn()
                if event.value == 1 and buffer:
                    tail_keys.append((event.code, shift_down))
                continue

            ui.write(e.EV_KEY, event.code, event.value); ui.syn()
            if event.value != 1:
                continue

            if kc == 'KEY_BACKSPACE':
                if buffer:
                    # стираем внутри текущего слова — обычная правка ввода
                    buffer.pop()
                elif (last_fix
                      and time.time() - last_fix['time'] <= UNDO_TIMEOUT):
                    # буфер пуст, а пользователь жмёт Backspace сразу после
                    # авто-правки — он стирает ТОЛЬКО ЧТО исправленное слово.
                    # Это самый надёжный сигнал отмены: учим исходный язык.
                    _learn_undo('backspace')
            elif kc in LAYOUT:
                # Начало нового слова: синхронизируем current с РЕАЛЬНОЙ
                # раскладкой. Без этого раннее переключение проверяло таблицу
                # не для той раскладки (движок помнил старое значение с
                # прошлого пробела) и не срабатывало вовсе. Читаем только на
                # первой букве — внутри слова раскладка уже не меняется.
                if not buffer:
                    early_done = False   # новое слово — снова можно
                    layout_known = True
                    real = read_real_layout()
                    if real in (0, 1):
                        if (last_fix
                                and time.time() - last_fix['time'] <= UNDO_TIMEOUT
                                and real != last_fix['to_layout']):
                            # раскладка уехала от нашей правки — пользователь
                            # вернул её сам. Это отмена: учим исходный язык.
                            current = real
                            _learn_undo('раскладка')
                        else:
                            current = real
                    else:
                        # Раскладку не прочитать (мост отвалился) — до конца
                        # слова не вмешиваемся: правка вслепую портит текст.
                        layout_known = False
                buffer.append((kc, shift_down))

                # ── РАННЕЕ ПЕРЕКЛЮЧЕНИЕ (как в Caramba) ──────────────
                # Не ждём пробела: если две последние клавиши дают в
                # текущей раскладке сочетание, невозможное для её языка,
                # значит печатается другой язык — правим прямо сейчас.
                # Слово уже набранное перепечатываем целиком, чтобы
                # исправились и первые буквы.
                if EARLY_SWITCH and not early_done and not complex_input \
                        and layout_known:
                    if mixed_case_inside(buffer) and current == 1:
                        # Заглавная ПОСЛЕ строчной — почти всегда пароль или
                        # служебное имя, а такие набирают латиницей. Если
                        # сейчас русская раскладка, человек ошибся: уводим в
                        # английский, не дожидаясь конца набора. Так же
                        # поступает Caramba — у неё пароль вводится по-
                        # английски, а не остаётся абракадаброй.
                        _apply_fix('ru', 'us', early=True,
                                   event_code=event.code)
                    elif not looks_like_password(buffer) \
                            and early_switch_needed(buffer, current):
                        _apply_fix('ru' if current == 1 else 'us',
                                   'us' if current == 1 else 'ru',
                                   early=True, event_code=event.code)
            else:
                # Цифра, спецсимвол или служебная клавиша посреди набора.
                # Это признак пароля/кода/артикула («Pass123!», «dom2»):
                # такие наборы движок НЕ конвертирует вовсе — человек мог
                # набрать их и латиницей, и намеренно кириллицей, любая
                # правка их сломает. И в лог они не попадают никогда,
                # даже при debug_log=on (там могут быть пароли).
                was_word = bool(buffer) and kc not in WORD_BREAK
                _reset_word()
                # цифра/символ посреди набора — до конца ввода не трогаем
                if was_word:
                    complex_input = True
    finally:
        kbd.ungrab()
        ui.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log("\nautoswitch stopped.")
