import { NextRequest } from "next/server"
import { proxyUsersApiAdmin } from "@/lib/users-api-admin-proxy"

// No `method:` option: the helper's own `init.method ?? "GET"` decides, so the
// upstream verb is GET even though the handler that calls it is a POST.
export async function POST(req: NextRequest) {
  return proxyUsersApiAdmin("/api/v1/admin/payment-request-batches/", {
    search: req.nextUrl.search,
  })
}

// A path the caller computes: guessing it would be worse than a warning.
export async function DELETE(req: NextRequest) {
  const target = await req.text()
  return proxyUsersApiAdmin(target, { method: "DELETE" })
}
