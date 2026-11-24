import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { Placeholder } from "@tiptap/extensions";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import { TableKit } from "@tiptap/extension-table";
import Image from "@tiptap/extension-image";
import Highlight from "@tiptap/extension-highlight";
import { TextStyle, Color } from "@tiptap/extension-text-style";

const images = list => [...(list || [])].filter(f => f.type && f.type.startsWith("image/"));
const safeColor = c => (/^(#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|[a-z]+)$/i.test(c || "") ? c : "");

// Markdown that Obsidian also reads: plain highlight ==x==, coloured ones and text colour as inline HTML.
const PMHighlight = Highlight.configure({ multicolor: true }).extend({
  renderMarkdown(node, h) {
    const c = safeColor(node.attrs && node.attrs.color), inner = h.renderChildren(node);
    return c ? `<mark style="background-color: ${c}">${inner}</mark>` : `==${inner}==`;
  },
});
const PMTextStyle = TextStyle.extend({
  renderMarkdown(node, h) {
    const c = safeColor(node.attrs && node.attrs.color), inner = h.renderChildren(node);
    return c ? `<span style="color: ${c}">${inner}</span>` : inner;
  },
});

const HIGHLIGHTS = [["#fff3a3", "黄"], ["#c8f7c5", "绿"], ["#cfe8ff", "蓝"], ["#ffd6e7", "粉"], ["#e6dcff", "紫"]];
const COLORS = [["#d92d20", "红"], ["#dc6803", "橙"], ["#079455", "绿"], ["#1570ef", "蓝"], ["#7a5af8", "紫"], ["#667085", "灰"]];
const tr = s => (window.t ? window.t(s) : s);

function toolbar(editor, opts) {
  const bar = document.createElement("div");
  bar.className = "pm-toolbar";
  bar.addEventListener("mousedown", e => { if (!e.target.closest("input")) e.preventDefault(); });  // keep the selection
  const btn = (label, title, run, active) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "pm-tb"; b.innerHTML = label; b.title = tr(title);
    b.addEventListener("click", () => { run(); sync(); });
    b._active = active; bar.appendChild(b); return b;
  };
  const sep = () => { const s = document.createElement("span"); s.className = "pm-tb-sep"; bar.appendChild(s); };
  const menu = (label, title, items, apply, clear) => {
    const wrap = document.createElement("span"); wrap.className = "pm-tb-menu";
    const b = document.createElement("button"); b.type = "button"; b.className = "pm-tb"; b.innerHTML = label; b.title = tr(title);
    const pop = document.createElement("span"); pop.className = "pm-tb-pop"; pop.hidden = true;
    for (const [c, name] of items) {
      const s = document.createElement("button"); s.type = "button"; s.className = "pm-swatch"; s.style.setProperty("--c", c); s.title = tr(name);
      s.addEventListener("click", () => { apply(c); pop.hidden = true; sync(); }); pop.appendChild(s);
    }
    const x = document.createElement("button"); x.type = "button"; x.className = "pm-swatch none"; x.title = tr("清除"); x.textContent = "∅";
    x.addEventListener("click", () => { clear(); pop.hidden = true; sync(); }); pop.appendChild(x);
    b.addEventListener("click", () => { pop.hidden = !pop.hidden; });
    document.addEventListener("mousedown", e => { if (!wrap.contains(e.target)) pop.hidden = true; });
    wrap.append(b, pop); bar.appendChild(wrap);
  };
  const c = () => editor.chain().focus();
  btn("<b>B</b>", "粗体 ⌘B", () => c().toggleBold().run(), () => editor.isActive("bold"));
  btn("<i>I</i>", "斜体 ⌘I", () => c().toggleItalic().run(), () => editor.isActive("italic"));
  btn("<u>U</u>", "下划线 ⌘U", () => c().toggleUnderline().run(), () => editor.isActive("underline"));
  btn("<s>S</s>", "删除线", () => c().toggleStrike().run(), () => editor.isActive("strike"));
  menu('<span class="pm-hl">A</span>', "高亮", HIGHLIGHTS, col => c().setHighlight({ color: col }).run(), () => c().unsetHighlight().run());
  menu('<span class="pm-fc">A</span>', "文字颜色", COLORS, col => c().setColor(col).run(), () => c().unsetColor().run());
  sep();
  btn("H1", "一级标题", () => c().toggleHeading({ level: 1 }).run(), () => editor.isActive("heading", { level: 1 }));
  btn("H2", "二级标题", () => c().toggleHeading({ level: 2 }).run(), () => editor.isActive("heading", { level: 2 }));
  btn("H3", "三级标题", () => c().toggleHeading({ level: 3 }).run(), () => editor.isActive("heading", { level: 3 }));
  sep();
  btn("•", "项目列表", () => c().toggleBulletList().run(), () => editor.isActive("bulletList"));
  btn("1.", "编号列表", () => c().toggleOrderedList().run(), () => editor.isActive("orderedList"));
  btn("☑", "待办", () => c().toggleTaskList().run(), () => editor.isActive("taskList"));
  btn("❝", "引用", () => c().toggleBlockquote().run(), () => editor.isActive("blockquote"));
  btn("&lt;/&gt;", "行内代码", () => c().toggleCode().run(), () => editor.isActive("code"));
  btn("{ }", "代码块", () => c().toggleCodeBlock().run(), () => editor.isActive("codeBlock"));
  sep();
  btn("🔗", "链接", () => {
    const prev = editor.getAttributes("link").href || "";
    const url = prompt(tr("链接地址（留空则移除链接）"), prev);
    if (url === null) return;
    if (!url.trim()) c().unsetLink().run(); else c().extendMarkRange("link").setLink({ href: url.trim() }).run();
  }, () => editor.isActive("link"));
  btn("▦", "插入表格", () => c().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run());
  if (opts.pickImage) btn("🖼", "插入图片（也可以直接粘贴或拖进来）", () => opts.pickImage());
  btn("―", "分隔线", () => c().setHorizontalRule().run());
  sep();
  btn("↶", "撤销 ⌘Z", () => c().undo().run());
  btn("↷", "重做 ⇧⌘Z", () => c().redo().run());
  const buttons = [...bar.querySelectorAll(".pm-tb")];
  function sync() { buttons.forEach(b => b._active && b.classList.toggle("on", !!b._active())); }
  editor.on("selectionUpdate", sync); editor.on("transaction", sync);
  return bar;
}

// One small global for PaperMap's note editors: Markdown in, Markdown out, live formatting while typing.
// upload(file) -> Promise<{src, alt}> stores a pasted / dropped picture and returns its Markdown path.
window.PMEditor = {
  create(el, markdown, placeholder, onUpdate, onBlur, upload, opts = {}) {
    let editor;
    const put = (files, pos) => {
      if (!upload || !files.length) return false;
      files.forEach(f => upload(f).then(r => {
        if (!r || !r.src) return;
        const node = { type: "image", attrs: { src: r.src, alt: r.alt || "" } };
        if (pos == null) editor.chain().focus().insertContent(node).run();
        else editor.chain().focus().insertContentAt(pos, node).run();
      }));
      return true;
    };
    const host = document.createElement("div");
    host.className = "pm-editor-host";
    editor = new Editor({
      element: host,
      extensions: [
        StarterKit.configure({ link: { openOnClick: false, autolink: true } }),
        TaskList, TaskItem.configure({ nested: true }), TableKit.configure({ table: { resizable: false } }),
        Image.configure({ allowBase64: false }), PMHighlight, PMTextStyle, Color,
        Placeholder.configure({ placeholder }), Markdown,
      ],
      content: markdown, contentType: "markdown",
      editorProps: {
        handlePaste: (view, event) => {
          const files = images(event.clipboardData && event.clipboardData.files);
          if (!files.length) return false;
          event.preventDefault();
          return put(files);
        },
        handleDrop: (view, event, slice, moved) => {
          if (moved) return false;
          const files = images(event.dataTransfer && event.dataTransfer.files);
          if (!files.length) return false;
          event.preventDefault();
          const at = view.posAtCoords({ left: event.clientX, top: event.clientY });
          return put(files, at ? at.pos : null);
        },
      },
      onUpdate: ({ editor }) => onUpdate && onUpdate(editor),
      onBlur: ({ editor }) => onBlur && onBlur(editor),
    });
    editor.pmInsertImages = files => put(images(files));
    if (opts.toolbar !== false) {
      const pick = document.createElement("input");
      pick.type = "file"; pick.accept = "image/png,image/jpeg,image/gif,image/webp"; pick.multiple = true; pick.hidden = true;
      pick.addEventListener("change", () => { put(images(pick.files)); pick.value = ""; });
      el.appendChild(toolbar(editor, { pickImage: upload ? () => pick.click() : null }));
      el.appendChild(pick);
    }
    el.appendChild(host);
    const destroy = editor.destroy.bind(editor);
    editor.destroy = () => { destroy(); el.querySelectorAll(".pm-toolbar, .pm-editor-host, input[type=file]").forEach(n => n.remove()); };
    return editor;
  },
};
