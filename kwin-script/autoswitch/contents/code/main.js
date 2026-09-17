// autoswitch — мост активного окна для KWin.
//
// На Plasma Wayland класс активного окна наружу не отдаётся: KWin знает
// его, но D-Bus такого запроса не принимает, а клиентские протоколы
// Wayland чужие окна не показывают. Поэтому класс выталкивает сам KWin
// через скрипт — ровно так же, как это делает расширение в GNOME.
//
// Скрипт умеет только отправлять (callDBus без ответа), поэтому на другом
// конце висит приёмник autoswitch-focus-bridge: он хранит последнее
// значение, а движок спрашивает уже у него.

var SERVICE = "org.autoswitch.Focus";
var PATH = "/Focus";
var IFACE = "org.autoswitch.Focus";

var last = null;

function push(window) {
    var cls = "";

    // Служебные окна (панели, всплывающие меню, оверлеи оболочки) не
    // считаем сменой окна: пользователь по-прежнему печатает в прежнем
    // приложении, и сбрасывать исключение из-за них нельзя.
    if (window && window.normalWindow && window.resourceClass) {
        cls = String(window.resourceClass).toLowerCase();
    } else if (window && !window.normalWindow) {
        return;
    }

    if (cls === last) {
        return;
    }
    last = cls;

    callDBus(SERVICE, PATH, IFACE, "SetClass", cls);
}

workspace.windowActivated.connect(push);

// стартовое значение: скрипт может запуститься уже при открытом окне
push(workspace.activeWindow);
