import { NextRequest } from "next/server"
import { proxyEither } from "@/lib/multi-proxy"

export async function GET(req: NextRequest) {
  return proxyEither(req, req.nextUrl.searchParams.get("u") === "1")
}
