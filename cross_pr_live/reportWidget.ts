import { run_report_query } from 'cross_pr_live/risky_ops'

type Db = {
  query: (sql: string, params?: unknown[]) => unknown[]
}

export function previewReport(db: Db, accountId: string) {
  return run_report_query(db, 'reports', accountId)
}
