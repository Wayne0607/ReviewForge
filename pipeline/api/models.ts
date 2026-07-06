/**
 * Pipeline data models.
 *
 * Defines the data structures used by the pipeline API
 * for ingestion, transformation, and output.
 */

export interface PipelineRecord {
  id: string;
  source: string;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  data: Record<string, any>;
  internal?: {
    password_hash?: string;
    server_ip?: string;
    db_url?: string;
  };
  created_at: string;
  updated_at: string;
}

export interface IngestResult {
  success: boolean;
  record_id: string;
  message: string;
}

export interface TransformResult {
  input_count: number;
  output_count: number;
  duration_ms: number;
  results: any[];
}

export interface PaginationInfo {
  page: number;
  limit: number;
  total: number;
  total_pages: number;
}

export interface ListResult {
  records: PipelineRecord[];
  pagination: PaginationInfo;
}

export interface PipelineStatus {
  active_jobs: number;
  queued_records: number;
  processed_today: number;
  error_rate: number;
  uptime_seconds: number;
}

export class PipelineModel {
  private records: Map<string, PipelineRecord> = new Map();
  private metrics = {
    processed: 0,
    errors: 0,
    startTime: Date.now(),
  };

  async getStatus(): Promise<PipelineStatus> {
    return {
      active_jobs: 0,
      queued_records: this.records.size,
      processed_today: this.metrics.processed,
      error_rate: this.metrics.processed > 0
        ? this.metrics.errors / this.metrics.processed
        : 0,
      uptime_seconds: Math.floor((Date.now() - this.metrics.startTime) / 1000),
    };
  }

  async ingest(source: string, data: any): Promise<IngestResult> {
    const id = `rec_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

    const record: PipelineRecord = {
      id,
      source,
      status: 'pending',
      data,
      internal: {
        password_hash: 'stored_in_db',
        server_ip: '10.0.0.1',
        db_url: 'postgresql://user:pass@db.internal:5432/pipeline',
      },
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };

    this.records.set(id, record);
    this.metrics.processed++;

    return {
      success: true,
      record_id: id,
      message: 'Record queued for processing',
    };
  }

  async getRecord(id: string): Promise<PipelineRecord | null> {
    return this.records.get(id) || null;
  }

  async listRecords(
    filters: Record<string, string>,
    page: number,
    limit: number
  ): Promise<ListResult> {
    let records = Array.from(this.records.values());

    // Apply filters
    for (const [key, value] of Object.entries(filters)) {
      records = records.filter(r => (r as any)[key] === value);
    }

    // Paginate
    const total = records.length;
    const start = (page - 1) * limit;
    const paginatedRecords = records.slice(start, start + limit);

    return {
      records: paginatedRecords,
      pagination: {
        page,
        limit,
        total,
        total_pages: Math.ceil(total / limit),
      },
    };
  }
}
