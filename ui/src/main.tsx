import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { CrashScreen } from './CrashScreen'
import { ZoomBadge } from './ZoomBadge'
import { registerShareWorker } from './shared'
import { reloadOnStaleChunk } from './staleChunk'

registerShareWorker()
reloadOnStaleChunk()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <CrashScreen>
      <App />
      <ZoomBadge />
    </CrashScreen>
  </StrictMode>,
)
