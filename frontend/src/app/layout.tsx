import type { Metadata } from "next";
import { Inter, IBM_Plex_Mono } from "next/font/google";

import { AppShell } from "@/components/app-shell";
import { Providers } from "@/components/providers";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-inter",
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "PulseRecover — Command Center",
    template: "%s · PulseRecover",
  },
  description:
    "AI Payment Reliability & Revenue Recovery Engine. Probabilistic AI proposes, deterministic policy decides, verification proves.",
  icons: { icon: "/favicon.svg" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${plexMono.variable} dark`}>
      <body className="font-sans">
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
        <script
          dangerouslySetInnerHTML={{
            __html: `
              // Suppress known devtools/extension performance observer errors that
              // do not originate from application code (e.g. React DevTools
              // reportAllChanges accessing undefined PerformanceEntry.startTime).
              window.addEventListener('error', function(event) {
                var msg = (event.message || '').trim();
                if (msg.indexOf('startTime') !== -1 && event.filename === '') {
                  event.preventDefault();
                  event.stopPropagation();
                }
              });
            `,
          }}
        />
      </body>
    </html>
  );
}
