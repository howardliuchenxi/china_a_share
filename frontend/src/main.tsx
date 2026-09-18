import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import ResearchVisualizationPage from "./ResearchVisualizationPage";
import "./styles.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Application root element is missing.");
}

const page = window.location.pathname.startsWith("/research/")
  ? <ResearchVisualizationPage />
  : <App />;

createRoot(root).render(
  <StrictMode>
    {page}
  </StrictMode>,
);
