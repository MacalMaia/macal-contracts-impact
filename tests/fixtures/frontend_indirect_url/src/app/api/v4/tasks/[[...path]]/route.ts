import { NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

// The URL is built two hops away from the fetch: a builder function, then a
// local. Nothing that `fetch(...)` can see mentions a path or an env var.
function makeUpstreamUrl(request: Request) {
  const url = new URL(request.url)
  const search = url.search || ""
  const subpath = url.pathname.replace(/^\/api\/v4\/tasks\/?/, "")
  const suffix = subpath ? `/${subpath}` : ""
  return `${API_BASE_URL}/api/v4/tasks${suffix}${search}`
}

async function proxy(request: Request) {
  const upstreamUrl = makeUpstreamUrl(request)
  const response = await fetch(upstreamUrl, {
    method: request.method,
    cache: "no-store",
  })
  return NextResponse.json(await response.json(), { status: response.status })
}

export async function GET(request: Request) {
  return proxy(request)
}

export async function POST(request: Request) {
  return proxy(request)
}

export async function DELETE(request: Request) {
  return proxy(request)
}
