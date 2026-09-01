"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

/**
 * Shared GET /api/v1/merchant/connection subscription — one cache entry feeds
 * the topbar badge, the Command Center empty states and the Settings card, so
 * every surface agrees on the live connection state. Polls lightly; retry 0
 * so an unavailable endpoint surfaces immediately instead of hanging.
 */
export function useMerchantConnection() {
  return useQuery({
    queryKey: ["merchant", "connection"],
    queryFn: () => api.merchant.connection(),
    refetchInterval: 30_000,
    retry: 0,
  });
}
