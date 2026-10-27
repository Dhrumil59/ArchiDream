/** @type {import('next').NextConfig} */
const nextConfig = {
  // DXF uploads proxy through the rewrite below. Two default limits break
  // large/slow uploads: the 10MB body cap silently truncates bigger CAD
  // files, and the 30s proxy timeout resets the request while the extraction
  // engine is still running (a dense DXF render takes ~60s) -> the upload
  // fails with "Internal Server Error". Raise both.
  experimental: {
    middlewareClientMaxBodySize: "256mb", // real-world CAD DXFs can be 70MB+
    proxyTimeout: 300000, // 5 min, comfortably above worst-case extraction
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" },
      { source: "/data/:path*", destination: "http://127.0.0.1:8000/data/:path*" },
    ];
  },
};

export default nextConfig;
