/** Merge into next.config.js — kit in public/driver-hiring/ */
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "https://krab-interviewer-bot.onrender.com/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;
