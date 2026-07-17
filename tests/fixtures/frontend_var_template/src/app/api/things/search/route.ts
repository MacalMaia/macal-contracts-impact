import { NextRequest, NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

export async function POST(req: NextRequest) {
  const upstreamUrl = `${API_BASE_URL}/api/v4/things/search`
  const body = await req.text()
  const upstream = await fetch(upstreamUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  })
  return NextResponse.json(await upstream.json())
}
