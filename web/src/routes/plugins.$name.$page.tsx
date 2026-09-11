import { createFileRoute } from "@tanstack/react-router"

import { PluginPage } from "@/features/plugins/PluginPage"

export const Route = createFileRoute("/plugins/$name/$page")({
  component: PluginPage,
})
