import { useEffect, useLayoutEffect, useRef } from 'react'
import { basicSetup } from 'codemirror'
import { Compartment, EditorSelection, EditorState } from '@codemirror/state'
import type { Text } from '@codemirror/state'
import { EditorView, keymap } from '@codemirror/view'
import { indentWithTab } from '@codemirror/commands'
import { HighlightStyle, LanguageDescription, syntaxHighlighting } from '@codemirror/language'
import { languages } from '@codemirror/language-data'
import { tags } from '@lezer/highlight'
import type { EditorProps } from './textEdit'

// Colours come from TextEditor.css, so the editor follows the light and dark themes.
const theme = EditorView.theme({
  '&': { height: '100%', backgroundColor: 'var(--bg)', color: 'var(--text)', fontSize: '12.5px' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': { fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace', lineHeight: '1.55', paddingBottom: 'env(safe-area-inset-bottom)' },
  '.cm-content': { caretColor: 'var(--text)', padding: '10px 0 24px' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--text)' },
  '.cm-gutters': { backgroundColor: 'var(--bg)', color: 'var(--muted)', border: 'none' },
  '.cm-lineNumbers .cm-gutterElement': { padding: '0 8px 0 12px' },
  '.cm-activeLine': { backgroundColor: 'var(--cm-active)' },
  '.cm-activeLineGutter': { backgroundColor: 'transparent', color: 'var(--text)' },
  '&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground': { backgroundColor: 'var(--cm-selection)' },
  '.cm-selectionMatch': { backgroundColor: 'var(--cm-match)' },
  '.cm-searchMatch': { backgroundColor: 'var(--cm-match)', outline: '1px solid var(--line)' },
  '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: 'var(--cm-selection)' },
  '&.cm-focused .cm-matchingBracket': { backgroundColor: 'var(--cm-match)', outline: '1px solid var(--line)' },
  '.cm-panels': { backgroundColor: 'var(--surface)', color: 'var(--text)' },
  '.cm-panels.cm-panels-top': { borderBottom: '1px solid var(--line)' },
  '.cm-panels.cm-panels-bottom': { borderTop: '1px solid var(--line)' },
  '.cm-panel.cm-search': { padding: '6px 10px', fontSize: '13px' },
  '.cm-panel input, .cm-panel button': { font: 'inherit', fontSize: '13px', color: 'var(--text)' },
  '.cm-textfield': { border: '1px solid var(--line)', borderRadius: 'var(--radius-sm)', background: 'var(--bg)', padding: '3px 6px' },
  '.cm-button': { border: '1px solid var(--line)', borderRadius: 'var(--radius-sm)', backgroundImage: 'none', background: 'var(--surface)', padding: '3px 8px' },
  '.cm-tooltip': { backgroundColor: 'var(--surface)', color: 'var(--text)', border: '1px solid var(--line)', borderRadius: 'var(--radius-sm)' },
  '.cm-tooltip-autocomplete > ul > li[aria-selected]': { backgroundColor: 'var(--cm-selection)', color: 'var(--text)' },
  '.cm-foldPlaceholder': { backgroundColor: 'var(--hover)', border: 'none', color: 'var(--muted)' },
})

const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.operatorKeyword, tags.modifier, tags.controlKeyword], color: 'var(--cm-keyword)' },
  { tag: [tags.string, tags.special(tags.string), tags.regexp], color: 'var(--cm-string)' },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], color: 'var(--cm-number)' },
  { tag: [tags.comment, tags.meta], color: 'var(--muted)', fontStyle: 'italic' },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: 'var(--cm-function)' },
  { tag: [tags.typeName, tags.className, tags.namespace], color: 'var(--cm-type)' },
  { tag: [tags.propertyName, tags.attributeName], color: 'var(--cm-property)' },
  { tag: [tags.tagName], color: 'var(--cm-keyword)' },
  { tag: tags.heading, fontWeight: '600', color: 'var(--cm-keyword)' },
  { tag: tags.emphasis, fontStyle: 'italic' },
  { tag: tags.strong, fontWeight: '600' },
  { tag: tags.strikethrough, textDecoration: 'line-through' },
  { tag: [tags.link, tags.url], color: 'var(--accent-ink)', textDecoration: 'underline' },
  { tag: tags.monospace, color: 'var(--cm-string)' },
  { tag: tags.invalid, color: 'var(--error)' },
])

/** Prose files wrap their lines; code keeps one line per row. */
const wraps = (path: string) => /\.(md|markdown|txt|text|rst|adoc|org)$/i.test(path) || !/\.[^/]+$/.test(path)

/** Edit a text or code file in CodeMirror 6, with syntax colours from its name. Cmd/Ctrl+S saves. */
export default function SourceEditor({ initial, path, line, handleRef, onDirty, onSave }: EditorProps) {
  const holder = useRef<HTMLDivElement>(null)
  const latest = useRef({ onDirty, onSave })
  useLayoutEffect(() => { latest.current = { onDirty, onSave } })
  useEffect(() => {
    if (!holder.current) return
    const language = new Compartment()
    let clean: Text
    let dirty = false
    const view = new EditorView({
      parent: holder.current,
      state: EditorState.create({
        doc: initial,
        extensions: [
          keymap.of([{ key: 'Mod-s', preventDefault: true, run: () => { latest.current.onSave(); return true } }, indentWithTab]),
          basicSetup, theme, syntaxHighlighting(highlight), language.of([]),
          wraps(path) ? EditorView.lineWrapping : [],
          EditorView.contentAttributes.of({ 'aria-label': `Edit ${path.split('/').pop() || path}`, autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false' }),
          EditorView.updateListener.of(update => {
            if (!update.docChanged) return
            const next = !update.state.doc.eq(clean)
            if (next !== dirty) { dirty = next; latest.current.onDirty(next) }
          }),
        ],
      }),
    })
    clean = view.state.doc
    handleRef.current = {
      text: () => view.state.doc.toString(),
      saved: () => { clean = view.state.doc; if (dirty) { dirty = false; latest.current.onDirty(false) } },
    }
    if (line && line <= view.state.doc.lines) {
      const at = view.state.doc.line(line).from
      view.dispatch({ selection: EditorSelection.cursor(at), effects: EditorView.scrollIntoView(at, { y: 'center' }) })
    }
    view.focus()
    let live = true
    const name = path.split('/').pop() || path
    void LanguageDescription.matchFilename(languages, name)?.load().then(support => {
      if (live) view.dispatch({ effects: language.reconfigure(support) })
    }).catch(() => {})
    return () => { live = false; handleRef.current = null; view.destroy() }
    // The editor mounts once per file; the viewer remounts it with a key when the text must start over.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return <div ref={holder} className="source-editor" />
}
