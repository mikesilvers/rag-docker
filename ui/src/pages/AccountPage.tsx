import { FormEvent, ReactNode, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import qrcode from 'qrcode-generator'
import { api, MfaStatus } from '../api/client'
import { useAuth } from '../context/AuthContext'

// The user's own account (#607): two-step verification with an authenticator
// app (TOTP), and a link to Change password. Everything sensitive (the secret
// while setting up, recovery codes) comes in one answer and is kept only in
// this page's memory until "Done". The QR code is drawn here in the browser,
// so the secret never leaves the machine for an image service; the secret is
// also shown as text for apps that take it typed. When two-step verification
// is required for everyone and this user hasn't set it up, this is the only
// page the UI shows (`forced`), with a way to log out.

function qrDataUrl(text: string): string {
  const qr = qrcode(0, 'M')
  qr.addData(text)
  qr.make()
  return qr.createDataURL(4, 4)
}

function Codes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  return (
    <div data-testid="recovery-codes" className="border border-amber-300 bg-amber-50 rounded p-4 text-sm space-y-3">
      <p>
        Your recovery codes. Each works once, in place of a code from the app, if you lose your phone. They are
        shown only once: save them somewhere safe now.
      </p>
      <ul className="grid grid-cols-2 gap-2 font-mono">
        {codes.map(c => <li key={c} className="bg-white border border-gray-200 rounded px-2 py-1 select-all">{c}</li>)}
      </ul>
      <button onClick={onDone} className="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-700">Done</button>
    </div>
  )
}

export default function AccountPage({ forced = false }: { forced?: boolean }) {
  const { user, logout, refresh } = useAuth()
  const navigate = useNavigate()
  const [status, setStatus] = useState<MfaStatus | null>(null)
  const [loadError, setLoadError] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null)
  const [codes, setCodes] = useState<string[] | null>(null)
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [form, setForm] = useState<'none' | 'regenerate' | 'disable'>('none')

  async function load() {
    try {
      setStatus(await api.mfaStatus())
      setLoadError('')
    } catch (e: unknown) {
      setLoadError(`Could not load your two-step verification: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  useEffect(() => { load() }, [])

  async function run(action: () => Promise<void>) {
    setError('')
    setBusy(true)
    try {
      await action()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const start = () => run(async () => {
    const started = await api.mfaEnroll()
    setSetup({ secret: started.secret, uri: started.otpauth_uri })
    setCode('')
  })

  const confirm = (e: FormEvent) => {
    e.preventDefault()
    run(async () => {
      try {
        const result = await api.mfaConfirm(code)
        setSetup(null)
        setStatus(result.status)
        setCodes(result.recovery_codes)
      } finally {
        setCode('')
      }
    })
  }

  const regenerate = (e: FormEvent) => {
    e.preventDefault()
    run(async () => {
      try {
        const result = await api.mfaRegenerate(code)
        setStatus(result.status)
        setCodes(result.recovery_codes)
        setForm('none')
      } finally {
        setCode('')
      }
    })
  }

  const disable = (e: FormEvent) => {
    e.preventDefault()
    run(async () => {
      try {
        const result = await api.mfaDisable(password, code)
        setStatus(result.status)
        setForm('none')
        await refresh()
      } finally {
        setCode('')
        setPassword('')
      }
    })
  }

  // After the codes of a new set-up are put away, the API's view of the user
  // (enrolled; no set-up due any more) is read again; a forced set-up then
  // opens Q&A, as a forced password change does.
  const done = async () => {
    setCodes(null)
    await refresh()
    if (forced) navigate('/qa', { replace: true })
  }

  const input = 'mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm'
  const primary = 'bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50'
  const codeField = (id: string, label: string) => (
    <label className="block text-sm text-gray-700">
      {label}
      <input id={id} autoComplete="one-time-code" value={code} maxLength={64} onChange={e => setCode(e.target.value)}
        className={input} required />
    </label>
  )

  let body: ReactNode
  if (codes) {
    body = <Codes codes={codes} onDone={done} />
  } else if (!status) {
    body = loadError ? null : <p className="text-sm text-gray-500">Loading…</p>
  } else if (!status.enrolled) {
    body = setup ? (
      <form onSubmit={confirm} className="space-y-4" data-testid="mfa-setup">
        <p className="text-sm text-gray-700">
          Scan this code with an authenticator app (for example Microsoft Authenticator, Google Authenticator or
          1Password), then enter the 6-digit code it shows.
        </p>
        <img src={qrDataUrl(setup.uri)} alt="QR code for your authenticator app" className="border border-gray-200 rounded" />
        <p className="text-sm text-gray-700">
          Or enter this key in the app by hand:{' '}
          <code data-testid="mfa-secret" className="bg-white border border-gray-200 rounded px-2 py-1 select-all break-all">{setup.secret}</code>
        </p>
        {codeField('mfa-confirm-code', 'Code from the app')}
        <div className="flex gap-3">
          <button type="submit" disabled={busy || !code} className={primary}>Confirm</button>
          <button type="button" onClick={() => setSetup(null)} className="text-sm text-gray-500 hover:text-blue-600">Cancel</button>
        </div>
      </form>
    ) : (
      <div className="space-y-3">
        <p className="text-sm text-gray-700">
          {forced
            ? 'Two-step verification is required for every account. Set it up to continue.'
            : 'Two-step verification is off. When it is on, logging in also needs a code from an authenticator app on your phone.'}
        </p>
        <button onClick={start} disabled={busy} className={primary}>Set up</button>
      </div>
    )
  } else {
    body = (
      <div className="space-y-4">
        <p className="text-sm text-gray-700" data-testid="mfa-on">
          Two-step verification is on{status.enrolled_at ? ` since ${new Date(status.enrolled_at).toLocaleDateString()}` : ''}.
          {' '}{status.recovery_codes_remaining} of 10 recovery codes left.
        </p>
        {form === 'regenerate' && (
          <form onSubmit={regenerate} className="space-y-3">
            <p className="text-sm text-gray-600">New codes replace all your old ones.</p>
            {codeField('mfa-regenerate-code', 'Code from the app')}
            <button type="submit" disabled={busy || !code} className={primary}>Get new recovery codes</button>
          </form>
        )}
        {form === 'disable' && (
          <form onSubmit={disable} className="space-y-3">
            <label className="block text-sm text-gray-700">
              Password
              <input id="mfa-disable-password" type="password" autoComplete="current-password" value={password}
                onChange={e => setPassword(e.target.value)} className={input} required />
            </label>
            {codeField('mfa-disable-code', 'Code from the app, or a recovery code')}
            <button type="submit" disabled={busy || !code || !password} className={primary}>Turn off</button>
          </form>
        )}
        {form === 'none' && (
          <div className="flex gap-4">
            <button onClick={() => { setForm('regenerate'); setCode(''); setError('') }}
              className="text-sm text-blue-600 hover:underline">Regenerate recovery codes</button>
            {!status.required && (
              <button onClick={() => { setForm('disable'); setCode(''); setPassword(''); setError('') }}
                className="text-sm text-blue-600 hover:underline">Turn off</button>
            )}
          </div>
        )}
        {form !== 'none' && (
          <button onClick={() => { setForm('none'); setError('') }} className="text-sm text-gray-500 hover:text-blue-600">Cancel</button>
        )}
      </div>
    )
  }

  const section = (
    <div className="bg-white border border-gray-200 rounded-xl p-6 shadow-xs w-full max-w-lg space-y-4">
      <h2 className="text-lg font-semibold text-gray-800">Two-step verification</h2>
      {loadError && <p role="alert" className="text-sm text-red-600">{loadError}</p>}
      {body}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    </div>
  )

  if (forced) {
    return (
      <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8 gap-4">
        <h1 className="text-3xl font-bold text-gray-800 mb-2">RAG Platform</h1>
        {section}
        <button type="button" onClick={() => logout()} className="text-sm text-gray-500 hover:text-blue-600">Log out</button>
      </div>
    )
  }
  return (
    <div className="flex flex-col items-center gap-6">
      <h1 className="text-2xl font-bold self-start">Account</h1>
      <p className="text-sm text-gray-600 self-start">
        {user?.username}. <Link to="/account/password" className="text-blue-600 hover:underline">Change password</Link>
      </p>
      {section}
    </div>
  )
}
