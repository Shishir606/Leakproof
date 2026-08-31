import "server-only";

import type { CaseDetail, CaseList, ExceptionReport, LatestEvals, Scoreboard } from "./types";

const API_BASE_URL = (process.env.API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");
const OPERATOR_API_TOKEN = process.env.LEAKPROOF_OPERATOR_API_TOKEN ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

async function apiGet<T>(path: string): Promise<T> {
  let response: Response;
  try {
    const headers = new Headers({ accept: "application/json" });
    if (OPERATOR_API_TOKEN) headers.set("authorization", `Bearer ${OPERATOR_API_TOKEN}`);
    response = await fetch(`${API_BASE_URL}${path}`, { headers, cache: "no-store" });
  } catch {
    throw new ApiError(`Cannot reach the Leakproof API at ${API_BASE_URL}.`);
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new ApiError(payload?.detail ?? `API request failed (${response.status}).`, response.status);
  }
  return response.json() as Promise<T>;
}

export const getLatestScoreboard = () => apiGet<Scoreboard>("/scoreboard/latest");

export const getLatestEvals = () => apiGet<LatestEvals>("/evals/latest");

export const getExceptionReport = (runId: string) =>
  apiGet<ExceptionReport>(`/scoreboard/${encodeURIComponent(runId)}/exceptions`);

export function getCases(filters: {
  state?: string;
  leakType?: string;
  batchRunId?: string;
  limit?: number;
}) {
  const query = new URLSearchParams();
  if (filters.state) query.set("state", filters.state);
  if (filters.leakType) query.set("leak_type", filters.leakType);
  if (filters.batchRunId) query.set("batch_run_id", filters.batchRunId);
  query.set("limit", String(filters.limit ?? 30));
  return apiGet<CaseList>(`/cases?${query}`);
}

export const getCase = (caseId: string) => apiGet<CaseDetail>(`/cases/${caseId}`);
