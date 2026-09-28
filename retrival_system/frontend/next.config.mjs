const backend = new URL(process.env.SIGNORA_BACKEND_URL ?? 'http://127.0.0.1:8000');
if (!['http:', 'https:'].includes(backend.protocol) || backend.username || backend.password ||
    backend.pathname !== '/' || backend.search || backend.hash) {
  throw new Error('SIGNORA_BACKEND_URL must be an HTTP(S) origin without credentials or a path.');
}

export default {
  poweredByHeader: false,
  reactStrictMode: true,
  experimental: {
    // Match the backend's bounded multipart envelope, including metadata/headers.
    // The default 10 MB truncates the supplied GLBs before validation can run.
    proxyClientMaxBodySize: 160 * 1024 * 1024 + 65536,
    proxyTimeout: 180000,
  },
  async rewrites() {
    return [{ source: '/api/:path*', destination: `${backend.origin}/api/:path*` }];
  },
  async headers() {
    return [{ source: '/:path*', headers: [
      { key: 'X-Content-Type-Options', value: 'nosniff' },
      { key: 'X-Frame-Options', value: 'DENY' },
      { key: 'Referrer-Policy', value: 'no-referrer' },
      { key: 'Permissions-Policy', value: 'camera=(), microphone=(self), geolocation=()' },
    ] }];
  },
};
