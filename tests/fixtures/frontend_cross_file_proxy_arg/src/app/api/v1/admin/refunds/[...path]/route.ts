import { NextRequest } from "next/server"
import { proxyUsersApiAdmin } from "@/lib/users-api-admin-proxy"

// A catch-all that splices its segments back into the helper's path argument,
// and forwards whatever verb arrived instead of naming one.
async function forward(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params
  return proxyUsersApiAdmin(`/api/v1/admin/refunds/${path.join("/")}`, {
    method: req.method,
    search: req.nextUrl.search,
  })
}

export async function GET(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  return forward(req, context)
}

export async function POST(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> }
) {
  return forward(req, context)
}
