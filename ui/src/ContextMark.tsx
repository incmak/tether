import { Gauge } from 'lucide-react'
import type { contextWarning } from './usage'

export function ContextMark({ warning }: { warning: ReturnType<typeof contextWarning> }) {
  if (!warning) return null
  return <span className="context-mark" title={warning.label}>
    <Gauge size={12} aria-hidden="true" /><span aria-hidden="true">{warning.value}</span>
    <span className="visually-hidden">, {warning.label}</span>
  </span>
}
