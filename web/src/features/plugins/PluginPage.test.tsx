import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { PluginsResponse } from "@/client"
import { PluginPage } from "@/features/plugins/PluginPage"
import { installedPlugin, pluginsResponse } from "@/tests/fixtures"
import { renderWithRouter } from "@/tests/router"

function mockApi(plugins: PluginsResponse) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
    Response.json(plugins),
  )
}

async function renderAt(url: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return renderWithRouter(
    <QueryClientProvider client={client}>
      <PluginPage />
    </QueryClientProvider>,
    // Mounted at the parameterized path the real route uses, so the page
    // reads `name` the way it does in the app.
    { url, path: "/plugins/$name" },
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe("PluginPage", () => {
  it("frames the plugin's page at its url, titled with its label", async () => {
    mockApi(pluginsResponse({ plugins: [installedPlugin()] }))
    await renderAt("/plugins/agent-gates")

    const frame = await screen.findByTitle("Agent gates")
    expect(frame.tagName).toBe("IFRAME")
    expect(frame).toHaveAttribute("src", "/plugins/agent-gates/ui/")
    // Scripts, forms and the session cookie stay; the rest of the sandbox
    // holds.
    const sandbox = (frame.getAttribute("sandbox") ?? "").split(" ")
    expect(sandbox).toEqual(
      expect.arrayContaining([
        "allow-scripts",
        "allow-forms",
        "allow-same-origin",
      ]),
    )
    expect(sandbox).not.toContain("allow-top-navigation")
    expect(
      screen.getByRole("heading", { level: 1, name: "Agent gates" }),
    ).toBeVisible()
    expect(
      screen.getByRole("link", { name: "Open in a new tab" }),
    ).toHaveAttribute("href", "/plugins/agent-gates/ui/")
  })

  it("says so when no plugin has that name", async () => {
    mockApi(pluginsResponse({ plugins: [installedPlugin()] }))
    await renderAt("/plugins/nope")

    expect(
      await screen.findByRole("heading", { name: "No plugin named nope" }),
    ).toBeVisible()
    expect(screen.queryByTitle("Agent gates")).toBeNull()
    expect(
      screen.getByRole("button", { name: "Open Marketplace" }),
    ).toBeVisible()
  })

  it("says so when the plugin failed to load, with the gateway's error", async () => {
    mockApi(
      pluginsResponse({
        plugins: [
          installedPlugin({
            status: "failed",
            error: "ImportError: cannot import name 'register'",
          }),
        ],
      }),
    )
    await renderAt("/plugins/agent-gates")

    expect(
      await screen.findByRole("heading", { name: "agent-gates did not load" }),
    ).toBeVisible()
    expect(
      screen.getByText("ImportError: cannot import name 'register'"),
    ).toBeVisible()
    expect(document.querySelector("iframe")).toBeNull()
  })

  it("says so when the plugin ships no page", async () => {
    mockApi(pluginsResponse({ plugins: [installedPlugin({ ui: null })] }))
    await renderAt("/plugins/agent-gates")

    expect(
      await screen.findByRole("heading", { name: "agent-gates has no page" }),
    ).toBeVisible()
    expect(document.querySelector("iframe")).toBeNull()
  })
})
