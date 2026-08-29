import { proxyBackend } from "@/lib/backend-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  return proxyBackend(request, "/demo/sessions", "POST");
}
