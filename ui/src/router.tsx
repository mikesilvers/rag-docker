import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import NavBar from './components/NavBar'
import LoginPage from './pages/LoginPage'
import SetupRequiredPage from './pages/SetupRequiredPage'
import ChangePasswordPage from './pages/ChangePasswordPage'
import QAPage from './pages/QAPage'
import ImportPage from './pages/ImportPage'
import ChunkingPage from './pages/ChunkingPage'
import RetrievalPage from './pages/RetrievalPage'
import GoldStandardPage from './pages/GoldStandardPage'
import CollectionsPage from './pages/CollectionsPage'
import HealthPage from './pages/HealthPage'
import UsersPage from './pages/UsersPage'
import ServiceKeysPage from './pages/ServiceKeysPage'
import AuditPage from './pages/AuditPage'
import TransferPage from './pages/TransferPage'
import HelpTransferPage from './pages/HelpTransferPage'

// What the API says decides what is shown (#25): set up, logged in, a password
// change due, and the role. Hiding a page is a convenience; the API refuses
// what the role can't do whatever the UI shows.
export default function AppRouter() {
  const { state } = useAuth()

  if (state.phase === 'loading') {
    return <p className="p-8 text-sm text-gray-500">Loading…</p>
  }
  if (state.phase === 'error') {
    return <p role="alert" className="p-8 text-sm text-red-600">{state.message}</p>
  }
  if (state.phase === 'setup_required') return <SetupRequiredPage />
  if (state.phase === 'logged_out') {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }
  if (state.user.must_change_password) {
    return (
      <Routes>
        <Route path="/account/password" element={<ChangePasswordPage forced />} />
        <Route path="*" element={<Navigate to="/account/password" replace />} />
      </Routes>
    )
  }
  const role = state.user.role

  return (
    <div className="min-h-screen bg-gray-50">
      <NavBar />
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/qa" replace />} />
          <Route path="/login" element={<Navigate to="/qa" replace />} />
          <Route path="/qa" element={<QAPage />} />
          <Route path="/account/password" element={<ChangePasswordPage />} />
          {role !== 'end_user' && (
            <>
              <Route path="/import" element={<ImportPage />} />
              <Route path="/chunking" element={<ChunkingPage />} />
              <Route path="/retrieval" element={<RetrievalPage />} />
              <Route path="/goldstandard" element={<GoldStandardPage />} />
              <Route path="/transfer" element={<TransferPage />} />
              <Route path="/help/transfer" element={<HelpTransferPage />} />
            </>
          )}
          {role === 'engineer' && (
            <>
              <Route path="/collections" element={<CollectionsPage />} />
              <Route path="/health" element={<HealthPage />} />
              <Route path="/users" element={<UsersPage />} />
              <Route path="/service-keys" element={<ServiceKeysPage />} />
              <Route path="/audit" element={<AuditPage />} />
            </>
          )}
          <Route path="*" element={<Navigate to="/qa" replace />} />
        </Routes>
      </main>
    </div>
  )
}
