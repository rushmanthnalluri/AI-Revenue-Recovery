import type { Metadata } from "next";

import { CommandCenterScreen } from "@/components/command-center/command-center-screen";

export const metadata: Metadata = {
  title: "Command Center",
};

export default function CommandCenterPage() {
  return <CommandCenterScreen />;
}
