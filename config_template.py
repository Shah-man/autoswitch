#!/usr/bin/env python3
"""Единый двуязычный шаблон конфига autoswitch.

Один источник правды для комментариев конфига на RU и EN. Используется:
  • installasc — создаёт конфиг при установке (на языке установки);
  • asc / asc-gui — перегенерируют конфиг при смене языка интерфейса,
    СОХРАНЯЯ значения, выставленные пользователем.

Порядок ключей и их дефолты заданы здесь же (SPEC). Комментарии — двуязычные
(COMMENTS[lang]). Функция render(lang, values) собирает готовый текст конфига:
берёт шаблон нужного языка и подставляет текущие значения ключей.

CLI:
  python3 config_template.py render <ru|en> [path-to-existing-config]
      -> печатает конфиг на выбранном языке, сохраняя значения из
         существующего файла (если путь дан и файл есть).
  python3 config_template.py get <path> <key>   -> печатает значение ключа
"""
import sys

# порядок ключей и значения по умолчанию
SPEC = [
    ("start_layout", "us"),
    ("min_word_length", "1"),
    ("show_indicator", "on"),
    ("fix_two_capitals", "on"),
    ("debug_log", "off"),
    ("log_max_size", "5"),
    ("terminal", ""),
    ("ui_lang", ""),
    ("exclude_apps", ""),
]

# Ключи ниже служба тоже понимает, но в файл их не пишем: это внутренняя
# механика движка (раннее переключение, учёт контекста, разбор коротких
# слов и т.п.). Их не настраивают — они просто должны работать, а отключение
# только ухудшит распознавание. Кому нужно, допишет строку вручную:
# keyboard, use_bigrams, short_words, use_context, early_switch,
# shift_switches_layout — при необходимости их можно дописать вручную.

# Комментарии перед каждым ключом (список строк без ведущего '#'),
# плюс спец-раздел 'learning' — текстовый блок без ключа.
COMMENTS = {
    "ru": {
        "_header": [
            "Настройки autoswitch. Формат: ключ=значение, строки с # — пояснения.",
            "Проще менять готовыми кнопками: asc → Настройки.",
        ],
        "start_layout": [
            "Раскладка клавиатуры при запуске службы: us или ru.",
        ],
        "keyboard": [
            "Устройство клавиатуры. auto — найти самому.",
            "Или явный путь: /dev/input/event0",
        ],
        "min_word_length": [
            "Слова короче этой длины не трогать. 1 — проверять все.",
            "Поставьте 3, если не нужно трогать «на», «is», «до».",
        ],
        "use_bigrams": [
            "Угадывать язык по сочетаниям букв, когда слова нет в словарях",
            "или оно есть в обоих (yes/no). no — решает только словарь.",
        ],
        "show_indicator": [
            "Значок в панели (on/off) — переключается в asc → Настройки.",
        ],
        "short_words": [
            "Учитывать список коротких слов: is, a, не, на (on/off).",
        ],
        "use_context": [
            "В спорных случаях смотреть на язык предыдущего слова (on/off).",
        ],
        "early_switch": [
            "Исправлять уже на 3-4 букве, не дожидаясь пробела (on/off).",
            "Решает по началу слова, ложных срабатываний не даёт.",
            "off — исправлять только по концу слова.",
        ],
        "shift_switches_layout": [
            "Одиночный Shift переключает раскладку (on/off).",
            "Заглавные буквы не страдают: Shift+буква работает как обычно.",
            "Двойной Shift отменяет последнее исправление.",
        ],
        "fix_two_capitals": [
            "Чинить залипший Shift: «ПРивет» → «Привет» (on/off).",
            "По умолчанию on. Исправляется, только если получившееся слово",
            "есть в словаре — поэтому сокращения вроде IDs и PCs остаются",
            "как набраны. Слова со смешанным регистром дальше («ПРиВет») не",
            "трогаются вовсе.",
        ],
        "debug_log": [
            "Подробный журнал для поиска проблем (on/off), по умолчанию off.",
            "При on записываются набранные слова. Пароли не записываются",
            "никогда: наборы от 6 знаков с цифрами или разным регистром",
            "программа не трогает и не логирует.",
        ],
        "log_max_size": [
            "Предел журнала в МБ, дальше старые записи отбрасываются. 0 — без",
            "предела. Журнал: ~/Applications/autoswitch.logs/autoswitch.log",
            "Очистить: asc → Журнал → Очистить.",
        ],
        "exclude_apps": [
            "Служебная строка: список приложений, где не вмешиваться.",
            "Вручную не правьте — он собирается мышью в",
            "asc → Настройки → Исключения приложений.",
        ],
        "ui_lang": [
            "Служебная строка: язык программы.",
            "Переключается в asc → Настройки → Язык.",
        ],
        "terminal": [
            "Чем открывать меню asc. Пусто — своим окном.",
            "Пример: terminal=alacritty  или  terminal=xfce4-terminal -x",
        ],
        "_learning": [
            "Как пользоваться",
            "",
            "Программа сама определяет язык слова и переключает раскладку.",
            "Одиночный Shift меняет раскладку вручную, как привычный хоткей.",
            "",
            "Если исправление оказалось лишним — верните слово двойным",
            "нажатием Shift или клавишей Pause. Программа запомнит его и",
            "больше не тронет. Список запомненного: asc → Мой словарь.",
            "",
            "Меню программы: команда asc в терминале или значок в панели.",
        ],
    },
    "en": {
        "_header": [
            "autoswitch settings. Format: key=value, lines with # are notes.",
            "Easier to change with the ready buttons: asc → Settings.",
        ],
        "start_layout": [
            "Keyboard layout the service starts with: us or ru.",
        ],
        "keyboard": [
            "Keyboard device. auto — detect it.",
            "Or an explicit path: /dev/input/event0",
        ],
        "min_word_length": [
            "Leave words shorter than this alone. 1 — check everything.",
            "Set 3 if 'on', 'is', 'не' should not be touched.",
        ],
        "use_bigrams": [
            "Guess the language by letter pairs when a word is in neither",
            "dictionary or in both (yes/no). no — dictionary only.",
        ],
        "show_indicator": [
            "Tray icon (on/off) — toggled in asc → Settings.",
        ],
        "short_words": [
            "Use the short-word list: is, a, не, на (on/off).",
        ],
        "use_context": [
            "In unclear cases look at the previous word's language (on/off).",
        ],
        "early_switch": [
            "Correct after 3-4 letters instead of waiting for a space (on/off).",
            "Decides by the word start, does not misfire.",
            "off — correct at the end of a word only.",
        ],
        "shift_switches_layout": [
            "A single Shift switches the layout (on/off).",
            "Capitals are safe: Shift+letter works as usual.",
            "Double Shift undoes the last correction.",
        ],
        "fix_two_capitals": [
            "Fix a stuck Shift: \"THis\" becomes \"This\" (on/off).",
            "On by default. A word is fixed only if the result is a real",
            "dictionary word, so abbreviations like IDs and PCs are left",
            "alone. Words with mixed case further on (\"THiS\") are never",
            "touched.",
        ],
        "debug_log": [
            "Verbose log for troubleshooting (on/off), off by default.",
            "With on, typed words are recorded. Passwords never are: inputs of",
            "6+ chars with digits or mixed case are neither touched nor logged.",
        ],
        "log_max_size": [
            "Log size limit in MB, older entries are then dropped. 0 — no limit.",
            "Log file: ~/Applications/autoswitch.logs/autoswitch.log",
            "Clear it: asc → Journal → Clear.",
        ],
        "exclude_apps": [
            "Service line: apps where the program stays out.",
            "Do not edit by hand — the list is built with the mouse in",
            "asc → Settings → App exclusions.",
        ],
        "ui_lang": [
            "Service line: program language.",
            "Switched in asc → Settings → Language.",
        ],
        "terminal": [
            "What opens the asc menu. Empty — its own window.",
            "Example: terminal=alacritty  or  terminal=xfce4-terminal -x",
        ],
        "_learning": [
            "How to use it",
            "",
            "The program detects the language of a word and switches the",
            "layout. A single Shift switches it by hand, like a usual hotkey.",
            "",
            "If a correction was wrong, put the word back with a double Shift",
            "or the Pause key. The program will remember it and leave it",
            "alone from then on. The list lives in asc → My dictionary.",
            "",
            "Program menu: the asc command in a terminal or the tray icon.",
        ],
    },
}


def parse_values(path):
    """Прочитать значения ключей из существующего конфига (key=value)."""
    vals = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                vals[k.strip()] = v.strip()
    except OSError:
        pass
    return vals


def render(lang, values=None):
    """Собрать текст конфига на языке lang, подставив значения values."""
    lang = lang if lang in COMMENTS else "en"
    c = COMMENTS[lang]
    values = values or {}
    out = []
    for line in c["_header"]:
        out.append("# " + line)
    out.append("")
    for key, default in SPEC:
        for line in c.get(key, []):
            out.append("# " + line)
        val = values.get(key, default)
        out.append(f"{key}={val}")
        out.append("")
    # блок про обучение — в конце, как заключение: он не про отдельный
    # ключ, а про то, как программа подстраивается под пользователя
    out.append("")
    for line in c["_learning"]:
        out.append("# " + line)
    # убрать хвостовые пустые строки, оставить одну
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) >= 2 and argv[1] == "render":
        lang = argv[2] if len(argv) > 2 else "en"
        vals = parse_values(argv[3]) if len(argv) > 3 else {}
        sys.stdout.write(render(lang, vals))
        return 0
    if len(argv) >= 4 and argv[1] == "get":
        vals = parse_values(argv[2])
        sys.stdout.write(vals.get(argv[3], ""))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
