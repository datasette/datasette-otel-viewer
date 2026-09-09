import { mount } from "svelte";
import SqlSummaryPage from "./SqlSummaryPage.svelte";
import "../../app.css";

const app = mount(SqlSummaryPage, {
  target: document.getElementById("app-root")!,
});

export default app;
