/*
 * Карточка настроек — визуальный аналог Adw.PreferencesGroup из
 * GNOME-расширения: скруглённый блок с фоном, внутри строки.
 * Kirigami готового такого контейнера не даёт, поэтому собран вручную.
 */

import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Rectangle {
    id: card

    default property alias content: inner.data

    Kirigami.Theme.colorSet: Kirigami.Theme.View
    Kirigami.Theme.inherit: false

    color: Kirigami.Theme.backgroundColor
    radius: Kirigami.Units.smallSpacing * 1.5
    border.width: 1
    border.color: Qt.rgba(Kirigami.Theme.textColor.r,
                          Kirigami.Theme.textColor.g,
                          Kirigami.Theme.textColor.b, 0.15)

    implicitHeight: inner.implicitHeight
    Layout.fillWidth: true

    ColumnLayout {
        id: inner
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: 0
    }
}
