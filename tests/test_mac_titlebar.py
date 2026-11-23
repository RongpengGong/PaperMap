from types import SimpleNamespace

import pytest


def test_glass_keeps_the_native_double_click_target():
    appkit = pytest.importorskip("AppKit")
    event = pytest.importorskip("webview.event").Event
    from papermap.mac_titlebar import apply_titlebar

    appkit.NSApplication.sharedApplication()
    if not appkit.NSScreen.screens():
        pytest.skip("A window server is required")
    styles = appkit.NSWindowStyleMaskTitled | appkit.NSWindowStyleMaskClosable | appkit.NSWindowStyleMaskResizable
    native = appkit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        appkit.NSMakeRect(0, 0, 900, 600), styles, appkit.NSBackingStoreBuffered, False
    )
    native.setReleasedWhenClosed_(False)
    closed = event(None, should_lock=True)
    try:
        frame = native.contentView().superview()
        close = native.standardWindowButton_(appkit.NSWindowCloseButton)
        center_y = frame.convertPoint_fromView_((0, close.bounds().size.height / 2), close).y
        blank = (frame.bounds().size.width * .75, center_y)
        target = frame.hitTest_(blank)
        assert target is not None

        apply_titlebar(SimpleNamespace(native=native, events=SimpleNamespace(closed=closed)))
        assert frame.hitTest_(blank) == target
    finally:
        closed.set()
        native.close()
