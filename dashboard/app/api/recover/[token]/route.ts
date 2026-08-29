import { proxyBackend } from "@/lib/backend-proxy";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ token: string }> },
) {
  const { token } = await params;
  return proxyBackend(request, `/recover/${encodeURIComponent(token)}`, "GET");
}
