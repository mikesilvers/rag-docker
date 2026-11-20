import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import { useAuth } from '../context/AuthContext'

// Replaces the role picker (#25): the role now comes with the account.
// With two-step verification (#607) a right password asks for a code from the
// authenticator app, or a recovery code, before the session opens.
export default function LoginPage() {
  const { login, verifyCode } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [step, setStep] = useState<'password' | 'code'>('password')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (await login(username, password)) {
        setPassword('')
        setStep('code')
        return
      }
      navigate('/qa', { replace: true })
    } catch (err) {
      // The server's own message: generic for a failed login, "wait" when throttled.
      setError(err instanceof Error ? err.message : String(err))
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  async function submitCode(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await verifyCode(code)
      navigate('/qa', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setCode('')
      // The challenge ended (expired or used up): start again with the password.
      if (err instanceof ApiError && err.code === 'MFA_CHALLENGE_INVALID') setStep('password')
    } finally {
      setBusy(false)
    }
  }

  function back() {
    setStep('password')
    setCode('')
    setError('')
  }

  const input = 'mt-1 w-full border border-gray-300 rounded px-3 py-2 text-sm'
  const button = 'w-full bg-blue-600 text-white rounded px-4 py-2 text-sm font-medium disabled:opacity-50'
  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold text-gray-800 mb-2">RAG Platform</h1>
      <p className="text-gray-500 mb-8">Log in to continue</p>
      {step === 'password' ? (
        <form onSubmit={submit} className="bg-white border border-gray-200 rounded-xl p-6 shadow-xs w-full max-w-sm space-y-4">
          <label className="block text-sm text-gray-700">
            Username
            <input id="login-username" autoComplete="username" value={username} onChange={e => setUsername(e.target.value)}
              className={input} required />
          </label>
          <label className="block text-sm text-gray-700">
            Password
            <input id="login-password" type="password" autoComplete="current-password" value={password}
              onChange={e => setPassword(e.target.value)}
              className={input} required />
          </label>
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <button type="submit" disabled={busy || !username || !password} className={button}>
            {busy ? 'Logging in…' : 'Log in'}
          </button>
        </form>
      ) : (
        <form onSubmit={submitCode} className="bg-white border border-gray-200 rounded-xl p-6 shadow-xs w-full max-w-sm space-y-4">
          <p className="text-sm text-gray-600">
            Enter the 6-digit code from your authenticator app, or one of your recovery codes.
          </p>
          <label className="block text-sm text-gray-700">
            Authentication code
            <input id="login-code" autoComplete="one-time-code" inputMode="text" value={code} maxLength={64}
              onChange={e => setCode(e.target.value)} className={input} required autoFocus />
          </label>
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <button type="submit" disabled={busy || !code} className={button}>
            {busy ? 'Verifying…' : 'Verify'}
          </button>
          <button type="button" onClick={back} className="w-full text-sm text-gray-500 hover:text-blue-600">Back</button>
        </form>
      )}
    </div>
  )
}
