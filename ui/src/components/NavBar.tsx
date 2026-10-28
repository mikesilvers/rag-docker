import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function NavBar() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const role = user?.role ?? null

  async function logOut() {
    await logout()
    navigate('/login', { replace: true })
  }

  const roleLabel = role === 'engineer' ? 'AI Engineer' : role === 'developer' ? 'Developer' : 'End User'

  return (
    <nav className="bg-white border-b border-gray-200 px-4 py-3 flex items-center gap-6">
      <span className="font-bold text-blue-700 text-lg">RAG Platform</span>
      <Link to="/qa" className="text-sm text-gray-700 hover:text-blue-600">Q&amp;A</Link>
      {role !== 'end_user' && (
        <>
          <Link to="/import" className="text-sm text-gray-700 hover:text-blue-600">Import</Link>
          <Link to="/chunking" className="text-sm text-gray-700 hover:text-blue-600">Chunking</Link>
          <Link to="/retrieval" className="text-sm text-gray-700 hover:text-blue-600">Retrieval</Link>
          <Link to="/goldstandard" className="text-sm text-gray-700 hover:text-blue-600">Gold Standard</Link>
          <Link to="/transfer" className="text-sm text-gray-700 hover:text-blue-600">Transfer</Link>
        </>
      )}
      {role === 'engineer' && (
        <>
          <Link to="/collections" className="text-sm text-gray-700 hover:text-blue-600">Collections</Link>
          <Link to="/health" className="text-sm text-gray-700 hover:text-blue-600">Health</Link>
          <Link to="/users" className="text-sm text-gray-700 hover:text-blue-600">Users</Link>
          <Link to="/service-keys" className="text-sm text-gray-700 hover:text-blue-600">Service keys</Link>
        </>
      )}
      <div className="ml-auto flex items-center gap-3">
        <span className="text-sm text-gray-700" data-testid="nav-username">{user?.username}</span>
        <span className="text-xs bg-blue-100 text-blue-800 px-2 py-1 rounded">{roleLabel}</span>
        {/* A button, not a link: the nav's links are the role's pages. */}
        <button onClick={() => navigate('/account/password')} className="text-sm text-gray-500 hover:text-blue-600">Change password</button>
        <button onClick={logOut} className="text-sm text-gray-500 hover:text-blue-600">Log out</button>
      </div>
    </nav>
  )
}
