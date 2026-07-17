import { NextRequest, NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params
  const upstreamUrl = `${API_BASE_URL}/api/v4/things/${encodeURIComponent(id)}`
  const upstream = await fetch(upstreamUrl, {
    headers: { Accept: "application/json" },
  })
  return NextResponse.json(await upstream.json())
}

export async function DELETE(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params
  const upstreamUrl = `${API_BASE_URL}/api/v4/things/${encodeURIComponent(id)}`
  const upstream = await fetch(upstreamUrl, { method: "DELETE" })
  return NextResponse.json(await upstream.json())
}
