import GLib from 'gi://GLib';

// ── язык интерфейса: тот же ui_lang, что у asc/asc-gui/индикатора ────
const CONFIG_PATH = '/etc/autoswitch/config';

const T = {
    ru: {
        page: 'Внешний вид',
        about_t: 'Важно',
        about_s: 'Часть переключателя раскладки autoswitch: переключает '
               + 'раскладку и определяет активное окно на Wayland, а также '
               + 'даёт индикатор в панели. Не отключайте — без него '
               + 'autoswitch не работает.',
        grp_ind: 'Индикатор',
        grp_ind_d: 'Настройте размер индикатора и отступы под своё зрение.',
        size: 'Размер индикатора',
        size_d: 'Диаметр в пикселях (6–16)',
        pad: 'Отступы по бокам',
        pad_d: 'Слева и справа в пикселях (0–10)',
        colors: 'Цвета состояний',
        colors_d: 'Цвет индикатора для каждого состояния службы.',
        run: 'Работает',
        run_d: 'Служба запущена и активна',
        stop: 'Остановлен',
        stop_d: 'Служба выключена пользователем',
        err: 'Ошибка',
        err_d: 'Служба упала или не запустилась',
        reset: 'Сбросить по умолчанию',
        reset_d: 'Размер 10 px, отступы 0 px, стандартные цвета',
        reset_b: 'Сбросить',
    },
    en: {
        page: 'Appearance',
        about_t: 'Important',
        about_s: 'Part of the autoswitch layout switcher: switches the '
               + 'keyboard layout and detects the active window on Wayland, '
               + "plus a panel indicator. Don't disable — autoswitch won't "
               + 'work without it.',
        grp_ind: 'Indicator',
        grp_ind_d: 'Set the indicator size and margins to suit your eyes.',
        size: 'Indicator size',
        size_d: 'Diameter in pixels (6–16)',
        pad: 'Side margins',
        pad_d: 'Left and right in pixels (0–10)',
        colors: 'State colours',
        colors_d: 'Indicator colour for each service state.',
        run: 'Running',
        run_d: 'Service is up and active',
        stop: 'Stopped',
        stop_d: 'Service switched off by the user',
        err: 'Error',
        err_d: 'Service crashed or failed to start',
        reset: 'Reset to defaults',
        reset_d: 'Size 10 px, margins 0 px, standard colours',
        reset_b: 'Reset',
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

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import Gdk from 'gi://Gdk';
import Gio from 'gi://Gio';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class AutoswitchPrefs extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        const page = new Adw.PreferencesPage({
            title: _('page'),
            icon_name: 'preferences-desktop-display-symbolic',
        });
        window.add(page);

        // ===== описание расширения =====
        const aboutGroup = new Adw.PreferencesGroup();
        page.add(aboutGroup);
        const about = new Adw.ActionRow({
            title: _('about_t'),
            subtitle: _('about_s'),
        });
        about.add_prefix(new Gtk.Image({
            icon_name: 'dialog-information-symbolic',
            valign: Gtk.Align.CENTER,
        }));
        aboutGroup.add(about);

        // ===== размер и отступы =====
        const group = new Adw.PreferencesGroup({
            title: _('grp_ind'),
            description: _('grp_ind_d'),
        });
        page.add(group);

        // --- размер индикатора ---
        const sizeRow = new Adw.ActionRow({
            title: _('size'),
            subtitle: _('size_d'),
        });
        const sizeScale = new Gtk.Scale({
            orientation: Gtk.Orientation.HORIZONTAL,
            adjustment: new Gtk.Adjustment({
                lower: 6, upper: 16, step_increment: 1, page_increment: 1,
            }),
            digits: 0,
            draw_value: true,
            value_pos: Gtk.PositionType.RIGHT,
            hexpand: true,
            width_request: 200,
            valign: Gtk.Align.CENTER,
        });
        sizeScale.set_value(settings.get_int('dot-size'));
        sizeScale.connect('value-changed', (w) => {
            settings.set_int('dot-size', Math.round(w.get_value()));
        });
        sizeRow.add_suffix(sizeScale);
        group.add(sizeRow);

        // --- отступы ---
        const padRow = new Adw.ActionRow({
            title: _('pad'),
            subtitle: _('pad_d'),
        });
        const padScale = new Gtk.Scale({
            orientation: Gtk.Orientation.HORIZONTAL,
            adjustment: new Gtk.Adjustment({
                lower: 0, upper: 10, step_increment: 1, page_increment: 1,
            }),
            digits: 0,
            draw_value: true,
            value_pos: Gtk.PositionType.RIGHT,
            hexpand: true,
            width_request: 200,
            valign: Gtk.Align.CENTER,
        });
        padScale.set_value(settings.get_int('dot-padding'));
        padScale.connect('value-changed', (w) => {
            settings.set_int('dot-padding', Math.round(w.get_value()));
        });
        padRow.add_suffix(padScale);
        group.add(padRow);

        // ===== цвета состояний =====
        const colorGroup = new Adw.PreferencesGroup({
            title: _('colors'),
            description: _('colors_d'),
        });
        page.add(colorGroup);

        // helper: строка с кнопкой выбора цвета, связанной с ключом настроек
        const makeColorRow = (title, subtitle, key) => {
            const row = new Adw.ActionRow({title, subtitle});
            const btn = new Gtk.ColorDialogButton({
                dialog: new Gtk.ColorDialog({with_alpha: false}),
                valign: Gtk.Align.CENTER,
            });
            // начальное значение из настроек
            const rgba = new Gdk.RGBA();
            rgba.parse(settings.get_string(key));
            btn.set_rgba(rgba);
            // запись при выборе
            btn.connect('notify::rgba', () => {
                const c = btn.get_rgba();
                // в hex #rrggbb
                const hex =
                    '#' +
                    [c.red, c.green, c.blue]
                        .map((v) => Math.round(v * 255)
                            .toString(16).padStart(2, '0'))
                        .join('');
                settings.set_string(key, hex);
            });
            row.add_suffix(btn);
            row._btn = btn;   // сохраним для сброса
            return row;
        };

        const rowRun = makeColorRow(_('run'), _('run_d'),
                                    'color-running');
        const rowStop = makeColorRow(_('stop'), _('stop_d'),
                                     'color-stopped');
        const rowErr = makeColorRow(_('err'), _('err_d'),
                                    'color-error');
        colorGroup.add(rowRun);
        colorGroup.add(rowStop);
        colorGroup.add(rowErr);

        // ===== сброс =====
        const resetGroup = new Adw.PreferencesGroup();
        page.add(resetGroup);
        const resetRow = new Adw.ActionRow({
            title: _('reset'),
            subtitle: _('reset_d'),
        });
        const resetBtn = new Gtk.Button({
            label: _('reset_b'),
            valign: Gtk.Align.CENTER,
        });
        resetBtn.connect('clicked', () => {
            settings.reset('dot-size');
            settings.reset('dot-padding');
            settings.reset('color-running');
            settings.reset('color-stopped');
            settings.reset('color-error');
            // обновить контролы
            sizeScale.set_value(settings.get_int('dot-size'));
            padScale.set_value(settings.get_int('dot-padding'));
            for (const [row, key] of [
                [rowRun, 'color-running'],
                [rowStop, 'color-stopped'],
                [rowErr, 'color-error'],
            ]) {
                const rgba = new Gdk.RGBA();
                rgba.parse(settings.get_string(key));
                row._btn.set_rgba(rgba);
            }
        });
        resetRow.add_suffix(resetBtn);
        resetGroup.add(resetRow);
    }
}
