#!/usr/bin/env python3
"""layout.py — всё, что связано с раскладкой клавиатуры и активным окном.

Этот модуль отвечает ТОЛЬКО за совместимость с графическими окружениями:
как узнать текущую раскладку, как её переключить и как узнать класс
активного окна. Логика определения языка (словари, обучение, разбор слов)
живёт в autoswitch.py и сюда не заглядывает.

Поддерживаются:
  • X11            — напрямую через libX11 (XkbLockGroup/XkbGetState);
  • GNOME Wayland  — через D-Bus мост расширения autoswitch;
  • KDE Plasma     — через org.kde.keyboard + KWin-скрипт;
  • sway           — через swaymsg;
  • Hyprland       — через hyprctl.

Служба работает от root, а IPC композитора живёт в сессии пользователя,
поэтому команды выполняются от имени графического пользователя (setpriv /
runuser — см. _user_prefix). Дорогие вызовы кэшируются.

Публичный интерфейс для ядра (всё остальное — внутреннее):
  active_window_class()  -> str   класс активного окна ('' если недоступно)
  read_real_layout()     -> 0|1|None  текущая раскладка (0=US, 1=RU)
  switch_layout(ui, target=None) -> bool  переключить раскладку
  session()              -> str   тип сессии ('wayland' | 'x11' | '')

КАРТА ФАЙЛА — код сгруппирован по окружениям, каждое цельным блоком:

  ОБЩЕЕ    сессия, шина пользователя, запуск команд от его имени
           (_resolve_user_bus, _user_prefix, _ensure_user_bus, _wl_run…)
  X11      libX11: _init_x11, _x11_get_group, _x11_set_group,
           _active_window_class_x11
  GNOME    D-Bus мост расширения: _bridge_get_layout, _bridge_set_layout,
           _active_window_class_wayland
  KDE      org.kde.keyboard + KWin-скрипт: _kde_get_layout, _kde_set_layout,
           _kde_focus_file, _active_window_class_kde
  sway     swaymsg: _sway_get_layout, _sway_set_layout, _active_window_class_sway
  Hyprland hyprctl: _hypr_get_layout, _hypr_set_layout, _active_window_class_hypr
  ПУБЛИЧНОЕ  active_window_class, read_real_layout, switch_layout, session
  ИНИЦИАЛИЗАЦИЯ  в самом конце: определение сессии и композитора при импорте

Чтобы добавить новое окружение, достаточно дописать свой блок по образцу
соседних и подключить его в _wl_layout_funcs() — ядро трогать не нужно.
"""
import os
import re
import glob
import json
import time
import subprocess

from evdev import ecodes as e


# Куда писать сообщения. По умолчанию — stdout, но служба подменяет это
# своей функцией (autoswitch.py: set_logger), чтобы всё уходило в общий
# файл журнала. Без этого сообщения модуля терялись: в юните stdout
# закрыт, а системный журнал мы не используем.
_log = print


def set_logger(fn):
    """Задать функцию вывода (служба передаёт свой log)."""
    global _log
    if callable(fn):
        _log = fn


_SESSION = os.environ.get('XDG_SESSION_TYPE', '').lower()

# X11-дисплей инициализируем лениво, после надёжного определения сессии
# (у root-службы XDG_SESSION_TYPE может отсутствовать).
_xdisplay = None

_USER_PREFIX_CACHE = None
_wl_resolve_last = 0.0
_WL_COMP = 'gnome'   # какой композитор Wayland (определяется при импорте)


# ========================================================================
# ОБЩЕЕ: сессия, шина пользователя, запуск команд от его имени
# ========================================================================

# Служба autoswitch обычно работает от root через systemd. Мост-расширение
# висит на session-шине графического пользователя. Root по --session попадает
# в свою (пустую) шину, поэтому надо явно указать адрес пользовательской шины
# и uid, иначе исключения на Wayland не работают.
def _resolve_user_bus():
    """Вернуть (env, uid_str, session_type) для вызова gdbus в шине графического
    пользователя. Под root унаследованный DBUS_SESSION_BUS_ADDRESS игнорируем."""
    env = os.environ.copy()
    stype = os.environ.get('XDG_SESSION_TYPE', '').lower()
    if os.getuid() != 0 and env.get('DBUS_SESSION_BUS_ADDRESS'):
        return env, str(os.getuid()), stype
    uid = None
    # 1) через loginctl — активная графическая сессия
    try:
        out = subprocess.run(['loginctl', 'list-sessions', '--no-legend'],
                             capture_output=True, text=True, timeout=1.0)
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                sid = parts[0]
                info = subprocess.run(
                    ['loginctl', 'show-session', sid,
                     '-p', 'Type', '-p', 'State', '-p', 'User'],
                    capture_output=True, text=True, timeout=1.0).stdout
                d = dict(l.split('=', 1) for l in info.splitlines() if '=' in l)
                if d.get('Type') in ('wayland', 'x11') and d.get('State') == 'active':
                    cand = d.get('User')
                    # только реальные пользователи (uid >= 1000); системные
                    # (messagebus и пр.) не годятся — у них нет расширения GNOME
                    if cand and cand.isdigit() and int(cand) >= 1000:
                        uid = cand
                        stype = d.get('Type', stype)
                        break
    except Exception:
        uid = None
    # 2) fallback: первый /run/user/<uid> с uid >= 1000 и живой шиной
    if not uid:
        try:
            for d in sorted(os.listdir('/run/user')):
                if d.isdigit() and int(d) >= 1000 and os.path.exists(f'/run/user/{d}/bus'):
                    uid = d
                    break
        except Exception:
            uid = None
    if uid:
        env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path=/run/user/{uid}/bus'
        env['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'
        return env, str(uid), stype
    return env, str(os.getuid()), stype


def _abs_tool(prog):
    """Абсолютный путь к утилите, устойчивый к урезанному PATH службы.
    Возвращает путь или None."""
    cand = [f'/usr/bin/{prog}', f'/bin/{prog}',
            f'/usr/sbin/{prog}', f'/sbin/{prog}']
    for d in os.environ.get('PATH', '').split(':'):
        if d:
            cand.append(os.path.join(d, prog))
    for p in cand:
        if os.path.exists(p):
            return p
    return None


def _user_prefix():
    """Префикс команды для запуска gdbus от имени графического пользователя.

    Служба работает от root. `sudo -u` требует, чтобы root был прописан в
    sudoers — на машинах без настроенного sudo (freebook: пользователь не в
    sudoers) это падало с «root NOT in sudoers», и мост к GNOME не работал.

    root может стать любым пользователем без sudoers через setpriv/runuser
    (util-linux, есть почти везде). Используем их по АБСОЛЮТНОМУ пути, т.к.
    у systemd-службы PATH урезан и утилиты из /sbin могут не находиться.
    Выбор логируем один раз, чтобы причина сбоя была видна сразу.
    """
    global _USER_PREFIX_CACHE
    if _USER_PREFIX_CACHE is not None:
        return list(_USER_PREFIX_CACHE)
    if os.getuid() != 0 or not _WL_UID.isdigit() or int(_WL_UID) < 1000:
        _USER_PREFIX_CACHE = []
        return []
    envargs = ['env',
               'DBUS_SESSION_BUS_ADDRESS=' + _WL_ENV.get(
                   'DBUS_SESSION_BUS_ADDRESS', ''),
               'XDG_RUNTIME_DIR=' + _WL_ENV.get(
                   'XDG_RUNTIME_DIR', f'/run/user/{_WL_UID}')]
    uname = _uid_to_name(_WL_UID)
    setpriv = _abs_tool('setpriv')
    runuser = _abs_tool('runuser')
    sudo = _abs_tool('sudo')
    if setpriv:
        pref = [setpriv, '--reuid', _WL_UID, '--regid', _WL_UID,
                '--init-groups'] + envargs
        how = f'setpriv ({setpriv})'
    elif runuser:
        pref = [runuser, '-u', uname, '--'] + envargs
        how = f'runuser ({runuser})'
    elif sudo:
        pref = [sudo, '-n', '-u', '#' + _WL_UID] + envargs
        how = f'sudo ({sudo}) — требует sudoers!'
    else:
        pref = envargs  # последний шанс: без смены uid
        how = 'НЕТ инструмента смены uid — мост, скорее всего, не заработает'
    _log(f"[autoswitch] user-prefix: {how} for uid={_WL_UID} ({uname})")
    _USER_PREFIX_CACHE = pref
    return list(pref)


def _which(prog):
    for d in os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin').split(':'):
        if d and os.path.exists(os.path.join(d, prog)):
            return True
    return os.path.exists('/usr/sbin/' + prog) or os.path.exists('/sbin/' + prog)


def _uid_to_name(uid):
    try:
        import pwd
        return pwd.getpwuid(int(uid)).pw_name
    except Exception:
        return '#' + str(uid)


def _detect_wl_compositor():
    """Вернуть 'gnome' | 'sway' | 'hyprland' | 'kde'."""
    desktop = (os.environ.get('XDG_CURRENT_DESKTOP') or '').lower()
    if os.getuid() != 0 and desktop:
        if 'sway' in desktop:
            return 'sway'
        if 'hyprland' in desktop:
            return 'hyprland'
        if 'kde' in desktop or 'plasma' in desktop:
            return 'kde'
        if 'gnome' in desktop:
            return 'gnome'
    # под root — по процессам
    for proc, name in (('sway', 'sway'),
                       ('Hyprland', 'hyprland'),
                       ('kwin_wayland', 'kde'),
                       ('gnome-shell', 'gnome')):
        try:
            r = subprocess.run(['pgrep', '-x', proc],
                               capture_output=True, timeout=1.0)
            if r.returncode == 0:
                return name
        except Exception:
            continue
    return 'gnome'


def _refresh_session(stype):
    """Уточнить тип сессии и композитор после появления пользователя."""
    global _SESSION, _WL_COMP
    changed = False
    if stype and stype != _SESSION:
        _SESSION = stype
        changed = True
        if _SESSION != 'wayland':
            _init_x11()
    if _SESSION == 'wayland':
        comp = _detect_wl_compositor()
        if comp != _WL_COMP:
            _WL_COMP = comp
            changed = True
    if changed:
        _log(f"[autoswitch] session={_SESSION or '?'}"
              + (f" compositor={_WL_COMP}" if _SESSION == 'wayland' else ''))


def _ensure_user_bus():
    """Повторно определить шину пользователя, если при старте её не было
    или нашёлся системный пользователь (uid < 1000). Пробует раз в 3 сек."""
    global _WL_ENV, _WL_UID, _wl_resolve_last
    if _WL_UID and _WL_UID.isdigit() and int(_WL_UID) >= 1000:
        return  # уже нашли реального пользователя
    now = time.time()
    if now - _wl_resolve_last < 3.0:
        return
    _wl_resolve_last = now
    env, uid, stype = _resolve_user_bus()
    if uid and uid.isdigit() and int(uid) >= 1000:
        _WL_ENV, _WL_UID = env, uid
        _log(f"[autoswitch] user bus ready: uid={uid}")
        # Сессия и композитор определялись при старте службы. Если она
        # поднялась раньше входа пользователя, там были пустые значения,
        # и без уточнения движок навсегда остался бы в режиме X11/GNOME.
        _refresh_session(stype)


def _wl_run(cmd, extra_env=None, timeout=0.7):
    """Выполнить команду в сессии графического пользователя."""
    # служба может стартовать раньше графической сессии — тогда при запуске
    # uid не определился; пробуем ещё раз, как это делает путь GNOME
    _ensure_user_bus()
    extra_env = extra_env or {}
    env = dict(_WL_ENV)
    env.update(extra_env)
    full = list(cmd)
    if os.getuid() == 0 and _WL_UID.isdigit() and int(_WL_UID) >= 1000:
        envargs = ['env'] + [f'{k}={v}' for k, v in extra_env.items()]
        envargs.append('XDG_RUNTIME_DIR=' + env.get(
            'XDG_RUNTIME_DIR', f'/run/user/{_WL_UID}'))
        setpriv = _abs_tool('setpriv')
        runuser = _abs_tool('runuser')
        sudo = _abs_tool('sudo')
        if setpriv:
            full = [setpriv, '--reuid', _WL_UID, '--regid', _WL_UID,
                    '--init-groups'] + envargs + full
        elif runuser:
            full = [runuser, '-u', _uid_to_name(_WL_UID), '--'] + envargs + full
        elif sudo:
            full = [sudo, '-n', '-u', '#' + _WL_UID] + envargs + full
    return subprocess.run(full, capture_output=True, text=True,
                          timeout=timeout, env=env)


# ========================================================================
# X11 — напрямую через libX11 (XkbLockGroup / XkbGetState)
# ========================================================================

_x11_next_try = 0.0


def _init_x11(force=False):
    """Подключение к X-серверу для чтения активного окна.

    Раньше подключались ровно один раз за всю жизнь процесса. Служба
    системная и переживает выход из сессии, а X-сервер при новом входе
    поднимается другой: соединение умирало, класс окна переставал
    читаться — и движок молча прекращал работать. Пустой класс он
    трактует как «не знаю, не терминал ли это» и не трогает ни одного
    слова. Со стороны это выглядело как «после перезахода не переключает»,
    а `systemctl restart` чинил ровно потому, что соединение создавалось
    заново.
    """
    global _xdisplay, _x11_next_try
    if force:
        try:
            if _xdisplay is not None:
                _xdisplay.close()
        except Exception:
            pass
        _xdisplay = None
    if _xdisplay is not None:
        return
    now = time.time()
    if now < _x11_next_try:
        return
    _x11_next_try = now + 3.0   # не долбить сервер чаще раза в 3 сек
    try:
        from Xlib import display as _xlib_display
        _xdisplay = _xlib_display.Display()
    except Exception:
        _xdisplay = None


def _active_window_class_x11():
    _init_x11()
    if _xdisplay is None:
        return ''
    try:
        win = _xdisplay.get_input_focus().focus
        # поднимаемся к окну верхнего уровня, у которого есть WM_CLASS
        for _ in range(4):
            if win is None:
                break
            cls = win.get_wm_class()
            if cls:
                return (cls[1] or cls[0] or '').lower()
            win = win.query_tree().parent
    except Exception:
        # Соединение с X умерло (сменилась сессия) — роняем его, чтобы
        # следующий вызов подключился заново.
        _init_x11(force=True)
        return ''
    return ''


# --- прямое переключение раскладки на X11 через libX11 (без внешних утилит) ---
# XkbLockGroup ставит группу раскладки напрямую в X-сервере — тот же механизм,
# что у xkb-switch, но через системную libX11, которая есть на любой X11-машине.
# Не зависит от системного хоткея (Ctrl+Shift/Alt+Shift/…), работает в любом DE.
_XKB = {'lib': None, 'dpy': None, 'next_try': 0.0}
_XKB_USE_CORE_KBD = 0x0100
import ctypes as _ct  # noqa: E402  (нужен для типа обработчика)
_XERR_HANDLER = _ct.CFUNCTYPE(_ct.c_int, _ct.c_void_p, _ct.c_void_p)
_XERR_KEEP = None      # ссылку держим, иначе обработчик соберёт сборщик


def _xkb_alive():
    """Живо ли соединение с X-сервером.

    Проверяем сам сокет, а не вызовом Xlib: у мёртвого соединения любой
    вызов libX11 уходит в её собственный обработчик ошибки ввода-вывода,
    а тот завершает процесс без предупреждения.
    """
    try:
        import select
        fd = _XKB['lib'].XConnectionNumber(_XKB['dpy'])
        if fd < 0:
            return False
        poller = select.poll()
        poller.register(fd, select.POLLHUP | select.POLLERR)
        return not poller.poll(0)
    except Exception:
        return False


def _xkb_init():
    """Подключиться к X-серверу, переоткрывая соединение при нужде.

    Раньше попытка была ровно одна за всю жизнь процесса: удалась —
    хорошо, нет — переключение раскладки мертво навсегда. Служба
    системная и переживает выход из сессии, а X-сервер при новом входе
    поднимается другой: старое соединение умирает, и раскладка
    переставала переключаться до перезапуска службы. Отсюда «после
    перезахода не работает, после systemctl restart работает».
    """
    if _XKB['lib'] is not None and _XKB['dpy']:
        if _xkb_alive():
            return True
        # Соединение мертво. Ссылки просто отпускаем: XCloseDisplay на
        # таком соединении сам уводит процесс в обработчик ошибки.
        _XKB['lib'] = None
        _XKB['dpy'] = None

    now = time.time()
    if now < _XKB['next_try']:
        return False
    _XKB['next_try'] = now + 3.0   # не долбить сервер чаще раза в 3 сек
    try:
        import ctypes
        lib = ctypes.CDLL('libX11.so.6')

        # Типы обязательны. По умолчанию ctypes считает, что функция
        # возвращает int, то есть 32 бита. XOpenDisplay отдаёт указатель, и
        # на 64-битной системе он молча обрезается: старшие байты теряются.
        # Дальше этот огрызок уходит в XkbGetState, libX11 разыменовывает
        # несуществующий адрес — и служба падает с SIGSEGV.
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XkbLockGroup.restype = ctypes.c_int
        lib.XkbLockGroup.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_uint]
        lib.XkbGetState.restype = ctypes.c_int
        lib.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p]
        lib.XFlush.restype = ctypes.c_int
        lib.XFlush.argtypes = [ctypes.c_void_p]
        lib.XConnectionNumber.restype = ctypes.c_int
        lib.XConnectionNumber.argtypes = [ctypes.c_void_p]

        # Свой обработчик ошибок протокола. По умолчанию libX11 при
        # любой ошибке печатает её и ЗАВЕРШАЕТ ПРОЦЕСС — то есть службу
        # целиком. Ошибку может вызвать что угодно: исчезнувшее окно,
        # смена сессии, обращение к устройству, которого уже нет.
        # Гасим их и продолжаем работать.
        global _XERR_KEEP
        _XERR_KEEP = _XERR_HANDLER(lambda _d, _e: 0)
        lib.XSetErrorHandler.restype = ctypes.c_void_p
        lib.XSetErrorHandler.argtypes = [_XERR_HANDLER]
        lib.XSetErrorHandler(_XERR_KEEP)

        dpy = lib.XOpenDisplay(None)
        if not dpy:
            return False
        _XKB['lib'] = lib
        _XKB['dpy'] = dpy
        return True
    except Exception:
        return False


def _x11_set_group(group):
    """Поставить группу раскладки (0=us, 1=ru) напрямую через X-сервер.

    Ставим мастер-клавиатуре. Была мысль проходить заодно по всем
    устройствам ввода (в XInput2 группа своя у каждого), но она не
    подтвердилась, а вреда от неё больше: у человека вторая клавиатура
    может быть намеренно в другой раскладке, и мы бы ей мешали.
    """
    if not _xkb_init():
        return False
    try:
        _XKB['lib'].XkbLockGroup(_XKB['dpy'], _XKB_USE_CORE_KBD, int(group))
        _XKB['lib'].XFlush(_XKB['dpy'])
        return True
    except Exception:
        return False


def _x11_get_group():
    """Текущая группа раскладки в X-сервере, или None."""
    if not _xkb_init():
        return None
    try:
        import ctypes

        class XkbStateRec(ctypes.Structure):
            _fields_ = [('group', ctypes.c_ubyte),
                        ('locked_group', ctypes.c_ubyte),
                        ('base_group', ctypes.c_ushort),
                        ('latched_group', ctypes.c_ushort),
                        ('mods', ctypes.c_ubyte),
                        ('base_mods', ctypes.c_ubyte),
                        ('latched_mods', ctypes.c_ubyte),
                        ('locked_mods', ctypes.c_ubyte),
                        ('compat_state', ctypes.c_ubyte),
                        ('grab_mods', ctypes.c_ubyte),
                        ('compat_grab_mods', ctypes.c_ubyte),
                        ('lookup_mods', ctypes.c_ubyte),
                        ('compat_lookup_mods', ctypes.c_ubyte),
                        ('ptr_buttons', ctypes.c_ushort)]

        st = XkbStateRec()
        _XKB['lib'].XkbGetState(_XKB['dpy'], _XKB_USE_CORE_KBD, ctypes.byref(st))
        return int(st.group)
    except Exception:
        return None


# ========================================================================
# GNOME Wayland — через D-Bus мост расширения autoswitch
# ========================================================================

_WL_BRIDGE_OBJ = '/org/gnome/Shell/Extensions/AutoswitchFocus'
_WL_BRIDGE_METHOD = 'org.gnome.Shell.Extensions.AutoswitchFocus.GetFocusedClass'
_wl_cache = {'val': '', 't': 0.0}
# GNOME Shell регистрируется на шине не мгновенно: сразу после логина мост
# может быть ещё недоступен. Первые секунды об этом не сообщаем, иначе в
# журнале каждый раз висит пугающая ошибка, которая сама проходит.
# Сколько молчать о сбоях моста, если он НИ РАЗУ не отвечал.
#
# Служба стартует раньше входа в систему — намеренно: на экране входа
# человек печатает пароль, и клавиатура должна быть захвачена заранее.
# Но оболочки в этот момент ещё нет, и мост честно не отвечает. Первое
# нажатие в такой день — тот самый пароль, и приходит оно через минуту с
# лишним после старта. Прежних 45 секунд не хватало, и при каждом
# включении компьютера в журнал попадала ложная тревога.
#
# Пять минут — порог «это точно не загрузка». Как только мост ответил
# хоть раз, срок не важен: о любом последующем сбое сообщаем сразу.
_WL_QUIET_START = 300.0
_WL_T0 = time.time()
_WL_CACHE_TTL = 2.0  # сек; окно меняется редко, а gdbus дорогой

def _active_window_class_wayland():
    now = time.time()
    if now - _wl_cache['t'] < _WL_CACHE_TTL:
        return _wl_cache['val']
    _ensure_user_bus()
    val = ''
    try:
        cmd = ['gdbus', 'call', '--session',
               '-d', 'org.gnome.Shell',
               '-o', _WL_BRIDGE_OBJ,
               '-m', _WL_BRIDGE_METHOD]
        # root и целевая шина чужая → выполнить от имени пользователя,
        # чтобы peer-to-peer сокет шины принял подключение по uid.
        pref = _user_prefix()
        if pref:
            cmd = pref + cmd
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=0.7, env=_WL_ENV,
        )
        if out.returncode == 0:
            # формат ответа gdbus: ('google-chrome',)
            s = out.stdout.strip()
            if "'" in s:
                val = s.split("'")[1].lower()
            _wl_cache['warned'] = False
            # мост ответил — рабочий стол поднялся; дальше любой сбой
            # уже настоящий, и молчать о нём незачем
            _wl_cache['ever_ok'] = True
        elif not _wl_cache.get('warned') and \
                (_wl_cache.get('ever_ok')
                 or time.time() - _WL_T0 > _WL_QUIET_START):
            _wl_cache['warned'] = True
            _log(f"[autoswitch] bridge FAILED rc={out.returncode} "
                  f"uid={_WL_UID} bus={_WL_ENV.get('DBUS_SESSION_BUS_ADDRESS','')} "
                  f"err={out.stderr.strip()[:120]}")
    except Exception as ex:
        val = ''
        if not _wl_cache.get('warned'):
            _wl_cache['warned'] = True
            _log(f"[autoswitch] bridge EXC uid={_WL_UID}: {ex}")
    # При сбое моста НЕ затираем последнее известное окно пустотой: иначе
    # две секунды подряд движок считает, что окна нет, и не понимает, что
    # это исключённый терминал — буквы оттуда прилипали к следующему слову.
    if val:
        _wl_cache['val'] = val
        _wl_cache['t'] = now
        return val
    return _wl_cache.get('val', '')


def _bridge_set_layout(index):
    """Переключить раскладку через расширение GNOME (Wayland).
    Возвращает True при успехе. Зовётся только в момент перебивки."""
    _ensure_user_bus()
    try:
        cmd = ['gdbus', 'call', '--session',
               '-d', 'org.gnome.Shell',
               '-o', _WL_BRIDGE_OBJ,
               '-m', 'org.gnome.Shell.Extensions.AutoswitchFocus.SetLayout',
               str(index)]
        pref = _user_prefix()
        if pref:
            cmd = pref + cmd
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=1.0, env=_WL_ENV)
        return out.returncode == 0 and 'true' in out.stdout.lower()
    except Exception:
        return False


def _bridge_get_layout():
    """Текущий индекс раскладки по данным GNOME, или None."""
    _ensure_user_bus()
    try:
        cmd = ['gdbus', 'call', '--session',
               '-d', 'org.gnome.Shell',
               '-o', _WL_BRIDGE_OBJ,
               '-m', 'org.gnome.Shell.Extensions.AutoswitchFocus.GetLayout']
        pref = _user_prefix()
        if pref:
            cmd = pref + cmd
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=1.0, env=_WL_ENV)
        if out.returncode == 0:
            # ответ вида "(uint32 1,)" — берём число ПОСЛЕ 'uint32',
            # иначе "32" из типа попадёт в результат
            s = out.stdout.strip()
            m = re.search(r'uint32\s+(\d+)', s)
            if not m:
                m = re.search(r'\(\s*(\d+)\s*,', s)
            if m:
                val = int(m.group(1))
                if 0 <= val <= 8:      # разумный предел числа раскладок
                    return val
    except Exception:
        pass
    return None


# ========================================================================
# KDE Plasma — org.kde.keyboard + KWin-скрипт
# ========================================================================

_KDE_SVC = 'org.kde.keyboard'
_KDE_OBJ = '/Layouts'
_KDE_IFACE = 'org.kde.KeyboardLayouts'
_kde_focus_warned = {'v': False}

def _kde_gdbus(method, *args):
    _ensure_user_bus()
    cmd = ['gdbus', 'call', '--session',
           '-d', _KDE_SVC, '-o', _KDE_OBJ,
           '-m', f'{_KDE_IFACE}.{method}'] + list(args)
    pref = _user_prefix()
    if pref:
        cmd = pref + cmd
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=1.0, env=_WL_ENV)


def _parse_uint(text):
    """Число из ответа gdbus вида "(uint32 1,)"."""
    m = re.search(r'uint32\s+(\d+)', text)
    if not m:
        m = re.search(r'\(\s*(\d+)\s*,', text)
    if m:
        val = int(m.group(1))
        if 0 <= val <= 8:
            return val
    return None


def _kde_get_layout():
    try:
        out = _kde_gdbus('getLayout')
        if out.returncode == 0:
            return _parse_uint(out.stdout.strip())
    except Exception:
        pass
    return None


def _kde_set_layout(index):
    try:
        out = _kde_gdbus('setLayout', f'uint32 {int(index)}')
        return out.returncode == 0
    except Exception:
        return False


def _kde_focus_file():
    uid = _WL_UID if _WL_UID.isdigit() else str(os.getuid())
    return f'/run/user/{uid}/autoswitch-focus'


def _active_window_class_kde():
    """Класс активного окна на Plasma Wayland — из файла приёмника."""
    _ensure_user_bus()
    try:
        with open(_kde_focus_file(), encoding='utf-8') as fh:
            return fh.read().strip().lower()
    except FileNotFoundError:
        if not _kde_focus_warned['v']:
            _kde_focus_warned['v'] = True
            # Диагностика этого модуля — всегда по-английски: он про
            # совместимость с окружением, и такие строки одинаково читают
            # и в отчётах пользователей, и в issue на GitHub.
            _log('[autoswitch] KDE: no file with the active window class — '
                 'app exclusions will not work. Check the '
                 'autoswitch-focus-bridge service and the autoswitch '
                 'KWin script.')
    except Exception as ex:
        if not _kde_focus_warned['v']:
            _kde_focus_warned['v'] = True
            _log(f'[autoswitch] KDE focus file EXC: {ex}')
    return ''


# ========================================================================
# sway — через swaymsg
# ========================================================================

def _sway_sock():
    """Путь к IPC-сокету sway. Под root переменной SWAYSOCK нет."""
    envsock = os.environ.get('SWAYSOCK')
    if envsock and os.path.exists(envsock):
        return envsock
    try:
        found = glob.glob(f'/run/user/{_WL_UID}/sway-ipc.*.sock')
        if found:
            # самый свежий — на случай прошлых незакрытых сессий
            return max(found, key=os.path.getmtime)
    except Exception:
        pass
    return ''


def _sway_env():
    sock = _sway_sock()
    return {'SWAYSOCK': sock} if sock else {}


def _parse_sway_focused(tree):
    """Найти класс сфокусированного окна в дереве sway.

    app_id — у нативных wayland-окон, window_properties.class — у xwayland.
    Функция чистая: дерево уже разобрано из JSON.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get('focused') is True:
            app_id = node.get('app_id')
            if app_id:
                return str(app_id).lower()
            props = node.get('window_properties') or {}
            cls = props.get('class') or props.get('instance')
            if cls:
                return str(cls).lower()
            return ''
        for key in ('nodes', 'floating_nodes'):
            children = node.get(key)
            if isinstance(children, list):
                stack.extend(children)
    return ''


def _parse_sway_layout(inputs):
    """Индекс активной раскладки из ответа `swaymsg -t get_inputs`."""
    if not isinstance(inputs, list):
        return None
    for dev in inputs:
        if not isinstance(dev, dict):
            continue
        if dev.get('type') != 'keyboard':
            continue
        idx = dev.get('xkb_active_layout_index')
        if isinstance(idx, int):
            return idx
    return None


def _active_window_class_sway():
    try:
        out = _wl_run(['swaymsg', '-t', 'get_tree', '-r'], _sway_env())
        if out.returncode != 0:
            return ''
        return _parse_sway_focused(json.loads(out.stdout))
    except Exception:
        return ''


def _sway_get_layout():
    try:
        out = _wl_run(['swaymsg', '-t', 'get_inputs', '-r'], _sway_env())
        if out.returncode != 0:
            return None
        return _parse_sway_layout(json.loads(out.stdout))
    except Exception:
        return None


def _sway_set_layout(index):
    try:
        out = _wl_run(['swaymsg', 'input', 'type:keyboard',
                       'xkb_switch_layout', str(int(index))],
                      _sway_env(), timeout=1.0)
        return out.returncode == 0
    except Exception:
        return False


# ========================================================================
# Hyprland — через hyprctl
# ========================================================================

def _hypr_sig():
    """Сигнатура запущенного экземпляра Hyprland."""
    sig = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE')
    if sig:
        return sig
    for base in (f'/run/user/{_WL_UID}/hypr', '/tmp/hypr'):
        try:
            subdirs = [d for d in glob.glob(base + '/*') if os.path.isdir(d)]
            if subdirs:
                return os.path.basename(
                    max(subdirs, key=os.path.getmtime))
        except Exception:
            continue
    return ''


def _hypr_env():
    sig = _hypr_sig()
    return {'HYPRLAND_INSTANCE_SIGNATURE': sig} if sig else {}


def _parse_hypr_class(win):
    """Класс активного окна из ответа `hyprctl -j activewindow`."""
    if not isinstance(win, dict):
        return ''
    cls = win.get('initialClass') or win.get('class') or ''
    return str(cls).lower()


def _parse_hypr_keyboard(devices):
    """Вернуть (имя_клавиатуры, индекс_раскладки) из `hyprctl -j devices`.

    Hyprland отдаёт не индекс, а имя активной раскладки ('Russian',
    'English (US)'). Autoswitch работает с парой us/ru, поэтому индекс
    выводим по признаку русской раскладки в названии.
    """
    if not isinstance(devices, dict):
        return ('', None)
    kbs = devices.get('keyboards')
    if not isinstance(kbs, list) or not kbs:
        return ('', None)
    chosen = None
    for kb in kbs:
        if isinstance(kb, dict) and kb.get('main') is True:
            chosen = kb
            break
    if chosen is None:
        chosen = kbs[0] if isinstance(kbs[0], dict) else None
    if chosen is None:
        return ('', None)
    name = str(chosen.get('name') or '')
    keymap = str(chosen.get('active_keymap') or '').lower()
    if not keymap:
        return (name, None)
    is_ru = ('russ' in keymap) or ('рус' in keymap)
    return (name, 1 if is_ru else 0)


def _active_window_class_hypr():
    try:
        out = _wl_run(['hyprctl', '-j', 'activewindow'], _hypr_env())
        if out.returncode != 0:
            return ''
        return _parse_hypr_class(json.loads(out.stdout))
    except Exception:
        return ''


def _hypr_keyboard():
    try:
        out = _wl_run(['hyprctl', '-j', 'devices'], _hypr_env())
        if out.returncode != 0:
            return ('', None)
        return _parse_hypr_keyboard(json.loads(out.stdout))
    except Exception:
        return ('', None)


def _hypr_get_layout():
    return _hypr_keyboard()[1]


def _hypr_set_layout(index):
    name, _ = _hypr_keyboard()
    if not name:
        return False
    try:
        out = _wl_run(['hyprctl', 'switchxkblayout', name, str(int(index))],
                      _hypr_env(), timeout=1.0)
        return out.returncode == 0
    except Exception:
        return False


# ========================================================================
# ПУБЛИЧНЫЙ ИНТЕРФЕЙС для ядра
# ========================================================================

_layout_cache = {'val': None, 'time': 0.0}
LAYOUT_CACHE_TTL = 0.4   # сек: как долго верим прочитанной раскладке

def active_window_class():
    """Вернуть WM_CLASS активного окна в нижнем регистре или '' если недоступно.

    X11 — через xlib. Wayland — через IPC своего композитора (с кешем):
    GNOME — D-Bus мост расширения, sway/Hyprland — штатный CLI,
    KDE — приёмник KWin-скрипта.
    """
    # Пока пользователь не найден, тип сессии может быть определён неверно:
    # служба стартует раньше входа и видит пустой XDG_SESSION_TYPE, то есть
    # уходит в ветку X11 и там остаётся. Вызов дешёвый — внутри стоит
    # ограничение по времени, а после нахождения uid он сразу выходит.
    _ensure_user_bus()

    if _SESSION != 'wayland':
        return _active_window_class_x11()
    if _WL_COMP == 'gnome':
        return _active_window_class_wayland()
    # общий кеш: опрос композитора дороже, чем чтение переменной
    now = time.time()
    if now - _wl_cache['t'] < _WL_CACHE_TTL:
        return _wl_cache['val']
    if _WL_COMP == 'sway':
        val = _active_window_class_sway()
    elif _WL_COMP == 'hyprland':
        val = _active_window_class_hypr()
    elif _WL_COMP == 'kde':
        val = _active_window_class_kde()
    else:
        val = ''
    # При сбое моста НЕ затираем последнее известное окно пустотой: иначе
    # две секунды подряд движок считает, что окна нет, и не понимает, что
    # это исключённый терминал — буквы оттуда прилипали к следующему слову.
    if val:
        _wl_cache['val'] = val
        _wl_cache['t'] = now
        return val
    return _wl_cache.get('val', '')


def read_real_layout():
    """Реальная раскладка системы (0=US, 1=RU) или None.

    На GNOME/Wayland чтение идёт через gdbus-мост — это запуск процесса,
    десятки миллисекунд. Дёргать его на каждой букве нельзя: ввод начнёт
    заметно подтормаживать. Поэтому результат кэшируется на короткое время
    (LAYOUT_CACHE_TTL): пользователь не меняет раскладку по нескольку раз в
    секунду, а лага не будет.
    """
    now = time.time()
    if (_layout_cache['val'] is not None
            and now - _layout_cache['time'] < LAYOUT_CACHE_TTL):
        return _layout_cache['val']
    try:
        if _SESSION == 'wayland':
            val = _wl_layout_funcs()[0]()
        else:
            val = _x11_get_group()
    except Exception:
        val = None
    if val in (0, 1):
        _layout_cache['val'] = val
        _layout_cache['time'] = now
        return val
    return None


def _wl_layout_funcs():
    """Пара (получить, установить) раскладку для текущего композитора."""
    if _WL_COMP == 'sway':
        return _sway_get_layout, _sway_set_layout
    if _WL_COMP == 'hyprland':
        return _hypr_get_layout, _hypr_set_layout
    if _WL_COMP == 'kde':
        return _kde_get_layout, _kde_set_layout
    return _bridge_get_layout, _bridge_set_layout


def _wait_layout_applied(getter, target, limit=0.35):
    """Дождаться, пока композитор действительно применит раскладку.

    Раньше здесь стояла глухая пауза перед печатью: на GNOME мост
    синхронный и хватало 50 мс, а на KDE вызов идёт через sudo и gdbus и в
    виртуальной машине занимает заметно больше. Первые буквы успевали
    напечататься в старой раскладке, потом раскладка применялась — и слово
    на глазах перерисовывалось, будто печатается дважды.

    Поэтому не ждём вслепую, а спрашиваем текущую раскладку, пока она не
    станет нужной. Обычно ответ приходит с первого раза, и задержки нет
    вовсе. limit — потолок ожидания: если композитор молчит, печатаем как
    есть, лучше кривые буквы, чем зависшая служба.
    """
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            if getter() == target:
                return True
        except Exception:
            return False
        time.sleep(0.01)
    return False


def switch_layout(ui, target=None):
    """Переключить раскладку.
    Wayland — через IPC своего композитора (GNOME — расширение,
    sway/Hyprland — CLI, KDE — D-Bus). X11 — напрямую через libX11.
    Ни там, ни там НЕ эмулируем системный хоткей — ставим группу явно.
    target: 0 = US, 1 = RU; None = переключить на другую."""
    # мы сами меняем раскладку — прежнее кэшированное значение неверно
    _layout_cache['val'] = None
    if _SESSION == 'wayland':
        getter, setter = _wl_layout_funcs()
        if target is None:
            cur = getter()
            target = 0 if cur else 1
        if setter(target):
            _wait_layout_applied(getter, target)
            return True
        # запасной путь на Wayland — эмуляция хоткея
    else:
        # X11: прямая установка группы через libX11
        if target is None:
            cur = _x11_get_group()
            target = 0 if cur else 1
        if _x11_set_group(target):
            # Проверяем РЕЗУЛЬТАТ, а не факт вызова. XkbLockGroup на
            # умершем соединении возвращает успех и ничего не меняет:
            # движок считал, что раскладка сменилась, стирал слово и
            # печатал те же коды заново — на экране выходил тот же текст,
            # а в журнале бодрое «ok». Читаем группу обратно и, если она
            # не та, роняем соединение и пробуем ещё раз на свежем.
            if _x11_get_group() == int(target):
                return True
            _log('[autoswitch] x11: группа не сменилась, '
                 'переоткрываю соединение')
            _XKB['lib'] = None
            _XKB['dpy'] = None
            _XKB['next_try'] = 0.0
            if _x11_set_group(target) and _x11_get_group() == int(target):
                return True
            _log('[autoswitch] x11: сменить раскладку не удалось')
        # если libX11 недоступна — падаем на эмуляцию хоткея ниже
    for k in SWITCH_KEYS:
        ui.write(e.EV_KEY, k, 1)
    ui.syn()
    for k in reversed(SWITCH_KEYS):
        ui.write(e.EV_KEY, k, 0)
    ui.syn()
    return False


def session():
    """Тип текущей сессии: 'wayland', 'x11' или '' если не определён.

    Ядру нужен только для пары решений; всё остальное про совместимость
    остаётся внутри этого модуля.
    """
    return _SESSION


# ========================================================================
# ИНИЦИАЛИЗАЦИЯ при импорте (после всех определений)
# ========================================================================

_WL_ENV, _WL_UID, _RESOLVED_TYPE = _resolve_user_bus()

if not _SESSION:
    _SESSION = _RESOLVED_TYPE
if _SESSION != 'wayland':
    _init_x11()

if _SESSION == 'wayland':
    _WL_COMP = _detect_wl_compositor()

def session_summary():
    """Строка о том, как определилась сессия — для журнала.

    Раньше печаталась прямо при импорте, но тогда файл журнала ещё не
    открыт, и сообщение пропадало. Теперь его выводит служба, когда
    логирование готово.
    """
    return (f"[autoswitch] session={_SESSION or '?'} uid={_WL_UID}"
            + (f" compositor={_WL_COMP}" if _SESSION == 'wayland' else ''))
