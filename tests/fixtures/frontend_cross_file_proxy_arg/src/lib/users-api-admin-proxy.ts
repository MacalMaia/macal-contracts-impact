import { NextResponse } from "next/server"
import { getAccessToken, getSessionEmail } from "@/lib/auth0"

const API_BASE_URL = process.env.MACAL_USERS_API_URL || "http://localhost:8001"
const API_KEY = process.env.MACAL_USERS_API_KEY || ""

/**
 * Shared proxy where the caller — not the request — supplies the upstream
 * path. Same cross-file shape as `proxyMacalApi`, opposite path source.
 */
export async function proxyUsersApiAdmin(
  backendPath: string,
  init: { method?: string; body?: unknown; search?: string } = {}
) {
  const email = await getSessionEmail()
  if (!email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
  }
  const accessToken = await getAccessToken()
  const upstream = await fetch(`${API_BASE_URL}${backendPath}${init.search ?? ""}`, {
    method: init.method ?? "GET",
    headers: {
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      "X-API-Key": API_KEY,
      "X-Admin-Email": email,
    },
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  })
  return NextResponse.json(await upstream.json(), { status: upstream.status })
}
