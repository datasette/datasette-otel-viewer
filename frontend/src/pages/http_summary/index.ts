import { mount } from "svelte";
import HttpSummaryPage from "./HttpSummaryPage.svelte";
import "../../app.css";

const app = mount(HttpSummaryPage, {
  target: document.getElementById("app-root")!,
});

export default app;
