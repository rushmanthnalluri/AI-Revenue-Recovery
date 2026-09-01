/**
 * Fully typed client for the PulseRecover API (contracts/openapi.json).
 *
 * - Base URL: NEXT_PUBLIC_API_BASE_URL (default http://localhost:8000)
 * - Auth: X-API-Key header from NEXT_PUBLIC_API_KEY (mutating routes need it)
 * - Errors: throws `ApiError` parsed from the backend error envelope
 *   {"error": {"code", "message", "request_id"}}; network/timeout failures
 *   surface as ApiError with status 0 so the UI can show "backend unreachable"
 * - Timeout: 10s per request (AbortSignal.timeout); synchronous-long calls
 *   (demo.triggerScenario, evaluation.run, recovery.buildOpportunities)
 *   override to 120s (LONG_RUNNING_TIMEOUT_MS)
 */

import type {
  ActionResponse,
  ApiErrorEnvelope,
  ApprovalsSummaryResponse,
  ApproveRequest,
  AuditListParams,
  AuditListResponse,
  AuditVerifyResponse,
  BuildRequest,
  BuildResponse,
  CancelRequest,
  DashboardSummary,
  DashboardTimeseries,
  DemoResetResponse,
  DetectionRunRequest,
  Environment,
  EscalateRequest,
  EvaluationMetrics,
  EvaluationRunDetail,
  EvaluationRunListResponse,
  ExecuteRequest,
  Granularity,
  HealthResponse,
  IncidentDetail,
  IncidentListParams,
  IncidentListResponse,
  InvestigateRequest,
  JsonObject,
  MerchantConnection,
  MerchantSyncResponse,
  MerchantSyncToggle,
  OpportunityDetail,
  OpportunityListParams,
  OpportunityListResponse,
  PageParams,
  PaymentListParams,
  PaymentListResponse,
  PolicyBacktestRequest,
  PolicyBacktestResponse,
  QueryValue,
  ReconcileRequest,
  ReconcileResponse,
  RecoveryPlan,
  RejectRequest,
  RunEvaluationRequest,
  RunEvaluationResponse,
  ScenarioListResponse,
  ScenarioTriggerResponse,
  SystemHealth,
  TimeseriesParams,
} from "./types";

const BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/+$/, "");
const API_KEY = process.env.NEXT_PUBLIC_API_KEY;
export const DEFAULT_TIMEOUT_MS = 10_000;
/**
 * Synchronous-long endpoints (demo scenario trigger, evaluation run) execute
 * a full simulate → detect → diagnose loop before responding.
 */
export const LONG_RUNNING_TIMEOUT_MS = 120_000;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;

  constructor(status: number, code: string, message: string, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }

  /** True when the backend could not be reached at all (down, CORS, timeout). */
  get isUnreachable(): boolean {
    return this.status === 0;
  }
}

export function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err;
  if (err instanceof Error) {
    return new ApiError(0, "network_error", err.message);
  }
  return new ApiError(0, "unknown_error", String(err));
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: Record<string, QueryValue>;
  body?: unknown;
  headers?: Record<string, string>;
  timeoutMs?: number;
  /** "blob" returns the raw response body as a Blob (file downloads). */
  responseType?: "blob";
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(`${BASE_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === "") continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

function isErrorEnvelope(body: unknown): body is ApiErrorEnvelope {
  return (
    typeof body === "object" &&
    body !== null &&
    "error" in body &&
    typeof (body as ApiErrorEnvelope).error === "object" &&
    typeof (body as ApiErrorEnvelope).error.code === "string"
  );
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS, responseType } = options;

  const finalHeaders: Record<string, string> = {
    Accept: "application/json",
    ...headers,
  };
  if (body !== undefined) finalHeaders["Content-Type"] = "application/json";
  if (API_KEY) finalHeaders["X-API-Key"] = API_KEY;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new ApiError(0, "timeout", `Request timed out after ${timeoutMs}ms.`);
    }
    if (err instanceof TypeError && err.name === "TimeoutError") {
      throw new ApiError(0, "timeout", `Request timed out after ${timeoutMs}ms.`);
    }
    throw new ApiError(
      0,
      "unreachable",
      `Cannot reach the PulseRecover API at ${BASE_URL}. Is the backend running?`,
    );
  }

  let parsed: unknown = null;
  // Blob responses keep the body untouched on success; error bodies are still
  // read as text so the envelope/detail mapping below works unchanged.
  const text = response.ok && responseType === "blob" ? "" : await response.text();
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }

  if (!response.ok) {
    if (isErrorEnvelope(parsed)) {
      throw new ApiError(
        response.status,
        parsed.error.code,
        parsed.error.message,
        parsed.error.request_id,
      );
    }
    // FastAPI default validation shape: {"detail": [...]}.
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail
                .map((d) =>
                  typeof d === "object" && d !== null && "msg" in d
                    ? String((d as { msg: unknown }).msg)
                    : String(d),
                )
                .join("; ")
            : "Request failed.";
      throw new ApiError(response.status, "validation_error", message);
    }
    throw new ApiError(response.status, "http_error", `HTTP ${response.status} ${response.statusText}`);
  }

  if (responseType === "blob") {
    return (await response.blob()) as T;
  }

  return parsed as T;
}

function enc(segment: string): string {
  return encodeURIComponent(segment);
}

/**
 * One method per endpoint in contracts/openapi.json. Responses whose contract
 * schema is still `{}` are typed as JsonObject until the backend firms them up.
 */
export const api = {
  system: {
    healthz: () => request<HealthResponse>("/healthz"),
    readyz: () => request<SystemHealth>("/readyz"),
    health: () => request<SystemHealth>("/api/v1/system/health"),
  },
  dashboard: {
    summary: (environment: Environment = "real_test") =>
      request<DashboardSummary>("/api/v1/dashboard/summary", {
        query: { environment },
      }),
    timeseries: (params: TimeseriesParams = {}) =>
      request<DashboardTimeseries>("/api/v1/dashboard/timeseries", {
        query: {
          metric: params.metric ?? "payment_success_rate",
          granularity: params.granularity ?? ("hour" satisfies Granularity),
          window_hours: params.window_hours ?? 24,
          environment: params.environment ?? "real_test",
        },
      }),
  },
  payments: {
    list: (params: PaymentListParams = {}) =>
      request<PaymentListResponse>("/api/v1/payments", { query: { ...params } }),
  },
  merchant: {
    /** Live Razorpay Test Mode connection state (secrets stay server-side). */
    connection: () => request<MerchantConnection>("/api/v1/merchant/connection"),
    /** Pull the latest orders/payments/links/subscriptions from the gateway. */
    sync: () =>
      request<MerchantSyncResponse>("/api/v1/merchant/sync", {
        method: "POST",
        timeoutMs: LONG_RUNNING_TIMEOUT_MS,
      }),
    enable: () =>
      request<MerchantSyncToggle>("/api/v1/merchant/sync/enable", { method: "POST" }),
    disable: () =>
      request<MerchantSyncToggle>("/api/v1/merchant/sync/disable", { method: "POST" }),
  },
  incidents: {
    list: (params: IncidentListParams = {}) =>
      request<IncidentListResponse>("/api/v1/incidents", { query: { ...params } }),
    get: (incidentId: string) =>
      request<IncidentDetail>(`/api/v1/incidents/${enc(incidentId)}`),
    investigate: (incidentId: string, body?: InvestigateRequest) =>
      request<JsonObject>(`/api/v1/incidents/${enc(incidentId)}/investigate`, {
        method: "POST",
        body: body ?? {},
      }),
    investigation: (incidentId: string) =>
      request<JsonObject>(`/api/v1/incidents/${enc(incidentId)}/investigation`),
  },
  recovery: {
    opportunities: (params: OpportunityListParams = {}) =>
      request<OpportunityListResponse>("/api/v1/recovery/opportunities", {
        query: { ...params },
      }),
    /** Idempotent incident → opportunities + strategies build. Synchronous
        server-side (build + strategy generation), so it shares the
        long-running timeout with demo.triggerScenario / evaluation.run. */
    buildOpportunities: (body: BuildRequest) =>
      request<BuildResponse>("/api/v1/recovery/opportunities/build", {
        method: "POST",
        body,
        timeoutMs: LONG_RUNNING_TIMEOUT_MS,
      }),
    /** Operator-triggered reconciliation sweep (ADR 0011): every UNKNOWN
        action is re-queried against gateway truth (GETs only — never a blind
        retry) and each failed webhook event is re-run through the live
        handler registry. Synchronous server-side (per-action gateway
        round-trips), so it shares the long-running timeout. Idempotent — a
        second sweep over a clean database is a no-op. */
    reconcile: (body: ReconcileRequest = {}) =>
      request<ReconcileResponse>("/api/v1/recovery/reconcile", {
        method: "POST",
        body,
        timeoutMs: LONG_RUNNING_TIMEOUT_MS,
      }),
    get: (opportunityId: string) =>
      request<OpportunityDetail>(`/api/v1/recovery/${enc(opportunityId)}`),
    plan: (opportunityId: string) =>
      request<RecoveryPlan>(`/api/v1/recovery/${enc(opportunityId)}/plan`),
    approve: (opportunityId: string, body: ApproveRequest) =>
      request<ActionResponse>(`/api/v1/recovery/${enc(opportunityId)}/approve`, {
        method: "POST",
        body,
      }),
    reject: (opportunityId: string, body: RejectRequest) =>
      request<ActionResponse>(`/api/v1/recovery/${enc(opportunityId)}/reject`, {
        method: "POST",
        body,
      }),
    escalate: (opportunityId: string, body: EscalateRequest) =>
      request<ActionResponse>(`/api/v1/recovery/${enc(opportunityId)}/escalate`, {
        method: "POST",
        body,
      }),
    execute: (opportunityId: string, body: ExecuteRequest) =>
      request<ActionResponse>(`/api/v1/recovery/${enc(opportunityId)}/execute`, {
        method: "POST",
        body,
      }),
    cancel: (opportunityId: string, body: CancelRequest) =>
      request<ActionResponse>(`/api/v1/recovery/${enc(opportunityId)}/cancel`, {
        method: "POST",
        body,
      }),
    /** Whole-queue COUNT/SUM over the pending-approval lane — the correct
        "Value awaiting decision" beyond page 1 of the opportunities list. */
    approvalsSummary: (environment: Environment = "real_test") =>
      request<ApprovalsSummaryResponse>("/api/v1/recovery/opportunities/approvals-summary", {
        query: { environment },
      }),
  },
  audit: {
    list: (params: AuditListParams = {}) =>
      request<AuditListResponse>("/api/v1/audit", { query: { ...params } }),
    /** Read-only full-chain verification of the hash-chained audit trail.
        Deliberately not environment-scoped — the chain spans both
        environments in insertion order. */
    verify: () => request<AuditVerifyResponse>("/api/v1/audit/verify"),
  },
  policy: {
    /** Replay stored policy decisions against the CURRENT policy document.
        The replay writes nothing (read-only report); only the run itself
        joins the audit trail. Synchronous server-side (up to `limit`
        decisions re-evaluated), so it shares the long-running timeout. */
    backtest: (body: PolicyBacktestRequest = {}) =>
      request<PolicyBacktestResponse>("/api/v1/policy/backtest", {
        method: "POST",
        body,
        timeoutMs: LONG_RUNNING_TIMEOUT_MS,
      }),
  },
  evaluation: {
    runs: (params: PageParams = {}) =>
      request<EvaluationRunListResponse>("/api/v1/evaluation/runs", { query: { ...params } }),
    getRun: (runId: string) =>
      request<EvaluationRunDetail>(`/api/v1/evaluation/runs/${enc(runId)}`),
    metrics: () => request<EvaluationMetrics>("/api/v1/evaluation/metrics"),
    run: (body: RunEvaluationRequest) =>
      request<RunEvaluationResponse>("/api/v1/evaluation/run", {
        method: "POST",
        body,
        timeoutMs: LONG_RUNNING_TIMEOUT_MS,
      }),
  },
  detection: {
    run: (body?: DetectionRunRequest) =>
      request<JsonObject>("/api/v1/detection/run", { method: "POST", body: body ?? {} }),
  },
  demo: {
    scenarios: () => request<ScenarioListResponse>("/api/v1/demo/scenarios"),
    triggerScenario: (name: string) =>
      request<ScenarioTriggerResponse>(`/api/v1/demo/scenario/${enc(name)}`, {
        method: "POST",
        timeoutMs: LONG_RUNNING_TIMEOUT_MS,
      }),
    reset: () => request<DemoResetResponse>("/api/v1/demo/reset", { method: "POST" }),
  },
  webhooks: {
    /** Server-to-server endpoint; exposed for completeness/contract parity. */
    razorpay: (payload: JsonObject, signature?: string) =>
      request<JsonObject>("/webhooks/razorpay", {
        method: "POST",
        body: payload,
        headers: signature ? { "X-Razorpay-Signature": signature } : {},
      }),
  },
  export: {
    audit: (params: { environment: Environment; format: "csv" | "json"; entity_type?: string; entity_id?: string }) =>
      request<Blob>("/api/v1/export/audit", { query: params, headers: { Accept: params.format === "json" ? "application/json" : "text/csv" }, responseType: "blob" }),
    incidents: (params: { environment: Environment; format: "csv" | "json"; status?: string; severity?: string; metric?: string; detected_from?: string; detected_to?: string }) =>
      request<Blob>("/api/v1/export/incidents", { query: params, headers: { Accept: params.format === "json" ? "application/json" : "text/csv" }, responseType: "blob" }),
    recovery: (params: { environment: Environment; format: "csv" | "json" }) =>
      request<Blob>("/api/v1/export/recovery", { query: params, headers: { Accept: params.format === "json" ? "application/json" : "text/csv" }, responseType: "blob" }),
    payments: (params: { environment: Environment; format: "csv" | "json"; status?: string; method?: string }) =>
      request<Blob>("/api/v1/export/payments", { query: params, headers: { Accept: params.format === "json" ? "application/json" : "text/csv" }, responseType: "blob" }),
    summary: (params: { environment: Environment; format: "csv" | "json" }) =>
      request<Blob>("/api/v1/export/summary", { query: params, headers: { Accept: params.format === "json" ? "application/json" : "text/csv" }, responseType: "blob" }),
  },
};

export { BASE_URL as API_BASE_URL };
