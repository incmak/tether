export function reportVoice(report: { kind: 'dictation' | 'friday'; event: string; trail?: string; detail?: string; ms?: number }) {
  try {
    void fetch('/mobile/api/voice-log', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Codex-Mobile': '1' },
      body: JSON.stringify({ ...report, ua: navigator.userAgent }),
      keepalive: true,
    }).catch(() => {})
  } catch { /* Diagnostics must never interrupt voice. */ }
}
