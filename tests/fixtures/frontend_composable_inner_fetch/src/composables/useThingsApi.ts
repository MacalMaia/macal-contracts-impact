export const useThingsApi = () => {
  const USERS_API_URL =
    import.meta.env.VITE_USERS_API_URL || 'http://localhost:8001/api/v1'

  // The path parameter belongs to THIS closure, which no other module can
  // import: the exported composable is not a cross-file proxy helper, and the
  // calls below are read in place.
  const apiFetch = async <T>(endpoint: string): Promise<T> => {
    const response = await fetch(`${USERS_API_URL}${endpoint}`)
    return response.json() as Promise<T>
  }

  const listThings = () => apiFetch('/things')
  const createThing = () => apiFetch('/things', { method: 'POST' })

  return { listThings, createThing }
}
