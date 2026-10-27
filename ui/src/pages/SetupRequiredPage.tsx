// Shown until an operator creates the first Engineer (#25). Nothing else in the
// UI works until then: the API refuses every protected request.
export default function SetupRequiredPage() {
  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold text-gray-800 mb-2">RAG Platform</h1>
      <div className="bg-white border border-gray-200 rounded-xl p-6 shadow-sm w-full max-w-xl space-y-3">
        <h2 className="text-lg font-semibold text-gray-800">Not set up yet</h2>
        <p className="text-sm text-gray-600">
          No account exists. On the machine running the stack, create the first AI Engineer from the
          project folder:
        </p>
        <pre className="bg-gray-100 rounded p-3 text-sm overflow-x-auto">docker compose exec api python -m auth bootstrap &lt;username&gt;</pre>
        <p className="text-sm text-gray-600">
          It prints a one-time password. Reload this page, log in with it, and choose a new password.
        </p>
      </div>
    </div>
  )
}
