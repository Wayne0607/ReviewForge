import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Reviews from './pages/Reviews'
import ReviewDetail from './pages/ReviewDetail'
import Analytics from './pages/Analytics'
import System from './pages/System'

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/reviews" element={<Reviews />} />
        <Route path="/reviews/:runId" element={<ReviewDetail />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="/system" element={<System />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  )
}
