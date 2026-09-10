import { createFileRoute } from "@tanstack/react-router"

import { MarketplacePage } from "@/features/plugins/MarketplacePage"

export const Route = createFileRoute("/marketplace")({
  component: MarketplacePage,
})
