import { useEffect } from 'react'
import type { ChangeEvent, InputHTMLAttributes, Ref, TextareaHTMLAttributes } from 'react'
import { Mic, Square, X } from 'lucide-react'
import { useDictation } from './useDictation'
import { usePrefs } from './prefs'

type Props = {
  value: string; setValue: (value: string) => void; label: string
  multiline?: boolean; fieldRef?: Ref<HTMLTextAreaElement | HTMLInputElement>
  field: InputHTMLAttributes<HTMLInputElement> & TextareaHTMLAttributes<HTMLTextAreaElement>
  onLeave?: () => void; onBusyChange?: (busy: boolean) => void
}

// Keep the mic beside its field, including while an inline title is edited.
export function VoiceField({ value, setValue, label, multiline, fieldRef, field, onLeave, onBusyChange }: Props) {
  // The app owns the zone. A field can mount before its own prefs request returns.
  const [prefs] = usePrefs({ syncZone: false })
  const dictation = useDictation({ text: value, setText: setValue, chime: prefs.voiceSound })
  const { listening, transcribing, canRecord } = dictation
  useEffect(() => { onBusyChange?.(listening || transcribing); return () => onBusyChange?.(false) }, [listening, transcribing, onBusyChange])
  const props = { ...field, value, readOnly: field.readOnly || listening || transcribing, onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setValue(event.target.value) }
  return <div className="voice-field" onKeyDownCapture={event => { if ((listening || transcribing) && event.key === 'Enter' && (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement)) { event.preventDefault(); event.stopPropagation() } }} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget) && !listening && !transcribing) onLeave?.()
  }}>
    {multiline ? <textarea {...props} ref={fieldRef as Ref<HTMLTextAreaElement>} /> : <input {...props} ref={fieldRef as Ref<HTMLInputElement>} />}
    {!prefs.voiceOff && canRecord && <button type="button" className={`icon-button mic ${listening || transcribing ? 'active' : ''}`}
      aria-label={transcribing ? `Stop converting ${label}` : listening ? `Stop recording ${label}` : `Dictate ${label}`}
      aria-pressed={listening} disabled={field.disabled || field.readOnly} title={transcribing ? 'Keep the live text' : listening ? 'Stop recording' : `Dictate ${label}`}
      onMouseDown={event => event.preventDefault()} onClick={() => { if (transcribing) dictation.cancelConverting(); else void dictation.toggle() }}>
      {transcribing ? <X size={19} /> : listening ? <Square size={18} /> : <Mic size={20} />}
    </button>}
    {(listening || transcribing) && <span className="composer-status" role="status">{transcribing ? 'Converting…' : `${dictation.seconds}s`}</span>}
    {dictation.error && <p className="error" role="alert">{dictation.error}</p>}
  </div>
}
