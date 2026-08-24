import { NextRequest } from "next/server"
import { proxyMacalApi } from "@/lib/macal-api-proxy"

/**
 * Optional catch-all: 14 concrete routes collapsed into one file. The verbs
 * are declared by the alias re-export, not by named handler functions.
 */
const forward = (req: NextRequest) =>
  proxyMacalApi(req, { errorMessage: "Failed to reach executive backend" })

export { forward as GET, forward as POST, forward as DELETE }
