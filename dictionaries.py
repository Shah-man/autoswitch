#!/usr/bin/env python3
# dictionaries.py — загрузчик словарей для autoswitch
#
# Читает hunspell .dic (/usr/share/hunspell/{ru_RU,en_US}.dic), чистит их
# (убирает флаги после '/', оставляет только слова нужного алфавита в нижнем
# регистре) и строит быстрые set. Кеширует результат в ~/.cache/autoswitch/
# как pickle, чтобы повторные запуски были мгновенными.
#
# Принцип: точность (большой словарь) + скорость (set O(1) + кеш).

import os
import pickle
import sys

# гарантируем, что каталог модуля в пути (для импорта tech_words)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RU_DIC = '/usr/share/hunspell/ru_RU.dic'
EN_DIC = '/usr/share/hunspell/en_US.dic'

# пользовательский словарь (не затирается обновлением программы)
USER_WORDS_FILE = '/etc/autoswitch/user_words.txt'

CACHE_DIR = os.path.expanduser('~/.cache/autoswitch')
CACHE_FILE = os.path.join(CACHE_DIR, 'words.pkl')

RU_LETTERS = set('абвгдежзийклмнопрстуфхцчшщъыьэюяё')
EN_LETTERS = set('abcdefghijklmnopqrstuvwxyz')


def _parse_dic(path, allowed):
    """Прочитать .dic, вернуть set слов (нижний регистр, только нужный алфавит)."""
    words = set()
    if not os.path.exists(path):
        return words
    with open(path, encoding='utf-8', errors='ignore') as f:
        first = True
        for line in f:
            line = line.strip()
            if first:
                # первая строка .dic — число слов, пропускаем если это цифра
                first = False
                if line.isdigit():
                    continue
            if not line:
                continue
            word = line.split('/', 1)[0].strip().lower()
            if not word:
                continue
            # оставляем только слова, целиком состоящие из букв нужного алфавита
            if all(ch in allowed for ch in word):
                words.add(word)
    return words


def load(rebuild=False):
    """Вернуть (ru_set, en_set). Использует кеш, если он свежий."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    # проверка свежести кеша по mtime исходных .dic + tech_words
    def newest_src():
        t = 0
        srcs = [RU_DIC, EN_DIC,
                os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tech_words.py')]
        for p in srcs:
            if os.path.exists(p):
                t = max(t, os.path.getmtime(p))
        return t

    if not rebuild and os.path.exists(CACHE_FILE):
        if os.path.getmtime(CACHE_FILE) >= newest_src():
            try:
                with open(CACHE_FILE, 'rb') as f:
                    data = pickle.load(f)
                return data['ru'], data['en']
            except Exception:
                pass  # битый кеш — пересоберём

    ru = _parse_dic(RU_DIC, RU_LETTERS)
    en = _parse_dic(EN_DIC, EN_LETTERS)

    # подмешиваем технические термины и команды в английский словарь
    try:
        from tech_words import TECH_WORDS
        en |= {w.lower() for w in TECH_WORDS}
    except Exception:
        pass

    # ПРИМЕЧАНИЕ: пользовательские слова («Мой словарь») больше НЕ
    # подмешиваются сюда. Они хранятся в /etc/autoswitch/learned.txt в
    # формате «US-форма<TAB>ru|us» и применяются напрямую службой
    # autoswitch (learned перебивает словарь). Так одно правило может
    # закрепить и русское, и английское слово, а не только «считать
    # английским», как было в старом user_words.txt.

    with open(CACHE_FILE, 'wb') as f:
        pickle.dump({'ru': ru, 'en': en}, f, protocol=pickle.HIGHEST_PROTOCOL)

    return ru, en


if __name__ == '__main__':
    import time
    t0 = time.time()
    ru, en = load(rebuild='--rebuild' in os.sys.argv)
    dt = time.time() - t0
    print(f"RU слов: {len(ru):>7}")
    print(f"EN слов: {len(en):>7}")
    print(f"Загрузка: {dt:.2f} c")
    print(f"Кеш: {CACHE_FILE}")
    # быстрая проверка
    for w in ['привет', 'проверка', 'лучше', 'словарь', 'hello', 'world', 'check', 'perfect']:
        tag = 'RU' if w in ru else ('EN' if w in en else '—')
        print(f"  {w:<12} -> {tag}")
