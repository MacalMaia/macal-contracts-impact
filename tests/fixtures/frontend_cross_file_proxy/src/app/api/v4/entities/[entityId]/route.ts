import { NextRequest } from "next/server"
import { proxyMacalApi } from "@/lib/macal-api-proxy"

// Not a catch-all: a plain dynamic segment still delegates to the same helper,
// and its path is just as absent from this file.
export async function GET(req: NextRequest) {
  return proxyMacalApi(req)
}

export async function PATCH(req: NextRequest) {
  return proxyMacalApi(req, { unwrapData: false })
}
