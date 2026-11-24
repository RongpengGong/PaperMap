// Drag a rectangle on the rendered page. Persist coordinates in displayed PDF points.
window.pmRegionTool = function (pages, capture) {
  const button = document.getElementById("capture-region");
  let active = false;
  let drag = null;
  function finishDrag() {
    if (!drag) return;
    const {page, pointer, mark} = drag;
    drag = null;
    mark.remove();
    if (page.hasPointerCapture(pointer)) page.releasePointerCapture(pointer);
  }
  function toggle(on) {
    finishDrag();
    active = on;
    button.setAttribute("aria-pressed", String(on));
    pages.classList.toggle("select-region", on);
    if (on) {
      window.getSelection()?.removeAllRanges();
      window.pmToast(t("在一页内拖动框选图或表，按 Esc 取消。"));
    }
  }
  function point(event, page) {
    const bounds = page.getBoundingClientRect();
    return [Math.min(bounds.width, Math.max(0, event.clientX - bounds.left)),
            Math.min(bounds.height, Math.max(0, event.clientY - bounds.top))];
  }
  function box(event) {
    const [x, y] = point(event, drag.page), [sx, sy] = drag.start;
    return [Math.min(x, sx), Math.min(y, sy), Math.max(x, sx), Math.max(y, sy)];
  }
  button.addEventListener("click", () => toggle(!active));
  document.addEventListener("keydown", event => { if (event.key === "Escape" && active) toggle(false); });
  pages.addEventListener("pointerdown", event => {
    const page = event.target.closest(".page");
    if (!active || !page || drag || event.button !== 0 || !event.isPrimary) return;
    const img = page.querySelector("img");
    if (!img.complete || !img.naturalWidth) { window.pmToast(t("正在载入此页，请稍后框选。")); return; }
    event.preventDefault();
    const mark = document.createElement("div");
    mark.className = "region-drag";
    page.append(mark);
    drag = {page, start: point(event, page), mark, pointer: event.pointerId};
    page.setPointerCapture(event.pointerId);
  });
  pages.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    const [x0, y0, x1, y1] = box(event);
    Object.assign(drag.mark.style, {left: x0 + "px", top: y0 + "px", width: x1 - x0 + "px", height: y1 - y0 + "px"});
  });
  pages.addEventListener("pointerup", event => {
    if (!drag || event.pointerId !== drag.pointer) return;
    const {page} = drag, region = box(event), scale = Number(page.dataset.scale);
    finishDrag();
    const [x0, y0, x1, y1] = region;
    if (Math.min(x1 - x0, y1 - y0) < Math.max(4, 2 * scale)) return;
    // Immediate visual confirmation. The server renders the saved crop independently from the verified PDF.
    const img = page.querySelector("img"), bounds = page.getBoundingClientRect();
    const rx = img.naturalWidth / bounds.width, ry = img.naturalHeight / bounds.height;
    const w = (x1 - x0) * rx, h = (y1 - y0) * ry;
    const size = Math.min(1, 800 / Math.max(w, h)), preview = document.createElement("canvas");
    preview.width = Math.max(1, Math.round(w * size)); preview.height = Math.max(1, Math.round(h * size));
    preview.getContext("2d").drawImage(img, x0 * rx, y0 * ry, w, h, 0, 0, preview.width, preview.height);
    toggle(false);
    capture(Number(page.dataset.page), region.map(v => v / scale), preview);
  });
  for (const name of ["pointercancel", "lostpointercapture"]) {
    pages.addEventListener(name, event => { if (drag?.pointer === event.pointerId) finishDrag(); });
  }
};
