import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import BrowserPopout from './BrowserPopout'
import { CrashScreen } from './CrashScreen'
import { ZoomBadge } from './ZoomBadge'
import { registerShareWorker } from './shared'
import { reloadOnStaleChunk } from './staleChunk'
import { guardFileDrops } from './chatDrop'
import { HostView } from './HostView'
import { trackFocus } from './hooks'

const popout = new URLSearchParams(location.search).get('browser') === 'window'
if (popout) document.title = 'Browser · Tether'

if (!popout) registerShareWorker()
reloadOnStaleChunk()
if (!popout) guardFileDrops()
trackFocus()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <CrashScreen>
      {popout ? <BrowserPopout /> : <>
        <App />
        <ZoomBadge />
        <HostView />
      </>}
    </CrashScreen>
  </StrictMode>,
)
