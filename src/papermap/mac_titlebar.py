"""Blue glass behind the existing AppKit window title and traffic lights."""

import AppKit as A
import objc


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
        if A.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency():
            solid = color(.141, .239, .345, 1) if dark else color(.745, .843, .941, 1)
            solid.setFill()
            A.NSRectFill(rect)
            return
        if dark:
            left, right = color(.165, .306, .439, .68), color(.102, .239, .384, .64)
        else:
            left, right = color(.675, .839, .965, .56), color(.510, .733, .925, .51)
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
    bar.addSubview_positioned_relativeTo_(material, A.NSWindowBelow, anchor)
    native.setTitlebarAppearsTransparent_(True)
    # pywebview sets an explicit opaque background on the title-bar container.
    if container.respondsToSelector_("setBackgroundColor:"):
        container.setBackgroundColor_(A.NSColor.clearColor())

    center = A.NSWorkspace.sharedWorkspace().notificationCenter()
    center.addObserver_selector_name_object_(
        tint, "refreshTint:", A.NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification, None
    )
    window.events.closed += lambda: center.removeObserver_(tint)
