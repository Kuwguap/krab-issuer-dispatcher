import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./styles/global.css";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    {/* import.meta.env.BASE_URL is whatever vite's `base` was set to, so the
        router and the asset URLs can never disagree about the prefix. "/" at
        the root, "/drivers/" when the tag site serves it.

        The trailing slash has to come off. Vite's BASE_URL always ends in one,
        and a <Router basename="/drivers/"> refuses to match the URL "/drivers"
        -- it does not start with the basename -- so the router renders nothing
        at all and the page is simply blank. */}
    <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/+$/, "") || "/"}>
      <App />
    </BrowserRouter>
  </StrictMode>
);
