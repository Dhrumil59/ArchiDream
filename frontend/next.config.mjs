/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" },
      { source: "/data/:path*", destination: "http://127.0.0.1:8000/data/:path*" },
    ];
  },
};

export default nextConfig;
