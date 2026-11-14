import { Bone, Skeleton } from './Skeleton'
import { NoteBone } from './SettingBones'
import { rememberCount, rememberedCount } from './skeletonCounts'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Api } from './api'
import { errorText } from './api'
import { usePolling } from './hooks'
import { LiveTerminal } from './Terminal'

// 80 columns at the smallest terminal font (9 px) need about this many pixels.
const COMPACT_BELOW = 480

interface Group {
  kind: string; name: string; cpu_pct: number | null; cpu_limit: number | null
  mem_bytes: number | null; mem_limit: number | null; io_read: number; io_write: number; pids: number | null
}
interface Stats {
  host: { cpu_pct: number | null; cpus: number; mem_bytes: number | null; mem_total: number | null; disk_bytes: number | null; disk_total: number | null; load: number[]; uptime: number | null }
  groups: Group[]; btop: boolean; glances?: boolean; processes?: { pid: number; name: string; cpu_pct: number }[]
}
const bytes = (value: number | null) => value == null ? '—' : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GB` : `${Math.round(value / 1024 ** 2)} MB`
const percent = (value: number | null) => value == null ? '—' : `${value.toFixed(1)}%`
const cpuUsage = (value: number | null, cores: number) => value == null ? '—' : `${(value / 100).toFixed(1)} cores · ${Math.round(value / cores)}%`
function Memory({ used, total, hostTotal }: { used: number | null; total: number | null; hostTotal: number | null }) {
  const ceiling = total ?? hostTotal
  return <><span>{bytes(used)} / {total == null ? 'unlimited' : bytes(total)}</span>{used != null && ceiling != null && ceiling > 0 && <div className="resource-meter" role="meter" aria-label={total == null ? "Memory used relative to host" : "Memory used"} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, used / ceiling * 100)}><span style={{ width: `${Math.min(100, used / ceiling * 100)}%` }} /></div>}</>
}
export function HostResources({ api, terminal }: { api: Api; terminal: boolean }) {
  const fetcher = useCallback((signal: AbortSignal) => api<Stats>('host-stats', undefined, signal), [api])
  const { data, error } = usePolling(fetcher, 5000)
  const groups = rememberedCount('resources.groups', 4, 12)
  useEffect(() => { if (data) rememberCount('resources.groups', data.groups.length) }, [data])
  const [shell, setShell] = useState<string | null>(null)
  const [showTerminal, setShowTerminal] = useState(false)
  const [busy, setBusy] = useState(false)
  const [shellError, setShellError] = useState('')
  const monitor = useRef<{ live: boolean; pane: string | null; compact: boolean }>({ live: true, pane: null, compact: false })
  const section = useRef<HTMLElement>(null)
  useEffect(() => {
    const own = monitor.current
    own.live = true
    return () => {
      own.live = false
      if (own.pane) void api('side-shell/end', { pane: own.pane }).catch(() => {})
    }
  }, [api])
  async function open() {
    setBusy(true); setShellError('')
    try {
      // btop needs 80 columns for every box. A narrow screen gets its CPU and process boxes (60 columns) at a readable font.
      const compact = (section.current?.clientWidth ?? 0) < COMPACT_BELOW
      const own = monitor.current
      const keep = own.pane && own.compact === compact ? shell : null
      if (own.pane && !keep) void api('side-shell/end', { pane: own.pane }).catch(() => {})
      const pane = (await api<{ pane: string }>('side-shell', { program: 'btop', pane: keep, compact })).pane
      if (!monitor.current.live) { void api('side-shell/end', { pane }).catch(() => {}); return }
      own.pane = pane; own.compact = compact
      setShell(pane); setShowTerminal(true)
    } catch (failure) { if (monitor.current.live) setShellError(errorText(failure)) } finally { if (monitor.current.live) setBusy(false) }
  }
  return <section ref={section} className="settings-group host-resources" aria-labelledby="settings-resources">
    <div className="resource-heading"><h3 id="settings-resources">Resources</h3>{data?.btop && terminal && <button type="button" className="secondary" disabled={busy} onClick={() => showTerminal ? setShowTerminal(false) : void open()}>{showTerminal ? 'Back to resources' : busy ? 'Opening…' : 'Open btop'}</button>}</div>
    {error && <p className="error" role="alert">{error}</p>}{shellError && <p className="error" role="alert">{shellError}</p>}
    {showTerminal && shell ? <div className="resources-terminal"><LiveTerminal paneId={shell} onChat={() => setShowTerminal(false)} shell fixed /></div> : data ? <>
      <dl className="resource-summary"><div><dt>CPU</dt><dd>{data.host.cpu_pct == null ? '—' : `${Math.round(data.host.cpu_pct)}% of ${data.host.cpus} cores`}</dd></div><div><dt>Memory</dt><dd>{bytes(data.host.mem_bytes)}<small> / {bytes(data.host.mem_total)}</small></dd></div><div><dt>Disk</dt><dd>{bytes(data.host.disk_bytes)}<small> / {bytes(data.host.disk_total)}</small></dd></div></dl>
      <p className="setting-note">Load {data.host.load.map(n => n.toFixed(2)).join(' · ') || '—'} · Uptime {data.host.uptime == null ? '—' : `${Math.floor(data.host.uptime / 3600)}h`}</p>
      <table className="resource-table"><caption className="sr-only">Host groups sorted by CPU. CPU shows cores used and the share of the CPU limit or host cores. Memory excludes file cache the kernel can free.</caption><thead><tr><th scope="col">Group</th><th scope="col">CPU</th><th scope="col" title="Excludes file cache the kernel can free">Memory</th></tr></thead><tbody>
        {[...data.groups].sort((a, b) => (b.cpu_pct ?? -1) - (a.cpu_pct ?? -1) || a.name.localeCompare(b.name)).map(group => <tr key={`${group.kind}:${group.name}`}>
          <th scope="row"><span className="resource-kind">{group.kind}</span><span className="resource-name" title={group.name}>{group.name}</span></th>
          <td className="resource-cpu" title={`${group.cpu_limit ?? data.host.cpus} CPU cores allowed`}>{cpuUsage(group.cpu_pct, group.cpu_limit ?? data.host.cpus)}</td>
          <td className="resource-memory"><Memory used={group.mem_bytes} total={group.mem_limit} hostTotal={data.host.mem_total} /></td>
        </tr>)}
      </tbody></table>
      {!data.groups.length && <p className="setting-note">No cgroup v2 groups found.</p>}
      {data.glances && <div className="glances-extras"><h3>Glances detected</h3>{data.processes?.length ? <ul>{data.processes.map(process => <li key={process.pid}><span>{process.name}</span><span>{percent(process.cpu_pct)}</span></li>)}</ul> : <p className="setting-note">No process samples available.</p>}</div>}
    </> : !error && <Skeleton label="Loading resources…"><div aria-hidden="true"><dl className="resource-summary">{['CPU', 'Memory', 'Disk'].map(name => <div key={name}><dt>{name}</dt><dd><Bone w="70%" /><small><Bone w="45%" /></small></dd></div>)}</dl><NoteBone lines={1} phone={0} />
      <table className="resource-table"><thead><tr><th>Group</th><th>CPU</th><th>Memory</th></tr></thead><tbody>{Array.from({ length: groups }, (_, index) => <tr key={index}>
        <th><span className="resource-kind"><Bone w="3em" /></span><span className="resource-name"><Bone w={index % 2 ? '60%' : '80%'} /></span></th><td><Bone w="70%" /></td><td><Bone w="60%" /></td></tr>)}</tbody></table></div></Skeleton>}
  </section>
}
