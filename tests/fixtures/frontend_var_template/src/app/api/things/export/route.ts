import { NextRequest, NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

export async function GET(req: NextRequest) {
  const search = req.nextUrl.searchParams.toString()
  const upstreamUrl = `${API_BASE_URL}/api/v4/things/export${search ? `?${search}` : ""}`
  const upstream = await fetch(upstreamUrl)
  return NextResponse.json(await upstream.json())
}

export async function PUT(req: NextRequest) {
  const params = new URLSearchParams({ full: "1" })
  const upstreamUrl = `${API_BASE_URL}/api/v4/things/export/?${params.toString()}`
  const upstream = await fetch(upstreamUrl, { method: "PUT", body: await req.text() })
  return NextResponse.json(await upstream.json())
}
