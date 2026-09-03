import { mount } from "svelte";
import TracesListPage from "./TracesListPage.svelte";
import "../../app.css";

const app = mount(TracesListPage, {
  target: document.getElementById("app-root")!,
});

export default app;
