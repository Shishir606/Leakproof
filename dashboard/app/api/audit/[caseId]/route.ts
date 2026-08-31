import "server-only";

const API_BASE_URL = (process.env.API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");
const OPERATOR_API_TOKEN = process.env.LEAKPROOF_OPERATOR_API_TOKEN ?? "";

export async function GET(_request: Request, context: { params: Promise<{ caseId: string }> }) {
  if (process.env.LEAKPROOF_OPERATOR_UI_ENABLED !== "true") {
    return new Response("Not found", { status: 404 });
  }
  const { caseId } = await context.params;
  const headers = new Headers({ accept: "application/json" });
  if (OPERATOR_API_TOKEN) headers.set("authorization", `Bearer ${OPERATOR_API_TOKEN}`);
  const response = await fetch(`${API_BASE_URL}/cases/${encodeURIComponent(caseId)}/audit.json`, {
    headers,
    cache: "no-store",
  });
  if (!response.ok) return new Response("Audit record not found", { status: response.status });
  return new Response(await response.text(), {
    headers: {
      "content-type": "application/json",
      "content-disposition": `attachment; filename="${caseId}-audit.json"`,
    },
  });
}
