import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import type {
  InstalledPlugin,
  MarketplaceResponse,
  PluginManifestSummary,
  PluginsResponse,
} from "@/client"
import { MarketplacePage } from "@/features/plugins/MarketplacePage"
import { API_ROOT } from "@/shared/api/client"
import {
  installedPlugin,
  marketplacePlugin,
  marketplaceResponse,
  pluginManifest,
  pluginsResponse,
} from "@/tests/fixtures"
import { renderWithRouter } from "@/tests/router"

interface Call {
  url: string
  method: string
  body: BodyInit | null | undefined
}

/** What the describe endpoint answers: a manifest, or a refusal with its reason. */
type DescribeAnswer = PluginManifestSummary | { status: number; detail: string }

/**
 * The gateway, with one piece of state: an install or a removal flips
 * `restart_required` and adds or drops the plugin, the way the real one does,
 * so the page's refetch after a write sees what the write changed.
 */
function mockApi({
  plugins = pluginsResponse(),
  marketplace = marketplaceResponse(),
  describe = pluginManifest(),
  writeStatus,
  writeDetail,
}: {
  plugins?: PluginsResponse
  marketplace?: MarketplaceResponse
  /** A promise lets a test hold the answer while it looks at the loading line. */
  describe?: DescribeAnswer | Promise<DescribeAnswer>
  /** What every write answers with, for the refusal cases. */
  writeStatus?: number
  writeDetail?: string
} = {}): Call[] {
  const calls: Call[] = []
  let installed = plugins
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input)
    const method = (init?.method ?? "GET").toUpperCase()
    calls.push({ url, method, body: init?.body })
    if (method !== "GET" && writeStatus !== undefined) {
      return Response.json(
        { detail: writeDetail ?? "refused" },
        { status: writeStatus },
      )
    }
    if (url.startsWith(`${API_ROOT}/plugins/marketplace/describe?`)) {
      const answer = await describe
      return "status" in answer
        ? Response.json({ detail: answer.detail }, { status: answer.status })
        : Response.json(answer)
    }
    if (url.startsWith(`${API_ROOT}/plugins/marketplace`)) {
      return Response.json(marketplace)
    }
    if (url === `${API_ROOT}/plugins`) {
      return Response.json(installed)
    }
    if (method === "POST") {
      const plugin = installedPlugin({
        name: "otari-request-log",
        status: "pending_restart",
        ui: null,
      })
      installed = {
        ...installed,
        plugins: [...installed.plugins, plugin],
        restart_required: true,
      }
      return Response.json({ plugin, restart_required: true }, { status: 201 })
    }
    if (method === "DELETE") {
      const name = decodeURIComponent(url.split("/").pop() ?? "")
      installed = {
        ...installed,
        plugins: installed.plugins.filter((one) => one.name !== name),
        restart_required: true,
      }
      return new Response(null, { status: 204 })
    }
    return Response.json(
      { detail: `unexpected ${method} ${url}` },
      { status: 500 },
    )
  })
  return calls
}

function writes(calls: Call[]): Call[] {
  return calls.filter((call) => call.method !== "GET")
}

function describes(calls: Call[]): Call[] {
  return calls.filter((call) =>
    call.url.startsWith(`${API_ROOT}/plugins/marketplace/describe?`),
  )
}

async function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return renderWithRouter(
    <QueryClientProvider client={client}>
      <MarketplacePage />
    </QueryClientProvider>,
    { url: "/marketplace" },
  )
}

const FAILED: InstalledPlugin = installedPlugin({
  name: "broken-hook",
  version: "0.9.0",
  source: "entry_point",
  status: "failed",
  error: "ModuleNotFoundError: No module named 'broken_hook'",
  ui: null,
})

const VERIFIED = marketplacePlugin({
  name: "agent-gates",
  repo: "mozilla-ai/otari-agent-gates",
  url: "https://github.com/mozilla-ai/otari-agent-gates",
  verified: true,
  version: "0.1.0",
  stars: 40,
})

const COMMUNITY = marketplacePlugin()

afterEach(() => {
  vi.restoreAllMocks()
})

describe("MarketplacePage", () => {
  it("lists installed, verified, and community plugins under their tabs", async () => {
    mockApi({
      plugins: pluginsResponse({
        plugins: [installedPlugin(), FAILED],
        problems: [
          {
            source: "directory",
            location: "/srv/otari/otari-plugins/stray",
            error: "no otari-plugin.toml found",
          },
        ],
      }),
      marketplace: marketplaceResponse({
        verified: [VERIFIED],
        community: [COMMUNITY],
      }),
    })
    const user = userEvent.setup()
    await renderPage()

    const installed = await screen.findByRole("list", {
      name: "Installed plugins",
    })
    expect(within(installed).getByText("agent-gates")).toBeVisible()
    expect(within(installed).getByText("Loaded")).toBeVisible()
    expect(within(installed).getByText("v0.1.0")).toBeVisible()
    expect(within(installed).getByText("Plugins directory")).toBeVisible()
    // What the manifest declares, one chip each, and where to start.
    const [firstRow] = within(installed).getAllByRole("listitem")
    for (const chip of ["API routes", "CLI", "Tables", "Page"]) {
      expect(within(firstRow).getByText(chip)).toBeVisible()
    }
    expect(within(firstRow).queryByText("Watches traffic")).toBeNull()
    expect(
      within(firstRow).getByRole("link", { name: "Getting started" }),
    ).toHaveAttribute(
      "href",
      "https://github.com/mozilla-ai/otari-agent-gates#getting-started",
    )
    // The page a loaded plugin ships is one click away, at its own route.
    expect(
      within(installed).getByRole("link", { name: "Open Agent gates" }),
    ).toHaveAttribute("href", "/plugins/agent-gates")
    // A failed plugin says why, in the gateway's words, and offers no page.
    expect(within(installed).getByText("Failed")).toBeVisible()
    expect(within(installed).getByText(FAILED.error ?? "")).toBeVisible()
    // A Python distribution is not the dashboard's to remove.
    expect(
      within(installed).getByText("Uninstall with pip or uv"),
    ).toBeVisible()
    expect(
      within(installed).getAllByRole("button", { name: "Remove" }),
    ).toHaveLength(1)
    // The candidate that could not be described is listed with its error.
    expect(screen.getByText("/srv/otari/otari-plugins/stray")).toBeVisible()
    expect(screen.getByText("no otari-plugin.toml found")).toBeVisible()

    await user.click(screen.getByRole("button", { name: "Verified (1)" }))
    const verified = await screen.findByRole("list", {
      name: "Verified plugins",
    })
    expect(within(verified).getByText("Verified by mozilla.ai")).toBeVisible()
    expect(within(verified).getByText("agent-gates")).toBeVisible()
    expect(
      within(verified).getByRole("link", {
        name: "mozilla-ai/otari-agent-gates",
      }),
    ).toHaveAttribute("href", "https://github.com/mozilla-ai/otari-agent-gates")

    await user.click(screen.getByRole("button", { name: "Community (1)" }))
    const community = await screen.findByRole("list", {
      name: "Community plugins",
    })
    expect(within(community).getByText("Unverified")).toBeVisible()
    expect(within(community).getByText("12")).toBeVisible()
    expect(
      within(community).getByRole("link", { name: COMMUNITY.repo }),
    ).toHaveAttribute("href", COMMUNITY.url)
  })

  it("disables every install control while installs are off, and names the config line", async () => {
    mockApi({
      plugins: pluginsResponse({
        plugins: [installedPlugin()],
        install_allowed: false,
      }),
      marketplace: marketplaceResponse({
        community: [COMMUNITY],
        install_allowed: false,
      }),
    })
    const user = userEvent.setup()
    await renderPage()

    // Disabled and explained, never hidden: the operator learns what to turn
    // on rather than wondering where the button went.
    expect(
      await screen.findByText(/Installing and removing plugins/),
    ).toHaveTextContent("plugins.allow_install: true")
    expect(screen.getByText("OTARI_PLUGINS_ALLOW_INSTALL=true")).toBeVisible()
    expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled()
    expect(screen.getByLabelText("Archive")).toBeDisabled()

    await user.click(screen.getByRole("button", { name: "Community (1)" }))
    expect(
      await screen.findByRole("button", { name: "Install" }),
    ).toBeDisabled()
  })

  it("holds an unverified install until the repository name is typed", async () => {
    const calls = mockApi({
      marketplace: marketplaceResponse({ community: [COMMUNITY] }),
    })
    const user = userEvent.setup()
    await renderPage()

    await user.click(
      await screen.findByRole("button", { name: "Community (1)" }),
    )
    await user.click(await screen.findByRole("button", { name: "Install" }))

    const dialog = await screen.findByRole("alertdialog")
    expect(
      within(dialog).getByText(/mozilla\.ai has not reviewed/),
    ).toHaveTextContent("provider keys, the database, and every request")
    const confirm = within(dialog).getByRole("button", { name: "Install" })
    expect(confirm).toBeDisabled()

    const field = within(dialog).getByRole("textbox")
    await user.type(field, "example/other-repo")
    expect(confirm).toBeDisabled()
    await user.clear(field)
    await user.type(field, COMMUNITY.repo)
    expect(confirm).toBeEnabled()

    await user.click(confirm)
    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toMatchObject({
      url: `${API_ROOT}/plugins/install`,
      method: "POST",
    })
    expect(JSON.parse(String(writes(calls)[0].body))).toEqual({
      repo: COMMUNITY.repo,
      ref: null,
      force: false,
    })
  })

  it("installs a verified plugin on a plain confirm, then says a restart is due", async () => {
    const calls = mockApi({
      marketplace: marketplaceResponse({ verified: [VERIFIED] }),
    })
    const user = userEvent.setup()
    await renderPage()
    // Nothing to apply yet.
    expect(screen.queryByText(/Restart the gateway to apply it/)).toBeNull()

    await user.click(
      await screen.findByRole("button", { name: "Verified (1)" }),
    )
    await user.click(await screen.findByRole("button", { name: "Install" }))
    const dialog = await screen.findByRole("alertdialog")
    // Reviewed, so no typed confirmation and no warning.
    expect(within(dialog).queryByRole("textbox")).toBeNull()
    expect(within(dialog).queryByText(/has not reviewed/)).toBeNull()
    expect(
      within(dialog).getByText(/Installs agent-gates 0.1.0 from/),
    ).toBeVisible()
    await user.click(within(dialog).getByRole("button", { name: "Install" }))

    // The write, then the re-read it invalidated: the installed list now holds
    // the plugin, and the banner reports what the gateway does.
    expect(
      await screen.findByText(
        "A plugin was installed or removed. Restart the gateway to apply it.",
      ),
    ).toBeVisible()
    const installed = await screen.findByRole("list", {
      name: "Installed plugins",
    })
    expect(within(installed).getByText("otari-request-log")).toBeVisible()
    expect(within(installed).getByText("Restart required")).toBeVisible()
    expect(screen.queryByRole("alertdialog")).toBeNull()
    expect(
      calls.filter(
        (call) => call.method === "GET" && call.url === `${API_ROOT}/plugins`,
      ).length,
    ).toBeGreaterThan(1)
  })

  it("shows the gateway's refusal inside the dialog", async () => {
    mockApi({
      marketplace: marketplaceResponse({ community: [COMMUNITY] }),
      writeStatus: 422,
      writeDetail: "The archive holds no otari-plugin.toml.",
    })
    const user = userEvent.setup()
    await renderPage()

    await user.click(
      await screen.findByRole("button", { name: "Community (1)" }),
    )
    await user.click(await screen.findByRole("button", { name: "Install" }))
    const dialog = await screen.findByRole("alertdialog")
    await user.type(within(dialog).getByRole("textbox"), COMMUNITY.repo)
    await user.click(within(dialog).getByRole("button", { name: "Install" }))

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The archive holds no otari-plugin.toml.",
    )
    // Still open, so the operator reads the reason where they acted.
    expect(screen.getByRole("alertdialog")).toBeInTheDocument()
  })

  it("uploads a picked archive as multipart, after its name is typed", async () => {
    const calls = mockApi()
    const user = userEvent.setup()
    await renderPage()

    const archive = new File(["zip"], "my-plugin.zip", {
      type: "application/zip",
    })
    const input = await screen.findByLabelText("Archive")
    await user.upload(input, archive)
    // The curl block follows the picked file, against this origin.
    await user.click(screen.getByRole("button", { name: "Or use curl" }))
    expect(screen.getByLabelText("curl")).toHaveValue(
      [
        `curl -X POST "${window.location.origin}${API_ROOT}/plugins/upload" \\`,
        '  -H "Authorization: Bearer $OTARI_MASTER_KEY" \\',
        '  -F "file=@my-plugin.zip"',
      ].join("\n"),
    )

    await user.click(screen.getByRole("button", { name: "Upload" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(within(dialog).getByText(/has not reviewed/)).toBeVisible()
    const confirm = within(dialog).getByRole("button", { name: "Upload" })
    expect(confirm).toBeDisabled()
    await user.type(within(dialog).getByRole("textbox"), "my-plugin.zip")
    await user.click(confirm)

    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    const [upload] = writes(calls)
    expect(upload.url).toBe(`${API_ROOT}/plugins/upload`)
    expect(upload.body).toBeInstanceOf(FormData)
    const sent = (upload.body as FormData).get("file")
    expect(sent).toBeInstanceOf(File)
    expect((sent as File).name).toBe("my-plugin.zip")
  })

  it("removes a directory plugin through the confirm dialog", async () => {
    const calls = mockApi({
      plugins: pluginsResponse({ plugins: [installedPlugin()] }),
    })
    const user = userEvent.setup()
    await renderPage()

    await user.click(await screen.findByRole("button", { name: "Remove" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(within(dialog).getByText(/agent-gates is deleted/)).toBeVisible()
    await user.click(
      within(dialog).getByRole("button", { name: "Remove plugin" }),
    )

    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toMatchObject({
      url: `${API_ROOT}/plugins/agent-gates`,
      method: "DELETE",
    })
    expect(
      await screen.findByText(/Restart the gateway to apply it/),
    ).toBeVisible()
    expect(screen.queryByRole("alertdialog")).toBeNull()
  })

  it("says what a plugin adds from the listing's own manifest, without a describe call", async () => {
    const calls = mockApi({
      marketplace: marketplaceResponse({
        verified: [
          {
            ...VERIFIED,
            manifest: pluginManifest({
              name: "agent-gates",
              contributes: ["routes", "cli", "migrations", "ui"],
              config_keys: ["agent_gates.policy", "agent_gates.strict"],
              getting_started:
                "https://github.com/mozilla-ai/otari-agent-gates#getting-started",
            }),
          },
        ],
      }),
    })
    const user = userEvent.setup()
    await renderPage()

    await user.click(
      await screen.findByRole("button", { name: "Verified (1)" }),
    )
    await user.click(await screen.findByRole("button", { name: "Install" }))
    const dialog = await screen.findByRole("alertdialog")

    expect(within(dialog).getByText("What it adds")).toBeVisible()
    const rows = within(dialog)
      .getAllByRole("listitem")
      .map((row) => row.textContent)
    expect(rows).toEqual([
      "API routes under /api/v1/plugins/agent-gates",
      "otari command groups",
      "database tables of its own",
      "a page in the dashboard",
    ])
    expect(
      within(dialog).getByText(/Reads these config keys/),
    ).toHaveTextContent("agent_gates.policy, agent_gates.strict")
    expect(
      within(dialog).getByRole("link", { name: "Getting started" }),
    ).toHaveAttribute(
      "href",
      "https://github.com/mozilla-ai/otari-agent-gates#getting-started",
    )
    expect(describes(calls)).toHaveLength(0)
  })

  it("shows a kind this dashboard does not know by its word, with the gateway's refusal", async () => {
    mockApi({
      marketplace: marketplaceResponse({
        verified: [
          {
            ...VERIFIED,
            manifest: pluginManifest({
              name: "agent-gates",
              contributes: ["routes", "kernel"],
              config_keys: [],
              needs_newer_gateway:
                "declares kernel, which this gateway (0.30.0) does not know",
            }),
          },
        ],
      }),
    })
    const user = userEvent.setup()
    await renderPage()

    await user.click(
      await screen.findByRole("button", { name: "Verified (1)" }),
    )
    await user.click(await screen.findByRole("button", { name: "Install" }))
    const dialog = await screen.findByRole("alertdialog")

    expect(
      within(dialog).getByText(/This gateway will not load it/),
    ).toHaveTextContent(
      "declares kernel, which this gateway (0.30.0) does not know",
    )
    const rows = within(dialog)
      .getAllByRole("listitem")
      .map((row) => row.textContent)
    expect(rows).toEqual([
      "API routes under /api/v1/plugins/agent-gates",
      "kernel (needs a newer gateway)",
    ])
  })

  it("reads the manifest from the repository when the listing has none", async () => {
    let answer: (manifest: DescribeAnswer) => void = () => {}
    const calls = mockApi({
      marketplace: marketplaceResponse({
        community: [{ ...COMMUNITY, ref: "v0.2.0" }],
      }),
      describe: new Promise<DescribeAnswer>((resolve) => {
        answer = resolve
      }),
    })
    const user = userEvent.setup()
    await renderPage()

    await user.click(
      await screen.findByRole("button", { name: "Community (1)" }),
    )
    await user.click(await screen.findByRole("button", { name: "Install" }))
    const dialog = await screen.findByRole("alertdialog")

    expect(
      await within(dialog).findByText("Reading the plugin's manifest…"),
    ).toBeVisible()
    expect(describes(calls)).toHaveLength(1)
    expect(describes(calls)[0].url).toBe(
      `${API_ROOT}/plugins/marketplace/describe?repo=example%2Fotari-request-log&ref=v0.2.0`,
    )

    answer(pluginManifest({ contributes: ["traffic"], config_keys: [] }))
    expect(await within(dialog).findByText("What it adds")).toBeVisible()
    expect(
      within(dialog).getByText(
        "watches inference traffic passing through this gateway",
      ),
    ).toBeVisible()
    expect(within(dialog).queryByText(/Reads these config keys/)).toBeNull()
    expect(
      within(dialog).queryByText("Reading the plugin's manifest…"),
    ).toBeNull()
    // The warning and the typed gate are unchanged by what was read.
    expect(within(dialog).getByText(/has not reviewed/)).toBeVisible()
    expect(
      within(dialog).getByRole("button", { name: "Install" }),
    ).toBeDisabled()
  })

  it("shows why a plugin could not be described and still lets it be installed", async () => {
    const calls = mockApi({
      marketplace: marketplaceResponse({ community: [COMMUNITY] }),
      describe: {
        status: 422,
        detail: "The repository has no otari-plugin.toml.",
      },
    })
    const user = userEvent.setup()
    await renderPage()

    await user.click(
      await screen.findByRole("button", { name: "Community (1)" }),
    )
    await user.click(await screen.findByRole("button", { name: "Install" }))
    const dialog = await screen.findByRole("alertdialog")

    expect(
      await within(dialog).findByText(
        "The repository has no otari-plugin.toml.",
      ),
    ).toBeVisible()
    expect(
      within(dialog).getByText(/could not be described before install/),
    ).toBeVisible()
    expect(within(dialog).queryByText("What it adds")).toBeNull()
    // Not the install's own refusal slot, which is still empty.
    expect(within(dialog).queryByRole("alert")).toBeNull()

    const confirm = within(dialog).getByRole("button", { name: "Install" })
    await user.type(within(dialog).getByRole("textbox"), COMMUNITY.repo)
    expect(confirm).toBeEnabled()
    await user.click(confirm)
    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0].url).toBe(`${API_ROOT}/plugins/install`)
  })
})
