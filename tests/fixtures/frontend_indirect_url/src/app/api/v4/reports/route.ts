import { NextRequest, NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url)
  const id = searchParams.get("id")
  const params = new URLSearchParams({ page: "1" })

  // Two branches, two real upstream paths: picking one arbitrarily would leave
  // the other looking unconsumed.
  const baseUrl = `${API_BASE_URL}/api/v4/reports`
  const url = id ? `${baseUrl}/${encodeURIComponent(id)}` : `${baseUrl}?${params.toString()}`

  const upstream = await fetch(url, { cache: "no-store" })
  return NextResponse.json(await upstream.json())
}

export async function POST(req: NextRequest) {
  // A local named `url` again — scoped to THIS handler, so the GET branches
  // above must not leak into it.
  const url = `${API_BASE_URL}/api/v4/reports/bulk`
  const upstream = await fetch(url, { method: "POST", body: await req.text() })
  return NextResponse.json(await upstream.json())
}
