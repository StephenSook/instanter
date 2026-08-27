import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    // A rejected registration must not vanish: PushToggle waits on
    // serviceWorker.ready, a promise that never settles when registration
    // failed, so without this catch the subscribe button would hang forever
    // with no message.
    navigator.serviceWorker.register("/sw.js").catch((e: unknown) => {
      console.error("service worker registration failed", e);
    });
  });
}
