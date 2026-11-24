import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { Placeholder } from "@tiptap/extensions";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import { TableKit } from "@tiptap/extension-table";
import Image from "@tiptap/extension-image";

const images = list => [...(list || [])].filter(f => f.type && f.type.startsWith("image/"));

// One small global for PaperMap's note editors: Markdown in, Markdown out, live formatting while typing.
// upload(file) -> Promise<{src, alt}> stores a pasted / dropped picture and returns its Markdown path.
window.PMEditor = {
  create(el, markdown, placeholder, onUpdate, onBlur, upload) {
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
    editor = new Editor({
      element: el,
      extensions: [
        StarterKit.configure({ link: { openOnClick: false, autolink: true } }),
        TaskList, TaskItem.configure({ nested: true }), TableKit.configure({ table: { resizable: false } }),
        Image.configure({ allowBase64: false }),
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
    return editor;
  },
};
