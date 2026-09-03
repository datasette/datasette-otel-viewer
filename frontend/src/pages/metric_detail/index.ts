import { mount } from "svelte";
import MetricDetailPage from "./MetricDetailPage.svelte";
import "../../app.css";

const app = mount(MetricDetailPage, {
  target: document.getElementById("app-root")!,
});

export default app;
