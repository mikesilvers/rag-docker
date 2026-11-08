import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

// The user's own password change (#25). When a generated password is due for
// change, this is the only page the UI shows, with a way to log out.
export default function ChangePasswordPage({ forced = false }: { forced?: boolean }) {
  const { changePassword, logout, user } = useAuth()
  const navigate = useNavigate()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setDone(false)
    if (next !== confirm) { setError('The new passwords do not match.'); return }
    setBusy(true)
    try {
      await changePassword(current, next)
      setCurrent(''); setNext(''); setConfirm('')
      if (forced) navigate('/qa', { replace: true })
      else setDone(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const field = (id: string, label: string, value: string, set: (v: string) => void, autoComplete: string) => (
    <label className="block text-sm text-gray-700">
      {label}
      <input id={id} type="password" autoComplete={autoComplete} value={value} onChange={e => set(e.target.value)}
        className="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm" required />
    </label>
  )

  const form = (
    <form onSubmit={submit} className="bg-white border border-gray-200 rounded-xl p-6 shadow-sm w-full max-w-sm space-y-4">
      <h2 className="text-lg font-semibold text-gray-800">Change password</h2>
      {forced && (
        <p className="text-sm text-gray-600">
          {user ? `${user.username}, your` : 'Your'} password was generated for you. Choose a new one to continue.
        </p>
      )}
      {field('current-password', 'Current password', current, setCurrent, 'current-password')}
      {field('new-password', 'New password (at least 12 characters)', next, setNext, 'new-password')}
      {field('confirm-password', 'New password again', confirm, setConfirm, 'new-password')}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {done && <p role="status" className="text-sm text-green-700">Password changed. Your other sessions were logged out.</p>}
      <button type="submit" disabled={busy || !current || !next || !confirm}
        className="w-full bg-blue-600 text-white rounded px-4 py-2 text-sm font-medium disabled:opacity-50">
        {busy ? 'Changing…' : 'Change password'}
      </button>
      {forced && (
        <button type="button" onClick={() => logout()} className="w-full text-sm text-gray-500 hover:text-blue-600">
          Log out
        </button>
      )}
    </form>
  )

  if (!forced) return <div className="flex justify-center">{form}</div>
  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold text-gray-800 mb-6">RAG Platform</h1>
      {form}
    </div>
  )
}
