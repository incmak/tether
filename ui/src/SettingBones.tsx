import type { ReactNode } from 'react'
import { Bone, Skeleton } from './Skeleton'

// Loading shapes for the settings sections. Each bone sits inside the real class (setting-note, switch-row,
// autonomy-level, helper-row), so it takes that line height and the card does not jump when the data lands.

const WIDTHS = ['92%', '64%', '78%', '56%', '84%', '70%']

/** Text lines inside one element. The last line is shorter, like wrapped prose. */
/** `phone` adds lines that show only on narrow screens, where the real text wraps more. */
export function BoneLines({ count = 1, phone = 0, last = '62%', from = 0 }: { count?: number; phone?: number; last?: string; from?: number }) {
  const total = count + phone
  return <>{Array.from({ length: total }, (_, index) => <span key={index} className={index >= count ? 'bone-phone' : undefined}>{index > 0 && <br />}
    <Bone w={index === total - 1 ? (total > 1 ? last : WIDTHS[(from + index) % WIDTHS.length]) : '100%'} /></span>)}</>
}

export function NoteBone({ lines = 2, phone = 1, className = '' }: { lines?: number; phone?: number; className?: string }) {
  return <p className={`setting-note ${className}`.trim()}><BoneLines count={lines} phone={phone} /></p>
}

/** A switch row: a title, a note and (optionally) the switch. */
export function SwitchBone({ lines = 1, phone = 1, control = 'switch', from = 0 }: { lines?: number; phone?: number; control?: 'switch' | 'none'; from?: number }) {
  return <div className="switch-row"><span><strong><Bone w={from % 2 ? '7em' : '9em'} /></strong><small><BoneLines count={lines} phone={phone} from={from} /></small></span>
    {control === 'switch' && <Bone w={46} h={28} className="bone-switch" />}</div>
}

/** The five autonomy levels: a number badge, a name and one or two lines. */
export function LevelBones({ count = 5 }: { count?: number }) {
  return <div className="autonomy-levels">{Array.from({ length: count }, (_, index) => <div key={index} className="autonomy-level">
    <Bone w={28} h={28} round />
    <span className="autonomy-level-text"><strong><Bone w={WIDTHS[(index + 3) % WIDTHS.length]} className="bone-name" /></strong><small><BoneLines count={index % 2 ? 1 : 2} phone={2} from={index} /></small></span>
  </div>)}</div>
}

/** The Codex level stepper: a legend, one segment per level and a two-line note. */
export function StepperBone({ count = 6 }: { count?: number }) {
  return <fieldset className="choice codex-level" disabled><legend>Level</legend>
    <div className="segmented">{Array.from({ length: count }, (_, index) => <label key={index}><span><Bone w="1ch" /></span></label>)}</div>
    <p className="setting-note codex-level-note"><BoneLines count={2} /></p>
  </fieldset>
}

/** Model, effort and fast mode for Codex runs: two select rows and a switch. */
export function RunOptionsBone() {
  return <fieldset className="choice codex-run-options" disabled><legend>How Claude runs Codex</legend>
    {['Model', 'Effort'].map(name => <div key={name} className="project-size-row"><label>{name}</label><Bone h="var(--control, 36px)" className="bone-select" /></div>)}
    <SwitchBone from={1} />
  </fieldset>
}

/** Rows like the helper and lesson lists: a title, one note line, a chevron. */
export function RowBones({ count, className }: { count: number; className: string }) {
  return <ul className={className}>{Array.from({ length: count }, (_, index) => <li key={index}><div className="helper-row">
    <span><strong><Bone w={index % 2 ? '6em' : '8em'} /></strong><small><BoneLines count={1} phone={1} from={index} /></small></span>
  </div></li>)}</ul>
}

/** A settings card while it loads. Screen readers hear `label` once. */
export function SettingSkeleton({ label, children }: { label: string; children: ReactNode }) {
  return <Skeleton label={label} className="setting-bones"><div aria-hidden="true">{children}</div></Skeleton>
}
