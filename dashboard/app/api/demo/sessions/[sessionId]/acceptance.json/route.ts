import { proxyBackend } from "@/lib/backend-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  const { sessionId } = await params;
  return proxyBackend(request, `/demo/sessions/${encodeURIComponent(sessionId)}/acceptance.json`, "GET");
}
