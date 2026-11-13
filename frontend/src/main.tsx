import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

// Design system styles, once, in this order (agent-guide §1-3, §1-3-1).
import '@bv-ds/tokens/root.css'
import '@bv-ds/tokens/tokens.css'
import '@bv-ds/ui/style.css'
import '@bv-ds/ui/agent.css'
import './app.css'

import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
