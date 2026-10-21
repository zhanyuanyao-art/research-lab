// Library surface of tiptap-editor.min.js, exposed as window.RLEditor.
// Only libraries live here; the app's schema rules, toolbar and Markdown plugins are in
// web/static/issue-editor.js so they stay readable and reviewable without a rebuild.
export * as core from '@tiptap/core'
export * as model from '@tiptap/pm/model'
export * as state from '@tiptap/pm/state'
export * as view from '@tiptap/pm/view'
export * as transform from '@tiptap/pm/transform'
export { Document } from '@tiptap/extension-document'
export { Paragraph } from '@tiptap/extension-paragraph'
export { Text } from '@tiptap/extension-text'
export { Heading } from '@tiptap/extension-heading'
export { Blockquote } from '@tiptap/extension-blockquote'
export { CodeBlock } from '@tiptap/extension-code-block'
export { HorizontalRule } from '@tiptap/extension-horizontal-rule'
export { HardBreak } from '@tiptap/extension-hard-break'
export { Bold } from '@tiptap/extension-bold'
export { Italic } from '@tiptap/extension-italic'
export { Strike } from '@tiptap/extension-strike'
export { Underline } from '@tiptap/extension-underline'
export { Code } from '@tiptap/extension-code'
export { Link } from '@tiptap/extension-link'
export { BulletList, OrderedList, ListItem, ListKeymap, TaskList, TaskItem } from '@tiptap/extension-list'
export { Details, DetailsSummary, DetailsContent } from '@tiptap/extension-details'
export { Placeholder, UndoRedo, Dropcursor, Gapcursor, TrailingNode } from '@tiptap/extensions'
export { default as MarkdownIt } from 'markdown-it'
export { MarkdownSerializer, MarkdownSerializerState } from 'prosemirror-markdown'
// Filled in by build.mjs from the installed packages (esbuild `define`).
export const versions = __RL_EDITOR_VERSIONS__
