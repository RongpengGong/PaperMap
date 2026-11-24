import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { Placeholder } from "@tiptap/extensions";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import { TableKit } from "@tiptap/extension-table";

// One small global for PaperMap's note editors: Markdown in, Markdown out, live formatting while typing.
window.PMEditor = {
  create(el, markdown, placeholder, onUpdate, onBlur) {
    return new Editor({
      element: el,
      extensions: [
        StarterKit.configure({ link: { openOnClick: false, autolink: true } }),
        TaskList, TaskItem.configure({ nested: true }), TableKit.configure({ table: { resizable: false } }),
        Placeholder.configure({ placeholder }), Markdown,
      ],
      content: markdown, contentType: "markdown",
      onUpdate: ({ editor }) => onUpdate && onUpdate(editor),
      onBlur: ({ editor }) => onBlur && onBlur(editor),
    });
  },
};
