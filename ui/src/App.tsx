import { BrowserRouter } from 'react-router-dom'
import { AuthProvider } from './context/AuthContext'
import { QueryConfigProvider } from './context/QueryConfigContext'
import AppRouter from './router'

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <QueryConfigProvider>
          <AppRouter />
        </QueryConfigProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
