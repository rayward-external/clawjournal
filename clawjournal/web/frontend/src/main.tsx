import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary.tsx'
import { api } from './api.ts'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)

// A successful SPA mount is an actual workbench open. Record it through the
// bearer-protected API instead of giving the unauthenticated index.html GET a
// state-changing side effect. The daemon schedules OS icon work off-thread.
void api.desktopOpened().catch(() => {})
