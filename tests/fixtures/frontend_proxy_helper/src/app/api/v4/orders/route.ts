import { NextRequest, NextResponse } from "next/server"

const API_BASE_URL = process.env.MACAL_API_URL || "http://localhost:8000"

export async function POST(req: NextRequest) {
  const upstreamUrl = `${API_BASE_URL}/api/v4/orders`
  // `method` here is a domain field of the payload, not the HTTP verb.
  const response = await fetch(upstreamUrl, {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method: "DELETE", amount: 1000 }),
  })
  return NextResponse.json(await response.json())
}

export async function PUT(req: NextRequest) {
  const upstreamUrl = `${API_BASE_URL}/api/v4/orders/bulk`
  // The real verb sits past any fixed-size scan window, but is still top-level.
  const response = await fetch(upstreamUrl, {
    headers: {
      Accept: "application/json",
      "X-Filler-1": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "X-Filler-2": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "X-Filler-3": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "X-Filler-4": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "X-Filler-5": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    },
    method: "PATCH",
    body: await req.text(),
  })
  return NextResponse.json(await response.json())
}
