import "server-only";

import { NextResponse } from "next/server";

const API_BASE_URL = (process.env.API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");

export async function proxyBackend(
  request: Request,
  path: string,
  method: "GET" | "POST",
): Promise<NextResponse> {
  const headers = new Headers({ accept: "application/json" });
  let body: string | undefined;
  if (method === "POST") {
    headers.set("content-type", "application/json");
    body = await request.text();
  }

  const sessionToken = request.headers.get("x-leakproof-session-token");
  if (sessionToken) headers.set("x-leakproof-session-token", sessionToken);
  const recoveryToken = request.headers.get("x-leakproof-recovery-token");
  if (recoveryToken) headers.set("x-leakproof-recovery-token", recoveryToken);
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (forwardedFor) headers.set("x-forwarded-for", forwardedFor);

  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body,
      cache: "no-store",
    });
    const responseHeaders = new Headers({
      "content-type": response.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    });
    const retryAfter = response.headers.get("retry-after");
    if (retryAfter) responseHeaders.set("retry-after", retryAfter);
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: responseHeaders,
    });
  } catch {
    return NextResponse.json(
      {
        error: {
          code: "api_unavailable",
          message: "The recovery API is temporarily unavailable.",
          retryable: true,
        },
      },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }
}
