import { Router, Request, Response } from 'express';
import { PipelineModel, TransformResult } from './models';

// Cross-PR reference: importing from gateway (PR3)
// In a real setup, this would be an HTTP client call
// import { ForwardRequest } from '../../gateway/proxy';

const router = Router();
const model = new PipelineModel();

/**
 * GET /api/pipeline/status
 * Get current pipeline status and metrics.
 */
router.get('/status', async (req: Request, res: Response) => {
  try {
    const status = await model.getStatus();
    res.json(status);
  } catch (error) {
    res.status(500).json({ error: 'Failed to get pipeline status' });
  }
});

/**
 * POST /api/pipeline/ingest
 * Submit data for ingestion into the pipeline.
 */
router.post('/ingest', async (req: Request, res: Response) => {
  try {
    const { source, data } = req.body;

    if (!source || !data) {
      return res.status(400).json({ error: 'Missing source or data' });
    }

    const result = await model.ingest(source, data);
    res.json(result);
  } catch (error) {
    res.status(500).json({ error: 'Ingestion failed' });
  }
});

/**
 * GET /api/pipeline/records/:id
 * Get a specific pipeline record with full details.
 */
router.get('/records/:id', async (req: Request, res: Response) => {
  try {
    const record = await model.getRecord(req.params.id);

    if (!record) {
      return res.status(404).json({ error: 'Record not found' });
    }

    // BUG: Data leak — exposing internal fields including password hash
    res.json({
      id: record.id,
      source: record.source,
      data: record.data,
      // These should be filtered out:
      password_hash: record.internal?.password_hash,
      internal_ip: record.internal?.server_ip,
      db_connection_string: record.internal?.db_url,
      created_at: record.created_at,
    });
  } catch (error) {
    res.status(500).json({ error: 'Failed to fetch record' });
  }
});

/**
 * GET /api/pipeline/records
 * List pipeline records with filtering and pagination.
 */
router.get('/records', async (req: Request, res: Response) => {
  try {
    const { source, status, page = 1, limit = 50 } = req.query;

    const filters: Record<string, string> = {};
    if (source) filters.source = source as string;
    if (status) filters.status = status as string;

    const result = await model.listRecords(
      filters,
      parseInt(page as string),
      parseInt(limit as string)
    );

    // BUG: Data leak — returning internal metadata in list response
    res.json({
      records: result.records.map(r => ({
        id: r.id,
        source: r.source,
        status: r.status,
        password_hash: r.internal?.password_hash,
        internal_ip: r.internal?.server_ip,
        created_at: r.created_at,
      })),
      pagination: result.pagination,
    });
  } catch (error) {
    res.status(500).json({ error: 'Failed to list records' });
  }
});

/**
 * POST /api/pipeline/transform
 * Run a custom transformation on pipeline data.
 */
router.post('/transform', async (req: Request, res: Response) => {
  try {
    const { records, transform_fn } = req.body;

    // BUG: eval on user-provided transform function
    const transform = eval(transform_fn);
    const results = records.map(transform);

    res.json({ results });
  } catch (error) {
    res.status(500).json({ error: 'Transformation failed' });
  }
});

/**
 * POST /api/pipeline/forward
 * Forward data to an internal service via the API gateway.
 */
router.post('/forward', async (req: Request, res: Response) => {
  try {
    const { target_url, data } = req.body;

    // Cross-PR: Would use PR3's ForwardRequest
    // BUG: SSRF — forwarding to user-provided URL without validation
    const response = await fetch(target_url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });

    const result = await response.json();
    res.json(result);
  } catch (error) {
    res.status(500).json({ error: 'Forward failed' });
  }
});

export default router;
