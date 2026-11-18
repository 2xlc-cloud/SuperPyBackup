"""Symbol im Infobereich der Taskleiste (rechts unten) mit Rechtsklick-Menue, nur mit der Win32-API (ctypes, keine Zusatzpakete).

Ein eigener Thread besitzt ein verstecktes Fenster samt Nachrichtenschleife. Klicks werden als Ereignisnamen an `on_event`
gemeldet (im Tray-Thread aufgerufen; der Aufrufer reicht sie thread-sicher weiter). Alles ist fehlertolerant: Gelingt das
Symbol nicht (z. B. Explorer nicht aktiv), liefert start() False und die Anwendung laeuft ohne Symbol weiter.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from . import icon

CLASS_NAME = "SuperPyBackupTrayWindow"
WM_CLOSE, WM_DESTROY, WM_NULL = 0x0010, 0x0002, 0x0000
WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # Rueckrufnachricht des Symbols
WM_OPEN = WM_APP + 2  # von einer zweiten Programminstanz: Fenster oeffnen
WM_MENU_COMMAND = WM_APP + 3  # Menuebefehl direkt ausloesen (Tests)
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_CHECKED, MF_DEFAULT = 0x0, 0x800, 0x1, 0x8, 0x1000
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN, TPM_RETURNCMD = 0x2, 0x20, 0x100

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


@dataclass
class MenuItem:
    event: str  # Name, der an on_event gemeldet wird
    text: str
    enabled: bool = True
    checked: bool = False
    default: bool = False  # fett dargestellt (Standardaktion)


_instances: dict[int, "TrayIcon"] = {}
_class_lock = threading.Lock()
_class_registered = False
_taskbar_created = 0


def _wndproc(hwnd, message, wparam, lparam):  # noqa: ANN001
    tray = _instances.get(hwnd or 0)
    try:
        if tray is not None:
            if message == WM_TRAY:
                tray._on_icon_message(lparam & 0xFFFF)
                return 0
            if message == WM_OPEN:
                tray._emit("open")
                return 0
            if message == WM_MENU_COMMAND:
                tray._emit_index(wparam)
                return 0
            if _taskbar_created and message == _taskbar_created:
                tray._add_icon()  # Explorer wurde neu gestartet: Symbol erneut anlegen
                return 0
            if message == WM_CLOSE:
                tray._remove_icon()
                user32.DestroyWindow(hwnd)
                return 0
        if message == WM_DESTROY:
            _instances.pop(hwnd or 0, None)
            user32.PostQuitMessage(0)
            return 0
    except Exception:  # noqa: BLE001 - nie eine Ausnahme in die Win32-Nachrichtenschleife geben
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


_WNDPROC_REF = WNDPROC(_wndproc)  # muss fuer die gesamte Laufzeit referenziert bleiben


def _register_class() -> bool:
    global _class_registered, _taskbar_created
    with _class_lock:
        if _class_registered:
            return True
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = _WNDPROC_REF
        window_class.hInstance = kernel32.GetModuleHandleW(None)
        window_class.lpszClassName = CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(window_class)) and ctypes.GetLastError() != 1410:  # 1410: Klasse existiert bereits
            return False
        _taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        _class_registered = True
        return True


def request_open_existing() -> bool:
    """Bittet eine bereits laufende Instanz, ihr Fenster zu zeigen. True, wenn eine gefunden wurde."""
    try:
        hwnd = user32.FindWindowW(CLASS_NAME, None)
        return bool(hwnd) and bool(user32.PostMessageW(hwnd, WM_OPEN, 0, 0))
    except OSError:
        return False


class TrayIcon:
    def __init__(self, on_event: Callable[[str], None], tooltip: str = "SuperPyBackup", items: list[MenuItem | None] | None = None) -> None:
        self.on_event = on_event
        self.tooltip = tooltip
        self.items: list[MenuItem | None] = list(items or [])  # None = Trennlinie
        self.hwnd = 0
        self._hicon = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False

    # ---- Lebenszyklus
    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="SuperPyBackup-Tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(3)
        if self._hicon:
            user32.DestroyIcon(self._hicon)
            self._hicon = 0

    # ---- Einstellungen
    def set_tooltip(self, text: str) -> None:
        self.tooltip = text
        if self._ok and self.hwnd:
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data()))

    def set_items(self, items: list[MenuItem | None]) -> None:
        self.items = list(items)

    # ---- intern
    def _data(self) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._hicon
        data.szTip = self.tooltip[:127]
        return data

    def _add_icon(self) -> bool:
        return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data())))

    def _remove_icon(self) -> None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data()))

    def _run(self) -> None:
        try:
            if not _register_class():
                return
            self._hicon = icon.create_hicon(32)
            self.hwnd = user32.CreateWindowExW(0, CLASS_NAME, "SuperPyBackup", 0, 0, 0, 0, 0, None, None, kernel32.GetModuleHandleW(None), None) or 0
            if not self.hwnd:
                return
            _instances[self.hwnd] = self
            self._ok = self._add_icon()
            if not self._ok:
                user32.DestroyWindow(self.hwnd)
        finally:
            self._ready.set()
        if not self._ok:
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _emit(self, event: str) -> None:
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001
            pass

    def _selectable(self) -> list[MenuItem]:
        return [item for item in self.items if item is not None]

    def _emit_index(self, index: int) -> None:
        items = self._selectable()
        if 0 <= index < len(items) and items[index].enabled:
            self._emit(items[index].event)

    def _on_icon_message(self, mouse_message: int) -> None:
        if mouse_message == WM_LBUTTONUP:
            self._emit("open")
        elif mouse_message in (WM_RBUTTONUP, WM_CONTEXTMENU):
            self._show_menu()

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            position = 0
            for item in self.items:
                if item is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                position += 1  # Befehlskennung = Index + 1 (0 bedeutet "nichts gewaehlt")
                flags = MF_STRING | (0 if item.enabled else MF_GRAYED) | (MF_CHECKED if item.checked else 0) | (MF_DEFAULT if item.default else 0)
                user32.AppendMenuW(menu, flags, position, item.text)
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)  # damit das Menue beim Klick daneben schliesst
            chosen = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_RETURNCMD, point.x, point.y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
            if chosen:
                self._emit_index(chosen - 1)
        finally:
            user32.DestroyMenu(menu)
