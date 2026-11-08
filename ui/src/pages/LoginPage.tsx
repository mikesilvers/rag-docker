import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

// Replaces the role picker (#25): the role now comes with the account.
export default function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await login(username, password)
      navigate('/qa', { replace: true })
    } catch (err) {
      // The server's own message: generic for a failed login, "wait" when throttled.
      setError(err instanceof Error ? err.message : String(err))
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold text-gray-800 mb-2">RAG Platform</h1>
      <p className="text-gray-500 mb-8">Log in to continue</p>
      <form onSubmit={submit} className="bg-white border border-gray-200 rounded-xl p-6 shadow-sm w-full max-w-sm space-y-4">
        <label className="block text-sm text-gray-700">
          Username
          <input id="login-username" autoComplete="username" value={username} onChange={e => setUsername(e.target.value)}
            className="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm" required />
        </label>
        <label className="block text-sm text-gray-700">
          Password
          <input id="login-password" type="password" autoComplete="current-password" value={password}
            onChange={e => setPassword(e.target.value)}
            className="mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm" required />
        </label>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <button type="submit" disabled={busy || !username || !password}
          className="w-full bg-blue-600 text-white rounded px-4 py-2 text-sm font-medium disabled:opacity-50">
          {busy ? 'Logging in…' : 'Log in'}
        </button>
      </form>
    </div>
  )
}
