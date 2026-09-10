import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type {
  InstallPluginRequest,
  InstallPluginResponse,
  MarketplaceResponse,
  PluginManifestSummary,
  PluginsResponse,
} from "@/client"
import { apiFetch, longRequestSignal } from "@/shared/api/client"
import {
  NO_RETRY,
  PLUGIN_DESCRIBE,
  PLUGIN_MARKETPLACE,
  PLUGINS,
} from "@/shared/api/queryKeys"

// Operator-only, all of it: `GET /plugins` answers 403 to anyone else, so a
// caller gates `enabled` on the caller axis rather than reading the refusal.
export function usePlugins(enabled = true) {
  return useQuery({
    queryKey: [PLUGINS],
    queryFn: () => apiFetch<PluginsResponse>("/plugins"),
    // The set changes on an install, a removal, or a restart, and the writes
    // invalidate it; a minute covers the restart.
    staleTime: 60_000,
    enabled,
  })
}

/**
 * The plugins on offer. `refresh` asks the gateway to bypass its own cache of
 * the verified index and the GitHub topic, and is part of the key so a page
 * asking for fresh data does fetch rather than reading the cached listing back.
 */
export function useMarketplace(refresh = false) {
  return useQuery({
    queryKey: [PLUGIN_MARKETPLACE, { refresh }],
    queryFn: () =>
      apiFetch<MarketplaceResponse>(
        refresh ? "/plugins/marketplace?refresh=true" : "/plugins/marketplace",
      ),
    // The gateway caches the listing for ten minutes; there is nothing to gain
    // from asking it more often than that.
    staleTime: 5 * 60_000,
    placeholderData: (previous) => previous,
    // Backed by two outbound fetches gateway-side, so a slow GitHub is the
    // reason this fails, and three sequential tries would hold the socket for
    // the whole time.
    ...NO_RETRY,
  })
}

/**
 * What a repository's plugin declares, read before it is installed. A 422
 * carries the gateway's own reason (no manifest, or one it could not read),
 * which the caller shows as it is.
 */
export function useDescribePlugin(
  repo: string,
  ref: string | null | undefined,
  enabled: boolean,
) {
  const query = new URLSearchParams({ repo })
  if (ref) query.set("ref", ref)
  return useQuery({
    queryKey: [PLUGIN_DESCRIBE, { repo, ref: ref ?? null }],
    queryFn: () =>
      apiFetch<PluginManifestSummary>(
        `/plugins/marketplace/describe?${query.toString()}`,
      ),
    // A manifest changes when the repository does, and a dialog opened twice
    // in a sitting should not read GitHub twice.
    staleTime: 5 * 60_000,
    enabled,
    // One outbound fetch gateway-side, with the same slow-GitHub failure mode
    // as the listing.
    ...NO_RETRY,
  })
}

function useInvalidatePlugins() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: [PLUGINS] })
    // `installed` on every listing entry is derived from the installed set.
    void queryClient.invalidateQueries({ queryKey: [PLUGIN_MARKETPLACE] })
  }
}

// Both installs are bounded by the long deadline rather than the default one.
// An upload carries up to 64 MB across the operator's own uplink, and an
// install has the gateway fetch the archive from GitHub before it answers;
// either can outrun 30s and still succeed, and the server unpacks the archive
// whether or not the browser is still listening, so an abort would report a
// failure for an install that landed and invite a second one.

/** Upload an archive. Multipart, with the file under the `file` field. */
export function useUploadPlugin() {
  const invalidate = useInvalidatePlugins()
  return useMutation({
    mutationFn: (file: File) => {
      const body = new FormData()
      body.append("file", file, file.name)
      return apiFetch<InstallPluginResponse>("/plugins/upload", {
        method: "POST",
        body,
        signal: longRequestSignal(),
      })
    },
    onSuccess: invalidate,
  })
}

/** Install from a GitHub repository, by `owner/name` and an optional ref. */
export function useInstallPlugin() {
  const invalidate = useInvalidatePlugins()
  return useMutation({
    mutationFn: (body: InstallPluginRequest) =>
      apiFetch<InstallPluginResponse>("/plugins/install", {
        method: "POST",
        body: JSON.stringify(body),
        signal: longRequestSignal(),
      }),
    onSuccess: invalidate,
  })
}

/** Remove a plugin installed in the plugins directory. It unloads on restart. */
export function useRemovePlugin() {
  const invalidate = useInvalidatePlugins()
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch<void>(`/plugins/${encodeURIComponent(name)}`, {
        method: "DELETE",
      }),
    onSuccess: invalidate,
  })
}
