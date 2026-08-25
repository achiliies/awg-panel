/// <reference types="vite/client" />

/**
 * Injected by Django's SPA template in production and by index.html during
 * `npm run dev`. It is the only build-time-unknown the app needs: the panel is
 * mounted under a random secret base path chosen at install time.
 */
interface AwgBootstrap {
  basePath: string;
  version: string;
}

interface Window {
  __AWG__: AwgBootstrap;
}
