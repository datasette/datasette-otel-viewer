import { mount } from "svelte";
import SpansSummaryPage from "./SpansSummaryPage.svelte";
import "../../app.css";

const app = mount(SpansSummaryPage, {
  target: document.getElementById("app-root")!,
});

export default app;
