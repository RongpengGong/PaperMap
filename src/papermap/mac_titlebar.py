"""Blue glass behind the existing AppKit window title and traffic lights."""

import AppKit as A
import objc

from .themes import titlebar_colors

THEME = {"palette": "blue", "mode": "system"}
_TINTS = []


class PaperMapTitlebarMaterial(A.NSVisualEffectView):
    def hitTest_(self, point):
        # The title bar owns dragging, double-clicks and its standard buttons.
        return None


class PaperMapTitlebarTint(A.NSView):
    def drawRect_(self, rect):
        dark = self.effectiveAppearance().bestMatchFromAppearancesWithNames_(
            [A.NSAppearanceNameAqua, A.NSAppearanceNameDarkAqua]
        ) == A.NSAppearanceNameDarkAqua
        color = A.NSColor.colorWithSRGBRed_green_blue_alpha_
        lt, rt, sd = titlebar_colors(THEME["palette"], dark, THEME["mode"] == "black")
        if A.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency():
            color(*sd).setFill()
            A.NSRectFill(rect)
            return
        left, right = color(*lt), color(*rt)
        gradient = A.NSGradient.alloc().initWithStartingColor_endingColor_(left, right)
        gradient.drawInRect_angle_(self.bounds(), 0)

    def viewDidChangeEffectiveAppearance(self):
        objc.super(PaperMapTitlebarTint, self).viewDidChangeEffectiveAppearance()
        self.setNeedsDisplay_(True)

    def refreshTint_(self, notification):
        self.setNeedsDisplay_(True)


def apply_titlebar(window):
    """Called by pywebview's synchronous Cocoa before_show event."""
    native = window.native
    close = native.standardWindowButton_(A.NSWindowCloseButton)
    if close is None:
        return
    bar = close.superview()
    frame = native.contentView().superview()
    # Some AppKit layouts group the buttons. Find their full-width title bar.
    controls = close
    while bar is not None and bar != frame and bar.bounds().size.width < native.frame().size.width - 1:
        controls, bar = bar, bar.superview()
    if bar is None or bar == frame:
        return
    anchor = next((child for child in bar.subviews() if child == controls or child.isKindOfClass_(A.NSControl)), controls)
    container = bar
    while container.superview() is not None and container.superview() != frame:
        container = container.superview()

    material = PaperMapTitlebarMaterial.alloc().initWithFrame_(bar.bounds())
    material.setMaterial_(A.NSVisualEffectMaterialTitlebar)
    material.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
    material.setState_(A.NSVisualEffectStateFollowsWindowActiveState)
    material.setAccessibilityElement_(False)
    resize = A.NSViewWidthSizable | A.NSViewHeightSizable
    material.setAutoresizingMask_(resize)
    tint = PaperMapTitlebarTint.alloc().initWithFrame_(material.bounds())
    tint.setAutoresizingMask_(resize)
    tint.setAccessibilityElement_(False)
    material.addSubview_(tint)
    _TINTS.append(tint)
    set_appearance(native)
    bar.addSubview_positioned_relativeTo_(material, A.NSWindowBelow, anchor)
    # Tint the container while retaining AppKit's native title-bar double-click action.
    # pywebview initially gives this container an opaque window background.
    if container.respondsToSelector_("setBackgroundColor:"):
        container.setBackgroundColor_(A.NSColor.clearColor())

    center = A.NSWorkspace.sharedWorkspace().notificationCenter()
    center.addObserver_selector_name_object_(
        tint, "refreshTint:", A.NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification, None
    )
    window.events.closed += lambda: center.removeObserver_(tint)


def set_appearance(native) -> None:
    """Force light/dark window chrome when the user picked one (None = follow macOS)."""
    mode = THEME["mode"]
    name = {"light": A.NSAppearanceNameAqua, "dark": A.NSAppearanceNameDarkAqua,
            "black": A.NSAppearanceNameDarkAqua}.get(mode)
    native.setAppearance_(A.NSAppearance.appearanceNamed_(name) if name else None)


def apply_theme(window, palette: str, mode: str) -> None:
    """Live theme change from Settings (call on the main thread)."""
    THEME.update(palette=palette, mode=mode)
    set_appearance(window.native)
    for tint in _TINTS:
        tint.setNeedsDisplay_(True)
