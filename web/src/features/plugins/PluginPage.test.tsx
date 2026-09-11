import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { PluginsResponse } from "@/client"
import { PluginPage } from "@/features/plugins/PluginPage"
import { installedPlugin, pluginPage, pluginsResponse } from "@/tests/fixtures"
import { renderWithRouter } from "@/tests/router"

function mockApi(plugins: PluginsResponse) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
    Response.json(plugins),
  )
}

async function renderAt(url: string, path = "/plugins/$name") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return renderWithRouter(
    <QueryClientProvider client={client}>
      <PluginPage />
    </QueryClientProvider>,
    // Mounted at the parameterized path the real route uses, so the page
    // reads `name` the way it does in the app. The probe is where a navigation
    // the frame asks for can be seen to land.
    { url, path, routes: [{ path: "/keys", element: <div>KEYS PAGE</div> }] },
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

  it("frames a named page at its own route, and says so when none has that id", async () => {
    mockApi(
      pluginsResponse({
        plugins: [
          installedPlugin({
            pages: [
              pluginPage(),
              pluginPage({
                id: "runs",
                label: "Runs",
                url: "/plugins/agent-gates/ui/runs/#/runs",
                path: "/plugins/agent-gates/runs",
              }),
            ],
          }),
        ],
      }),
    )
    await renderAt("/plugins/agent-gates/runs", "/plugins/$name/$page")

    const frame = await screen.findByTitle("Runs")
    expect(frame).toHaveAttribute("src", "/plugins/agent-gates/ui/runs/#/runs")

    vi.restoreAllMocks()
    mockApi(pluginsResponse({ plugins: [installedPlugin()] }))
    await renderAt("/plugins/agent-gates/nope", "/plugins/$name/$page")
    expect(
      await screen.findByRole("heading", {
        name: "agent-gates has no page named nope",
      }),
    ).toBeVisible()
  })

  it("tells the frame the theme when it loads, and follows a navigation it asks for", async () => {
    mockApi(pluginsResponse({ plugins: [installedPlugin()] }))
    document.documentElement.dataset.theme = "dark"
    await renderAt("/plugins/agent-gates")

    const frame = (await screen.findByTitle("Agent gates")) as HTMLIFrameElement
    const posted = vi.fn()
    // jsdom gives the frame a window; what it is told is what is asserted.
    Object.defineProperty(frame, "contentWindow", {
      value: { postMessage: posted },
    })
    frame.dispatchEvent(new Event("load"))
    expect(posted).toHaveBeenCalledWith(
      { type: "otari:theme", theme: "dark" },
      window.location.origin,
    )

    // A message from the frame's own window, on this origin, moves the dashboard.
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "otari:navigate", to: "/keys" },
        origin: window.location.origin,
        source: frame.contentWindow,
      }),
    )
    expect(await screen.findByText("KEYS PAGE")).toBeVisible()
    delete document.documentElement.dataset.theme
  })

  it("shows a notice the frame asks for, and ignores a message from anywhere else", async () => {
    mockApi(pluginsResponse({ plugins: [installedPlugin()] }))
    await renderAt("/plugins/agent-gates")

    const frame = (await screen.findByTitle("Agent gates")) as HTMLIFrameElement
    Object.defineProperty(frame, "contentWindow", {
      value: { postMessage: vi.fn() },
    })
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "otari:toast", title: "Elsewhere" },
        origin: window.location.origin,
        source: window,
      }),
    )
    expect(screen.queryByText("Elsewhere")).toBeNull()

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "otari:toast",
          title: "Policy saved",
          description: "Three gates.",
          variant: "success",
        },
        origin: window.location.origin,
        source: frame.contentWindow,
      }),
    )
    const notice = await screen.findByRole("status")
    expect(within(notice).getByText("Policy saved")).toBeVisible()
    expect(within(notice).getByText("Three gates.")).toBeVisible()
    await userEvent.click(
      within(notice).getByRole("button", { name: "Dismiss notice" }),
    )
    expect(screen.queryByRole("status")).toBeNull()
  })

  it("says so when the plugin ships no page", async () => {
    mockApi(
      pluginsResponse({ plugins: [installedPlugin({ ui: null, pages: [] })] }),
    )
    await renderAt("/plugins/agent-gates")

    expect(
      await screen.findByRole("heading", { name: "agent-gates has no page" }),
    ).toBeVisible()
    expect(document.querySelector("iframe")).toBeNull()
  })
})
