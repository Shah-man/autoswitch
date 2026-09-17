import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';

import * as Keyboard from 'resource:///org/gnome/shell/ui/status/keyboard.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `
<node>
  <interface name="org.gnome.Shell.Extensions.AutoswitchFocus">
    <method name="GetFocusedClass">
      <arg type="s" direction="out" name="wm_class"/>
    </method>
    <method name="GetFocusedInfo">
      <arg type="s" direction="out" name="wm_class"/>
      <arg type="s" direction="out" name="title"/>
    </method>
    <method name="GetLayout">
      <arg type="u" direction="out" name="index"/>
    </method>
    <method name="SetLayout">
      <arg type="u" direction="in" name="index"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <method name="GetLayoutCount">
      <arg type="u" direction="out" name="count"/>
    </method>
    <method name="CloseWindowByTitle">
      <arg type="s" direction="in" name="title"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
  </interface>
</node>`;

const SERVICE = 'autoswitch.service';

// Конфиг движка. Флаг show_indicator ставится через `asc` и общий для всех
// оболочек: его же читают GTK-индикатор и апплет Plasma.
const CONFIG_PATH = '/etc/autoswitch/config';
// Файл-маячок: asc/asc-gui трогают его после действий со службой. Мы —
// отдельная программа в оболочке, напрямую нас не позвать, зато следить за
// файлом умеем. Изменился — значит службу перезапустили, показываем это.
const BEACON_PATH = GLib.build_filenamev(
    [GLib.get_user_runtime_dir(), 'autoswitch.beacon']);

// Цвета по умолчанию (используются, если настройки недоступны).
// Реальные значения берутся из GSettings и настраиваются пользователем.

// ── язык интерфейса: общий с asc/asc-gui через ui_lang в конфиге ──────
// Читаем тот же ключ, что и остальные части проекта, иначе смена языка
// в меню Autoswitch не доходила бы до панели.
const T = {
    ru: {
        status: 'Статус',
        start: 'Запустить',
        stop: 'Остановить',
        restart: 'Перезапустить',
        prefs: 'Расширение',
        settings: 'Настройки',
        running: 'работает',
        stopped: 'остановлен',
        error: 'ошибка',
        failed: 'Не удалось выполнить: ',
        failed_asc: 'Не удалось открыть asc',
    },
    en: {
        status: 'Status',
        start: 'Start',
        stop: 'Stop',
        restart: 'Restart',
        prefs: 'Extension',
        settings: 'Settings',
        running: 'running',
        stopped: 'stopped',
        error: 'error',
        failed: 'Action failed: ',
        failed_asc: 'Could not open asc',
    },
};

function uiLang() {
    try {
        const [ok, bytes] = GLib.file_get_contents(CONFIG_PATH);
        if (ok) {
            const text = new TextDecoder().decode(bytes);
            for (const line of text.split('\n')) {
                const m = line.match(/^\s*ui_lang\s*=\s*(\S+)/);
                if (m) {
                    const v = m[1].toLowerCase();
                    if (v === 'ru' || v === 'en')
                        return v;
                }
            }
        }
    } catch {
        // нет файла или нет прав — падаем на системную локаль
    }
    const loc = (GLib.getenv('LC_ALL') || GLib.getenv('LC_MESSAGES')
                 || GLib.getenv('LANG') || '').toLowerCase();
    return loc.startsWith('ru') ? 'ru' : 'en';
}

function _(key) {
    const d = T[uiLang()] || T.en;
    return d[key] !== undefined ? d[key] : (T.en[key] !== undefined
                                            ? T.en[key] : key);
}

const DOT = {
    running: '#33d17a',   // зелёный — работает
    stopped: '#deddda',   // светлый нейтральный — остановлен пользователем
    error:   '#e01b24',   // красный — ошибка (упала/не запустилась)
    unknown: '#9a9996',   // серый — статус ещё не определён (не настраивается)
};

// ключ настроек для каждого состояния
const COLOR_KEY = {
    running: 'color-running',
    stopped: 'color-stopped',
    error:   'color-error',
};

const AutoswitchIndicator = GObject.registerClass(
class AutoswitchIndicator extends PanelMenu.Button {
    _init(settings, openPrefs) {
        super._init(0.5, 'Autoswitch', false);   // 0.5 = меню центрируется под кнопкой

        this._settings = settings;
        this._openPrefs = openPrefs;

        // убрать лишние горизонтальные отступы самой панельной кнопки,
        // чтобы точка не висела далеко от соседей
        this.add_style_class_name('autoswitch-button');

        // маленькая точка: рисуем через St.Icon с цветной заливкой не выйдет,
        // поэтому используем St.Widget-кружок нужного цвета, обёрнутый с воздухом
        // размер и отступы точки берём из настроек
        const size = this._settings ? this._settings.get_int('dot-size') : 10;
        const pad = this._settings ? this._settings.get_int('dot-padding') : 0;
        this._dot = new St.Widget({
            style_class: 'autoswitch-dot',
            width: size,
            height: size,
            y_align: Clutter.ActorAlign.CENTER,
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._dotBox = new St.Bin({
            child: this._dot,
            y_align: Clutter.ActorAlign.CENTER,
            style: `padding: 0 ${pad}px;`,
        });
        this.add_child(this._dotBox);

        // реагировать на изменение настроек в реальном времени
        if (this._settings) {
            this._settingsChanged = this._settings.connect('changed', () => {
                this._applyGeometry();
            });
        }

        this._state = 'unknown';
        this._applyDot();

        // --- меню ---
        // заголовок с названием программы
        this._itemTitle = new PopupMenu.PopupMenuItem('Autoswitch', {
            reactive: false,
            style_class: 'autoswitch-menu-title',
        });
        this._itemTitle.label.clutter_text.set_markup(
            '<b>Autoswitch</b>'
        );
        this.menu.addMenuItem(this._itemTitle);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._itemStatus = new PopupMenu.PopupMenuItem(_('status') + ': …', {
            reactive: false,
        });
        this.menu.addMenuItem(this._itemStatus);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._itemStart = new PopupMenu.PopupMenuItem(_('start'));
        this._itemStart.connect('activate', () => this._svc('start'));
        this.menu.addMenuItem(this._itemStart);

        this._itemStop = new PopupMenu.PopupMenuItem(_('stop'));
        this._itemStop.connect('activate', () => this._svc('stop'));
        this.menu.addMenuItem(this._itemStop);

        this._itemRestart = new PopupMenu.PopupMenuItem(_('restart'));
        this._itemRestart.connect('activate', () => this._svc('restart'));
        this.menu.addMenuItem(this._itemRestart);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // пункт открытия настроек самого расширения (размер/отступы точки)
        this._itemPrefs = new PopupMenu.PopupMenuItem(_('prefs'));
        this._itemPrefs.connect('activate', () => {
            if (this._openPrefs)
                this._openPrefs();
        });
        this.menu.addMenuItem(this._itemPrefs);

        this._itemSettings = new PopupMenu.PopupMenuItem(_('settings') + ' (asc)');
        this._itemSettings.connect('activate', () => this._openAsc());
        this.menu.addMenuItem(this._itemSettings);
        this._colorAscLabel();   // фирменные цвета для (asc)

        // обновлять статус при открытии меню и периодически
        this.menu.connect('open-state-changed', (menu, open) => {
            if (open)
                this._refresh();
        });
        this._refresh();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 3, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _applyGeometry() {
        if (!this._settings)
            return;
        const size = this._settings.get_int('dot-size');
        const pad = this._settings.get_int('dot-padding');
        this._dot.set_width(size);
        this._dot.set_height(size);
        this._dotBox.set_style(`padding: 0 ${pad}px;`);
        this._applyDot();          // перерисовать точку (размер/радиус/цвет)
        this._setState(this._state); // обновить цвет статуса в меню
    }

    // цвет для состояния: из настроек, иначе дефолт
    _colorFor(state) {
        const key = COLOR_KEY[state];
        if (key && this._settings) {
            const v = this._settings.get_string(key);
            if (v)
                return v;
        }
        return DOT[state] || DOT.unknown;
    }

    _applyDot() {
        const color = this._colorFor(this._state);
        const size = this._settings ? this._settings.get_int('dot-size') : 10;
        const r = Math.round(size / 2);
        this._dot.set_style(
            `background-color: ${color};` +
            `border-radius: ${r}px;` +
            'box-shadow: 0 0 2px rgba(0,0,0,0.5);'
        );
    }

    // Перерисовать подписи меню под новый ui_lang, без перезахода в сессию.
    relang() {
        const set = (item, text) => {
            if (item && item.label)
                item.label.text = text;
        };
        set(this._itemStart, _('start'));
        set(this._itemStop, _('stop'));
        set(this._itemRestart, _('restart'));
        set(this._itemPrefs, _('prefs'));
        this._colorAscLabel();
        // слово состояния живёт в _setState — перерисовываем принудительно,
        // иначе «Статус: работает» осталось бы на прежнем языке
        if (this._state)
            this._setState(this._state);
    }

    // «(asc)» фирменными цветами логотипа: a=зелёный, s=синий, c=оранжевый
    _colorAscLabel() {
        if (this._itemSettings && this._itemSettings.label) {
            const ct = this._itemSettings.label.clutter_text;
            // Сброс перед markup обязателен при смене языка НА ЛЕТУ: иначе
            // clutter_text держит ширину от прежней подписи, и более длинный
            // «Settings (asc)» рисуется в старую (узкую) рамку → «Settings (a…».
            // Пустой text заставляет пересчитать предпочтительную ширину.
            ct.set_text('');
            ct.set_markup(
                _('settings') + ' (' +
                '<span foreground="#2ecc40" weight="bold">a</span>' +
                '<span foreground="#0074ff" weight="bold">s</span>' +
                '<span foreground="#ff8c00" weight="bold">c</span>' +
                ')'
            );
            // подпись меняет длину — пусть родитель пересчитает раскладку
            this._itemSettings.label.queue_relayout();
        }
    }

    _setState(state) {
        if (state !== this._state) {
            this._state = state;
            this._applyDot();
        }
        // текст статуса под состояние; цвет — из настроек (для unknown дефолт)
        const text = {
            running: _('running'),
            stopped: _('stopped'),
            error:   _('error'),
            unknown: '…',
        }[state] || '…';
        const color = this._colorFor(state);
        if (this._itemStatus && this._itemStatus.label) {
            this._itemStatus.label.clutter_text.set_markup(
                `${_('status')}: ` +
                `<span foreground="${color}" weight="bold">${text}</span>`
            );
        }
    }

    // Спросить systemctl is-active и обновить состояние
    _refresh() {
        try {
            const proc = Gio.Subprocess.new(
                ['systemctl', 'is-active', SERVICE],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
            );
            proc.communicate_utf8_async(null, null, (p, res) => {
                try {
                    const [, out] = p.communicate_utf8_finish(res);
                    const s = (out || '').trim();
                    if (s === 'active')
                        this._setState('running');
                    else if (s === 'failed')
                        this._setState('error');
                    else
                        this._setState('stopped');
                } catch (e) {
                    this._setState('unknown');
                }
            });
        } catch (e) {
            this._setState('unknown');
        }
    }

    // Управление службой без пароля (через polkit-правило, ставится установщиком)
    _svc(action) {
        try {
            // Сразу гасим значок: перезапуск занимает доли секунды, и если
            // просто подождать и опросить, служба уже снова работает —
            // цвет не меняется и кажется, что команда не сработала.
            // Показываем промежуточное состояние сами, а через момент
            // выясняем настоящее.
            if (action === 'restart' || action === 'stop')
                this._setState('stopped');
            const proc = Gio.Subprocess.new(
                ['systemctl', action, SERVICE],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
            );
            proc.communicate_utf8_async(null, null, (p, res) => {
                // после действия обновим статус чуть позже
                GLib.timeout_add(GLib.PRIORITY_DEFAULT, 600, () => {
                    this._refresh();
                    return GLib.SOURCE_REMOVE;
                });
            });
        } catch (e) {
            Main.notify('Autoswitch', _('failed') + action);
        }
    }

    // Открыть графический интерфейс asc-gui; если его нет — TUI в терминале.
    // Ищем по абсолютному пути: у gnome-shell свой PATH, и /usr/local/bin
    // в него может не входить — тогда запуск по имени не срабатывает и
    // вместо окна открывается терминал, хотя asc-gui в системе есть.
    _openAsc() {
        const gui = GLib.find_program_in_path('asc-gui')
            || ['/usr/local/bin/asc-gui', '/usr/bin/asc-gui'].find(
                p => GLib.file_test(p, GLib.FileTest.IS_EXECUTABLE));
        try {
            if (!gui)
                throw new Error('asc-gui не найден');
            GLib.spawn_command_line_async(gui);
        } catch (e) {
            try {
                GLib.spawn_command_line_async('kgx -- bash -lc asc');
            } catch (e2) {
                Main.notify('Autoswitch', _('failed_asc'));
            }
        }
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = null;
        }
        if (this._settings && this._settingsChanged) {
            this._settings.disconnect(this._settingsChanged);
            this._settingsChanged = null;
        }
        super.destroy();
    }
});

export default class AutoswitchFocusExtension extends Extension {
    // Читает show_indicator из конфига движка. Файл может отсутствовать
    // (расширение поставили отдельно) — тогда индикатор показываем.
    _showIndicator() {
        try {
            const [ok, bytes] = GLib.file_get_contents(CONFIG_PATH);
            if (!ok)
                return true;
            const text = new TextDecoder().decode(bytes);
            for (const line of text.split('\n')) {
                const m = line.match(/^\s*show_indicator\s*=\s*(\S+)/);
                if (m)
                    return m[1].toLowerCase() !== 'off';
            }
        } catch {
            // нет файла или нет прав — ведём себя как при show_indicator=on
        }
        return true;
    }

    // Запоминает соседа слева перед тем, как убрать индикатор из панели.
    // Индекс в addToStatusArea считается от числа значков, которые лежат
    // в боксе в момент вставки, а оно разное при входе в сессию (соседи ещё
    // не загрузились) и при возврате индикатора через `asc` (панель полная).
    // Поэтому позицию держим не номером, а привязкой к соседу.
    _rememberSlot() {
        this._savedSibling = null;
        this._savedIndex = null;
        this._savedBox = null;
        try {
            const container = this._indicator?.container;
            const box = container?.get_parent();
            if (!box)
                return;
            const kids = box.get_children();
            const idx = kids.indexOf(container);
            if (idx < 0)
                return;
            this._savedBox = box;
            this._savedIndex = idx;
            this._savedSibling = idx > 0 ? kids[idx - 1] : null;
        } catch {
            // панель уже разбирается — вернёмся на место по запасному правилу
        }
    }

    // Ставит индикатор туда же, где он был до выключения. Если прошлого места
    // нет (индикатор выключили ещё до входа в сессию) — становимся слева
    // от часов, это его штатное место.
    _restoreSlot() {
        try {
            const container = this._indicator?.container;
            const box = container?.get_parent();
            if (!box)
                return;
            // сосед мог исчезнуть из панели, пока индикатор был выключен
            if (this._savedSibling && this._savedSibling.get_parent() === box) {
                box.set_child_above_sibling(container, this._savedSibling);
                return;
            }
            // индикатор был крайним слева в своём боксе
            if (this._savedBox === box && this._savedIndex === 0) {
                box.set_child_at_index(container, 0);
                return;
            }
            const clock = Main.panel.statusArea.dateMenu;
            if (clock?.container && clock.container.get_parent() === box)
                box.set_child_below_sibling(container, clock.container);
        } catch {
            // не смогли встать точно — остаёмся там, куда положила оболочка
        }
    }

    // Приводит панель в соответствие флагу. Вызывается при включении
    // расширения и при каждом изменении конфига.
    _syncIndicator() {
        const want = this._showIndicator();
        if (want && !this._indicator) {
            // Оболочка держит значки в statusArea по имени. Если от прошлой
            // жизни расширения там остался хвост, повторное добавление под
            // тем же именем сломается — поэтому сначала подчищаем.
            const stale = Main.panel.statusArea['autoswitch-indicator'];
            if (stale)
                stale.destroy();
            this._indicator = new AutoswitchIndicator(this._settings,
                                                      () => this.openPreferences());
            Main.panel.addToStatusArea('autoswitch-indicator', this._indicator, 5, 'right');
            this._restoreSlot();
        } else if (!want && this._indicator) {
            this._rememberSlot();
            this._indicator.destroy();
            this._indicator = null;
        }
    }

    // `asc` правит конфиг через sed -i, то есть подменяет файл целиком.
    // Поэтому следим за самим файлом и пересоздаём слежение при подмене,
    // а события гасим таймером: за одну правку их прилетает несколько.
    // Слепок настроек, которые реально читает служба: всё, кроме строк,
    // влияющих только на интерфейс (ui_lang, show_indicator). По нему видно,
    // надо ли показывать перезапуск.
    _engineConfigSignature() {
        try {
            const [ok, bytes] = GLib.file_get_contents(CONFIG_PATH);
            if (!ok)
                return '';
            const text = new TextDecoder().decode(bytes);
            return text.split('\n')
                .filter(l => {
                    const t = l.trim();
                    if (!t || t.startsWith('#'))
                        return false;
                    return !t.startsWith('ui_lang=') &&
                           !t.startsWith('show_indicator=');
                })
                .join('\n');
        } catch {
            return '';
        }
    }

    _watchBeacon() {
        try {
            const f = Gio.File.new_for_path(BEACON_PATH);
            // За несуществующим файлом следить нельзя — создаём пустой,
            // чтобы монитор заработал сразу, а не после первого сигнала.
            if (!f.query_exists(null)) {
                try {
                    f.replace_contents(new TextEncoder().encode(''), null,
                                       false, Gio.FileCreateFlags.NONE, null);
                } catch { }
            }
            this._beacon = f.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._beaconId = this._beacon.connect('changed', () => {
                if (this._beaconDebounce)
                    GLib.Source.remove(this._beaconDebounce);
                this._beaconDebounce = GLib.timeout_add(
                    GLib.PRIORITY_DEFAULT, 150, () => {
                        this._beaconDebounce = null;
                        if (this._indicator) {
                            this._indicator._setState('stopped');
                            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 600, () => {
                                this._indicator._refresh();
                                return GLib.SOURCE_REMOVE;
                            });
                        }
                        return GLib.SOURCE_REMOVE;
                    });
            });
        } catch {
            this._beacon = null;
        }
    }

    _watchConfig() {
        try {
            const file = Gio.File.new_for_path(CONFIG_PATH);
            this._monitor = file.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._monitorId = this._monitor.connect('changed', () => {
                if (this._debounceId)
                    GLib.Source.remove(this._debounceId);
                this._debounceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
                    this._debounceId = null;
                    this._syncIndicator();
                    // ui_lang лежит в том же файле — язык меняем сразу
                    if (this._indicator && this._indicator.relang)
                        this._indicator.relang();
                    // Конфиг правят через asc/asc-gui, а те сразу
                    // перезапускают службу. Перезапуск мгновенный, поэтому
                    // сами показываем промежуточное состояние — иначе в
                    // панели ничего не меняется и кажется, что настройка
                    // не применилась.
                    // Но мигаем ТОЛЬКО если изменилось то, что читает
                    // служба: язык интерфейса и видимость значка живут в
                    // том же файле, а службу не трогают — на них вспышка
                    // была бы обманом.
                    const sig = this._engineConfigSignature();
                    if (this._indicator && sig !== this._cfgSig) {
                        this._cfgSig = sig;
                        this._indicator._setState('stopped');
                        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 700, () => {
                            this._indicator._refresh();
                            return GLib.SOURCE_REMOVE;
                        });
                    } else {
                        this._cfgSig = sig;
                    }
                    return GLib.SOURCE_REMOVE;
                });
            });
        } catch {
            // без слежения флаг применится при следующем входе в сессию
            this._monitor = null;
        }
    }

    enable() {
        // настройки расширения (размер/отступы точки)
        this._settings = this.getSettings();
        this._indicator = null;
        this._monitor = null;
        this._monitorId = null;
        this._debounceId = null;
        // прошлое место индикатора в панели (см. _rememberSlot)
        this._savedSibling = null;
        this._savedIndex = null;
        this._savedBox = null;
        // D-Bus мост (для переключения раскладки/исключений на Wayland)
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(
            Gio.DBus.session,
            '/org/gnome/Shell/Extensions/AutoswitchFocus'
        );
        this._syncIndicator();
        this._cfgSig = this._engineConfigSignature();
        this._watchConfig();
        this._watchBeacon();
    }

    disable() {
        if (this._debounceId) {
            GLib.Source.remove(this._debounceId);
            this._debounceId = null;
        }
        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
            this._monitorId = null;
        }
        if (this._beacon) {
            if (this._beaconId)
                this._beacon.disconnect(this._beaconId);
            this._beacon.cancel();
            this._beacon = null;
            this._beaconId = null;
        }
        if (this._beaconDebounce) {
            GLib.Source.remove(this._beaconDebounce);
            this._beaconDebounce = null;
        }
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    GetFocusedClass() {
        const w = global.display.focus_window;
        if (!w)
            return '';
        return w.get_wm_class() || '';
    }

    GetFocusedInfo() {
        const w = global.display.focus_window;
        if (!w)
            return ['', ''];
        return [w.get_wm_class() || '', w.get_title() || ''];
    }

    _manager() {
        return Keyboard.getInputSourceManager();
    }

    GetLayout() {
        try {
            const m = this._manager();
            const cur = m.currentSource;
            return cur ? cur.index : 0;
        } catch (e) {
            return 0;
        }
    }

    GetLayoutCount() {
        try {
            const m = this._manager();
            return Object.keys(m.inputSources).length;
        } catch (e) {
            return 0;
        }
    }

    SetLayout(index) {
        try {
            const m = this._manager();
            const src = m.inputSources[index];
            if (!src)
                return false;
            src.activate(true);
            return true;
        } catch (e) {
            logError(e, 'autoswitch SetLayout');
            return false;
        }
    }

    CloseWindowByTitle(title) {
        try {
            if (!title)
                return false;
            const actors = global.get_window_actors();
            for (const pass of [0, 1]) {
                for (const actor of actors) {
                    const w = actor.meta_window;
                    if (!w)
                        continue;
                    const t = w.get_title() || '';
                    const hit = pass === 0 ? (t === title) : t.includes(title);
                    if (hit) {
                        w.delete(global.get_current_time());
                        return true;
                    }
                }
            }
            return false;
        } catch (e) {
            logError(e, 'autoswitch CloseWindowByTitle');
            return false;
        }
    }
}
