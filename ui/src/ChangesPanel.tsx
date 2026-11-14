import { RetryButton } from './Retry'
import { CodeBones, SkeletonRows } from './Skeleton'
import { rememberCount, rememberedCount } from './skeletonCounts'
import { useCallback, useEffect, useId, useState } from 'react'
import { ArrowDown, ArrowUp, ChevronRight, GitBranch } from 'lucide-react'
import { ApiError, errorText } from './api'
import type { Api } from './api'
import { folderQuery, usePanelPoll } from './panelProps'
import type { PanelProps } from './panelProps'

// GET repo/changes. mode 'repo': the folder is in one repository. 'parent': every repository found below the folder.
export interface ChangedFile { path: string; status: string; staged: boolean; unstaged: boolean; from?: string; added?: number | null; deleted?: number | null; binary?: boolean }
export interface RepoChanges { id: string; name: string; path: string; branch: string | null; ahead: number | null; behind: number | null; upstream: string | null; files: ChangedFile[]; truncated: boolean; error?: string }
export interface Changes { folder: string; mode: 'repo' | 'parent'; repos: RepoChanges[]; truncated: boolean }
interface FileDiff { path: string; diff: string; binary: boolean; truncated: boolean; note: string | null }
// Untracked files show U (as in editors); a merge conflict shows !.
const STATUS: Record<string, string> = { M: 'Modified', A: 'Added', D: 'Deleted', R: 'Renamed', C: 'Copied', U: 'Conflict', '?': 'Untracked' }
const POLL_MS = 6000

// The repository choice for parent folders lives in the synced prefs ('repos': {folder: [repo id, ...]}), so phone and desktop share it.
// No entry = All repos. The panel reads and writes only its own key; the server merges per folder.
function useRepoChoice(api: Api, folder: string | undefined) {
  const [chosen, setChosen] = useState<Record<string, string[]>>({})
  useEffect(() => {
    if (!folder) return
    const controller = new AbortController()
    api<{ prefs: { repos?: Record<string, string[]> } }>('prefs', undefined, controller.signal)
      .then(stored => setChosen(stored.prefs.repos ?? {}), () => {})
    return () => controller.abort()
  }, [api, folder])
  const choose = useCallback((ids: string[]) => {
    if (!folder) return
    setChosen(current => ({ ...current, [folder]: ids }))
    void api('prefs', { changes: { repos: { [folder]: ids } } }).catch(() => {})
  }, [api, folder])
  return [folder ? chosen[folder] ?? [] : [], choose] as const
}

export function ChangesPanel({ api, pane, folder, active }: PanelProps) {
  const [data, setData] = useState<Changes | null>(null)
  const [error, setError] = useState('')
  const query = folderQuery(pane, folder)
  const load = useCallback(async (signal: AbortSignal) => {
    try {
      const result = await api<Changes>(`repo/changes?${query}`, undefined, signal)
      if (!signal.aborted) { setData(result); setError('') }
    } catch (failure) { if (!signal.aborted) setError(errorText(failure)) }
  }, [api, query])
  const [previousQuery, setPreviousQuery] = useState(query)
  if (query !== previousQuery) { setPreviousQuery(query); setData(null); setError('') }
  usePanelPoll(load, POLL_MS, active)
  const [picked, choose] = useRepoChoice(api, data?.folder)
  useEffect(() => { if (data) rememberCount('changes.rows', data.repos.reduce((sum, repo) => sum + repo.files.length, 0)) }, [data])

  if (!data) return <div className="repo-panel">{error ? <p className="repo-note" role="alert">{error}</p> : <SkeletonRows count={Math.max(1, rememberedCount('changes.rows', 4, 10))} label="Loading changes…" className="repo-skeleton" icon={14} meta={false} />}</div>
  if (!data.repos.length) return <div className="repo-panel"><div className="repo-empty">
    <GitBranch size={20} aria-hidden="true" /><p><strong>Changes need a Git repository</strong>None was found in this folder.</p>
  </div></div>
  // A chosen repo that is gone no longer counts; when none is left, show all.
  const known = picked.filter(id => data.repos.some(repo => repo.id === id))
  const shown = data.mode === 'parent' && known.length ? data.repos.filter(repo => known.includes(repo.id)) : data.repos
  const changed = shown.filter(repo => repo.files.length || repo.error)
  return <div className="repo-panel">
    {error && <p className="repo-note" role="alert">{error}</p>}
    {data.repos.length > 1 && <RepoPicker repos={data.repos} picked={known} choose={choose} />}
    {changed.length ? changed.map(repo => <RepoGroup key={repo.id} repo={repo} api={api} query={query} heading={data.mode === 'parent'} />)
      : <div className="repo-empty calm"><p><strong>No changes</strong>{shown.length === 1 ? `${shown[0].name} matches its last commit.` : 'Every repository matches its last commit.'}</p></div>}
    {data.mode === 'repo' && changed.length > 0 && <BranchLine repo={data.repos[0]} />}
    {data.truncated && <p className="repo-note">Tether stopped looking after {data.repos.length} repositories.</p>}
  </div>
}

function BranchLine({ repo }: { repo: RepoChanges }) {
  return <p className="repo-branch"><GitBranch size={13} aria-hidden="true" />{repo.branch ?? 'Detached HEAD'}<Sync repo={repo} /></p>
}

function Sync({ repo }: { repo: RepoChanges }) {
  if (!repo.ahead && !repo.behind) return null
  const label = [repo.ahead ? `${repo.ahead} ahead` : '', repo.behind ? `${repo.behind} behind` : ''].filter(Boolean).join(', ')
  return <span className="repo-sync" aria-label={label} title={`${label} ${repo.upstream ?? ''}`.trim()}>
    {!!repo.ahead && <span><ArrowUp size={11} aria-hidden="true" />{repo.ahead}</span>}
    {!!repo.behind && <span><ArrowDown size={11} aria-hidden="true" />{repo.behind}</span>}
  </span>
}

function RepoPicker({ repos, picked, choose }: { repos: RepoChanges[]; picked: string[]; choose: (ids: string[]) => void }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const all = !picked.length
  const label = all ? `All repos (${repos.length})` : picked.length === 1 ? repos.find(repo => repo.id === picked[0])?.name ?? '1 repo' : `${picked.length} of ${repos.length} repos`
  const toggle = (repo: string) => {
    const current = all ? repos.map(item => item.id) : picked
    const next = current.includes(repo) ? current.filter(item => item !== repo) : [...current, repo]
    if (next.length) choose(next.length === repos.length ? [] : next)  // at least one repo stays chosen
  }
  return <div className="repo-picker">
    <button type="button" className="repo-picker-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
      <span>{label}</span><ChevronRight className="chevron" size={14} aria-hidden="true" />
    </button>
    {open && <fieldset id={id}>
      <legend className="sr-only">Repositories to show</legend>
      <label><input type="checkbox" checked={all} onChange={() => choose([])} disabled={all} />All repos</label>
      {repos.map(repo => <label key={repo.id}>
        <input type="checkbox" checked={all || picked.includes(repo.id)} disabled={!all && picked.length === 1 && picked[0] === repo.id} onChange={() => toggle(repo.id)} />
        <span className="repo-picker-name">{repo.id}</span>
        {repo.files.length > 0 && <span className="repo-count">{repo.files.length}</span>}
      </label>)}
    </fieldset>}
  </div>
}

function RepoGroup({ repo, api, query, heading }: { repo: RepoChanges; api: Api; query: string; heading: boolean }) {
  const [open, setOpen] = useState<string | null>(null)
  const id = useId()
  return <section className="repo-group" aria-labelledby={heading ? id : undefined} aria-label={heading ? undefined : 'Changed files'}>
    {heading && <h3 id={id} className="repo-group-head"><span className="repo-group-name" title={repo.path}>{repo.id}</span>
      <span className="repo-group-branch">{repo.branch ?? 'detached'}</span><Sync repo={repo} /></h3>}
    {repo.error ? <p className="repo-note" role="alert">{repo.error}</p> : <ul className="repo-files">
      {repo.files.map(file => <FileRow key={file.path} file={file} open={open === file.path} toggle={() => setOpen(open === file.path ? null : file.path)} api={api} query={`${query}&repo=${encodeURIComponent(repo.id)}`} />)}
    </ul>}
    {repo.truncated && <p className="repo-note">Showing the first {repo.files.length} files.</p>}
  </section>
}

function FileRow({ file, open, toggle, api, query }: { file: ChangedFile; open: boolean; toggle: () => void; api: Api; query: string }) {
  const id = useId()
  const slash = file.path.replace(/\/$/, '').lastIndexOf('/')
  const name = file.path.slice(slash + 1)
  const dir = slash > 0 ? file.path.slice(0, slash + 1) : ''
  const folderEntry = file.path.endsWith('/')
  const status = STATUS[file.status] ?? file.status
  const counts = file.binary ? 'binary' : [file.added ? `${file.added} added` : '', file.deleted ? `${file.deleted} removed` : ''].filter(Boolean).join(', ')
  return <li>
    <button type="button" className="repo-file" aria-expanded={folderEntry ? undefined : open} aria-controls={open ? id : undefined} disabled={folderEntry}
      aria-label={`${file.path}, ${status}${file.staged ? ', staged' : ''}${counts ? `, ${counts}` : ''}`} onClick={toggle}>
      <span className={`repo-status s-${file.status === '?' ? 'new' : file.status}`} aria-hidden="true">{file.status === '?' ? 'U' : file.status === 'U' ? '!' : file.status}</span>
      <span className="repo-file-name" title={file.from ? `${file.from} → ${file.path}` : file.path}>{name}{dir && <span>{dir}</span>}</span>
      <span className="repo-counts" aria-hidden="true">{file.binary ? <span>bin</span> : <>
        {!!file.added && <span className="add">+{file.added}</span>}{!!file.deleted && <span className="del">−{file.deleted}</span>}</>}</span>
    </button>
    {open && <FileDiffView id={id} api={api} query={`${query}&path=${encodeURIComponent(file.path)}`} label={file.path} />}
  </li>
}

// The hunks only: the file header lines (diff --git, index, ---/+++) repeat what the row already says.
const body = (diff: string) => { const lines = diff.replace(/\n$/, '').split('\n'); const first = lines.findIndex(line => line.startsWith('@@')); return first < 0 ? lines : lines.slice(first) }

function FileDiffView({ id, api, query, label }: { id: string; api: Api; query: string; label: string }) {
  const [state, setState] = useState<{ query: string; diff?: FileDiff; error?: string; final?: boolean; attempt: number } | null>(null)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    api<FileDiff>(`repo/diff?${query}`, undefined, controller.signal)
      .then(diff => { if (!controller.signal.aborted) setState({ query, diff, attempt }) }, failure => { if (!controller.signal.aborted) setState({ query, attempt, error: errorText(failure), final: failure instanceof ApiError && failure.status === 404 }) })
    return () => controller.abort()
  }, [api, query, attempt])
  const current = state?.query === query ? state : null
  return <div id={id} className="repo-diff">
    {current?.error ? <p className="repo-note" role="alert">{current.error}{!current.final && <> <RetryButton label="Retry" className="repo-retry" busy={current.attempt !== attempt} onRetry={() => setAttempt(attempt + 1)} /></>}</p>
      : !current?.diff ? <CodeBones lines={6} label="Loading diff…" />
      : current.diff.note && !current.diff.diff ? <p className="repo-note">{current.diff.note}</p>
      : <pre tabIndex={0} aria-label={`Changes in ${label}`}><code>{body(current.diff.diff).map((line, at) =>
        <span key={at} className={line.startsWith('@@') ? 'diff-hunk' : line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : undefined}>{line || ' '}{'\n'}</span>)}</code></pre>}
    {current?.diff?.truncated && <p className="repo-note">Diff cut at 400 KB.</p>}
  </div>
}
