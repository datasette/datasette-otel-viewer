import { mount } from "svelte";
import TraceDetailPage from "./TraceDetailPage.svelte";
import "../../app.css";

const app = mount(TraceDetailPage, {
  target: document.getElementById("app-root")!,
});

export default app;
