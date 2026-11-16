import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { LucideIcon } from 'lucide-react'
import { Bold, Code, Italic, Link, List, ListIndentDecrease, ListIndentIncrease, ListOrdered, ListTodo, Minus, Quote, Redo2, SquareCode, Strikethrough, Undo2 } from 'lucide-react'
import { EditorContent, useEditor, useEditorState } from '@tiptap/react'
import type { Editor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import { Markdown } from '@tiptap/markdown'
import { TableKit } from '@tiptap/extension-table'
import { TaskItem, TaskList } from '@tiptap/extension-list'
import type { EditorProps } from './textEdit'
import { wheelSideways } from './wheelSideways'

// StarterKit's trailing node adds an empty last paragraph on the first transaction, which only adds trailing newlines; a file ends with one newline.
const markdownOf = (editor: Editor) => editor.getMarkdown().replace(/\n*$/, '\n')

/** Links may be web addresses, mail, anchors or paths. Script and data URLs are refused. */
const unsafeLink = (href: string) => /^\s*(javascript|data|vbscript|file):/i.test(href)

type Block = 'paragraph' | 'h1' | 'h2' | 'h3' | 'h4' | 'other'
const BLOCKS: { value: Block; label: string }[] = [
  { value: 'paragraph', label: 'Paragraph' }, { value: 'h1', label: 'Heading 1' }, { value: 'h2', label: 'Heading 2' },
  { value: 'h3', label: 'Heading 3' }, { value: 'h4', label: 'Heading 4' },
]

function Tool({ label, icon: Icon, active, disabled, onClick }: { label: string; icon: LucideIcon; active?: boolean; disabled?: boolean; onClick: () => void }) {
  // Keep the editor's selection: a toolbar press must not take focus first.
  return <button type="button" className="rich-tool" aria-label={label} title={label} aria-pressed={active === undefined ? undefined : active}
    disabled={disabled} onMouseDown={event => event.preventDefault()} onClick={onClick}><Icon size={16} aria-hidden="true" /></button>
}

const Gap = () => <span className="rich-gap" aria-hidden="true" />

/** The Markdown toolbar: block type, marks, lists, nesting, quote, code block, rule and link. */
function Toolbar({ editor }: { editor: Editor }) {
  const state = useEditorState({
    editor,
    selector: ({ editor: e }) => {
      const heading = [1, 2, 3, 4].find(level => e.isActive('heading', { level }))
      const block: Block = heading ? `h${heading}` as Block : e.isActive('paragraph') ? 'paragraph' : 'other'
      return {
        block, undo: e.can().undo(), redo: e.can().redo(),
        bold: e.isActive('bold'), italic: e.isActive('italic'), strike: e.isActive('strike'), code: e.isActive('code'),
        bullet: e.isActive('bulletList'), ordered: e.isActive('orderedList'), task: e.isActive('taskList'),
        sink: e.can().sinkListItem('listItem') || e.can().sinkListItem('taskItem'),
        lift: e.can().liftListItem('listItem') || e.can().liftListItem('taskItem'),
        quote: e.isActive('blockquote'), codeBlock: e.isActive('codeBlock'),
        link: e.isActive('link'), empty: e.state.selection.empty,
      }
    },
  })
  const [linking, setLinking] = useState<{ href: string; from: number; to: number; error?: string } | null>(null)
  const chain = () => editor.chain().focus()
  const setBlock = (value: Block) => {
    if (value === 'paragraph') chain().setParagraph().run()
    else if (value !== 'other') chain().setHeading({ level: Number(value[1]) as 1 | 2 | 3 | 4 }).run()
  }
  const openLink = () => {
    const { from, to } = editor.state.selection
    const href = editor.getAttributes('link').href
    setLinking({ href: typeof href === 'string' ? href : '', from, to })
  }
  const applyLink = (href: string | null) => {
    if (!linking) return
    const range = { from: linking.from, to: linking.to }
    setLinking(null)
    const run = chain().setTextSelection(range).extendMarkRange('link')
    if (href) run.setLink({ href }).run()
    else run.unsetLink().run()
  }
  return <div ref={wheelSideways} className="rich-toolbar" role="toolbar" aria-label="Formatting">
    <Tool label="Undo" icon={Undo2} disabled={!state.undo} onClick={() => chain().undo().run()} />
    <Tool label="Redo" icon={Redo2} disabled={!state.redo} onClick={() => chain().redo().run()} />
    <Gap />
    <select className="rich-block" aria-label="Text style" value={state.block} onChange={event => setBlock(event.target.value as Block)}>
      {state.block === 'other' && <option value="other" disabled>Text style</option>}
      {BLOCKS.map(block => <option key={block.value} value={block.value}>{block.label}</option>)}
    </select>
    <Gap />
    <Tool label="Bold" icon={Bold} active={state.bold} onClick={() => chain().toggleBold().run()} />
    <Tool label="Italic" icon={Italic} active={state.italic} onClick={() => chain().toggleItalic().run()} />
    <Tool label="Strikethrough" icon={Strikethrough} active={state.strike} onClick={() => chain().toggleStrike().run()} />
    <Tool label="Inline code" icon={Code} active={state.code} onClick={() => chain().toggleCode().run()} />
    <Gap />
    <Tool label="Bullet list" icon={List} active={state.bullet} onClick={() => chain().toggleBulletList().run()} />
    <Tool label="Numbered list" icon={ListOrdered} active={state.ordered} onClick={() => chain().toggleOrderedList().run()} />
    <Tool label="Task list" icon={ListTodo} active={state.task} onClick={() => chain().toggleTaskList().run()} />
    <Tool label="Indent" icon={ListIndentIncrease} disabled={!state.sink}
      onClick={() => { if (!chain().sinkListItem('listItem').run()) chain().sinkListItem('taskItem').run() }} />
    <Tool label="Outdent" icon={ListIndentDecrease} disabled={!state.lift}
      onClick={() => { if (!chain().liftListItem('listItem').run()) chain().liftListItem('taskItem').run() }} />
    <Gap />
    <Tool label="Quote" icon={Quote} active={state.quote} onClick={() => chain().toggleBlockquote().run()} />
    <Tool label="Code block" icon={SquareCode} active={state.codeBlock} onClick={() => chain().toggleCodeBlock().run()} />
    <Tool label="Horizontal rule" icon={Minus} onClick={() => chain().setHorizontalRule().run()} />
    <Tool label="Link" icon={Link} active={state.link} disabled={!state.link && state.empty} onClick={openLink} />
    {linking && <form className="rich-link" aria-label="Link" onSubmit={event => {
      event.preventDefault()
      const href = linking.href.trim()
      if (!href) return
      if (unsafeLink(href)) { setLinking({ ...linking, error: 'Use a web address, a mail address or a file path.' }); return }
      applyLink(href)
    }} onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); event.preventDefault(); setLinking(null); editor.commands.focus() } }}>
      <label htmlFor="rich-link-url">Link address</label>
      <input id="rich-link-url" type="text" inputMode="url" autoFocus autoComplete="off" autoCapitalize="off" spellCheck={false}
        value={linking.href} placeholder="https://example.com" aria-invalid={!!linking.error} aria-describedby={linking.error ? 'rich-link-error' : undefined}
        onChange={event => setLinking({ ...linking, href: event.target.value, error: undefined })} />
      {linking.error && <p id="rich-link-error" role="alert">{linking.error}</p>}
      <div className="rich-link-actions">
        {state.link && <button type="button" className="ghost sm" onClick={() => applyLink(null)}>Remove link</button>}
        <button type="button" className="secondary sm" onClick={() => { setLinking(null); editor.commands.focus() }}>Cancel</button>
        <button type="submit" className="primary sm">Apply</button>
      </div>
    </form>}
  </div>
}

/** Edit Markdown as formatted text with Tiptap. It writes Markdown back, which can restyle unchanged parts of the file. */
export default function RichEditor({ initial, path, handleRef, onDirty, onSave }: EditorProps) {
  const latest = useRef({ onDirty, onSave })
  useLayoutEffect(() => { latest.current = { onDirty, onSave } })
  // Normalisation plugins change the doc without changing the Markdown text.
  const cleanMarkdown = useRef<string | null>(null)
  const dirty = useRef(false)
  const dirtyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const editor = useEditor({
    extensions: [
      StarterKit.configure({ underline: false, link: { openOnClick: false, autolink: true, isAllowedUri: (url, ctx) => !unsafeLink(url) && ctx.defaultValidate(url) } }),
      Markdown.configure({ indentation: { style: 'space', size: 2 } }),
      TableKit.configure({ table: { resizable: false } }),
      TaskList, TaskItem.configure({ nested: true }),
    ],
    content: initial,
    contentType: 'markdown',
    autofocus: 'start',
    immediatelyRender: true,
    editorProps: {
      attributes: { class: 'text-viewer-doc rich-doc', 'aria-label': `Edit ${path.split('/').pop() || path}`, spellcheck: 'true' },
      handleKeyDown: (_view, event) => {
        if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 's') { event.preventDefault(); latest.current.onSave(); return true }
        return false
      },
    },
    onUpdate: ({ editor: e }) => {
      if (dirtyTimer.current !== null) return
      dirtyTimer.current = setTimeout(() => {
        dirtyTimer.current = null
        // An editor can update before its mount effect stores the clean Markdown; it has no edits yet.
        if (cleanMarkdown.current === null) return
        const next = markdownOf(e) !== cleanMarkdown.current
        if (next !== dirty.current) { dirty.current = next; latest.current.onDirty(next) }
      }, 150)
    },
  })
  useEffect(() => {
    cleanMarkdown.current = markdownOf(editor)
    handleRef.current = {
      text: () => markdownOf(editor),
      saved: () => {
        cleanMarkdown.current = markdownOf(editor)
        if (dirtyTimer.current !== null) { clearTimeout(dirtyTimer.current); dirtyTimer.current = null }
        if (dirty.current) { dirty.current = false; latest.current.onDirty(false) }
      },
    }
    return () => {
      handleRef.current = null
      if (dirtyTimer.current !== null) { clearTimeout(dirtyTimer.current); dirtyTimer.current = null }
    }
  }, [editor, handleRef])
  return <div className="rich-editor">
    <Toolbar editor={editor} />
    <EditorContent editor={editor} className="rich-body" />
  </div>
}
