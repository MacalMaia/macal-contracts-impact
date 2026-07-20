import { NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

// Shared proxy: the fetch lives here, but the verbs it serves are declared by
// the exported handlers below. `search` carries the whole query string, so the
// trailing interpolation is not a path param.
async function proxy(request: Request) {
  const url = new URL(request.url)
  const search = url.search || ""
  const upstreamUrl = `${API_BASE_URL}/api/v4/permissions/${search}`
  const init: RequestInit = {
    method: request.method,
    headers: { Accept: "application/json" },
  }
  const response = await fetch(upstreamUrl, init)
  return NextResponse.json(await response.json())
}

export async function GET(request: Request) {
  return proxy(request)
}

export async function POST(request: Request) {
  return proxy(request)
}
