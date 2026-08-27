import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained production server for deploy/Dockerfile.frontend.
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;
