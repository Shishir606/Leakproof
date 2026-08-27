const API_BASE_URL = (process.env.API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");

export async function GET(_request: Request, context: { params: Promise<{ caseId: string }> }) {
  const { caseId } = await context.params;
  const response = await fetch(`${API_BASE_URL}/cases/${encodeURIComponent(caseId)}/audit.json`, { cache: "no-store" });
  if (!response.ok) return new Response("Audit record not found", { status: response.status });
  return new Response(await response.text(), {
    headers: {
      "content-type": "application/json",
      "content-disposition": `attachment; filename="${caseId}-audit.json"`,
    },
  });
}
