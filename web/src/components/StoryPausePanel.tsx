import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { Link } from 'react-router-dom'
import { messageOf } from './StoryUI'

export function StoryPausePanel({ sessionId, branchId, ended, canEnd, onChanged }: {
  sessionId: string; branchId: string; ended: boolean; canEnd: boolean; onChanged: () => void
}) {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const active = useRef(true)
  const pending = useRef(false)
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  async function end() {
    if (!canEnd || ended || pending.current) return
    pending.current = true
    setBusy(true)
    setError('')
    try {
      await api.endRoute(sessionId, branchId)
      if (active.current) { setConfirm(false); onChanged() }
    } catch (e) {
      if (active.current) setError(messageOf(e))
    } finally {
      pending.current = false
      if (active.current) setBusy(false)
    }
  }
  return <section className="closure-panel" aria-label="保存与告别">
    <p>已经写成的故事会自动保存。暂时离开后，可以从“我的故事”继续阅读。</p>
    <Link className="text-button" to="/sessions">回到我的故事</Link>
    {ended ? <p>这条路线已停在此处。你仍可回顾经历，或回到更早的选择尝试另一种可能。</p> : canEnd && <>
      <p>如果决定就此告别，也可以主动结束这条路线；暂时休息无需结束。</p>
      {confirm ? <div role="group" aria-label="确认告别这条路线">
        <p>确认在这里告别？正文会保留，本页将停止续写；这不会被记为达成故事结局。</p>
        <button className="button subtle" disabled={busy} onClick={() => void end()}>确认告别</button>
        <button className="text-button" disabled={busy} onClick={() => setConfirm(false)}>继续旅程</button>
      </div> : <button className="text-button" disabled={busy} onClick={() => setConfirm(true)}>在这里告别</button>}
    </>}
    {busy && <p role="status">正在保存告别记录…</p>}
    {error && <p role="alert">{error}</p>}
  </section>
}
