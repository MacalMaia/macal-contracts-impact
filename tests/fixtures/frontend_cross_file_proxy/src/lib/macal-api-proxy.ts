import { NextResponse } from "next/server"
import { getAccessToken } from "@/lib/auth0"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

type ProxyMacalApiOptions = {
  /** Attach the Auth0 Bearer to the upstream call. */
  forwardAuth?: boolean
  /** macal-api wraps in `{ success, data }`; `false` passes the body through. */
  unwrapData?: boolean
  errorMessage?: string
}

/**
 * Shared macal-api proxy for the `/api/v4/*` routes.
 *
 * The frontend routes mirror the backend path 1:1, so pathname and query
 * travel verbatim — there is no literal path anywhere in this file, and the
 * route files that delegate here have no env var. The edge only exists as the
 * pair of the two.
 */
export async function proxyMacalApi(
  request: Request,
  {
    forwardAuth = true,
    unwrapData = true,
    errorMessage = "Upstream request failed",
  }: ProxyMacalApiOptions = {}
) {
  try {
    const token = await getAccessToken()
    if (!token) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 })
    }

    const { pathname, search } = new URL(request.url)
    const hasBody = request.method !== "GET" && request.method !== "HEAD"

    const upstream = await fetch(`${API_BASE_URL}${pathname}${search}`, {
      method: request.method,
      headers: {
        ...(forwardAuth ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: hasBody ? await request.arrayBuffer() : undefined,
      cache: "no-store",
    })

    const json = await upstream.json()
    return NextResponse.json(unwrapData ? (json?.data ?? json) : json, {
      status: upstream.status,
    })
  } catch (err) {
    return NextResponse.json(
      { error: (err as Error).message || errorMessage },
      { status: 500 }
    )
  }
}
