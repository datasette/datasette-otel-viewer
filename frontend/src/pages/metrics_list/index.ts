import { mount } from "svelte";
import MetricsListPage from "./MetricsListPage.svelte";
import "../../app.css";

const app = mount(MetricsListPage, {
  target: document.getElementById("app-root")!,
});

export default app;
