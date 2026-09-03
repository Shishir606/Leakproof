import { proxyBackend } from "@/lib/backend-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyBackend(request, "/demo/scenarios", "GET");
}
