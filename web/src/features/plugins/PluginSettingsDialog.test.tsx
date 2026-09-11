import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { PluginSettingsResponse } from "@/client"
import { PluginSettingsDialog } from "@/features/plugins/PluginSettingsDialog"
import { API_ROOT } from "@/shared/api/client"
import { pluginSettingField } from "@/tests/fixtures"
import { renderWithRouter } from "@/tests/router"

interface Call {
  url: string
  method: string
  body: string | undefined
}

const SETTINGS: PluginSettingsResponse = {
  plugin: "agent-gates",
  fields: [
    pluginSettingField(),
    pluginSettingField({
      key: "strict",
      type: "bool",
      default: false,
      description: "Fail closed.",
    }),
    pluginSettingField({
      key: "api_token",
      type: "str",
      default: null,
      description: "",
      secret: true,
    }),
    pluginSettingField({
      key: "paths",
      type: "list",
      default: [],
      description: "",
    }),
    pluginSettingField({
      key: "policy",
      type: "str",
      default: "team",
      description: "",
      editable: false,
    }),
  ],
  values: {
    judge_timeout_seconds: 120,
    strict: false,
    api_token: "********",
    paths: ["src", "tests"],
    policy: "team",
  },
}

function mockApi(): Call[] {
  const calls: Call[] = []
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input)
    const method = (init?.method ?? "GET").toUpperCase()
    calls.push({
      url,
      method,
      body: typeof init?.body === "string" ? init.body : undefined,
    })
    if (url === `${API_ROOT}/plugins/agent-gates/settings`) {
      return Response.json(SETTINGS)
    }
    return Response.json({ detail: `unexpected ${url}` }, { status: 500 })
  })
  return calls
}

async function renderDialog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const onOpenChange = vi.fn()
  await renderWithRouter(
    <QueryClientProvider client={client}>
      <PluginSettingsDialog
        pluginName="agent-gates"
        isOpen
        onOpenChange={onOpenChange}
      />
    </QueryClientProvider>,
  )
  return onOpenChange
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe("PluginSettingsDialog", () => {
  it("renders one control per field by type, with the locked one set in config", async () => {
    mockApi()
    await renderDialog()

    const dialog = await screen.findByRole("dialog")
    expect(
      within(dialog).getByRole("heading", { name: "agent-gates settings" }),
    ).toBeVisible()
    const timeout = await within(dialog).findByLabelText(
      "judge_timeout_seconds",
    )
    expect(timeout).toHaveValue("120")
    expect(within(dialog).getByText("Cap on one judge call.")).toBeVisible()
    expect(
      within(dialog).getByRole("switch", { name: "strict" }),
    ).not.toBeChecked()
    expect(within(dialog).getByLabelText("paths")).toHaveValue("src\ntests")
    const policy = within(dialog).getByLabelText("policy")
    expect(policy).toBeDisabled()
    expect(within(dialog).getByText("Set in config.yml")).toBeVisible()
  })

  it("submits only what changed, never the secret mask, and null for a reset", async () => {
    const calls = mockApi()
    const user = userEvent.setup()
    const onOpenChange = await renderDialog()

    const dialog = await screen.findByRole("dialog")
    const timeout = await within(dialog).findByLabelText(
      "judge_timeout_seconds",
    )
    await user.clear(timeout)
    await user.type(timeout, "30")
    await user.click(within(dialog).getByRole("switch", { name: "strict" }))
    await user.click(
      within(dialog).getByRole("button", { name: "Reset paths to default" }),
    )
    await user.click(
      within(dialog).getByRole("button", { name: "Save settings" }),
    )

    await waitFor(() =>
      expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1),
    )
    const [put] = calls.filter((call) => call.method === "PUT")
    expect(put.url).toBe(`${API_ROOT}/plugins/agent-gates/settings`)
    expect(JSON.parse(put.body ?? "")).toEqual({
      values: { judge_timeout_seconds: 30, strict: true, paths: null },
    })
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it("refuses a number that is not one, without a request", async () => {
    const calls = mockApi()
    const user = userEvent.setup()
    await renderDialog()

    const dialog = await screen.findByRole("dialog")
    const timeout = await within(dialog).findByLabelText(
      "judge_timeout_seconds",
    )
    await user.clear(timeout)
    await user.type(timeout, "soon")
    await user.click(
      within(dialog).getByRole("button", { name: "Save settings" }),
    )

    expect(
      await within(dialog).findByText("Must be a whole number."),
    ).toBeVisible()
    expect(calls.filter((call) => call.method === "PUT")).toEqual([])
  })
})
