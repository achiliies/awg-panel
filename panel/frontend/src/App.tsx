import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { RequireAuth } from "@/components/RequireAuth";
import { AppShell } from "@/layout/AppShell";
import { bootstrap } from "@/api/client";
import About from "@/pages/About";
import ApiDocs from "@/pages/ApiDocs";
import Clients from "@/pages/Clients";
import Dashboard from "@/pages/Dashboard";
import Login from "@/pages/Login";
import Obfuscation from "@/pages/Obfuscation";
import ServerConfig from "@/pages/ServerConfig";
import Settings from "@/pages/Settings";
import Statistics from "@/pages/Statistics";
import Support from "@/pages/Support";

/**
 * The route table.
 *
 * The router's basename is the secret prefix the panel was installed under, so
 * every <Link> and every history entry stays inside it. Nothing below may
 * hardcode a path that starts at the server root.
 *
 * Document language and direction are not set here: src/i18n owns them, so they
 * are already right for the first paint, including the error boundary.
 */
export default function App(): JSX.Element {
  return (
    <BrowserRouter basename={bootstrap.basePath}>
      <Routes>
        <Route path="/login" element={<Login />} />

        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<Dashboard />} />
            <Route path="clients" element={<Clients />} />
            <Route path="server" element={<ServerConfig />} />
            <Route path="obfuscation" element={<Obfuscation />} />
            <Route path="statistics" element={<Statistics />} />
            <Route path="settings" element={<Settings />} />
            {/* "api-docs" and not "api": every path beginning `api/` is the
                API's, and a client-side route there would 404 on a refresh
                rather than come back as the SPA. See awgui/urls.SPA_FALLBACK. */}
            <Route path="api-docs" element={<ApiDocs />} />
            <Route path="about" element={<About />} />
            {/* Not in the rail's list: it is reached from the line at the foot
                of the sidebar, which is the only place the panel mentions it. */}
            <Route path="support" element={<Support />} />
          </Route>
        </Route>

        {/* A stale bookmark or a hand-typed path lands on the dashboard rather
            than a dead end; the panel has nothing outside these nine pages. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
