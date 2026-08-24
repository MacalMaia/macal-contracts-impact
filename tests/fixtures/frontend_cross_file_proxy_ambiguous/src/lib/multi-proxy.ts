const USERS_URL = process.env.MACAL_USERS_API_URL || "http://localhost:8001"
const MACAL_URL = process.env.MACAL_API_URL || "http://localhost:8000"

// One helper, two backends chosen at runtime: attributing its callers to
// either service would be a coin flip.
export async function proxyEither(request: Request, useUsers: boolean) {
  const { pathname } = new URL(request.url)
  return useUsers
    ? fetch(`${USERS_URL}${pathname}`)
    : fetch(`${MACAL_URL}${pathname}`)
}
