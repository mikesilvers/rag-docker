import { FileProgress, JobStatus } from '../api/client'

// A time left as people read it, never more precise than a minute (#614).
function formatDuration(seconds: number): string {
  if (seconds < 60) return 'under a minute'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

function fileState(f: FileProgress, running: boolean): string {
  if (f.failed) return 'failed'
  const stored = `${f.chunks_stored.toLocaleString()} stored`
  if (f.chunked && f.chunks_stored >= f.chunks) return `${stored} · done`
  if (running && typeof f.seconds_left === 'number') return `${stored} · about ${formatDuration(f.seconds_left)} left`
  return stored
}

export default function ProgressPanel({ job }: { job: JobStatus }) {
  const pct = job.files_total > 0 ? Math.round((job.files_completed / job.files_total) * 100) : 0
  // Shown only when sending, acknowledgement and the check disagree (#302), so a
  // clean job looks as it always did. Older records carry no counts at all.
  const counted = typeof job.chunks_submitted === 'number' && typeof job.chunks_accepted === 'number'
  const writesDiffer = counted &&
    !(job.chunks_submitted === job.chunks_stored && job.chunks_accepted === job.chunks_stored)
  // The time estimate (#614): only while the job runs; the per-file counts stay.
  const running = job.status === 'running'
  const files = job.file_progress ?? []
  const omitted = job.file_progress_omitted ?? 0
  const rate = job.rate_characters_per_second
  const hasRate = typeof rate === 'number'
  const left = job.seconds_left
  const notChunked = job.files_not_chunked ?? 0
  return (
    <div className="mt-4 p-4 border rounded bg-gray-50">
      <div className="flex justify-between text-sm mb-1">
        <span>Files: {job.files_completed}/{job.files_total}</span>
        <span>Chunks stored: {job.chunks_stored}</span>
        <span className={job.status === 'completed' ? 'text-green-600' : job.status === 'failed' ? 'text-red-600' : 'text-blue-600'}>
          {job.status}
        </span>
      </div>
      <div className="w-full bg-gray-200 rounded h-2">
        <div className="bg-blue-500 h-2 rounded transition-all" style={{ width: `${pct}%` }} />
      </div>
      {running && (
        <div className="mt-2 text-xs text-gray-700" data-testid="ingest-estimate">
          <p>
            {hasRate
              ? `Embedding rate: ${Math.round(rate!).toLocaleString()} characters/s`
              : 'No estimate yet: the rate is measured when the first part is stored'}
          </p>
          {hasRate && typeof left === 'number' && (
            <p className="font-medium">
              {job.seconds_left_is_minimum
                ? `At least ${formatDuration(left)} left (${notChunked > 0
                    ? `${notChunked} file${notChunked === 1 ? '' : 's'} not chunked yet`
                    : 'more parts may follow'})`
                : `About ${formatDuration(left)} left`}
            </p>
          )}
        </div>
      )}
      {files.length > 0 && (
        // Each file's chunk count once it is chunked, and its time left (#614).
        <div className="mt-2 text-xs text-gray-700 max-h-48 overflow-y-auto" data-testid="file-progress">
          {omitted > 0 && (
            <p className="text-gray-500">{omitted} earlier file{omitted === 1 ? '' : 's'} not shown</p>
          )}
          <ul className="space-y-1">
            {files.map((f, i) => (
              <li key={i} className="flex justify-between gap-2">
                <span className="truncate">{f.file}</span>
                <span className={`whitespace-nowrap ${f.failed ? 'text-red-600' : ''}`}>
                  {f.chunks.toLocaleString()} chunk{f.chunks === 1 ? '' : 's'}{f.chunked ? '' : ' so far'} · {fileState(f, running)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {writesDiffer && (
        <p className="mt-2 text-xs text-gray-600">
          Submitted {job.chunks_submitted} · accepted {job.chunks_accepted} · stored {job.chunks_stored}
        </p>
      )}
      {(job.chunks_replaced ?? 0) > 0 && (
        // Re-ingesting a name replaces its chunks (#22), so the total can stay
        // the same while the content changed.
        <p className="mt-2 text-xs text-gray-600">
          Replaced {job.chunks_replaced} chunk{job.chunks_replaced === 1 ? '' : 's'} from a previous version
        </p>
      )}
      {job.errors.length > 0 && (
        <ul className="mt-2 text-xs text-red-600 space-y-1">
          {job.errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
      {(job.split?.length ?? 0) > 0 && (
        // A file over the per-file limits is stored as parts named
        // `<name> (part N)` (#621); not a failure, but the names differ.
        <div className="mt-2 text-xs text-gray-700" data-testid="split-files">
          <p className="font-medium">
            {job.split!.length} file{job.split!.length === 1 ? '' : 's'} split into parts:
          </p>
          <ul className="space-y-1 mt-1">
            {job.split!.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        </div>
      )}
      {(job.skipped?.length ?? 0) > 0 && (
        // Skipped files are never counted in files_total, so without this the
        // count simply reads lower than what the user uploaded. Each entry
        // says why: an unsupported type, or a later file with the same name.
        <div className="mt-2 text-xs text-amber-700">
          <p className="font-medium">
            {job.skipped!.length} file{job.skipped!.length === 1 ? '' : 's'} skipped:
          </p>
          <ul className="space-y-1 mt-1">
            {job.skipped!.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}
