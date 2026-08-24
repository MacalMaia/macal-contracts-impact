import { NextRequest } from "next/server"
import { proxyMacalApi } from "@/lib/macal-api-proxy"

/**
 * Required catch-all, and the one handler that does NOT forward auth: the
 * upstream is public and only the session gate lives here.
 */
const forward = (req: NextRequest) =>
  proxyMacalApi(req, {
    forwardAuth: false,
    unwrapData: false,
    errorMessage: "Failed to resolve SII predio",
  })

export { forward as GET }
