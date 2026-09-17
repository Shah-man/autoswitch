/*
 * Autoswitch — апплет панели Plasma.
 *
 * Зачем он нужен, если есть autoswitch-indicator на GTK: тот попадает в
 * панель через AppIndicator, а значит его меню уезжает оболочке по
 * протоколу DBusMenu. В этом протоколе у пункта меню есть только текст,
 * иконка и признак активности — поля «цвет текста» там нет, разметка
 * отбрасывается при сериализации. Поэтому на Plasma цветное меню чужому
 * приложению недоступно в принципе.
 *
 * Апплет же рисует сама оболочка, как виджет громкости или часы: цвета,
 * шрифты и отступы берутся из темы, окно настроек — штатное.
 *
 * Со службой апплет общается тем же способом, что и GTK-индикатор:
 * systemctl запускается от пользователя, а разрешение даёт правило
 * polkit, которое ставит installasc (только для autoswitch.service).
 */

import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support

PlasmoidItem {
    id: root

    readonly property string service: "autoswitch.service"

    // время последнего изменения файла-маячка (см. обработчик выше)
    property string beaconStamp: ""

    // running | stopped | error | unknown
    property string serviceState: "unknown"

    // Тот же флаг, которым GTK-индикатор управляется из меню asc
    // (пункт «Индикатор в панели»). Апплет обязан его слушаться, иначе
    // выключение в asc ни к чему не приводит.
    property bool indicatorEnabled: true

    // Сколько приложений в списке исключений — показывается справа в меню
    // у пункта «Настройки (asc)», где этот список и правится.
    property int exclusionCount: 0

    // Сведения о службе для правой колонки меню.
    property string autostart: ""     // включён / отключён
    property string uptime: ""        // сколько работает
    property string startedAt: ""     // когда последний раз стартовала
    property string stoppedAt: ""     // когда последний раз останавливалась


    // ── язык интерфейса ───────────────────────────────────────────────
    // Тот же ключ ui_lang из /etc/autoswitch/config, что читают asc,
    // asc-gui, GTK-индикатор и GNOME-расширение. Пустая строка — язык
    // ещё не прочитан, тогда берём системную локаль.
    property string uiLang: ""

    readonly property var tr_ru: ({
        "running": "работает", "stopped": "остановлен", "error": "ошибка",
        "status": "Статус", "start": "Запустить", "stop": "Остановить",
        "restart": "Перезапустить", "settings": "Настройки",
        "autostart": "автозапуск", "uptime": "в работе",
        "last_stop": "последняя", "last_start": "последний",
        "indicator": "индикатор", "enabled": "включён",
        "prefs": "Расширение", "exclusions": "исключений",
        "restart_svc": "Перезапустить службу",
        "disabled": "отключён", "less_min": "меньше минуты",
        "mins": "мин", "hours": "ч", "days": "сут", "apps": "прил."
    })

    readonly property var tr_en: ({
        "running": "running", "stopped": "stopped", "error": "error",
        "status": "Status", "start": "Start", "stop": "Stop",
        "restart": "Restart", "settings": "Settings",
        "autostart": "autostart", "uptime": "uptime",
        "last_stop": "last", "last_start": "last",
        "indicator": "indicator", "enabled": "enabled",
        "prefs": "Extension", "exclusions": "exclusions",
        "restart_svc": "Restart service",
        "disabled": "disabled", "less_min": "less than a minute",
        "mins": "min", "hours": "h", "days": "d", "apps": "apps"
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

    readonly property var stateColors: ({
        "running": plasmoid.configuration.colorRunning,
        "stopped": plasmoid.configuration.colorStopped,
        "error":   plasmoid.configuration.colorError,
        "unknown": plasmoid.configuration.colorUnknown
    })

    readonly property var stateWords: ({
        "running": tr("running"),
        "stopped": tr("stopped"),
        "error":   tr("error"),
        "unknown": "…"
    })

    readonly property color currentColor: stateColors[serviceState]
                                          || plasmoid.configuration.colorUnknown

    // PassiveStatus убирает значок из видимой части лотка
    Plasmoid.status: indicatorEnabled ? PlasmaCore.Types.ActiveStatus
                                      : PlasmaCore.Types.PassiveStatus
    toolTipMainText: "Autoswitch"
    toolTipSubText: tr("status") + ": " + stateWords[serviceState]

    // ------------------------------------------------------------ команды
    P5Support.DataSource {
        id: shell
        engine: "executable"
        connectedSources: []

        onNewData: function (source, data) {
            var out = (data["stdout"] || "").trim()
            if (source.indexOf("is-active") !== -1) {
                root.applyState(out)
            } else if (source.indexOf("systemctl show") !== -1) {
                root.applyUnitInfo(out)
            } else if (source.indexOf("exclude_apps") !== -1) {
                root.exclusionCount = out === "" ? 0 : out.split(",").filter(
                    function (x) { return x.trim() !== "" }).length
            } else if (source.indexOf("autoswitch.beacon") !== -1) {
                // Маячок: asc/asc-gui трогают этот файл после действий со
                // службой. Изменилось время — значит службу перезапустили
                // из окна программы; показываем это в панели, иначе цвет
                // не меняется и кажется, что настройка не применилась.
                if (root.beaconStamp !== "" && out !== root.beaconStamp) {
                    root.serviceState = "stopped"
                    delayedRefresh.restart()
                }
                root.beaconStamp = out
            } else if (source.indexOf("show_indicator|ui_lang") !== -1) {
                var lang = ""
                var show = ""
                var rows = out.split("\n")
                for (var i = 0; i < rows.length; i++) {
                    var kv = rows[i].split("=")
                    if (kv.length < 2)
                        continue
                    var key = kv[0].trim()
                    var val = kv[1].trim().toLowerCase()
                    if (key === "ui_lang")
                        lang = val
                    else if (key === "show_indicator")
                        show = val
                }
                root.uiLang = (lang === "ru" || lang === "en") ? lang : ""
                // пусто или "on" — показывать; всё остальное — прятать
                root.indicatorEnabled = (show === "" || show === "on"
                                         || show === "1" || show === "yes"
                                         || show === "true")
            }
            disconnectSource(source)
        }

        function run(cmd) {
            // повторное подключение того же источника не сработает,
            // поэтому сначала отцепляем на случай зависшего вызова
            disconnectSource(cmd)
            connectSource(cmd)
        }
    }

    function applyState(out) {
        if (out === "active") {
            serviceState = "running"
        } else if (out === "failed") {
            serviceState = "error"
        } else if (out === "inactive" || out === "deactivating") {
            serviceState = "stopped"
        } else if (out === "activating") {
            // промежуточное состояние — не мигаем цветом, ждём следующий опрос
            serviceState = serviceState === "unknown" ? "stopped" : serviceState
        } else {
            serviceState = "unknown"
        }
    }

    // Штамп systemd выглядит как "Sat 2026-08-01 20:29:31 +05";
    // Date его не разбирает, поэтому вытаскиваем числа сами.
    function parseStamp(v) {
        var m = /(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/.exec(v || "")
        if (m === null) {
            return null
        }
        return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]),
                        Number(m[4]), Number(m[5]), Number(m[6]))
    }

    function pad2(n) {
        return n < 10 ? "0" + n : "" + n
    }

    function shortStamp(d) {
        if (d === null) {
            return ""
        }
        var now = new Date()
        var sameDay = d.getFullYear() === now.getFullYear()
                      && d.getMonth() === now.getMonth()
                      && d.getDate() === now.getDate()
        var time = pad2(d.getHours()) + ":" + pad2(d.getMinutes())
        return sameDay ? time
                       : pad2(d.getDate()) + "." + pad2(d.getMonth() + 1) + " " + time
    }

    function humanSpan(d) {
        if (d === null) {
            return ""
        }
        var mins = Math.floor((new Date().getTime() - d.getTime()) / 60000)
        if (mins < 1) {
            return tr("less_min")
        }
        if (mins < 60) {
            return mins + " " + tr("mins")
        }
        var hours = Math.floor(mins / 60)
        if (hours < 24) {
            var rest = mins % 60
            return rest === 0 ? hours + " " + tr("hours")
                              : hours + " " + tr("hours") + " "
                                + rest + " " + tr("mins")
        }
        return Math.floor(hours / 24) + " " + tr("days")
    }

    function applyUnitInfo(out) {
        var started = null
        var stopped = null
        var lines = out.split("\n")
        for (var i = 0; i < lines.length; i++) {
            var eq = lines[i].indexOf("=")
            if (eq === -1) {
                continue
            }
            var key = lines[i].substring(0, eq)
            var val = lines[i].substring(eq + 1).trim()
            if (key === "UnitFileState") {
                autostart = val === "enabled" ? tr("enabled")
                          : val === "" ? "" : tr("disabled")
            } else if (key === "ActiveEnterTimestamp") {
                started = parseStamp(val)
            } else if (key === "InactiveEnterTimestamp") {
                stopped = parseStamp(val)
            }
        }
        // длительность показываем только у работающей службы
        uptime = serviceState === "running" ? humanSpan(started) : ""
        startedAt = shortStamp(started)
        stoppedAt = shortStamp(stopped)
    }

    function refresh() {
        shell.run("systemctl is-active " + service)
        // заодно проверяем маячок: не перезапускали ли службу из GUI
        // через bash: нужна подстановка переменной окружения, движок
        // executable сам её не раскрывает
        shell.run("bash -c 'stat -c %Y \"${XDG_RUNTIME_DIR:-/tmp}\""
                  + "/autoswitch.beacon 2>/dev/null || echo 0'")
        // grep -h, чтобы в выводе не было имени файла
        // один grep на оба ключа: каждый bash-конвейер — это пяток
        // процессов, а опрос идёт каждые три секунды
        shell.run("grep -hE '^(show_indicator|ui_lang)=' /etc/autoswitch/config")
    }

    // Сведения для правой колонки меню: спрашиваем только когда меню
    // открывают. В опросе по таймеру им делать нечего — их никто не
    // видит, а это два лишних процесса каждые три секунды.
    function refreshDetails() {
        shell.run("systemctl show " + service
                  + " -p UnitFileState -p ActiveEnterTimestamp -p InactiveEnterTimestamp")
        shell.run("bash -c \"grep -h '^exclude_apps=' /etc/autoswitch/config"
                  + " 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]'\"")
    }

    onExpandedChanged: {
        if (expanded) {
            refresh()
            refreshDetails()
        }
    }

    function serviceAction(action) {
        // Перезапуск занимает доли секунды: если просто подождать и
        // опросить, служба уже снова работает — цвет не меняется, и
        // кажется, что кнопка не сработала. Показываем промежуточное
        // состояние сами, а через момент выясняем настоящее.
        if (action === "restart" || action === "stop")
            serviceState = "stopped"
        shell.run("systemctl " + action + " " + service)
        // служба меняет состояние не мгновенно — перечитываем с задержкой
        delayedRefresh.restart()
    }

    function openAsc() {
        // Сначала графический asc-gui, и только если его нет — консольный
        // asc в терминале. Кроме имени проверяем абсолютные пути: PATH у
        // апплета бывает урезан, и /usr/local/bin в него не входит — тогда
        // вместо окна открывался терминал, хотя asc-gui в системе есть.
        shell.run("bash -lc 'for g in asc-gui /usr/local/bin/asc-gui"
                  + " /usr/bin/asc-gui; do command -v $g >/dev/null 2>&1"
                  + " && exec $g; done;"
                  + " for t in konsole x-terminal-emulator xfce4-terminal"
                  + " qterminal alacritty foot xterm; do command -v $t >/dev/null 2>&1"
                  + " && exec $t -e bash -lc asc; done'")
    }

    Timer {
        id: poll
        interval: 3000
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }

    Timer {
        id: delayedRefresh
        interval: 800
        onTriggered: {
            root.refresh()
            if (root.expanded) {
                root.refreshDetails()
            }
        }
    }

    // --------------------------------------------------- значок в панели
    compactRepresentation: MouseArea {
        id: compact

        readonly property int dot: Math.round(
            Kirigami.Units.iconSizes.small * (plasmoid.configuration.dotSize / 16))

        Layout.minimumWidth: dot
        Layout.minimumHeight: dot

        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        onClicked: function (mouse) {
            if (mouse.button === Qt.MiddleButton) {
                // средняя кнопка — быстрый переключатель, как у громкости
                root.serviceAction(root.serviceState === "running" ? "stop" : "start")
            } else {
                root.expanded = !root.expanded
            }
        }

        Rectangle {
            anchors.centerIn: parent
            width: compact.dot
            height: compact.dot
            radius: width / 2
            color: root.currentColor

            Behavior on color {
                ColorAnimation { duration: Kirigami.Units.shortDuration }
            }
        }
    }

    // ------------------------------------------------------------- меню
    // Вид повторяет меню GNOME-расширения, но без строки с именем:
    // в Plasma имя апплета уже написано в шапке, которую рисует сам
    // лоток, и вторая такая строка была дублем. Не кнопки, а строки —
    // кнопочные рамки в трее выглядят чужеродно и раздувают ширину.
    fullRepresentation: Item {
        id: menuRoot

        // Размер считаем по содержимому, как это делает GNOME: там меню
        // собрано из обычных PopupMenuItem и ширина нигде не задаётся —
        // подгоняется под самый широкий пункт. Фиксированное число здесь
        // давало то слишком широкое меню, то обрезанные подписи.
        // Общий отступ слева: одинаковый у пунктов и у строки состояния,
        // иначе они не выравниваются по левому краю.
        readonly property int pad: Kirigami.Units.smallSpacing * 2
        readonly property int iconGap: Kirigami.Units.smallSpacing

        implicitWidth: column.implicitWidth + Kirigami.Units.smallSpacing * 2
        implicitHeight: column.implicitHeight + Kirigami.Units.smallSpacing * 2

        // Ширину внутри лотка задаёт сам лоток: у его ExpandedRepresentation
        // прописан Layout.minimumWidth в 24 единицы сетки, сузить окно
        // апплет не может. Поэтому maximumWidth не ставим — колонка
        // занимает всю выданную ширину, чтобы подсветка строки при
        // наведении тянулась от края до края, а подписи шли слева.
        // Высота считается по содержимому, её и фиксируем.
        Layout.minimumWidth: implicitWidth
        Layout.minimumHeight: implicitHeight
        Layout.maximumHeight: implicitHeight

        // Одна строка меню: подсветка при наведении во всю ширину окна,
        // подпись слева. Рамки нет — кнопочные рамки в трее выглядят
        // чужеродно.
        // Строка меню: значок слева, подпись, значение у правого края.
        // Значение заодно занимает ширину, которую навязывает лоток.
        component Row_: PlasmaComponents.ItemDelegate {
            id: row

            property string iconName: ""
            property string value: ""

            Layout.fillWidth: true
            leftPadding: menuRoot.pad
            rightPadding: menuRoot.pad
            topPadding: Kirigami.Units.smallSpacing
            bottomPadding: Kirigami.Units.smallSpacing

            contentItem: RowLayout {
                spacing: 0

                Kirigami.Icon {
                    source: row.iconName
                    Layout.preferredWidth: Kirigami.Units.iconSizes.small
                    Layout.preferredHeight: Kirigami.Units.iconSizes.small
                    Layout.rightMargin: menuRoot.iconGap
                }

                PlasmaComponents.Label { text: row.text }

                Item { Layout.fillWidth: true; Layout.minimumWidth: Kirigami.Units.gridUnit }

                PlasmaComponents.Label {
                    visible: row.value !== ""
                    text: row.value
                    opacity: 0.7
                }
            }
        }

        // Разделитель во всю ширину окна.
        component Sep_: Item {
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.smallSpacing / 2
            Layout.bottomMargin: Kirigami.Units.smallSpacing / 2
            implicitWidth: 0
            implicitHeight: 1

            Kirigami.Separator {
                width: menuRoot.width
                height: 1
                x: (parent.width - width) / 2
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        ColumnLayout {
            id: column
            // По левому краю, вровень с заголовком в шапке лотка.
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: Kirigami.Units.smallSpacing
            spacing: 0

            // Разделитель над состоянием: в GNOME строка состояния
            // отчёркнута с обеих сторон, здесь верхняя линия ушла вместе
            // с заголовком — возвращаем её.
            Sep_ { }

            // --- состояние: слово окрашено, как в GNOME ---
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                Layout.bottomMargin: Kirigami.Units.smallSpacing
                spacing: 0

                // вровень со значками, а не с подписями: своего значка
                // у состояния нет, и подпись, сдвинутая на пустую
                // колонку, выглядела съехавшей
                Layout.leftMargin: menuRoot.pad

                PlasmaComponents.Label { text: tr("status") + ": " }

                PlasmaComponents.Label {
                    text: root.stateWords[root.serviceState]
                    color: root.currentColor
                    font.bold: true
                }

                Item { Layout.fillWidth: true; Layout.minimumWidth: Kirigami.Units.gridUnit }

                PlasmaComponents.Label {
                    Layout.rightMargin: menuRoot.pad
                    visible: root.autostart !== ""
                    text: tr("autostart") + ": " + root.autostart
                    opacity: 0.7
                }
            }

            Sep_ { }

            // --- действия ---
            Row_ {
                iconName: "media-playback-start"
                value: root.uptime === "" ? "" : tr("uptime") + ": " + root.uptime
                text: tr("start")
                onClicked: root.serviceAction("start")
            }

            Row_ {
                iconName: "media-playback-stop"
                value: root.stoppedAt === "" ? "" : tr("last_stop") + ": " + root.stoppedAt
                text: tr("stop")
                onClicked: root.serviceAction("stop")
            }

            Row_ {
                iconName: "view-refresh"
                value: root.startedAt === "" ? "" : tr("last_start") + ": " + root.startedAt
                text: tr("restart")
                onClicked: root.serviceAction("restart")
            }

            Sep_ { }

            Row_ {
                iconName: "configure"
                value: tr("indicator") + ": " + plasmoid.configuration.dotSize + " px"
                text: tr("prefs")
                onClicked: plasmoid.internalAction("configure").trigger()
            }

            // --- «Настройки (asc)»: буквы разноцветные, как в GNOME ---
            Row_ {
                onClicked: root.openAsc()

                iconName: "input-keyboard-symbolic"

                contentItem: RowLayout {
                    spacing: 0

                    // здесь contentItem свой из-за разноцветных букв,
                    // поэтому значок ставим сами
                    Kirigami.Icon {
                        // контурный вариант: цветной preferences-desktop-keyboard
                        // выбивался из ряда — остальные значки однотонные
                        source: "input-keyboard-symbolic"
                        // имя из набора Adwaita: в Breeze есть, в сторонних
                        // темах может не быть — тогда берётся запасное
                        fallback: "preferences-desktop-keyboard"
                        Layout.preferredWidth: Kirigami.Units.iconSizes.small
                        Layout.preferredHeight: Kirigami.Units.iconSizes.small
                        Layout.rightMargin: menuRoot.iconGap
                    }

                    PlasmaComponents.Label { text: tr("settings") + " (" }
                    PlasmaComponents.Label { text: "a"; color: "#2ecc40"; font.bold: true }
                    PlasmaComponents.Label { text: "s"; color: "#0074ff"; font.bold: true }
                    PlasmaComponents.Label { text: "c"; color: "#ff8c00"; font.bold: true }
                    PlasmaComponents.Label { text: ")" }

                    Item { Layout.fillWidth: true }

                    // Число исключений у правого края: заодно занимает
                    // ширину, которую лоток навязывает окну.
                    PlasmaComponents.Label {
                        visible: root.exclusionCount > 0
                        text: tr("exclusions") + ": " + root.exclusionCount
                        opacity: 0.7
                    }
                }
            }
        }
    }

    // штатные пункты контекстного меню апплета
    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: tr("restart_svc")
            icon.name: "view-refresh"
            onTriggered: root.serviceAction("restart")
        }
    ]

    Component.onCompleted: refresh()
}
