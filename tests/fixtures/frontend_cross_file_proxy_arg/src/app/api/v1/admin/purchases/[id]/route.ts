import { NextRequest } from "next/server"
import { proxyUsersApiAdmin } from "@/lib/users-api-admin-proxy"

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params
  return proxyUsersApiAdmin(`/api/v1/admin/purchases/${encodeURIComponent(id)}`)
}

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params
  return proxyUsersApiAdmin(
    `/api/v1/admin/purchases/${encodeURIComponent(id)}`,
    { method: "PATCH", body: await req.json() }
  )
}
