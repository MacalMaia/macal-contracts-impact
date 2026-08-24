import { NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

// The front mounts this subtree at /api/admin but the backend serves it at
// /api/v3: the path must come from the template, not from the file's location.
function makeUpstreamUrl(request: Request) {
  const url = new URL(request.url)
  const search = url.search || ""
  const subpath = url.pathname.replace(/^\/api\/admin\//, "")
  const base = `${API_BASE_URL}/api/v3`
  return `${base}/${subpath}${search}`
}

export async function GET(request: Request) {
  const response = await fetch(makeUpstreamUrl(request), { cache: "no-store" })
  return NextResponse.json(await response.json())
}
