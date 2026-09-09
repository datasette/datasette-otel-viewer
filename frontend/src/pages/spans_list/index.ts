import { mount } from "svelte";
import SpansListPage from "./SpansListPage.svelte";
import "../../app.css";

const app = mount(SpansListPage, {
  target: document.getElementById("app-root")!,
});

export default app;
