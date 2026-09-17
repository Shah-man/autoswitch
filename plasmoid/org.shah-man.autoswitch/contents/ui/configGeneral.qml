/*
 * Окно настроек апплета.
 *
 * Повторяет prefs.js GNOME-расширения: те же блоки, заголовки и подписи,
 * строки собраны в карточки, ползунки вместо счётчиков, сброс возвращает
 * и размер, и цвета.
 *
 * Отличие одно и по существу: на GNOME расширение обязательно — через
 * него движок читает активное окно и переключает раскладку. Апплет же
 * только показывает состояние, служба работает и без него, поэтому текст
 * «не отключайте» заменён на честный.
 */

import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support

ColumnLayout {
    id: page

    // имена cfg_* — соглашение Plasma: она сама связывает их с main.xml
    property alias cfg_dotSize: sizeSlider.value
    property alias cfg_colorRunning: runningRow.chosen
    property alias cfg_colorStopped: stoppedRow.chosen
    property alias cfg_colorError: errorRow.chosen

    readonly property int rowPad: Kirigami.Units.largeSpacing

    // ── язык интерфейса: тот же ui_lang, что у остальных частей ───────
    // Окно настроек живёт отдельным процессом от апплета, поэтому читает
    // конфиг само, синхронной командой — строк мало, задержки не видно.
    property string uiLang: ""

    P5Support.DataSource {
        id: cfgReader
        engine: "executable"
        connectedSources: []
        onNewData: function (source, data) {
            // grep отдаёт строку целиком: ui_lang=en
            var out = (data["stdout"] || "").trim().toLowerCase()
            var rows = out.split("\n")
            var v = ""
            for (var i = 0; i < rows.length; i++) {
                var kv = rows[i].split("=")
                if (kv.length >= 2 && kv[0].trim() === "ui_lang")
                    v = kv[1].trim()
            }
            page.uiLang = (v === "ru" || v === "en") ? v : ""
            disconnectSource(source)
        }
    }

    Component.onCompleted: {
        cfgReader.connectSource(
            "grep -h '^ui_lang=' /etc/autoswitch/config")
    }

    readonly property var tr_ru: ({
        "about": "Часть переключателя раскладки autoswitch: показывает "
               + "состояние службы и даёт управлять ею из панели. "
               + "Сама раскладка переключается службой и без апплета.",
        "ind": "Индикатор",
        "ind_d": "Настройте размер индикатора под своё зрение.",
        "size": "Размер индикатора",
        "size_d": "Диаметр в пикселях (6–16)",
        "colors": "Цвета состояний",
        "colors_d": "Цвет индикатора для каждого состояния службы.",
        "run": "Работает", "run_d": "Служба запущена и активна",
        "stop": "Остановлен", "stop_d": "Служба выключена пользователем",
        "err": "Ошибка", "err_d": "Служба упала или не запустилась",
        "reset": "Сбросить по умолчанию",
        "reset_d": "Размер 10 px, стандартные цвета",
        "reset_b": "Сбросить"
    })

    readonly property var tr_en: ({
        "about": "Part of the autoswitch layout switcher: shows the service "
               + "state and lets you control it from the panel. "
               + "The layout itself is switched by the service anyway.",
        "ind": "Indicator",
        "ind_d": "Set the indicator size to suit your eyes.",
        "size": "Indicator size",
        "size_d": "Diameter in pixels (6–16)",
        "colors": "State colours",
        "colors_d": "Indicator colour for each service state.",
        "run": "Running", "run_d": "Service is up and active",
        "stop": "Stopped", "stop_d": "Service switched off by the user",
        "err": "Error", "err_d": "Service crashed or failed to start",
        "reset": "Reset to defaults",
        "reset_d": "Size 10 px, standard colours",
        "reset_b": "Reset"
    })

    function tr(key) {
        var lang = uiLang
        if (lang !== "ru" && lang !== "en") {
            var loc = (Qt.locale().name || "").toLowerCase()
            lang = loc.indexOf("ru") === 0 ? "ru" : "en"
        }
        var d = lang === "ru" ? tr_ru : tr_en
        return d[key] !== undefined ? d[key] : key
    }

    spacing: Kirigami.Units.largeSpacing

    // ===================================================== о чём это
    Kirigami.InlineMessage {
        Layout.fillWidth: true
        visible: true
        type: Kirigami.MessageType.Information
        text: page.tr("about")
    }

    // ===================================================== размер
    ColumnLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing / 2

        Kirigami.Heading { level: 3; text: page.tr("ind") }

        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            opacity: 0.7
            text: page.tr("ind_d")
        }
    }

    SettingsCard {
        RowLayout {
            Layout.fillWidth: true
            Layout.margins: page.rowPad
            spacing: Kirigami.Units.largeSpacing

            ColumnLayout {
                spacing: 0
                Layout.fillWidth: true
                QQC2.Label { text: page.tr("size") }
                QQC2.Label {
                    text: page.tr("size_d")
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                }
            }

            QQC2.Slider {
                id: sizeSlider
                Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                from: 6
                to: 16
                stepSize: 1
                snapMode: QQC2.Slider.SnapAlways
            }

            QQC2.Label {
                text: sizeSlider.value
                Layout.preferredWidth: Kirigami.Units.gridUnit * 1.5
                horizontalAlignment: Text.AlignRight
            }
        }

    }

    // ================================================ цвета состояний
    ColumnLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing / 2

        Kirigami.Heading { level: 3; text: page.tr("colors") }

        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            opacity: 0.7
            text: page.tr("colors_d")
        }
    }

    SettingsCard {
        ColorRow {
            id: runningRow
            Layout.fillWidth: true
            Layout.margins: page.rowPad
            title: page.tr("run")
            subtitle: page.tr("run_d")
            chosen: "#33d17a"
        }

        Kirigami.Separator { Layout.fillWidth: true }

        ColorRow {
            id: stoppedRow
            Layout.fillWidth: true
            Layout.margins: page.rowPad
            title: page.tr("stop")
            subtitle: page.tr("stop_d")
            chosen: "#deddda"
        }

        Kirigami.Separator { Layout.fillWidth: true }

        ColorRow {
            id: errorRow
            Layout.fillWidth: true
            Layout.margins: page.rowPad
            title: page.tr("err")
            subtitle: page.tr("err_d")
            chosen: "#e01b24"
        }
    }

    // ========================================================= сброс
    SettingsCard {
        RowLayout {
            Layout.fillWidth: true
            Layout.margins: page.rowPad
            spacing: Kirigami.Units.largeSpacing

            ColumnLayout {
                spacing: 0
                Layout.fillWidth: true
                QQC2.Label { text: page.tr("reset") }
                QQC2.Label {
                    text: page.tr("reset_d")
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                }
            }

            QQC2.Button {
                text: page.tr("reset_b")
                icon.name: "edit-undo"
                onClicked: {
                    sizeSlider.value = 10
                    runningRow.chosen = "#33d17a"
                    stoppedRow.chosen = "#deddda"
                    errorRow.chosen   = "#e01b24"
                }
            }
        }
    }

    Item { Layout.fillHeight: true }
}
