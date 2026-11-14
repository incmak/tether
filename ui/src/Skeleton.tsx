import { LoaderCircle } from 'lucide-react'
import type { CSSProperties, ReactNode } from 'react'
import './Skeleton.css'

// One loading system for the whole UI.
// - Bone: a placeholder shape. Put it inside the real text element (same class), so it takes that line height and nothing jumps when the text lands.
// - Skeleton: a placeholder area made of bones. It holds its space at once and fades in after 180 ms, so a fast load does not flash.
// - Loading: a one-line note with a spinner, for small areas where a skeleton has no shape to copy. Same 180 ms delay.
// - Spinner: an action in flight (a button, an upload). It shows at once, because the person just pressed something.

type Size = number | string

/** A placeholder shape. Without a height it is a text line: .75em tall, on the parent's line box. */
export function Bone({ w, h, round, className }: { w?: Size; h?: Size; round?: boolean; className?: string }) {
  const style: CSSProperties = { width: w, height: h }
  return <span className={['bone', round && 'round', className].filter(Boolean).join(' ')} style={style} aria-hidden="true" />
}

/** A placeholder area. Screen readers hear `label` once; the bones are hidden from them. */
export function Skeleton({ label, className, children }: { label: string; className?: string; children: ReactNode }) {
  return <div className={['skeleton', className].filter(Boolean).join(' ')} aria-busy="true"><span className="sk-label" role="status">{label}</span>{children}</div>
}

/** A short note with a spinner. `className` keeps the host's own note style (padding, colour, alignment). */
export function Loading({ label = 'Loading…', className, as: Tag = 'p' }: { label?: string; className?: string; as?: 'p' | 'div' | 'li' | 'span' }) {
  return <Tag className={['loading-note', className].filter(Boolean).join(' ')} role="status"><Spinner />{label}</Tag>
}

/** An action in flight. With `label` it names itself; otherwise the text beside it does. */
export function Spinner({ size = 14, label, className }: { size?: number; label?: string; className?: string }) {
  return <LoaderCircle size={size} className={['spinner', className].filter(Boolean).join(' ')} aria-hidden={label ? undefined : true} aria-label={label} role={label ? 'img' : undefined} />
}

/** Lines of code or log output, inside the host's real `pre` class so the box, font and padding match. */
export function CodeBones({ lines, label, className }: { lines: number; label: string; className?: string }) {
  const widths = ['46%', '82%', '64%', '90%', '38%', '72%', '58%']
  return <Skeleton label={label} className="sk-code"><pre className={className} aria-hidden="true"><code>
    {Array.from({ length: lines }, (_, index) => <span key={index}><Bone w={widths[index % widths.length]} /></span>)}
  </code></pre></Skeleton>
}

/** List rows: an icon and one or two lines, like a sidebar or picker row. Row height comes from `className`. */
export function SkeletonRows({ count, label, className, icon = 16, meta = true }: { count: number; label: string; className?: string; icon?: number | false; meta?: boolean }) {
  const widths = ['72%', '58%', '84%', '64%', '76%', '50%']
  return <Skeleton label={label} className={['sk-rows', className].filter(Boolean).join(' ')}><ul aria-hidden="true">
    {Array.from({ length: count }, (_, index) => <li key={index} className="sk-row">
      {icon !== false && <Bone w={icon} h={icon} className="sk-icon" />}
      <span className="sk-text"><span className="sk-title"><Bone w={widths[index % widths.length]} /></span>{meta && <span className="sk-meta"><Bone w="38%" /></span>}</span>
    </li>)}
  </ul></Skeleton>
}
