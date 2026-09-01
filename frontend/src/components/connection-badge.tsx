"use client";

import * as React from "react";

import { useEnvironment } from "@/components/environment-provider";
import { useMerchantConnection } from "@/components/merchant-connection";
import { Badge } from "@/components/ui/badge";

/**
 * The topbar's truth badge — it always says exactly which world the data
 * comes from:
 *   real merchant mode → "Razorpay Test Mode · Connected" (emerald) or
 *     "… · Not connected" (amber), straight from /merchant/connection;
 *   research mode → "Synthetic Research" (slate);
 *   connection endpoint unreachable → a neutral "Razorpay Test Mode" that
 *     claims nothing about connectivity.
 */
export function ConnectionBadge() {
  const { environment } = useEnvironment();
  const connection = useMerchantConnection();

  if (environment === "research") {
    return (
      <Badge variant="info" title="Research Lab — synthetic simulator data, not merchant activity">
        Synthetic Research
      </Badge>
    );
  }

  if (connection.data) {
    const keyHint = connection.data.key_id_masked ? ` · ${connection.data.key_id_masked}` : "";
    return connection.data.connected ? (
      <Badge variant="success" title={`Razorpay Test Mode connection live${keyHint}`}>
        Razorpay Test Mode · Connected
      </Badge>
    ) : (
      <Badge
        variant="warning"
        title="No live Razorpay Test Mode connection — open Settings to connect"
      >
        Razorpay Test Mode · Not connected
      </Badge>
    );
  }

  return (
    <Badge
      variant="outline"
      title="Connection state unavailable — the merchant connection API did not answer"
    >
      Razorpay Test Mode
    </Badge>
  );
}
