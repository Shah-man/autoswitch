/*
 * Строка выбора цвета — повторяет Adw.ActionRow с Gtk.ColorDialogButton
 * из GNOME-расширения.
 *
 * По нажатию открывается палитра Adwaita — та же, что показывает GTK.
 * Выбранный цвет отмечен галочкой, поэтому родной цвет состояния всегда
 * видно и можно вернуть одним кликом. Внизу — переход к полному диалогу
 * для произвольного оттенка.
 */

import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import QtQuick.Dialogs as Dialogs
import org.kde.kirigami as Kirigami

RowLayout {
    id: row

    property string title: ""
    property string subtitle: ""
    property string chosen: "#ffffff"

    // палитра Adwaita: ровно те цвета, что предлагает GTK на GNOME,
    // поэтому #33d17a, #deddda и #e01b24 в ней уже есть
    readonly property var palette: [
        "#99c1f1", "#62a0ea", "#3584e4", "#1c71d8", "#1a5fb4",
        "#8ff0a4", "#57e389", "#33d17a", "#2ec27e", "#26a269",
        "#f9f06b", "#f8e45c", "#f6d32d", "#f5c211", "#e5a50a",
        "#ffbe6f", "#ffa348", "#ff7800", "#e66100", "#c64600",
        "#f66151", "#ed333b", "#e01b24", "#c01c28", "#a51d2d",
        "#dc8add", "#c061cb", "#9141ac", "#813d9c", "#613583",
        "#cdab8f", "#b5835a", "#986a44", "#865e3c", "#63452c",
        "#ffffff", "#f6f5f4", "#deddda", "#c0bfbc", "#9a9996",
        "#77767b", "#5e5c64", "#3d3846", "#241f31", "#000000"
    ]

    spacing: Kirigami.Units.largeSpacing

    // подпись слева
    ColumnLayout {
        spacing: 0
        Layout.fillWidth: true

        QQC2.Label {
            text: row.title
            elide: Text.ElideRight
            Layout.fillWidth: true
        }

        QQC2.Label {
            text: row.subtitle
            font: Kirigami.Theme.smallFont
            opacity: 0.7
            visible: text.length > 0
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }

    // кнопка с текущим цветом
    QQC2.Button {
        id: swatchButton
        implicitWidth: Kirigami.Units.gridUnit * 3
        implicitHeight: Kirigami.Units.gridUnit * 1.6
        onClicked: palettePopup.open()

        contentItem: Rectangle {
            radius: Kirigami.Units.smallSpacing
            color: row.chosen
            border.width: 1
            border.color: Qt.rgba(Kirigami.Theme.textColor.r,
                                  Kirigami.Theme.textColor.g,
                                  Kirigami.Theme.textColor.b, 0.35)
        }

        QQC2.Popup {
            id: palettePopup
            y: parent.height + Kirigami.Units.smallSpacing
            x: parent.width - width
            padding: Kirigami.Units.smallSpacing
            modal: false
            focus: true

            contentItem: ColumnLayout {
                spacing: Kirigami.Units.smallSpacing

                Grid {
                    columns: 5
                    spacing: Kirigami.Units.smallSpacing

                    Repeater {
                        model: row.palette

                        delegate: Rectangle {
                            required property string modelData

                            width: Kirigami.Units.gridUnit * 1.4
                            height: width
                            radius: Kirigami.Units.smallSpacing
                            color: modelData
                            border.width: 1
                            border.color: Qt.rgba(Kirigami.Theme.textColor.r,
                                                  Kirigami.Theme.textColor.g,
                                                  Kirigami.Theme.textColor.b, 0.25)

                            // галочка на выбранном — как в палитре GTK
                            Kirigami.Icon {
                                anchors.centerIn: parent
                                width: Kirigami.Units.iconSizes.small
                                height: width
                                source: "checkmark"
                                visible: row.chosen.toLowerCase()
                                         === parent.modelData.toLowerCase()
                                // на светлых плашках чёрная галочка, на тёмных белая
                                color: Qt.colorEqual(parent.color, "transparent")
                                       ? Kirigami.Theme.textColor
                                       : (parent.color.hslLightness > 0.55
                                          ? "#000000" : "#ffffff")
                                isMask: true
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    row.chosen = parent.modelData
                                    palettePopup.close()
                                }
                            }
                        }
                    }
                }

                Kirigami.Separator { Layout.fillWidth: true }

                QQC2.Button {
                    Layout.fillWidth: true
                    text: (Qt.locale().name || "").toLowerCase().indexOf("ru") === 0
                          ? "Другой цвет…" : "Custom colour…"
                    icon.name: "color-picker"
                    onClicked: {
                        palettePopup.close()
                        colorDialog.open()
                    }
                }
            }
        }
    }

    Dialogs.ColorDialog {
        id: colorDialog
        selectedColor: row.chosen
        onAccepted: {
            // храним строкой #rrggbb — так же, как в GNOME-расширении
            row.chosen = selectedColor.toString().substring(0, 7)
        }
    }
}
