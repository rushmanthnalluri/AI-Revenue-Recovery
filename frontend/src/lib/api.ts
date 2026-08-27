/**
 * Fully typed client for the PulseRecover API (contracts/openapi.json).
 *
 * - Base URL: NEXT_PUBLIC_API_BASE_URL (default http://localhost:8000)
 * - Auth: X-API-Key header from NEXT_PUBLIC_API_KEY (mutating routes need it)
 * - Errors: throws `ApiError` parsed from the backend error envelope
 *   {"error": {"code", "message", "request_id"}}; network/timeout failures
 *   surface as ApiError with status 0 so the UI can show "backend unreachable"
 * - Timeout: 10s per request (AbortSignal.timeout); synchronous-long calls
 *   (demo.triggerScenario, evaluation.run) override to 120s
 *   (LONG_RUNNING_TIMEOUT_MS)
 */

import type {
  ActionResponse,
  ApiErrorEnvelope,
  ApproveRequest,
  AuditListParams,
  AuditListResponse,
  CancelRequest,
  DashboardSummary,
  DashboardTimeseries,
  DemoResetResponse,
  DetectionRunRequest,
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
  OpportunityDetail,
  OpportunityListParams,
  OpportunityListResponse,
  PageParams,
  QueryValue,
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
  const { method = "GET", query, body, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS } = options;

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
  const text = await response.text();
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
    summary: () => request<DashboardSummary>("/api/v1/dashboard/summary"),
    timeseries: (params: TimeseriesParams = {}) =>
      request<DashboardTimeseries>("/api/v1/dashboard/timeseries", {
        query: {
          metric: params.metric ?? "payment_success_rate",
          granularity: params.granularity ?? ("hour" satisfies Granularity),
          window_hours: params.window_hours ?? 24,
        },
      }),
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
  },
  audit: {
    list: (params: AuditListParams = {}) =>
      request<AuditListResponse>("/api/v1/audit", { query: { ...params } }),
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
};

export { BASE_URL as API_BASE_URL };
